from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models import NotificationLog


@dataclass
class TokenCache:
    access_token: str = ""
    expires_at: float = 0.0


_token_cache = TokenCache()


def _enabled() -> bool:
    return bool(settings.wechat_appid and settings.wechat_appsecret)


def wechat_code2session(js_code: str) -> dict[str, Any]:
    """小程序登录：用 js_code 换 openid / session_key。"""
    if not _enabled():
        raise RuntimeError("未配置 WECHAT_APPID/WECHAT_APPSECRET")
    url = "https://api.weixin.qq.com/sns/jscode2session"
    params = {
        "appid": settings.wechat_appid,
        "secret": settings.wechat_appsecret,
        "js_code": js_code,
        "grant_type": "authorization_code",
    }
    with httpx.Client(timeout=8.0) as client:
        r = client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    # 失败时通常返回 errcode/errmsg
    if "errcode" in data and data.get("errcode") not in (0, "0", None):
        raise RuntimeError(f"code2session failed: {data}")
    return data


def _get_access_token() -> str:
    """服务端 access_token：内存缓存。"""
    if not _enabled():
        raise RuntimeError("未配置 WECHAT_APPID/WECHAT_APPSECRET")
    now = time.time()
    if _token_cache.access_token and now < _token_cache.expires_at - 60:
        return _token_cache.access_token
    url = "https://api.weixin.qq.com/cgi-bin/token"
    params = {
        "grant_type": "client_credential",
        "appid": settings.wechat_appid,
        "secret": settings.wechat_appsecret,
    }
    with httpx.Client(timeout=8.0) as client:
        r = client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    if "access_token" not in data:
        raise RuntimeError(f"get token failed: {data}")
    _token_cache.access_token = data["access_token"]
    _token_cache.expires_at = now + int(data.get("expires_in", 7200))
    return _token_cache.access_token


def _post_subscribe_send(payload: dict[str, Any]) -> dict[str, Any]:
    token = _get_access_token()
    url = "https://api.weixin.qq.com/cgi-bin/message/subscribe/send"
    with httpx.Client(timeout=8.0) as client:
        r = client.post(url, params={"access_token": token}, json=payload)
        r.raise_for_status()
        data = r.json()
    if data.get("errcode") not in (0, "0", None):
        raise RuntimeError(f"subscribe send failed: {data}")
    return data


def push_application_result(
    db: Session,
    application_id: int,
    openid: str,
    applicant_name: str,
    status_text: str,
    *,
    phone_masked: str = "",
    note: str = "",
    submitted_at: str = "",
    action_at: str = "",
) -> None:
    """推送：审核结果/状态更新。"""
    if not settings.wechat_tmpl_application_result:
        return
    if not _enabled() or not openid:
        return
    def v(x: str, fallback: str = "—", max_len: int = 20) -> str:
        s = (x or "").strip()
        if not s:
            s = fallback
        return s[:max_len]

    keys = [k.strip() for k in (settings.wechat_fields_application_result or "").split(",") if k.strip()]
    if not keys:
        keys = ["thing1", "thing2", "thing3", "thing4", "thing5"]

    # 针对你当前使用的模板（thing4,time5,time6）做语义填充：
    # - thing4：项目名称/事项
    # - time5：申请时间
    # - time6：通过/拒绝时间
    # 其他字段：尽量填非空文本，避免 47003
    now_str = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    sub_time = submitted_at or now_str
    act_time = action_at or now_str

    data: dict[str, Any] = {}
    for k in keys:
        if k.startswith("time"):
            # 微信 time* 字段要求时间格式
            val = sub_time if k in ("time5", "time3") else act_time
            data[k] = {"value": val}
        elif k.startswith("date"):
            data[k] = {"value": sub_time.split(" ")[0]}
        else:
            # thing*/phrase*/name*/phone_number* 等：文本
            if k == "thing4":
                data[k] = {"value": v("流浪猫TNR申请", max_len=20)}
            else:
                # 备用内容按优先级填入，确保非空
                fallback_text = v(note, fallback=v(status_text), max_len=20)
                data[k] = {"value": fallback_text}

    payload = {
        "touser": openid,
        "template_id": settings.wechat_tmpl_application_result,
        "page": settings.wechat_message_page,
        "data": data,
    }
    try:
        resp = _post_subscribe_send(payload)
        db.add(
            NotificationLog(
                application_id=application_id,
                channel="wechat_miniapp",
                payload=json.dumps({"type": "application_result", "resp": resp}, ensure_ascii=False),
                success=True,
            )
        )
        db.commit()
    except Exception as e:
        db.add(
            NotificationLog(
                application_id=application_id,
                channel="wechat_miniapp",
                payload=str(e),
                success=False,
            )
        )
        db.commit()


