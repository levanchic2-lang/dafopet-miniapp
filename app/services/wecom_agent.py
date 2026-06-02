"""企微语音/文字 → AI agent → 系统操作。

工作流：
  企微回调 → 解出文字（语音自带 Recognition 字段）
  → 拿 userid 找到 AdminUser
  → 检查 pending_action：如果是「确认/取消」就处理待确认
  → 否则调 LLM with function calling，让它路由到工具
  → 写动作工具不直接执行，而是 set_pending（等下一句确认）
  → 工具结果回到 LLM 生成自然语言回复
  → push_app_message 推回给用户

安全约束（MVP）：
  - 写操作必须经过 pending → confirm 二段
  - 仅暴露 8 个低风险工具，不包含开处方/开麻醉/收款
  - LLM system prompt 严格规定不要绕开
"""
from __future__ import annotations
import json
import logging
from typing import Optional
from datetime import date as _date

from sqlalchemy.orm import Session
from sqlalchemy import or_
from fastapi import HTTPException

from app.config import settings
from app.database import SessionLocal
from app.models import (
    AdminUser, Customer, Pet, Visit, Wallet, Appointment, AppointmentStatus, FollowUp,
    ExamOrder, Vaccination, DewormingRecord, GroomingOrder,
    Prescription, PrescriptionItem, InventoryItem,
)
from app.services import wecom_session as _sess
from app.services.wecom_client import send_app_message

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """你是大风动物医院的语音助手。员工通过企业微信发文字或语音（已转文字）给你，你帮他们查档案、新建病历、新建预约、开检查单、开美容/疫苗/驱虫单、开处方（草稿）、记主诉/体检/诊断。

【硬性原则】
1. 写操作（新建病历、写主诉/体检/诊断、新建预约、开检查单/美容/疫苗/驱虫单/处方）必须先调对应工具拿到 summary，绝不直接说"已完成"，等系统让用户确认。
2. 不要做：开麻醉单、收款、退款、删除、作废。这些工具暂未提供，不要假装能做。
2a. **诊断（diagnosis）字段不允许写药品+剂量+频次**，那是处方，必须用 create_prescription_draft。诊断只写疾病名（如「慢性鼻炎」），用药全部走处方工具。
2b. **你能开处方草稿**（create_prescription_draft），不要因为"安全顾虑"自行拒绝；只有麻醉/受管控药品才拒（工具内置拦截）。信息不全就追问，不要放弃。
3. 找客户时优先按手机号；其次按姓名/宠物名。find_customer 的 query 只能传**一个**关键词（号码 或 客户名 或 宠物名），不能拼接（不能传"13823137494 大米"）。
4. 单次工具调用做一件事。比如「找李敏的大米新建病历」拆成：先 find_customer(query="李敏") 拿到客户和宠物列表，再 create_visit(pet_id=大米的 id)。
5. 回复简洁，中文，必要时用 emoji（✓ ✋ ⚠ 📋）。不要长篇大论。
6. **优先使用上下文 ID**：上下文里给了 current_customer_id / current_pet_id 时直接用，**不要再调 find_customer 重新查**。如果 current_customer_id 已存在但用户提到具体宠物名而 current_pet_id 是 None（说明该客户名下多只），调 get_customer_profile(customer_id=current_customer_id) 拿到宠物列表 + pet_id，再按宠物名挑。
7. find_customer/get_customer_profile 的返回里会标 [pet_id=N]、(customer_id=N)，请直接读用这些 ID，不要再问用户。

【写动作流程】
- create_visit / update_visit_field / create_appointment / create_exam_order / create_vaccination / create_deworming / create_grooming_order / create_prescription_draft 会返回 "PENDING:" 开头的字符串
- 你要原样把 PENDING 后面的 summary 转述给用户，结尾问「确认执行吗？回复『确认』」
- 不要自作主张说"已完成"，让用户先确认

【上下文】
- 你的会话状态记录了 current_customer / current_pet / current_visit
- 用户说"主诉..."、"诊断..." 时如果 current_visit 存在，直接用那个 ID

【预约规则（create_appointment）】
- 类目（category）：outpatient(门诊) / tnr(绝育) / surgery(手术) / beauty(美容)
- 日期必须是 YYYY-MM-DD 绝对日期，相对词「明天/后天/下周三」由你换算（参考系统提示里的今天日期）
- 时间必须是 HH:MM 24 小时制；门诊只能 10:00-21:00，TNR 只能 11:00-18:00
- 门店：员工已绑定门店时不需要问；超管必须明确指定「东环店」或「横岗店」
- 美容（beauty）必须让用户说出具体服务项（洗澡 / 造型 / 剪指甲...），不能默认
- 信息不全时先问清楚，不要瞎填

【检查单规则（create_exam_order）】
- 工具会自动找今天的病历；今天没有会列最近 3 次让用户选；不要直接 create_visit
- items 是字符串数组，每项是一个检查名称：「B超」「X光」「血常规」「生化」「粪检」「尿检」「显微镜检」等
- 用户说「开个 B 超和血常规」→ items=["B超", "血常规"]
- 单价和数量不填（医生会回系统补），agent 只负责拉好骨架
- 草稿态：单据 status=pending，会自动同步到收费单（不扣钱，等医生在系统确认）

【疫苗/驱虫/美容单规则】
- 都需要 current_pet_id（先 find_customer 锁定宠物）
- create_vaccination：vaccine_type 必填，枚举 rabies(狂犬) / combo_3(三联) / combo_6(六联) / canine_8(犬八联) / other；vaccine_name 是品牌（妙三多/英特威/卫佳等）；日期默认今天
- create_deworming：product_name 必填（海乐妙/拜耳大宠爱/福来恩等）；deworm_type 默认 external（体外），用户明确说体内/内外同驱再改
- create_grooming_order：services 是服务项数组，如 ["洗澡", "造型"]；门店从员工绑定取；价格/数量留给医生回系统补
- 草稿态：不扣库存、不收款；医生回系统补批号/价格/上次记录核对

【处方规则（create_prescription_draft）· 最高风险】
- 这是合法工具，用户让你开处方时**必须调用它**，不要自行说「无法开处方/暂时不能」之类拒绝。
- 拒绝场景只有 1 个：用户明确说要开麻醉药/受管控药品（工具内部也会拦）
- 工具会自动找今天的病历；今天没有会列最近 3 次让用户选。用户说「用病历 #N」时把 N 当 visit_id 传；说「新建病历」时再调 create_visit
- drugs 是对象数组，每项必须完整：drug_name + dose_amount + dose_unit + times_per_day + duration_days
  例：「多西环素，每次 10mg/kg，一天 2 次，连服 7 天」→
    {"drug_name":"多西环素","dose_amount":10,"dose_unit":"mg/kg","times_per_day":2,"duration_days":"7"}
- **信息不全时追问用户，不要直接拒绝**。例：用户只说「强力素 50mg 一天两次」，缺连服天数 → 回「请问连服几天？」让用户补
- 一律 status=draft（草稿），不扣库存、不收款；医生回系统选库存品目 + 改 issued 才生效
- 复诵时把每个药完整念一遍：药名+单次剂量+频次+天数。语音误识别这块容错为 0，必须用户明确说「确认」
"""


