"""拍摄模式：只改当前登录会话看到的页面，不写数据库。

开启后，这一次浏览器会话的 HTML / JSON 响应会：
- 把医院品牌名换成「XX动物医院」
- 把门店显示名换成「一店 / 二店」，链接和表单提交值仍是真实店名
- 去掉「始于 2018」这类开业年份标语
- 把客户（主人）姓名、手机号打码
- 把医生 / 员工姓名打码（导航栏登录名、主诊、开单人、操作人、日志等）

客户姓名电话的输入框改成打码且不可编辑，避免星号被存回去。
页面上原来的隐藏域保持真名，预约等流程提交的仍是真实数据。
医生下拉框的显示文字打码，option 的提交值仍是真名。
手填的医生姓名框画面上是打码，提交到服务器时再按打码对照表还原成真名。
"""
from __future__ import annotations

import html
import json
import logging
import re
import threading
from contextvars import ContextVar

logger = logging.getLogger(__name__)

_filming_ctx: ContextVar[bool] = ContextVar("filming_mode", default=False)

# 先替换长的，避免「大风动物医院」被短词拆开。
_BRAND_PAIRS: tuple[tuple[str, str], ...] = (
    ("大风动物医院", "XX动物医院"),
    ("大风宠物医院", "XX宠物医院"),
    ("大风宠物", "XX宠物"),
    ("Dafo Animal Hospital", "XX Animal Hospital"),
    ("DAFO Animal Hospital", "XX Animal Hospital"),
)
# 前后不是汉字/字母/数字时，单独的品牌简称「大风」（如 PWA short_name）。
_STANDALONE_BRAND = re.compile(r"(?<![\u4e00-\u9fffA-Za-z0-9])大风(?![\u4e00-\u9fffA-Za-z0-9])")
_DAFOPET = re.compile(r"dafopet", re.IGNORECASE)
# 字与字之间允许空格或 &nbsp;，以盖住「东&nbsp;环&nbsp;店」这种排版。
_STORE_GAP = r"(?:[\s\u00a0]|&nbsp;|&#160;|&#x0*a0;)*"
_STORE_DISPLAY: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(rf"东{_STORE_GAP}环{_STORE_GAP}分院"), "一分院"),
    (re.compile(rf"横{_STORE_GAP}岗{_STORE_GAP}分院"), "二分院"),
    (re.compile(rf"东{_STORE_GAP}环{_STORE_GAP}店"), "一店"),
    (re.compile(rf"横{_STORE_GAP}岗{_STORE_GAP}店"), "二店"),
    # 单独作为店名出现的简称。后面紧挨汉字（东环路、东环客户、横岗街道）则不动。
    (re.compile(rf"(?<![\u4e00-\u9fff])东{_STORE_GAP}环(?!{_STORE_GAP}[\u4e00-\u9fff])"), "一店"),
    (re.compile(rf"(?<![\u4e00-\u9fff])横{_STORE_GAP}岗(?!{_STORE_GAP}[\u4e00-\u9fff])"), "二店"),
)
_STORE_QUERY = re.compile(r"(?:[?&]|&amp;)store=[^&\"'<>\s]*", re.IGNORECASE)
_TAGLINES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:\s*[·•|]\s*)?(?:est\.?\s*)?始\s*于\s*2018", re.IGNORECASE),
    re.compile(r"(?:\s*[·•|]\s*)?since\s+2018", re.IGNORECASE),
    re.compile(r"(?:\s*[·•|]\s*)?est\.?\s*2018", re.IGNORECASE),
    re.compile(r"(?:\s*[·•|]\s*)?创建于\s*2018"),
)
# 万一画面上的店名被提交回来，写库前改回真实值。
_STORE_UNMASK = (
    ("XX动物医院（一店）", "大风动物医院（东环店）"),
    ("XX动物医院（二店）", "大风动物医院（横岗店）"),
    ("XX动物医院（东环店）", "大风动物医院（东环店）"),
    ("XX动物医院（横岗店）", "大风动物医院（横岗店）"),
    ("大风动物医院（一店）", "大风动物医院（东环店）"),
    ("大风动物医院（二店）", "大风动物医院（横岗店）"),
    ("一分院", "东环店"),
    ("二分院", "横岗店"),
    ("一店", "东环店"),
    ("二店", "横岗店"),
)
# 大陆手机：允许中间有空格或短横。已打码的 **** 不会再命中。
_MOBILE = re.compile(r"(?<!\d)(1[3-9]\d)[-\s]?(\d{4})[-\s]?(\d{4})(?!\d)")

