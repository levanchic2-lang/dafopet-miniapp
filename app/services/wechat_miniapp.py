from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
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


def _get_access_token(*, force: bool = False) -> str:
    """服务端 access_token：内存缓存。force=True 时丢掉缓存重新向微信要。"""
    if not _enabled():
        raise RuntimeError("未配置 WECHAT_APPID/WECHAT_APPSECRET")
    now = time.time()
    if (
        not force
        and _token_cache.access_token
        and now < _token_cache.expires_at - 60
    ):
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


def send_customer_service_text(openid: str, content: str) -> dict[str, Any]:
    """小程序客服文本消息。复用上面的 access_token 缓存。"""
    if not _enabled():
        raise RuntimeError("未配置 WECHAT_APPID/WECHAT_APPSECRET")
    touser = (openid or "").strip()
    text = (content or "").strip()
    if not touser or not text:
        raise RuntimeError("客服消息缺少接收人或内容")
    raw = text.encode("utf-8")
    if len(raw) > 2000:
        text = raw[:2000].decode("utf-8", errors="ignore")
    payload = {"touser": touser, "msgtype": "text", "text": {"content": text}}
    return _post_custom_message(payload)


def send_customer_service_miniprogram_page(
    openid: str,
    *,
    title: str,
    pagepath: str,
    thumb_media_id: str,
) -> dict[str, Any]:
    """小程序客服里发一张可点开的小程序卡片。"""
    if not _enabled():
        raise RuntimeError("未配置 WECHAT_APPID/WECHAT_APPSECRET")
    touser = (openid or "").strip()
    card_title = (title or "").strip()
    path = (pagepath or "").strip()
    media_id = (thumb_media_id or "").strip()
    if not touser or not card_title or not path or not media_id:
        raise RuntimeError("小程序卡片缺少接收人、标题、路径或封面")
    payload = {
        "touser": touser,
        "msgtype": "miniprogrampage",
        "miniprogrampage": {
            "title": card_title[:64],
            "pagepath": path[:1000],
            "thumb_media_id": media_id,
        },
    }
    return _post_custom_message(payload)


def upload_temp_image(path: str) -> str:
    """上传临时图片素材，返回 media_id。临时素材大约 3 天有效。"""
    if not _enabled():
        raise RuntimeError("未配置 WECHAT_APPID/WECHAT_APPSECRET")
    file_path = Path(path)
    if not file_path.is_file():
        raise RuntimeError(f"封面图片不存在: {file_path}")
    mime = "image/png" if file_path.suffix.lower() == ".png" else "image/jpeg"
    url = "https://api.weixin.qq.com/cgi-bin/media/upload"
    last: dict[str, Any] = {}
    for attempt in (1, 2):
        token = _get_access_token(force=(attempt == 2))
        with file_path.open("rb") as handle:
            files = {"media": (file_path.name, handle, mime)}
            with httpx.Client(timeout=20.0) as client:
                response = client.post(
                    url,
                    params={"access_token": token, "type": "image"},
                    files=files,
                )
                response.raise_for_status()
                data = response.json()
        err = data.get("errcode")
        media_id = str(data.get("media_id") or "").strip()
        if media_id and err in (0, "0", None):
            return media_id
        try:
            code = int(err)
        except (TypeError, ValueError):
            code = -1
        last = data
        if code in _TOKEN_RETRY_CODES and attempt == 1:
            _token_cache.access_token = ""
            _token_cache.expires_at = 0.0
            continue
        raise RuntimeError(f"media upload failed: {data}")
    raise RuntimeError(f"media upload failed: {last}")


