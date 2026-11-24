"""TNR 申请满 3 个月未完成手术时进入隔离区。

按北京时间的日历月计算：提交日加 3 个月仍未完成手术，且没有尚未到来的
手术预约，就从当前状态转入 ``quarantined``。已预约、日期在今天或以后的
手术不打断。重新申请只追加新资料，旧照片和字段快照留在原申请上。
"""

from __future__ import annotations

import json
import logging
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import (
    Application,
    ApplicationRevision,
    ApplicationStatus,
    Appointment,
    AppointmentCategory,
    AppointmentStatus,
    AuditLog,
    MediaFile,
    MediaKind,
)
from app.time_utils import BEIJING_TZ, beijing_now, to_beijing


logger = logging.getLogger(__name__)

QUARANTINE_MONTHS = 3
MIN_RESTART_IMAGES = 2

IDLE_NOTICE = (
    "若自申请提交之日起 3 个月内仍未完成手术，该申请将进入隔离区。"
    "进入隔离区后不能直接预约；如需继续，须重新提交猫咪近照和最新资料"
    "（毛色、体型等可能已变化），并经医院审核通过。"
)
QUARANTINED_NOTICE = (
    "该申请已进入隔离区，暂不能预约手术。请点「重新申请」，更新猫咪近照、"
    "体型和毛色等资料后再提交。医院审核通过后，才会重新进入正常流程。"
)
RESTART_PENDING_NOTICE = (
    "重新申请已提交，正在等待医院审核。审核通过前不能预约手术；"
    "若未通过，申请仍留在隔离区。"
)
BOOKING_BLOCKED_QUARANTINE = (
    "该申请已进入隔离区，暂不能预约手术。请先重新提交猫咪近照和资料，并等待医院审核。"
)
BOOKING_BLOCKED_RESTART = "重新申请正在等待医院审核，通过前不能预约手术。"

# 仍在流程中、手术还没做完的状态。草稿未提交，取消/拒绝已终结，不进隔离区。
_IDLE_STATUSES = {
    ApplicationStatus.pending_ai.value,
    ApplicationStatus.pending_manual.value,
    ApplicationStatus.pre_approved.value,
    ApplicationStatus.approved.value,
    ApplicationStatus.scheduled.value,
    ApplicationStatus.arrived_verified.value,
    ApplicationStatus.no_show.value,
}
_BOOKABLE_STATUSES = {
    ApplicationStatus.approved.value,
    ApplicationStatus.pre_approved.value,
    ApplicationStatus.scheduled.value,
}
_UPCOMING_APPOINTMENT_STATUSES = {
    AppointmentStatus.pending.value,
    AppointmentStatus.confirmed.value,
    AppointmentStatus.arrived.value,
}
_SURGERY_CATEGORIES = {
    AppointmentCategory.tnr.value,
    AppointmentCategory.surgery.value,
}
_CAT_GENDERS = {"male", "female", "unknown"}
_STATUS_ZH = {
    ApplicationStatus.draft.value: "草稿",
    ApplicationStatus.pending_ai.value: "系统处理中",
    ApplicationStatus.pending_manual.value: "待人工审核",
    ApplicationStatus.pre_approved.value: "预通过（待复核）",
    ApplicationStatus.approved.value: "已通过",
    ApplicationStatus.scheduled.value: "已预约",
    ApplicationStatus.no_show.value: "爽约",
    ApplicationStatus.cancelled.value: "已取消",
    ApplicationStatus.rejected.value: "已拒绝",
    ApplicationStatus.arrived_verified.value: "已到店",
    ApplicationStatus.surgery_completed.value: "手术已完成",
    ApplicationStatus.quarantined.value: "隔离区",
    ApplicationStatus.quarantine_restart_pending.value: "隔离区·待审核",
}


class QuarantineError(ValueError):
    """救助人或员工能直接看到的校验失败。"""


def status_zh(status: str) -> str:
    return _STATUS_ZH.get(status or "", status or "")


def add_months(day: date, months: int) -> date:
    """按日历月平移。目标月没有这一天时，取该月最后一天。"""
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def _as_beijing(now: datetime | None) -> datetime:
    if now is None:
        return beijing_now()
    if now.tzinfo is None:
        return now.replace(tzinfo=BEIJING_TZ)
    return now.astimezone(BEIJING_TZ)


def _as_utc_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=BEIJING_TZ)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def submission_date(row: Application) -> date | None:
    moment = row.submitted_at or row.created_at
    converted = to_beijing(moment)
    return converted.date() if converted else None


def submission_is_due(row: Application, today: date) -> bool:
    submitted = submission_date(row)
    if submitted is None:
        return False
    return add_months(submitted, QUARANTINE_MONTHS) <= today


