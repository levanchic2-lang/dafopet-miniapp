"""体型按体重分档，猫洗护时长，以及 AI 查空档不再在缺体重时按 60 分钟兜底。"""

import os
import re
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="ai-cs-grooming-duration-")
os.environ["DATABASE_URL"] = f"sqlite:///{(Path(TEMP_DIR.name) / 'test.db').as_posix()}"
os.environ["UPLOAD_DIR"] = str(Path(TEMP_DIR.name) / "uploads")
os.environ["SESSION_SECRET"] = "ai-cs-grooming-duration"
os.environ["SESSION_HTTPS_ONLY"] = "0"
os.environ["AI_CS_API_KEY"] = "test-ai-cs-key"

from fastapi.testclient import TestClient
from passlib.hash import bcrypt

from app.database import Base, SessionLocal, engine
from app.main import (
    BEAUTY_CAT_SIZE_OPTIONS,
    BEAUTY_DOG_SIZE_OPTIONS,
    _calc_beauty_duration,
    app,
    pet_size_from_weight,
    resolve_beauty_pet_size,
)
from app.models import AdminUser, Customer, Pet, WeightRecord
from app.services.ai_cs_assistant import PET_FILE_RULE, tool_query_slots
from app.services.ai_cs_booking import AiCsError, query_slots
from app.time_utils import beijing_today


@asynccontextmanager
async def _test_lifespan(_app):
    yield


app.router.lifespan_context = _test_lifespan
Base.metadata.create_all(bind=engine)

KEY = {"X-AI-CS-Key": "test-ai-cs-key"}
HG = "大风动物医院（横岗店）"
DAY = (beijing_today() + timedelta(days=2)).isoformat()
client = TestClient(app)


def test_weight_boundaries():
    cats = [
        (2.9, "小型猫"),
        (3, "中型猫"),
        (4.9, "中型猫"),
        (5, "大型猫"),
        ("5.0", "大型猫"),
    ]
    for weight, token in cats:
        label = pet_size_from_weight("cat", weight)
        assert label.startswith(token), (weight, label)
        assert label in BEAUTY_CAT_SIZE_OPTIONS
    assert pet_size_from_weight("猫", 5).startswith("大型猫")
    assert pet_size_from_weight("", 5, service_name="猫洗护").startswith("大型猫")

    dogs = [
        (2.9, "微小型犬"),
        (3, "小型犬"),
        (11.9, "小型犬"),
        (12, "中型犬"),
        (17.9, "中型犬"),
        (18, "中大型犬"),
        (27.9, "中大型犬"),
        (28, "大型犬"),
    ]
    for weight, token in dogs:
        label = pet_size_from_weight("dog", weight)
        assert label.startswith(token), (weight, label)
        assert label in BEAUTY_DOG_SIZE_OPTIONS
    assert pet_size_from_weight("狗", 28).startswith("大型犬")
    assert pet_size_from_weight("cat", None) == ""
    assert pet_size_from_weight("", 5) == ""
    assert pet_size_from_weight("rabbit", 5) == ""


