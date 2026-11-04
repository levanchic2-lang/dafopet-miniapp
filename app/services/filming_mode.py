"""拍摄模式：只改当前登录会话看到的页面，不写数据库。

开启后，这一次浏览器会话的 HTML / JSON 响应会：
- 把医院品牌名换成「XX动物医院」
- 把客户（主人）姓名、手机号打码

表单里看得见的姓名/电话输入框改成打码且不可编辑，避免星号被存回去。
隐藏域里的真实值保持不动，这样预约等依赖提交的流程仍用真实数据。
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
_cache: dict = {"sig": None, "names": (), "name_set": frozenset(), "phones": ()}


def filming_active() -> bool:
    return bool(_filming_ctx.get())


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


def load_identity_lists(db) -> tuple[tuple[str, ...], frozenset[str], tuple[str, ...]]:
    """客户/预约/申请人姓名和电话。按人数变化缓存，拍摄模式才调用。"""
    from sqlalchemy import func

    from app.models import Application, Appointment, Customer, RabiesVaccineRecord

    cust_sig = db.query(
        func.count(Customer.id),
        func.max(Customer.id),
        func.max(Customer.updated_at),
    ).one()
    appt_sig = db.query(func.count(Appointment.id), func.max(Appointment.id)).one()
    app_sig = db.query(func.count(Application.id), func.max(Application.id)).one()
    rabies_sig = db.query(func.count(RabiesVaccineRecord.id), func.max(RabiesVaccineRecord.id)).one()
    sig = (cust_sig, appt_sig, app_sig, rabies_sig)
    with _cache_lock:
        if _cache["sig"] == sig and _cache["names"]:
            return _cache["names"], _cache["name_set"], _cache["phones"]
        if _cache["sig"] == sig:
            return _cache["names"], _cache["name_set"], _cache["phones"]

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

    ordered = tuple(sorted(names, key=len, reverse=True))
    phone_ordered = tuple(sorted(phones, key=len, reverse=True))
    name_set = frozenset(ordered)
    with _cache_lock:
        _cache["sig"] = sig
        _cache["names"] = ordered
        _cache["name_set"] = name_set
        _cache["phones"] = phone_ordered
    return ordered, name_set, phone_ordered


def mask_display_text(
    text: str,
    names: tuple[str, ...] | list[str],
    phones: tuple[str, ...] | list[str],
    *,
    domains: bool = True,
) -> str:
    if not text:
        return text
    for src, dst in _BRAND_PAIRS:
        if src in text:
            text = text.replace(src, dst)
    text = _STANDALONE_BRAND.sub("XX", text)
    if domains and "dafopet" in text.lower():
        text = _DAFOPET.sub("xxpet", text)
    for phone in phones:
        if phone and phone in text:
            text = text.replace(phone, mask_phone_value(phone))
    text = _MOBILE.sub(lambda m: f"{m.group(1)}****{m.group(3)}", text)
    for name in names:
        if not name or name not in text:
            continue
        masked = mask_person_name(name)
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


def _mask_safe_attrs(tag: str, names, phones) -> str:
    def repl(match: re.Match) -> str:
        attr = match.group(1)
        if attr.lower() not in _SAFE_ATTRS:
            return match.group(0)
        raw = match.group(2) if match.group(2) is not None else match.group(3)
        if raw is None:
            return match.group(0)
        masked = mask_display_text(html.unescape(raw), names, phones)
        return f'{attr}="{html.escape(masked, quote=True)}"'

    return _QUOTED_ATTR_RE.sub(repl, tag)


def _rewrite_input(tag: str, names, name_set: frozenset[str], phones) -> str:
    attrs = _parse_attrs(tag)
    field_type = (attrs.get("type") or "text")
    if field_type is None:
        field_type = "text"
    field_type = str(field_type).lower()
    field_name = str(attrs.get("name") or "")
    if field_type in _SKIP_INPUT_TYPES or field_name not in _IDENTITY_FIELDS:
        return _mask_safe_attrs(tag, names, phones)
    value = attrs.get("value") or ""
    if not isinstance(value, str) or not _is_sensitive(value, name_set, phones):
        return _mask_safe_attrs(tag, names, phones)
    masked = mask_display_text(value, names, phones, domains=False)
    tag = _upsert_attr(tag, "value", masked)
    tag = _upsert_attr(tag, "disabled", None)
    tag = _upsert_attr(tag, "readonly", None)
    return tag


def _rewrite_textarea(match: re.Match, names, name_set, phones) -> str:
    open_tag, body, close = match.group(1), match.group(2), match.group(3)
    attrs = _parse_attrs(open_tag)
    field_name = str(attrs.get("name") or "")
    plain = html.unescape(body)
    if field_name not in _IDENTITY_FIELDS or not _is_sensitive(plain, name_set, phones):
        return _mask_safe_attrs(open_tag, names, phones) + body + close
    masked = mask_display_text(plain, names, phones, domains=False)
    open_tag = _upsert_attr(open_tag, "disabled", None)
    open_tag = _upsert_attr(open_tag, "readonly", None)
    return open_tag + html.escape(masked) + close


def _rewrite_tag(tag: str, names, name_set, phones) -> str:
    match = _TAG_NAME_RE.match(tag)
    if not match or match.group(1):
        return tag
    if match.group(2).lower() == "input":
        return _rewrite_input(tag, names, name_set, phones)
    return _mask_safe_attrs(tag, names, phones)


def mask_html(page: str, names, phones, name_set: frozenset[str] | None = None) -> str:
    if name_set is None:
        name_set = frozenset(names)
    blocks: list[str] = []

    def hold(match: re.Match) -> str:
        blocks.append(match.group(0))
        return f"\ue000FILM{len(blocks) - 1}\ue001"

    protected = _BLOCK_RE.sub(hold, page)

    def rewrite_area(match: re.Match) -> str:
        return _rewrite_textarea(match, names, name_set, phones)

    protected = _TEXTAREA_RE.sub(rewrite_area, protected)
    bits = _TAG_SPLIT_RE.split(protected)
    out: list[str] = []
    for bit in bits:
        if bit.startswith("<") and bit.endswith(">"):
            out.append(_rewrite_tag(bit, names, name_set, phones))
        else:
            out.append(mask_display_text(bit, names, phones))
    merged = "".join(out)
    for index, block in enumerate(blocks):
        merged = merged.replace(f"\ue000FILM{index}\ue001", block)
    return merged


def mask_json_data(data, names, phones):
    if isinstance(data, dict):
        return {key: mask_json_data(value, names, phones) for key, value in data.items()}
    if isinstance(data, list):
        return [mask_json_data(item, names, phones) for item in data]
    if isinstance(data, str):
        # JSON 里常有跳转 URL，不改 dafopet 域名，避免拍的时候点不开。
        return mask_display_text(data, names, phones, domains=False)
    return data


def mask_json_text(raw: str, names, phones) -> str:
    try:
        data = json.loads(raw)
    except Exception:
        return raw
    return json.dumps(mask_json_data(data, names, phones), ensure_ascii=False)


def _content_type(headers: list[tuple[bytes, bytes]]) -> str:
    for key, value in headers:
        if key.lower() == b"content-type":
            return value.decode("latin1", "replace").lower()
    return ""


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
            await self._mask_response(scope, path, receive, send)
        finally:
            _filming_ctx.reset(token)

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
            names, name_set, phones = load_identity_lists(db)
        finally:
            db.close()
        if kind == "html":
            return mask_html(text, names, phones, name_set).encode("utf-8")
        return mask_json_text(text, names, phones).encode("utf-8")
