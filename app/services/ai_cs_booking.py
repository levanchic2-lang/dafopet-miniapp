"""AI 客服可调用的可约时段、预约、改期和取消。

时段规则复用 app.main 里的容量、美容师占用和日历封锁，不另写一套。
TNR 不走这里。密钥只从服务器环境变量 AI_CS_API_KEY 读取。
"""

from __future__ import annotations

import calendar
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Appointment, AppointmentCategory, AppointmentStatus, AuditLog, Customer, Pet
from app.time_utils import beijing_now, beijing_today

logger = logging.getLogger(__name__)

AI_CS_TOKEN_ENV = "AI_CS_API_KEY"
HORIZON_DAYS = 15
SLOT_STEP = 30
SURGERY_ARRIVAL_NOTE = "预约时间仅为预计到院时间，实际手术时间以当天门诊安排为准。"
LATE_PENALTY = False

# 与预约目录里的手术项目时长一致。容量仍按手术 4 个单位，不按这段时长折算。
_SURGERY_MINUTES = {
    "绝育手术": 60,
    "骨科手术": 120,
    "软组织手术": 90,
    "眼科手术": 60,
    "神经外科手术": 120,
}

# 只有服务名能唯一对上这三种时才校验。kind=vaccine 本身只表示 30 分钟、1 个容量单位。
_VACCINE_RULES = {
    "rabies": {
        "label": "狂犬疫苗",
        "species": frozenset({"cat", "dog"}),
        "min_months": 3,
        "min_days": None,
        "requirement": "3 个月",
        "henggang_only": True,
    },
    "canine_8": {
        "label": "犬八联",
        "species": frozenset({"dog"}),
        "min_months": None,
        "min_days": 6 * 7,
        "requirement": "6 周",
        "henggang_only": False,
    },
    "combo_3": {
        "label": "猫三联",
        "species": frozenset({"cat"}),
        "min_months": None,
        "min_days": 8 * 7,
        "requirement": "8 周",
        "henggang_only": False,
    },
}


class AiCsError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def read_ai_cs_api_key() -> str:
    """每次请求现读环境变量。空白视为没配置。"""
    if AI_CS_TOKEN_ENV in os.environ:
        return (os.environ.get(AI_CS_TOKEN_ENV) or "").strip()
    return (getattr(settings, "ai_cs_api_key", "") or "").strip()


def require_ai_cs_key(supplied: str | None) -> None:
    expected = read_ai_cs_api_key()
    if not expected:
        raise AiCsError("auth_not_configured", "AI 客服接口密钥未配置", 503)
    provided = supplied or ""
    if not secrets.compare_digest(
        hashlib.sha256(provided.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    ):
        raise AiCsError("auth_invalid", "AI 客服接口密钥无效", 401)


def identify_vaccine_type(service_name: str = "", category: str = "") -> str | None:
    """从预约项目或类别里认出狂犬、犬八联、猫三联。

    认不出就返回 None，调用方跳过年龄和门店限制。
    预约目录里的「疫苗/驱虫」不包含具体苗种，驱虫也不包含，所以不会命中。
    名称同时像两种苗时返回 ambiguous，同样不做硬性年龄校验。
    """
    text = f"{service_name or ''} {category or ''}"
    lowered = text.lower()
    hits: list[str] = []
    if "狂犬" in text or "rabies" in lowered:
        hits.append("rabies")
    if "八联" in text or "8联" in text or "canine_8" in lowered:
        hits.append("canine_8")
    if "猫三联" in text or "三联" in text or "3联" in text or "combo_3" in lowered:
        hits.append("combo_3")
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        return "ambiguous"
    return None


def _month_end(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _shift_months(day: date, months: int) -> date:
    index = day.year * 12 + (day.month - 1) - months
    year, month0 = divmod(index, 12)
    month = month0 + 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day.day, last))


