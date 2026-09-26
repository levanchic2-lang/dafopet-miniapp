"""Regression coverage for application photos becoming TNR pre-operation media."""

import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-surgery-media-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
UPLOAD_DIR = Path(TEMP_DIR.name) / "uploads"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["UPLOAD_DIR"] = str(UPLOAD_DIR)
os.environ["SESSION_SECRET"] = "tnr-surgery-media-test"

from app.database import Base, SessionLocal, engine
from app.main import (
    _application_has_surgery_before_and_after,
    _ensure_application_images_as_surgery_before,
)
from app.models import Application, ApplicationStatus, MediaFile, MediaKind


Base.metadata.create_all(bind=engine)
with SessionLocal() as db:
    application = Application(
        applicant_name="测试申请人",
        phone="13800000000",
        address="深圳市",
        cat_gender="unknown",
        status=ApplicationStatus.arrived_verified.value,
    )
    db.add(application)
    db.flush()

    source_dir = UPLOAD_DIR / str(application.id)
    source_dir.mkdir(parents=True, exist_ok=True)
    source = source_dir / "app_img_original.jpg"
    source.write_bytes(b"application-photo")
    after = source_dir / "surg_after.jpg"
    after.write_bytes(b"post-operation-photo")
    db.add_all([
        MediaFile(
            application_id=application.id,
            kind=MediaKind.application_image.value,
            stored_path=str(source),
            original_name="original.jpg",
        ),
        MediaFile(
            application_id=application.id,
            kind=MediaKind.surgery_after.value,
            stored_path=str(after),
            original_name="after.jpg",
        ),
    ])
    db.commit()

    assert not _application_has_surgery_before_and_after(db, application.id)
    assert _ensure_application_images_as_surgery_before(db, application.id) == 1
    db.commit()
    assert _application_has_surgery_before_and_after(db, application.id)

    before = db.query(MediaFile).filter_by(
        application_id=application.id,
        kind=MediaKind.surgery_before.value,
    ).one()
    assert Path(before.stored_path).read_bytes() == b"application-photo"
    assert Path(before.stored_path) != source
    assert _ensure_application_images_as_surgery_before(db, application.id) == 0

TEMP_DIR.cleanup()
print("TNR surgery media regression test passed")
