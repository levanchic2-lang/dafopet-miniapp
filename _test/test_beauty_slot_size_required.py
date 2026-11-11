"""员工端美容查时段和改期不再在缺体型、缺体重时按 60 分钟占档。"""

import os
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="beauty-slot-size-required-")
os.environ["DATABASE_URL"] = f"sqlite:///{(Path(TEMP_DIR.name) / 'test.db').as_posix()}"
os.environ["UPLOAD_DIR"] = str(Path(TEMP_DIR.name) / "uploads")
os.environ["SESSION_SECRET"] = "beauty-slot-size-required"
os.environ["SESSION_HTTPS_ONLY"] = "0"
os.environ["AI_CS_API_KEY"] = "test-ai-cs-key"

from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import BEAUTY_SIZE_REQUIRED_MESSAGE, app
from app.models import Appointment, AppointmentCategory, AppointmentStatus, Customer, Pet, WeightRecord
from app.services.ai_cs_assistant import tool_stage_reschedule
from app.time_utils import beijing_today


@asynccontextmanager
async def _test_lifespan(_app):
    yield


app.router.lifespan_context = _test_lifespan
Base.metadata.create_all(bind=engine)

KEY = {"X-AI-CS-Key": "test-ai-cs-key"}
HG = "大风动物医院（横岗店）"
DAY = (beijing_today() + timedelta(days=3)).isoformat()
OTHER_DAY = (beijing_today() + timedelta(days=4)).isoformat()
client = TestClient(app)


def _customer(openid: str, phone: str, pet_name: str, species: str = "cat"):
    db = SessionLocal()
    try:
        owner = Customer(name="主人", phone=phone, wechat_openid=openid)
        db.add(owner)
        db.flush()
        pet = Pet(customer_id=owner.id, name=pet_name, species=species, gender="female", store="横岗店")
        db.add(pet)
        db.commit()
        return owner.id, pet.id
    finally:
        db.close()