_IDENTITY_FIELDS = {
    "name",
    "phone",
    "phones_extra",
    "customer_name",
    "applicant_name",
    "proxy_name",
    "proxy_phone",
    "owner_name",
    "owner_phone",
    "emergency_contact_name",
    "emergency_contact_phone",
}
_SKIP_INPUT_TYPES = {
    "hidden",
    "checkbox",
    "radio",
    "submit",
    "button",
    "file",
    "image",
    "reset",
}
# 这些词如果刚好是某位客户的姓名，全局替换会把按钮和门店简称打坏。
_NAME_SKIP = {
    "未命名",
    "客户",
    "主人",
    "先生",
    "女士",
    "小姐",
    "未知",
    "匿名",
    "散客",
    "东环",
    "横岗",
    "东环店",
    "横岗店",
    "宠物",
    "动物",
    "拍摄",
    "拍摄模式",
    "关闭",
    "关闭拍摄",
    # 职位和界面用词。若真有人叫这个，也不能拿来做整页替换，否则「医生」两个字会从按钮上消失。
    "医生",
    "医师",
    "助理",
    "护士",
    "前台",
    "美容师",
    "员工",
    "管理员",
    "超级管理员",
    "系统",
    "一店",
    "二店",
    "一分院",
    "二分院",
}
# 登录名里的系统账号，不当成真人姓名打码。
_ACCOUNT_SKIP = {
    "admin",
    "test",
    "staff",
    "system",
    "miniapp",
    "root",
    "guest",
    "import",
    "warmsoft",
    "api",
    "bot",
    "null",
    "none",
}
# 这些字段提交的是员工姓名。画面上打码后，写库前按对照表还原。
# 不要放客户的 name / phone：那些字段靠星号判断「保持原值」，还原错人会改掉客户档案。
_STAFF_VALUE_FIELDS = {
    "vet_name",
    "staff_name",
    "examiner",
    "groomer_name",
    "assistant_name",
    "groomer",
    "assistant",
    "assigned_to",
    "operator",
    "created_by",
    "reviewed_by",
    "medical_reviewer",
    "anesthetist",
    "surgeon",
    "cosigner",
    "dispensed_by",
    "administered_by",
    "closed_by",
    "display_name",
    "username",
    "voided_by",
    "uploaded_by",
    "actor",
    "doctor",
    "doctor_name",
    "handled_by",
    "confirmed_by",
}
_SAFE_ATTRS = {
    "title",
    "alt",
    "placeholder",
    "aria-label",
    "content",
    "onsubmit",
    "onclick",
    "onchange",
}

_BLOCK_RE = re.compile(r"(?is)<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>")
_TEXTAREA_RE = re.compile(r"(?is)(<textarea\b[^>]*>)(.*?)(</textarea>)")
_TAG_SPLIT_RE = re.compile(r"(<[^>]+>)")
_TAG_NAME_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z][\w:-]*)")
_QUOTED_ATTR_RE = re.compile(
    r"""([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')"""
)
_ATTR_RE = re.compile(
    r"""([\w:.-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?"""
)

_cache_lock = threading.Lock()
_cache: dict = {
    "sig": None,
    "names": (),
    "name_set": frozenset(),
    "phones": (),
    "masks": {},
    "unmasks": {},
}


def filming_active() -> bool:
    return bool(_filming_ctx.get())


def unmask_store(value: str) -> str:
    """拍摄模式的显示店名改回库里用的店名。真实店名原样返回。"""
    text = (value or "").strip()
    for masked, real in _STORE_UNMASK:
        if text == masked:
            return real
    return value


def _mask_stores(text: str) -> str:
    saved: list[str] = []

    def hold(match: re.Match) -> str:
        saved.append(match.group(0))
        return f"\ue010{len(saved) - 1}\ue011"

    text = _STORE_QUERY.sub(hold, text)
    for pattern, repl in _STORE_DISPLAY:
        text = pattern.sub(repl, text)
    for index, original in enumerate(saved):
        text = text.replace(f"\ue010{index}\ue011", original)
    return text


def _mask_tagline(text: str) -> str:
    for pattern in _TAGLINES:
        text = pattern.sub("", text)
    return text


def restore_masked_field(submitted: str, stored: str | None, *, filming: bool) -> str:
    """拍摄模式下，空值或带 * 的姓名/电话不覆盖库里的原值。"""
    if not filming:
        return submitted
    text = (submitted or "").strip()
    if not text or "*" in text:
        return "" if stored is None else stored
    return submitted


