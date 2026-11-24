"""TNR 满 3 个月未完成手术进入隔离区，已约未做的手术除外。"""

import json
import os
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-quarantine-", dir=ROOT / "_test")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
UPLOAD_DIR = Path(TEMP_DIR.name) / "uploads"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["UPLOAD_DIR"] = str(UPLOAD_DIR)
os.environ["SESSION_SECRET"] = "tnr-quarantine-test"

from jinja2 import Environment, FileSystemLoader

from app.database import Base, SessionLocal, engine
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
from app.services.tnr_quarantine import (
    BOOKING_BLOCKED_QUARANTINE,
    BOOKING_BLOCKED_RESTART,
    QuarantineError,
    add_months,
    approve_restart,
    begin_restart,
    booking_block_message,
    customer_may_book,
    finalize_restart,
    prepare_for_booking,
    reject_restart,
    scan_idle_applications,
    submission_is_due,
    touch_rows,
)
from app.time_utils import BEIJING_TZ


assert "tnr-quarantine-" in str(engine.url), engine.url

NOW = datetime(2026, 11, 24, 0, 30, tzinfo=BEIJING_TZ)
TODAY = date(2026, 11, 24)


def utc_noon(day: date) -> datetime:
    moment = datetime(day.year, day.month, day.day, 12, 0, tzinfo=BEIJING_TZ)
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def expect_error(fn) -> QuarantineError:
    try:
        fn()
    except QuarantineError as exc:
        return exc
    raise AssertionError("expected QuarantineError")


Base.metadata.create_all(bind=engine)
db = SessionLocal()
seq = 0


