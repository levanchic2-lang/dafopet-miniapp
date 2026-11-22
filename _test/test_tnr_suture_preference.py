"""新申请即使传入 intradermal 也存皮外；历史皮内记录和后台展示保持原样。"""

import os
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-suture-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["UPLOAD_DIR"] = str(Path(TEMP_DIR.name) / "uploads")
os.environ["SESSION_SECRET"] = "tnr-suture-test"
os.environ["SESSION_HTTPS_ONLY"] = "0"
os.environ["OPENAI_API_KEY"] = ""
os.environ["WECHAT_TMPL_PENDING_MANUAL"] = ""
os.environ["NOTIFY_WEBHOOK_URL"] = ""
os.environ["SMS_GATEWAY_URL"] = ""
os.environ["WECOM_CORP_ID"] = ""
os.environ["WECOM_SECRET"] = ""

from fastapi.testclient import TestClient
from jinja2 import Environment

import app.main as main_module
import app.services.wecom_notify as wecom_notify
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Application


@asynccontextmanager
async def _test_lifespan(_app):
    yield


async def _quiet_review(image_paths, video_paths=None):
    return {
        "evidence_quality_pass": False,
        "same_cat": None,
        "distinct_view_count": 0,
        "is_likely_stray": None,
        "confidence": None,
        "suggested_next_step": "manual_review",
        "anti_fraud_flags": [],
        "reasons": [],
        "caveats": ["test"],
    }


app.router.lifespan_context = _test_lifespan
main_module.review_application_media = _quiet_review
wecom_notify.notify_tnr_pending_manual = lambda *args, **kwargs: None
Base.metadata.create_all(bind=engine)
engine.echo = False

HISTORICAL_LABEL = (
    "{{ '皮内可吸收缝合（通常无需拆线）' if a.suture_preference == 'intradermal' "
    "else '皮外缝合（需返院复查拆线）' }}"
)


def _form(phone: str, nickname: str, gender: str = "female", suture: str | None = "intradermal") -> dict:
    data = {
        "applicant_name": "测试申请人",
        "phone": phone,
        "address": "广东省深圳市罗湖区桂园街道测试路1号",
        "clinic_store": "大风动物医院（东环店）",
        "post_surgery_plan": "原地放归",
        "id_number": "110101199001011234",
        "cat_nickname": nickname,
        "cat_gender": gender,
        "age_estimate": "2020-05",
        "health_note": "测试用流浪说明，不作为缝合选项。",
        "agree_ear_tip": "true",
        "agree_no_pet_fraud": "true",
    }
    if suture is not None:
        data["suture_preference"] = suture
    return data


def _stored(nickname: str) -> str:
    db = SessionLocal()
    try:
        row = db.query(Application).filter(Application.cat_nickname == nickname).one()
        return row.suture_preference
    finally:
        db.close()


def _must_contain(path: Path, *needles: str) -> None:
    text = path.read_text(encoding="utf-8")
    for needle in needles:
        if needle not in text:
            raise AssertionError(f"{path.name} 缺少：{needle}")


def _must_not_contain(path: Path, *needles: str) -> None:
    text = path.read_text(encoding="utf-8")
    for needle in needles:
        if needle in text:
            raise AssertionError(f"{path.name} 仍包含：{needle}")


db = SessionLocal()
historical = Application(
    applicant_name="历史申请人",
    phone="13800001099",
    address="广东省深圳市罗湖区桂园街道历史路1号",
    clinic_store="大风动物医院（东环店）",
    cat_nickname="历史皮内猫",
    cat_gender="female",
    age_estimate="2019-01",
    health_note="已提交的历史申请",
    post_surgery_plan="原地放归",
    suture_preference="intradermal",
    status="approved",
)
db.add(historical)
db.commit()
historical_id = historical.id
db.close()

client = TestClient(app)

created = client.post("/api/apply/create", data=_form("13800001001", "新母猫", "female", "intradermal"))
assert created.status_code == 200, created.text
assert _stored("新母猫") == "external"

created_male = client.post("/api/apply/create", data=_form("13800001002", "新公猫", "male", "intradermal"))
assert created_male.status_code == 200, created_male.text
assert _stored("新公猫") == "external"

created_blank = client.post("/api/apply/create", data=_form("13800001003", "空值猫", "female", ""))
assert created_blank.status_code == 200, created_blank.text
assert _stored("空值猫") == "external"

created_default = client.post("/api/apply/create", data=_form("13800001004", "缺字段猫", "female", None))
assert created_default.status_code == 200, created_default.text
assert _stored("缺字段猫") == "external"

images = [
    ("images", ("cat-a.jpg", b"\xff\xd8\xff\xd9", "image/jpeg")),
    ("images", ("cat-b.jpg", b"\xff\xd8\xff\xd9", "image/jpeg")),
]
legacy = client.post(
    "/api/apply",
    data=_form("13800001005", "旧接口猫", "female", "intradermal"),
    files=images,
)
assert legacy.status_code == 200, legacy.text
assert _stored("旧接口猫") == "external"

db = SessionLocal()
try:
    kept = db.get(Application, historical_id)
    assert kept is not None
    assert kept.suture_preference == "intradermal"
    assert kept.status == "approved"
    still_intradermal = (
        db.query(Application)
        .filter(Application.suture_preference == "intradermal")
        .all()
    )
    assert [row.id for row in still_intradermal] == [historical_id]
finally:
    db.close()

client.close()

apply_js = ROOT / "miniapp/pages/apply/index.js"
apply_wxml = ROOT / "miniapp/pages/apply/index.wxml"
_must_not_contain(apply_js, "intradermal", "皮内", "sutureOptions", "onSutureChange")
_must_contain(apply_js, 'suture_preference: "external"')
_must_not_contain(apply_wxml, "intradermal", "皮内", "无需拆线", "sutureOptions", "onSutureChange", "母猫伤口缝合方式")
_must_contain(apply_wxml, "母猫伤口由本院采用皮外缝合")

info = ROOT / "miniapp/pages/tnr-info/index.js"
_must_contain(info, "本院TNR默认采用皮外缝合", "皮内可吸收缝合不再作为申请选项")
_must_not_contain(info, "通常无需拆线", "未选择缝合方式", "美容缝合", "救助人可选")

content = ROOT / "scripts/seed_public_content_v1.py"
_must_contain(content, "本院TNR默认采用皮外缝合", "皮内可吸收缝合不再作为申请选项")
_must_not_contain(content, "若救助人未作选择", "通常无需拆线", "由医生与主人沟通后确定", "救助人可选")

knowledge = ROOT / "scripts/seed_public_knowledge_v1.py"
_must_contain(knowledge, "可吸收缝线也不等于伤口不需要保护")
_must_not_contain(knowledge, "皮内可吸收缝合", "通常无需拆线", "救助人可选")

schema = ROOT / "app/database.py"
_must_contain(schema, "ADD COLUMN suture_preference")
_must_not_contain(schema, "SET suture_preference", "intradermal")

env = Environment()
for rel in ("templates/includes/admin_tnr_body.html", "templates/admin.html"):
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert 'name="suture_preference"' not in text
    assert "intradermal" in text
    assert HISTORICAL_LABEL in text
    shown = env.from_string(HISTORICAL_LABEL).render(a=type("Row", (), {"suture_preference": "intradermal"})())
    assert shown == "皮内可吸收缝合（通常无需拆线）"
    plain = env.from_string(HISTORICAL_LABEL).render(a=type("Row", (), {"suture_preference": "external"})())
    assert plain == "皮外缝合（需返院复查拆线）"

TEMP_DIR.cleanup()
print("tnr suture preference test passed")
