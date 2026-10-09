"""Cancelled TNR records are read-only; only a superadmin may correct proven legacy data."""

import hashlib
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-terminal-guard-", dir=ROOT / "_test")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
UPLOAD_DIR = Path(TEMP_DIR.name) / "uploads"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["UPLOAD_DIR"] = str(UPLOAD_DIR)
os.environ["SESSION_SECRET"] = "tnr-terminal-guard-test"

from fastapi import HTTPException

from app.database import Base, SessionLocal, engine
from app.main import (
    api_staff_miniapp_tnr_upload,
    correct_surgery_completed,
    upload_surgery,
)
from app.models import AdminUser, Application, ApplicationStatus, AuditLog, MediaFile, MediaKind


class DummyRequest:
    def __init__(self, *, token: str = ""):
        self.session = {
            "admin": True,
            "admin_role": "superadmin",
            "admin_username": "tnr_guard_admin",
            "csrf_token": "csrf-test",
        }
        self.headers = {"authorization": f"Bearer {token}"} if token else {}
        self.client = None


def run_without_io(awaitable):
    """Drive these handlers directly; their guarded paths perform no async I/O."""
    try:
        awaitable.send(None)
    except StopIteration as done:
        return done.value
    raise AssertionError("handler unexpectedly yielded async I/O")


def expect_http(status_code: int, awaitable) -> None:
    try:
        run_without_io(awaitable)
    except HTTPException as exc:
        assert exc.status_code == status_code, exc.detail
    else:
        raise AssertionError(f"expected HTTP {status_code}")


Base.metadata.create_all(bind=engine)
db = SessionLocal()
try:
    token = "tnr-terminal-test-token"
    admin = AdminUser(
        username="tnr_guard_admin",
        password_hash="unused",
        role="superadmin",
        store="横岗店",
        is_active=True,
        miniapp_token_hash=hashlib.sha256(token.encode()).hexdigest(),
        miniapp_token_created_at=datetime.utcnow(),
    )
    complete = Application(
        applicant_name="历史完成申请",
        phone="13800000001",
        address="深圳市",
        cat_gender="female",
        clinic_store="大风动物医院（横岗店）",
        status=ApplicationStatus.cancelled.value,
    )
    incomplete = Application(
        applicant_name="历史取消申请",
        phone="13800000002",
        address="深圳市",
        cat_gender="male",
        clinic_store="大风动物医院（横岗店）",
        status=ApplicationStatus.cancelled.value,
    )
    db.add_all([admin, complete, incomplete])
    db.flush()
    media_dir = UPLOAD_DIR / str(complete.id)
    media_dir.mkdir(parents=True, exist_ok=True)
    before = media_dir / "before.jpg"
    after = media_dir / "after.jpg"
    before.write_bytes(b"before")
    after.write_bytes(b"after")
    db.add_all([
        MediaFile(application_id=complete.id, kind=MediaKind.surgery_before.value, stored_path=str(before)),
        MediaFile(application_id=complete.id, kind=MediaKind.surgery_after.value, stored_path=str(after)),
    ])
    db.commit()
    complete_id = complete.id
    incomplete_id = incomplete.id
finally:
    db.close()

try:
    db = SessionLocal()
    try:
        expect_http(409, upload_surgery(
            complete_id,
            DummyRequest(),
            db=db,
            csrf_token="csrf-test",
        ))
        expect_http(409, api_staff_miniapp_tnr_upload(
            complete_id,
            DummyRequest(token="tnr-terminal-test-token"),
            kind="after",
            media_type="image",
            file=None,
            db=db,
        ))
        expect_http(409, correct_surgery_completed(
            incomplete_id,
            DummyRequest(),
            db=db,
            csrf_token="csrf-test",
        ))

        response = run_without_io(correct_surgery_completed(
            complete_id,
            DummyRequest(),
            db=db,
            csrf_token="csrf-test",
        ))
        assert response.status_code == 303
    finally:
        db.close()

    db = SessionLocal()
    try:
        row = db.get(Application, complete_id)
        assert row.status == ApplicationStatus.surgery_completed.value
        assert row.staff_cat_verified is True
        assert db.query(AuditLog).filter_by(
            application_id=complete_id,
            action="correct_surgery_completed",
        ).count() == 1
    finally:
        db.close()
finally:
    engine.dispose()
    TEMP_DIR.cleanup()

print("PASS: TNR terminal status guards and correction flow")