def mask_person_name(name: str) -> str:
    chars = list((name or "").strip())
    if len(chars) <= 1:
        return (chars[0] + "*") if chars else name
    return chars[0] + ("*" * (len(chars) - 1))


def allow_person_token(name: str) -> bool:
    """员工、账号、病历上的医生名能不能拿来做整页替换。"""
    text = (name or "").strip()
    if not _usable_name(text):
        return False
    if text.lower() in _ACCOUNT_SKIP:
        return False
    if any(ch.isspace() for ch in text):
        return False
    if any(ch in text for ch in "，。；;、,/\\@<>()（）"):
        return False
    return True


def build_unique_masks(names: tuple[str, ...] | list[str]) -> dict[str, str]:
    """真名 → 打码。同姓同长度的人不会打成同一个码，方便提交时还原成正确的人。"""
    real_to_mask: dict[str, str] = {}
    used: dict[str, str] = {}
    for name in sorted(names, key=lambda item: (-len(item), item)):
        base = mask_person_name(name)
        mask = base
        serial = 2
        while mask in used and used[mask] != name:
            mask = f"{base}{serial}"
            serial += 1
        used[mask] = name
        real_to_mask[name] = mask
    return real_to_mask


def _masked_name(name: str, masks: dict[str, str] | None) -> str:
    if masks and name in masks:
        return masks[name]
    return mask_person_name(name)


def mask_phone_value(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) >= 11:
        return digits[:3] + "****" + digits[-4:]
    if len(digits) >= 7:
        return digits[:2] + "****" + digits[-2:]
    return raw


def _usable_name(name: str) -> bool:
    text = (name or "").strip()
    if len(text) < 2 or len(text) > 60:
        return False
    if text in _NAME_SKIP:
        return False
    if text.isdigit():
        return False
    if "大风" in text or "Dafo" in text or "dafo" in text:
        return False
    if set(text) <= set("*Xx "):
        return False
    return True


def _add_name(bucket: set[str], name: str | None) -> None:
    if _usable_name(name or ""):
        bucket.add((name or "").strip())


def _add_phone(bucket: set[str], phone: str | None) -> None:
    text = (phone or "").strip()
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 7 and "*" not in text:
        bucket.add(text)


def _split_phones(raw: str | None) -> list[str]:
    return [p for p in re.split(r"[\s,，;；、]+", raw or "") if p.strip()]


def _cached_identity(sig):
    with _cache_lock:
        if _cache["sig"] != sig:
            return None
        return (
            _cache["names"],
            _cache["name_set"],
            _cache["phones"],
            _cache["masks"],
            _cache["unmasks"],
        )


def _store_identity(sig, ordered, name_set, phone_ordered, masks, unmasks) -> None:
    with _cache_lock:
        _cache["sig"] = sig
        _cache["names"] = ordered
        _cache["name_set"] = name_set
        _cache["phones"] = phone_ordered
        _cache["masks"] = masks
        _cache["unmasks"] = unmasks


def _count_sig(db, model, *extra):
    from sqlalchemy import func

    cols = [func.count(model.id), func.max(model.id), *extra]
    return db.query(*cols).one()


def _distinct_texts(db, column) -> list[str]:
    try:
        rows = db.query(column).distinct().all()
    except Exception:
        logger.exception("拍摄模式读取姓名列失败")
        return []
    return [row[0] for row in rows if row and isinstance(row[0], str)]


