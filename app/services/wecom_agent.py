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

from app.config import settings
from app.database import SessionLocal
from app.models import (
    AdminUser, Customer, Pet, Visit, Wallet, Appointment, FollowUp,
)
from app.services import wecom_session as _sess
from app.services.wecom_client import send_app_message

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """你是大风动物医院的语音助手。员工通过企业微信发文字或语音（已转文字）给你，你帮他们查档案、新建病历、记主诉/体检/诊断。

【硬性原则】
1. 写操作（新建病历、写主诉/体检/诊断）必须先调对应工具拿到 summary，绝不直接说"已完成"，等系统让用户确认。
2. 不要做：开处方、开麻醉单、收款、退款、删除、作废。这些工具不存在，你也不要假装能做。
3. 找客户时优先按手机号；其次按姓名/宠物名；找到多个就列出来让用户挑。
4. 单次工具调用做一件事。比如「找李敏的大米新建病历」拆成：先 find_customer，确认是哪只宠物，再 create_visit。
5. 回复简洁，中文，必要时用 emoji（✓ ✋ ⚠ 📋）。不要长篇大论。

【写动作流程】
- create_visit / update_visit_field 会返回 "PENDING:" 开头的字符串
- 你要原样把 PENDING 后面的 summary 转述给用户，结尾问「确认执行吗？回复『确认』」
- 不要自作主张说"已完成"，让用户先确认

【上下文】
- 你的会话状态记录了 current_customer / current_pet / current_visit
- 用户说"主诉..."、"诊断..." 时如果 current_visit 存在，直接用那个 ID
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
        lines = [f"✓ {c.name or '客户#'+str(c.id)} · {c.phone or '—'}"]
        if pets:
            lines.append(f"名下 {len(pets)} 只宠物：")
            for i, p in enumerate(pets, 1):
                lines.append(f"  {i}. {_fmt_pet(p)}")
        return "\n".join(lines)
    # 多条命中
    lines = [f"找到 {len(custs)} 位客户："]
    for i, c in enumerate(custs, 1):
        lines.append(f"  {i}. {c.name or '客户#'+str(c.id)} · {c.phone or '—'}")
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
    lines = [f"📋 {c.name or '客户'} · {c.phone or '—'}"]
    if pets:
        lines.append(f"名下 {len(pets)} 只：")
        for p in pets:
            lines.append(f"  • {_fmt_pet(p)}")
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


def _execute_pending(db: Session, userid: str, pending: dict) -> str:
    action = pending.get("action")
    args = pending.get("args", {})
    if action == "create_visit":
        return _execute_create_visit(db, userid, args)
    if action == "update_visit_field":
        return _execute_update_visit_field(db, userid, args)
    return f"❌ 未知动作 {action}"


# ─────────────────────────────────────────────────────────
# LLM function calling
# ─────────────────────────────────────────────────────────

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "find_customer",
            "description": "按手机号、姓名或宠物名查客户。命中 1 个时自动聚焦。",
            "parameters": {"type": "object", "properties": {
                "query": {"type": "string", "description": "手机号/客户姓名/宠物名"},
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
]

TOOL_HANDLERS = {
    "find_customer":          tool_find_customer,
    "get_customer_profile":   tool_get_customer_profile,
    "get_recent_visits":      tool_get_recent_visits,
    "get_wallet":             tool_get_wallet,
    "get_today_appointments": tool_get_today_appointments,
    "create_visit":           tool_create_visit,
    "update_visit_field":     tool_update_visit_field,
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
    return (getattr(settings, "openai_model", "") or "gpt-4o-mini").strip()


def _run_llm_with_tools(db: Session, userid: str, user_text: str, ctx: dict) -> str:
    """走一轮 LLM with function calling，最多 5 跳。"""
    client = _llm_client()
    if client is None:
        return "❌ LLM 未配置（settings.openai_api_key 缺失）"

    # 把会话上下文塞到 system 提示里
    ctx_lines = []
    if ctx.get("current_customer_id"):
        ctx_lines.append(f"current_customer_id={ctx['current_customer_id']}")
    if ctx.get("current_pet_id"):
        ctx_lines.append(f"current_pet_id={ctx['current_pet_id']}")
    if ctx.get("current_visit_id"):
        ctx_lines.append(f"current_visit_id={ctx['current_visit_id']}")
    system = _SYSTEM_PROMPT
    if ctx_lines:
        system += "\n\n【当前上下文】\n" + "\n".join(ctx_lines)

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