def upcoming_application_ids(db: Session, application_ids: list[int], today: date) -> set[int]:
    """尚未到来的 TNR / 手术预约。今天的手术也算未发生，不打断。"""
    if not application_ids:
        return set()
    today_text = today.isoformat()
    rows = (
        db.query(Appointment.related_application_id)
        .filter(
            Appointment.related_application_id.in_(application_ids),
            Appointment.category.in_(tuple(_SURGERY_CATEGORIES)),
            Appointment.status.in_(tuple(_UPCOMING_APPOINTMENT_STATUSES)),
            or_(
                Appointment.appointment_date == "",
                Appointment.appointment_date >= today_text,
            ),
        )
        .all()
    )
    return {row[0] for row in rows if row[0]}


def _enter_quarantine(db: Session, row: Application, now_bj: datetime) -> None:
    previous = row.status or ""
    row.status_before_quarantine = previous
    row.status = ApplicationStatus.quarantined.value
    row.quarantined_at = _as_utc_naive(now_bj)
    row.updated_at = row.quarantined_at
    db.add(AuditLog(
        action="auto_quarantine",
        actor="system",
        application_id=row.id,
        detail=json.dumps({
            "from": previous,
            "submitted_at": (row.submitted_at or row.created_at).isoformat() if (row.submitted_at or row.created_at) else "",
            "reason": "提交已满 3 个月且手术未完成，且没有未到期的手术预约",
        }, ensure_ascii=False),
    ))


def maybe_quarantine_one(db: Session, row: Application, now: datetime | None = None) -> bool:
    now_bj = _as_beijing(now)
    if (row.status or "") not in _IDLE_STATUSES:
        return False
    if not submission_is_due(row, now_bj.date()):
        return False
    if row.id in upcoming_application_ids(db, [row.id], now_bj.date()):
        return False
    _enter_quarantine(db, row, now_bj)
    return True


def scan_idle_applications(db: Session, now: datetime | None = None) -> int:
    """把已到期且没有未发生手术预约的申请转入隔离区。"""
    now_bj = _as_beijing(now)
    today = now_bj.date()
    # 日历月判断以北京日期为准。SQL 只先缩小到可能到期的记录，最终仍由 submission_is_due 决定。
    cutoff = add_months(today, -QUARANTINE_MONTHS)
    wide_cutoff = datetime(cutoff.year, cutoff.month, cutoff.day, tzinfo=BEIJING_TZ).astimezone(timezone.utc).replace(tzinfo=None)
    wide_cutoff = wide_cutoff + timedelta(days=2)
    rows = (
        db.query(Application)
        .filter(
            Application.status.in_(tuple(_IDLE_STATUSES)),
            or_(
                Application.submitted_at <= wide_cutoff,
                (Application.submitted_at.is_(None)) & (Application.created_at <= wide_cutoff),
            ),
        )
        .all()
    )
    due = [row for row in rows if submission_is_due(row, today)]
    if not due:
        return 0
    protected = upcoming_application_ids(db, [row.id for row in due], today)
    changed = 0
    for row in due:
        if row.id in protected:
            continue
        _enter_quarantine(db, row, now_bj)
        changed += 1
    if changed:
        db.commit()
    return changed


def touch_rows(db: Session, rows: list[Application], now: datetime | None = None) -> int:
    """只检查眼前这几份申请，避免进度页为了隔离规则去扫全库。"""
    now_bj = _as_beijing(now)
    today = now_bj.date()
    due = [row for row in rows if (row.status or "") in _IDLE_STATUSES and submission_is_due(row, today)]
    if not due:
        return 0
    protected = upcoming_application_ids(db, [row.id for row in due], today)
    changed = 0
    for row in due:
        if row.id in protected:
            continue
        _enter_quarantine(db, row, now_bj)
        changed += 1
    if changed:
        db.commit()
    return changed


def booking_block_message(row: Application | None) -> str | None:
    if row is None:
        return None
    if row.status == ApplicationStatus.quarantined.value:
        return BOOKING_BLOCKED_QUARANTINE
    if row.status == ApplicationStatus.quarantine_restart_pending.value:
        return BOOKING_BLOCKED_RESTART
    return None


def customer_may_book(row: Application | None) -> bool:
    if row is None or booking_block_message(row):
        return False
    return (row.status or "") in _BOOKABLE_STATUSES


def prepare_for_booking(db: Session, row: Application | None, now: datetime | None = None) -> str | None:
    """预约前先落实隔离。到期则立刻隔离并提交，避免这次预约把过期申请又救活。"""
    if row is None:
        return None
    if maybe_quarantine_one(db, row, now=now):
        db.commit()
        db.refresh(row)
    return booking_block_message(row)