def load_identity_lists(db):
    """客户和员工姓名、电话。按数据变化缓存，拍摄模式才调用。"""
    from sqlalchemy import func

    from app.models import (
        AdminUser,
        AnesthesiaMonitorSheet,
        Application,
        Appointment,
        AuditLog,
        CbcReport,
        Customer,
        DewormingRecord,
        ExamReport,
        FollowUp,
        GroomingOrder,
        Invoice,
        MicroscopyReport,
        Prescription,
        RabiesVaccineRecord,
        Staff,
        UltrasoundReport,
        Vaccination,
        Visit,
        XrayReport,
    )

    cust_sig = _count_sig(db, Customer, func.max(Customer.updated_at))
    appt_sig = _count_sig(db, Appointment)
    app_sig = _count_sig(db, Application)
    rabies_sig = _count_sig(db, RabiesVaccineRecord)
    staff_sig = _count_sig(db, Staff, func.max(Staff.updated_at))
    admin_sig = tuple(
        sorted((row.username or "", row.display_name or "") for row in db.query(AdminUser.username, AdminUser.display_name))
    )
    visit_sig = _count_sig(db, Visit, func.max(Visit.updated_at))
    presc_sig = _count_sig(db, Prescription, func.max(Prescription.updated_at))
    groom_sig = _count_sig(db, GroomingOrder, func.max(GroomingOrder.updated_at))
    audit_sig = _count_sig(db, AuditLog)
    follow_sig = _count_sig(db, FollowUp)
    invoice_sig = _count_sig(db, Invoice, func.max(Invoice.updated_at))
    sig = (
        cust_sig, appt_sig, app_sig, rabies_sig, staff_sig, admin_sig, visit_sig,
        presc_sig, groom_sig, audit_sig, follow_sig, invoice_sig,
    )
    cached = _cached_identity(sig)
    if cached is not None:
        return cached

    names: set[str] = set()
    phones: set[str] = set()
    for name, phone, extra in db.query(Customer.name, Customer.phone, Customer.phones_extra):
        _add_name(names, name)
        _add_phone(phones, phone)
        for part in _split_phones(extra):
            _add_phone(phones, part)
    for name, phone, proxy_name, proxy_phone in db.query(
        Appointment.customer_name, Appointment.phone, Appointment.proxy_name, Appointment.proxy_phone
    ):
        _add_name(names, name)
        _add_phone(phones, phone)
        _add_name(names, proxy_name)
        _add_phone(phones, proxy_phone)
    for name, phone, proxy_name, proxy_phone in db.query(
        Application.applicant_name, Application.phone, Application.proxy_name, Application.proxy_phone
    ):
        _add_name(names, name)
        _add_phone(phones, phone)
        _add_name(names, proxy_name)
        _add_phone(phones, proxy_phone)
    for name, phone in db.query(RabiesVaccineRecord.owner_name, RabiesVaccineRecord.owner_phone):
        _add_name(names, name)
        _add_phone(phones, phone)

    for column in (
        Staff.name,
        Staff.emergency_contact_name,
        AdminUser.username,
        AdminUser.display_name,
        Visit.vet_name,
        Visit.created_by,
        Visit.closed_by,
        Prescription.vet_name,
        Prescription.created_by,
        Prescription.dispensed_by,
        GroomingOrder.groomer_name,
        GroomingOrder.assistant_name,
        GroomingOrder.created_by,
        AuditLog.actor,
        FollowUp.assigned_to,
        FollowUp.created_by,
        Invoice.created_by,
        DewormingRecord.vet_name,
        DewormingRecord.created_by,
        Vaccination.vet_name,
        Vaccination.created_by,
        RabiesVaccineRecord.staff_name,
        MicroscopyReport.vet_name,
        MicroscopyReport.operator,
        UltrasoundReport.vet_name,
        XrayReport.vet_name,
        CbcReport.vet_name,
        CbcReport.operator,
        AnesthesiaMonitorSheet.anesthetist,
        AnesthesiaMonitorSheet.surgeon,
        ExamReport.uploaded_by,
    ):
        for text in _distinct_texts(db, column):
            if allow_person_token(text):
                names.add(text.strip())
    for phone in _distinct_texts(db, Staff.phone):
        _add_phone(phones, phone)
    for phone in _distinct_texts(db, Staff.emergency_contact_phone):
        _add_phone(phones, phone)

    ordered = tuple(sorted(names, key=len, reverse=True))
    phone_ordered = tuple(sorted(phones, key=len, reverse=True))
    name_set = frozenset(ordered)
    masks = build_unique_masks(ordered)
    unmasks = {mask: real for real, mask in masks.items()}
    _store_identity(sig, ordered, name_set, phone_ordered, masks, unmasks)
    return ordered, name_set, phone_ordered, masks, unmasks


def mask_display_text(
    text: str,
    names: tuple[str, ...] | list[str],
    phones: tuple[str, ...] | list[str],
    *,
    domains: bool = True,
    masks: dict[str, str] | None = None,
) -> str:
    if not text:
        return text
    for src, dst in _BRAND_PAIRS:
        if src in text:
            text = text.replace(src, dst)
    text = _STANDALONE_BRAND.sub("XX", text)
    if domains and "dafopet" in text.lower():
        text = _DAFOPET.sub("xxpet", text)
    text = _mask_stores(text)
    text = _mask_tagline(text)
    for phone in phones:
        if phone and phone in text:
            text = text.replace(phone, mask_phone_value(phone))
    text = _MOBILE.sub(lambda m: f"{m.group(1)}****{m.group(3)}", text)
    for name in names:
        if not name or name not in text:
            continue
        masked = _masked_name(name, masks)
        if masked == name:
            continue
        if re.search(r"[\u4e00-\u9fff]", name):
            text = text.replace(name, masked)
        else:
            text = re.sub(
                r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])",
                lambda _m, repl=masked: repl,
                text,
            )
    return text


