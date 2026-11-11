"""AI 客服时段、预约、改期、取消，以及疫苗年龄和美容师封锁。"""

import json
import os
import sys
import tempfile
from urllib.parse import unquote
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="ai-cs-booking-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["UPLOAD_DIR"] = str(Path(TEMP_DIR.name) / "uploads")
os.environ["SESSION_SECRET"] = "ai-cs-booking-test"
os.environ["SESSION_HTTPS_ONLY"] = "0"
os.environ["AI_CS_API_KEY"] = "test-ai-cs-key"

from fastapi.testclient import TestClient
from passlib.hash import bcrypt

from app.database import Base, SessionLocal, engine
import app.main as main_module
from app.main import _staff_token_hash, app
from app.models import (
    AdminUser,
    Appointment,
    AppointmentCategory,
    AppointmentStatus,
    AuditLog,
    CalendarBlock,
    Customer,
    Pet,
    WeightRecord,
)
from app.services.ai_cs_booking import identify_vaccine_type, parse_birth_date
from app.time_utils import beijing_now, beijing_today


@asynccontextmanager
async def _test_lifespan(_app):
    yield


app.router.lifespan_context = _test_lifespan
Base.metadata.create_all(bind=engine)

KEY = {"X-AI-CS-Key": "test-ai-cs-key"}
HG = "大风动物医院（横岗店）"
DH = "大风动物医院（东环店）"
TODAY = beijing_today()
TOMORROW = TODAY + timedelta(days=1)
PLUS_15 = TODAY + timedelta(days=15)
PLUS_16 = TODAY + timedelta(days=16)


def _client():
    return TestClient(app)


def _seed():
    db = SessionLocal()
    try:
        owner = Customer(name="王瑞", phone="13800001111", wechat_openid="openid-one")
        other = Customer(name="另一人", phone="13800002222", wechat_openid="openid-dup")
        dup = Customer(name="重复", phone="13800003333", wechat_openid="openid-dup")
        db.add_all([owner, other, dup])
        db.flush()
        old = (TODAY - timedelta(days=400)).isoformat()
        young_rabies = (TODAY - timedelta(days=60)).isoformat()
        dog_5w = (TODAY - timedelta(days=35)).isoformat()
        dog_6w = (TODAY - timedelta(days=42)).isoformat()
        cat_7w = (TODAY - timedelta(days=49)).isoformat()
        cat_8w = (TODAY - timedelta(days=56)).isoformat()
        neuter_young = (TODAY - timedelta(days=150)).isoformat()
        pets = [
            Pet(customer_id=owner.id, name="大黄", species="dog", gender="male", birthday_estimate=old, store="横岗店"),
            Pet(customer_id=owner.id, name="小白", species="cat", gender="female", birthday_estimate=old, store="横岗店"),
            Pet(customer_id=owner.id, name="幼犬", species="dog", gender="male", birthday_estimate=young_rabies, store="横岗店"),
            Pet(customer_id=owner.id, name="未知龄", species="dog", gender="male", birthday_estimate="", store="横岗店"),
            Pet(customer_id=owner.id, name="五周", species="dog", gender="male", birthday_estimate=dog_5w, store="横岗店"),
            Pet(customer_id=owner.id, name="六周", species="dog", gender="male", birthday_estimate=dog_6w, store="横岗店"),
            Pet(customer_id=owner.id, name="七周猫", species="cat", gender="female", birthday_estimate=cat_7w, store="横岗店"),
            Pet(customer_id=owner.id, name="八周猫", species="cat", gender="female", birthday_estimate=cat_8w, store="横岗店"),
            Pet(customer_id=owner.id, name="兔子", species="other", gender="unknown", birthday_estimate=old, store="横岗店"),
            Pet(customer_id=owner.id, name="未填种", species="", gender="unknown", birthday_estimate=old, store="横岗店"),
            Pet(customer_id=owner.id, name="幼年绝育", species="cat", gender="female", birthday_estimate=neuter_young, store="横岗店"),
            Pet(customer_id=owner.id, name="美容犬", species="dog", gender="male", birthday_estimate=old, store="横岗店"),
            Pet(customer_id=owner.id, name="美容猫", species="cat", gender="female", birthday_estimate=old, store="横岗店"),
        ]
        db.add_all(pets)
        db.flush()
        db.add(WeightRecord(pet_id=pets[11].id, record_date=TODAY.isoformat(), weight_kg=8))
        admin = AdminUser(username="admin", password_hash=bcrypt.hash("test123456"), role="superadmin", store="", is_active=True)
        staff = AdminUser(
            username="staff_hg", password_hash=bcrypt.hash("test123456"), role="staff",
            store="横岗店", is_active=True, miniapp_token_hash=_staff_token_hash("staff-token"),
            miniapp_token_created_at=datetime.utcnow(),
        )
        db.add_all([admin, staff])
        db.commit()
        return {pet.name: pet.id for pet in pets}
    finally:
        db.close()