def public_flags(row: Application) -> dict:
    status = row.status or ""
    quarantined = status == ApplicationStatus.quarantined.value
    restart_pending = status == ApplicationStatus.quarantine_restart_pending.value
    terminal = status in {
        ApplicationStatus.rejected.value,
        ApplicationStatus.cancelled.value,
        ApplicationStatus.surgery_completed.value,
    }
    reason = (row.restart_reject_reason or "").strip()
    notice = ""
    if restart_pending:
        notice = RESTART_PENDING_NOTICE
    elif quarantined:
        notice = QUARANTINED_NOTICE
        if reason:
            notice = f"{notice} 上次重新申请未通过：{reason}"
    return {
        "status_zh": status_zh(status),
        "can_book": customer_may_book(row) and status in {
            ApplicationStatus.approved.value,
            ApplicationStatus.pre_approved.value,
        },
        "can_restart": quarantined,
        "restart_pending": restart_pending,
        "in_quarantine": quarantined or restart_pending,
        "idle_notice": "" if quarantined or restart_pending or terminal else IDLE_NOTICE,
        "quarantine_notice": notice,
        "restart_reject_reason": reason if quarantined else "",
        "cat_breed": row.cat_breed or "",
        "cat_color": row.cat_color or "",
        "weight_estimate": row.weight_estimate or "",
    }


def mark_submitted(row: Application, when: datetime | None = None) -> None:
    """首次真正提交时记下隔离时钟。草稿创建不算提交。"""
    moment = when or datetime.utcnow()
    if row.submitted_at is None:
        row.submitted_at = moment
    if row.original_submitted_at is None:
        row.original_submitted_at = row.submitted_at


def _clean_text(raw, limit: int) -> str:
    return str(raw or "").strip()[:limit]


def clean_restart_fields(raw: dict) -> dict:
    nickname = _clean_text(raw.get("cat_nickname"), 120)
    if not nickname:
        raise QuarantineError("请填写猫咪名字。")
    gender = _clean_text(raw.get("cat_gender"), 20).lower()
    if gender not in _CAT_GENDERS:
        raise QuarantineError("请选择猫咪性别。")
    color = _clean_text(raw.get("cat_color"), 80)
    if not color:
        raise QuarantineError("请填写毛色。猫咪相隔数月，毛色可能已经变化。")
    weight = _clean_text(raw.get("weight_estimate"), 80)
    if not weight:
        raise QuarantineError("请填写体型或体重。")
    return {
        "cat_nickname": nickname,
        "cat_gender": gender,
        "cat_breed": _clean_text(raw.get("cat_breed"), 80),
        "cat_color": color,
        "age_estimate": _clean_text(raw.get("age_estimate"), 80),
        "weight_estimate": weight,
        "health_note": _clean_text(raw.get("health_note"), 2000),
    }


def _clear_pending_media(db: Session, row: Application) -> None:
    pending = (
        db.query(MediaFile)
        .filter(
            MediaFile.application_id == row.id,
            MediaFile.restart_pending.is_(True),
        )
        .all()
    )
    for media in pending:
        path = Path(media.stored_path or "")
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                logger.warning("[tnr-quarantine] 无法删除未提交的重新申请文件 %s", path)
        db.delete(media)


def begin_restart(db: Session, row: Application, raw: dict) -> dict:
    if row.status == ApplicationStatus.quarantine_restart_pending.value:
        raise QuarantineError("重新申请已提交，请等待医院审核。")
    if row.status != ApplicationStatus.quarantined.value:
        raise QuarantineError("当前申请不在隔离区，无需重新申请。")
    fields = clean_restart_fields(raw)
    _clear_pending_media(db, row)
    row.restart_draft_json = json.dumps(fields, ensure_ascii=False)
    row.updated_at = datetime.utcnow()
    db.commit()
    return fields


def assert_restart_upload(row: Application) -> None:
    if row.status != ApplicationStatus.quarantined.value or not (row.restart_draft_json or "").strip():
        raise QuarantineError("请先保存猫咪的最新资料，再上传近照。")


