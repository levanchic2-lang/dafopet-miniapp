"""
通用短信网关适配。
config 里配 sms_gateway_url + sms_gateway_secret 后，向网关 POST：
  {
    "to": "13800138000",
    "text": "短信正文",
    "scene": "followup",
    "timestamp": 1700000000
  }
请求头：X-Signature: HMAC-SHA256(body, secret)（如果配了 secret）

网关返回 2xx 视为成功。具体的供应商对接（阿里云/腾讯云/容联）由网关那边处理。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def send_sms(to: str, text: str, scene: str = "general") -> bool:
    """同步发短信。配置缺失或失败一律返回 False。"""
    url = (settings.sms_gateway_url or "").strip()
    if not url or not to or not text:
        return False
    body = {
        "to": to.strip(),
        "text": text[:480],
        "scene": scene,
        "timestamp": int(time.time()),
    }
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers: dict[str, str] = {"content-type": "application/json"}
    secret = (settings.sms_gateway_secret or "").strip()
    if secret:
        sig = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
        headers["X-Signature"] = sig
    try:
        with httpx.Client(timeout=8.0) as cli:
            r = cli.post(url, content=raw, headers=headers)
        if 200 <= r.status_code < 300:
            return True
        logger.warning("[sms] gateway %s returned %d: %s", url, r.status_code, r.text[:200])
        return False
    except Exception as e:
        logger.warning("[sms] gateway error: %s", e)
        return False