def _is_sensitive(value: str, names: frozenset[str], phones: tuple[str, ...]) -> bool:
    text = (value or "").strip()
    if not text or text in _NAME_SKIP:
        return False
    if text in names:
        return True
    if _MOBILE.search(text):
        return True
    for phone in phones:
        if phone and phone in text:
            return True
    return False


def _parse_attrs(tag: str) -> dict[str, str | None]:
    inner = tag.strip()[1:-1]
    if inner.endswith("/"):
        inner = inner[:-1]
    parts = inner.split(None, 1)
    rest = parts[1] if len(parts) > 1 else ""
    out: dict[str, str | None] = {}
    for match in _ATTR_RE.finditer(rest):
        key = match.group(1).lower()
        if match.group(2) is not None:
            out[key] = html.unescape(match.group(2))
        elif match.group(3) is not None:
            out[key] = html.unescape(match.group(3))
        elif match.group(4) is not None:
            out[key] = html.unescape(match.group(4))
        else:
            out[key] = None
    return out


def _insert_before_end(tag: str, snippet: str) -> str:
    if tag.endswith("/>"):
        return tag[:-2].rstrip() + snippet + " />"
    if tag.endswith(">"):
        return tag[:-1] + snippet + ">"
    return tag


def _upsert_attr(tag: str, name: str, value: str | None) -> str:
    pattern = re.compile(
        rf"""(\s{re.escape(name)}\s*=\s*)(?:"[^"]*"|'[^']*'|[^\s"'=<>`]+)""",
        re.IGNORECASE,
    )
    if value is None:
        if re.search(rf"\s{re.escape(name)}(?=\s|/?>)", tag, re.IGNORECASE):
            return tag
        return _insert_before_end(tag, " " + name)
    escaped = html.escape(value, quote=True)
    replacement = f' {name}="{escaped}"'
    if pattern.search(tag):
        return pattern.sub(replacement, tag, count=1)
    return _insert_before_end(tag, replacement)


_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}


def _track_tag(stack: list[str], tag: str) -> None:
    match = _TAG_NAME_RE.match(tag)
    if not match:
        return
    closing, name = bool(match.group(1)), match.group(2).lower()
    if closing:
        for index in range(len(stack) - 1, -1, -1):
            if stack[index] == name:
                del stack[index:]
                return
        return
    if name in _VOID_TAGS or tag.rstrip().endswith("/>"):
        return
    stack.append(name)


def _mask_safe_attrs(tag: str, names, phones, masks=None) -> str:
    def repl(match: re.Match) -> str:
        attr = match.group(1)
        if attr.lower() not in _SAFE_ATTRS:
            return match.group(0)
        raw = match.group(2) if match.group(2) is not None else match.group(3)
        if raw is None:
            return match.group(0)
        masked = mask_display_text(html.unescape(raw), names, phones, masks=masks)
        return f'{attr}="{html.escape(masked, quote=True)}"'

    return _QUOTED_ATTR_RE.sub(repl, tag)


def _rewrite_input(tag: str, names, name_set: frozenset[str], phones, masks=None) -> str:
    attrs = _parse_attrs(tag)
    field_type = (attrs.get("type") or "text")
    if field_type is None:
        field_type = "text"
    field_type = str(field_type).lower()
    field_name = str(attrs.get("name") or "")
    value = attrs.get("value") or ""
    if not isinstance(value, str):
        value = ""
    stripped = value.strip()
    if field_type in _SKIP_INPUT_TYPES or field_name not in _IDENTITY_FIELDS:
        # 医生/员工姓名框：画面打码，字段名保留，提交后由服务端还原成真名。
        # 没有 name 的只读展示框直接改值，它不会被提交。
        if field_type not in _SKIP_INPUT_TYPES and stripped in name_set and (
            field_name in _STAFF_VALUE_FIELDS or not field_name
        ):
            masked = _masked_name(stripped, masks)
            if masked != value:
                return _upsert_attr(tag, "value", masked)
        return _mask_safe_attrs(tag, names, phones, masks)
    if not _is_sensitive(value, name_set, phones):
        return _mask_safe_attrs(tag, names, phones, masks)
    masked = mask_display_text(value, names, phones, domains=False, masks=masks)
    tag = _upsert_attr(tag, "value", masked)
    tag = _upsert_attr(tag, "disabled", None)
    tag = _upsert_attr(tag, "readonly", None)
    # 不把原值写回页面。禁用后浏览器不提交，保存接口见到空值或星号会沿用库里的原值。
    return tag


