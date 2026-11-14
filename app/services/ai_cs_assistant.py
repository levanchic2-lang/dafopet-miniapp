"""小程序客服的对话、工具和转人工。

写预约只走 app.services.ai_cs_booking 里的函数，不请求本机 HTTP。
客户明确回复确认之前，stage_* 只保存待办，不写预约表。
"""
from __future__ import annotations

import asyncio
import calendar
import contextvars
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AiCsHandoff,
    AiCsHandoffSession,
    AiCsMediaCache,
    AiCsMessage,
    AiCsPending,
    AiCsToolLog,
    AnesthesiaMonitorSheet,
    Application,
    Appointment,
    AppointmentCategory,
    AppointmentStatus,
    Customer,
    DewormingRecord,
    GroomingOrder,
    Hospitalization,
    Pet,
    WeightRecord,
)
from app.services.ai_cs_booking import (
    BEAUTY_WEIGHT_REQUIRED_MESSAGE,
    HORIZON_DAYS,
    LATE_PENALTY,
    AiCsError,
    _rules_payload,
    book_appointments,
    cancel_appointment,
    parse_birth_date,
    query_slots,
    reschedule_appointment,
)
from app.services.report_llm import report_llm_configured, report_text_client_model
from app.time_utils import beijing_now, beijing_today, format_beijing

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
    "这件事我交给同事了。您也可以直接打电话："
    "17820633031（微信同号），或者 0755-28704990。"
    "东环店 18026901718，横岗店 17820633031。"
    "要是宠物呼吸困难、抽搐、昏迷，或者出血很多，请马上打过来，直接到店。"
)
GREETING = (
    "您好呀，这里是大风动物医院。"
    "想约时间、改期，或者问问到院，直接跟我说就好。"
    "找同事的话，回复「人工」。"
)
BIND_CARD_ASK = "点下面这个卡片绑定一下，绑好回来跟我说一声就行"
GREETING_UNBOUND = (
    GREETING
    + "想预约或者看档案的话，" + BIND_CARD_ASK + "。"
)
MEDIA_REPLY = (
    "图片和小程序卡片我这边看不了。"
    "您用文字跟我说就行，是想预约、改期、取消，还是问问到院。"
)
RESUME_NOTE = "同事这边先告一段落，我继续帮您。还需要人工的话，随时回复「人工」。"
UNBOUND_REPLY = (
    "我还没对上您的档案，预约、改期、取消，还有查自己的预约和宠物，暂时办不了。"
    + BIND_CARD_ASK
    + "。卡片上用在医院留的手机号收个验证码。"
    "然后告诉我哪家店、做什么、哪只宠物、哪天几点。只是问问到院的话，现在就可以说。"
)
RATE_REPLY = "您发得有点快，我先歇一会儿。过几分钟再发，或者直接打 17820633031。"
BIND_HINT = "还没绑定。请告诉客户" + BIND_CARD_ASK + "。不要让对方退出去首页找按钮。"
BIND_PAGE_PATH = "pages/bind/bind?from=cs"
BIND_CARD_TITLE = "点这里绑定我的档案"
BIND_CARD_COOLDOWN_MINUTES = 30
BIND_CARD_MEDIA_HOURS = 60
BIND_CARD_CACHE_KEY = "bind_card_thumb"
BIND_CARD_FALLBACK = (
    "卡片没发出去。您回到小程序首页，点一下「绑定我的档案」，"
    "绑好回来跟我说一声就行。"
)
BIND_CARD_LOG = "（已发送绑定卡片：点这里绑定我的档案）"
PET_FILE_RULE = (
    "顺序：名下有多只宠物，或这个微信有多份档案时，先问是哪一只。"
    "还没确定是哪一只之前，不要问体重、毛长、品种，也不要用某一只的档案去确认。"
    "用宠物名字问，例如「这次是给大米洗，还是大橘呀？」不要说「您名下有两只需要安排」，也不要编成清单。\n"
    "宠物确定之后，用档案里这一只的物种、品种、性别、年龄、绝育、最近体重和上次毛长。"
    "已经有的不要当成不知道再问一遍，只问还缺的，或需要确认的。"
    "只有一只宠物时，直接使用这份档案，不要再问是哪一只。\n"
    "美容以及任何要用体重的地方先用这份档案，只请客户确认。内部用来对价目表和去底绒加时，不要对客户说「价格档」或「用于算价格档」。"
    "对客户说人话，例如「大橘上次大概 5.2 公斤，短毛，对吗？」。"
    "不要说「档案里大米是田园猫，上次称重 5.2kg（2026-09-03），上次洗护是短毛，对吗？」这种把档案念成标签的句子。\n"
    "体重日期早于 6 个月时，说出日期，并问变化大不大。"
    "档案没有体重或毛长时才问。直接问，不要解释这个数是用来干什么的，也不要说「档案里没有」。"
    "例如「大橘最近大概多重呀？是短毛吧？」品种可以提示可能的毛长，但仍要客户确认。"
    "档案毛长若是中毛，先确认按长毛还是短毛，不要猜。\n"
    "is_neutered 为 true 时按已绝育使用，不要再问有没有绝育，也不要再约绝育。"
    "为 false 时不要断言未绝育。年龄已有的不要再问生日。\n"
    "本轮对话里客户已经确认过的体重、毛长和品种，不要再问。\n"
    "确认后的体重只在内部对价目表和去底绒分钟，不要把算法讲给客户。"
    "预约时把确认后的 coat_length（长毛或短毛）和 weight_kg 传给工具；"
    "没传时用档案里的值写进给客户看的确认话和预约。"
    "体型由系统按确认后的体重分档，不要让客户再报一次体重，也不要自己换算结束时间。"
    "猫：不足 3kg 小型猫；3kg 至不足 5kg 中型猫；5kg 及以上大型猫（正好 5kg 是大型猫）。"
    "犬：不足 3kg 微小型犬；3kg 至不足 12kg 小型犬；12kg 至不足 18kg 中型犬；"
    "18kg 至不足 28kg 中大型犬；28kg 及以上大型犬（每一档都含下界）。"
    "查空档时把 weight_kg 传给 query_slots。没传 pet_size 时系统按体重填写。"
    "没有体重就先问主人，不要按 60 分钟估算。"
    "只提供工具返回的开始时间，结束时间只用返回的 end。\n"
    "多只宠物时，主人选定之后再调用 list_profiles_and_pets 并传入 pet_id，加载这一只的档案。\n"
)

