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


# ─── 渠道发送 ───
_VISIT_TYPE_ZH = {
    "outpatient": "门诊", "surgery": "手术", "postop": "术后复查",
    "beauty": "美容", "vaccine": "疫苗", "followup": "复诊",
    "surgery_consult": "手术咨询", "other": "其他",
}


def _build_feedback_url(token: str) -> str:
    """根据 public_base_url 构造完整反馈链接。未配则用相对路径。"""
    base = (settings.public_base_url or "").strip().rstrip("/")
    if base:
        return f"{base}/follow-up/{token}"
    return f"/follow-up/{token}"


def _customer_openid(cust: Optional[Customer], db: Session) -> str:
    """从 Customer.wechat_openid 取；空则回退查 Application."""
    if not cust:
        return ""
    if cust.wechat_openid:
        return cust.wechat_openid.strip()
    # fallback：按手机号查 Application
    if cust.phone:
        from app.models import Application
        app = (
            db.query(Application)
            .filter(Application.phone == cust.phone, Application.wechat_openid != "")
            .order_by(Application.id.desc())
            .first()
        )
        if app and app.wechat_openid:
            return app.wechat_openid.strip()
    return ""


def send_via_miniapp(fu: FollowUp, cust: Optional[Customer],
                     pet: Optional[Pet], visit: Optional[Visit],
                     db: Optional[Session] = None) -> bool:
    """通过微信小程序订阅消息发送回访。返回是否成功。"""
    if db is None or not cust or not visit:
        return False
    if not settings.wechat_tmpl_followup:
        return False
    openid = _customer_openid(cust, db)
    if not openid:
        return False
    try:
        from app.services.wechat_miniapp import push_followup
        return push_followup(
            db,
            openid=openid,
            pet_name=(pet.name if pet else "您的宝贝"),
            visit_type_zh=_VISIT_TYPE_ZH.get(visit.visit_type or "", visit.visit_type or "门诊"),
            visit_date=visit.visit_date or "",
            feedback_url=_build_feedback_url(fu.feedback_token),
            customer_id=cust.id,
        )
    except Exception as e:
        logger.warning("[followup] miniapp send failed: %s", e)
        return False


def send_via_sms(fu: FollowUp, cust: Optional[Customer],
                 pet: Optional[Pet], visit: Optional[Visit],
                 db: Optional[Session] = None) -> bool:
    """通过短信网关发送回访。返回是否成功。"""
    if not settings.sms_gateway_url or not cust or not cust.phone:
        return False
    pet_name = (pet.name if pet else "您的宝贝")
    vt_zh = _VISIT_TYPE_ZH.get((visit.visit_type if visit else "") or "", "诊后") if visit else "诊后"
    text = (
        f"【大风动物医院】您家{pet_name}{visit.visit_date if visit else ''}做了{vt_zh}，"
        f"现在感觉怎么样？点击反馈：{_build_feedback_url(fu.feedback_token)}"
    )
    try:
        from app.services.sms_gateway import send_sms
        return send_sms(cust.phone, text, scene="followup")
    except Exception as e:
        logger.warning("[followup] sms send failed: %s", e)
        return False


# ─── 调度任务 ───
def _try_send(fu: FollowUp, cust, pet, visit, db: Session) -> str:
    """按优先级（小程序 → 短信）尝试发送，返回成功的 channel 或空串。"""
    if send_via_miniapp(fu, cust, pet, visit, db):
        return "miniapp"
    if send_via_sms(fu, cust, pet, visit, db):
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
            channel = _try_send(fu, cust, pet, visit, db)
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
    """把已发送 48h 仍未反馈的转为 phone_pending，并按 assigned_to 聚合后推送企微通知。"""
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
        promoted_by_user: dict[str, list] = {}
        for fu in rows:
            fu.status = "phone_pending"
            fu.response = fu.response or "no_reply"
            fu.updated_at = datetime.utcnow()
            key = (fu.assigned_to or "").strip()
            if key:
                promoted_by_user.setdefault(key, []).append(fu)
        if n:
            db.commit()
            logger.info("[followup no-reply] promoted=%d", n)
            # 按主治医师聚合后推企微一条（避免刷屏）
            try:
                _push_phone_pending_summary(db, promoted_by_user)
            except Exception as e:
                logger.warning("[followup no-reply] wecom push failed: %s", e)
        return {"promoted": n}
    finally:
        if own_session:
            db.close()


def _push_phone_pending_summary(db: Session, by_user: dict) -> None:
    """按 assigned_to (AdminUser.username) 聚合 → 一条企微消息列出该用户名下的电话兜底列表。"""
    if not by_user:
        return
    try:
        from app.services.wecom_client import send_app_message as _send
        from app.models import AdminUser as _AU, Pet as _Pet, Customer as _Cust
    except Exception:
        return
    base = (settings.public_base_url or "").strip().rstrip("/")
    for username, fus in by_user.items():
        u = db.query(_AU).filter(_AU.username == username, _AU.is_active == True).first()
        if not u or not u.wecom_userid:
            continue
        lines = [f"📞 客户 48h 未反馈，请电话回访（{len(fus)} 单）"]
        for fu in fus[:8]:
            pet = db.get(_Pet, fu.pet_id) if fu.pet_id else None
            cust = db.get(_Cust, fu.customer_id) if fu.customer_id else None
            phone = (cust.phone if cust else "") or ""
            pet_name = pet.name if pet else "客户宠物"
            cust_name = cust.name if cust else "客户"
            lines.append(f"· {pet_name}（{cust_name}{' '+phone if phone else ''}）")
        if len(fus) > 8:
            lines.append(f"...还有 {len(fus) - 8} 单")
        if base:
            lines.append(f"详情：{base}/admin/follow-ups?tab=overdue&mine=1")
        try:
            _send(u.wecom_userid, "\n".join(lines))
        except Exception as e:
            logger.warning("[followup no-reply] send to %s failed: %s", username, e)


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