def _rewrite_textarea(match: re.Match, names, name_set, phones, masks=None, keep_bodies=None) -> str:
    open_tag, body, close = match.group(1), match.group(2), match.group(3)
    attrs = _parse_attrs(open_tag)
    field_name = str(attrs.get("name") or "")
    plain = html.unescape(body)
    if field_name not in _IDENTITY_FIELDS or not _is_sensitive(plain, name_set, phones):
        # 备注、病历正文会随表单提交。这里先藏起来，避免后面的全文替换把医生姓名打码后写进数据库。
        token = f"\ue002TA{len(keep_bodies or [])}\ue003"
        if keep_bodies is not None:
            keep_bodies.append(body)
        return _mask_safe_attrs(open_tag, names, phones, masks) + token + close
    masked = mask_display_text(plain, names, phones, domains=False, masks=masks)
    open_tag = _upsert_attr(open_tag, "disabled", None)
    open_tag = _upsert_attr(open_tag, "readonly", None)
    return open_tag + html.escape(masked) + close


def _rewrite_option(tag: str, names, name_set, phones, masks, parent: str) -> str:
    """下拉的显示文字由文本节点打码。datalist 的候选项没有文本，浏览器直接显示 value，所以 value 也要打码。"""
    if parent != "datalist":
        return _mask_safe_attrs(tag, names, phones, masks)
    attrs = _parse_attrs(tag)
    value = attrs.get("value") or ""
    if not isinstance(value, str):
        return _mask_safe_attrs(tag, names, phones, masks)
    stripped = value.strip()
    if stripped not in name_set:
        return _mask_safe_attrs(tag, names, phones, masks)
    masked = _masked_name(stripped, masks)
    # 只改 value。浏览器的候选列表显示的就是 value，提交后再按对照表还原。
    return _upsert_attr(tag, "value", masked)


def _rewrite_tag(tag: str, names, name_set, phones, masks=None, parent: str = "") -> str:
    match = _TAG_NAME_RE.match(tag)
    if not match or match.group(1):
        return tag
    name = match.group(2).lower()
    if name == "input":
        return _rewrite_input(tag, names, name_set, phones, masks)
    if name == "option":
        return _rewrite_option(tag, names, name_set, phones, masks, parent)
    return _mask_safe_attrs(tag, names, phones, masks)


def mask_html(
    page: str,
    names,
    phones,
    name_set: frozenset[str] | None = None,
    masks: dict[str, str] | None = None,
) -> str:
    if name_set is None:
        name_set = frozenset(names)
    blocks: list[str] = []

    def hold(match: re.Match) -> str:
        blocks.append(match.group(0))
        return f"\ue000FILM{len(blocks) - 1}\ue001"

    protected = _BLOCK_RE.sub(hold, page)
    kept_textareas: list[str] = []

    def rewrite_area(match: re.Match) -> str:
        return _rewrite_textarea(match, names, name_set, phones, masks, kept_textareas)

    protected = _TEXTAREA_RE.sub(rewrite_area, protected)
    bits = _TAG_SPLIT_RE.split(protected)
    out: list[str] = []
    stack: list[str] = []
    for bit in bits:
        if bit.startswith("<") and bit.endswith(">"):
            parent = stack[-1] if stack else ""
            out.append(_rewrite_tag(bit, names, name_set, phones, masks, parent))
            _track_tag(stack, bit)
        else:
            out.append(mask_display_text(bit, names, phones, masks=masks))
    merged = "".join(out)
    for index, block in enumerate(blocks):
        merged = merged.replace(f"\ue000FILM{index}\ue001", block)
    for index, body in enumerate(kept_textareas):
        merged = merged.replace(f"\ue002TA{index}\ue003", body)
    return merged


def mask_json_data(data, names, phones, masks=None):
    if isinstance(data, dict):
        return {key: mask_json_data(value, names, phones, masks) for key, value in data.items()}
    if isinstance(data, list):
        return [mask_json_data(item, names, phones, masks) for item in data]
    if isinstance(data, str):
        # JSON 里常有跳转 URL，不改 dafopet 域名，避免拍的时候点不开。
        return mask_display_text(data, names, phones, domains=False, masks=masks)
    return data