# ─────────────────────────────────────────────────────────
# 工具实现（数据库操作）
# ─────────────────────────────────────────────────────────

def _admin_store_of(db: Session, userid: str) -> str:
    """从企微 userid 找 AdminUser → 该员工的门店短名（空 = 超管）。"""
    if not userid:
        return ""
    u = db.query(AdminUser).filter(AdminUser.wecom_userid == userid).first()
    if not u:
        return ""
    if u.role == "superadmin":
        return ""
    return u.store or ""


def _fmt_pet(p: "Pet") -> str:
    sp = {"cat": "猫", "dog": "犬"}.get(p.species, p.species or "")
    parts = [p.name or "未命名"]
    if sp or p.breed:
        parts.append(f"({sp}{'·'+p.breed if p.breed else ''})")
    return " ".join(parts)


def tool_find_customer(db: Session, userid: str, query: str) -> str:
    """按手机号/姓名/宠物名 查客户。返回多条概览（最多 5）。"""
    q = (query or "").strip()
    if not q:
        return "请提供手机号、姓名或宠物名"
    # 先按手机号或姓名匹配 Customer
    custs = db.query(Customer).filter(
        or_(Customer.phone.ilike(f"%{q}%"), Customer.name.ilike(f"%{q}%"))
    ).limit(5).all()
    # 再按宠物名找到所属客户
    if not custs:
        pets = db.query(Pet).filter(Pet.name.ilike(f"%{q}%")).limit(5).all()
        cust_ids = {p.customer_id for p in pets if p.customer_id}
        if cust_ids:
            custs = db.query(Customer).filter(Customer.id.in_(cust_ids)).all()
    if not custs:
        return f"没找到「{q}」相关客户"
    if len(custs) == 1:
        c = custs[0]
        pets = db.query(Pet).filter(Pet.customer_id == c.id).all()
        _sess.touch(userid, current_customer_id=c.id,
                    current_pet_id=(pets[0].id if len(pets) == 1 else None))
        lines = [f"✓ {c.name or '客户#'+str(c.id)} (customer_id={c.id}) · {c.phone or '—'}"]
        if pets:
            lines.append(f"名下 {len(pets)} 只宠物：")
            for i, p in enumerate(pets, 1):
                lines.append(f"  {i}. {_fmt_pet(p)} [pet_id={p.id}]")
        return "\n".join(lines)
    # 多条命中
    lines = [f"找到 {len(custs)} 位客户："]
    for i, c in enumerate(custs, 1):
        lines.append(f"  {i}. {c.name or '客户#'+str(c.id)} (customer_id={c.id}) · {c.phone or '—'}")
    lines.append("说「1」或姓名继续")
    return "\n".join(lines)


def tool_get_customer_profile(db: Session, userid: str, customer_id: int) -> str:
    """看客户详细信息：宠物 / 钱包 / 最近就诊。"""
    c = db.get(Customer, customer_id) if customer_id else None
    if not c:
        return "客户不存在"
    _sess.touch(userid, current_customer_id=c.id)
    pets = db.query(Pet).filter(Pet.customer_id == c.id).all()
    wallet = db.query(Wallet).filter(Wallet.customer_id == c.id).first()
    last_visit = db.query(Visit).filter(Visit.customer_id == c.id)\
        .order_by(Visit.visit_date.desc(), Visit.id.desc()).first()
    lines = [f"📋 {c.name or '客户'} (customer_id={c.id}) · {c.phone or '—'}"]
    if pets:
        lines.append(f"名下 {len(pets)} 只：")
        for p in pets:
            lines.append(f"  • {_fmt_pet(p)} [pet_id={p.id}]")
    if wallet and (wallet.balance or 0) > 0:
        lines.append(f"💰 钱包 ¥{wallet.balance:.2f}")
    if last_visit:
        diag = (last_visit.diagnosis or last_visit.chief_complaint or "—")[:30]
        lines.append(f"📌 上次就诊 {last_visit.visit_date} · {diag}")
    return "\n".join(lines)


def tool_get_recent_visits(db: Session, userid: str, pet_id: int, limit: int = 3) -> str:
    """该宠物最近 N 次就诊摘要。"""
    pet = db.get(Pet, pet_id) if pet_id else None
    if not pet:
        return "宠物不存在"
    _sess.touch(userid, current_pet_id=pet.id)
    visits = db.query(Visit).filter(Visit.pet_id == pet.id)\
        .order_by(Visit.visit_date.desc(), Visit.id.desc()).limit(int(limit or 3)).all()
    if not visits:
        return f"{pet.name} 还没有就诊记录"
    lines = [f"📋 {pet.name} 最近 {len(visits)} 次就诊："]
    for v in visits:
        diag = (v.diagnosis or v.chief_complaint or "—")[:40]
        flag = "🔒" if (v.status or "open") == "closed" else ""
        lines.append(f"  • #{v.id} · {v.visit_date} · {diag} {flag}")
    return "\n".join(lines)


def tool_get_wallet(db: Session, userid: str, customer_id: int) -> str:
    """查客户钱包余额。"""
    w = db.query(Wallet).filter(Wallet.customer_id == customer_id).first()
    if not w:
        return "该客户暂无钱包"
    return f"💰 余额 ¥{w.balance:.2f}（累计充值 ¥{w.lifetime_recharge:.2f}）"


def tool_get_today_appointments(db: Session, userid: str) -> str:
    """看今日待确认/到店的预约（限当前员工门店）。"""
    store_short = _admin_store_of(db, userid)
    today = _date.today().isoformat()
    q = db.query(Appointment).filter(Appointment.appointment_date == today)
    if store_short:
        # Appointment.store 是全名，需要转换
        from app.main import _STORE_SHORT_TO_FULL
        full = _STORE_SHORT_TO_FULL.get(store_short, store_short)
        q = q.filter(Appointment.store == full)
    rows = q.order_by(Appointment.appointment_time).limit(10).all()
    if not rows:
        return "今日没有预约"
    lines = [f"📅 今日 {today} {len(rows)} 个预约："]
    for a in rows:
        t = a.appointment_time or "—"
        nm = a.applicant_name or a.pet_name or "客户"
        st = {"confirmed": "已确认", "pending": "待确认", "arrived": "已到店",
              "completed": "已完成", "cancelled": "已取消", "no_show": "爽约"}.get(a.status, a.status)
        lines.append(f"  {t} · {nm} · {st}")
    return "\n".join(lines)


def _resolve_or_list_visit(db: Session, pet_id: int) -> tuple[Optional[int], str]:
    """优先取今天的病历；没有就返回最近 3 次的清单字符串，让 LLM 让用户选。

    返回 (visit_id_or_None, message)
      - visit_id 非空：自动找到了今天的，可继续
      - visit_id None：message 是给用户的提示文本（最近就诊列表 + 操作指引）
    """
    today = _date.today().isoformat()
    # 只看 status='open'（结束的病历按合规不可改）
    today_v = db.query(Visit).filter(
        Visit.pet_id == pet_id,
        Visit.visit_date == today,
        Visit.status != "closed",
    ).order_by(Visit.id.desc()).first()
    if today_v:
        return today_v.id, ""
    recent = db.query(Visit).filter(
        Visit.pet_id == pet_id,
        Visit.status != "closed",
    ).order_by(Visit.id.desc()).limit(3).all()
    if not recent:
        return None, "❌ 该宠物没有未结束的病历，请说「新建病历」"
    lines = ["⚠ 今天还没有新病历。最近 3 次未结束就诊："]
    for v in recent:
        diag = (v.diagnosis or v.chief_complaint or "—")[:30]
        lines.append(f"  • #{v.id} · {v.visit_date} · {diag}")
    lines.append("→ 用最近这个请说「用病历 #N」；要新建请说「新建病历」")
    return None, "\n".join(lines)