def finalize_restart(db: Session, row: Application) -> None:
    if row.status != ApplicationStatus.quarantined.value:
        raise QuarantineError("当前申请不在隔离区，不能提交重新申请。")
    try:
        fields = json.loads(row.restart_draft_json or "{}")
    except (TypeError, ValueError):
        fields = {}
    if not isinstance(fields, dict) or not fields:
        raise QuarantineError("请先填写猫咪的最新资料。")
    fields = clean_restart_fields(fields)
    pending_images = (
        db.query(MediaFile)
        .filter(
            MediaFile.application_id == row.id,
            MediaFile.kind == MediaKind.application_image.value,
            MediaFile.restart_pending.is_(True),
        )
        .order_by(MediaFile.id.asc())
        .all()
    )
    if len(pending_images) < MIN_RESTART_IMAGES:
        raise QuarantineError(f"请至少上传 {MIN_RESTART_IMAGES} 张猫咪近照。旧照片会保留，但不能代替新照片。")

    current_media = (
        db.query(MediaFile)
        .filter(
            MediaFile.application_id == row.id,
            MediaFile.kind.in_([
                MediaKind.application_image.value,
                MediaKind.application_video.value,
            ]),
            MediaFile.restart_pending.is_(False),
            or_(MediaFile.is_current.is_(True), MediaFile.is_current.is_(None)),
        )
        .all()
    )
    now = datetime.utcnow()
    snapshot = {
        "cat_nickname": row.cat_nickname or "",
        "cat_gender": row.cat_gender or "",
        "cat_breed": row.cat_breed or "",
        "cat_color": row.cat_color or "",
        "age_estimate": row.age_estimate or "",
        "weight_estimate": row.weight_estimate or "",
        "health_note": row.health_note or "",
        "media_ids": [media.id for media in current_media],
        "status_before": row.status_before_quarantine or "",
        "submitted_at": row.submitted_at.isoformat() if row.submitted_at else "",
    }
    db.add(ApplicationRevision(
        application_id=row.id,
        kind="quarantine_restart",
        snapshot_json=json.dumps(snapshot, ensure_ascii=False),
        created_at=now,
    ))
    for media in current_media:
        media.is_current = False
        media.superseded_at = now
    for media in pending_images:
        media.is_current = True
        media.restart_pending = False
    pending_videos = (
        db.query(MediaFile)
        .filter(
            MediaFile.application_id == row.id,
            MediaFile.kind == MediaKind.application_video.value,
            MediaFile.restart_pending.is_(True),
        )
        .all()
    )
    for media in pending_videos:
        media.is_current = True
        media.restart_pending = False
    for key, value in fields.items():
        setattr(row, key, value)
    row.status = ApplicationStatus.quarantine_restart_pending.value
    row.restart_draft_json = ""
    row.restart_reject_reason = ""
    row.restart_count = int(row.restart_count or 0) + 1
    row.updated_at = now
    db.add(AuditLog(
        action="quarantine_restart_submit",
        actor="customer",
        application_id=row.id,
        detail=json.dumps({
            "cat_nickname": fields["cat_nickname"],
            "cat_color": fields["cat_color"],
            "weight_estimate": fields["weight_estimate"],
            "new_image_ids": [media.id for media in pending_images],
            "previous_media_ids": snapshot["media_ids"],
        }, ensure_ascii=False),
    ))
    db.commit()


def approve_restart(db: Session, row: Application, actor: str) -> None:
    if row.status != ApplicationStatus.quarantine_restart_pending.value:
        raise QuarantineError("只有「隔离区·待审核」的重新申请可以通过。")
    now = datetime.utcnow()
    row.status = ApplicationStatus.pending_manual.value
    row.submitted_at = now
    if row.original_submitted_at is None:
        row.original_submitted_at = now
    row.restart_reject_reason = ""
    row.updated_at = now
    db.add(AuditLog(
        action="quarantine_restart_approve",
        actor=(actor or "admin")[:80],
        application_id=row.id,
        detail="重新申请通过，回到待人工审核。通过后救助人才能预约手术。",
    ))
    db.commit()


def reject_restart(db: Session, row: Application, reason: str, actor: str) -> None:
    if row.status != ApplicationStatus.quarantine_restart_pending.value:
        raise QuarantineError("只有「隔离区·待审核」的重新申请可以退回。")
    text = (reason or "").strip()
    if not text:
        raise QuarantineError("请填写未通过原因。")
    now = datetime.utcnow()
    row.status = ApplicationStatus.quarantined.value
    row.restart_reject_reason = text[:500]
    row.updated_at = now
    db.add(AuditLog(
        action="quarantine_restart_reject",
        actor=(actor or "admin")[:80],
        application_id=row.id,
        detail=row.restart_reject_reason,
    ))
    db.commit()


def run_idle_quarantine_scan() -> None:
    db = SessionLocal()
    try:
        changed = scan_idle_applications(db)
        if changed:
            logger.info("[tnr-quarantine] 转入隔离区 %s 份申请", changed)
    except Exception:
        logger.exception("[tnr-quarantine] 扫描失败")
        db.rollback()
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
        logger.warning("[tnr-quarantine] APScheduler 未安装，跳过：%s", exc)
        return
    scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
    scheduler.add_job(
        run_idle_quarantine_scan,
        "cron",
        hour=3,
        minute=20,
        id="tnr_idle_quarantine",
        replace_existing=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info("[tnr-quarantine] scheduler started")


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        try:
            _scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler = None