def _post_custom_message(payload: dict[str, Any]) -> dict[str, Any]:
    url = "https://api.weixin.qq.com/cgi-bin/message/custom/send"
    last: dict[str, Any] = {}
    for attempt in (1, 2):
        token = _get_access_token(force=(attempt == 2))
        with httpx.Client(timeout=8.0) as client:
            response = client.post(url, params={"access_token": token}, json=payload)
            response.raise_for_status()
            data = response.json()
        err = data.get("errcode")
        if err in (0, "0", None):
            return data
        try:
            code = int(err)
        except (TypeError, ValueError):
            code = -1
        last = data
        if code in _TOKEN_RETRY_CODES and attempt == 1:
            _token_cache.access_token = ""
            _token_cache.expires_at = 0.0
            continue
        raise RuntimeError(f"customer service send failed: {data}")
    raise RuntimeError(f"customer service send failed: {last}")


# 40001 凭证无效、40014 token 不合法、42001 token 过期：刷新后再试一次。
_TOKEN_RETRY_CODES = {40001, 40014, 42001}


def _post_subscribe_send(payload: dict[str, Any]) -> dict[str, Any]:
    url = "https://api.weixin.qq.com/cgi-bin/message/subscribe/send"
    last: dict[str, Any] = {}
    for attempt in (1, 2):
        token = _get_access_token(force=(attempt == 2))
        with httpx.Client(timeout=8.0) as client:
            response = client.post(url, params={"access_token": token}, json=payload)
            response.raise_for_status()
            data = response.json()
        err = data.get("errcode")
        if err in (0, "0", None):
            return data
        try:
            code = int(err)
        except (TypeError, ValueError):
            code = -1
        last = data
        if code in _TOKEN_RETRY_CODES and attempt == 1:
            _token_cache.access_token = ""
            _token_cache.expires_at = 0.0
            continue
        raise RuntimeError(f"subscribe send failed: {data}")
    raise RuntimeError(f"subscribe send failed: {last}")


def _write_notify_log(
    db: Session,
    *,
    application_id: int | None,
    event_type: str,
    success: bool,
    body: dict[str, Any],
    appointment_id: int | None = None,
    commit: bool,
) -> None:
    db.add(
        NotificationLog(
            application_id=application_id,
            channel="wechat_miniapp",
            payload=json.dumps(body, ensure_ascii=False),
            success=success,
            event_type=(event_type or "")[:40],
            appointment_id=appointment_id,
        )
    )
    if commit:
        db.commit()