PETS = _seed()
client = _client()


def _book(kind, pet_name, **extra):
    body = {
        "openid": "openid-one",
        "store": "横岗店",
        "date": TOMORROW.isoformat(),
        "time": "14:00",
        "kind": kind,
        "pet_id": PETS[pet_name],
    }
    body.update(extra)
    return client.post("/api/ai-cs/appointments", headers=KEY, json=body)


def test_auth_and_customer_binding():
    denied = client.get("/api/ai-cs/slots/outpatient", params={"store": "横岗店", "date": TOMORROW.isoformat()})
    assert denied.status_code == 401
    assert denied.json()["error_code"] == "auth_invalid"
    bad = client.get(
        "/api/ai-cs/slots/outpatient",
        headers={"X-AI-CS-Key": "nope"},
        params={"store": "横岗店", "date": TOMORROW.isoformat()},
    )
    assert bad.status_code == 401
    missing = _book("outpatient", "大黄", openid="nobody", service_name="皮肤", time="10:00")
    assert missing.status_code == 400 and missing.json()["error_code"] == "customer_not_found"
    ambiguous = _book("outpatient", "大黄", openid="openid-dup", service_name="皮肤", time="10:30")
    assert ambiguous.status_code == 400 and ambiguous.json()["error_code"] == "customer_ambiguous"


def test_horizon_lead_time_and_same_day():
    far = client.get(
        "/api/ai-cs/slots/outpatient", headers=KEY,
        params={"store": "横岗店", "date": PLUS_16.isoformat(), "service_name": "皮肤"},
    )
    assert far.status_code == 400 and far.json()["error_code"] == "beyond_horizon"
    edge = client.get(
        "/api/ai-cs/slots/outpatient", headers=KEY,
        params={"store": "东环店", "date": PLUS_15.isoformat(), "service_name": "皮肤"},
    )
    assert edge.status_code == 200 and edge.json()["slots"]
    assert "10:00" in edge.json()["slots"] and "21:00" not in edge.json()["slots"]
    surgery_today = client.get(
        "/api/ai-cs/slots/surgery", headers=KEY,
        params={"store": "横岗店", "date": TODAY.isoformat(), "service_name": "绝育手术"},
    )
    assert surgery_today.status_code == 400 and surgery_today.json()["error_code"] == "lead_time"
    surgery = client.get(
        "/api/ai-cs/slots/surgery", headers=KEY,
        params={"store": "横岗店", "date": TOMORROW.isoformat(), "service_name": "绝育手术"},
    )
    assert surgery.status_code == 200
    body = surgery.json()
    assert "11:00" in body["slots"] and "10:30" not in body["slots"] and "18:00" not in body["slots"]
    assert "预计到院" in body["arrival_note"]
    same_day = client.get(
        "/api/ai-cs/slots/vaccine", headers=KEY,
        params={"store": "横岗店", "date": TODAY.isoformat(), "service_name": "疫苗/驱虫"},
    )
    assert same_day.status_code == 200
    now_min = beijing_now().hour * 60 + beijing_now().minute
    for slot in same_day.json()["slots"]:
        hour, minute = slot.split(":")
        assert int(hour) * 60 + int(minute) >= now_min
    assert same_day.json()["warning"] == ""


