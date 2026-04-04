import logging
import smtplib
import hmac
import hashlib
import json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.models import NotificationLog

logger = logging.getLogger(__name__)


def notify_application_result(db: Session, application_id: int, phone: str, applicant_name: str, approved: bool, extra: str = "") -> None:
    body_lines = [
        f"尊敬的 {applicant_name}：",
        "",
        "您在大风动物医院提交的流浪猫 TNR 绝育申请已有更新。",
        "",
        "审核结果：" + ("已通过。请按短信/电话约定时间携带猫咪来院，并遵守术前禁食禁水等须知。" if approved else "未通过。如有疑问请联系医院前台。"),
    ]
    if extra:
        body_lines.extend(["", "补充说明：", extra])
    body_lines.extend(["", "—— 大风动物医院（系统自动通知）"])
    body = "\n".join(body_lines)
    subject = settings.notify_email_subject_prefix + ("申请已通过" if approved else "申请未通过")

    payload = {
        "event": "application_result",
        "application_id": application_id,
        "to": phone,
        "applicant_name": applicant_name,
        "approved": approved,
        "subject": subject,
        "body": body,
        "extra": extra,
    }

    log = NotificationLog(
        application_id=application_id,
        channel="log",
        payload=f"SUBJECT={subject}\nTO={phone}\n\n{body}",
        success=True,
    )
    db.add(log)
    db.commit()
    logger.info("通知记录 application_id=%s approved=%s", application_id, approved)

    # Webhook 推送（不阻塞主流程）
    if settings.notify_webhook_url:
        try:
            body_json = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json; charset=utf-8"}
            if settings.notify_webhook_secret:
                sig = hmac.new(
                    settings.notify_webhook_secret.encode("utf-8"),
                    body_json,
                    hashlib.sha256,
                ).hexdigest()
                headers["X-Signature"] = sig
            with httpx.Client(timeout=8.0) as client:
                r = client.post(settings.notify_webhook_url, content=body_json, headers=headers)
                ok = 200 <= r.status_code < 300
                db.add(
                    NotificationLog(
                        application_id=application_id,
                        channel="webhook",
                        payload=f"status={r.status_code}\nresp={(r.text or '')[:500]}",
                        success=ok,
                    )
                )
                db.commit()
        except Exception as e:
            logger.exception("Webhook 推送失败: %s", e)
            db.add(
                NotificationLog(
                    application_id=application_id,
                    channel="webhook",
                    payload=str(e),
                    success=False,
                )
            )
            db.commit()

    # 短信网关推送（不阻塞主流程）
    # 由网关将 body/subject 渲染成短信模板并下发到手机号
    if settings.sms_gateway_url:
        try:
            sms_payload = {
                "event": payload["event"],
                "application_id": application_id,
                "phone": phone,
                "approved": approved,
                "subject": subject,
                "text": body,
                "extra": extra,
            }
            body_json = json.dumps(sms_payload, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json; charset=utf-8"}
            if settings.sms_gateway_secret:
                sig = hmac.new(
                    settings.sms_gateway_secret.encode("utf-8"),
                    body_json,
                    hashlib.sha256,
                ).hexdigest()
                headers["X-Signature"] = sig
            with httpx.Client(timeout=8.0) as client:
                r = client.post(settings.sms_gateway_url, content=body_json, headers=headers)
                ok = 200 <= r.status_code < 300
                db.add(
                    NotificationLog(
                        application_id=application_id,
                        channel="sms_gateway",
                        payload=f"status={r.status_code}\nresp={(r.text or '')[:500]}",
                        success=ok,
                    )
                )
                db.commit()
        except Exception as e:
            logger.exception("短信网关推送失败: %s", e)
            db.add(
                NotificationLog(
                    application_id=application_id,
                    channel="sms_gateway",
                    payload=str(e),
                    success=False,
                )
            )
            db.commit()

    if not settings.smtp_host or not settings.smtp_from:
        return

    try:
        msg = MIMEMultipart()
        msg["Subject"] = subject
        msg["From"] = settings.smtp_from
        msg["To"] = phone if "@" in phone else settings.smtp_user
        msg.attach(MIMEText(body, "plain", "utf-8"))
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as s:
            s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.sendmail(settings.smtp_from, msg["To"].split(","), msg.as_string())
        email_log = NotificationLog(
            application_id=application_id,
            channel="email",
            payload=subject,
            success=True,
        )
        db.add(email_log)
        db.commit()
    except Exception as e:
        logger.exception("邮件发送失败: %s", e)
        fail = NotificationLog(
            application_id=application_id,
            channel="email",
            payload=str(e),
            success=False,
        )
        db.add(fail)
        db.commit()