# ─── 写操作（不直接执行，挂 pending） ───
def tool_create_visit(db: Session, userid: str, pet_id: int, visit_type: str = "outpatient") -> str:
    pet = db.get(Pet, pet_id) if pet_id else None
    if not pet:
        return "❌ 宠物不存在，先 find_customer"
    cust = db.get(Customer, pet.customer_id) if pet.customer_id else None
    cust_name = cust.name if cust else "客户"
    visit_type_zh = {"outpatient": "门诊", "surgery": "手术", "postop": "术后",
                     "followup": "复诊", "beauty": "美容"}.get(visit_type, visit_type)
    summary = f"✋ 即将为「{cust_name} · {pet.name}」创建{visit_type_zh}病历\n回复「确认」执行"
    _sess.set_pending(userid, "create_visit", {
        "pet_id": pet.id, "customer_id": pet.customer_id, "visit_type": visit_type,
    }, summary)
    return f"PENDING:{summary}"


def tool_update_visit_field(db: Session, userid: str, visit_id: int, field: str, value: str) -> str:
    """字段限定：chief_complaint(主诉) / physical_exam(体检) / diagnosis(诊断) / treatment_plan(医嘱) / notes(备注)"""
    ALLOWED = {"chief_complaint": "主诉", "physical_exam": "体检",
               "diagnosis": "诊断", "treatment_plan": "医嘱", "notes": "备注"}
    if field not in ALLOWED:
        return f"❌ 字段 {field} 不支持"
    v = db.get(Visit, visit_id) if visit_id else None
    if not v:
        return "❌ 病历不存在"
    if (v.status or "open") == "closed":
        return f"❌ 病历 #{visit_id} 已结束，不可修改（合规要求）。如需追加请说「新建病历」"
    value = (value or "").strip()
    if not value:
        return "❌ 内容不能为空"
    pet = db.get(Pet, v.pet_id) if v.pet_id else None
    summary = (f"✋ 即将写入病历 #{visit_id}（{pet.name if pet else ''}）{ALLOWED[field]}：\n"
               f"「{value[:80]}」\n回复「确认」执行")
    _sess.set_pending(userid, "update_visit_field", {
        "visit_id": visit_id, "field": field, "value": value,
    }, summary)
    return f"PENDING:{summary}"


_CAT_ZH = {"outpatient": "门诊", "tnr": "TNR 绝育", "surgery": "手术", "beauty": "美容"}
_CAT_DEFAULT_SERVICE = {"outpatient": "门诊就诊", "surgery": "手术"}
_CAT_DEFAULT_DURATION = {"outpatient": 30, "tnr": 60, "surgery": 90, "beauty": 60}


def tool_create_appointment(
    db: Session, userid: str, pet_id: int, category: str,
    appointment_date: str, appointment_time: str,
    service_name: Optional[str] = None,
    duration_minutes: Optional[int] = None,
    notes: str = "",
    store_short: Optional[str] = None,
) -> str:
    """新建预约。校验通过后挂 pending。"""
    if category not in _CAT_ZH:
        return f"❌ 类目 {category} 不支持（仅 outpatient/tnr/surgery/beauty）"
    pet = db.get(Pet, pet_id) if pet_id else None
    if not pet:
        return "❌ 宠物不存在，先 find_customer 锁定客户和宠物"
    cust = db.get(Customer, pet.customer_id) if pet.customer_id else None
    if not cust:
        return "❌ 宠物没有关联客户"
    if not cust.phone:
        return f"❌ 客户「{cust.name or '客户'}」没填手机号，无法创建预约（请先到系统补全档案）"

    # 门店：用户传 > 员工档案里的 store（即使是超管也用，作为「常驻门店」默认值）
    from app.main import _STORE_SHORT_TO_FULL
    u_row = db.query(AdminUser).filter(AdminUser.wecom_userid == userid).first()
    bound_store = (u_row.store if u_row else "") or ""
    use_short = (store_short or "").strip() or bound_store
    if not use_short:
        return "❌ 请告诉我哪个门店（东环店 / 横岗店）"
    store_full = _STORE_SHORT_TO_FULL.get(use_short)
    if not store_full:
        return f"❌ 不认识门店「{use_short}」，仅支持「东环店」或「横岗店」"

    # 服务名 + 时长默认值
    if category == "tnr":
        service_name = "TNR 手术安排"  # 强制
    if not service_name:
        service_name = _CAT_DEFAULT_SERVICE.get(category, "")
    if not service_name:
        return f"❌ {_CAT_ZH[category]}必须指定具体服务项（如「{'洗澡/造型' if category=='beauty' else '服务名'}」）"
    if not duration_minutes:
        duration_minutes = _CAT_DEFAULT_DURATION.get(category, 30)

    # 跑系统已有的所有校验（保持后台 / agent 一致）
    from app.main import (
        _assert_appointment_fields, _check_tnr_constraints,
        _check_outpatient_time, _check_slot_capacity, _check_appointment_conflict,
    )
    try:
        _assert_appointment_fields(
            category=category, service_name=service_name,
            customer_name=cust.name or "客户", phone=cust.phone,
            pet_name=pet.name or "宠物", pet_gender=pet.gender or "unknown",
            store=store_full, appointment_date=appointment_date,
            appointment_time=appointment_time, notes=notes,
            duration_minutes=str(duration_minutes),
        )
    except HTTPException as e:
        return f"❌ {e.detail}"

    err = _check_tnr_constraints(
        db, category=category, store=store_full,
        appointment_date=appointment_date, appointment_time=appointment_time,
        phone=cust.phone,
    )
    if err:
        return f"❌ {err}"
    err = _check_outpatient_time(category, appointment_time)
    if err:
        return f"❌ {err}"
    err = _check_slot_capacity(
        db, store=store_full, appointment_date=appointment_date,
        appointment_time=appointment_time, category=category, service_name=service_name,
    )
    if err:
        return f"❌ {err}"
    conflict = _check_appointment_conflict(
        db, store=store_full, appointment_date=appointment_date,
        appointment_time=appointment_time, duration_minutes=int(duration_minutes),
    )
    if conflict:
        return (f"❌ 时间冲突：{conflict.appointment_date} {conflict.appointment_time} "
                f"门店已有预约 #{conflict.id} {conflict.customer_name}，请换时间")

    # 复诵
    summary = (
        f"✋ 即将新建预约：\n"
        f"  客户：{cust.name or '客户'} · {cust.phone}\n"
        f"  宠物：{_fmt_pet(pet)}\n"
        f"  类目：{_CAT_ZH[category]} · {service_name}\n"
        f"  门店：{use_short}\n"
        f"  时间：{appointment_date} {appointment_time}（{duration_minutes} 分钟）\n"
        f"回复「确认」执行"
    )
    _sess.set_pending(userid, "create_appointment", {
        "pet_id": pet.id, "customer_id": cust.id,
        "customer_name": cust.name or "客户", "phone": cust.phone,
        "pet_name": pet.name or "宠物", "pet_gender": pet.gender or "unknown",
        "store": store_full, "category": category, "service_name": service_name,
        "appointment_date": appointment_date, "appointment_time": appointment_time,
        "duration_minutes": int(duration_minutes), "notes": (notes or "").strip(),
    }, summary)
    return f"PENDING:{summary}"