def parse_birth_date(raw: str, today: date | None = None) -> date | None:
    """能解析出生日就返回日期，否则 None（年龄未知）。

    只有年月时用该月最后一天，避免把未知的日子当成更早、从而把月龄算大。
    """
    today = today or beijing_today()
    text = (raw or "").strip()
    if not text or text in {"—", "-", "未知", "不详", "不清楚", "不明", "无"}:
        return None
    normalized = text.replace("/", "-").replace(".", "-")
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", normalized)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    match = re.fullmatch(r"(\d{4})-(\d{1,2})", normalized)
    if match:
        try:
            return _month_end(int(match.group(1)), int(match.group(2)))
        except ValueError:
            return None
    years = re.search(r"(\d+)\s*岁", text)
    months = re.search(r"(\d+)\s*个?\s*月", text)
    weeks = re.search(r"(\d+)\s*周", text)
    if years or months or weeks:
        birth = today
        if years:
            birth = _shift_months(birth, int(years.group(1)) * 12)
        if months:
            birth = _shift_months(birth, int(months.group(1)))
        if weeks:
            birth = birth - timedelta(days=int(weeks.group(1)) * 7)
        return birth
    return None


def _age_parts(birth: date, today: date) -> tuple[int, int]:
    days = (today - birth).days
    months = (today.year - birth.year) * 12 + (today.month - birth.month)
    if today.day < birth.day:
        months -= 1
    return days, months


def _species_key(species: str) -> str:
    text = (species or "").strip().lower()
    if text in {"cat", "猫", "feline"}:
        return "cat"
    if text in {"dog", "犬", "狗", "canine"}:
        return "dog"
    if not text or text in {"unknown", "未知"}:
        return ""
    return "other"


def vaccine_booking_check(
    *,
    service_name: str,
    category: str,
    store: str,
    species: str,
    birthday: str,
    today: date | None = None,
) -> tuple[AiCsError | None, list[str]]:
    """返回 (错误, 警告列表)。认不出苗种时两者都空，调用方应跳过。"""
    today = today or beijing_today()
    kind = identify_vaccine_type(service_name, category)
    if kind is None:
        return None, []
    if kind == "ambiguous":
        return None, ["预约项目同时像多种疫苗，无法确认是狂犬、犬八联还是猫三联，因此没有做年龄和物种校验。"]
    rule = _VACCINE_RULES[kind]
    if rule["henggang_only"] and "东环" in (store or "") and "横岗" not in (store or ""):
        return AiCsError(
            "rabies_store",
            "政府狂犬疫苗目前只在横岗店接种，请改约横岗店。",
        ), []
    species_key = _species_key(species)
    warnings: list[str] = []
    if species_key and species_key not in rule["species"]:
        if kind == "canine_8":
            message = "犬八联只适用于犬，当前宠物不是犬。"
        elif kind == "combo_3":
            message = "猫三联只适用于猫，当前宠物不是猫。"
        else:
            message = "狂犬疫苗适用于犬和猫，当前宠物物种不适用。"
        return AiCsError("vaccine_species", message), []
    if not species_key:
        warnings.append(
            f"档案未填写物种，无法确认是否符合{rule['label']}的动物种类要求，已接受预约，请到店时核对。"
        )
    birth = parse_birth_date(birthday, today)
    if birth is None:
        warnings.append(
            f"宠物年龄或生日未知，已接受预约。{rule['label']}需满 {rule['requirement']}，请到店时由医生确认。"
        )
        return None, warnings
    days, months = _age_parts(birth, today)
    too_young = days < 0
    if rule["min_months"] is not None and months < int(rule["min_months"]):
        too_young = True
    if rule["min_days"] is not None and days < int(rule["min_days"]):
        too_young = True
    if too_young:
        return AiCsError(
            "vaccine_too_young",
            f"{rule['label']}需满 {rule['requirement']}，当前宠物年龄不足。",
        ), []
    return None, warnings


def neuter_age_check(service_name: str, birthday: str, today: date | None = None) -> tuple[AiCsError | None, str]:
    if "绝育" not in (service_name or ""):
        return None, ""
    today = today or beijing_today()
    birth = parse_birth_date(birthday, today)
    if birth is None:
        return None, "宠物年龄或生日未知，已接受预约。绝育需满 6 个月，请到店后由医生确认。"
    days, months = _age_parts(birth, today)
    if days < 0 or months < 6:
        return AiCsError(
            "age_too_young",
            "绝育预约要求宠物满 6 个月。当前档案年龄不足，请改约其他项目或由医生评估。",
        ), ""
    return None, ""


def _main():
    import app.main as main
    return main