def _deliver(
    db: Session,
    payload: dict[str, Any],
    *,
    event_type: str,
    application_id: int | None = None,
    appointment_id: int | None = None,
    event_key: str = "",
    extra: dict[str, Any] | None = None,
    commit: bool = True,
    skip_if_any_attempt: bool = False,
) -> bool:
    """发送订阅消息并记日志。成功扣授权次数；43101 标未授权。同一 event_key 成功过不再发。"""
    from app.services.wechat_subscribe import (
        consume_grant,
        mark_grant_unauthorized,
        subscribe_event_seen,
    )

    if event_key and subscribe_event_seen(db, event_key, success_only=not skip_if_any_attempt):
        return False
    openid = str(payload.get("touser") or "")
    template_id = str(payload.get("template_id") or "")
    try:
        resp = _post_subscribe_send(payload)
    except Exception as exc:
        if "43101" in str(exc):
            mark_grant_unauthorized(db, openid, template_id)
        body: dict[str, Any] = {"type": event_type, "event_key": event_key, "error": str(exc)}
        if extra:
            body.update(extra)
        _write_notify_log(
            db,
            application_id=application_id,
            event_type=event_type,
            success=False,
            body=body,
            appointment_id=appointment_id,
            commit=commit,
        )
        return False
    consume_grant(db, openid, template_id)
    body = {"type": event_type, "event_key": event_key, "resp": resp}
    if extra:
        body.update(extra)
    _write_notify_log(
        db,
        application_id=application_id,
        event_type=event_type,
        success=True,
        body=body,
        appointment_id=appointment_id,
        commit=commit,
    )
    return True


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
    event_key: str = "",
) -> bool:
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
    return _deliver(
        db,
        payload,
        event_type="application_result",
        application_id=application_id,
        event_key=event_key or f"application_result:{application_id}:{status_text}",
    )


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
    phone: str = "",
    customer_name: str = "",
    note: str = "",
    event_key: str = "",
    application_id: int | None = None,
) -> bool:
    """推送：预约状态变更（确认/取消/改约）通知。使用 wechat_tmpl_appointment 模板。"""
    tmpl = settings.wechat_tmpl_appointment or settings.wechat_tmpl_application_result
    if not tmpl:
        return
    if not _enabled() or not openid:
        return

    def v(x: str, fallback: str = "—", max_len: int = 20) -> str:
        s = (x or "").strip()
        if not s:
            s = fallback
        return s[:max_len]

    # 优先使用预约专属字段配置，否则回退到通用配置
    fields_cfg = settings.wechat_fields_appointment or settings.wechat_fields_application_result
    keys = [k.strip() for k in (fields_cfg or "").split(",") if k.strip()]
    if not keys:
        keys = ["time1", "thing2", "phone_number3", "thing4", "thing5"]

    now_str = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    appt_time_str = f"{appointment_date} {appointment_time}".strip() or now_str

    data: dict[str, Any] = {}
    for k in keys:
        if k.startswith("time") or k.startswith("date"):
            # time1 → 预约时间
            data[k] = {"value": appt_time_str}
        elif k.startswith("phone_number"):
            # phone_number3 → 联系电话
            data[k] = {"value": v(phone, fallback="—", max_len=20)}
        elif k == "thing2":
            # thing2 → 预约项目
            data[k] = {"value": v(service_name, fallback="TNR手术预约", max_len=20)}
        elif k == "thing4":
            # thing4 → 客户姓名
            data[k] = {"value": v(customer_name, fallback="申请人", max_len=20)}
        elif k == "thing5":
            # thing5 → 预约地址/门店
            data[k] = {"value": v(store, fallback="大风动物医院", max_len=20)}
        else:
            # 其余字段填状态或备注
            data[k] = {"value": v(note, fallback=v(status_text, max_len=20), max_len=20)}

    payload = {
        "touser": openid,
        "template_id": tmpl,
        "page": settings.wechat_message_page,
        "data": data,
    }
    return _deliver(
        db,
        payload,
        event_type="appointment_status",
        application_id=application_id,
        appointment_id=appointment_id,
        event_key=event_key or f"appointment_status:{appointment_id}:{status_text}",
        extra={"appointment_id": appointment_id, "status": status_text},
    )


def push_surgery_reminder(
    db: Session,
    appointment_id: int,
    openid: str,
    cat_name: str,
    *,
    appointment_date: str = "",
    appointment_time: str = "",
    reminder_type: str = "day_before",
    event_key: str = "",
    application_id: int | None = None,
    skip_if_any_attempt: bool = False,
) -> bool:
    """推送：手术前提醒（前一天或当天）。使用专用 surgery_reminder 模板（预约提醒）。"""
    tmpl_id = (settings.wechat_tmpl_surgery_reminder or "").strip()
    if not tmpl_id:
        return
    if not _enabled() or not openid:
        return

    def v(x: str, fallback: str = "—", max_len: int = 20) -> str:
        s = (x or "").strip()
        if not s:
            s = fallback
        return s[:max_len]

    # 字段：thing1=用户名称, thing2=预约项目, time3=预约时间, thing4=温馨提示
    keys = [k.strip() for k in (settings.wechat_fields_surgery_reminder or "thing1,thing2,time3,thing4").split(",") if k.strip()]

    appt_time_str = f"{appointment_date} {appointment_time}".strip()
    if reminder_type == "day_before":
        note_text = "明天手术，请提前禁食禁水"
    else:
        note_text = "今天手术，请按约定时间到院"

    data: dict[str, Any] = {}
    for k in keys:
        if k.startswith("time"):
            data[k] = {"value": appt_time_str or time.strftime("%Y-%m-%d", time.localtime())}
        elif k == "thing2":
            data[k] = {"value": v("TNR手术安排", max_len=20)}
        elif k == "thing4" or k == "thing5":
            data[k] = {"value": v(note_text, max_len=20)}
        else:
            # thing1 = 用户名称
            data[k] = {"value": v(cat_name, fallback="猫咪", max_len=20)}

    payload = {
        "touser": openid,
        "template_id": tmpl_id,
        "page": settings.wechat_message_page,
        "data": data,
    }
    log_type = f"surgery_reminder_{reminder_type}"
    return _deliver(
        db,
        payload,
        event_type=log_type,
        application_id=application_id,
        appointment_id=appointment_id,
        event_key=event_key or f"{log_type}_{appointment_id}",
        extra={"appointment_id": appointment_id},
        skip_if_any_attempt=skip_if_any_attempt,
    )