def tool_create_exam_order(
    db: Session, userid: str, items: list[str], notes: str = "",
    visit_id: Optional[int] = None,
) -> str:
    """开检查单：items 是检查项目名数组。挂 pending 等确认。"""
    ctx = _sess.get(userid)
    vid = visit_id or ctx.get("current_visit_id")
    if not vid:
        # 自动找今天的病历；没有就列最近 3 次让用户选
        pid = ctx.get("current_pet_id")
        if not pid:
            return "❌ 没有当前宠物，先 find_customer 锁定"
        vid, msg = _resolve_or_list_visit(db, pid)
        if not vid:
            return msg
        _sess.touch(userid, current_visit_id=vid)
    v = db.get(Visit, vid)
    if not v:
        return f"❌ 病历 #{vid} 不存在"
    if (v.status or "open") == "closed":
        return f"❌ 病历 #{vid} 已结束，不可追加；请说「新建病历」"
    if not isinstance(items, list) or not items:
        return "❌ 检查项目不能为空，请告诉我要开什么（如 B超 / 血常规 / X光）"
    # 清洗项目名
    clean = [str(x).strip() for x in items if str(x).strip()]
    if not clean:
        return "❌ 检查项目不能为空"
    if len(clean) > 20:
        return "❌ 一次最多 20 个项目"
    pet = db.get(Pet, v.pet_id) if v.pet_id else None
    summary = (
        f"✋ 即将为病历 #{vid}（{pet.name if pet else '宠物'}）开检查单：\n"
        + "\n".join(f"  • {n}" for n in clean)
        + (f"\n备注：{notes.strip()}" if notes and notes.strip() else "")
        + "\n（草稿态，价格/数量需医生回系统补；不扣库存/不收款）\n回复「确认」执行"
    )
    _sess.set_pending(userid, "create_exam_order", {
        "visit_id": vid, "items": clean, "notes": (notes or "").strip(),
    }, summary)
    return f"PENDING:{summary}"


_VACCINE_TYPE_ZH = {
    "rabies": "狂犬", "combo_3": "三联", "combo_6": "六联",
    "canine_8": "犬八联", "deworming": "驱虫", "other": "其他",
}
_DEWORM_TYPE_ZH = {"external": "体外", "internal": "体内", "combo": "内外同驱"}


def tool_create_vaccination(
    db: Session, userid: str,
    pet_id: Optional[int] = None,
    vaccine_type: str = "other",
    vaccine_name: str = "",
    vaccinated_date: Optional[str] = None,
    dose_number: int = 1,
    notes: str = "",
) -> str:
    ctx = _sess.get(userid)
    pid = pet_id or ctx.get("current_pet_id")
    if not pid:
        return "❌ 没有当前宠物，先 find_customer"
    pet = db.get(Pet, pid)
    if not pet:
        return "❌ 宠物不存在"
    if vaccine_type not in _VACCINE_TYPE_ZH:
        return f"❌ vaccine_type {vaccine_type} 不支持（rabies/combo_3/combo_6/canine_8/other）"
    if not (vaccine_name or "").strip() and vaccine_type != "rabies":
        return "❌ 请告诉我疫苗品牌（如妙三多/英特威/卫佳）"
    today = _date.today().isoformat()
    v_date = (vaccinated_date or "").strip() or today
    summary = (
        f"✋ 即将为 {pet.name or '宠物'} 登记疫苗：\n"
        f"  品种：{_VACCINE_TYPE_ZH[vaccine_type]}（{vaccine_name or '—'}）\n"
        f"  第 {dose_number} 针 · 接种日 {v_date}\n"
        f"  （草稿；批号/库存出库需医生回系统补）\n回复「确认」执行"
    )
    _sess.set_pending(userid, "create_vaccination", {
        "pet_id": pid, "customer_id": pet.customer_id,
        "vaccine_type": vaccine_type, "vaccine_name": (vaccine_name or "").strip(),
        "vaccinated_date": v_date, "dose_number": int(dose_number or 1),
        "notes": (notes or "").strip(),
    }, summary)
    return f"PENDING:{summary}"


def tool_create_deworming(
    db: Session, userid: str,
    pet_id: Optional[int] = None,
    product_name: str = "",
    deworm_date: Optional[str] = None,
    deworm_type: str = "external",
    weight_kg: float = 0.0,
    dose: str = "",
    notes: str = "",
) -> str:
    ctx = _sess.get(userid)
    pid = pet_id or ctx.get("current_pet_id")
    if not pid:
        return "❌ 没有当前宠物，先 find_customer"
    pet = db.get(Pet, pid)
    if not pet:
        return "❌ 宠物不存在"
    if not (product_name or "").strip():
        return "❌ 请告诉我驱虫药名（如海乐妙/拜耳大宠爱/福来恩）"
    if deworm_type not in _DEWORM_TYPE_ZH:
        return f"❌ deworm_type 取值 external/internal/combo"
    d_date = (deworm_date or "").strip() or _date.today().isoformat()
    summary = (
        f"✋ 即将为 {pet.name or '宠物'} 登记驱虫：\n"
        f"  药品：{product_name.strip()}（{_DEWORM_TYPE_ZH[deworm_type]}）\n"
        f"  日期：{d_date}\n"
        + (f"  体重：{weight_kg}kg  剂量：{dose}\n" if weight_kg or dose else "")
        + f"  （草稿；下次到期 / 批号需医生回系统补）\n回复「确认」执行"
    )
    _sess.set_pending(userid, "create_deworming", {
        "pet_id": pid, "customer_id": pet.customer_id,
        "product_name": product_name.strip(), "deworm_type": deworm_type,
        "deworm_date": d_date, "weight_kg": float(weight_kg or 0.0),
        "dose": (dose or "").strip(), "notes": (notes or "").strip(),
    }, summary)
    return f"PENDING:{summary}"