SPEAKING_STYLE = """【对客户怎么说】
你在微信里跟主人聊天，像医院前台随口说话。短句，口语。呢、哈、呀偶尔用一下就好，不要每句都带。
一次尽量只问一个问题。用宠物的名字称呼。
不要使用 Markdown。微信是纯文本，星号、下划线、井号、反引号都不会排版，会原样露出来。
不要写加粗、标题、代码、项目符号、表格和分隔线。日期和时间写成普通文字，例如「今天 11/13（周五）」。
不要用「1. 2. 3.」这种清单，除非是在几只宠物里让人选，或者同一天要报出 3 个以上的时间。
不要把客户刚说的话复读成标签。不要说「大橘：猫洗护，大型猫（5.2kg），短毛」，也不要说「大橘（猫洗护·短毛·大型猫）」。
说成句子。有工具返回的分钟数时可以换成口语，例如 120 分钟就说「大概两个小时左右」。没有返回时长就不要自己报要多久。
不要解释某个问题是为了什么。不要说「用于算价格档」「档案里没有它的体重和上次毛长」「我先给您出确认稿」「您名下有两只需要安排」。
缺什么就直接问，例如「大橘最近大概多重呀？是短毛吧？」
核对时说「我帮您确认一下」，把店、项目、宠物、日期和时间说成一两句话。不要说「确认稿」。
空档只能用工具返回的开始时间，结束时间只用同一条的 end，绝对不要编时间。
不要把一天的空档全部贴出来。从返回结果里每天挑 3 到 4 个比较合适的，按上午、下午、晚上分开说，例如「周六下午有 15:00、16:00，晚上有 19:00。要看更多跟我说。」
返回的不够 3 个，就有几个说几个。客户问某个钟点，在结果里有才说有。
报价用句子说，不要贴表格。
表情最多一个，也可以不用。
不要说「作为AI」「作为人工智能」「很高兴为您服务」「请问有什么可以帮您」。
客户问你是不是真人、是不是机器人时，不要装成某一位同事。告诉对方：我是大风动物医院的在线客服助手，需要同事就回复「人工」。
知识库里如果有「档案里大米是田园猫，上次称重……对吗」这类举例，不要照着对客户念。按上面的口语说。
急症还是第一句就请对方马上打电话并到店。不诊断、不开药、不解读报告。
"""

_PHONE_RE = re.compile(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)")
_CONFIRM_RE = re.compile(
    r"^(确认|是|是的|好|好的|可以|对|对的|没错|没问题|行|同意|就这样|就这么定|ok|okay|嗯|要)[。.!！~～]?$",
    re.IGNORECASE,
)
_DECLINE_RE = re.compile(r"^(取消|不用了|算了|先不|不约了|不要了|先别|不行|不)[。.!！]?$")
_WRITE_INTENT = ("预约", "约个", "约一下", "帮我约", "改期", "改约", "换个时间", "取消预约", "取消这", "取消明天")
_OWN_RECORD_RE = re.compile(
    r"我的预约|查预约|看看预约|预约记录|有没有预约|"
    r"我的宠物|我家宠物|宠物档案|我的猫|我家猫|我的狗|我家狗|哪只|名下"
)
_bind_offer: contextvars.ContextVar[bool] = contextvars.ContextVar("ai_cs_bind_offer", default=False)
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


_MD_MARKERS = re.compile(r"\*\*|__|`+")
_MD_BULLET = re.compile(r"^[ \t]{0,3}[-*+][ \t]+")
_MD_RULE = re.compile(r"^[-*_]{3,}$")
_MD_TABLE_SEP = re.compile(r"^[\s|:\-]+$")


def _markdown_rule_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if _MD_RULE.fullmatch(stripped):
        return True
    return stripped.count("-") >= 3 and _MD_TABLE_SEP.fullmatch(stripped) is not None


def sanitize_wechat_text(text: str) -> str:
    """微信客服是纯文本。去掉 Markdown 记号，保留换行和普通数字。"""
    raw = text or ""
    if not raw.strip():
        return ""
    value = raw.replace("\r\n", "\n").replace("\r", "\n")
    value = _MD_MARKERS.sub("", value)
    value = value.replace("#", "")
    lines = []
    for line in value.split("\n"):
        if _markdown_rule_line(line):
            continue
        line = _MD_BULLET.sub("", line)
        line = line.replace("|", " ")
        line = line.replace("*", "")
        line = re.sub(r"[ \t]{2,}", " ", line).strip()
        lines.append(line)
    return "\n".join(lines).strip()


def wants_human(text: str) -> bool:
    return bool(_HUMAN_RE.search(text or ""))


def needs_binding_intent(text: str) -> bool:
    """预约、改期、取消，或查看自己的预约和宠物，都得先绑定。"""
    raw = text or ""
    if any(word in raw for word in _WRITE_INTENT):
        return True
    return _OWN_RECORD_RE.search(raw) is not None


def note_bind_needed() -> None:
    _bind_offer.set(True)


def bind_offer_pending() -> bool:
    return bool(_bind_offer.get())


