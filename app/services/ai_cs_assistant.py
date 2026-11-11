"""小程序客服的对话、工具和转人工。

写预约只走 app.services.ai_cs_booking 里的函数，不请求本机 HTTP。
客户明确回复确认之前，stage_* 只保存待办，不写预约表。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import (
    AiCsHandoff,
    AiCsMessage,
    AiCsPending,
    AiCsToolLog,
    Appointment,
    AppointmentCategory,
    AppointmentStatus,
    Customer,
    Pet,
)
from app.services.ai_cs_booking import (
    HORIZON_DAYS,
    LATE_PENALTY,
    AiCsError,
    _rules_payload,
    book_appointments,
    cancel_appointment,
    query_slots,
    reschedule_appointment,
)
from app.services.report_llm import report_llm_configured, report_text_client_model
from app.time_utils import beijing_now, beijing_today

logger = logging.getLogger(__name__)

KNOWLEDGE_DIR = Path(__file__).resolve().parent.parent / "ai_cs_knowledge"
MEMORY_TURNS = 12
MEMORY_HOURS = 24
RATE_LIMIT = 10
RATE_WINDOW_MINUTES = 10
GREETING_COOLDOWN_MINUTES = 30
PENDING_MINUTES = 30
LLM_TIMEOUT_SECONDS = 28
MAX_TOOL_HOPS = 4

PHONE_FALLBACK = (
    "这件事我交给同事。您也可以直接打电话："
    "17820633031（微信同号），或 0755-28704990。"
    "东环店手机 18026901718，横岗店手机 17820633031。"
    "如果宠物呼吸困难、抽搐、昏迷或大量出血，请马上拨打并到店。"
)
GREETING = (
    "您好，我是大风动物医院的客服助手。"
    "可以帮您查空档、预约、改期或取消，也可以说说体检、疫苗、绝育和美容怎么到院。"
    "要帮您提交预约，请先在小程序首页打开「绑定我的档案」。"
    "需要同事接待，直接回复「人工」。"
)
MEDIA_REPLY = (
    "我这边暂时看不了图片和小程序卡片。"
    "您用文字告诉我，是想预约、改期、取消，还是问到院须知，我来帮您。"
)
UNBOUND_REPLY = (
    "我还没对上您的档案，暂时不能提交预约、改期或取消。"
    "请打开小程序，在首页使用「绑定我的档案」，用在医院留的手机号收验证码。"
    "绑定好再告诉我门店、项目、宠物和时间。只问医院须知的话，现在就可以问。"
)
RATE_REPLY = "您发得有点快，我先等一会儿。请过几分钟再发，或直接拨打 17820633031。"
BIND_HINT = "请先在小程序首页使用「绑定我的档案」。"

_PHONE_RE = re.compile(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)")
_CONFIRM_RE = re.compile(
    r"^(确认|是|是的|好|好的|可以|对|对的|没错|没问题|行|同意|就这样|就这么定|ok|okay|嗯|要)[。.!！~～]?$",
    re.IGNORECASE,
)
_DECLINE_RE = re.compile(r"^(取消|不用了|算了|先不|不约了|不要了|先别|不行|不)[。.!！]?$")
_WRITE_INTENT = ("预约", "约个", "约一下", "帮我约", "改期", "改约", "换个时间", "取消预约", "取消这", "取消明天")
_FALSE_SUCCESS = ("已经帮您约", "已帮您约", "已经约好", "预约成功", "已经取消", "已为您取消", "已经改好", "改期成功", "已经改到")
_HUMAN_RE = re.compile(r"人工|转人工|找人工|转接")

_knowledge_cache: dict = {"stamp": None, "text": ""}
complete_override = None


def beijing_naive():
    return beijing_now().replace(tzinfo=None)


def redact(text: str) -> str:
    return _PHONE_RE.sub(r"\1****\2", text or "")


def mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) >= 7:
        return digits[:3] + "****" + digits[-4:]
    return ""


def clip(text: str, limit: int = 1000) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit] + "…"


def clip_wechat(text: str) -> str:
    raw = (text or "").strip().encode("utf-8")
    if len(raw) <= 1800:
        return (text or "").strip()
    return raw[:1800].decode("utf-8", errors="ignore") + "…"


def wants_human(text: str) -> bool:
    return bool(_HUMAN_RE.search(text or ""))


def is_confirm(text: str) -> bool:
    return bool(_CONFIRM_RE.match((text or "").strip()))


def is_decline(text: str) -> bool:
    return bool(_DECLINE_RE.match((text or "").strip()))


def load_knowledge() -> str:
    files = sorted(KNOWLEDGE_DIR.glob("*.md"))
    stamp = tuple((path.name, path.stat().st_mtime_ns) for path in files)
    if stamp == _knowledge_cache["stamp"]:
        return _knowledge_cache["text"]
    text = "\n\n".join(path.read_text(encoding="utf-8") for path in files)
    _knowledge_cache["stamp"] = stamp
    _knowledge_cache["text"] = text
    return text


def _main():
    import app.main as main
    return main


def customers_for_openid(db: Session, openid: str) -> list[Customer]:
    key = (openid or "").strip()
    if not key:
        return []
    return (
        db.query(Customer)
        .filter(Customer.wechat_openid == key)
        .order_by(Customer.id.asc())
        .all()
    )


def _pets(db: Session, customer_id: int) -> list[Pet]:
    return db.query(Pet).filter(Pet.customer_id == customer_id).order_by(Pet.id.asc()).all()


def species_zh(value: str) -> str:
    text = (value or "").strip().lower()
    if text in {"cat", "猫", "feline"}:
        return "猫"
    if text in {"dog", "犬", "狗", "canine"}:
        return "犬"
    return "宠物"


def store_short(value: str) -> str:
    text = value or ""
    if "横岗" in text:
        return "横岗店"
    if "东环" in text:
        return "东环店"
    return text or "门店"


def _profile_payload(db: Session, customer: Customer) -> dict:
    pets = []
    for pet in _pets(db, customer.id):
        pets.append({
            "pet_id": pet.id,
            "name": (pet.name or "").strip() or "未命名",
            "species": species_zh(pet.species or ""),
        })
    return {
        "customer_id": customer.id,
        "name": (customer.name or "").strip() or "未命名",
        "phone_masked": mask_phone(customer.phone or ""),
        "pets": pets,
    }


def describe_profiles(db: Session, openid: str) -> dict:
    rows = customers_for_openid(db, openid)
    profiles = [_profile_payload(db, row) for row in rows]
    return {
        "bound": bool(profiles),
        "profile_count": len(profiles),
        "need_profile_choice": len(profiles) > 1,
        "profiles": profiles,
        "bind_hint": "" if profiles else BIND_HINT,
    }


def _save_message(db: Session, openid: str, role: str, content: str, msg_type: str = "") -> None:
    db.add(AiCsMessage(
        openid=(openid or "")[:64],
        role=(role or "")[:20],
        content=clip(redact(content), 1000),
        msg_type=(msg_type or "")[:40],
        created_at=beijing_naive(),
    ))
    db.commit()


def _log_tool(db: Session, openid: str, name: str, args: dict, result: dict) -> None:
    safe_args = redact(json.dumps(args or {}, ensure_ascii=False, default=str))
    safe_result = redact(json.dumps(result or {}, ensure_ascii=False, default=str))
    db.add(AiCsToolLog(
        openid=(openid or "")[:64],
        tool_name=(name or "")[:60],
        arguments_json=clip(safe_args, 2000),
        result_summary=clip(safe_result, 2000),
        created_at=beijing_naive(),
    ))
    db.commit()


def record_handoff(db: Session, openid: str, reason: str, method: str, note: str = "") -> int:
    row = AiCsHandoff(
        openid=(openid or "")[:64],
        reason=(reason or "")[:80],
        method=(method or "")[:40],
        note=clip(redact(note), 500),
        staff_notify_status="pending_step5",
        created_at=beijing_naive(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    notify_staff_later(db, row.id)
    return row.id


def notify_staff_later(db: Session, handoff_id: int) -> None:
    """第 5 步在这里给值班同事发服务号模板消息。

    现在只把 ai_cs_handoffs.staff_notify_status 留在 pending_step5，不外发。
    """
    row = db.get(AiCsHandoff, handoff_id)
    if row is None:
        return
    if (row.staff_notify_status or "") != "pending_step5":
        row.staff_notify_status = "pending_step5"
        db.commit()
    logger.info("[ai-cs] handoff %s waiting for step-5 staff template", handoff_id)


def _rate_limited(db: Session, openid: str) -> bool:
    cutoff = beijing_naive() - timedelta(minutes=RATE_WINDOW_MINUTES)
    count = (
        db.query(AiCsMessage)
        .filter(
            AiCsMessage.openid == openid,
            AiCsMessage.role == "user",
            AiCsMessage.created_at >= cutoff,
        )
        .count()
    )
    return count > RATE_LIMIT


def recent_messages(db: Session, openid: str) -> list[AiCsMessage]:
    cutoff = beijing_naive() - timedelta(hours=MEMORY_HOURS)
    rows = (
        db.query(AiCsMessage)
        .filter(AiCsMessage.openid == openid, AiCsMessage.created_at >= cutoff)
        .order_by(AiCsMessage.id.desc())
        .limit(MEMORY_TURNS)
        .all()
    )
    rows.reverse()
    return rows


def _pending(db: Session, openid: str) -> AiCsPending | None:
    row = db.get(AiCsPending, openid)
    if row is None:
        return None
    if row.created_at and beijing_naive() - row.created_at > timedelta(minutes=PENDING_MINUTES):
        db.delete(row)
        db.commit()
        return None
    return row


def _clear_pending(db: Session, openid: str) -> None:
    row = db.get(AiCsPending, openid)
    if row is not None:
        db.delete(row)
        db.commit()


def _stage(db: Session, openid: str, action: str, payload: dict, script: str) -> None:
    row = db.get(AiCsPending, openid)
    if row is None:
        row = AiCsPending(openid=openid[:64])
        db.add(row)
    row.action = action[:20]
    row.payload_json = json.dumps(payload, ensure_ascii=False)
    row.script = script
    row.created_at = beijing_naive()
    db.commit()


def remember_gated(db: Session, openid: str) -> None:
    """白名单挡住时记下微信标识，方便把测试号抄进 AI_CS_TEST_OPENIDS。不保存原话。"""
    _save_message(db, openid, "user", "（这条没有交给 AI，已转给微信客服）", "gated")


def maybe_greet(db: Session, openid: str) -> str:
    cutoff = beijing_naive() - timedelta(minutes=GREETING_COOLDOWN_MINUTES)
    recent = (
        db.query(AiCsMessage)
        .filter(
            AiCsMessage.openid == openid,
            AiCsMessage.msg_type == "greeting",
            AiCsMessage.created_at >= cutoff,
        )
        .first()
    )
    if recent is not None:
        return ""
    _save_message(db, openid, "assistant", GREETING, "greeting")
    return GREETING


def media_reply(db: Session, openid: str) -> str:
    if _rate_limited(db, openid):
        _save_message(db, openid, "assistant", RATE_REPLY, "rate_limit")
        return RATE_REPLY
    _save_message(db, openid, "assistant", MEDIA_REPLY, "media")
    return MEDIA_REPLY


def _tool_list_services() -> dict:
    catalog = _main()._appointment_catalog()
    items = []
    for category in catalog.get("categories") or []:
        kind = {"outpatient": "outpatient", "surgery": "surgery", "beauty": "beauty"}.get(category.get("value"))
        if not kind:
            continue
        item = {
            "kind": kind,
            "label": category.get("label") or kind,
            "services": [service.get("name") for service in category.get("services") or [] if service.get("name")],
            "rules": _rules_payload(kind),
        }
        if kind == "beauty":
            item["sizes"] = category.get("size_options") or []
            item["coats"] = category.get("coat_options") or []
            item["addons"] = category.get("addon_options") or []
            item["addon_note"] = "去浮毛就是去底绒，按体重加时间；其他附加项目不加时间。价格只看知识库价目表。"
        items.append(item)
    items.append({
        "kind": "vaccine",
        "label": "疫苗驱虫",
        "services": ["狂犬疫苗", "犬八联", "猫三联", "疫苗/驱虫"],
        "rules": _rules_payload("vaccine"),
        "note": "要校验年龄和狂犬门店时，service_name 必须写成具体苗名。",
    })
    return {
        "services": items,
        "horizon_days": HORIZON_DAYS,
        "late_penalty": LATE_PENALTY,
        "tnr": "流浪猫 TNR 不在这里办理，请用小程序提交申请。",
        "prices": "不要在这个工具里报价。美容看知识库价目表，手术和药品价格尚未提供。",
    }


def _upcoming(db: Session, openid: str) -> list[dict]:
    ids = [row.id for row in customers_for_openid(db, openid)]
    if not ids:
        return []
    today = beijing_today().isoformat()
    rows = (
        db.query(Appointment)
        .filter(
            Appointment.customer_id.in_(ids),
            Appointment.status.in_((AppointmentStatus.pending.value, AppointmentStatus.confirmed.value)),
            Appointment.appointment_date >= today,
        )
        .order_by(Appointment.appointment_date.asc(), Appointment.appointment_time.asc(), Appointment.id.asc())
        .limit(8)
        .all()
    )
    status_zh = {
        AppointmentStatus.pending.value: "待确认",
        AppointmentStatus.confirmed.value: "已确认",
    }
    out = []
    for row in rows:
        out.append({
            "id": row.id,
            "customer_id": row.customer_id,
            "pet_id": row.pet_id,
            "pet_name": row.pet_name or "",
            "store": store_short(row.store or ""),
            "service_name": row.service_name or "",
            "category": row.category or "",
            "date": row.appointment_date or "",
            "time": row.appointment_time or "",
            "status": status_zh.get(row.status or "", row.status or ""),
        })
    return out


def _choose_customer(db: Session, openid: str, customer_id) -> tuple[Customer | None, dict | None]:
    profiles = describe_profiles(db, openid)
    if not profiles["bound"]:
        return None, {"ok": False, "code": "unbound", "message": UNBOUND_REPLY}
    rows = customers_for_openid(db, openid)
    if len(rows) == 1:
        if customer_id and int(customer_id) != rows[0].id:
            return None, {"ok": False, "message": "这份档案不在您当前绑定的微信下。", "profiles": profiles["profiles"]}
        return rows[0], None
    if not customer_id:
        return None, {
            "ok": False,
            "code": "need_profile",
            "message": "这个微信绑了多份档案，请先问客户是哪一只宠物。",
            "profiles": profiles["profiles"],
        }
    chosen = next((row for row in rows if row.id == int(customer_id)), None)
    if chosen is None:
        return None, {"ok": False, "message": "所选档案不属于这个微信。", "profiles": profiles["profiles"]}
    return chosen, None


def _choose_pet(db: Session, customer: Customer, pet_id) -> tuple[Pet | None, dict | None]:
    pets = _pets(db, customer.id)
    if not pets:
        return None, {"ok": False, "message": "这份档案里还没有宠物，请先到店建档。"}
    if len(pets) > 1 and not pet_id:
        return None, {
            "ok": False,
            "code": "need_pet",
            "message": "名下有多只宠物，请先问是哪一只。",
            "pets": [{"pet_id": pet.id, "name": pet.name or "未命名", "species": species_zh(pet.species or "")} for pet in pets],
        }
    if not pet_id:
        return pets[0], None
    chosen = next((pet for pet in pets if pet.id == int(pet_id)), None)
    if chosen is None:
        return None, {"ok": False, "message": "这只宠物不在所选档案里。"}
    return chosen, None


def _booking_script(store: str, service: str, pet: Pet, day: str, clock: str, extra: dict) -> str:
    lines = [
        "请核对后回复「确认」，我才会提交：",
        f"门店：{store_short(store)}",
        f"项目：{service}",
        f"宠物：{(pet.name or '未命名')}（{species_zh(pet.species or '')}）",
        f"时间：{day} {clock}",
    ]
    duration = extra.get("duration_minutes")
    if duration:
        lines.append(f"预计占用 {duration} 分钟，实际以到店为准。")
    if extra.get("arrival_note"):
        lines.append(str(extra["arrival_note"]))
    if extra.get("warning"):
        lines.append(str(extra["warning"]))
    lines.append("要改门店、项目、宠物或时间，直接说。先不要回复确认。")
    return "\n".join(lines)


def tool_stage_booking(db: Session, openid: str, args: dict) -> dict:
    customer, error = _choose_customer(db, openid, args.get("customer_id"))
    if error:
        return error
    pet, error = _choose_pet(db, customer, args.get("pet_id"))
    if error:
        return error
    kind = str(args.get("kind") or "").strip().lower()
    service = str(args.get("service_name") or "").strip()
    store = str(args.get("store") or "").strip()
    day = str(args.get("date") or "").strip()
    clock = str(args.get("time") or "").strip()
    if not all([kind, service, store, day, clock]):
        return {"ok": False, "message": "还缺门店、项目、日期或时间。问清楚后再确认，不要提交。"}
    if kind == "beauty" and (not str(args.get("pet_size") or "").strip() or not str(args.get("coat_length") or "").strip()):
        return {"ok": False, "message": "美容还需要体型和毛长（长毛或短毛）。先问客户，不要提交。"}
    try:
        looked = query_slots(
            db,
            store=store,
            day=day,
            kind=kind,
            service_name=service,
            pet_size=str(args.get("pet_size") or ""),
            coat_length=str(args.get("coat_length") or ""),
            addons=str(args.get("addons") or ""),
            weight_kg=args.get("weight_kg") or None,
            pet_id=pet.id,
            species=pet.species or "",
            birthday=pet.birthday_estimate or "",
        )
    except AiCsError as exc:
        return {"ok": False, "message": exc.message, "error_code": exc.code}
    if clock not in (looked.get("slots") or []):
        return {
            "ok": False,
            "message": "这个时间目前约不上。请改用下面的空档，不要说已经约好。",
            "slots": (looked.get("slots") or [])[:12],
        }
    payload = {
        "openid": openid,
        "customer_id": customer.id,
        "pet_id": pet.id,
        "store": looked.get("store") or store,
        "date": day,
        "time": clock,
        "kind": kind,
        "service_name": looked.get("service_name") or service,
        "pet_size": str(args.get("pet_size") or ""),
        "coat_length": str(args.get("coat_length") or ""),
        "addons": str(args.get("addons") or ""),
        "weight_kg": args.get("weight_kg") or "",
        "notes": clip(redact(str(args.get("notes") or "")), 200),
    }
    script = _booking_script(payload["store"], payload["service_name"], pet, day, clock, looked)
    _stage(db, openid, "book", payload, script)
    return {"ok": True, "staged": True, "script": script}


def _owned_upcoming(db: Session, openid: str, appointment_id: int) -> tuple[dict | None, dict | None]:
    rows = _upcoming(db, openid)
    chosen = next((row for row in rows if row["id"] == int(appointment_id or 0)), None)
    if chosen is None:
        return None, {"ok": False, "message": "没有找到这张还能改的预约。可以先调用 list_upcoming_appointments。"}
    if chosen["category"] == AppointmentCategory.tnr.value:
        return None, {"ok": False, "message": "TNR 不在这里改。请让客户用小程序，或回复「人工」。"}
    return chosen, None


def tool_stage_reschedule(db: Session, openid: str, args: dict) -> dict:
    current, error = _owned_upcoming(db, openid, int(args.get("appointment_id") or 0))
    if error:
        return error
    day = str(args.get("date") or "").strip()
    clock = str(args.get("time") or "").strip()
    store = str(args.get("store") or current["store"]).strip()
    if not day or not clock:
        return {"ok": False, "message": "改期还缺日期或时间。"}
    kind = "beauty" if current["category"] in {"beauty", "grooming", "washcare"} else (
        "surgery" if current["category"] == "surgery" else "outpatient"
    )
    if kind == "outpatient" and any(word in (current["service_name"] or "") for word in ("疫苗", "驱虫")):
        kind = "vaccine"
    try:
        looked = query_slots(
            db, store=store, day=day, kind=kind, service_name=current["service_name"], pet_id=current["pet_id"],
        )
    except AiCsError as exc:
        return {"ok": False, "message": exc.message, "error_code": exc.code}
    if clock not in (looked.get("slots") or []):
        return {"ok": False, "message": "这个时间目前改不过去。", "slots": (looked.get("slots") or [])[:12]}
    payload = {
        "openid": openid,
        "customer_id": current["customer_id"],
        "appointment_id": current["id"],
        "store": store,
        "date": day,
        "time": clock,
    }
    script = (
        "请核对后回复「确认」，我才会改期：\n"
        f"原预约：#{current['id']} {current['store']} {current['service_name']} {current['pet_name']} "
        f"{current['date']} {current['time']}\n"
        f"改到：{store_short(looked.get('store') or store)} {day} {clock}\n"
        "要改说法直接说。先不要回复确认。"
    )
    if looked.get("arrival_note"):
        script += "\n" + str(looked["arrival_note"])
    _stage(db, openid, "reschedule", payload, script)
    return {"ok": True, "staged": True, "script": script}


def tool_stage_cancel(db: Session, openid: str, args: dict) -> dict:
    current, error = _owned_upcoming(db, openid, int(args.get("appointment_id") or 0))
    if error:
        return error
    payload = {
        "openid": openid,
        "customer_id": current["customer_id"],
        "appointment_id": current["id"],
        "reason": clip(redact(str(args.get("reason") or "")), 200),
    }
    script = (
        "请核对后回复「确认」，我才会取消：\n"
        f"预约 #{current['id']}，{current['store']}，{current['service_name']}，"
        f"{current['pet_name']}，{current['date']} {current['time']}。\n"
        "当天可以取消，迟到不另外收费。先不要回复确认，除非信息没错。"
    )
    _stage(db, openid, "cancel", payload, script)
    return {"ok": True, "staged": True, "script": script}


def tool_query_slots(db: Session, args: dict) -> dict:
    try:
        result = query_slots(
            db,
            store=str(args.get("store") or ""),
            day=str(args.get("date") or ""),
            kind=str(args.get("kind") or ""),
            service_name=str(args.get("service_name") or ""),
            pet_size=str(args.get("pet_size") or ""),
            coat_length=str(args.get("coat_length") or ""),
            addons=str(args.get("addons") or ""),
            weight_kg=args.get("weight_kg") or None,
            pet_id=int(args.get("pet_id") or 0) or None,
            species=str(args.get("species") or ""),
            birthday=str(args.get("birthday") or ""),
        )
    except AiCsError as exc:
        return {"ok": False, "message": exc.message, "error_code": exc.code}
    slots = result.get("slots") or []
    return {
        "ok": True,
        "store": store_short(result.get("store") or ""),
        "date": result.get("date") or "",
        "service_name": result.get("service_name") or "",
        "duration_minutes": result.get("duration_minutes"),
        "slots": slots[:16],
        "slot_count": len(slots),
        "rules": result.get("rules") or {},
        "arrival_note": result.get("arrival_note") or "",
        "warning": result.get("warning") or "",
        "late_penalty": False,
    }


def execute_tool(db: Session, openid: str, name: str, args: dict) -> dict:
    args = args or {}
    try:
        if name == "list_services":
            result = {"ok": True, **_tool_list_services()}
        elif name == "list_profiles_and_pets":
            result = {"ok": True, **describe_profiles(db, openid)}
        elif name == "list_upcoming_appointments":
            profiles = describe_profiles(db, openid)
            if not profiles["bound"]:
                result = {"ok": False, "code": "unbound", "message": UNBOUND_REPLY}
            else:
                result = {"ok": True, "appointments": _upcoming(db, openid)}
        elif name == "query_slots":
            result = tool_query_slots(db, args)
        elif name == "stage_booking":
            result = tool_stage_booking(db, openid, args)
        elif name == "stage_reschedule":
            result = tool_stage_reschedule(db, openid, args)
        elif name == "stage_cancel":
            result = tool_stage_cancel(db, openid, args)
        elif name == "request_human":
            reason = clip(redact(str(args.get("reason") or "low_confidence")), 80)
            record_handoff(db, openid, reason or "low_confidence", "phone_fallback", reason)
            result = {"ok": True, "handoff": True, "message": PHONE_FALLBACK}
        else:
            result = {"ok": False, "message": "没有这个工具。"}
    except AiCsError as exc:
        result = {"ok": False, "message": exc.message, "error_code": exc.code}
    except Exception as exc:
        logger.exception("[ai-cs] tool %s failed", name)
        result = {"ok": False, "message": "刚才没有查成，请换个说法或转人工。"}
        result["error"] = type(exc).__name__
    _log_tool(db, openid, name, args, {key: value for key, value in result.items() if key != "script"})
    return result


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_services",
            "description": "列出可预约的项目和规则。不含价格。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_profiles_and_pets",
            "description": "查看这个微信绑定的档案和宠物。未绑定时会说明要去小程序绑定。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_upcoming_appointments",
            "description": "查看已绑定档案里尚未完成的预约。",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_slots",
            "description": "查询某一天还能约的开始时间。不要自己编时间。",
            "parameters": {
                "type": "object",
                "properties": {
                    "store": {"type": "string", "description": "横岗店或东环店"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "kind": {"type": "string", "enum": ["outpatient", "vaccine", "surgery", "beauty"]},
                    "service_name": {"type": "string"},
                    "pet_id": {"type": "integer"},
                    "pet_size": {"type": "string"},
                    "coat_length": {"type": "string"},
                    "addons": {"type": "string"},
                    "weight_kg": {"type": "number"},
                    "species": {"type": "string"},
                    "birthday": {"type": "string"},
                },
                "required": ["store", "date", "kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stage_booking",
            "description": "生成预约确认稿。不会写入预约。多份档案或多只宠物时必须先让客户选定。",
            "parameters": {
                "type": "object",
                "properties": {
                    "customer_id": {"type": "integer"},
                    "pet_id": {"type": "integer"},
                    "store": {"type": "string"},
                    "date": {"type": "string"},
                    "time": {"type": "string"},
                    "kind": {"type": "string", "enum": ["outpatient", "vaccine", "surgery", "beauty"]},
                    "service_name": {"type": "string"},
                    "pet_size": {"type": "string"},
                    "coat_length": {"type": "string"},
                    "addons": {"type": "string"},
                    "weight_kg": {"type": "number"},
                    "notes": {"type": "string"},
                },
                "required": ["store", "date", "time", "kind", "service_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stage_reschedule",
            "description": "生成改期确认稿。不会立刻改期。",
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "integer"},
                    "store": {"type": "string"},
                    "date": {"type": "string"},
                    "time": {"type": "string"},
                },
                "required": ["appointment_id", "date", "time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stage_cancel",
            "description": "生成取消确认稿。不会立刻取消。",
            "parameters": {
                "type": "object",
                "properties": {
                    "appointment_id": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["appointment_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_human",
            "description": "没有把握、知识库没有、或客户明确要人工时调用。不要猜价格和诊断。",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
]


def _system_prompt(db: Session, openid: str) -> str:
    now = beijing_now().strftime("%Y-%m-%d %H:%M")
    profiles = describe_profiles(db, openid)
    return (
        "你是大风动物医院小程序里的客服助手。用简体中文，短、暖、具体。一次说清一件事。\n"
        f"现在是北京时间 {now}。最远可约 {HORIZON_DAYS} 天。\n"
        "你只能依据下面的知识库，以及工具返回的档案、空档和预约。知识库没写的价格、套餐、优惠、政策，"
        "就说医院还没提供，不要编。标了「尚未提供」的内容保持空白。\n"
        "猫三联须知原文写「8周龄以上的犬」是笔误，只按猫来说。\n"
        "不诊断、不开药、不解读报告。急症（呼吸困难、抽搐、昏迷、大量出血、站不起来、中毒）"
        "第一句就请客户立刻打电话并到店，电话用知识库里的号码。\n"
        "预约、改期、取消必须先调用 stage 工具，把确认稿里的门店、项目、宠物、日期和时间给客户，"
        "等客户下一句明确同意。没有同意之前，禁止说已经约好、已经改期或已经取消。\n"
        "一次只确认一只宠物。多份档案或多只宠物时，先问是哪一只。\n"
        "未绑定档案时，预约类问题请客户去小程序使用「绑定我的档案」；一般须知仍可回答。\n"
        "没有把握或知识库没有时调用 request_human。\n\n"
        f"【当前绑定】\n{json.dumps(profiles, ensure_ascii=False)}\n\n"
        f"【知识库】\n{load_knowledge()}"
    )


async def complete_chat(messages: list[dict], tools: list[dict]):
    if complete_override is not None:
        return await complete_override(messages, tools)
    if not report_llm_configured():
        raise RuntimeError("llm_not_configured")
    client, model, _provider, is_reasoner = report_text_client_model()
    try:
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.3,
                max_tokens=2000 if is_reasoner else 500,
            ),
            timeout=LLM_TIMEOUT_SECONDS,
        )
    finally:
        closer = getattr(client, "close", None)
        if closer is not None:
            await closer()
    return response.choices[0].message


def _commit_pending(db: Session, openid: str) -> str:
    row = _pending(db, openid)
    if row is None:
        return "刚才那条核对已经过期了。您再说一下门店、项目、宠物和时间，我重新跟您确认。"
    try:
        payload = json.loads(row.payload_json or "{}")
    except json.JSONDecodeError:
        payload = {}
    action = row.action or ""
    _clear_pending(db, openid)
    try:
        if action == "book":
            result = book_appointments(db, payload)
            made = result.get("appointments") or []
            first = made[0] if made else {}
            text = (
                f"已经帮您约好了 #{first.get('id', '')}。"
                f"{store_short(first.get('store') or '')}，{first.get('service_name') or ''}，"
                f"{first.get('pet_name') or ''}，{first.get('appointment_date') or ''} {first.get('appointment_time') or ''}。"
            )
            if result.get("arrival_note"):
                text += str(result["arrival_note"])
            if result.get("warning"):
                text += str(result["warning"])
            text += "当天可以取消，迟到不处罚。"
            return text
        if action == "reschedule":
            result = reschedule_appointment(db, int(payload.get("appointment_id") or 0), payload)
            item = result.get("appointment") or {}
            text = (
                f"已经改到 {item.get('appointment_date') or ''} {item.get('appointment_time') or ''}，"
                f"{store_short(item.get('store') or '')}。"
            )
            if result.get("arrival_note"):
                text += str(result["arrival_note"])
            return text
        if action == "cancel":
            cancel_appointment(db, int(payload.get("appointment_id") or 0), payload)
            return "已经取消。当天取消也可以，没有迟到处罚。"
    except AiCsError as exc:
        return f"{exc.message} 这次没有提交成功。您可以换个时间再告诉我。"
    except Exception:
        logger.exception("[ai-cs] commit failed")
        return "这次没有提交成功。请稍后再试，或回复「人工」。"
    return "这次没有对上要办的事。您再说一下是预约、改期还是取消。"


async def handle_user_text(db: Session, openid: str, text: str) -> str:
    cleaned = clip(redact(text), 1000)
    if not cleaned:
        return ""
    _save_message(db, openid, "user", cleaned, "text")
    if _rate_limited(db, openid):
        _save_message(db, openid, "assistant", RATE_REPLY, "rate_limit")
        return RATE_REPLY

    pending = _pending(db, openid)
    if pending is not None and is_confirm(cleaned):
        reply = _commit_pending(db, openid)
        _save_message(db, openid, "assistant", reply, "commit")
        return reply
    if pending is not None and is_decline(cleaned):
        _clear_pending(db, openid)
        reply = "好的，这个先不办。想改时间或换项目，直接告诉我就行。"
        _save_message(db, openid, "assistant", reply, "decline")
        return reply
    if pending is not None:
        _clear_pending(db, openid)

    if not customers_for_openid(db, openid) and any(word in cleaned for word in _WRITE_INTENT):
        _save_message(db, openid, "assistant", UNBOUND_REPLY, "unbound")
        return UNBOUND_REPLY

    reply = await _llm_turn(db, openid, cleaned)
    _save_message(db, openid, "assistant", reply, "assistant")
    return reply


async def _llm_turn(db: Session, openid: str, text: str) -> str:
    history = recent_messages(db, openid)
    messages = [{"role": "system", "content": _system_prompt(db, openid)}]
    for item in history:
        if item.role in {"user", "assistant"} and item.content:
            messages.append({"role": item.role, "content": item.content})
    if not history or history[-1].content != text:
        messages.append({"role": "user", "content": text})

    staged_script = ""
    handoff_text = ""
    try:
        for _hop in range(MAX_TOOL_HOPS):
            message = await complete_chat(messages, TOOLS)
            tool_calls = list(getattr(message, "tool_calls", None) or [])
            if not tool_calls:
                content = (getattr(message, "content", None) or "").strip()
                if handoff_text:
                    return handoff_text
                if staged_script:
                    return staged_script
                if not content or any(phrase in content for phrase in _FALSE_SUCCESS):
                    record_handoff(db, openid, "low_confidence", "phone_fallback", "模型没有给出可靠回复")
                    return PHONE_FALLBACK
                return content
            messages.append({
                "role": "assistant",
                "content": getattr(message, "content", None) or "",
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {"name": call.function.name, "arguments": call.function.arguments or "{}"},
                    }
                    for call in tool_calls
                ],
            })
            for call in tool_calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                result = execute_tool(db, openid, call.function.name, args)
                if result.get("handoff"):
                    _clear_pending(db, openid)
                    return PHONE_FALLBACK
                if result.get("staged") and result.get("script"):
                    staged_script = str(result["script"])
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(
                        {key: value for key, value in result.items() if key != "script"},
                        ensure_ascii=False,
                    ),
                })
            if staged_script:
                return staged_script
        if handoff_text:
            return handoff_text
        if staged_script:
            return staged_script
    except Exception as exc:
        logger.warning("[ai-cs] llm failed openid=%s error=%s", (openid or "")[:8], type(exc).__name__)
        record_handoff(db, openid, "llm_unavailable", "phone_fallback", type(exc).__name__)
        return PHONE_FALLBACK
    record_handoff(db, openid, "low_confidence", "phone_fallback", "工具轮次用尽")
    return PHONE_FALLBACK


def admin_snapshot(db: Session) -> dict:
    rows = (
        db.query(AiCsMessage)
        .order_by(AiCsMessage.id.desc())
        .limit(300)
        .all()
    )
    order: list[str] = []
    grouped: dict[str, list] = {}
    for row in rows:
        key = row.openid or ""
        if key not in grouped:
            if len(order) >= 30:
                continue
            order.append(key)
            grouped[key] = []
        if len(grouped[key]) < 8:
            grouped[key].append(row)
    conversations = []
    for key in order:
        messages = list(reversed(grouped[key]))
        conversations.append({"openid": key or "—", "messages": messages})
    handoffs = (
        db.query(AiCsHandoff)
        .order_by(AiCsHandoff.id.desc())
        .limit(40)
        .all()
    )
    return {"conversations": conversations, "handoffs": handoffs}