def _appointment(customer_id, pet_id, *, service, when, clock, duration, pet_size="", coat="短毛"):
    db = SessionLocal()
    try:
        row = Appointment(
            category=AppointmentCategory.beauty.value,
            status=AppointmentStatus.confirmed.value,
            service_name=service,
            customer_name="主人",
            phone="13800007777",
            pet_name="宠物",
            pet_gender="female",
            store=HG,
            appointment_date=when,
            appointment_time=clock,
            duration_minutes=duration,
            source="admin",
            customer_id=customer_id,
            pet_id=pet_id,
            pet_size=pet_size or None,
            coat_length=coat or None,
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _duration_of(appointment_id) -> int:
    db = SessionLocal()
    try:
        row = db.get(Appointment, appointment_id)
        return int(row.duration_minutes or 0)
    finally:
        db.close()


def test_beauty_slots_rejects_missing_size_and_weight():
    missing = client.get(
        "/api/appointments/beauty-slots",
        params={"service": "猫洗护", "coat_length": "短毛", "date": DAY, "store": HG},
    )
    assert missing.status_code == 400, missing.text
    body = missing.json()
    assert body["message"] == BEAUTY_SIZE_REQUIRED_MESSAGE
    assert body["detail"] == BEAUTY_SIZE_REQUIRED_MESSAGE
    assert "duration_minutes" not in body

    old_label = client.get(
        "/api/appointments/beauty-slots",
        params={
            "service": "猫洗护",
            "pet_size": "大型猫（4kg 以上）",
            "coat_length": "长毛",
            "date": DAY,
            "store": HG,
        },
    )
    assert old_label.status_code == 200, old_label.text
    assert old_label.json()["duration_minutes"] == 120
    assert "21:00" not in old_label.json()["available_slots"]

    by_weight = client.get(
        "/api/appointments/beauty-slots",
        params={"service": "猫洗护", "weight_kg": "5", "coat_length": "短毛", "date": DAY, "store": HG},
    )
    assert by_weight.status_code == 200, by_weight.text
    assert by_weight.json()["duration_minutes"] == 120
    assert by_weight.json()["pet_size"].startswith("大型猫")
    assert "21:00" not in by_weight.json()["available_slots"]


def test_file_weight_and_stored_duration_are_not_rewritten():
    customer_id, pet_id = _customer("slot-file", "13800006661", "五公斤")
    db = SessionLocal()
    try:
        db.add(WeightRecord(pet_id=pet_id, record_date=beijing_today().isoformat(), weight_kg=5))
        db.commit()
    finally:
        db.close()
    from_file = client.get(
        "/api/appointments/beauty-slots",
        params={"service": "猫洗护", "pet_id": pet_id, "coat_length": "短毛", "date": DAY, "store": HG},
    )
    assert from_file.status_code == 200, from_file.text
    assert from_file.json()["duration_minutes"] == 120
    assert from_file.json()["pet_size"].startswith("大型猫")

    appointment_id = _appointment(
        customer_id, pet_id, service="猫洗护", when=OTHER_DAY, clock="13:00", duration=90,
    )
    kept = client.get(
        "/api/appointments/beauty-slots",
        params={"service": "猫洗护", "appointment_id": appointment_id, "date": DAY, "store": HG},
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["duration_minutes"] == 90
    assert "20:30" in kept.json()["available_slots"]
    assert "21:00" not in kept.json()["available_slots"]
    assert _duration_of(appointment_id) == 90


def test_reschedule_reuses_stored_duration():
    customer_id, pet_id = _customer("slot-move", "13800006662", "无体重")
    moving = _appointment(
        customer_id, pet_id, service="猫洗护", when=OTHER_DAY, clock="13:00", duration=120,
    )
    _appointment(
        customer_id, pet_id, service="犬洗护", when=DAY, clock="16:30", duration=60, pet_size="小型犬（3kg 至不足 12kg）",
    )

    db = SessionLocal()
    try:
        looked = tool_stage_reschedule(db, "slot-move", {
            "appointment_id": moving,
            "date": DAY,
            "time": "21:00",
        })
    finally:
        db.close()
    assert looked["ok"] is False
    assert "改不过去" in looked["message"]
    assert _duration_of(moving) == 120

    late = client.post(
        f"/api/ai-cs/appointments/{moving}/reschedule",
        headers=KEY,
        json={"openid": "slot-move", "date": DAY, "time": "21:00"},
    )
    assert late.status_code == 400, late.text
    assert "22:00" in late.text
    assert _duration_of(moving) == 120

    overlap = client.post(
        f"/api/ai-cs/appointments/{moving}/reschedule",
        headers=KEY,
        json={"openid": "slot-move", "date": DAY, "time": "15:00"},
    )
    assert overlap.status_code == 409, overlap.text
    assert "冲突" in overlap.text
    assert _duration_of(moving) == 120

    moved = client.post(
        f"/api/ai-cs/appointments/{moving}/reschedule",
        headers=KEY,
        json={"openid": "slot-move", "date": DAY, "time": "13:00"},
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["appointment"]["duration_minutes"] == 120
    assert _duration_of(moving) == 120


def test_reschedule_without_duration_does_not_guess_sixty():
    customer_id, pet_id = _customer("slot-empty", "13800006663", "没资料")
    bare = _appointment(
        customer_id, pet_id, service="猫洗护", when=OTHER_DAY, clock="13:00", duration=0,
    )
    db = SessionLocal()
    try:
        looked = tool_stage_reschedule(db, "slot-empty", {
            "appointment_id": bare,
            "date": DAY,
            "time": "14:00",
        })
    finally:
        db.close()
    assert looked["ok"] is False
    assert looked["message"] == BEAUTY_SIZE_REQUIRED_MESSAGE
    assert looked["error_code"] == "pet_weight_required"

    refused = client.post(
        f"/api/ai-cs/appointments/{bare}/reschedule",
        headers=KEY,
        json={"openid": "slot-empty", "date": DAY, "time": "14:00"},
    )
    assert refused.status_code == 400, refused.text
    assert refused.json()["error_code"] == "pet_weight_required"
    assert refused.json()["message"] == BEAUTY_SIZE_REQUIRED_MESSAGE
    assert _duration_of(bare) == 0

    weighed_customer, weighed_pet = _customer("slot-weigh", "13800006664", "刚称过")
    db = SessionLocal()
    try:
        db.add(WeightRecord(pet_id=weighed_pet, record_date=beijing_today().isoformat(), weight_kg=5))
        db.commit()
    finally:
        db.close()
    weighed = _appointment(
        weighed_customer, weighed_pet, service="猫洗护", when=OTHER_DAY, clock="13:00", duration=0,
    )
    db = SessionLocal()
    try:
        preview = tool_stage_reschedule(db, "slot-weigh", {
            "appointment_id": weighed,
            "date": DAY,
            "time": "20:30",
        })
    finally:
        db.close()
    # 5kg 是大型猫，洗护 120 分钟，20:30 开始会超过 22:00。按 60 分钟则会放行。
    assert preview["ok"] is False, preview
    assert "改不过去" in preview["message"]
    assert _duration_of(weighed) == 0
    kept = client.post(
        f"/api/ai-cs/appointments/{weighed}/reschedule",
        headers=KEY,
        json={"openid": "slot-weigh", "date": DAY, "time": "18:00"},
    )
    assert kept.status_code == 200, kept.text
    assert _duration_of(weighed) == 0


def test_pages_and_old_miniapp_contract():
    for relative in (
        "templates/uk/appointment_create.html",
        "templates/m_uk/appointment_new.html",
        "templates/includes/admin_appointments_scripts.html",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "请先填写体型或体重" in text, relative
    mini = (ROOT / "miniapp/pages/appointment/index.js").read_text(encoding="utf-8")
    assert "if (!sn || !sz || !cl)" in mini
    assert "pet_size=${encodeURIComponent(sz)}" in mini
    assert "大型猫（5kg 及以上）" in mini


if __name__ == "__main__":
    test_beauty_slots_rejects_missing_size_and_weight()
    test_file_weight_and_stored_duration_are_not_rewritten()
    test_reschedule_reuses_stored_duration()
    test_reschedule_without_duration_does_not_guess_sixty()
    test_pages_and_old_miniapp_contract()
    print("beauty slot size required tests passed")