def push_surgery_done(
    db: Session,
    application_id: int,
    openid: str,
    cat_name: str,
    *,
    note: str = "",
    action_at: str = "",
    event_key: str = "",
) -> bool:
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
    return _deliver(
        db,
        payload,
        event_type="surgery_done",
        application_id=application_id,
        event_key=event_key or f"surgery_done:{application_id}",
    )

def push_rejection_notice(
    db,
    application_id: int,
    openid: str,
    cat_nickname: str,
    *,
    reason: str = "",
    action_at: str = "",
    event_key: str = "",
) -> bool:
    """推送：审核不通过通知。模板字段：thing1=审核说明, phrase2=审核结果, thing3=审核对象, time4=审核时间。"""
    if not settings.wechat_tmpl_rejection:
        return
    if not _enabled() or not openid:
        return

    def v(x: str, fallback: str = "—", max_len: int = 20) -> str:
        s = (x or "").strip()
        if not s:
            s = fallback
        return s[:max_len]

    now_str = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    act_time = action_at or now_str

    keys = [k.strip() for k in (settings.wechat_fields_rejection or "").split(",") if k.strip()]
    if not keys:
        keys = ["thing1", "phrase2", "thing3", "time4"]

    data = {}
    for k in keys:
        if k.startswith("time"):
            data[k] = {"value": act_time}
        elif k.startswith("phrase"):
            data[k] = {"value": "审核不通过"}
        elif k == "thing1":
            data[k] = {"value": v(reason, fallback="不符合申请条件", max_len=20)}
        elif k == "thing3":
            data[k] = {"value": v(cat_nickname, fallback="申请猫咪", max_len=20)}
        else:
            data[k] = {"value": v(reason, fallback="请联系医院前台", max_len=20)}

    payload = {
        "touser": openid,
        "template_id": settings.wechat_tmpl_rejection,
        "page": settings.wechat_message_page,
        "data": data,
    }
    return _deliver(
        db,
        payload,
        event_type="rejection_notice",
        application_id=application_id,
        event_key=event_key or f"rejection:{application_id}",
    )


