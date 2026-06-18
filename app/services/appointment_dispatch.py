"""预约开始前 15 分钟企业微信提醒。
每分钟扫一次今日预约：开始前 15 分钟内、状态 pending/confirmed、尚未推送过的，
推企微给该门店绑了 wecom_userid 的员工 + 所有超管。每条只推一次
（reminder_pushed_at 标记）。

时间口径：预约的 appointment_date/appointment_time 是员工录入的本地（北京）时间字符串；
进程时区已钉 Asia/Shanghai，故 datetime.now() 即北京时间，直接比对。
"""
from __future__ import annotations
import logging
import re
from datetime import datetime

from sqlalchemy import or_

from app.config import settings
from app.database import SessionLocal
from app.models import Appointment, AppointmentStatus, AdminUser, Pet

logger = logging.getLogger(__name__)

LEAD_MIN = 15   # 提前提醒分钟数

_FULL_TO_SHORT = {
    "大风动物医院（东环店）": "东环店",
    "大风动物医院（横岗店）": "横岗店",
}
_CATEGORY_SHORT = {
    "tnr": "TNR", "outpatient": "门诊", "surgery": "手术",
    "beauty": "美容", "grooming": "造型", "washcare": "洗护",
}


def _calendar_url() -> str:
    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}/admin/calendar" if base else "/admin/calendar"


def _push_wecom(userid: str, content: str) -> None:
    try:
        from app.services.wecom_client import send_app_message
        send_app_message({
            "touser": userid,
            "msgtype": "text",
            "text": {"content": content},
        })
    except Exception as e:
        logger.warning("[appt push] %s failed: %s", userid, e)


def _store_admins(db, store_short: str) -> list:
    """该门店有 wecom_userid 的活跃员工 + 所有超管。store_short 为空 → 仅超管。"""
    q = db.query(AdminUser).filter(
        AdminUser.is_active == True,   # noqa: E712
        AdminUser.wecom_userid != "",
    )
    if store_short:
        q = q.filter(or_(AdminUser.store == store_short, AdminUser.role == "superadmin"))
    else:
        q = q.filter(AdminUser.role == "superadmin")
    return q.all()


def scan_upcoming_appointments() -> None:
    """每分钟跑一次。"""
    db = SessionLocal()
    try:
        now = datetime.now()
        today = now.date().isoformat()
        rows = db.query(Appointment).filter(
            Appointment.appointment_date == today,
            Appointment.status.in_([
                AppointmentStatus.pending.value, AppointmentStatus.confirmed.value,
            ]),
            Appointment.reminder_pushed_at == None,  # noqa: E711
        ).all()
        if not rows:
            return

        pushed = 0
        for a in rows:
            t = (a.appointment_time or "").strip()
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
                continue
            try:
                appt_dt = datetime.strptime(f"{a.appointment_date} {t}", "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            mins = (appt_dt - now).total_seconds() / 60.0
            if mins > LEAD_MIN:
                continue                      # 还没到提醒窗，下分钟再看
            if mins < -2:
                a.reminder_pushed_at = now    # 已错过窗口，标记防重复扫
                continue

            store_short = _FULL_TO_SHORT.get(a.store or "", a.store or "")
            admins = _store_admins(db, store_short)
            if not admins:
                # 没人绑企微 → 别标记，等有人绑了再推（窗口内会重试）
                continue

            pet = db.get(Pet, a.pet_id) if a.pet_id else None
            pet_name = (pet.name if pet else None) or a.pet_name or ""
            cat = _CATEGORY_SHORT.get(a.category or "", a.category or "")
            when = "已到点" if mins <= 0 else f"还有 {int(round(mins))} 分钟"
            lines = [
                f"⏰ 预约提醒 · {when}",
                f"{t}  {cat}{(' · ' + a.service_name) if a.service_name else ''}",
                f"{a.customer_name or '客户'}{(' · ' + pet_name) if pet_name else ''}{(' · ' + store_short) if store_short else ''}",
            ]
            if a.phone:
                lines.append(a.phone)
            lines.append(f"日历：{_calendar_url()}")
            content = "\n".join(lines)
            for u in admins:
                _push_wecom(u.wecom_userid, content)
            a.reminder_pushed_at = now
            pushed += 1

        db.commit()
        if pushed:
            logger.info("[appt] 15min reminder pushed: %d appointments", pushed)
    except Exception:
        logger.exception("[appt] scan_upcoming_appointments failed")
    finally:
        db.close()


# ─── 调度器 ───
_scheduler = None


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception as e:
        logger.warning("[appt] APScheduler 未安装，跳过：%s", e)
        return
    sch = BackgroundScheduler(timezone="Asia/Shanghai")
    sch.add_job(scan_upcoming_appointments, "cron", minute="*",
                id="appt_15min_reminder", replace_existing=True)
    sch.start()
    _scheduler = sch
    logger.info("[appt] scheduler started")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