def test_cat_durations_and_old_labels():
    assert _calc_beauty_duration("猫洗护", "小型猫（不足 3kg）", "短毛") == 90
    assert _calc_beauty_duration("猫洗护", "小型猫（不足 3kg）", "长毛") == 90
    assert _calc_beauty_duration("猫洗护", "中型猫（3kg 至不足 5kg）", "短毛") == 120
    assert _calc_beauty_duration("猫洗护", "中型猫（3kg 至不足 5kg）", "长毛") == 120
    assert _calc_beauty_duration("猫洗护", "大型猫（5kg 及以上）", "短毛") == 120
    assert _calc_beauty_duration("猫洗护", "大型猫（5kg 及以上）", "长毛") == 120
    # 历史文案仍按档名计时，不因括号里的旧体重被换算掉。
    assert _calc_beauty_duration("猫洗护", "小型猫（2.5kg 以下）", "短毛") == 90
    assert _calc_beauty_duration("猫洗护", "大型猫（4kg 以上）", "长毛") == 120
    assert resolve_beauty_pet_size("大型猫（4kg 以上）", species="cat", weight_kg=2.9) == "大型猫（4kg 以上）"
    assert _calc_beauty_duration("猫洗护", "", "短毛", weight_kg=5) == 120
    assert _calc_beauty_duration("猫洗护", "", "长毛", weight_kg=4.9) == 120
    assert _calc_beauty_duration("猫洗护", "", "短毛", weight_kg=2.9) == 90
    assert _calc_beauty_duration("猫造型", "小型猫（2.5kg 以下）", "长毛") == 120
    assert _calc_beauty_duration("猫造型", "中型猫（2.5–4kg）", "短毛") == 120
    assert _calc_beauty_duration("猫造型", "大型猫（4kg 以上）", "长毛") == 150
    assert _calc_beauty_duration("犬洗护", "微小型犬（4kg 以下）", "长毛") == 30
    assert _calc_beauty_duration("犬洗护", "小型犬（4–10kg）", "长毛") == 60
    assert _calc_beauty_duration("犬洗护", "中型犬（10–15kg）", "长毛") == 90
    assert _calc_beauty_duration("犬洗护", "中大型犬（15–25kg）", "短毛") == 90
    assert _calc_beauty_duration("犬洗护", "大型犬（25kg 以上）", "长毛") == 150
    assert _calc_beauty_duration("犬造型", "大型犬（28kg 及以上）", "短毛") == 210
    # 员工端：体型和体重都没有时仍是原来的 60 分钟。
    assert _calc_beauty_duration("猫洗护", "", "短毛") == 60


def _beauty_slots(**params):
    query = {
        "store": "横岗店",
        "date": DAY,
        "service_name": "猫洗护",
        "coat_length": "短毛",
    }
    query.update(params)
    return client.get("/api/ai-cs/slots/beauty", headers=KEY, params=query)


def test_five_kg_cat_cannot_start_after_20():
    response = _beauty_slots(weight_kg="5")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["duration_minutes"] == 120
    assert body["pet_size"].startswith("大型猫")
    assert "20:00" in body["slots"]
    assert "20:30" not in body["slots"]
    assert "21:00" not in body["slots"]
    ends = {item["start"]: item["end"] for item in body["slot_details"]}
    assert ends["20:00"] == "22:00"
    assert set(ends) == set(body["slots"])
    for start, end in ends.items():
        assert end <= "22:00", (start, end)
    long_hair = _beauty_slots(weight_kg="5", coat_length="长毛")
    assert long_hair.status_code == 200
    assert long_hair.json()["duration_minutes"] == 120
    assert "20:30" not in long_hair.json()["slots"]
    # 90 分钟的小型猫可以 20:30 开始，并在 22:00 结束。
    small = _beauty_slots(weight_kg="2.9")
    assert small.status_code == 200, small.text
    assert small.json()["duration_minutes"] == 90
    assert small.json()["pet_size"].startswith("小型猫")
    assert "20:30" in small.json()["slots"]
    assert "21:00" not in small.json()["slots"]
    small_ends = {item["start"]: item["end"] for item in small.json()["slot_details"]}
    assert small_ends["20:30"] == "22:00"


def test_missing_size_and_weight_asks_for_weight():
    missing = _beauty_slots()
    assert missing.status_code == 400
    body = missing.json()
    assert body["error_code"] == "pet_weight_required"
    assert "体重" in body["message"]
    assert "60" in body["message"]
    kept = _beauty_slots(pet_size="小型猫（2.5kg 以下）", weight_kg="6")
    assert kept.status_code == 200, kept.text
    assert kept.json()["pet_size"] == "小型猫（2.5kg 以下）"
    assert kept.json()["duration_minutes"] == 90
    fallback = query_slots(
        SessionLocal(),
        store="横岗店",
        day=DAY,
        kind="beauty",
        service_name="猫洗护",
        coat_length="短毛",
        require_beauty_size=False,
    )
    assert fallback["duration_minutes"] == 60
    assert "21:00" in fallback["slots"]
    try:
        query_slots(
            SessionLocal(),
            store="横岗店",
            day=DAY,
            kind="beauty",
            service_name="猫洗护",
        )
        raise AssertionError("缺体重时应拒绝")
    except AiCsError as exc:
        assert exc.code == "pet_weight_required"


