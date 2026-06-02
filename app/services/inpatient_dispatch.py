"""住院模块的定时推送：
1. scan_overdue_medications  每 5 分钟扫一次。
   scheduled_at 已过 grace 分钟仍 pending → 推企微给该店绑了 wecom_userid 的助理。
   每条 log 只推一次（reminder_sent_at 标记）。
2. send_shift_handover_reminder  班次切换前 10 分钟推今日剩余任务清单。
   早 7点（6:50）/ 中 15点（14:50）/ 夜 22点（21:50）触发。
"""
from __future__ import annotations
import logging
from datetime import datetime, timedelta

from sqlalchemy import or_

from app.config import settings
from app.database import SessionLocal
from app.models import (
    Hospitalization, MedicationAdminLog, AdminUser, Pet, Customer,
    PrescriptionItem,
)

logger = logging.getLogger(__name__)

OVERDUE_GRACE_MIN = 30   # 超过 30 分钟未打勾算漏药


def _build_inpatient_url(hosp_id: int) -> str:
    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}/admin/inpatient/{hosp_id}#meds" if base else f"/admin/inpatient/{hosp_id}#meds"


def _push_wecom(userid: str, content: str) -> None:
    try:
        from app.services.wecom_client import send_app_message
        send_app_message({
            "touser": userid,
            "msgtype": "text",
            "text": {"content": content},
        })
    except Exception as e:
        logger.warning("[inpatient push] %s failed: %s", userid, e)


def _store_admins(db, store: str) -> list:
    """该门店有 wecom_userid 的活跃员工 + 所有超管。"""
    q = db.query(AdminUser).filter(
        AdminUser.is_active == True,
        AdminUser.wecom_userid != "",
    )
    if store:
        q = q.filter(or_(AdminUser.store == store, AdminUser.role == "superadmin"))
    return q.all()


def scan_overdue_medications() -> None:
    """每 5 分钟跑一次。"""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        threshold = now - timedelta(minutes=OVERDUE_GRACE_MIN)
        rows = db.query(MedicationAdminLog).filter(
            MedicationAdminLog.status == "pending",
            MedicationAdminLog.scheduled_at <= threshold,
            MedicationAdminLog.reminder_sent_at == None,  # noqa: E711
        ).all()
        if not rows:
            return

        # 按 hosp 分组
        by_hosp: dict[int, list] = {}
        for r in rows:
            by_hosp.setdefault(r.hospitalization_id, []).append(r)

        pushed_total = 0
        for hosp_id, logs in by_hosp.items():
            h = db.get(Hospitalization, hosp_id)
            if not h or h.status != "admitted":
                # 出院/取消的就别再推了，直接标记
                for r in logs:
                    r.reminder_sent_at = now
                continue
            pet = db.get(Pet, h.pet_id) if h.pet_id else None
            cust = db.get(Customer, h.customer_id) if h.customer_id else None
            admins = _store_admins(db, h.store or "")
            if not admins:
                # 没人收，先别标记，等 admin 绑了再推
                continue
            # 构造内容
            lines = [
                f"⚠ 漏药提醒",
                f"宠物：{pet.name if pet else '宠物'}（{cust.name if cust else '客户'}）"
                f"{' · ' + h.cage.code if h.cage else ''}",
                "",
                "未打勾的发药：",
            ]
            sorted_logs = sorted(logs, key=lambda x: x.scheduled_at)
            for r in sorted_logs[:5]:
                drug = r.prescription_item.drug_name if r.prescription_item else "药物"
                lines.append(f"  · {r.scheduled_at.strftime('%H:%M')} {drug}")
            if len(sorted_logs) > 5:
                lines.append(f"  …还有 {len(sorted_logs) - 5} 条")
            lines.append("")
            lines.append(f"打勾：{_build_inpatient_url(hosp_id)}")
            content = "\n".join(lines)
            # 推
            for u in admins:
                _push_wecom(u.wecom_userid, content)
            # 标记
            for r in logs:
                r.reminder_sent_at = now
            pushed_total += 1

        db.commit()
        if pushed_total:
            logger.info("[inpatient] overdue push: %d hospitalizations", pushed_total)
    except Exception:
        logger.exception("[inpatient] scan_overdue_medications failed")
    finally:
        db.close()


