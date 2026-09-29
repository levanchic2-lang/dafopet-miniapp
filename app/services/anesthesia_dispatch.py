"""Automatic lifecycle maintenance for anesthesia monitoring sheets."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from app.database import SessionLocal
from app.models import AnesthesiaMonitorSheet

logger = logging.getLogger(__name__)

STALE_HOURS = 72
AUTO_CLOSE_NOTE = "系统：监护记录超过72小时未更新，已自动结束监护。"


def auto_close_stale_monitors(now_utc: datetime | None = None) -> int:
    """Close open sheets whose last activity is older than 72 hours."""
    db = SessionLocal()
    now = now_utc or datetime.utcnow()
    cutoff = now - timedelta(hours=STALE_HOURS)
    try:
        rows = db.query(AnesthesiaMonitorSheet).filter(
            AnesthesiaMonitorSheet.status == "open",
            AnesthesiaMonitorSheet.updated_at <= cutoff,
        ).all()
        for sheet in rows:
            last_activity = sheet.updated_at or sheet.created_at or now
            sheet.status = "closed"
            sheet.closed_at = now
            if not sheet.end_time:
                sheet.end_time = (last_activity + timedelta(hours=8)).strftime("%H:%M")
            if not sheet.recovery_status:
                sheet.recovery_status = "苏醒良好"
            existing = (sheet.recovery_notes or "").strip()
            if AUTO_CLOSE_NOTE not in existing:
                sheet.recovery_notes = f"{existing}\n{AUTO_CLOSE_NOTE}".strip()
            sheet.updated_at = now
        db.commit()
        if rows:
            logger.info("[anesthesia] auto-closed %d stale monitor sheets", len(rows))
        return len(rows)
    except Exception:
        db.rollback()
        logger.exception("[anesthesia] stale monitor auto-close failed")
        return 0
    finally:
        db.close()


_scheduler = None


def start_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception as exc:
        logger.warning("[anesthesia] APScheduler unavailable: %s", exc)
        return
    auto_close_stale_monitors()
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        auto_close_stale_monitors,
        "cron",
        minute=20,
        id="anesthesia_stale_auto_close",
        replace_existing=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("[anesthesia] scheduler started")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