def test_partial_groomer_block_and_consecutive_pets():
    db = SessionLocal()
    try:
        db.add(CalendarBlock(
            title="美容师请假", block_date=TOMORROW.isoformat(), store="横岗店",
            track="beauty", start_time="14:00", end_time="16:00",
        ))
        db.commit()
    finally:
        db.close()
    slots = client.get(
        "/api/ai-cs/slots/beauty", headers=KEY,
        params={
            "store": "横岗店", "date": TOMORROW.isoformat(), "service_name": "犬洗护",
            "pet_size": "微小型犬（4kg 以下）", "coat_length": "短毛",
        },
    )
    assert slots.status_code == 200, slots.text
    names = slots.json()["slots"]
    assert "13:00" in names and "13:30" in names
    assert "14:00" not in names and "15:30" not in names
    assert "16:00" in names
    group = client.get(
        "/api/ai-cs/slots/beauty", headers=KEY,
        params={
            "store": "横岗店", "date": TOMORROW.isoformat(),
            "pets": json.dumps([
                {"service_name": "犬洗护", "pet_size": "微小型犬（4kg 以下）", "coat_length": "短毛"},
                {"service_name": "猫洗护", "pet_size": "小型猫（2.5kg 以下）", "coat_length": "短毛"},
            ], ensure_ascii=False),
        },
    )
    assert group.status_code == 200, group.text
    grouped = {item["start"]: item for item in group.json()["groups"]}
    assert "13:00" not in grouped
    assert "16:00" in grouped
    assert [pet["time"] for pet in grouped["16:00"]["pets"]] == ["16:00", "16:30"]
    booked = client.post("/api/ai-cs/appointments", headers=KEY, json={
        "openid": "openid-one", "store": "横岗店", "date": TOMORROW.isoformat(), "time": "16:00",
        "kind": "beauty",
        "pets": [
            {"pet_id": PETS["美容犬"], "service_name": "犬洗护", "pet_size": "小型犬（4–10kg）", "coat_length": "短毛", "addons": "去浮毛"},
            {"pet_id": PETS["美容猫"], "service_name": "猫洗护", "pet_size": "小型猫（2.5kg 以下）", "coat_length": "短毛"},
        ],
    })
    assert booked.status_code == 200, booked.text
    rows = booked.json()["appointments"]
    assert rows[0]["source"] == "ai_cs"
    assert rows[0]["duration_minutes"] == 90
    assert rows[0]["appointment_time"] == "16:00"
    assert rows[1]["appointment_time"] == "17:30"
    db = SessionLocal()
    try:
        dog = db.get(Pet, PETS["美容犬"])
        assert dog is not None
        weight = db.query(WeightRecord).filter(WeightRecord.pet_id == dog.id).one()
        assert weight.weight_kg == 8
    finally:
        db.close()


def test_double_book_and_cancel_same_day():
    first = _book("outpatient", "大黄", service_name="皮肤", time="15:00", date=TOMORROW.isoformat())
    assert first.status_code == 200, first.text
    second = _book("outpatient", "小白", service_name="口腔", time="15:00", date=TOMORROW.isoformat())
    assert second.status_code == 409
    assert second.json()["error_code"] in {"conflict", "capacity_full"}
    appointment_id = first.json()["appointments"][0]["id"]
    db = SessionLocal()
    try:
        row = db.get(Appointment, appointment_id)
        row.appointment_date = TODAY.isoformat()
        row.appointment_time = "16:00"
        db.commit()
    finally:
        db.close()
    cancelled = client.post(
        f"/api/ai-cs/appointments/{appointment_id}/cancel",
        headers=KEY, json={"openid": "openid-one", "reason": "临时有事"},
    )
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    assert body["late_penalty"] is False and body["same_day_cancel_allowed"] is True
    assert "迟到" in body["message"]


def test_neuter_age_and_surgery_window():
    too_young = _book("surgery", "幼年绝育", service_name="绝育手术", time="11:00")
    assert too_young.status_code == 400 and too_young.json()["error_code"] == "age_too_young"
    unknown = _book("surgery", "未知龄", service_name="绝育手术", time="11:00")
    assert unknown.status_code == 200, unknown.text
    assert "未知" in unknown.json()["warning"]
    assert "预计到院" in unknown.json()["arrival_note"]
    early = _book("surgery", "大黄", service_name="软组织手术", time="10:30")
    assert early.status_code == 400 and early.json()["error_code"] == "outside_hours"
    same_day = _book("surgery", "大黄", service_name="软组织手术", time="11:00", date=TODAY.isoformat())
    assert same_day.status_code == 400 and same_day.json()["error_code"] == "lead_time"
    ok = _book("surgery", "大黄", service_name="软组织手术", time="11:00", date=(TOMORROW + timedelta(days=1)).isoformat())
    assert ok.status_code == 200, ok.text
    assert ok.json()["warning"] == ""