def send_shift_handover_reminder(shift_label: str) -> None:
    """接班前 10 分钟推送今日剩余任务清单。"""
    db = SessionLocal()
    try:
        # 当前 admitted 的住院
        hosps = db.query(Hospitalization).filter(Hospitalization.status == "admitted").all()
        if not hosps:
            return
        now = datetime.utcnow()
        # 今天剩余 + 明天前几小时的任务（避免接班瞬间漏掉接班后立即到的药）
        cutoff_end = now + timedelta(hours=9)
        # 按 store 聚合
        by_store: dict[str, list] = {}
        for h in hosps:
            by_store.setdefault(h.store or "", []).append(h)

        for store, hosp_list in by_store.items():
            admins = _store_admins(db, store)
            if not admins:
                continue
            hosp_ids = [h.id for h in hosp_list]
            pending = db.query(MedicationAdminLog).filter(
                MedicationAdminLog.hospitalization_id.in_(hosp_ids),
                MedicationAdminLog.status == "pending",
                MedicationAdminLog.scheduled_at <= cutoff_end,
            ).order_by(MedicationAdminLog.scheduled_at).all()
            if not pending:
                continue
            lines = [
                f"📋 {shift_label}交接提醒 · {store or '通用'}",
                f"接下来 9h 待执行的发药任务：",
                "",
            ]
            # 按 hosp 分组
            by_h: dict[int, list] = {}
            for r in pending:
                by_h.setdefault(r.hospitalization_id, []).append(r)
            for hosp_id, items in list(by_h.items())[:10]:
                h = next((x for x in hosp_list if x.id == hosp_id), None)
                if not h:
                    continue
                pet_name = h.pet.name if h.pet else "宠物"
                cage_code = h.cage.code if h.cage else "—"
                lines.append(f"🐾 {pet_name}（{cage_code}）· {len(items)} 条")
                for r in items[:3]:
                    drug = r.prescription_item.drug_name if r.prescription_item else "药"
                    lines.append(f"  {r.scheduled_at.strftime('%H:%M')} {drug}")
                if len(items) > 3:
                    lines.append(f"  …还有 {len(items) - 3}")
                lines.append("")
            if len(by_h) > 10:
                lines.append(f"…还有 {len(by_h) - 10} 只宠物")
            lines.append(f"看板：{_build_inpatient_url(0).replace('/0', '')}")
            content = "\n".join(lines)
            for u in admins:
                _push_wecom(u.wecom_userid, content)
        logger.info("[inpatient] shift handover pushed: %s", shift_label)
    except Exception:
        logger.exception("[inpatient] shift handover failed")
    finally:
        db.close()


def _push_shift_morning():
    send_shift_handover_reminder("早班")


def _push_shift_afternoon():
    send_shift_handover_reminder("中班")


def _push_shift_night():
    send_shift_handover_reminder("夜班")


# ─── 调度器 ───
_scheduler = None


def start_scheduler() -> None:
    """在 FastAPI 启动时调用一次。重复调用安全。"""
    global _scheduler
    if _scheduler is not None:
        return
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception as e:
        logger.warning("[inpatient] APScheduler 未安装，跳过：%s", e)
        return
    sch = BackgroundScheduler(timezone="Asia/Shanghai")
    # 漏药扫描：每 5 分钟跑一次
    sch.add_job(scan_overdue_medications, "cron", minute="*/5",
                id="inpatient_overdue", replace_existing=True)
    # 接班提醒：早班 6:50 / 中班 14:50 / 夜班 21:50
    sch.add_job(_push_shift_morning,   "cron", hour=6,  minute=50,
                id="inpatient_shift_morning",   replace_existing=True)
    sch.add_job(_push_shift_afternoon, "cron", hour=14, minute=50,
                id="inpatient_shift_afternoon", replace_existing=True)
    sch.add_job(_push_shift_night,     "cron", hour=21, minute=50,
                id="inpatient_shift_night",     replace_existing=True)
    sch.start()
    _scheduler = sch
    logger.info("[inpatient] scheduler started")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
