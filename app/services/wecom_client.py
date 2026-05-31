"""企业微信自建应用客户端。

封装：
  - access_token 内存缓存（2 小时有效，自动续期）
  - OAuth 网页授权（code → userid）
  - 应用消息发送（Phase 2 用）

配置：app.config.settings.wecom_corp_id / wecom_agent_id / wecom_secret
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings

logger = logging.getLogger("wecom")

_API_BASE = "https://qyapi.weixin.qq.com"


@dataclass
class _TokenCache:
    access_token: str = ""
    expires_at: float = 0.0


_token_cache = _TokenCache()


def enabled() -> bool:
    return bool(settings.wecom_corp_id and settings.wecom_agent_id and settings.wecom_secret)


def _get_access_token() -> str:
    if not enabled():
        raise RuntimeError("企业微信未配置（WECOM_CORP_ID/WECOM_AGENT_ID/WECOM_SECRET）")
    now = time.time()
    if _token_cache.access_token and now < _token_cache.expires_at - 60:
        return _token_cache.access_token
    url = f"{_API_BASE}/cgi-bin/gettoken"
    params = {"corpid": settings.wecom_corp_id, "corpsecret": settings.wecom_secret}
    with httpx.Client(timeout=8.0) as client:
        r = client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    if data.get("errcode") not in (0, "0", None) or "access_token" not in data:
        raise RuntimeError(f"企微 gettoken 失败: {data}")
    _token_cache.access_token = data["access_token"]
    _token_cache.expires_at = now + int(data.get("expires_in", 7200))
    return _token_cache.access_token


def build_oauth_url(redirect_uri: str, state: str = "") -> str:
    """构造企业微信 OAuth 授权链接。

    用户在企业微信内打开 → 自动带 code 回跳 redirect_uri。
    snsapi_base 静默授权，无弹窗。
    """
    if not enabled():
        raise RuntimeError("企业微信未配置")
    params = {
        "appid": settings.wecom_corp_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "snsapi_base",
        "agentid": settings.wecom_agent_id,
        "state": state or "wecom",
    }
    qs = urllib.parse.urlencode(params)
    return f"https://open.weixin.qq.com/connect/oauth2/authorize?{qs}#wechat_redirect"


def code_to_userid(code: str) -> dict[str, Any]:
    """OAuth code → userid。

    返回示例：{"errcode":0, "errmsg":"ok", "userid":"LiangTianBing", "user_ticket":"..."}
    或外部用户：{"errcode":0, "errmsg":"ok", "external_userid":"..."}
    """
    token = _get_access_token()
    url = f"{_API_BASE}/cgi-bin/auth/getuserinfo"
    with httpx.Client(timeout=8.0) as client:
        r = client.get(url, params={"access_token": token, "code": code})
        r.raise_for_status()
        data = r.json()
    if data.get("errcode") not in (0, "0", None):
        raise RuntimeError(f"企微 getuserinfo 失败: {data}")
    return data


def get_user_detail(userid: str) -> dict[str, Any]:
    """根据 userid 拉取员工详情（姓名、部门、手机等），用于绑定时展示。"""
    token = _get_access_token()
    url = f"{_API_BASE}/cgi-bin/user/get"
    with httpx.Client(timeout=8.0) as client:
        r = client.get(url, params={"access_token": token, "userid": userid})
        r.raise_for_status()
        data = r.json()
    if data.get("errcode") not in (0, "0", None):
        raise RuntimeError(f"企微 user/get 失败: {data}")
    return data


# ── 客户联系 API（Phase 3 用）─────────────────────────────────

def external_get_follow_user_list() -> dict[str, Any]:
    """列出企业里所有「配置了客户联系」的成员 userid 列表。

    第一个要调的 API。返回 errcode=0 说明应用有客户联系权限。
    常见错误码：
      60011 - 应用无客户联系权限
      60020 - IP 不在白名单
      48002 - 接口未在客户联系 → 权限配置 → 「可调用接口的应用」白名单里
    """
    token = _get_access_token()
    url = f"{_API_BASE}/cgi-bin/externalcontact/get_follow_user_list"
    with httpx.Client(timeout=8.0) as client:
        r = client.get(url, params={"access_token": token})
        r.raise_for_status()
        return r.json()


def external_list_by_userid(userid: str) -> dict[str, Any]:
    """列出某个成员（userid）名下的所有外部联系人（external_userid 数组）。"""
    token = _get_access_token()
    url = f"{_API_BASE}/cgi-bin/externalcontact/list"
    with httpx.Client(timeout=8.0) as client:
        r = client.get(url, params={"access_token": token, "userid": userid})
        r.raise_for_status()
        return r.json()


def external_get_detail(external_userid: str) -> dict[str, Any]:
    """拿单个外部联系人详情：unionid / name / avatar / 跟进员工 / 备注名 / 备注手机号 等。"""
    token = _get_access_token()
    url = f"{_API_BASE}/cgi-bin/externalcontact/get"
    with httpx.Client(timeout=8.0) as client:
        r = client.get(url, params={"access_token": token, "external_userid": external_userid})
        r.raise_for_status()
        return r.json()


# ── JSAPI Ticket（JS-SDK 鉴权签名用）─────────────────────────────

@dataclass
class _JsapiTicketCache:
    corp_ticket: str = ""
    corp_expires_at: float = 0.0
    agent_ticket: str = ""
    agent_expires_at: float = 0.0


_jsapi_cache = _JsapiTicketCache()


def _get_jsapi_ticket(agent: bool = False) -> str:
    """获取 jsapi_ticket（公司级 / 应用级）。

    agent=False: 用于 wx.config（公司级）
    agent=True:  用于 wx.agentConfig（应用级）
    """
    now = time.time()
    if agent:
        if _jsapi_cache.agent_ticket and now < _jsapi_cache.agent_expires_at - 60:
            return _jsapi_cache.agent_ticket
    else:
        if _jsapi_cache.corp_ticket and now < _jsapi_cache.corp_expires_at - 60:
            return _jsapi_cache.corp_ticket
    token = _get_access_token()
    # 企微两套不同的接口：
    #   公司级 wx.config 用：/cgi-bin/get_jsapi_ticket?access_token=...（独立端点，无 type 参数）
    #   应用级 wx.agentConfig 用：/cgi-bin/ticket/get?access_token=...&type=agent_config
    if agent:
        url = f"{_API_BASE}/cgi-bin/ticket/get"
        params = {"access_token": token, "type": "agent_config"}
    else:
        url = f"{_API_BASE}/cgi-bin/get_jsapi_ticket"
        params = {"access_token": token}
    with httpx.Client(timeout=8.0) as client:
        r = client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    if data.get("errcode") not in (0, "0", None):
        raise RuntimeError(f"jsapi ticket 失败({'agent' if agent else 'corp'}): {data}")
    ticket = data["ticket"]
    expires_in = int(data.get("expires_in", 7200))
    if agent:
        _jsapi_cache.agent_ticket = ticket
        _jsapi_cache.agent_expires_at = now + expires_in
    else:
        _jsapi_cache.corp_ticket = ticket
        _jsapi_cache.corp_expires_at = now + expires_in
    return ticket


def build_jsapi_signature(url: str, agent: bool = False) -> dict[str, Any]:
    """构造 JS-SDK 鉴权所需的 4 元素（含签名）。

    返回：{appId, agentid, timestamp, nonceStr, signature}
    URL 必须是完整的页面 URL（不含 #hash）。
    """
    if not enabled():
        raise RuntimeError("企业微信未配置")
    import hashlib
    import secrets as _sec
    ticket = _get_jsapi_ticket(agent=agent)
    timestamp = int(time.time())
    nonce = _sec.token_hex(8)
    # 去掉 URL fragment
    clean_url = url.split("#")[0]
    raw = f"jsapi_ticket={ticket}&noncestr={nonce}&timestamp={timestamp}&url={clean_url}"
    signature = hashlib.sha1(raw.encode("utf-8")).hexdigest()
    return {
        "appId": settings.wecom_corp_id,
        "agentid": int(settings.wecom_agent_id),
        "timestamp": timestamp,
        "nonceStr": nonce,
        "signature": signature,
    }


def external_batch_get_by_user(userid_list: list[str], cursor: str = "", limit: int = 100) -> dict[str, Any]:
    """批量拉取多个员工名下的客户详情（含跟进员工备注名/备注手机号 等）。

    单次最多 100 条，配合 cursor 翻页。最高效的同步方式。
    """
    token = _get_access_token()
    url = f"{_API_BASE}/cgi-bin/externalcontact/batch/get_by_user"
    body = {"userid_list": userid_list, "limit": limit}
    if cursor:
        body["cursor"] = cursor
    with httpx.Client(timeout=15.0) as client:
        r = client.post(url, params={"access_token": token}, json=body)
        r.raise_for_status()
        return r.json()


def send_app_message(payload: dict[str, Any]) -> dict[str, Any]:
    """发送应用消息（Phase 2 用，先预留）。

    payload 形如：
      {"touser":"xxx|xxx","msgtype":"textcard","agentid":int,
       "textcard":{"title":"...","description":"...","url":"...","btntxt":"详情"}}
    """
    token = _get_access_token()
    payload = dict(payload)
    payload.setdefault("agentid", int(settings.wecom_agent_id))
    url = f"{_API_BASE}/cgi-bin/message/send"
    with httpx.Client(timeout=8.0) as client:
        r = client.post(url, params={"access_token": token}, json=payload)
        r.raise_for_status()
        data = r.json()
    if data.get("errcode") not in (0, "0", None):
        logger.warning("[wecom send fail] %s payload=%s", data, payload)
    return data
