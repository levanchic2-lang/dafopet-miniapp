"""两次上传手术资料应追加，不能覆盖已经落库的术前/术后文件。"""

import asyncio
import os
import sys
import tempfile
from io import BytesIO
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-surgery-append-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
UPLOAD_DIR = Path(TEMP_DIR.name) / "uploads"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["UPLOAD_DIR"] = str(UPLOAD_DIR)
os.environ["SESSION_SECRET"] = "tnr-surgery-append-test"

from starlette.datastructures import Headers, UploadFile

from app.database import Base, SessionLocal, engine
from app.main import upload_surgery
from app.models import Application, ApplicationStatus, MediaFile, MediaKind


class DummyRequest:
    def __init__(self):
        self.session = {
            "admin": True,
            "admin_role": "superadmin",
            "admin_username": "append_tester",
            "csrf_token": "csrf-test",
        }
        self.headers = {"user-agent": "test", "referer": ""}
        self.client = None


def upload(name: str, payload: bytes) -> UploadFile:
    return UploadFile(
        file=BytesIO(payload),
        filename=name,
        headers=Headers({"content-type": "image/jpeg"}),
    )


Base.metadata.create_all(bind=engine)
db = SessionLocal()
application = Application(
    applicant_name="追加测试",
    phone="13800000009",
    address="深圳市",
    cat_gender="unknown",
    status=ApplicationStatus.arrived_verified.value,
)
db.add(application)
db.commit()
app_id = application.id
request = DummyRequest()


async def main():
    first = await upload_surgery(
        app_id,
        request,
        db=db,
        csrf_token="csrf-test",
        before_images=[upload("first.jpg", b"first-image")],
        after_images=None,
        before_videos=None,
        after_videos=None,
    )
    assert first.status_code == 303
    second = await upload_surgery(
        app_id,
        request,
        db=db,
        csrf_token="csrf-test",
        before_images=[upload("second.jpg", b"second-image")],
        after_images=None,
        before_videos=None,
        after_videos=None,
    )
    assert second.status_code == 303
    after = await upload_surgery(
        app_id,
        request,
        db=db,
        csrf_token="csrf-test",
        before_images=None,
        after_images=[upload("after.jpg", b"after-image")],
        before_videos=None,
        after_videos=[upload("after.mp4", b"after-video")],
    )
    assert after.status_code == 303


asyncio.run(main())
db.expire_all()

before = (
    db.query(MediaFile)
    .filter_by(application_id=app_id, kind=MediaKind.surgery_before.value)
    .order_by(MediaFile.id)
    .all()
)
after_rows = (
    db.query(MediaFile)
    .filter_by(application_id=app_id, kind=MediaKind.surgery_after.value)
    .order_by(MediaFile.id)
    .all()
)
assert [row.original_name for row in before] == ["first.jpg", "second.jpg"]
assert [row.original_name for row in after_rows] == ["after.jpg", "after.mp4"]
assert Path(before[0].stored_path).read_bytes() == b"first-image"
assert Path(before[1].stored_path).read_bytes() == b"second-image"
assert before[0].stored_path != before[1].stored_path

body = (ROOT / "templates/includes/admin_tnr_body.html").read_text(encoding="utf-8")
page = (ROOT / "templates/uk/admin_tnr.html").read_text(encoding="utf-8")
script = (ROOT / "static/tnr_surgery_drop.js").read_text(encoding="utf-8")
assert body.count('class="tnr-drop"') == 8
for name in ("before_images", "after_images", "before_videos", "after_videos"):
    assert body.count(f'name="{name}"') == 2
assert "tnr_surgery_drop.js" in page
assert "DataTransfer" in script
assert "_tnrPending" in script
assert "再次拖入会追加" in body

db.close()
engine.dispose()
TEMP_DIR.cleanup()
print("TNR surgery upload append test passed")