def _resolve_store(raw: str) -> str:
    main = _main()
    text = (raw or "").strip()
    if text in main._STORE_SHORT_TO_FULL:
        return main._STORE_SHORT_TO_FULL[text]
    if text in main._ALLOWED_CLINIC_STORES:
        return text
    if "横岗" in text and "东环" not in text:
        return main._STORE_SHORT_TO_FULL["横岗店"]
    if "东环" in text and "横岗" not in text:
        return main._STORE_SHORT_TO_FULL["东环店"]
    raise AiCsError("store_invalid", "请选择横岗店或东环店。")


def _parse_date(raw: str) -> date:
    try:
        return datetime.strptime((raw or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        raise AiCsError("date_invalid", "日期格式应为 YYYY-MM-DD。")


def _check_horizon(day: date, *, min_days: int) -> None:
    delta = (day - beijing_today()).days
    if delta < 0:
        raise AiCsError("date_in_past", "预约日期不能早于今天。")
    if delta < min_days:
        raise AiCsError("lead_time", "绝育和手术需至少提前一天预约，请从明天起选择。")
    if delta > HORIZON_DAYS:
        raise AiCsError("beyond_horizon", f"最多只能预约 {HORIZON_DAYS} 天内的时段。")


def _parse_clock(raw: str) -> tuple[str, int]:
    text = (raw or "").strip()[:5]
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", text):
        raise AiCsError("time_invalid", "时间格式应为 HH:MM。")
    main = _main()
    minutes = main._time_to_minutes(text)
    if minutes % SLOT_STEP != 0:
        raise AiCsError("slot_invalid", "请选择 30 分钟整点时段，例如 10:00、10:30。")
    return text, minutes


def _now_minutes() -> int:
    now = beijing_now()
    return now.hour * 60 + now.minute


def _customer_by_openid(db: Session, openid: str) -> Customer:
    key = (openid or "").strip()
    if not key:
        raise AiCsError("customer_not_found", "请提供已绑定的微信 openid。")
    rows = db.query(Customer).filter(Customer.wechat_openid == key).all()
    if not rows:
        raise AiCsError("customer_not_found", "该微信没有绑定客户档案。")
    if len(rows) > 1:
        raise AiCsError("customer_ambiguous", "该微信绑定了多个客户档案，请由门店人工处理。")
    return rows[0]


def _pet_for_customer(db: Session, customer: Customer, pet_id: int) -> Pet:
    pet = db.get(Pet, int(pet_id or 0))
    if pet is None:
        raise AiCsError("pet_not_found", "找不到这只宠物。")
    if pet.customer_id != customer.id:
        raise AiCsError("pet_not_owned", "这只宠物不属于该微信绑定的客户。")
    return pet


def _birthday_for_check(pet: Pet, supplied: str) -> str:
    stored = (pet.birthday_estimate or "").strip()
    if parse_birth_date(stored) is not None:
        return stored
    return (supplied or "").strip()


def _surgery_duration(service_name: str) -> int:
    name = (service_name or "").strip()
    if name in _SURGERY_MINUTES:
        return _SURGERY_MINUTES[name]
    if "绝育" in name:
        return 60
    return 60


def _planned_duration(kind: str, service_name: str, pet_size: str, coat_length: str, addons: str, weight) -> int:
    main = _main()
    if kind == "beauty":
        return main._calc_beauty_duration(service_name, pet_size, coat_length, addons, weight_kg=weight)
    if kind == "vaccine":
        return 30
    if kind == "surgery":
        return _surgery_duration(service_name)
    return 30 if any(word in (service_name or "") for word in main._VACCINE_KEYWORDS) else 60


def _capacity_service_name(kind: str, service_name: str) -> str:
    """kind=vaccine 固定按 1 个容量单位。已有容量函数靠名称里的「疫苗/驱虫」识别。"""
    name = (service_name or "").strip()
    if kind != "vaccine":
        return name
    if not name:
        return "疫苗/驱虫"
    if "疫苗" in name or "驱虫" in name:
        return name
    return f"{name}疫苗"


def _window_for(kind: str, service_name: str) -> tuple[int, int, int]:
    """返回 (最早开始分钟, 最晚开始分钟不含, 最少提前天数)。"""
    if kind == "surgery":
        return 11 * 60, 18 * 60, 1
    if kind == "beauty":
        main = _main()
        return main._BEAUTY_WORK_START, main._BEAUTY_WORK_END, 0
    return 10 * 60, 21 * 60, 0


def _ensure_clock(kind: str, service_name: str, minutes: int, duration: int, day: date) -> None:
    start, end, min_days = _window_for(kind, service_name)
    _check_horizon(day, min_days=min_days)
    if kind == "beauty":
        if minutes < start or minutes + duration > end:
            raise AiCsError("outside_hours", "美容可约时间为 13:00–22:00，且服务须在 22:00 前结束。")
    elif kind == "surgery":
        if minutes < start or minutes >= end:
            raise AiCsError("outside_hours", "绝育和手术的可约到院时间为 11:00–18:00。")
    else:
        if minutes < start or minutes >= end:
            raise AiCsError("outside_hours", "门诊和疫苗驱虫的可约时间为 10:00–21:00。")
    if day == beijing_today() and minutes < _now_minutes():
        raise AiCsError("slot_passed", "该时段已过，请选择更晚的时间。")


def _rules_payload(kind: str) -> dict:
    if kind == "surgery":
        return {
            "lead": "至少提前一天，不能预约当天",
            "window": "11:00–18:00",
            "horizon_days": HORIZON_DAYS,
            "arrival_note": SURGERY_ARRIVAL_NOTE,
        }
    if kind == "beauty":
        return {
            "lead": "可预约当天，没有最短提前时间",
            "window": "13:00–22:00",
            "horizon_days": HORIZON_DAYS,
            "arrival_note": "",
        }
    if kind == "vaccine":
        return {
            "lead": "可预约当天，没有最短提前时间",
            "window": "10:00–21:00",
            "horizon_days": HORIZON_DAYS,
            "arrival_note": "",
            "vaccine_checks": (
                "只有服务名能认出狂犬、犬八联或猫三联时才校验年龄、物种和狂犬门店。"
                "通用项目「疫苗/驱虫」认不出具体苗种，不套这些限制。"
            ),
        }
    return {
        "lead": "可预约当天，没有最短提前时间",
        "window": "10:00–21:00",
        "horizon_days": HORIZON_DAYS,
        "arrival_note": "",
    }


def _slot_labels(db: Session, *, store: str, day: date, kind: str, service_name: str, duration: int, capacity_name: str) -> list[str]:
    main = _main()
    date_str = day.isoformat()
    if kind == "beauty":
        labels = main._beauty_slots_for_date(db, store, date_str, service_name, duration)
    else:
        start, end, _min_days = _window_for(kind, service_name)
        category = AppointmentCategory.surgery.value if kind == "surgery" else AppointmentCategory.outpatient.value
        labels = []
        minute = start
        limit = end if kind != "beauty" else end
        while minute < limit:
            label = f"{minute // 60:02d}:{minute % 60:02d}"
            errors = main._availability_errors(
                db, store=store, appointment_date=date_str, appointment_time=label,
                duration_minutes=duration, category=category, service_name=service_name,
                capacity_service_name=capacity_name,
            )
            if not errors:
                labels.append(label)
            minute += SLOT_STEP
    if day == beijing_today():
        now_min = _now_minutes()
        labels = [item for item in labels if main._time_to_minutes(item) >= now_min]
    return labels


def query_slots(
    db: Session,
    *,
    store: str,
    day: str,
    kind: str,
    service_name: str = "",
    pet_size: str = "",
    coat_length: str = "",
    addons: str = "",
    weight_kg: float | str | None = None,
    pets: list[dict] | None = None,
    pet_id: int | None = None,
    birthday: str = "",
    species: str = "",
) -> dict:
    kind = (kind or "").strip().lower()
    if kind not in {"outpatient", "vaccine", "surgery", "beauty"}:
        raise AiCsError("kind_invalid", "kind 只能是 outpatient、vaccine、surgery 或 beauty。")
    full_store = _resolve_store(store)
    parsed_day = _parse_date(day)
    service = (service_name or "").strip()
    if kind == "vaccine" and not service:
        service = "疫苗/驱虫"
    if kind == "surgery" and not service:
        service = "绝育手术"
    _check_horizon(parsed_day, min_days=1 if kind == "surgery" else 0)
    main = _main()
    weight = main._parse_weight_kg(weight_kg)
    warnings: list[str] = []
    if pet_id:
        pet = db.get(Pet, int(pet_id))
        if pet is not None:
            if weight is None:
                recorded = main._latest_pet_weight(db, pet.id)
                weight = recorded if recorded > 0 else None
            if not species:
                species = pet.species or ""
            if not birthday:
                birthday = pet.birthday_estimate or ""
    if kind == "vaccine":
        error, warnings = vaccine_booking_check(
            service_name=service, category=kind, store=full_store,
            species=species, birthday=birthday,
        )
        if error:
            raise error
    if kind == "beauty" and pets and len(pets) > 1:
        segments = []
        for item in pets:
            item_service = str(item.get("service_name") or item.get("service") or "").strip()
            item_size = str(item.get("pet_size") or "").strip()
            item_coat = str(item.get("coat_length") or "").strip()
            item_addons = item.get("addons") or item.get("addon_services") or ""
            if isinstance(item_addons, list):
                item_addons = ",".join(str(x) for x in item_addons if str(x).strip())
            item_weight = main._parse_weight_kg(item.get("weight_kg"))
            if not item_service:
                raise AiCsError("service_required", "每只宠物都要填写美容项目。")
            duration = main._calc_beauty_duration(item_service, item_size, item_coat, str(item_addons), weight_kg=item_weight)
            segments.append((item_service, duration))
        groups = main._beauty_group_starts(db, full_store, parsed_day.isoformat(), segments)
        if parsed_day == beijing_today():
            now_min = _now_minutes()
            groups = [group for group in groups if main._time_to_minutes(group["start"]) >= now_min]
        return {
            "ok": True,
            "kind": kind,
            "store": full_store,
            "date": parsed_day.isoformat(),
            "slots": [group["start"] for group in groups],
            "groups": groups,
            "rules": _rules_payload(kind),
            "warning": "；".join(warnings),
            "warnings": warnings,
            "late_penalty": LATE_PENALTY,
        }
    duration = _planned_duration(kind, service, pet_size, coat_length, addons, weight)
    capacity_name = _capacity_service_name(kind, service)
    category = AppointmentCategory.surgery.value if kind == "surgery" else (
        AppointmentCategory.beauty.value if kind == "beauty" else AppointmentCategory.outpatient.value
    )
    slots = _slot_labels(
        db, store=full_store, day=parsed_day, kind=kind, service_name=service,
        duration=duration, capacity_name=capacity_name,
    )
    return {
        "ok": True,
        "kind": kind,
        "category": category,
        "service_name": service,
        "store": full_store,
        "date": parsed_day.isoformat(),
        "duration_minutes": duration,
        "capacity_units": main._capacity_units(category, capacity_name),
        "slots": slots,
        "rules": _rules_payload(kind),
        "arrival_note": SURGERY_ARRIVAL_NOTE if kind == "surgery" else "",
        "warning": "；".join(warnings),
        "warnings": warnings,
        "late_penalty": LATE_PENALTY,
    }


def _public_appointment(row: Appointment) -> dict:
    return {
        "id": row.id,
        "source": row.source,
        "category": row.category,
        "service_name": row.service_name,
        "status": row.status,
        "store": row.store,
        "appointment_date": row.appointment_date,
        "appointment_time": row.appointment_time,
        "duration_minutes": row.duration_minutes,
        "customer_id": row.customer_id,
        "pet_id": row.pet_id,
        "pet_name": row.pet_name,
        "notes": row.notes or "",
    }


def _gender(pet: Pet) -> str:
    value = (pet.gender or "unknown").strip().lower()
    return value if value in {"male", "female", "unknown"} else "unknown"


def _require_phone(customer: Customer) -> str:
    phone = (customer.phone or "").strip()
    if not re.fullmatch(r"1\d{10}", phone):
        raise AiCsError("customer_phone_invalid", "客户档案里的手机号不是 11 位大陆号码，请先由门店补全后再预约。")
    return phone


@contextmanager
def _booking_lock():
    path = Path("data/.ai_cs_booking.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _assert_segment_free(
    db: Session,
    *,
    store: str,
    day: str,
    clock: str,
    duration: int,
    category: str,
    service_name: str,
    capacity_name: str,
    exclude_id: int | None = None,
) -> None:
    errors = _main()._availability_errors(
        db, store=store, appointment_date=day, appointment_time=clock,
        duration_minutes=duration, category=category, service_name=service_name,
        exclude_id=exclude_id, capacity_service_name=capacity_name,
    )
    if not errors:
        return
    message = errors[0]
    if "封锁" in message:
        code = "blocked"
    elif "容量" in message:
        code = "capacity_full"
    elif "冲突" in message:
        code = "conflict"
    else:
        code = "slot_unavailable"
    raise AiCsError(code, message, 409)


def _audit(db: Session, action: str, detail: dict, application_id: int | None = None) -> None:
    db.add(AuditLog(
        action=action,
        actor="ai_cs",
        application_id=application_id,
        detail=json.dumps(detail, ensure_ascii=False),
    ))


def _notify(db: Session, row: Appointment, event: str, note: str = "") -> None:
    try:
        from app.services.wechat_subscribe import notify_appointment_event
        notify_appointment_event(db, row, event, note=note)
    except Exception as exc:
        logger.warning("[ai-cs] appointment %s push failed: %s", event, exc)


def book_appointments(db: Session, payload: dict) -> dict:
    payload = payload or {}
    kind = str(payload.get("kind") or "").strip().lower()
    if kind not in {"outpatient", "vaccine", "surgery", "beauty"}:
        raise AiCsError("kind_invalid", "kind 只能是 outpatient、vaccine、surgery 或 beauty。")
    customer = _customer_by_openid(db, str(payload.get("openid") or ""))
    phone = _require_phone(customer)
    full_store = _resolve_store(str(payload.get("store") or ""))
    day = _parse_date(str(payload.get("date") or payload.get("appointment_date") or ""))
    clock, minutes = _parse_clock(str(payload.get("time") or payload.get("appointment_time") or ""))
    notes = str(payload.get("notes") or "").strip()[:2000]
    main = _main()
    warnings: list[str] = []

    pet_rows: list[dict] = []
    raw_pets = payload.get("pets")
    if kind == "beauty" and isinstance(raw_pets, list) and raw_pets:
        cursor = minutes
        for item in raw_pets:
            if not isinstance(item, dict):
                raise AiCsError("pet_required", "多宠美容需要每只宠物的 pet_id 和项目。")
            pet = _pet_for_customer(db, customer, int(item.get("pet_id") or 0))
            service = str(item.get("service_name") or item.get("service") or "").strip()
            if service not in {"猫洗护", "猫造型", "犬洗护", "犬造型"}:
                raise AiCsError("service_invalid", "美容项目请填写猫洗护、猫造型、犬洗护或犬造型。")
            size = str(item.get("pet_size") or "").strip()
            coat = str(item.get("coat_length") or "").strip()
            addons = item.get("addons") if item.get("addons") is not None else item.get("addon_services") or ""
            if isinstance(addons, list):
                addons = ",".join(str(x).strip() for x in addons if str(x).strip())
            weight = main._parse_weight_kg(item.get("weight_kg"))
            if weight is None:
                recorded = main._latest_pet_weight(db, pet.id)
                weight = recorded if recorded > 0 else None
            duration = main._calc_beauty_duration(service, size, coat, str(addons or ""), weight_kg=weight)
            label = f"{cursor // 60:02d}:{cursor % 60:02d}"
            pet_rows.append({
                "pet": pet, "service": service, "size": size, "coat": coat,
                "addons": str(addons or ""), "duration": duration, "time": label,
                "category": AppointmentCategory.beauty.value,
            })
            cursor += duration
    else:
        pet = _pet_for_customer(db, customer, int(payload.get("pet_id") or 0))
        service = str(payload.get("service_name") or "").strip()
        if kind == "vaccine" and not service:
            service = "疫苗/驱虫"
        if kind == "surgery" and not service:
            service = "绝育手术"
        if kind == "outpatient" and not service:
            raise AiCsError("service_required", "请填写门诊项目。")
        if kind == "beauty" and service not in {"猫洗护", "猫造型", "犬洗护", "犬造型"}:
            raise AiCsError("service_invalid", "美容项目请填写猫洗护、猫造型、犬洗护或犬造型。")
        weight = main._parse_weight_kg(payload.get("weight_kg"))
        if weight is None and kind == "beauty":
            recorded = main._latest_pet_weight(db, pet.id)
            weight = recorded if recorded > 0 else None
        duration = _planned_duration(
            kind, service,
            str(payload.get("pet_size") or ""),
            str(payload.get("coat_length") or ""),
            str(payload.get("addons") or payload.get("addon_services") or ""),
            weight,
        )
        birthday = _birthday_for_check(pet, str(payload.get("birthday") or ""))
        if kind == "surgery":
            error, warning = neuter_age_check(service, birthday)
            if error:
                raise error
            if warning:
                warnings.append(warning)
        if kind == "vaccine" or identify_vaccine_type(service, kind):
            error, vaccine_warnings = vaccine_booking_check(
                service_name=_capacity_service_name(kind, service) if kind == "vaccine" else service,
                category=kind, store=full_store, species=pet.species or "", birthday=birthday,
            )
            if error:
                raise error
            warnings.extend(vaccine_warnings)
        category = {
            "surgery": AppointmentCategory.surgery.value,
            "beauty": AppointmentCategory.beauty.value,
        }.get(kind, AppointmentCategory.outpatient.value)
        stored_service = _capacity_service_name(kind, service) if kind == "vaccine" else service
        pet_rows.append({
            "pet": pet, "service": stored_service, "size": str(payload.get("pet_size") or ""),
            "coat": str(payload.get("coat_length") or ""),
            "addons": str(payload.get("addons") or payload.get("addon_services") or ""),
            "duration": duration, "time": clock, "category": category,
        })

    first_duration = pet_rows[0]["duration"]
    if kind == "beauty" and len(pet_rows) > 1:
        total = sum(item["duration"] for item in pet_rows)
        _ensure_clock(kind, pet_rows[0]["service"], minutes, total, day)
    else:
        _ensure_clock(kind, pet_rows[0]["service"], minutes, first_duration, day)

    created: list[Appointment] = []
    try:
        with _booking_lock():
            for item in pet_rows:
                capacity_name = item["service"] if kind == "vaccine" or "疫苗" in item["service"] or "驱虫" in item["service"] else item["service"]
                if kind == "vaccine":
                    capacity_name = item["service"]
                _assert_segment_free(
                    db, store=full_store, day=day.isoformat(), clock=item["time"],
                    duration=item["duration"], category=item["category"],
                    service_name=item["service"], capacity_name=capacity_name,
                )
                pet = item["pet"]
                row = Appointment(
                    wechat_openid=customer.wechat_openid or "",
                    category=item["category"],
                    status=AppointmentStatus.confirmed.value,
                    service_name=item["service"],
                    customer_name=(customer.name or "")[:120],
                    phone=phone,
                    pet_name=(pet.name or "")[:120],
                    pet_gender=_gender(pet),
                    store=full_store,
                    appointment_date=day.isoformat(),
                    appointment_time=item["time"],
                    duration_minutes=item["duration"],
                    source="ai_cs",
                    notes=notes,
                    pet_size=(item["size"] or None) if kind == "beauty" else None,
                    coat_length=(item["coat"] or None) if kind == "beauty" else None,
                    addon_services=(item["addons"] or None) if kind == "beauty" else None,
                    customer_id=customer.id,
                    pet_id=pet.id,
                )
                db.add(row)
                db.flush()
                created.append(row)
            _audit(db, "ai_cs_appointment_create", {
                "appointment_ids": [row.id for row in created],
                "kind": kind,
                "store": full_store,
                "date": day.isoformat(),
                "customer_id": customer.id,
                "warnings": warnings,
            })
            db.commit()
    except AiCsError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    for row in created:
        db.refresh(row)
        _notify(db, row, "confirmed", note="AI 客服已确认预约")
    warning_text = "；".join(dict.fromkeys(warnings))
    return {
        "ok": True,
        "warning": warning_text,
        "warnings": list(dict.fromkeys(warnings)),
        "arrival_note": SURGERY_ARRIVAL_NOTE if kind == "surgery" else "",
        "late_penalty": LATE_PENALTY,
        "appointments": [_public_appointment(row) for row in created],
    }


def _owned_appointment(db: Session, customer: Customer, appointment_id: int) -> Appointment:
    row = db.get(Appointment, int(appointment_id or 0))
    if row is None:
        raise AiCsError("appointment_not_found", "预约不存在。", 404)
    if (row.category or "") == AppointmentCategory.tnr.value:
        raise AiCsError("tnr_not_supported", "TNR 预约不通过这个接口办理。")
    if row.customer_id != customer.id:
        raise AiCsError("not_owner", "这张预约不属于该微信绑定的客户。", 403)
    return row


def reschedule_appointment(db: Session, appointment_id: int, payload: dict) -> dict:
    payload = payload or {}
    customer = _customer_by_openid(db, str(payload.get("openid") or ""))
    row = _owned_appointment(db, customer, appointment_id)
    if row.status not in (AppointmentStatus.pending.value, AppointmentStatus.confirmed.value):
        raise AiCsError("status_locked", "只有待确认或已确认的预约可以改期，请联系门店。")
    day = _parse_date(str(payload.get("date") or payload.get("appointment_date") or ""))
    clock, minutes = _parse_clock(str(payload.get("time") or payload.get("appointment_time") or ""))
    store = _resolve_store(str(payload.get("store") or row.store or ""))
    kind = "beauty" if (row.category or "") in _main()._BEAUTY_TRACK else (
        "surgery" if row.category == AppointmentCategory.surgery.value else "outpatient"
    )
    service = row.service_name or ""
    if kind == "outpatient" and any(word in service for word in _main()._VACCINE_KEYWORDS):
        kind = "vaccine"
    duration = int(row.duration_minutes or 30)
    warnings: list[str] = []
    pet = db.get(Pet, row.pet_id) if row.pet_id else None
    birthday = _birthday_for_check(pet, "") if pet is not None else ""
    species = (pet.species or "") if pet is not None else ""
    if kind == "surgery":
        error, warning = neuter_age_check(service, birthday)
        if error:
            raise error
        if warning:
            warnings.append(warning)
    if identify_vaccine_type(service, kind):
        error, vaccine_warnings = vaccine_booking_check(
            service_name=service, category=kind, store=store, species=species, birthday=birthday,
        )
        if error:
            raise error
        warnings.extend(vaccine_warnings)
    _ensure_clock(kind, service, minutes, duration, day)
    try:
        with _booking_lock():
            _assert_segment_free(
                db, store=store, day=day.isoformat(), clock=clock, duration=duration,
                category=row.category or "", service_name=service, capacity_name=service,
                exclude_id=row.id,
            )
            old = {"store": row.store, "date": row.appointment_date, "time": row.appointment_time}
            row.store = store
            row.appointment_date = day.isoformat()
            row.appointment_time = clock
            row.updated_at = datetime.utcnow()
            _audit(db, "ai_cs_appointment_reschedule", {
                "appointment_id": row.id, "old": old,
                "new": {"store": store, "date": day.isoformat(), "time": clock},
                "warnings": warnings,
            })
            db.commit()
    except AiCsError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    db.refresh(row)
    _notify(db, row, "rescheduled")
    warning_text = "；".join(dict.fromkeys(warnings))
    return {
        "ok": True,
        "warning": warning_text,
        "warnings": list(dict.fromkeys(warnings)),
        "arrival_note": SURGERY_ARRIVAL_NOTE if kind == "surgery" else "",
        "late_penalty": LATE_PENALTY,
        "appointment": _public_appointment(row),
    }


def cancel_appointment(db: Session, appointment_id: int, payload: dict) -> dict:
    payload = payload or {}
    customer = _customer_by_openid(db, str(payload.get("openid") or ""))
    row = _owned_appointment(db, customer, appointment_id)
    if row.status not in (AppointmentStatus.pending.value, AppointmentStatus.confirmed.value):
        raise AiCsError("status_locked", "只有待确认或已确认的预约可以取消，请联系门店。")
    reason = str(payload.get("reason") or "").strip()[:300]
    row.status = AppointmentStatus.cancelled.value
    row.updated_at = datetime.utcnow()
    if reason:
        existing = (row.notes or "").strip()
        row.notes = (existing + f"\n[取消原因] {reason}").strip()
    _audit(db, "ai_cs_appointment_cancel", {
        "appointment_id": row.id,
        "date": row.appointment_date,
        "reason": reason,
        "late_penalty": False,
    })
    db.commit()
    db.refresh(row)
    _notify(db, row, "cancelled", note=reason or "顾客已取消预约")
    return {
        "ok": True,
        "id": row.id,
        "status": row.status,
        "same_day_cancel_allowed": True,
        "late_penalty": LATE_PENALTY,
        "message": "已取消。当天可以取消，迟到不另外收费或处罚。",
    }