def clear_bind_offer() -> None:
    _bind_offer.set(False)


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
    # 绑定写在另一条请求里。只读事务不结束的话，SQLite 会一直看到绑定前的快照。
    if not db.new and not db.dirty and not db.deleted:
        db.rollback()
    return (
        db.query(Customer)
        .populate_existing()
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


def gender_zh(value: str) -> str:
    text = (value or "").strip().lower()
    if text in {"male", "m", "公", "雄"}:
        return "公"
    if text in {"female", "f", "母", "雌"}:
        return "母"
    return "未知"


def normalize_coat(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    key = text.lower().replace(" ", "").replace("_", "")
    table = {
        "short": "短毛",
        "长": "长毛",
        "长毛": "长毛",
        "long": "长毛",
        "中": "中毛",
        "中毛": "中毛",
        "medium": "中毛",
        "短": "短毛",
        "短毛": "短毛",
    }
    if key in table:
        return table[key]
    if "长毛" in text:
        return "长毛"
    if "短毛" in text:
        return "短毛"
    if "中毛" in text:
        return "中毛"
    return ""


def format_kg(value: float) -> str:
    return f"{round(float(value), 2):.2f}".rstrip("0").rstrip(".")


def _usable_day(raw: str) -> str:
    text = (raw or "").strip()
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not match:
        return ""
    try:
        day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return ""
    if day.year < 1900:
        return ""
    return day.isoformat()


def _business_day(raw) -> str:
    if isinstance(raw, datetime):
        return ""
    return _usable_day("" if raw is None else str(raw))


def _stored_day(value) -> str:
    if not isinstance(value, datetime):
        return ""
    return _usable_day(format_beijing(value, "%Y-%m-%d"))


def _pick_day(business, stored) -> str:
    if isinstance(business, datetime):
        return _stored_day(business)
    if business not in (None, ""):
        parsed = _business_day(business)
        if parsed:
            return parsed
        if re.search(r"\d{4}-\d{2}-\d{2}", str(business)):
            return ""
    return _stored_day(stored)


def _months_ago(today: date, months: int) -> date:
    month = today.month - months
    year = today.year
    while month <= 0:
        month += 12
        year -= 1
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(today.day, last))


def _loose_weight(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        weight = float(value)
    else:
        text = str(value).strip().lower()
        if not text or text in {"未知", "不详", "无", "-", "—", "null"}:
            return None
        match = re.search(r"(\d+(?:\.\d+)?)", text)
        if not match:
            return None
        weight = float(match.group(1))
    if weight <= 0 or weight > 200:
        return None
    return round(weight, 2)


def _source_rows(db: Session, label: str, query):
    try:
        return query.all()
    except Exception:
        logger.warning("[ai-cs] skip pet source %s", label, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return []


def _consider(best: dict, pet_id: int, day: str, priority: int, seq: int, kg: float | None, source: str) -> None:
    weight = _loose_weight(kg)
    if pet_id <= 0 or weight is None:
        return
    rank = (1 if day else 0, day or "", priority, seq)
    current = best.get(pet_id)
    if current is None or rank > current[0]:
        best[pet_id] = (rank, weight, day or "", source)


def weight_facts_for(db: Session, pet_ids: list[int]) -> dict[int, dict]:
    ids = [int(pid) for pid in pet_ids if int(pid) > 0]
    if not ids:
        return {}
    best: dict[int, tuple] = {}
    for row in _source_rows(db, "weight_records", db.query(WeightRecord).filter(WeightRecord.pet_id.in_(ids))):
        _consider(
            best, int(row.pet_id or 0), _pick_day(row.record_date, row.created_at),
            40, int(row.id or 0), row.weight_kg, "weight_records",
        )
    for row in _source_rows(db, "deworming_records", db.query(DewormingRecord).filter(DewormingRecord.pet_id.in_(ids))):
        if (row.status or "") == "voided":
            continue
        _consider(
            best, int(row.pet_id or 0), _pick_day(row.deworm_date, row.created_at),
            10, int(row.id or 0), row.weight_kg, "deworming_records",
        )
    for row in _source_rows(db, "hospitalizations", db.query(Hospitalization).filter(Hospitalization.pet_id.in_(ids))):
        _consider(
            best, int(row.pet_id or 0), _pick_day("", row.admitted_at),
            20, int(row.id or 0), row.admission_weight_kg, "hospitalizations",
        )
    for row in _source_rows(
        db, "anesthesia_monitor_sheets",
        db.query(AnesthesiaMonitorSheet).filter(AnesthesiaMonitorSheet.pet_id.in_(ids)),
    ):
        _consider(
            best, int(row.pet_id or 0), _pick_day(row.monitor_date, row.created_at),
            30, int(row.id or 0), row.weight_kg, "anesthesia_monitor_sheets",
        )
    for row in _source_rows(db, "applications", db.query(Application).filter(Application.pet_id.in_(ids))):
        _consider(
            best, int(row.pet_id or 0), _stored_day(row.created_at),
            0, int(row.id or 0), row.weight_estimate, "applications",
        )
    today = beijing_today()
    cutoff = _months_ago(today, 6)
    out = {}
    for pet_id, (_rank, weight, day, source) in best.items():
        stale = True
        if day:
            try:
                stale = date.fromisoformat(day) < cutoff
            except ValueError:
                stale = True
        out[pet_id] = {
            "weight_kg": weight,
            "weight_date": day,
            "weight_source": source,
            "weight_older_than_6_months": stale,
        }
    return out


def latest_weight_fact(db: Session, pet_id: int) -> dict | None:
    return weight_facts_for(db, [pet_id]).get(int(pet_id or 0))


def latest_weight_kg(db: Session, pet_id: int) -> float | None:
    fact = latest_weight_fact(db, pet_id)
    if not fact:
        return None
    return fact.get("weight_kg")


def coat_facts_for(db: Session, pet_ids: list[int]) -> dict[int, dict]:
    ids = [int(pid) for pid in pet_ids if int(pid) > 0]
    if not ids:
        return {}
    best: dict[int, tuple] = {}
    for row in _source_rows(db, "grooming_orders", db.query(GroomingOrder).filter(GroomingOrder.pet_id.in_(ids))):
        if (row.status or "") == "voided":
            continue
        coat = normalize_coat(row.coat_length or "")
        pid = int(row.pet_id or 0)
        if not coat or pid <= 0:
            continue
        day = _pick_day(row.groom_date, row.created_at)
        rank = (1 if day else 0, day or "", 2, int(row.id or 0))
        current = best.get(pid)
        if current is None or rank > current[0]:
            best[pid] = (rank, coat, day, "grooming_orders")
    skipped = {AppointmentStatus.cancelled.value, AppointmentStatus.no_show.value}
    for row in _source_rows(db, "appointments", db.query(Appointment).filter(Appointment.pet_id.in_(ids))):
        if (row.status or "") in skipped:
            continue
        coat = normalize_coat(row.coat_length or "")
        pid = int(row.pet_id or 0)
        if not coat or pid <= 0:
            continue
        day = _pick_day(row.appointment_date, row.created_at)
        rank = (1 if day else 0, day or "", 1, int(row.id or 0))
        current = best.get(pid)
        if current is None or rank > current[0]:
            best[pid] = (rank, coat, day, "appointments")
    return {
        pet_id: {
            "coat_length": coat,
            "coat_date": day,
            "coat_source": source,
        }
        for pet_id, (_rank, coat, day, source) in best.items()
    }


def latest_coat_fact(db: Session, pet_id: int) -> dict | None:
    return coat_facts_for(db, [pet_id]).get(int(pet_id or 0))


def pet_age_fields(raw: str, today: date | None = None) -> tuple[str, str]:
    today = today or beijing_today()
    text = (raw or "").strip()
    if not text:
        return "", ""
    birth = parse_birth_date(text, today)
    if birth is None or birth > today:
        return "", ""
    months_total = (today.year - birth.year) * 12 + (today.month - birth.month)
    if today.day < birth.day:
        months_total -= 1
    if months_total < 0:
        return "", ""
    years, rem = divmod(months_total, 12)
    if years >= 1 and rem:
        label = f"{years}岁{rem}个月"
    elif years >= 1:
        label = f"{years}岁"
    elif months_total >= 1:
        label = f"{months_total}个月"
    else:
        label = f"{(today - birth).days}天"
    normalized = text.replace("/", "-").replace(".", "-")
    birthday = birth.isoformat() if re.fullmatch(r"\d{4}-\d{1,2}(?:-\d{1,2})?", normalized) else ""
    return label, birthday


def _pet_brief(pet: Pet) -> dict:
    return {
        "pet_id": pet.id,
        "name": (pet.name or "").strip() or "未命名",
        "species": species_zh(pet.species or ""),
    }


def _pet_chart(db: Session, pet: Pet) -> dict:
    age, birthday = pet_age_fields(pet.birthday_estimate or "")
    item = {
        **_pet_brief(pet),
        "breed": (pet.breed or "").strip(),
        "gender": gender_zh(pet.gender or ""),
        "age": age,
        "birthday": birthday,
        "is_neutered": bool(pet.is_neutered),
        "weight_kg": None,
        "weight_date": "",
        "weight_source": "",
        "weight_older_than_6_months": None,
        "coat_length": "",
        "coat_date": "",
        "coat_source": "",
    }
    weight = latest_weight_fact(db, pet.id)
    if weight:
        item.update(weight)
    coat = latest_coat_fact(db, pet.id)
    if coat:
        item.update(coat)
    return item


def _profile_payload(db: Session, customer: Customer, detail_ids: set[int] | None) -> dict:
    pets = []
    for pet in _pets(db, customer.id):
        if detail_ids is None or pet.id in detail_ids:
            pets.append(_pet_chart(db, pet))
        else:
            pets.append(_pet_brief(pet))
    return {
        "customer_id": customer.id,
        "name": (customer.name or "").strip() or "未命名",
        "phone_masked": mask_phone(customer.phone or ""),
        "pets": pets,
    }


def describe_profiles(db: Session, openid: str, pet_id=None) -> dict:
    rows = customers_for_openid(db, openid)
    owned: list[Pet] = []
    for row in rows:
        owned.extend(_pets(db, row.id))
    selected = None
    unknown_pet = False
    if pet_id not in (None, "", 0):
        try:
            wanted = int(pet_id)
        except (TypeError, ValueError):
            wanted = 0
        selected = next((pet for pet in owned if pet.id == wanted), None)
        unknown_pet = selected is None
    ask_which = len(rows) > 1 or len(owned) > 1
    if selected is not None:
        detail_ids = {selected.id}
        ask_which = False
        next_step = (
            "这一只已经选定。使用档案里的品种、年龄、体重、毛长和绝育，只问还缺的，或早于 6 个月的体重。"
            "对客户用口语，不要念档案标签，也不要解释为什么要问。"
        )
    elif not ask_which:
        detail_ids = {pet.id for pet in owned}
        next_step = "只有一只宠物，直接使用档案。只问还缺的；体重早于 6 个月要确认变化大不大。对客户用口语。"
    else:
        detail_ids = set()
        next_step = (
            "先问是哪一只。主人选定后，再用 pet_id 调用 list_profiles_and_pets 加载这一只的档案，只问还缺的或需要确认的。"
            "用名字问，不要说「您名下有两只需要安排」。"
        )
    profiles = [_profile_payload(db, row, detail_ids) for row in rows]
    payload = {
        "bound": bool(profiles),
        "profile_count": len(profiles),
        "need_profile_choice": len(profiles) > 1,
        "ask_which_pet_first": ask_which,
        "selected_pet_id": selected.id if selected is not None else None,
        "next_step": next_step if profiles else BIND_HINT,
        "profiles": profiles,
        "bind_hint": "" if profiles else BIND_HINT,
    }
    if unknown_pet:
        payload["message"] = "这只宠物不在您绑定的档案里。"
    return payload


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

    人工会话打开时也会走到这里（begin_human_handoff 写 ai_cs_handoffs）。
    现在只把 staff_notify_status 留在 pending_step5，不外发。
    """
    row = db.get(AiCsHandoff, handoff_id)
    if row is None:
        return
    if (row.staff_notify_status or "") != "pending_step5":
        row.staff_notify_status = "pending_step5"
        db.commit()
    logger.info("[ai-cs] handoff %s waiting for step-5 staff template", handoff_id)


def handoff_idle_minutes() -> int:
    """人工会话连续多久没有客户消息就结束。环境变量优先，默认 30。"""
    if "AI_CS_HANDOFF_IDLE_MINUTES" in os.environ:
        raw = os.environ.get("AI_CS_HANDOFF_IDLE_MINUTES") or ""
    else:
        raw = getattr(settings, "ai_cs_handoff_idle_minutes", 30)
    try:
        minutes = int(str(raw).strip())
    except (TypeError, ValueError):
        return 30
    if minutes < 1:
        return 30
    return min(minutes, 7 * 24 * 60)


def _session_is_idle(row: AiCsHandoffSession, now: datetime | None = None) -> bool:
    if row.ended_at is not None:
        return False
    last = row.last_customer_msg_at or row.started_at
    if last is None:
        return False
    current = now or beijing_naive()
    return current - last >= timedelta(minutes=handoff_idle_minutes())


def expire_idle_sessions(db: Session, openid: str | None = None) -> int:
    """把超过空闲窗口、仍未结束的人工会话标成 idle。只补结束时间，不删行。"""
    query = db.query(AiCsHandoffSession).filter(AiCsHandoffSession.ended_at.is_(None))
    if openid is not None:
        query = query.filter(AiCsHandoffSession.openid == (openid or "")[:64])
    now = beijing_naive()
    ended = 0
    for row in query.all():
        if not _session_is_idle(row, now):
            continue
        row.ended_at = now
        row.end_reason = "idle"
        ended += 1
        logger.info("[ai-cs] handoff session %s ended reason=idle", row.id)
    if ended:
        db.commit()
    return ended


def active_handoff_session(db: Session, openid: str) -> AiCsHandoffSession | None:
    """这个微信还在人工接待中就返回那一行。空闲超时的会先结束。"""
    key = (openid or "").strip()[:64]
    if not key:
        return None
    expire_idle_sessions(db, key)
    return (
        db.query(AiCsHandoffSession)
        .filter(AiCsHandoffSession.openid == key, AiCsHandoffSession.ended_at.is_(None))
        .order_by(AiCsHandoffSession.id.desc())
        .first()
    )


def touch_handoff_session(db: Session, row: AiCsHandoffSession) -> None:
    row.last_customer_msg_at = beijing_naive()
    db.commit()


def begin_human_handoff(
    db: Session,
    openid: str,
    reason: str,
    note: str = "",
    *,
    transfer_this_message: bool,
) -> str:
    """打开或延续人工会话，并写一条 ai_cs_handoffs。

    能在本次微信响应里转接时，返回空字符串，调用方应回 transfer_customer_service。
    本次已经应答过、微信不再接受转接时，返回电话文案。会话仍然打开，
    客户的下一条消息会转接。会话写不进去时，才只记 phone_fallback。

    同事通知走 notify_staff_later，状态保持 pending_step5。
    """
    key = (openid or "").strip()[:64]
    if not key:
        return PHONE_FALLBACK
    active = active_handoff_session(db, key)
    if active is not None:
        active.last_customer_msg_at = beijing_naive()
        db.commit()
        return "" if transfer_this_message else PHONE_FALLBACK

    now = beijing_naive()
    full_note = note or ""
    if not transfer_this_message:
        extra = "当前这条已无法转接，已回复电话，后续消息转人工"
        full_note = f"{full_note}；{extra}" if full_note else extra
    try:
        row = AiCsHandoffSession(
            openid=key,
            started_at=now,
            last_customer_msg_at=now,
            ended_at=None,
            end_reason="",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
    except SQLAlchemyError:
        db.rollback()
        logger.exception("[ai-cs] handoff session insert failed")
        try:
            record_handoff(db, key, reason, "phone_fallback", note or reason)
        except SQLAlchemyError:
            logger.exception("[ai-cs] phone fallback log failed")
        return PHONE_FALLBACK
    try:
        handoff_id = record_handoff(db, key, reason, "wechat_transfer", full_note)
        logger.info(
            "[ai-cs] handoff session %s opened openid=%s reason=%s handoff=%s",
            row.id, key[:8], (reason or "")[:40], handoff_id,
        )
    except SQLAlchemyError:
        logger.exception("[ai-cs] handoff log failed")
    try:
        _clear_pending(db, key)
    except SQLAlchemyError:
        logger.exception("[ai-cs] clear pending failed")
    return "" if transfer_this_message else PHONE_FALLBACK


def end_handoff_session(db: Session, session_id: int) -> str:
    """员工点「结束人工、恢复AI」。已经结束过的不改原因。"""
    row = db.get(AiCsHandoffSession, session_id)
    if row is None:
        return "missing"
    if row.ended_at is not None:
        return "already"
    row.ended_at = beijing_naive()
    row.end_reason = "staff"
    db.commit()
    logger.info("[ai-cs] handoff session %s ended reason=staff", row.id)
    return "staff"


def log_handoff_customer_message(db: Session, openid: str, message: dict) -> None:
    """人工会话进行中的客户消息。只记转接，不调用 AI。"""
    kind = (message.get("msg_type") or "").lower()
    content = (message.get("content") or "").strip()
    if not content:
        if kind == "image":
            content = "（图片，已转给人工）"
        elif kind == "miniprogrampage":
            title = (message.get("title") or "").strip()
            content = "（小程序卡片，已转给人工）"
            if title:
                content = f"{content}{title}"
        elif kind == "event":
            event = (message.get("event") or "").lower()
            content = "（进入客服，已转给人工）" if event == "user_enter_tempsession" else "（事件，已转给人工）"
        else:
            content = "（这条消息已转给人工）"
    _save_message(db, openid, "user", content, "handoff")


def _resume_notice_due(db: Session, openid: str) -> bool:
    key = (openid or "").strip()[:64]
    if not key:
        return False
    latest = (
        db.query(AiCsHandoffSession)
        .filter(AiCsHandoffSession.openid == key)
        .order_by(AiCsHandoffSession.id.desc())
        .first()
    )
    if latest is None or latest.ended_at is None:
        return False
    announced = (
        db.query(AiCsMessage)
        .filter(
            AiCsMessage.openid == key,
            AiCsMessage.role == "assistant",
            AiCsMessage.created_at >= latest.ended_at,
        )
        .first()
    )
    return announced is None


def with_resume_notice(db: Session, openid: str, reply: str) -> str:
    text = (reply or "").strip()
    if not text or text.startswith(RESUME_NOTE):
        return text
    if not _resume_notice_due(db, openid):
        return text
    return RESUME_NOTE + "\n" + text


def _deliver_assistant(db: Session, openid: str, reply: str, msg_type: str) -> str:
    text = sanitize_wechat_text(with_resume_notice(db, openid, reply))
    if not text:
        return ""
    _save_message(db, openid, "assistant", text, msg_type)
    return text


def _rate_limited(db: Session, openid: str) -> bool:
    cutoff = beijing_naive() - timedelta(minutes=RATE_WINDOW_MINUTES)
    count = (
        db.query(AiCsMessage)
        .filter(
            AiCsMessage.openid == openid,
            AiCsMessage.role == "user",
            AiCsMessage.msg_type != "handoff",
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
    if active_handoff_session(db, openid) is not None:
        return ""
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
    text = GREETING if customers_for_openid(db, openid) else GREETING_UNBOUND
    return _deliver_assistant(db, openid, text, "greeting")


def media_reply(db: Session, openid: str) -> str:
    if active_handoff_session(db, openid) is not None:
        return ""
    if _rate_limited(db, openid):
        return _deliver_assistant(db, openid, RATE_REPLY, "rate_limit")
    return _deliver_assistant(db, openid, MEDIA_REPLY, "media")


def bind_card_thumb_path() -> Path:
    if "AI_CS_BIND_CARD_THUMB_PATH" in os.environ:
        raw = os.environ.get("AI_CS_BIND_CARD_THUMB_PATH") or ""
    else:
        raw = getattr(settings, "ai_cs_bind_card_thumb_path", "") or ""
    raw = raw.strip()
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parents[2] / "static" / "ai_cs" / "bind_card_thumb.png"


def prepare_bind_card_thumb(db: Session) -> str:
    """上传或复用绑定卡片封面，返回 media_id。失败时抛异常。"""
    path = bind_card_thumb_path()
    if not path.is_file():
        raise RuntimeError(f"绑定卡片封面不存在: {path}")
    stat = path.stat()
    stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
    now = beijing_naive()
    row = db.get(AiCsMediaCache, BIND_CARD_CACHE_KEY)
    if (
        row is not None
        and (row.media_id or "")
        and (row.source_stamp or "") == stamp
        and row.expires_at is not None
        and row.expires_at > now
    ):
        return row.media_id
    from app.services.wechat_miniapp import upload_temp_image

    media_id = upload_temp_image(str(path))
    if row is None:
        row = AiCsMediaCache(cache_key=BIND_CARD_CACHE_KEY)
        db.add(row)
    row.media_id = media_id[:128]
    row.source_stamp = stamp[:80]
    row.expires_at = now + timedelta(hours=BIND_CARD_MEDIA_HOURS)
    db.commit()
    return row.media_id


def bind_card_on_cooldown(db: Session, openid: str) -> bool:
    cutoff = beijing_naive() - timedelta(minutes=BIND_CARD_COOLDOWN_MINUTES)
    found = (
        db.query(AiCsMessage)
        .filter(
            AiCsMessage.openid == (openid or "")[:64],
            AiCsMessage.msg_type == "bind_card",
            AiCsMessage.created_at >= cutoff,
        )
        .first()
    )
    return found is not None


def should_attach_bind_card(db: Session, openid: str, *, kind: str, user_text: str, reply: str) -> bool:
    flagged = bind_offer_pending()
    clear_bind_offer()
    text = (reply or "").strip()
    if not text or not (openid or "").strip() or text == RATE_REPLY:
        return False
    if customers_for_openid(db, openid):
        return False
    if kind == "greeting":
        return True
    if kind != "text":
        return False
    if flagged or needs_binding_intent(user_text) or BIND_CARD_ASK in reply:
        return True
    return False


def deliver_bind_card(db: Session, openid: str) -> str:
    """未绑定且需要档案时，在文字回复后再发一张绑定卡片。失败就改发一句文字。"""
    key = (openid or "").strip()[:64]
    if not key:
        return "skipped"
    try:
        if customers_for_openid(db, key):
            return "skipped_bound"
        if bind_card_on_cooldown(db, key):
            return "skipped_rate"
    except Exception:
        logger.exception("[ai-cs] bind card check failed")
        return "error"
    from app.services.wechat_miniapp import (
        send_customer_service_miniprogram_page,
        send_customer_service_text,
    )

    try:
        media_id = prepare_bind_card_thumb(db)
        send_customer_service_miniprogram_page(
            key,
            title=BIND_CARD_TITLE,
            pagepath=BIND_PAGE_PATH,
            thumb_media_id=media_id,
        )
        _save_message(db, key, "assistant", BIND_CARD_LOG, "bind_card")
        return "card"
    except Exception:
        logger.exception("[ai-cs] bind card send failed")
    try:
        send_customer_service_text(key, clip_wechat(BIND_CARD_FALLBACK))
        _save_message(db, key, "assistant", BIND_CARD_FALLBACK, "bind_card")
        return "fallback"
    except Exception:
        logger.exception("[ai-cs] bind card fallback failed")
        try:
            _save_message(db, key, "assistant", "（绑定卡片和补发文字都没发出去）", "bind_card_error")
        except Exception:
            logger.exception("[ai-cs] bind card error log failed")
        return "error"


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
            sizes = list(category.get("size_options") or [])
            for extra in _main().BEAUTY_CAT_SIZE_OPTIONS:
                if extra not in sizes:
                    sizes.append(extra)
            item["sizes"] = sizes
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
            "message": "名下有多只宠物，请先问是哪一只。选定前不要问体重、毛长或品种。",
            "pets": [_pet_brief(pet) for pet in pets],
        }
    if not pet_id:
        return pets[0], None
    chosen = next((pet for pet in pets if pet.id == int(pet_id)), None)
    if chosen is None:
        return None, {"ok": False, "message": "这只宠物不在所选档案里。"}
    return chosen, None


def _booking_script(store: str, service: str, pet: Pet, day: str, clock: str, extra: dict, detail: dict | None = None) -> str:
    pet_name = pet.name or "未命名"
    species = species_zh(pet.species or "")
    place = store_short(store)
    lines = [f"我帮您确认一下：{pet_name}（{species}）{day} {clock} 到{place}做{service}。"]
    detail = detail or {}
    notes = []
    coat = detail.get("coat_length") or ""
    if coat:
        if detail.get("coat_from_file"):
            coat_date = detail.get("coat_date") or ""
            when = f"，上次是 {coat_date}" if coat_date else ""
            notes.append(f"毛长按{coat}算{when}")
        else:
            notes.append(f"毛长是{coat}")
    if detail.get("weight_kg") not in (None, ""):
        weight_text = f"体重 {detail['weight_kg']} 公斤"
        if detail.get("weight_from_file"):
            weight_date = detail.get("weight_date") or ""
            when = f"上次记录 {weight_date}" if weight_date else "上次记录"
            if detail.get("weight_stale"):
                weight_text += f"（{when}，已经超过 6 个月了，变化大不大呀）"
            else:
                weight_text += f"（{when}）"
        notes.append(weight_text)
    if notes:
        lines.append("，".join(notes) + "。")
    duration = extra.get("duration_minutes")
    if duration:
        lines.append(f"大概要 {duration} 分钟，到店如果毛量差很多，以门店为准。")
    if extra.get("arrival_note"):
        lines.append(str(extra["arrival_note"]))
    if extra.get("warning"):
        lines.append(str(extra["warning"]))
    lines.append("对的话回复「确认」，我再帮您提交。想改店、项目、宠物或时间，直接说就行。")
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
    size = str(args.get("pet_size") or "").strip()
    supplied_coat = normalize_coat(str(args.get("coat_length") or ""))
    coat = supplied_coat
    coat_from_file = False
    coat_date = ""
    if kind == "beauty" and not coat:
        coat_fact = latest_coat_fact(db, pet.id) or {}
        coat = coat_fact.get("coat_length") or ""
        coat_date = coat_fact.get("coat_date") or ""
        coat_from_file = bool(coat)
    supplied_weight = args.get("weight_kg")
    weight = None if supplied_weight in (None, "") else supplied_weight
    weight_from_file = False
    weight_date = ""
    weight_stale = False
    parsed_weight = _main()._parse_weight_kg(weight)
    if kind == "beauty" and parsed_weight is None:
        weight_fact = latest_weight_fact(db, pet.id) or {}
        if weight_fact.get("weight_kg"):
            parsed_weight = weight_fact["weight_kg"]
            weight_from_file = True
            weight_date = weight_fact.get("weight_date") or ""
            weight_stale = bool(weight_fact.get("weight_older_than_6_months"))
    if kind == "beauty" and not size:
        size = _main().resolve_beauty_pet_size(
            "", species=pet.species or "", service_name=service, weight_kg=parsed_weight,
        )
    if kind == "beauty" and not size:
        return {"ok": False, "message": BEAUTY_WEIGHT_REQUIRED_MESSAGE}
    if kind == "beauty" and coat == "中毛":
        return {
            "ok": False,
            "message": "毛长记的是中毛。请先用口语问客户按长毛还是短毛，不要猜，也不要说档案。",
        }
    if kind == "beauty" and not coat:
        return {
            "ok": False,
            "message": "还没有毛长。请直接问客户是长毛还是短毛，例如「是短毛吧？」。不要说「档案里没有」，也不要解释原因。品种只能提示，不能当成已确认。",
        }
    try:
        looked = query_slots(
            db,
            store=store,
            day=day,
            kind=kind,
            service_name=service,
            pet_size=size,
            coat_length=coat,
            addons=str(args.get("addons") or ""),
            weight_kg=parsed_weight,
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
        "pet_size": size,
        "coat_length": coat,
        "addons": str(args.get("addons") or ""),
        "weight_kg": parsed_weight if parsed_weight is not None else "",
        "notes": clip(redact(str(args.get("notes") or "")), 200),
    }
    detail = {
        "coat_length": coat,
        "coat_from_file": coat_from_file,
        "coat_date": coat_date,
        "weight_kg": format_kg(parsed_weight) if parsed_weight else "",
        "weight_from_file": weight_from_file,
        "weight_date": weight_date,
        "weight_stale": weight_stale,
    }
    script = _booking_script(payload["store"], payload["service_name"], pet, day, clock, looked, detail)
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
    current_row = db.get(Appointment, int(current["id"]))
    stored_duration = int(current_row.duration_minutes or 0) if current_row is not None else 0
    try:
        looked = query_slots(
            db,
            store=store,
            day=day,
            kind=kind,
            service_name=current["service_name"],
            pet_id=current["pet_id"],
            pet_size=(current_row.pet_size or "") if current_row is not None else "",
            coat_length=(current_row.coat_length or "") if current_row is not None else "",
            addons=(current_row.addon_services or "") if current_row is not None else "",
            # 已保存的时长原样看位。没有时长时才用体型或档案体重，都没有就拒绝。
            duration_override=stored_duration if stored_duration > 0 else None,
            require_beauty_size=stored_duration <= 0,
        )
    except AiCsError as exc:
        message = exc.message
        if exc.code == "pet_weight_required":
            message = "请先填写体型或体重"
        return {"ok": False, "message": message, "error_code": exc.code}
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
        f"我帮您确认一下改期：原来是{current['pet_name']} {current['date']} {current['time']} "
        f"在{current['store']}做{current['service_name']}，单号 {current['id']}。\n"
        f"改到 {store_short(looked.get('store') or store)} {day} {clock}。\n"
        "对的话回复「确认」，我再改。想换时间直接说就行。"
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
        f"我帮您确认一下：取消 {current['pet_name']} {current['date']} {current['time']} "
        f"在{current['store']}的{current['service_name']}，单号 {current['id']}。\n"
        "当天取消也可以，没有迟到处罚。对的话回复「确认」。想留下就直接说。"
    )
    _stage(db, openid, "cancel", payload, script)
    return {"ok": True, "staged": True, "script": script}


def _owned_pet(db: Session, openid: str, pet_id) -> Pet | None:
    try:
        wanted = int(pet_id or 0)
    except (TypeError, ValueError):
        return None
    if wanted <= 0:
        return None
    pet = db.get(Pet, wanted)
    if pet is None:
        return None
    owned_ids = {row.id for row in customers_for_openid(db, openid)}
    if pet.customer_id not in owned_ids:
        return None
    return pet


def tool_query_slots(db: Session, openid: str, args: dict) -> dict:
    pet = _owned_pet(db, openid, args.get("pet_id"))
    coat = normalize_coat(str(args.get("coat_length") or ""))
    weight = args.get("weight_kg") if args.get("weight_kg") not in (None, "") else None
    if pet is not None and not coat:
        coat = (latest_coat_fact(db, pet.id) or {}).get("coat_length") or ""
    if pet is not None and weight is None:
        weight = latest_weight_kg(db, pet.id)
    try:
        result = query_slots(
            db,
            store=str(args.get("store") or ""),
            day=str(args.get("date") or ""),
            kind=str(args.get("kind") or ""),
            service_name=str(args.get("service_name") or ""),
            pet_size=str(args.get("pet_size") or ""),
            coat_length=coat,
            addons=str(args.get("addons") or ""),
            weight_kg=weight,
            pet_id=pet.id if pet is not None else None,
            species=str(args.get("species") or "") or (pet.species if pet is not None else ""),
            birthday=str(args.get("birthday") or "") or (pet.birthday_estimate if pet is not None else ""),
        )
    except AiCsError as exc:
        return {"ok": False, "message": exc.message, "error_code": exc.code}
    slots = result.get("slots") or []
    details = list(result.get("slot_details") or [])
    if not details:
        details = [{"start": label, "end": ""} for label in slots]
    shown = details[:16]
    payload = {
        "ok": True,
        "store": store_short(result.get("store") or ""),
        "date": result.get("date") or "",
        "service_name": result.get("service_name") or "",
        "pet_size": result.get("pet_size") or "",
        "duration_minutes": result.get("duration_minutes"),
        "slots": shown,
        "slot_count": len(slots),
        "offer_rule": (
            "只报 slots 里的 start。结束时间用同一条的 end，不要自己用开始时间加时长推算。"
            "对客户不要一次列出全部空档。同一天从这些 start 里挑 3 到 4 个，按上午、下午、晚上分开说，并问要不要看更多。"
            "客户点名的时刻如果在 slots 里就报出来。不够 3 个就有几个说几个。不要编造 slots 里没有的时间。"
        ),
        "rules": result.get("rules") or {},
        "arrival_note": result.get("arrival_note") or "",
        "warning": result.get("warning") or "",
        "late_penalty": False,
        "coat_length": coat or "",
        "weight_kg": weight,
    }
    if result.get("segments"):
        payload["segments"] = result["segments"]
    if result.get("groups"):
        payload["groups"] = result["groups"]
    return payload


def execute_tool(db: Session, openid: str, name: str, args: dict) -> dict:
    args = args or {}
    try:
        if name == "list_services":
            result = {"ok": True, **_tool_list_services()}
        elif name == "list_profiles_and_pets":
            result = {"ok": True, **describe_profiles(db, openid, args.get("pet_id"))}
        elif name == "list_upcoming_appointments":
            profiles = describe_profiles(db, openid)
            if not profiles["bound"]:
                result = {"ok": False, "code": "unbound", "message": UNBOUND_REPLY}
            else:
                result = {"ok": True, "appointments": _upcoming(db, openid)}
        elif name == "query_slots":
            result = tool_query_slots(db, openid, args)
        elif name == "stage_booking":
            result = tool_stage_booking(db, openid, args)
        elif name == "stage_reschedule":
            result = tool_stage_reschedule(db, openid, args)
        elif name == "stage_cancel":
            result = tool_stage_cancel(db, openid, args)
        elif name == "request_human":
            raw_reason = clip(redact(str(args.get("reason") or "")), 80)
            reason = raw_reason or "low_confidence"
            reply = begin_human_handoff(
                db, openid, reason, raw_reason or reason, transfer_this_message=False,
            )
            result = {"ok": True, "handoff": True, "message": reply or PHONE_FALLBACK}
        else:
            result = {"ok": False, "message": "没有这个工具。"}
    except AiCsError as exc:
        result = {"ok": False, "message": exc.message, "error_code": exc.code}
    except Exception as exc:
        logger.exception("[ai-cs] tool %s failed", name)
        result = {"ok": False, "message": "刚才没有查成，请换个说法或转人工。"}
        result["error"] = type(exc).__name__
    if result.get("code") == "unbound":
        note_bind_needed()
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
            "description": (
                "查看这个微信绑定的档案和宠物。多只宠物时先只返回名字，不要问体重或毛长；"
                "主人选定后传入 pet_id，再返回这一只的品种、年龄、性别、绝育、最近体重和上次毛长。"
                "只有一只时直接返回这些档案。未绑定时请让客户点下面的卡片绑定，不要让对方去首页找按钮。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pet_id": {"type": "integer", "description": "主人选定哪一只之后再传。多只时先不要传。"},
                },
                "additionalProperties": False,
            },
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
            "description": (
                "查询某一天还能约的开始时间。不要自己编时间。"
                "美容没传 pet_size 时，用 weight_kg 或这只宠物档案里的最近体重分档。"
                "返回的每条空档都有 start 和 end。结束时间只用 end，不要自己加时长。"
            ),
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
            "description": (
                "生成预约确认稿。不会写入预约。多份档案或多只宠物时必须先让客户选定。"
                "美容请传客户确认后的 coat_length（长毛或短毛）和 weight_kg；"
                "不传时用该宠物档案里的上次毛长和最近体重占档，并写进确认稿。"
            ),
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
            "description": (
                "生成改期确认稿。不会立刻改期。"
                "美容沿用这张预约已保存的时长，不按新的体型表重算。"
                "若返回请先填写体型或体重，先问客户，不要按 60 分钟改期。"
            ),
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
            "description": "没有把握、知识库没有、或客户明确要人工时调用。不要猜价格和诊断。调用后会转给同事。",
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
        "你是大风动物医院小程序里的在线客服。用简体中文，像前台同事一样短、暖、具体。一次说清一件事。\n"
        f"现在是北京时间 {now}。最远可约 {HORIZON_DAYS} 天。\n"
        "你只能依据下面的知识库，以及工具返回的档案、空档和预约。知识库没写的价格、套餐、优惠、政策，"
        "就说医院还没提供，不要编。标了「尚未提供」的内容保持空白。\n"
        "猫三联须知原文写「8周龄以上的犬」是笔误，只按猫来说。\n"
        "不诊断、不开药、不解读报告。急症（呼吸困难、抽搐、昏迷、大量出血、站不起来、中毒）"
        "第一句就请客户立刻打电话并到店，电话用知识库里的号码。\n"
        "预约、改期、取消必须先调用 stage 工具。工具返回的确认话里，门店、项目、宠物、日期和时间都要告诉客户，"
        "等客户下一句明确同意。对客户说「我帮您确认一下」，不要说「确认稿」。"
        "没有同意之前，禁止说已经约好、已经改期或已经取消。\n"
        f"{PET_FILE_RULE}"
        "未绑定档案时，如果对方要预约、改期、取消，或者查看自己的预约和宠物，"
        f"就说「{BIND_CARD_ASK}」。不要让对方退出小程序去首页找按钮。"
        "一般须知仍可回答。已经绑定的不要再提绑定。\n"
        "没有把握或知识库没有时调用 request_human。\n\n"
        f"【当前绑定】\n{json.dumps(profiles, ensure_ascii=False)}\n\n"
        f"【知识库】\n{load_knowledge()}\n\n"
        f"{SPEAKING_STYLE}"
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
        return "刚才那条确认过期了。您再说一下哪家店、做什么、哪只宠物、哪天几点，我重新跟您对一下。"
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
            pet_name = first.get("pet_name") or ""
            service = first.get("service_name") or ""
            when = f"{first.get('appointment_date') or ''} {first.get('appointment_time') or ''}".strip()
            place = store_short(first.get("store") or "")
            text = f"已经帮您约好了。{pet_name} {when} 到{place}做{service}。"
            if first.get("id") not in (None, ""):
                text += f"单号是 {first.get('id')}。"
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
            return "已经取消了。当天取消也可以，没有迟到处罚。"
    except AiCsError as exc:
        return f"{exc.message} 这次没有提交成功。您可以换个时间再告诉我。"
    except Exception:
        logger.exception("[ai-cs] commit failed")
        return "这次没有提交成功。请稍后再试，或回复「人工」。"
    return "这次没有对上要办的事。您再说一下是预约、改期还是取消。"


async def handle_user_text(db: Session, openid: str, text: str) -> str:
    clear_bind_offer()
    cleaned = clip(redact(text), 1000)
    if not cleaned:
        return ""
    session = active_handoff_session(db, openid)
    if session is not None:
        touch_handoff_session(db, session)
        _save_message(db, openid, "user", cleaned, "handoff")
        return ""
    _save_message(db, openid, "user", cleaned, "text")
    if _rate_limited(db, openid):
        return _deliver_assistant(db, openid, RATE_REPLY, "rate_limit")

    pending = _pending(db, openid)
    if pending is not None and is_confirm(cleaned):
        reply = _commit_pending(db, openid)
        return _deliver_assistant(db, openid, reply, "commit")
    if pending is not None and is_decline(cleaned):
        _clear_pending(db, openid)
        reply = "好的，这个先不办啦。想改时间或者换项目，直接跟我说。"
        return _deliver_assistant(db, openid, reply, "decline")
    if pending is not None:
        _clear_pending(db, openid)

    if not customers_for_openid(db, openid) and needs_binding_intent(cleaned):
        note_bind_needed()
        return _deliver_assistant(db, openid, UNBOUND_REPLY, "unbound")

    reply = await _llm_turn(db, openid, cleaned)
    return _deliver_assistant(db, openid, reply, "assistant")


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
                    return begin_human_handoff(
                        db, openid, "low_confidence", "模型没有给出可靠回复",
                        transfer_this_message=False,
                    )
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
                    return str(result.get("message") or PHONE_FALLBACK)
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
        return begin_human_handoff(
            db, openid, "llm_unavailable", type(exc).__name__,
            transfer_this_message=False,
        )
    return begin_human_handoff(
        db, openid, "low_confidence", "工具轮次用尽",
        transfer_this_message=False,
    )


def admin_snapshot(db: Session) -> dict:
    expire_idle_sessions(db)
    active_sessions = (
        db.query(AiCsHandoffSession)
        .filter(AiCsHandoffSession.ended_at.is_(None))
        .order_by(AiCsHandoffSession.started_at.asc(), AiCsHandoffSession.id.asc())
        .limit(100)
        .all()
    )
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
    return {
        "conversations": conversations,
        "handoffs": handoffs,
        "active_sessions": active_sessions,
        "idle_minutes": handoff_idle_minutes(),
    }