def push_appointment_status(
    db: Session,
    appointment_id: int,
    openid: str,
    status_text: str,
    *,
    service_name: str = "",
    store: str = "",
    appointment_date: str = "",
    appointment_time: str = "",
    note: str = "",
) -> None:
    """推送：预约状态变更（确认/取消）通知。复用 application_result 模板。"""
    if not settings.wechat_tmpl_application_result:
        return
    if not _enabled() or not openid:
        return

    def v(x: str, fallback: str = "—", max_len: int = 20) -> str:
        s = (x or "").strip()
        if not s:
            s = fallback
        return s[:max_len]

    keys = [k.strip() for k in (settings.wechat_fields_application_result or "").split(",") if k.strip()]
    if not keys:
        keys = ["thing1", "thing2", "thing3", "thing4", "thing5"]

    now_str = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    appt_time_str = f"{appointment_date} {appointment_time}".strip() or now_str

    data: dict[str, Any] = {}
    for k in keys:
        if k.startswith("time") or k.startswith("date"):
            data[k] = {"value": appt_time_str if "date" in k else appt_time_str}
        elif k == "thing4":
            svc = v(service_name, fallback="美容/门诊预约", max_len=20)
            data[k] = {"value": svc}
        else:
            msg = v(note, fallback=v(status_text, max_len=20), max_len=20)
            data[k] = {"value": msg}

    payload = {
        "touser": openid,
        "template_id": settings.wechat_tmpl_application_result,
        "page": settings.wechat_message_page,
        "data": data,
    }
    try:
        resp = _post_subscribe_send(payload)
        db.add(
            NotificationLog(
                application_id=None,
                channel="wechat_miniapp",
                payload=json.dumps({"type": "appointment_status", "appointment_id": appointment_id, "status": status_text, "resp": resp}, ensure_ascii=False),
                success=True,
            )
        )
        db.commit()
    except Exception as e:
        db.add(
            NotificationLog(
                application_id=None,
                channel="wechat_miniapp",
                payload=str(e),
                success=False,
            )
        )
        db.commit()


def push_surgery_reminder(
    db: Session,
    appointment_id: int,
    openid: str,
    cat_name: str,
    *,
    appointment_date: str = "",
    appointment_time: str = "",
    reminder_type: str = "day_before",
) -> None:
    """推送：手术前提醒（前一天或当天）。复用 surgery_done 模板。"""
    if not settings.wechat_tmpl_surgery_done:
        return
    if not _enabled() or not openid:
        return

    def v(x: str, fallback: str = "—", max_len: int = 20) -> str:
        s = (x or "").strip()
        if not s:
            s = fallback
        return s[:max_len]

    keys = [k.strip() for k in (settings.wechat_fields_surgery_done or "").split(",") if k.strip()]
    if not keys:
        keys = ["thing1", "thing2", "thing3"]

    appt_time_str = f"{appointment_date} {appointment_time}".strip()
    if reminder_type == "day_before":
        note_text = "明天手术，请提前禁食禁水"
    else:
        note_text = "今天手术，请按约定时间到院"

    data: dict[str, Any] = {}
    for k in keys:
        if k.startswith("time"):
            data[k] = {"value": appt_time_str or time.strftime("%Y-%m-%d", time.localtime())}
        elif k == "thing5":
            data[k] = {"value": v("TNR手术提醒", max_len=20)}
        elif k == "thing4":
            data[k] = {"value": v(note_text, max_len=20)}
        else:
            data[k] = {"value": v(cat_name, fallback="猫咪", max_len=20)}

    payload = {
        "touser": openid,
        "template_id": settings.wechat_tmpl_surgery_done,
        "page": settings.wechat_message_page,
        "data": data,
    }
    log_type = f"surgery_reminder_{reminder_type}"
    try:
        resp = _post_subscribe_send(payload)
        db.add(
            NotificationLog(
                application_id=None,
                channel="wechat_miniapp",
                payload=json.dumps({"type": log_type, "appointment_id": appointment_id, "resp": resp}, ensure_ascii=False),
                success=True,
            )
        )
        db.commit()
    except Exception as e:
        db.add(
            NotificationLog(
                application_id=None,
                channel="wechat_miniapp",
                payload=str(e),
                success=False,
            )
        )
        db.commit()


def push_surgery_done(
    db: Session,
    application_id: int,
    openid: str,
    cat_name: str,
    *,
    note: str = "",
    action_at: str = "",
) -> None:
    """推送：手术完成。"""
    if not settings.wechat_tmpl_surgery_done:
        return
    if not _enabled() or not openid:
        return
    def v(x: str, fallback: str = "—", max_len: int = 20) -> str:
        s = (x or "").strip()
        if not s:
            s = fallback
        return s[:max_len]

    keys = [k.strip() for k in (settings.wechat_fields_surgery_done or "").split(",") if k.strip()]
    if not keys:
        keys = ["thing1", "thing2", "thing3"]

    # 针对你当前模板（thing5,time3,thing4）：
    # - thing5：服务通知/标题
    # - time3：完成时间
    # - thing4：温馨提示
    now_str = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    act_time = action_at or now_str
    data: dict[str, Any] = {}
    for k in keys:
        if k.startswith("time"):
            data[k] = {"value": act_time}
        elif k == "thing5":
            data[k] = {"value": v("TNR手术完成通知", max_len=20)}
        elif k == "thing4":
            data[k] = {"value": v(note, fallback="请按医嘱护理", max_len=20)}
        else:
            data[k] = {"value": v(cat_name, fallback="猫咪", max_len=20)}

    payload = {
        "touser": openid,
        "template_id": settings.wechat_tmpl_surgery_done,
        "page": settings.wechat_message_page,
        "data": data,
    }
    try:
        resp = _post_subscribe_send(payload)
        db.add(
            NotificationLog(
                application_id=application_id,
                channel="wechat_miniapp",
                payload=json.dumps({"type": "surgery_done", "resp": resp}, ensure_ascii=False),
                success=True,
            )
        )
        db.commit()
    except Exception as e:
        db.add(
            NotificationLog(
                application_id=application_id,
                channel="wechat_miniapp",
                payload=str(e),
                success=False,
            )
        )
        db.commit()