def test_vaccine_rules():
    assert identify_vaccine_type("疫苗/驱虫") is None
    assert identify_vaccine_type("驱虫") is None
    assert identify_vaccine_type("狂犬疫苗") == "rabies"
    assert identify_vaccine_type("犬八联") == "canine_8"
    assert identify_vaccine_type("猫三联") == "combo_3"
    assert identify_vaccine_type("狂犬八联") == "ambiguous"
    assert parse_birth_date("") is None
    generic = _book("vaccine", "幼犬", service_name="疫苗/驱虫", time="10:00", store="东环店")
    assert generic.status_code == 200, generic.text
    assert generic.json()["warning"] == ""
    assert generic.json()["appointments"][0]["duration_minutes"] == 30
    rabies_store = _book("vaccine", "大黄", service_name="狂犬疫苗", time="10:30", store="东环店")
    assert rabies_store.status_code == 400 and rabies_store.json()["error_code"] == "rabies_store"
    assert "横岗" in rabies_store.json()["message"]
    rabies_young = _book("vaccine", "幼犬", service_name="狂犬疫苗", time="11:00")
    assert rabies_young.status_code == 400 and rabies_young.json()["error_code"] == "vaccine_too_young"
    rabies_unknown = _book("vaccine", "未知龄", service_name="狂犬疫苗", time="10:00")
    assert rabies_unknown.status_code == 200, rabies_unknown.text
    assert "未知" in rabies_unknown.json()["warning"]
    rabies_ok = _book("vaccine", "小白", service_name="狂犬疫苗", time="10:30")
    assert rabies_ok.status_code == 200, rabies_ok.text
    moved_store = client.post(
        f"/api/ai-cs/appointments/{rabies_ok.json()['appointments'][0]['id']}/reschedule",
        headers=KEY,
        json={"openid": "openid-one", "store": "东环店", "date": TOMORROW.isoformat(), "time": "10:30"},
    )
    assert moved_store.status_code == 400 and moved_store.json()["error_code"] == "rabies_store"
    eight_young = _book("vaccine", "五周", service_name="犬八联", time="12:30")
    assert eight_young.status_code == 400 and eight_young.json()["error_code"] == "vaccine_too_young"
    eight_ok = _book("vaccine", "六周", service_name="犬八联", time="13:00", store="东环店")
    assert eight_ok.status_code == 200, eight_ok.text
    assert eight_ok.json()["appointments"][0]["service_name"].endswith("疫苗")
    cat_young = _book("vaccine", "七周猫", service_name="猫三联", time="13:30")
    assert cat_young.status_code == 400 and cat_young.json()["error_code"] == "vaccine_too_young"
    cat_ok = _book("vaccine", "八周猫", service_name="猫三联", time="14:30")
    assert cat_ok.status_code == 200, cat_ok.text
    wrong_species = _book("vaccine", "大黄", service_name="猫三联", time="15:30")
    assert wrong_species.status_code == 400 and wrong_species.json()["error_code"] == "vaccine_species"
    wrong_dog = _book("vaccine", "小白", service_name="犬八联", time="16:00")
    assert wrong_dog.status_code == 400 and wrong_dog.json()["error_code"] == "vaccine_species"
    other = _book("vaccine", "兔子", service_name="狂犬疫苗", time="16:30")
    assert other.status_code == 400 and other.json()["error_code"] == "vaccine_species"
    unknown_species = _book("vaccine", "未填种", service_name="狂犬疫苗", time="17:00")
    assert unknown_species.status_code == 200, unknown_species.text
    assert "物种" in unknown_species.json()["warning"]
    ambiguous = _book("vaccine", "大黄", service_name="狂犬八联", time="17:30")
    assert ambiguous.status_code == 200, ambiguous.text
    assert "无法确认" in ambiguous.json()["warning"]
    late = _book("vaccine", "大黄", service_name="疫苗/驱虫", time="21:00", date=(TOMORROW + timedelta(days=2)).isoformat())
    assert late.status_code == 400 and late.json()["error_code"] == "outside_hours"


