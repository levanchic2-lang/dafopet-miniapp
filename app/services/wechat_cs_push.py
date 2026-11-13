"""小程序客服消息推送：验签、解密、立刻应答，再异步回复。

AI_CS_ENABLED 默认关闭。关闭或 openid 不在白名单时，把消息转给微信客服工具，
这样人工接待不会因为配置了推送地址而收不到消息。人工会话状态也不介入。
客户明确要人工，或人工会话还没结束时，必须在这次 HTTP 响应里返回
transfer_customer_service。小程序没有事后再转接的发送接口。
AI 没把握、出错或工具轮次用尽时，模型是在已经应答之后才跑完的，
这一条只能回电话；同时打开人工会话，客户接下来的消息再转接。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AiCsSeenMsg
from app.services.ai_cs_assistant import (
    PHONE_FALLBACK,
    active_handoff_session,
    begin_human_handoff,
    beijing_naive,
    clip_wechat,
    sanitize_wechat_text,
    log_handoff_customer_message,
    media_reply,
    maybe_greet,
    record_handoff,
    remember_gated,
    touch_handoff_session,
    wants_human,
)
from app.services.wechat_cs_crypto import (
    WeChatCsCryptoError,
    decrypt_text,
    parse_message_text,
    plain_signature,
    secure_signature,
    signatures_match,
)
from app.time_utils import beijing_now

logger = logging.getLogger(__name__)


@dataclass
class PushResult:
    status: int
    body: str
    media_type: str
    job: dict | None = None


def _env(name: str, setting_name: str) -> str:
    if name in os.environ:
        return (os.environ.get(name) or "").strip()
    return (getattr(settings, setting_name, "") or "").strip()


def cs_token() -> str:
    return _env("WECHAT_CS_TOKEN", "wechat_cs_token")


def cs_aes_key() -> str:
    return _env("WECHAT_CS_AES_KEY", "wechat_cs_aes_key")


def mini_appid() -> str:
    return _env("WECHAT_APPID", "wechat_appid")


def ai_cs_enabled() -> bool:
    if "AI_CS_ENABLED" in os.environ:
        raw = os.environ.get("AI_CS_ENABLED") or ""
    else:
        raw = "1" if getattr(settings, "ai_cs_enabled", False) else "0"
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def test_openids() -> set[str]:
    if "AI_CS_TEST_OPENIDS" in os.environ:
        raw = os.environ.get("AI_CS_TEST_OPENIDS") or ""
    else:
        raw = getattr(settings, "ai_cs_test_openids", "") or ""
    return {item.strip() for item in str(raw).split(",") if item.strip()}


def openid_allowed(openid: str) -> bool:
    allowed = test_openids()
    if not allowed:
        return True
    return (openid or "").strip() in allowed


def _query(query: dict, *names: str) -> str:
    for name in names:
        value = query.get(name)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _looks_json(raw: bytes, content_type: str) -> bool:
    if "json" in (content_type or "").lower():
        return True
    stripped = raw.lstrip()
    return stripped.startswith(b"{") or stripped.startswith(b"[")


def _normalize(data: dict) -> dict:
    def text(key: str) -> str:
        value = data.get(key)
        if value is None:
            return ""
        return str(value).strip()

    return {
        "to_user": text("ToUserName"),
        "from_user": text("FromUserName"),
        "create_time": text("CreateTime"),
        "msg_type": text("MsgType").lower(),
        "event": text("Event").lower(),
        "content": text("Content"),
        "msg_id": text("MsgId") or text("MsgID"),
        "title": text("Title"),
    }


def _open_payload(raw: bytes, content_type: str, query: dict) -> tuple[dict, str]:
    text = raw.decode("utf-8", errors="replace").strip()
    fmt = "json" if _looks_json(raw, content_type) else "xml"
    if not text:
        raise WeChatCsCryptoError("消息为空")
    outer = parse_message_text(text)
    encrypt = str(outer.get("Encrypt") or "").strip()
    encrypt_type = _query(query, "encrypt_type").lower()
    token = cs_token()
    timestamp = _query(query, "timestamp")
    nonce = _query(query, "nonce")
    if encrypt or encrypt_type == "aes":
        if not encrypt:
            raise WeChatCsCryptoError("安全模式缺少 Encrypt")
        provided = _query(query, "msg_signature", "signature")
        expected = secure_signature(token, timestamp, nonce, encrypt)
        if not signatures_match(expected, provided):
            raise WeChatCsCryptoError("安全模式签名不正确")
        plain = decrypt_text(encrypt, cs_aes_key(), mini_appid())
        inner = parse_message_text(plain)
        inner_fmt = "json" if plain.lstrip().startswith("{") else "xml"
        return _normalize(inner), inner_fmt
    provided = _query(query, "signature")
    expected = plain_signature(token, timestamp, nonce)
    if not signatures_match(expected, provided):
        raise WeChatCsCryptoError("签名不正确")
    return _normalize(outer), fmt


def _transfer_body(message: dict, fmt: str) -> tuple[str, str]:
    to_user = message["from_user"].replace("]]>", "")
    from_user = message["to_user"].replace("]]>", "")
    created = int(beijing_now().timestamp())
    if fmt == "json":
        body = json.dumps({
            "ToUserName": to_user,
            "FromUserName": from_user,
            "CreateTime": created,
            "MsgType": "transfer_customer_service",
        }, ensure_ascii=False)
        return body, "application/json"
    body = (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        f"<CreateTime>{created}</CreateTime>"
        "<MsgType><![CDATA[transfer_customer_service]]></MsgType>"
        "</xml>"
    )
    return body, "application/xml"


def _success() -> PushResult:
    return PushResult(200, "success", "text/plain", None)


def _claim(db: Session, key: str, openid: str) -> bool:
    try:
        db.add(AiCsSeenMsg(msg_key=key[:128], openid=(openid or "")[:64], created_at=beijing_naive()))
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def _dedupe_key(message: dict) -> str:
    if message["msg_id"]:
        return "msg:" + message["msg_id"]
    if message["msg_type"] == "event":
        return f"evt:{message['from_user']}:{message['create_time']}:{message['event']}"
    return f"misc:{message['from_user']}:{message['create_time']}:{message['msg_type']}"


def _job_for(message: dict) -> dict | None:
    kind = ""
    if message["msg_type"] == "text":
        kind = "text"
    elif message["msg_type"] == "event" and message["event"] == "user_enter_tempsession":
        kind = "greeting"
    elif message["msg_type"] in {"image", "miniprogrampage"}:
        kind = "media"
    if not kind:
        return None
    return {
        "openid": message["from_user"][:64],
        "kind": kind,
        "text": message["content"],
        "msg_id": message["msg_id"],
    }


def handle_get(query: dict) -> PushResult:
    token = cs_token()
    if not token:
        return PushResult(503, "wechat cs token not configured", "text/plain")
    timestamp = _query(query, "timestamp")
    nonce = _query(query, "nonce")
    echostr = _query(query, "echostr")
    if not echostr:
        return PushResult(403, "forbidden", "text/plain")
    encrypt_type = _query(query, "encrypt_type").lower()
    msg_signature = _query(query, "msg_signature")
    if encrypt_type == "aes" or msg_signature:
        expected = secure_signature(token, timestamp, nonce, echostr)
        provided = msg_signature or _query(query, "signature")
        if not signatures_match(expected, provided):
            return PushResult(403, "forbidden", "text/plain")
        try:
            plain = decrypt_text(echostr, cs_aes_key(), mini_appid())
        except WeChatCsCryptoError:
            logger.info("[ai-cs] url verify decrypt failed")
            return PushResult(403, "forbidden", "text/plain")
        return PushResult(200, plain, "text/plain")
    expected = plain_signature(token, timestamp, nonce)
    if not signatures_match(expected, _query(query, "signature")):
        return PushResult(403, "forbidden", "text/plain")
    return PushResult(200, echostr, "text/plain")


def handle_push(db: Session, *, method: str, query: dict, raw: bytes, content_type: str) -> PushResult:
    if method.upper() == "GET":
        return handle_get(query)
    if not cs_token():
        return PushResult(503, "wechat cs token not configured", "text/plain")
    if len(raw or b"") > 1_000_000:
        return PushResult(413, "too large", "text/plain")
    try:
        message, fmt = _open_payload(raw or b"", content_type, query)
    except WeChatCsCryptoError as exc:
        logger.info("[ai-cs] push rejected: %s", exc)
        text = str(exc)
        status = 403 if "签名" in text or "AppId" in text else 400
        return PushResult(status, "forbidden" if status == 403 else "bad message", "text/plain")
    openid = message["from_user"]
    if not openid:
        return _success()

    human = message["msg_type"] == "text" and wants_human(message["content"])
    enabled = ai_cs_enabled()
    allowed = openid_allowed(openid)
    if enabled and allowed:
        session = active_handoff_session(db, openid)
        if session is not None:
            if _claim(db, _dedupe_key(message), openid):
                try:
                    touch_handoff_session(db, session)
                    log_handoff_customer_message(db, openid, message)
                except Exception:
                    logger.exception("[ai-cs] handoff follow-up log failed")
            body, media = _transfer_body(message, fmt)
            return PushResult(200, body, media, None)
        if human:
            if _claim(db, _dedupe_key(message), openid):
                try:
                    begin_human_handoff(
                        db, openid, "customer_requested", message["content"][:80],
                        transfer_this_message=True,
                    )
                    log_handoff_customer_message(db, openid, message)
                except Exception:
                    logger.exception("[ai-cs] start handoff failed")
            body, media = _transfer_body(message, fmt)
            return PushResult(200, body, media, None)
    elif human:
        if _claim(db, _dedupe_key(message), openid):
            record_handoff(db, openid, "customer_requested", "wechat_transfer", message["content"][:80])
        body, media = _transfer_body(message, fmt)
        return PushResult(200, body, media, None)
    if not enabled:
        body, media = _transfer_body(message, fmt)
        return PushResult(200, body, media, None)
    if not allowed:
        if _claim(db, _dedupe_key(message), openid):
            remember_gated(db, openid)
        body, media = _transfer_body(message, fmt)
        return PushResult(200, body, media, None)

    if not _claim(db, _dedupe_key(message), openid):
        return _success()
    job = _job_for(message)
    if job is None:
        return _success()
    return PushResult(200, "success", "text/plain", job)


async def handle_http(request, background_tasks, db: Session):
    from starlette.responses import Response

    raw = await request.body()
    result = handle_push(
        db,
        method=request.method,
        query=dict(request.query_params),
        raw=raw,
        content_type=request.headers.get("content-type") or "",
    )
    if result.job:
        background_tasks.add_task(run_cs_job, result.job)
    return Response(content=result.body, status_code=result.status, media_type=result.media_type)


async def run_cs_job(job: dict) -> None:
    from app.database import SessionLocal
    from app.services.ai_cs_assistant import handle_user_text
    from app.services.wechat_miniapp import send_customer_service_text

    openid = (job or {}).get("openid") or ""
    reply = ""
    db = SessionLocal()
    try:
        kind = (job or {}).get("kind") or ""
        if kind == "greeting":
            reply = maybe_greet(db, openid)
        elif kind == "media":
            reply = media_reply(db, openid)
        elif kind == "text":
            reply = await handle_user_text(db, openid, (job or {}).get("text") or "")
    except Exception:
        logger.exception("[ai-cs] job failed")
        reply = PHONE_FALLBACK
        try:
            reply = begin_human_handoff(
                db, openid, "job_error", "处理失败", transfer_this_message=False,
            ) or PHONE_FALLBACK
        except Exception:
            logger.exception("[ai-cs] handoff log failed")
    finally:
        db.close()
    reply = sanitize_wechat_text(reply)
    if not reply or not openid:
        return
    try:
        send_customer_service_text(openid, clip_wechat(reply))
    except Exception:
        logger.exception("[ai-cs] send failed openid=%s", openid[:8])