def push_preventive_reminder(
    db: Session,
    openid: str,
    pet_name: str,
    service_name: str,
    *,
    next_due_date: str = "",
    source_refs: list[dict[str, Any]] | None = None,
) -> bool:
    """发送疫苗/驱虫到期订阅消息；只有微信确认发送成功才返回 True。"""
    tmpl_id = (settings.wechat_tmpl_vaccine_reminder or "").strip()
    if not tmpl_id:
        return False
    if not _enabled() or not openid:
        return False

    def v(x: str, fallback: str = "—", max_len: int = 20) -> str:
        s = (x or "").strip()
        if not s:
            s = fallback
        return s[:max_len]

    keys = [k.strip() for k in (settings.wechat_fields_vaccine_reminder or "thing5,thing8,thing11,time7").split(",") if k.strip()]
    data: dict[str, Any] = {}
    for k in keys:
        if k.startswith("time") or k.startswith("date"):
            # time7 → 服务时间（到期日）
            data[k] = {"value": next_due_date or time.strftime("%Y-%m-%d", time.localtime())}
        elif k in ("thing11",):
            # thing11 → 服务项目（疫苗 / 驱虫）
            data[k] = {"value": v(service_name, fallback="疫苗或驱虫", max_len=20)}
        elif k in ("thing8",):
            # thing8 → 服务对象（宠物名）
            data[k] = {"value": v(pet_name, fallback="宠物", max_len=20)}
        elif k in ("thing5",):
            # thing5 → 温馨提示
            data[k] = {"value": "项目即将到期，可按需要预约到院"}
        else:
            # 其余 thing* 字段兜底：填宠物名
            data[k] = {"value": v(pet_name, fallback="宠物", max_len=20)}

    payload = {
        "touser": openid,
        "template_id": tmpl_id,
        "page": "pages/immunization/index",
        "data": data,
        "miniprogram_state": "formal",
        "lang": "zh_CN",
    }
    try:
        resp = _post_subscribe_send(payload)
        db.add(
            NotificationLog(
                application_id=None,
                channel="wechat_miniapp",
                payload=json.dumps({
                    "type": "preventive_reminder",
                    "sources": source_refs or [],
                    "resp": resp,
                }, ensure_ascii=False),
                success=True,
            )
        )
        db.commit()
        return True
    except Exception as e:
        db.add(
            NotificationLog(
                application_id=None,
                channel="wechat_miniapp",
                payload=json.dumps({
                    "type": "preventive_reminder",
                    "sources": source_refs or [],
                    "error": str(e),
                }, ensure_ascii=False),
                success=False,
            )
        )
        db.commit()
        return False


def push_vaccine_reminder(
    db: Session,
    vaccination_id: int,
    openid: str,
    pet_name: str,
    vaccine_type_zh: str,
    *,
    next_due_date: str = "",
) -> bool:
    """兼容旧调用；新流程统一走疫苗/驱虫到期提醒。"""
    return push_preventive_reminder(
        db,
        openid=openid,
        pet_name=pet_name,
        service_name=vaccine_type_zh,
        next_due_date=next_due_date,
        source_refs=[{"type": "vaccination", "id": vaccination_id}],
    )


def push_pending_manual_notice(
    db,
    application_id: int,
    openid: str,
    applicant_name: str,
    *,
    submitted_at: str = "",
    event_key: str = "",
) -> bool:
    """推送：进入人工审核提醒。模板字段：thing2=申请人, time3=申请时间, thing4=业务类型, character_string5=业务编号, phrase11=审核状态。"""
    if not settings.wechat_tmpl_pending_manual:
        return
    if not _enabled() or not openid:
        return

    def v(x: str, fallback: str = "—", max_len: int = 20) -> str:
        s = (x or "").strip()
        if not s:
            s = fallback
        return s[:max_len]

    now_str = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    sub_time = submitted_at or now_str

    keys = [k.strip() for k in (settings.wechat_fields_pending_manual or "").split(",") if k.strip()]
    if not keys:
        keys = ["thing2", "time3", "thing4", "character_string5", "phrase11"]

    data = {}
    for k in keys:
        if k.startswith("time"):
            data[k] = {"value": sub_time}
        elif k.startswith("phrase"):
            # phrase 类型接受预定义词汇，"审核中"是合法值
            data[k] = {"value": "审核中"}
        elif k.startswith("character_string"):
            data[k] = {"value": str(application_id)}
        elif k == "thing2":
            data[k] = {"value": v(applicant_name, fallback="申请人", max_len=20)}
        elif k == "thing4":
            data[k] = {"value": "流浪猫TNR申请"}
        else:
            data[k] = {"value": v(applicant_name, fallback="申请人", max_len=20)}

    payload = {
        "touser": openid,
        "template_id": settings.wechat_tmpl_pending_manual,
        "page": settings.wechat_message_page,
        "data": data,
    }
    return _deliver(
        db,
        payload,
        event_type="pending_manual_notice",
        application_id=application_id,
        event_key=event_key or f"pending_manual:{application_id}",
    )