def test_reschedule_and_staff_admin_checks():
    created = _book("outpatient", "小白", service_name="体检", time="18:00", date=(TOMORROW + timedelta(days=3)).isoformat())
    assert created.status_code == 200, created.text
    appointment_id = created.json()["appointments"][0]["id"]
    moved = client.post(
        f"/api/ai-cs/appointments/{appointment_id}/reschedule",
        headers=KEY,
        json={"openid": "openid-one", "date": (TOMORROW + timedelta(days=3)).isoformat(), "time": "18:30"},
    )
    assert moved.status_code == 200, moved.text
    blocked_day = (TOMORROW + timedelta(days=4)).isoformat()
    db = SessionLocal()
    try:
        db.add(CalendarBlock(title="美容师休息", block_date=blocked_day, store="横岗店", track="beauty"))
        beauty = Appointment(
            category=AppointmentCategory.beauty.value, status=AppointmentStatus.confirmed.value,
            service_name="犬洗护", customer_name="王瑞", phone="13800001111", pet_name="美容犬",
            pet_gender="male", store=HG, appointment_date=TOMORROW.isoformat(), appointment_time="18:00",
            duration_minutes=60, source="admin", customer_id=1, pet_id=PETS["美容犬"],
        )
        filler_a = Appointment(
            category=AppointmentCategory.outpatient.value, status=AppointmentStatus.confirmed.value,
            service_name="皮肤", customer_name="甲", phone="13800004444", pet_name="甲",
            store=HG, appointment_date=blocked_day, appointment_time="10:00", duration_minutes=60, source="admin",
        )
        filler_b = Appointment(
            category=AppointmentCategory.outpatient.value, status=AppointmentStatus.confirmed.value,
            service_name="口腔", customer_name="乙", phone="13800005555", pet_name="乙",
            store=HG, appointment_date=blocked_day, appointment_time="11:00", duration_minutes=60, source="admin",
        )
        surgery = Appointment(
            category=AppointmentCategory.surgery.value, status=AppointmentStatus.confirmed.value,
            service_name="软组织手术", customer_name="丙", phone="13800006666", pet_name="丙",
            store=HG, appointment_date=TOMORROW.isoformat(), appointment_time="16:00", duration_minutes=60, source="admin",
        )
        db.add_all([beauty, filler_a, filler_b, surgery])
        db.commit()
        beauty_id = beauty.id
        surgery_id = surgery.id
    finally:
        db.close()
    staff = client.post(
        f"/api/staff-miniapp/appointments/{beauty_id}/reschedule",
        headers={"Authorization": "Bearer staff-token"},
        json={"date": blocked_day, "time": "13:00"},
    )
    assert staff.status_code == 400 and "封锁" in staff.text
    page = client.get("/admin/login")
    token = __import__("re").search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
    logged = client.post(
        "/admin/login",
        data={"username": "admin", "password": "test123456", "csrf_token": token},
        follow_redirects=False,
    )
    assert logged.status_code == 303
    form_page = client.get("/admin/appointments")
    csrf = __import__("re").search(r'name="csrf_token" value="([^"]+)"', form_page.text).group(1)
    refused = client.post(
        f"/admin/appointments/{beauty_id}/reschedule",
        data={
            "csrf_token": csrf, "new_store": HG, "new_date": blocked_day, "new_time": "13:00",
            "new_duration": "60", "reschedule_reason": "试试", "next": "/admin/appointments",
        },
        follow_redirects=False,
    )
    assert refused.status_code == 303 and "封锁" in unquote(refused.headers.get("location", ""))
    forced = client.post(
        f"/admin/appointments/{beauty_id}/reschedule",
        data={
            "csrf_token": csrf, "new_store": HG, "new_date": blocked_day, "new_time": "13:00",
            "new_duration": "60", "override": "1", "next": "/admin/appointments",
        },
        follow_redirects=False,
    )
    assert forced.status_code == 303 and "appointment_ok=reschedule" in forced.headers.get("location", "")
    cap = client.post(
        f"/admin/appointments/{surgery_id}/reschedule",
        data={
            "csrf_token": csrf, "new_store": HG, "new_date": blocked_day, "new_time": "09:00",
            "new_duration": "60", "next": "/admin/appointments",
        },
        follow_redirects=False,
    )
    assert cap.status_code == 303 and "容量" in unquote(cap.headers.get("location", ""))
    db = SessionLocal()
    try:
        audit = db.query(AuditLog).filter(AuditLog.action == "appointment_reschedule").order_by(AuditLog.id.desc()).first()
        detail = json.loads(audit.detail)
        assert detail.get("override") is True
        assert detail.get("skipped_checks")
    finally:
        db.close()


