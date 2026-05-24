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