def push_followup(
    db: Session,
    openid: str,
    pet_name: str,
    visit_type_zh: str,
    visit_date: str,
    feedback_url: str,
    *,
    customer_id: int | None = None,
) -> bool:
    """推送回访订阅消息。客户点击进入反馈短链。
    返回 True/False（不抛异常，调度器靠返回值判断是否走 fallback 渠道）。
    """
    tmpl_id = (settings.wechat_tmpl_followup or "").strip()
    if not tmpl_id or not _enabled() or not openid:
        return False

    def v(x: str, fallback: str = "—", max_len: int = 20) -> str:
        s = (x or "").strip()
        return (s if s else fallback)[:max_len]

    keys = [k.strip() for k in (settings.wechat_fields_followup or "").split(",") if k.strip()]
    if not keys:
        keys = ["thing1", "thing2", "time3", "thing4"]

    data: dict[str, Any] = {}
    for k in keys:
        if k == "thing1":
            data[k] = {"value": v(pet_name, "您的宝贝")}
        elif k == "thing2":
            data[k] = {"value": v(visit_type_zh, "门诊")}
        elif k.startswith("time"):
            data[k] = {"value": v(visit_date, time.strftime("%Y-%m-%d"))}
        elif k == "thing4":
            data[k] = {"value": "点击反馈当前状况，30 秒搞定"[:20]}
        else:
            data[k] = {"value": v(visit_type_zh)}

    payload = {
        "touser": openid,
        "template_id": tmpl_id,
        "page": settings.wechat_message_page,
        "data": data,
        "miniprogram_state": "formal",
        "lang": "zh_CN",
    }
    try:
        resp = _post_subscribe_send(payload)
        db.add(NotificationLog(
            application_id=customer_id or 0,
            channel="wechat_miniapp",
            payload=json.dumps({"type": "followup", "feedback_url": feedback_url, "resp": resp}, ensure_ascii=False),
            success=True,
        ))
        db.commit()
        return True
    except Exception as e:
        db.add(NotificationLog(
            application_id=customer_id or 0,
            channel="wechat_miniapp",
            payload=f"followup push failed: {e}",
            success=False,
        ))
        db.commit()
        return False


def push_consent_signature(
    db: Session,
    openid: str,
    cust_name: str,
    clinic_name: str,
    title: str,
    initiated_at: str,
    sign_url: str,
    *,
    customer_id: int | None = None,
) -> bool:
    """推送电子合同签约通知（协议签署）。返回是否成功。
    模板字段：thing5=甲方(客户) / thing6=乙方(医院) / thing1=合同名称 / time12=发起时间 / thing4=备注。
    """
    tmpl_id = (settings.wechat_tmpl_consent or "").strip()
    if not tmpl_id or not _enabled() or not openid:
        return False

    def v(x: str, fallback: str = "—", max_len: int = 20) -> str:
        s = (x or "").strip()
        return (s if s else fallback)[:max_len]

    keys = [k.strip() for k in (settings.wechat_fields_consent or "").split(",") if k.strip()]
    if not keys:
        keys = ["thing5", "thing6", "thing1", "time12", "thing4"]

    data: dict[str, Any] = {}
    for k in keys:
        if k == "thing5":   # 甲方 = 客户
            data[k] = {"value": v(cust_name, "客户")}
        elif k == "thing6": # 乙方 = 医院
            data[k] = {"value": v(clinic_name, "大风动物医院")}
        elif k == "thing1": # 合同名称
            data[k] = {"value": v(title, "诊疗协议")}
        elif k.startswith("time"):  # 发起时间
            data[k] = {"value": v(initiated_at, time.strftime("%Y-%m-%d %H:%M"))}
        elif k == "thing4": # 备注
            data[k] = {"value": "请尽快前往小程序完成签约"[:20]}
        else:
            data[k] = {"value": v(title)}

    payload = {
        "touser": openid,
        "template_id": tmpl_id,
        "page": settings.wechat_message_page,
        "data": data,
        "miniprogram_state": "formal",
        "lang": "zh_CN",
    }
    try:
        resp = _post_subscribe_send(payload)
        db.add(NotificationLog(
            application_id=customer_id or 0,
            channel="wechat_miniapp",
            payload=json.dumps({"type": "consent_signature", "sign_url": sign_url, "resp": resp}, ensure_ascii=False),
            success=True,
        ))
        db.commit()
        return True
    except Exception as e:
        db.add(NotificationLog(
            application_id=customer_id or 0,
            channel="wechat_miniapp",
            payload=f"consent push failed: {e}",
            success=False,
        ))
        db.commit()
        return False


