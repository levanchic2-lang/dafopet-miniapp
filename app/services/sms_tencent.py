"""
腾讯云短信适配器：使用官方 SDK 直接调用，无需自建网关。
所有失败都不抛异常，返回 False 以便调用方静默兜底。
"""
from __future__ import annotations

import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return bool(
        (settings.tencent_sms_secret_id or "").strip()
        and (settings.tencent_sms_secret_key or "").strip()
        and (settings.tencent_sms_sdk_app_id or "").strip()
        and (settings.tencent_sms_sign_name or "").strip()
    )


def send_sms_template(
    phone: str,
    template_id: str,
    template_params: list[str],
) -> tuple[bool, Optional[str]]:
    """通过腾讯云发模板短信。
    手机号自动加 +86；返回 (ok, error_msg)。
    """
    if not _enabled():
        return False, "未配置腾讯云短信"
    if not phone or not template_id:
        return False, "缺手机号或模板 ID"
    try:
        from tencentcloud.common import credential
        from tencentcloud.common.profile.client_profile import ClientProfile
        from tencentcloud.common.profile.http_profile import HttpProfile
        from tencentcloud.sms.v20210111 import sms_client, models
    except ImportError as e:
        logger.warning("[sms_tencent] SDK 未装：%s", e)
        return False, f"SDK 未装: {e}"

    try:
        cred = credential.Credential(
            settings.tencent_sms_secret_id.strip(),
            settings.tencent_sms_secret_key.strip(),
        )
        hp = HttpProfile()
        hp.reqMethod = "POST"
        hp.reqTimeout = 10
        cp = ClientProfile()
        cp.httpProfile = hp
        client = sms_client.SmsClient(cred, settings.tencent_sms_region or "ap-guangzhou", cp)

        req = models.SendSmsRequest()
        # 手机号需要带国家码：+86
        normalized = phone.strip()
        if not normalized.startswith("+"):
            normalized = "+86" + normalized.lstrip("0").lstrip("86")
        req.PhoneNumberSet = [normalized]
        req.SmsSdkAppId = settings.tencent_sms_sdk_app_id.strip()
        req.SignName = settings.tencent_sms_sign_name.strip()
        req.TemplateId = template_id.strip()
        req.TemplateParamSet = [str(p) for p in template_params]

        resp = client.SendSms(req)
        # 校验返回：SendStatusSet[0].Code == "Ok"
        statuses = resp.SendStatusSet or []
        if not statuses:
            return False, "腾讯云无返回"
        first = statuses[0]
        if first.Code == "Ok":
            return True, None
        return False, f"{first.Code}: {first.Message}"
    except Exception as e:
        logger.warning("[sms_tencent] 发送异常：%s", e)
        return False, f"{type(e).__name__}: {e}"