def mask_json_text(raw: str, names, phones, masks=None) -> str:
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    return json.dumps(mask_json_data(data, names, phones, masks), ensure_ascii=False)


def _content_type(headers: list[tuple[bytes, bytes]]) -> str:
    for key, value in headers:
        if key.lower() == b"content-type":
            return value.decode("latin1", "replace").lower()
    return ""


def unmask_request_body(raw: bytes, content_type: str, unmasks: dict[str, str]) -> bytes:
    """把员工姓名字段里的打码还原成真名。客户姓名电话不在这里还原。"""
    if not raw or not unmasks:
        return raw
    low = (content_type or "").lower()
    if "application/json" in low:
        return _unmask_json_body(raw, unmasks)
    if "multipart/form-data" in low:
        return _unmask_multipart_body(raw, content_type, unmasks)
    if "application/x-www-form-urlencoded" in low:
        return _unmask_urlencoded_body(raw, unmasks)
    return raw


def _restore_staff_value(key: str, value: str, unmasks: dict[str, str]) -> str | None:
    if key not in _STAFF_VALUE_FIELDS:
        return None
    restored = unmasks.get((value or "").strip())
    if restored is None or restored == value:
        return None
    return restored


def _unmask_urlencoded_body(raw: bytes, unmasks: dict[str, str]) -> bytes:
    from urllib.parse import parse_qsl, urlencode

    pairs = parse_qsl(raw.decode("utf-8"), keep_blank_values=True)
    changed = False
    rewritten = []
    for key, value in pairs:
        restored = _restore_staff_value(key, value, unmasks)
        if restored is None:
            rewritten.append((key, value))
        else:
            rewritten.append((key, restored))
            changed = True
    if not changed:
        return raw
    return urlencode(rewritten, doseq=True).encode("utf-8")


def _unmask_json_body(raw: bytes, unmasks: dict[str, str]) -> bytes:
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception:
        return raw
    changed = False

    def walk(node):
        nonlocal changed
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                if isinstance(value, str):
                    restored = _restore_staff_value(str(key), value, unmasks)
                    if restored is not None:
                        out[key] = restored
                        changed = True
                        continue
                if isinstance(value, (dict, list)):
                    out[key] = walk(value)
                else:
                    out[key] = value
            return out
        if isinstance(node, list):
            return [walk(item) if isinstance(item, (dict, list)) else item for item in node]
        return node

    rewritten = walk(data)
    if not changed:
        return raw
    return json.dumps(rewritten, ensure_ascii=False).encode("utf-8")


def _multipart_boundary(content_type: str) -> bytes | None:
    match = re.search(r"""boundary=(?:"([^"]+)"|([^;]+))""", content_type, re.IGNORECASE)
    if not match:
        return None
    token = (match.group(1) or match.group(2) or "").strip()
    if not token:
        return None
    try:
        return token.encode("ascii")
    except UnicodeEncodeError:
        return None


def _unmask_multipart_body(raw: bytes, content_type: str, unmasks: dict[str, str]) -> bytes:
    boundary = _multipart_boundary(content_type)
    if not boundary:
        return raw
    delimiter = b"--" + boundary
    pieces = raw.split(delimiter)
    if len(pieces) < 2:
        return raw
    changed = False
    rebuilt = [pieces[0]]
    for piece in pieces[1:]:
        if piece.startswith(b"--") or b"\r\n\r\n" not in piece:
            rebuilt.append(piece)
            continue
        header_blob, rest = piece.split(b"\r\n\r\n", 1)
        header_text = header_blob.decode("utf-8", "replace")
        if "filename=" in header_text.lower():
            rebuilt.append(piece)
            continue
        name_match = re.search(r'name="([^"]*)"', header_text)
        if not name_match:
            rebuilt.append(piece)
            continue
        if rest.endswith(b"\r\n"):
            body, tail = rest[:-2], b"\r\n"
        else:
            body, tail = rest, b""
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            rebuilt.append(piece)
            continue
        restored = _restore_staff_value(name_match.group(1), text, unmasks)
        if restored is None:
            rebuilt.append(piece)
            continue
        rebuilt.append(header_blob + b"\r\n\r\n" + restored.encode("utf-8") + tail)
        changed = True
    if not changed:
        return raw
    return delimiter.join(rebuilt)


def _set_content_length(scope, length: int) -> None:
    headers = [
        (key, value)
        for key, value in scope.get("headers") or []
        if key.lower() != b"content-length"
    ]
    headers.append((b"content-length", str(length).encode("ascii")))
    scope["headers"] = headers