def test_file_weight_and_tool_end_times():
    db = SessionLocal()
    try:
        owner = Customer(name="猫主人", phone="13800004444", wechat_openid="cs-groom")
        db.add(owner)
        db.flush()
        pet = Pet(customer_id=owner.id, name="五公斤", species="cat", gender="female", store="横岗店")
        db.add(pet)
        db.flush()
        db.add(WeightRecord(pet_id=pet.id, record_date=beijing_today().isoformat(), weight_kg=5))
        db.commit()
        pet_id = pet.id
    finally:
        db.close()

    response = _beauty_slots(pet_id=pet_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["duration_minutes"] == 120
    assert body["pet_size"].startswith("大型猫")
    assert "20:30" not in body["slots"]
    assert "21:00" not in body["slots"]

    db = SessionLocal()
    try:
        looked = tool_query_slots(db, "cs-groom", {
            "store": "横岗店",
            "date": DAY,
            "kind": "beauty",
            "service_name": "猫洗护",
            "pet_id": pet_id,
            "coat_length": "短毛",
        })
    finally:
        db.close()
    assert looked["ok"] is True
    assert looked["duration_minutes"] == 120
    assert looked["pet_size"].startswith("大型猫")
    assert looked["slots"]
    assert all(item["start"] and item["end"] for item in looked["slots"])
    by_start = {item["start"]: item["end"] for item in looked["slots"]}
    assert "20:30" not in by_start and "21:00" not in by_start
    assert by_start["20:00"] == "22:00"
    assert "end" in looked["offer_rule"]

    db = SessionLocal()
    try:
        empty = tool_query_slots(db, "cs-groom", {
            "store": "横岗店",
            "date": DAY,
            "kind": "beauty",
            "service_name": "猫洗护",
        })
    finally:
        db.close()
    assert empty["ok"] is False
    assert empty["error_code"] == "pet_weight_required"
    assert "体重" in empty["message"]


def test_staff_slot_fallback_and_labels():
    derived = client.get(
        "/api/appointments/beauty-slots",
        params={"service": "猫洗护", "coat_length": "短毛", "weight_kg": "5", "date": DAY, "store": HG},
    )
    assert derived.status_code == 200, derived.text
    assert derived.json()["duration_minutes"] == 120
    assert "21:00" not in derived.json()["available_slots"]
    silent = client.get(
        "/api/appointments/beauty-slots",
        params={"service": "猫洗护", "coat_length": "短毛", "date": DAY, "store": HG},
    )
    assert silent.status_code == 200
    assert silent.json()["duration_minutes"] == 60

    db = SessionLocal()
    try:
        db.add(AdminUser(
            username="admin", password_hash=bcrypt.hash("test123456"),
            role="superadmin", store="", is_active=True,
        ))
        db.commit()
    finally:
        db.close()
    page = client.get("/admin/login")
    token = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert token
    logged = client.post(
        "/admin/login",
        data={"username": "admin", "password": "test123456", "csrf_token": token.group(1)},
        follow_redirects=False,
    )
    assert logged.status_code == 303
    for path in ("/admin/appointments", "/admin/appointments/create", "/m/appointments/new"):
        html = client.get(path).text
        assert "不足 3kg" in html, path
        assert "5kg 及以上" in html, path
        assert "28kg 及以上" in html, path
        assert "4kg 以上" not in html, path
        assert "2.5kg" not in html, path
    rules = (ROOT / "app/ai_cs_knowledge/02-booking-rules.md").read_text(encoding="utf-8")
    assert "正好 5kg 的猫是大型猫" in rules
    assert "猫洗护 | 小型猫 | 90 | 90" in rules
    assert "不要自己用开始时间加时长" in rules
    assert "2.5kg" not in PET_FILE_RULE
    assert "正好 5kg 是大型猫" in PET_FILE_RULE
    mini = (ROOT / "miniapp/pages/appointment/index.js").read_text(encoding="utf-8")
    assert "大型猫（5kg 及以上）" in mini
    assert "4kg 以上" not in mini
    assert "isWash ? 90 : 120" in mini


if __name__ == "__main__":
    test_weight_boundaries()
    test_cat_durations_and_old_labels()
    test_five_kg_cat_cannot_start_after_20()
    test_missing_size_and_weight_asks_for_weight()
    test_file_weight_and_tool_end_times()
    test_staff_slot_fallback_and_labels()
    print("ai cs grooming duration tests passed")