def tool_create_grooming_order(
    db: Session, userid: str,
    pet_id: Optional[int] = None,
    services: Optional[list] = None,
    groom_date: Optional[str] = None,
    notes: str = "",
) -> str:
    ctx = _sess.get(userid)
    pid = pet_id or ctx.get("current_pet_id")
    if not pid:
        return "❌ 没有当前宠物，先 find_customer"
    pet = db.get(Pet, pid)
    if not pet:
        return "❌ 宠物不存在"
    if not isinstance(services, list) or not services:
        return "❌ 请告诉我具体服务项（如 洗澡 / 造型 / 剪指甲）"
    clean = [str(s).strip() for s in services if str(s).strip()]
    if not clean:
        return "❌ 服务项不能为空"
    if len(clean) > 10:
        return "❌ 一单最多 10 个服务项"
    # 门店
    u_row = db.query(AdminUser).filter(AdminUser.wecom_userid == userid).first()
    store_short = (u_row.store if u_row else "") or ""
    g_date = (groom_date or "").strip() or _date.today().isoformat()
    summary = (
        f"✋ 即将为 {pet.name or '宠物'} 开美容单：\n"
        f"  服务：{'、'.join(clean)}\n"
        f"  日期：{g_date}\n"
        f"  门店：{store_short or '（未绑定）'}\n"
        f"  （草稿；价格/前后照片/总额需医生回系统补）\n回复「确认」执行"
    )
    _sess.set_pending(userid, "create_grooming_order", {
        "pet_id": pid, "customer_id": pet.customer_id,
        "services": clean, "groom_date": g_date,
        "store_short": store_short, "notes": (notes or "").strip(),
    }, summary)
    return f"PENDING:{summary}"


def _check_controlled_drug(db: Session, drug_name: str) -> Optional[str]:
    """返回受管控药品名（命中即拒绝），未命中返回 None。

    匹配：库存里 is_controlled=True 的品目，名字包含/被包含 drug_name 关键词。
    """
    name = (drug_name or "").strip()
    if not name:
        return None
    rows = db.query(InventoryItem).filter(
        InventoryItem.is_controlled == True,  # noqa: E712
        InventoryItem.is_active == True,      # noqa: E712
    ).all()
    for it in rows:
        if not it.name:
            continue
        if name in it.name or it.name in name:
            return it.name
    return None


def tool_create_prescription_draft(
    db: Session, userid: str,
    drugs: list,
    visit_id: Optional[int] = None,
    notes: str = "",
) -> str:
    """开处方草稿。drugs 是对象数组，每项必须有：
       drug_name / dose_amount / dose_unit / times_per_day / duration_days
       可选：instructions（用药提示，给客户看的）
    """
    ctx = _sess.get(userid)
    vid = visit_id or ctx.get("current_visit_id")
    if not vid:
        # 自动找今天的病历；没有就列最近 3 次让用户选
        pid = ctx.get("current_pet_id")
        if not pid:
            return "❌ 没有当前宠物，先 find_customer 锁定"
        vid, msg = _resolve_or_list_visit(db, pid)
        if not vid:
            return msg
        _sess.touch(userid, current_visit_id=vid)
    v = db.get(Visit, vid)
    if not v:
        return f"❌ 病历 #{vid} 不存在"
    if (v.status or "open") == "closed":
        return f"❌ 病历 #{vid} 已结束，不可追加；请说「新建病历」"
    if not isinstance(drugs, list) or not drugs:
        return "❌ 没有药品；请说清楚药名+单次剂量+频次+天数"
    if len(drugs) > 10:
        return "❌ 一张处方最多 10 个药"

    # 逐项校验 + 受管控检查
    cleaned = []
    for i, d in enumerate(drugs, 1):
        if not isinstance(d, dict):
            return f"❌ 第 {i} 项格式错"
        name = str(d.get("drug_name") or "").strip()
        dose_amount = d.get("dose_amount")
        dose_unit = str(d.get("dose_unit") or "").strip()
        tpd = d.get("times_per_day")
        days = d.get("duration_days")
        if not name:
            return f"❌ 第 {i} 项缺药名"
        if dose_amount in (None, "", 0, 0.0):
            return f"❌ 第 {i} 项「{name}」缺单次剂量（dose_amount）"
        if not dose_unit:
            return f"❌ 第 {i} 项「{name}」缺剂量单位（mg/kg、ml、片 等）"
        if not tpd:
            return f"❌ 第 {i} 项「{name}」缺频次（一天几次）"
        if not days:
            return f"❌ 第 {i} 项「{name}」缺连服天数"
        # 受管控药品拒绝
        controlled = _check_controlled_drug(db, name)
        if controlled:
            return (f"⚠ 第 {i} 项「{name}」匹配到受管控药品「{controlled}」"
                    f"（精神类/麻药），agent 不开此类，请医生在系统手动开处方")
        cleaned.append({
            "drug_name": name,
            "dose_amount": float(dose_amount),
            "dose_unit": dose_unit,
            "times_per_day": float(tpd),
            "duration_days": str(days).strip(),
            "instructions": str(d.get("instructions") or "").strip(),
        })

    pet = db.get(Pet, v.pet_id) if v.pet_id else None
    lines = [f"✋ 即将开处方（草稿 · 不扣库存不收款）：",
             f"病历 #{vid}（{pet.name if pet else '宠物'}）"]
    for i, c in enumerate(cleaned, 1):
        lines.append(
            f"  {i}. {c['drug_name']}：单次 {c['dose_amount']}{c['dose_unit']}"
            f" · 一天 {int(c['times_per_day']) if c['times_per_day'].is_integer() else c['times_per_day']} 次"
            f" · 连服 {c['duration_days']} 天"
            + (f"（{c['instructions']}）" if c['instructions'] else "")
        )
    lines.append("⚠ 请逐条核对药名/剂量/频次/天数；任何一处不对就回「取消」")
    lines.append("无误回「确认」执行（医生需回系统绑库存品目后改 issued）")
    summary = "\n".join(lines)
    _sess.set_pending(userid, "create_prescription_draft", {
        "visit_id": vid, "customer_id": v.customer_id, "pet_id": v.pet_id,
        "drugs": cleaned, "notes": (notes or "").strip(),
    }, summary)
    return f"PENDING:{summary}"


# ─── 实际执行（pending → confirm 后调） ───
def _execute_create_visit(db: Session, userid: str, args: dict) -> str:
    """真正建病历。复用 main 里的 _sync_followup_for_visit 来衍生回访。"""
    from app.main import _sync_followup_for_visit, _resolve_vet_username
    pet_id = args.get("pet_id")
    customer_id = args.get("customer_id")
    visit_type = args.get("visit_type", "outpatient")
    if not pet_id:
        return "❌ pet_id 缺失"
    # 当前员工作为 vet
    u = db.query(AdminUser).filter(AdminUser.wecom_userid == userid).first()
    vet_name = (u.display_name if u and u.display_name else (u.username if u else "")) or ""
    pet = db.get(Pet, pet_id)
    today = _date.today().isoformat()
    v = Visit(
        customer_id=customer_id, pet_id=pet_id,
        visit_date=today, visit_type=visit_type,
        vet_name=vet_name,
        chief_complaint="", physical_exam="", diagnosis="",
        treatment_plan="", follow_up_note="",
        created_by=(u.username if u else "wecom_agent"),
    )
    db.add(v)
    db.commit()
    db.refresh(v)
    try:
        _sync_followup_for_visit(db, v)
        db.commit()
    except Exception as e:
        logger.warning(f"sync_followup after wecom create_visit failed: {e}")
    _sess.touch(userid, current_visit_id=v.id, current_pet_id=pet_id)
    return f"✓ 病历 #{v.id} 已为 {pet.name if pet else ''} 创建\n下一步说「主诉…」或「诊断…」"