def push_inpatient_medication_reminder(
    db: Session,
    openid: str,
    pet_name: str,
    drug_summary: str,
    scheduled_at: str,
    *,
    dose_summary: str = "",
    cage_code: str = "",
    hospitalization_id: int | None = None,
    overdue_summary: str = "",
) -> bool:
    """Send the staff miniapp service notification for an overdue medication group."""
    tmpl_id = (settings.wechat_tmpl_inpatient_medication or "").strip()
    if not tmpl_id or not _enabled() or not openid:
        return False

    def v(value: str, fallback: str = "—", max_len: int = 20) -> str:
        text = (value or "").strip() or fallback
        return text[:max_len]

    keys = [
        key.strip() for key in (settings.wechat_fields_inpatient_medication or "").split(",")
        if key.strip()
    ] or ["short_thing4", "thing5", "time6", "short_thing7"]
    pet_label = pet_name + (f" · {cage_code}" if cage_code else "")
    field_values = {
        # 微信 short_thing 最多 5 个字符，超长会返回 47003。
        "short_thing4": v(drug_summary, "用药", 5),
        "thing5": v(pet_label, "住院动物"),
        "time6": v(scheduled_at, time.strftime("%Y-%m-%d %H:%M")),
        "short_thing7": v(dose_summary, "按医嘱", 5),
    }
    data: dict[str, Any] = {}
    for key in keys:
        if key in field_values:
            data[key] = {"value": field_values[key]}
            continue
        if key.startswith("time"):
            data[key] = {"value": v(scheduled_at, time.strftime("%Y-%m-%d %H:%M"))}
        elif key.startswith("date"):
            data[key] = {"value": v(scheduled_at.split(" ")[0] if scheduled_at else "")}
        elif key.startswith("phrase"):
            data[key] = {"value": "待处理"}
        elif key == "thing1":
            data[key] = {"value": v(pet_label, "住院动物")}
        elif key == "thing2":
            data[key] = {"value": v(drug_summary, "住院用药")}
        elif key == "thing4":
            data[key] = {"value": v(overdue_summary, "用药任务尚未确认")}
        else:
            data[key] = {"value": v(drug_summary, "住院用药")}

    payload = {
        "touser": openid,
        "template_id": tmpl_id,
        "page": "pages/staff/inpatient-meds/inpatient-meds",
        "data": data,
        "miniprogram_state": "formal",
        "lang": "zh_CN",
    }
    try:
        response = _post_subscribe_send(payload)
        db.add(NotificationLog(
            application_id=0,
            channel="wechat_miniapp",
            payload=json.dumps({
                "type": "inpatient_medication",
                "hospitalization_id": hospitalization_id,
                "resp": response,
            }, ensure_ascii=False),
            success=True,
        ))
        return True
    except Exception as exc:
        db.add(NotificationLog(
            application_id=0,
            channel="wechat_miniapp",
            payload=f"inpatient medication push failed: {exc}",
            success=False,
        ))
        return False