def make_app(*, status: str, submitted: date | None, **extra) -> Application:
    global seq
    seq += 1
    row = Application(
        applicant_name=extra.pop("applicant_name", f"救助人{seq}"),
        phone=f"1380000{seq:04d}",
        address="深圳市龙岗区",
        cat_gender=extra.pop("cat_gender", "female"),
        cat_nickname=extra.pop("cat_nickname", f"小猫{seq}"),
        cat_color=extra.pop("cat_color", "黑白"),
        weight_estimate=extra.pop("weight_estimate", "3公斤"),
        clinic_store="大风动物医院（横岗店）",
        status=status,
        submitted_at=utc_noon(submitted) if submitted else None,
        original_submitted_at=utc_noon(submitted) if submitted else None,
        created_at=utc_noon(submitted or TODAY),
        **extra,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def make_appt(app: Application, *, category: str, status: str, day: str) -> Appointment:
    row = Appointment(
        category=category,
        status=status,
        store="大风动物医院（横岗店）",
        appointment_date=day,
        related_application_id=app.id,
        customer_name=app.applicant_name,
        phone=app.phone,
        pet_name=app.cat_nickname,
    )
    db.add(row)
    db.commit()
    return row


def add_media(app: Application, name: str, body: bytes, *, current: bool = True, pending: bool = False, kind: str | None = None) -> MediaFile:
    folder = UPLOAD_DIR / str(app.id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(body)
    media = MediaFile(
        application_id=app.id,
        kind=kind or MediaKind.application_image.value,
        stored_path=str(path),
        original_name=name,
        is_current=current,
        restart_pending=pending,
    )
    db.add(media)
    db.commit()
    db.refresh(media)
    return media


def reload(app_id: int) -> Application:
    db.expire_all()
    row = db.get(Application, app_id)
    assert row is not None
    return row


def media_is_current(media_id: int) -> bool:
    media = db.get(MediaFile, media_id)
    assert media is not None
    return bool(media.is_current)


try:
    assert add_months(date(2026, 1, 31), 3) == date(2026, 4, 30)
    assert add_months(date(2026, 8, 24), 3) == date(2026, 11, 24)
    assert add_months(date(2026, 8, 25), 3) == date(2026, 11, 25)

    jan = Application(applicant_name="月", phone="1", address="深圳", cat_gender="unknown", submitted_at=utc_noon(date(2026, 1, 31)))
    assert submission_is_due(jan, date(2026, 4, 30))
    assert not submission_is_due(jan, date(2026, 4, 29))

    due = make_app(status=ApplicationStatus.approved.value, submitted=date(2026, 8, 24), cat_nickname="到期猫")
    fresh = make_app(status=ApplicationStatus.approved.value, submitted=date(2026, 8, 25), cat_nickname="未到期")
    done = make_app(status=ApplicationStatus.surgery_completed.value, submitted=date(2026, 1, 1))
    cancelled = make_app(status=ApplicationStatus.cancelled.value, submitted=date(2026, 1, 1))
    rejected = make_app(status=ApplicationStatus.rejected.value, submitted=date(2026, 1, 1))
    draft = make_app(status=ApplicationStatus.draft.value, submitted=None)
    future = make_app(status=ApplicationStatus.approved.value, submitted=date(2026, 5, 1), cat_nickname="已约")
    today_surgery = make_app(status=ApplicationStatus.scheduled.value, submitted=date(2026, 5, 1), cat_nickname="今天手术")
    blank_date = make_app(status=ApplicationStatus.approved.value, submitted=date(2026, 5, 1), cat_nickname="日期空")
    past = make_app(status=ApplicationStatus.approved.value, submitted=date(2026, 5, 1), cat_nickname="过期预约")
    cancelled_future = make_app(status=ApplicationStatus.approved.value, submitted=date(2026, 5, 1), cat_nickname="取消的预约")
    outpatient = make_app(status=ApplicationStatus.approved.value, submitted=date(2026, 5, 1), cat_nickname="门诊不保护")

    make_appt(future, category=AppointmentCategory.tnr.value, status=AppointmentStatus.pending.value, day="2026-12-01")
    make_appt(today_surgery, category=AppointmentCategory.surgery.value, status=AppointmentStatus.confirmed.value, day="2026-11-24")
    make_appt(blank_date, category=AppointmentCategory.tnr.value, status=AppointmentStatus.arrived.value, day="")
    make_appt(past, category=AppointmentCategory.tnr.value, status=AppointmentStatus.pending.value, day="2026-11-23")
    make_appt(cancelled_future, category=AppointmentCategory.tnr.value, status=AppointmentStatus.cancelled.value, day="2026-12-20")
    make_appt(outpatient, category=AppointmentCategory.outpatient.value, status=AppointmentStatus.confirmed.value, day="2026-12-20")

    old_photo = add_media(due, "old.jpg", b"old-photo-bytes")
    surgery_before = add_media(due, "before.jpg", b"before", kind=MediaKind.surgery_before.value)
    old_color = due.cat_color
    original_submitted = due.original_submitted_at

    changed = scan_idle_applications(db, now=NOW)
    quarantined_ids = {due.id, past.id, cancelled_future.id, outpatient.id}
    assert changed == len(quarantined_ids), changed

    due = reload(due.id)
    assert due.status == ApplicationStatus.quarantined.value
    assert due.status_before_quarantine == ApplicationStatus.approved.value
    assert due.quarantined_at is not None
    assert reload(fresh.id).status == ApplicationStatus.approved.value
    assert reload(done.id).status == ApplicationStatus.surgery_completed.value
    assert reload(cancelled.id).status == ApplicationStatus.cancelled.value
    assert reload(rejected.id).status == ApplicationStatus.rejected.value
    assert reload(draft.id).status == ApplicationStatus.draft.value
    assert reload(future.id).status == ApplicationStatus.approved.value
    assert reload(today_surgery.id).status == ApplicationStatus.scheduled.value
    assert reload(blank_date.id).status == ApplicationStatus.approved.value
    assert reload(past.id).status == ApplicationStatus.quarantined.value
    assert reload(cancelled_future.id).status == ApplicationStatus.quarantined.value
    assert reload(outpatient.id).status == ApplicationStatus.quarantined.value
    assert db.query(AuditLog).filter(AuditLog.action == "auto_quarantine", AuditLog.application_id == due.id).count() == 1

    untouched = make_app(status=ApplicationStatus.approved.value, submitted=date(2026, 8, 1), cat_nickname="只扫眼前")
    touched = make_app(status=ApplicationStatus.pending_manual.value, submitted=date(2026, 8, 1), cat_nickname="眼前这份")
    assert touch_rows(db, [touched], now=NOW) == 1
    assert reload(touched.id).status == ApplicationStatus.quarantined.value
    assert reload(untouched.id).status == ApplicationStatus.approved.value

    booking = make_app(status=ApplicationStatus.approved.value, submitted=date(2026, 8, 24), cat_nickname="预约拦截")
    message = prepare_for_booking(db, booking, now=NOW)
    assert message == BOOKING_BLOCKED_QUARANTINE
    booking = reload(booking.id)
    assert booking.status == ApplicationStatus.quarantined.value
    assert customer_may_book(booking) is False
    assert prepare_for_booking(db, reload(future.id), now=NOW) is None
    assert customer_may_book(reload(future.id)) is True

    plain = reload(past.id)
    expect_error(lambda: approve_restart(db, plain, "staff"))
    expect_error(lambda: begin_restart(db, reload(fresh.id), {
        "cat_color": "三花",
        "weight_estimate": "4公斤",
    }))

    begin_restart(db, due, {
        "cat_nickname": "不该改名",
        "cat_color": "三花",
        "weight_estimate": "4公斤",
    })
    only_one = add_media(due, "only-one.jpg", b"one", current=False, pending=True)
    err = expect_error(lambda: finalize_restart(db, reload(due.id)))
    assert "2" in str(err)
    due = reload(due.id)
    assert due.status == ApplicationStatus.quarantined.value
    assert due.cat_color == old_color
    assert media_is_current(old_photo.id)
    db.delete(db.get(MediaFile, only_one.id))
    db.commit()

    add_media(due, "new-a.jpg", b"new-a", current=False, pending=True)
    add_media(due, "new-b.jpg", b"new-b", current=False, pending=True)
    finalize_restart(db, reload(due.id))
    due = reload(due.id)
    assert due.status == ApplicationStatus.quarantine_restart_pending.value
    assert due.cat_nickname == "到期猫"
    assert due.cat_color == old_color == "黑白"
    assert due.weight_estimate == "3公斤"
    assert due.restart_count == 1
    assert booking_block_message(due) == BOOKING_BLOCKED_RESTART
    assert customer_may_book(due) is False
    assert prepare_for_booking(db, due, now=NOW) == BOOKING_BLOCKED_RESTART

    old = db.get(MediaFile, old_photo.id)
    assert old is not None and not old.is_current
    assert Path(old.stored_path).read_bytes() == b"old-photo-bytes"
    before = db.get(MediaFile, surgery_before.id)
    assert before is not None and before.is_current
    current_images = [
        media for media in due.media
        if media.kind == MediaKind.application_image.value and media.is_current and not media.restart_pending
    ]
    assert len(current_images) == 2
    assert {Path(media.stored_path).read_bytes() for media in current_images} == {b"new-a", b"new-b"}
    revisions = db.query(ApplicationRevision).filter(ApplicationRevision.application_id == due.id).all()
    assert len(revisions) == 1
    snapshot = json.loads(revisions[0].snapshot_json)
    assert snapshot["cat_color"] == "黑白"
    assert snapshot["weight_estimate"] == "3公斤"
    assert old_photo.id in snapshot["media_ids"]
    assert due.latest_restart_snapshot()["weight_estimate"] == "3公斤"

    approve_restart(db, due, "前台")
    due = reload(due.id)
    assert due.status == ApplicationStatus.approved.value
    assert due.original_submitted_at == original_submitted
    assert due.submitted_at is not None and due.submitted_at > original_submitted
    assert due.cat_color == "黑白"
    assert due.weight_estimate == "3公斤"
    assert booking_block_message(due) is None
    assert customer_may_book(due) is True
    assert prepare_for_booking(db, due, now=NOW) is None
    assert db.query(AuditLog).filter(AuditLog.action == "quarantine_restart_approve", AuditLog.application_id == due.id).count() == 1

    due.submitted_at = utc_noon(date(2026, 8, 24))
    db.commit()
    assert scan_idle_applications(db, now=NOW) >= 1
    due = reload(due.id)
    assert due.status == ApplicationStatus.quarantined.value
    assert due.cat_color == "黑白"
    assert due.weight_estimate == "3公斤"
    assert not db.get(MediaFile, old_photo.id).is_current

    reject_app = reload(cancelled_future.id)
    old_reject_photo = add_media(reject_app, "reject-old.jpg", b"reject-old")
    begin_restart(db, reject_app, {
        "cat_color": "橘白",
        "weight_estimate": "偏瘦",
    })
    add_media(reject_app, "reject-new-a.jpg", b"ra", current=False, pending=True)
    add_media(reject_app, "reject-new-b.jpg", b"rb", current=False, pending=True)
    finalize_restart(db, reload(reject_app.id))
    expect_error(lambda: reject_restart(db, reload(reject_app.id), "  ", "前台"))
    reject_restart(db, reload(reject_app.id), "近照看不清猫咪", "前台")
    reject_app = reload(reject_app.id)
    assert reject_app.status == ApplicationStatus.quarantined.value
    assert reject_app.restart_reject_reason == "近照看不清猫咪"
    assert reject_app.cat_nickname == "取消的预约"
    assert reject_app.cat_color == "黑白"
    assert reject_app.weight_estimate == "3公斤"
    assert not db.get(MediaFile, old_reject_photo.id).is_current
    assert Path(db.get(MediaFile, old_reject_photo.id).stored_path).read_bytes() == b"reject-old"
    assert booking_block_message(reject_app) == BOOKING_BLOCKED_QUARANTINE
    assert customer_may_book(reject_app) is False
    assert db.query(AuditLog).filter(AuditLog.action == "quarantine_restart_reject", AuditLog.application_id == reject_app.id).count() == 1

    env = Environment(loader=FileSystemLoader(str(ROOT / "templates")))
    for name in (
        "includes/admin_tnr_body.html",
        "m_uk/tnr_list.html",
        "m_uk/tnr_detail.html",
        "uk/admin_tnr.html",
    ):
        env.get_template(name)

    from app.database import _try_sqlite_migrations
    _try_sqlite_migrations()
    print("PASS")
finally:
    db.close()
    TEMP_DIR.cleanup()