def test_multiple_profiles_require_customer_id():
    db = SessionLocal()
    try:
        rows = db.query(Customer).filter(Customer.wechat_openid == "openid-dup").order_by(Customer.id.asc()).all()
        assert len(rows) == 2
        phones = {row.phone for row in rows}
        if not db.query(Pet).filter(Pet.customer_id == rows[0].id, Pet.name == "豆豆").first():
            db.add(Pet(customer_id=rows[0].id, name="豆豆", species="dog", gender="male", store="横岗店"))
        if not db.query(Pet).filter(Pet.customer_id == rows[1].id, Pet.name == "米米").first():
            db.add(Pet(customer_id=rows[1].id, name="米米", species="cat", gender="female", store="横岗店"))
        db.commit()
        first_id, second_id = rows[0].id, rows[1].id
        pet_id = db.query(Pet).filter(Pet.customer_id == first_id, Pet.name == "豆豆").one().id
    finally:
        db.close()
    missing = _book("outpatient", "大黄", openid="openid-dup", service_name="皮肤", time="20:00")
    assert missing.status_code == 400
    body = missing.json()
    assert body["error_code"] == "customer_ambiguous"
    text = missing.text
    for secret in phones:
        assert secret not in text
    assert "另一人" not in text and "重复" not in text
    assert "豆豆" in text and "米米" in text
    choices = {item["id"]: item["pet_names"] for item in body["choices"]}
    assert choices[first_id] == ["豆豆"]
    assert choices[second_id] == ["米米"]
    assert "address" not in body
    booked = client.post("/api/ai-cs/appointments", headers=KEY, json={
        "openid": "openid-dup",
        "customer_id": first_id,
        "store": "横岗店",
        "date": TOMORROW.isoformat(),
        "time": "20:00",
        "kind": "outpatient",
        "service_name": "皮肤",
        "pet_id": pet_id,
    })
    assert booked.status_code == 200, booked.text
    assert booked.json()["appointments"][0]["customer_id"] == first_id
    wrong_pet = client.post("/api/ai-cs/appointments", headers=KEY, json={
        "openid": "openid-dup",
        "customer_id": second_id,
        "store": "横岗店",
        "date": TOMORROW.isoformat(),
        "time": "20:30",
        "kind": "outpatient",
        "service_name": "皮肤",
        "pet_id": pet_id,
    })
    assert wrong_pet.status_code == 400
    assert wrong_pet.json()["error_code"] == "pet_not_owned"
    outsider = client.post("/api/ai-cs/appointments", headers=KEY, json={
        "openid": "openid-dup",
        "customer_id": 999999,
        "store": "横岗店",
        "date": TOMORROW.isoformat(),
        "time": "20:30",
        "kind": "outpatient",
        "service_name": "皮肤",
        "pet_id": pet_id,
    })
    assert outsider.status_code == 400
    assert outsider.json()["error_code"] == "customer_ambiguous"
    assert "13800001111" not in outsider.text


def test_tnr_endpoint_still_rejects_other_categories():
    main_module.wechat_code2session = lambda code: {"openid": "openid-one"} if code == "owner-code" else (_ for _ in ()).throw(RuntimeError("bad"))
    response = client.post("/api/appointments/create", json={
        "code": "owner-code",
        "category": "outpatient", "service_name": "皮肤", "customer_name": "王瑞",
        "phone": "13800001111", "pet_name": "大黄", "pet_gender": "male", "store": HG,
        "appointment_date": TOMORROW.isoformat(), "appointment_time": "10:00",
    })
    assert response.status_code == 400
    assert "TNR" in response.text


if __name__ == "__main__":
    test_auth_and_customer_binding()
    test_horizon_lead_time_and_same_day()
    test_partial_groomer_block_and_consecutive_pets()
    test_double_book_and_cancel_same_day()
    test_neuter_age_and_surgery_window()
    test_vaccine_rules()
    test_reschedule_and_staff_admin_checks()
    test_multiple_profiles_require_customer_id()
    test_tnr_endpoint_still_rejects_other_categories()
    print("ai cs booking tests passed")
