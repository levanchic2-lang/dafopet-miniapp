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
    ExamOrder,
)
from app.services import wecom_session as _sess
from app.services.wecom_client import send_app_message

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """你是大风动物医院的语音助手。员工通过企业微信发文字或语音（已转文字）给你，你帮他们查档案、新建病历、新建预约、开检查单、记主诉/体检/诊断。

【硬性原则】
1. 写操作（新建病历、写主诉/体检/诊断、新建预约、开检查单）必须先调对应工具拿到 summary，绝不直接说"已完成"，等系统让用户确认。
2. 不要做：开处方、开麻醉单、收款、退款、删除、作废、开美容/疫苗/驱虫单。这些工具暂未提供，不要假装能做。
3. 找客户时优先按手机号；其次按姓名/宠物名。find_customer 的 query 只能传**一个**关键词（号码 或 客户名 或 宠物名），不能拼接（不能传"13823137494 大米"）。
4. 单次工具调用做一件事。比如「找李敏的大米新建病历」拆成：先 find_customer(query="李敏") 拿到客户和宠物列表，再 create_visit(pet_id=大米的 id)。
5. 回复简洁，中文，必要时用 emoji（✓ ✋ ⚠ 📋）。不要长篇大论。
6. **优先使用上下文 ID**：上下文里给了 current_customer_id / current_pet_id 时直接用，**不要再调 find_customer 重新查**。如果 current_customer_id 已存在但用户提到具体宠物名而 current_pet_id 是 None（说明该客户名下多只），调 get_customer_profile(customer_id=current_customer_id) 拿到宠物列表 + pet_id，再按宠物名挑。
7. find_customer/get_customer_profile 的返回里会标 [pet_id=N]、(customer_id=N)，请直接读用这些 ID，不要再问用户。

【写动作流程】
- create_visit / update_visit_field / create_appointment / create_exam_order 会返回 "PENDING:" 开头的字符串
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
- 必须先有 current_visit_id（病历），没有就先 create_visit
- items 是字符串数组，每项是一个检查名称：「B超」「X光」「血常规」「生化」「粪检」「尿检」「显微镜检」等
- 用户说「开个 B 超和血常规」→ items=["B超", "血常规"]
- 单价和数量不填（医生会回系统补），agent 只负责拉好骨架
- 草稿态：单据 status=pending，会自动同步到收费单（不扣钱，等医生在系统确认）
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
        lines.append(f"  • {v.visit_date} · {diag}")
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
        return "❌ 没有当前病历，请先 create_visit 新建病历再开检查单"
    v = db.get(Visit, vid)
    if not v:
        return f"❌ 病历 #{vid} 不存在"
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
    return f"❌ 未知动作 {action}"


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

    for _ in range(5):
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
            return (msg.content or "").strip() or "（空回复）"
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
            try:
                args = json.loads(tc.function.arguments or "{}")
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
            # PENDING 结果直接返回给用户，不再让 LLM 续写
            if isinstance(result, str) and result.startswith("PENDING:"):
                return result[len("PENDING:"):]
            messages.append({
                "role": "tool", "tool_call_id": tc.id, "content": str(result),
            })
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