def _should_mask(path: str, content_type: str) -> str | None:
    """返回 html / json，不需要处理时返回 None。"""
    if path.startswith("/uploads/"):
        return None
    if path.startswith("/static/") and not path.endswith("manifest.json"):
        return None
    if path.endswith("manifest.json"):
        return "json"
    if "text/html" in content_type or content_type.startswith("application/xhtml"):
        return "html"
    if "json" in content_type:
        return "json"
    return None


class FilmingModeMiddleware:
    """必须加在 SessionMiddleware 里面（先 add 本类，再 add Session），这样能读到 session。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        session = scope.get("session") or {}
        if not session.get("filming_mode"):
            await self.app(scope, receive, send)
            return

        token = _filming_ctx.set(True)
        path = scope.get("path") or ""
        try:
            if path.startswith("/uploads/") or (
                path.startswith("/static/") and not path.endswith("manifest.json")
            ):
                await self.app(scope, receive, send)
                return
            receive = await self._unmask_receive(scope, receive)
            await self._mask_response(scope, path, receive, send)
        finally:
            _filming_ctx.reset(token)

    async def _unmask_receive(self, scope, receive):
        method = (scope.get("method") or "GET").upper()
        if method not in {"POST", "PUT", "PATCH"}:
            return receive
        ctype = ""
        for key, value in scope.get("headers") or []:
            if key.lower() == b"content-type":
                ctype = value.decode("latin1", "replace")
                break
        low = ctype.lower()
        if not any(token in low for token in (
            "application/x-www-form-urlencoded",
            "application/json",
            "multipart/form-data",
        )):
            return receive
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            _names, _name_set, _phones, _masks, unmasks = load_identity_lists(db)
        except Exception:
            logger.exception("拍摄模式读取姓名对照失败，提交值保持原样")
            return receive
        finally:
            db.close()
        if not unmasks:
            return receive

        body = bytearray()
        extra = []
        while True:
            message = await receive()
            if message["type"] != "http.request":
                extra.append(message)
                break
            body.extend(message.get("body") or b"")
            if not message.get("more_body"):
                break
        original = bytes(body)
        try:
            rewritten = unmask_request_body(original, ctype, unmasks)
        except Exception:
            logger.exception("拍摄模式还原提交值失败，保持原请求")
            rewritten = original
        if rewritten != original:
            _set_content_length(scope, len(rewritten))
        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": rewritten, "more_body": False}
            if extra:
                return extra.pop(0)
            return await receive()

        return replay

    async def _mask_response(self, scope, path: str, receive, send) -> None:
        start: dict | None = None
        body = bytearray()
        passthrough = False
        kind: str | None = None

        async def send_wrapper(message):
            nonlocal start, passthrough, kind
            if passthrough:
                await send(message)
                return
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                if any(key.lower() == b"content-encoding" for key, _ in headers):
                    passthrough = True
                    await send(message)
                    return
                kind = _should_mask(path, _content_type(headers))
                if kind is None:
                    passthrough = True
                    await send(message)
                    return
                start = message
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            body.extend(message.get("body") or b"")
            if message.get("more_body"):
                if len(body) > 8_000_000:
                    passthrough = True
                    if start is not None:
                        await send(start)
                    await send({"type": "http.response.body", "body": bytes(body), "more_body": True})
                return
            raw = bytes(body)
            transformed = raw
            try:
                if kind and len(raw) <= 8_000_000:
                    transformed = self._transform(raw, kind)
            except Exception:
                logger.exception("拍摄模式过滤失败，本次响应保持原样")
                transformed = raw
            headers = [
                (key, value)
                for key, value in (start or {}).get("headers") or []
                if key.lower() != b"content-length"
            ]
            headers.append((b"content-length", str(len(transformed)).encode("ascii")))
            start_msg = dict(start or {"type": "http.response.start", "status": 200})
            start_msg["headers"] = headers
            await send(start_msg)
            await send({"type": "http.response.body", "body": transformed})

        await self.app(scope, receive, send_wrapper)

    def _transform(self, raw: bytes, kind: str) -> bytes:
        text = raw.decode("utf-8", "replace")
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            names, name_set, phones, masks, _unmasks = load_identity_lists(db)
        finally:
            db.close()
        if kind == "html":
            return mask_html(text, names, phones, name_set, masks).encode("utf-8")
        return mask_json_text(text, names, phones, masks).encode("utf-8")
