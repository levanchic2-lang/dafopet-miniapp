"""
回访任务调度：
- run_due_dispatch(): 把"今日 / 已逾期 + pending"的回访打标签 status=due
  并尝试通过外部渠道（小程序订阅消息 / 短信）发送，发送成功 → status=sent。
  全部渠道失败 → status=phone_pending（员工电话兜底）。
- run_no_reply_promote(): 把 status=sent 但发出超过 48h 还没反馈的，
  转为 phone_pending，提醒员工打电话。

发送渠道适配（次提交里逐步实现）：
- send_via_miniapp(fu, customer, pet, visit) → bool
- send_via_sms(fu, customer, pet, visit) → bool

本提交先把骨架建起来：调度器、循环、状态流转、日志，发送函数返回 False（占位）。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings  # noqa
from app.database import SessionLocal
from app.models import FollowUp, Customer, Pet, Visit

logger = logging.getLogger(__name__)


# ─── 渠道发送（占位，下一 commit 实现） ───
def send_via_miniapp(fu: FollowUp, cust: Optional[Customer],
                     pet: Optional[Pet], visit: Optional[Visit]) -> bool:
    """通过微信小程序订阅消息发送回访。返回是否成功。"""
    return False


def send_via_sms(fu: FollowUp, cust: Optional[Customer],
                 pet: Optional[Pet], visit: Optional[Visit]) -> bool:
    """通过短信网关发送回访。返回是否成功。"""
    if not settings.sms_gateway_url:
        return False
    return False


# ─── 调度任务 ───
def _try_send(fu: FollowUp, cust, pet, visit) -> str:
    """按优先级（小程序 → 短信）尝试发送，返回成功的 channel 或空串。"""
    if send_via_miniapp(fu, cust, pet, visit):
        return "miniapp"
    if send_via_sms(fu, cust, pet, visit):
        return "sms"
    return ""


def run_due_dispatch(db: Optional[Session] = None) -> dict:
    """扫描需要派发的回访 → 尝试发送 → 更新状态。
    返回 {scanned, sent, phone_pending} 计数。
    """
    own_session = db is None
    if own_session:
        db = SessionLocal()
    try:
        today_str = date.today().isoformat()
        rows = (
            db.query(FollowUp)
            .filter(
                FollowUp.status.in_(["pending", "due"]),
                FollowUp.planned_date <= today_str,
            )
            .all()
        )
        scanned = len(rows)
        sent = 0
        phone_pending = 0
        now = datetime.utcnow()
        for fu in rows:
            cust  = db.get(Customer, fu.customer_id) if fu.customer_id else None
            pet   = db.get(Pet,      fu.pet_id)      if fu.pet_id      else None
            visit = db.get(Visit,    fu.visit_id)    if fu.visit_id    else None
            channel = _try_send(fu, cust, pet, visit)
            if channel:
                fu.status = "sent"
                fu.channel = channel
                fu.sent_at = now
                sent += 1
            else:
                # 全部渠道失败 → 转电话兜底
                fu.status = "phone_pending"
                phone_pending += 1
            fu.updated_at = now
        if scanned:
            db.commit()
            logger.info("[followup dispatch] scanned=%d sent=%d phone_pending=%d",
                        scanned, sent, phone_pending)
        return {"scanned": scanned, "sent": sent, "phone_pending": phone_pending}
    finally:
        if own_session:
            db.close()


def run_no_reply_promote(db: Optional[Session] = None) -> dict:
    """把已发送 48h 仍未反馈的转为 phone_pending。"""
    own_session = db is None
    if own_session:
        db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(hours=48)
        rows = (
            db.query(FollowUp)
            .filter(
                FollowUp.status == "sent",
                FollowUp.sent_at.isnot(None),
                FollowUp.sent_at < cutoff,
            )
            .all()
        )
        n = len(rows)
        for fu in rows:
            fu.status = "phone_pending"
            fu.response = fu.response or "no_reply"
            fu.updated_at = datetime.utcnow()
        if n:
            db.commit()
            logger.info("[followup no-reply] promoted=%d", n)
        return {"promoted": n}
    finally:
        if own_session:
            db.close()


# ─── 调度器（APScheduler） ───
_scheduler = None


def start_scheduler() -> None:
    """在 FastAPI 启动时调用一次。重复调用安全。"""
    global _scheduler
    if _scheduler is not None:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception as e:
        logger.warning("[followup] APScheduler 未安装，跳过定时任务：%s", e)
        return
    sch = BackgroundScheduler(timezone="Asia/Shanghai")
    # 每小时第 5 分钟跑一次派发
    sch.add_job(run_due_dispatch, "cron", minute=5, id="followup_dispatch", replace_existing=True)
    # 每小时第 35 分钟跑一次"48h 未反馈 → 电话兜底"
    sch.add_job(run_no_reply_promote, "cron", minute=35, id="followup_no_reply", replace_existing=True)
    sch.start()
    _scheduler = sch
    logger.info("[followup] scheduler started")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