def _execute_update_visit_field(db: Session, userid: str, args: dict) -> str:
    from app.main import _sync_followup_for_visit
    v = db.get(Visit, args.get("visit_id"))
    if not v:
        return "❌ 病历不存在"
    field = args.get("field")
    value = args.get("value", "")
    setattr(v, field, value)
    db.commit()
    if field == "diagnosis":
        try:
            _sync_followup_for_visit(db, v)
            db.commit()
        except Exception as e:
            logger.warning(f"sync_followup after wecom update failed: {e}")
    LABEL = {"chief_complaint": "主诉", "physical_exam": "体检",
             "diagnosis": "诊断", "treatment_plan": "医嘱", "notes": "备注"}
    return f"✓ {LABEL.get(field, field)} 已保存到病历 #{v.id}"


def _execute_create_appointment(db: Session, userid: str, args: dict) -> str:
    u = db.query(AdminUser).filter(AdminUser.wecom_userid == userid).first()
    row = Appointment(
        category=args["category"],
        status=AppointmentStatus.pending.value,
        service_name=args["service_name"],
        customer_name=args["customer_name"],
        phone=args["phone"],
        pet_name=args["pet_name"],
        pet_gender=args["pet_gender"],
        store=args["store"],
        appointment_date=args["appointment_date"],
        appointment_time=args["appointment_time"],
        duration_minutes=int(args.get("duration_minutes") or 30),
        notes=args.get("notes", ""),
        source="wecom_agent",
        customer_id=args.get("customer_id"),
        pet_id=args.get("pet_id"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return (f"✓ 预约 #{row.id} 已创建\n"
            f"{args['appointment_date']} {args['appointment_time']} · "
            f"{args['service_name']} · {args['pet_name']}（待门店确认）")


def _execute_create_exam_order(db: Session, userid: str, args: dict) -> str:
    from app.main import _exam_order_token, _sync_visit_invoice
    u = db.query(AdminUser).filter(AdminUser.wecom_userid == userid).first()
    vid = args["visit_id"]
    items_payload = [
        {"name": n, "item_id": None, "qty": 1.0, "unit": "",
         "unit_price": 0.0, "subtotal": 0.0, "notes": ""}
        for n in args["items"]
    ]
    token, exp = _exam_order_token(db)
    order = ExamOrder(
        visit_id=vid,
        items_json=json.dumps(items_payload, ensure_ascii=False),
        notes=args.get("notes", ""),
        upload_token=token,
        token_expires_at=exp,
        created_by=(u.username if u else "wecom_agent"),
    )
    db.add(order)
    db.commit()
    db.refresh(order)
    # 同步到收费单（草稿态，不收款）
    try:
        _sync_visit_invoice(db, vid, (u.username if u else "wecom_agent"))
        db.commit()
    except Exception as e:
        logger.warning(f"sync_invoice after wecom create_exam_order failed: {e}")
    items_zh = "、".join(args["items"])
    return (f"✓ 检查单 #{order.id} 已创建（草稿）\n"
            f"项目：{items_zh}\n"
            f"请回系统补价格/数量并上传报告")


def _execute_pending(db: Session, userid: str, pending: dict) -> str:
    action = pending.get("action")
    args = pending.get("args", {})
    if action == "create_visit":
        return _execute_create_visit(db, userid, args)
    if action == "update_visit_field":
        return _execute_update_visit_field(db, userid, args)
    if action == "create_appointment":
        return _execute_create_appointment(db, userid, args)
    if action == "create_exam_order":
        return _execute_create_exam_order(db, userid, args)
    if action == "create_vaccination":
        return _execute_create_vaccination(db, userid, args)
    if action == "create_deworming":
        return _execute_create_deworming(db, userid, args)
    if action == "create_grooming_order":
        return _execute_create_grooming_order(db, userid, args)
    if action == "create_prescription_draft":
        return _execute_create_prescription_draft(db, userid, args)
    return f"❌ 未知动作 {action}"


def _execute_create_prescription_draft(db: Session, userid: str, args: dict) -> str:
    u = db.query(AdminUser).filter(AdminUser.wecom_userid == userid).first()
    vet_name = (u.display_name if u and u.display_name else (u.username if u else "")) or ""
    presc = Prescription(
        visit_id=args["visit_id"],
        customer_id=args.get("customer_id"),
        pet_id=args.get("pet_id"),
        prescribed_date=_date.today().isoformat(),
        vet_name=vet_name,
        status="draft",            # 草稿 → sync_invoice 跳过、不扣钱
        total_amount=0.0,
        notes=args.get("notes", ""),
        created_by=(u.username if u else "wecom_agent"),
    )
    db.add(presc)
    db.flush()
    for c in args["drugs"]:
        # 注意：不绑 item_id，所以 _deduct_inventory 不会被触发
        db.add(PrescriptionItem(
            prescription_id=presc.id,
            drug_name=c["drug_name"],
            dose_amount=c["dose_amount"],
            dose_unit=c["dose_unit"],
            times_per_day=c["times_per_day"],
            duration_days=c["duration_days"],
            instructions=c.get("instructions", ""),
            # 兼容字段：dosage/frequency 用人话拼一份，方便老打印模板
            dosage=f"{c['dose_amount']}{c['dose_unit']}",
            frequency=f"一天{int(c['times_per_day']) if float(c['times_per_day']).is_integer() else c['times_per_day']}次",
        ))
    db.commit()
    db.refresh(presc)
    return (f"✓ 处方 #{presc.id} 已创建（草稿，{len(args['drugs'])} 个药）\n"
            f"请回系统绑库存品目 + 改 issued，库存才会扣减、收费单才会同步")


def _execute_create_vaccination(db: Session, userid: str, args: dict) -> str:
    u = db.query(AdminUser).filter(AdminUser.wecom_userid == userid).first()
    vet_name = (u.display_name if u and u.display_name else (u.username if u else "")) or ""
    row = Vaccination(
        pet_id=args["pet_id"], customer_id=args.get("customer_id"),
        vaccine_type=args["vaccine_type"], vaccine_name=args.get("vaccine_name", ""),
        vaccinated_date=args["vaccinated_date"], dose_number=int(args.get("dose_number") or 1),
        is_free=(args["vaccine_type"] == "rabies"),
        vet_name=vet_name, notes=args.get("notes", ""),
        created_by=(u.username if u else "wecom_agent"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    zh = _VACCINE_TYPE_ZH.get(args["vaccine_type"], args["vaccine_type"])
    return (f"✓ 疫苗记录 #{row.id} 已登记（草稿）\n"
            f"{zh}·{args.get('vaccine_name') or '—'}·第 {row.dose_number} 针\n"
            f"请回系统补批号/有效期/库存出库")


def _execute_create_deworming(db: Session, userid: str, args: dict) -> str:
    u = db.query(AdminUser).filter(AdminUser.wecom_userid == userid).first()
    vet_name = (u.display_name if u and u.display_name else (u.username if u else "")) or ""
    row = DewormingRecord(
        pet_id=args["pet_id"], customer_id=args.get("customer_id"),
        deworm_date=args["deworm_date"], deworm_type=args["deworm_type"],
        product_name=args["product_name"],
        weight_kg=float(args.get("weight_kg") or 0.0),
        dose=args.get("dose", ""), vet_name=vet_name,
        notes=args.get("notes", ""),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return (f"✓ 驱虫记录 #{row.id} 已登记（草稿）\n"
            f"{args['product_name']}·{_DEWORM_TYPE_ZH.get(args['deworm_type'], args['deworm_type'])}\n"
            f"请回系统补下次到期日 / 批号")


def _execute_create_grooming_order(db: Session, userid: str, args: dict) -> str:
    u = db.query(AdminUser).filter(AdminUser.wecom_userid == userid).first()
    services_payload = [
        {"name": n, "qty": 1.0, "price": 0.0, "subtotal": 0.0, "notes": ""}
        for n in args["services"]
    ]
    row = GroomingOrder(
        customer_id=args.get("customer_id"), pet_id=args["pet_id"],
        groom_date=args["groom_date"],
        groomer_name=(u.display_name if u and u.display_name else (u.username if u else "")) or "",
        services_json=json.dumps(services_payload, ensure_ascii=False),
        total_amount=0.0,
        store=args.get("store_short", ""),
        notes=args.get("notes", ""),
        created_by=(u.username if u else "wecom_agent"),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return (f"✓ 美容单 #{row.id} 已创建（草稿）\n"
            f"服务：{'、'.join(args['services'])}\n"
            f"请回系统补价格 / 前后照片 / 美容师")


# ─────────────────────────────────────────────────────────
# LLM function calling
# ─────────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "find_customer",
            "description": "按手机号、姓名或宠物名查客户。query 只能传一个关键词（号码 或 名字之一），禁止拼接。命中 1 个时自动聚焦。返回结果会标 customer_id / pet_id，请直接使用。",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "只填一个：手机号 或 客户姓名 或 宠物名（择一）"},
            }, "required": ["query"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_customer_profile",
            "description": "看客户详细：宠物列表 + 钱包 + 上次就诊。",
            "parameters": {"type": "object", "properties": {
                "customer_id": {"type": "integer"},
            }, "required": ["customer_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_recent_visits",
            "description": "看某只宠物最近 N 次就诊摘要（日期+诊断）。",
            "parameters": {"type": "object", "properties": {
                "pet_id": {"type": "integer"},
                "limit": {"type": "integer", "default": 3},
            }, "required": ["pet_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_wallet",
            "description": "查客户钱包余额。",
            "parameters": {"type": "object", "properties": {
                "customer_id": {"type": "integer"},
            }, "required": ["customer_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_today_appointments",
            "description": "今日预约列表（当前员工的门店）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_visit",
            "description": "为某只宠物创建新病历。写操作 - 会返回 PENDING，等用户确认。",
            "parameters": {"type": "object", "properties": {
                "pet_id": {"type": "integer"},
                "visit_type": {"type": "string", "enum": ["outpatient", "surgery", "postop", "followup", "beauty"], "default": "outpatient"},
            }, "required": ["pet_id"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_visit_field",
            "description": "把内容写入病历某字段。写操作 - 会返回 PENDING，等用户确认。",
            "parameters": {"type": "object", "properties": {
                "visit_id": {"type": "integer"},
                "field": {"type": "string", "enum": ["chief_complaint", "physical_exam", "diagnosis", "treatment_plan", "notes"]},
                "value": {"type": "string"},
            }, "required": ["visit_id", "field", "value"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_prescription_draft",
            "description": "开处方（草稿态，不扣库存不收款，医生回系统绑库存品目后改 issued 才生效）。写操作 - 返回 PENDING 等用户严格逐条核对后确认。受管控（麻药/精神类）自动拒绝。",
            "parameters": {"type": "object", "properties": {
                "drugs": {
                    "type": "array",
                    "description": "药品数组，每项必须完整：药名+单次剂量+剂量单位+频次+天数",
                    "items": {"type": "object", "properties": {
                        "drug_name":     {"type": "string", "description": "药品名（如 多西环素）"},
                        "dose_amount":   {"type": "number", "description": "单次剂量数字（如 10）"},
                        "dose_unit":     {"type": "string", "description": "单次剂量单位（mg/kg、mg、ml、片、粒）"},
                        "times_per_day": {"type": "number", "description": "一天几次（如 2）"},
                        "duration_days": {"type": "string", "description": "连服天数，可以是数字或描述（如 7 或「症状缓解为止」）"},
                        "instructions":  {"type": "string", "description": "用药提示，可选（如「饭后服用」）"},
                    }, "required": ["drug_name", "dose_amount", "dose_unit", "times_per_day", "duration_days"]},
                },
                "visit_id": {"type": "integer", "description": "病历 ID；留空用 current_visit_id"},
                "notes":    {"type": "string", "description": "整张处方备注，可选"},
            }, "required": ["drugs"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_vaccination",
            "description": "登记疫苗记录（草稿态，批号/库存出库需医生回系统补）。写操作 - 返回 PENDING 等确认。",
            "parameters": {"type": "object", "properties": {
                "pet_id":          {"type": "integer", "description": "宠物 ID；留空用 current_pet_id"},
                "vaccine_type":    {"type": "string", "enum": ["rabies", "combo_3", "combo_6", "canine_8", "other"]},
                "vaccine_name":    {"type": "string", "description": "品牌商品名（妙三多/英特威/卫佳等）；rabies 可空"},
                "vaccinated_date": {"type": "string", "description": "YYYY-MM-DD，留空用今天"},
                "dose_number":     {"type": "integer", "default": 1, "description": "第几针；加强针填 99"},
                "notes":           {"type": "string"},
            }, "required": ["vaccine_type"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_deworming",
            "description": "登记驱虫记录（草稿态，下次到期日/批号需医生回系统补）。写操作 - 返回 PENDING 等确认。",
            "parameters": {"type": "object", "properties": {
                "pet_id":       {"type": "integer", "description": "宠物 ID；留空用 current_pet_id"},
                "product_name": {"type": "string", "description": "驱虫药商品名（海乐妙/拜耳大宠爱/福来恩等）"},
                "deworm_date":  {"type": "string", "description": "YYYY-MM-DD，留空用今天"},
                "deworm_type":  {"type": "string", "enum": ["external", "internal", "combo"], "default": "external"},
                "weight_kg":    {"type": "number"},
                "dose":         {"type": "string"},
                "notes":        {"type": "string"},
            }, "required": ["product_name"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_grooming_order",
            "description": "开美容单（草稿态，价格/前后照片/总额需医生回系统补）。写操作 - 返回 PENDING 等确认。",
            "parameters": {"type": "object", "properties": {
                "pet_id":     {"type": "integer", "description": "宠物 ID；留空用 current_pet_id"},
                "services":   {"type": "array", "items": {"type": "string"},
                               "description": "服务项数组，如 [\"洗澡\", \"造型\"]"},
                "groom_date": {"type": "string", "description": "YYYY-MM-DD，留空用今天"},
                "notes":      {"type": "string"},
            }, "required": ["services"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_exam_order",
            "description": "在当前病历上开检查单（草稿态，需医生回系统补价格/上传报告）。写操作 - 返回 PENDING 等确认。",
            "parameters": {"type": "object", "properties": {
                "items":    {"type": "array", "items": {"type": "string"},
                             "description": "检查项目名数组，如 [\"B超\", \"血常规\"]"},
                "notes":    {"type": "string", "description": "整张单的备注，可选"},
                "visit_id": {"type": "integer", "description": "显式指定病历 ID；留空则用 current_visit_id"},
            }, "required": ["items"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_appointment",
            "description": "为某只宠物新建预约。写操作 - 会返回 PENDING 让用户确认；信息不全先问用户，不要瞎填。",
            "parameters": {"type": "object", "properties": {
                "pet_id":           {"type": "integer", "description": "宠物 ID（必填，先 find_customer）"},
                "category":         {"type": "string", "enum": ["outpatient", "tnr", "surgery", "beauty"]},
                "appointment_date": {"type": "string", "description": "YYYY-MM-DD（相对词如「明天」请换算成绝对日期）"},
                "appointment_time": {"type": "string", "description": "HH:MM 24 小时制"},
                "service_name":     {"type": "string", "description": "具体服务项；TNR 不用填（强制为 TNR 手术安排）；门诊/手术留空则用默认；美容必须明确（洗澡/造型/剪指甲等）"},
                "duration_minutes": {"type": "integer", "description": "时长分钟，留空用默认"},
                "notes":            {"type": "string"},
                "store_short":      {"type": "string", "enum": ["东环店", "横岗店"], "description": "门店；超管必填，员工留空用绑定门店"},
            }, "required": ["pet_id", "category", "appointment_date", "appointment_time"]},
        },
    },
]

TOOL_HANDLERS = {
    "find_customer":          tool_find_customer,
    "get_customer_profile":   tool_get_customer_profile,
    "get_recent_visits":      tool_get_recent_visits,
    "get_wallet":             tool_get_wallet,
    "get_today_appointments": tool_get_today_appointments,
    "create_visit":           tool_create_visit,
    "update_visit_field":     tool_update_visit_field,
    "create_appointment":     tool_create_appointment,
    "create_exam_order":      tool_create_exam_order,
    "create_vaccination":     tool_create_vaccination,
    "create_deworming":       tool_create_deworming,
    "create_grooming_order":  tool_create_grooming_order,
    "create_prescription_draft": tool_create_prescription_draft,
}


def _llm_client():
    """复用 settings.openai_*"""
    try:
        from openai import OpenAI
    except Exception:
        return None
    base_url = (getattr(settings, "openai_base_url", "") or "https://api.openai.com/v1").strip()
    api_key = (getattr(settings, "openai_api_key", "") or "").strip()
    if not api_key:
        return None
    return OpenAI(api_key=api_key, base_url=base_url)


def _llm_model() -> str:
    # 企微 agent 优先用专用模型（一般是更便宜的纯文本），没配再回退 openai_model
    return ((getattr(settings, "wecom_agent_model", "") or "").strip()
            or (getattr(settings, "openai_model", "") or "gpt-4o-mini").strip())


def _run_llm_with_tools(db: Session, userid: str, user_text: str, ctx: dict) -> str:
    """走一轮 LLM with function calling，最多 5 跳。"""
    client = _llm_client()
    if client is None:
        return "❌ LLM 未配置（settings.openai_api_key 缺失）"

    # 把会话上下文塞到 system 提示里
    _u_row = db.query(AdminUser).filter(AdminUser.wecom_userid == userid).first()
    _bound = (_u_row.store if _u_row else "") or ""
    ctx_lines = [f"today={_date.today().isoformat()}（你的默认门店：{_bound or '未绑定，需要时请向用户确认'}）"]
    if ctx.get("current_customer_id"):
        ctx_lines.append(f"current_customer_id={ctx['current_customer_id']}")
    if ctx.get("current_pet_id"):
        ctx_lines.append(f"current_pet_id={ctx['current_pet_id']}")
    if ctx.get("current_visit_id"):
        ctx_lines.append(f"current_visit_id={ctx['current_visit_id']}")
    system = _SYSTEM_PROMPT + "\n\n【当前上下文】\n" + "\n".join(ctx_lines)

    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_text},
    ]

    logger.info("[wecom agent] user=%s text=%r", userid, user_text)
    for hop in range(5):
        try:
            resp = client.chat.completions.create(
                model=_llm_model(),
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.2,
            )
        except Exception as e:
            logger.exception("LLM call failed")
            return f"❌ LLM 调用失败：{e}"
        msg = resp.choices[0].message
        if not msg.tool_calls:
            content = (msg.content or "").strip() or "（空回复）"
            logger.info("[wecom agent] hop=%d FINAL text=%r", hop, content[:200])
            return content
        # 执行工具
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [{"id": tc.id, "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                           for tc in msg.tool_calls],
        })
        for tc in msg.tool_calls:
            name = tc.function.name
            raw_args = tc.function.arguments or "{}"
            try:
                args = json.loads(raw_args)
            except Exception:
                args = {}
            handler = TOOL_HANDLERS.get(name)
            if not handler:
                result = f"❌ 未知工具 {name}"
            else:
                try:
                    result = handler(db, userid, **args)
                except Exception as e:
                    logger.exception(f"tool {name} crashed")
                    result = f"❌ 工具 {name} 异常：{e}"
            logger.info("[wecom agent] hop=%d tool=%s args=%s → %r",
                        hop, name, raw_args[:200], (str(result) or "")[:200])
            # PENDING 结果直接返回给用户，不再让 LLM 续写
            if isinstance(result, str) and result.startswith("PENDING:"):
                return result[len("PENDING:"):]
            messages.append({
                "role": "tool", "tool_call_id": tc.id, "content": str(result),
            })
    logger.warning("[wecom agent] hit 5-hop limit for user=%s text=%r", userid, user_text)
    return "（处理超出步数，请重新说一次）"


# ─────────────────────────────────────────────────────────
# 入口：处理一条入站消息
# ─────────────────────────────────────────────────────────

def handle_inbound_message(userid: str, text: str) -> Optional[str]:
    """主入口：返回应该 push 给用户的回复（None 表示忽略此消息）。"""
    if not userid or not text:
        return None
    text = text.strip()

    db = SessionLocal()
    try:
        # 1) 校验 userid 在我们后台有账号
        u = db.query(AdminUser).filter(AdminUser.wecom_userid == userid).first()
        if not u or not u.is_active:
            return "你的企微账号未绑定后台员工，无法使用语音助手"

        # 2) 处理 pending 确认/取消
        ctx = _sess.get(userid)
        if ctx.get("pending_action"):
            if _sess.is_confirm(text):
                pending = _sess.pop_pending(userid)
                return _execute_pending(db, userid, pending)
            if _sess.is_cancel(text):
                _sess.pop_pending(userid)
                return "已取消"
            # 既不是确认也不是取消 → 当作新指令，并清空 pending
            _sess.pop_pending(userid)

        # 3) 跑 LLM
        return _run_llm_with_tools(db, userid, text, _sess.get(userid))
    finally:
        db.close()


def push_reply(userid: str, text: str) -> None:
    """把回复推到用户企微（被动响应不发）。"""
    if not userid or not text:
        return
    try:
        res = send_app_message({
            "touser": userid,
            "msgtype": "text",
            "text": {"content": text},
        })
        if res.get("errcode") not in (0, "0", None):
            logger.error("[wecom agent push fail] %s", res)
    except Exception as e:
        logger.exception(f"push_reply failed: {e}")
