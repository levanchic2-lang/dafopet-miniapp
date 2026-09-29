"""员工小程序登录、令牌和门店隔离回归测试。"""

import os
import sys
import tempfile
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-staff-miniapp-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
UPLOAD_DIR = Path(TEMP_DIR.name) / "uploads"
os.environ["UPLOAD_DIR"] = str(UPLOAD_DIR)

from fastapi.testclient import TestClient
from passlib.hash import bcrypt

from app import models  # noqa: F401
from app.database import Base, SessionLocal, engine
import app.main as main_module
from app.main import app
from app.models import AdminUser, Application, Appointment, Customer, MediaFile, Pet, Visit


@asynccontextmanager
async def _test_lifespan(_app):
    """接口回归不启动调度器，也不创建正式数据目录中的启动锁。"""
    yield


app.router.lifespan_context = _test_lifespan
Base.metadata.create_all(bind=engine)
db = SessionLocal()
try:
    henggang = AdminUser(
        username="staff_hg", password_hash=bcrypt.hash("test123456"), role="staff",
        store="横岗店", display_name="横岗医生", mobile_role="doctor", is_active=True,
    )
    donghuan = AdminUser(
        username="staff_dh", password_hash=bcrypt.hash("test123456"), role="staff",
        store="东环店", display_name="东环员工", mobile_role="nurse", is_active=True,
    )
    hg_customer = Customer(name="横岗客户", phone="13900001111")
    dh_customer = Customer(name="东环客户", phone="13900002222")
    db.add_all([henggang, donghuan, hg_customer, dh_customer])
    db.flush()
    hg_pet = Pet(customer_id=hg_customer.id, name="横岗犬", species="dog", store="横岗店",
                 medical_record_no="HC26090001")
    dh_pet = Pet(customer_id=dh_customer.id, name="东环猫", species="cat", store="东环店")
    db.add_all([hg_pet, dh_pet])
    db.flush()
    today = datetime.now().date().isoformat()
    db.add_all([
        Visit(customer_id=hg_customer.id, pet_id=hg_pet.id, visit_date=today, store="横岗店"),
        Visit(customer_id=dh_customer.id, pet_id=dh_pet.id, visit_date=today, store="东环店"),
        Appointment(customer_id=hg_customer.id, pet_id=hg_pet.id, customer_name="横岗客户",
                    pet_name="横岗犬", appointment_date=today, appointment_time="23:59",
                    store="大风动物医院（横岗店）", status="confirmed"),
        Appointment(customer_id=dh_customer.id, pet_id=dh_pet.id, customer_name="东环客户",
                    pet_name="东环猫", appointment_date=today, appointment_time="11:00",
                    store="大风动物医院（东环店）", status="confirmed"),
    ])
    db.commit()
finally:
    db.close()

main_module.wechat_code2session = lambda code: {"openid": f"openid-{code}"}

try:
    with TestClient(app, base_url="https://testserver") as client:
        wrong = client.post("/api/staff-miniapp/login", json={
            "username": "staff_hg", "password": "wrong", "code": "hg",
        })
        assert wrong.status_code == 401

        login = client.post("/api/staff-miniapp/login", json={
            "username": "staff_hg", "password": "test123456", "code": "hg",
        })
        assert login.status_code == 200, login.text
        payload = login.json()
        token = payload["token"]
        assert payload["profile"]["store"] == "横岗店"
        headers = {"Authorization": f"Bearer {token}"}

        me = client.get("/api/staff-miniapp/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["profile"]["display_name"] == "横岗医生"

        dashboard = client.get("/api/staff-miniapp/dashboard", headers=headers)
        assert dashboard.status_code == 200, dashboard.text
        assert dashboard.json()["stats"]["appointments"] == 1
        assert dashboard.json()["stats"]["visits"] == 1
        assert dashboard.json()["next_appointment"]["pet_name"] == "横岗犬"

        calendar = client.get("/api/staff-miniapp/calendar", params={"start": today, "days": 3}, headers=headers)
        assert calendar.status_code == 200, calendar.text
        assert [row["pet_name"] for row in calendar.json()["appointments"]] == ["横岗犬"]
        own_appointment_id = calendar.json()["appointments"][0]["id"]

        moved = client.post(
            f"/api/staff-miniapp/appointments/{own_appointment_id}/reschedule",
            json={"date": today, "time": "20:15"}, headers=headers,
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["appointment"]["time"] == "20:15"

        arrived = client.post(
            f"/api/staff-miniapp/appointments/{own_appointment_id}/status",
            json={"status": "arrived"}, headers=headers,
        )
        assert arrived.status_code == 200, arrived.text
        assert arrived.json()["appointment"]["status"] == "arrived"

        db = SessionLocal()
        try:
            other_appointment_id = (
                db.query(Appointment.id)
                .filter(Appointment.store == "大风动物医院（东环店）")
                .scalar()
            )
        finally:
            db.close()
        forbidden = client.post(
            f"/api/staff-miniapp/appointments/{other_appointment_id}/status",
            json={"status": "arrived"}, headers=headers,
        )
        assert forbidden.status_code == 403

        db = SessionLocal()
        try:
            hg_tnr = Application(
                applicant_name="横岗申请人", phone="13900001111", clinic_store="横岗店",
                address="深圳", cat_nickname="横岗TNR猫", cat_gender="female",
                status="scheduled",
            )
            dh_tnr = Application(
                applicant_name="东环申请人", phone="13900002222", clinic_store="东环店",
                address="深圳", cat_nickname="东环TNR猫", cat_gender="male",
                status="scheduled",
            )
            db.add_all([hg_tnr, dh_tnr])
            db.flush()
            image_dir = UPLOAD_DIR / str(hg_tnr.id)
            image_dir.mkdir(parents=True, exist_ok=True)
            image_path = image_dir / "application.png"
            png_bytes = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
            image_path.write_bytes(png_bytes)
            media = MediaFile(
                application_id=hg_tnr.id, kind="application_image",
                stored_path=str(image_path), original_name="application.png",
            )
            db.add_all([
                media,
                Appointment(
                    related_application_id=hg_tnr.id, category="tnr", status="confirmed",
                    service_name="TNR 手术安排", customer_name="横岗申请人", phone="13900001111",
                    pet_name="横岗TNR猫", appointment_date=today, appointment_time="12:00",
                    store="大风动物医院（横岗店）", duration_minutes=60,
                ),
                Appointment(
                    related_application_id=dh_tnr.id, category="tnr", status="confirmed",
                    service_name="TNR 手术安排", customer_name="东环申请人", phone="13900002222",
                    pet_name="东环TNR猫", appointment_date=today, appointment_time="13:00",
                    store="大风动物医院（东环店）", duration_minutes=60,
                ),
            ])
            db.commit()
            hg_tnr_id, dh_tnr_id, media_id = hg_tnr.id, dh_tnr.id, media.id
        finally:
            db.close()

        tnr_today = client.get("/api/staff-miniapp/tnr/today", headers=headers)
        assert tnr_today.status_code == 200, tnr_today.text
        assert [item["cat_name"] for item in tnr_today.json()["items"]] == ["横岗TNR猫"]
        assert tnr_today.json()["items"][0]["application_media_ids"] == [media_id]
        media_response = client.get(f"/api/staff-miniapp/tnr/media/{media_id}", headers=headers)
        assert media_response.status_code == 200
        cross_store_tnr = client.post(f"/api/staff-miniapp/tnr/{dh_tnr_id}/verify", headers=headers)
        assert cross_store_tnr.status_code == 403
        verified_tnr = client.post(f"/api/staff-miniapp/tnr/{hg_tnr_id}/verify", headers=headers)
        assert verified_tnr.status_code == 200, verified_tnr.text
        assert verified_tnr.json()["item"]["verified"] is True
        assert verified_tnr.json()["item"]["before_count"] == 1
        uploaded_tnr = client.post(
            f"/api/staff-miniapp/tnr/{hg_tnr_id}/upload",
            data={"kind": "after", "media_type": "image"},
            files={"file": ("after.png", png_bytes, "image/png")}, headers=headers,
        )
        assert uploaded_tnr.status_code == 200, uploaded_tnr.text
        assert uploaded_tnr.json()["item"]["after_count"] == 1
        assert uploaded_tnr.json()["item"]["can_complete"] is True
        completed_tnr = client.post(f"/api/staff-miniapp/tnr/{hg_tnr_id}/complete", headers=headers)
        assert completed_tnr.status_code == 200, completed_tnr.text
        assert completed_tnr.json()["item"]["completed"] is True
        db = SessionLocal()
        try:
            tnr_appointment = db.query(Appointment).filter(
                Appointment.related_application_id == hg_tnr_id,
            ).one()
            assert tnr_appointment.status == "completed"
        finally:
            db.close()

        own_customer = client.get("/api/staff-miniapp/customers", params={"q": "横岗"}, headers=headers).json()["items"][0]
        created = client.post("/api/staff-miniapp/appointments", json={
            "category": "outpatient", "service_name": "手机端复诊",
            "customer_id": own_customer["id"], "pet_id": own_customer["pets"][0]["id"],
            "appointment_date": today, "appointment_time": "16:00", "duration_minutes": 30,
        }, headers=headers)
        assert created.status_code == 200, created.text
        assert created.json()["appointment"]["store"] == "横岗店"

        tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
        beauty_created = client.post("/api/staff-miniapp/appointments", json={
            "category": "grooming", "service_name": "犬造型",
            "customer_id": own_customer["id"], "pet_id": own_customer["pets"][0]["id"],
            "appointment_date": tomorrow, "appointment_time": "18:00", "duration_minutes": 90,
        }, headers=headers)
        assert beauty_created.status_code == 200, beauty_created.text
        assert beauty_created.json()["appointment"]["category"] == "grooming"
        assert beauty_created.json()["appointment"]["service_name"] == "犬造型"
        beauty_id = beauty_created.json()["appointment"]["id"]
        beauty_edited = client.post(
            f"/api/staff-miniapp/appointments/{beauty_id}/service",
            json={"service_name": "犬洗护", "duration": 90}, headers=headers,
        )
        assert beauty_edited.status_code == 200, beauty_edited.text
        assert beauty_edited.json()["appointment"]["category"] == "washcare"
        cat_mismatch = client.post(
            f"/api/staff-miniapp/appointments/{beauty_id}/service",
            json={"service_name": "猫洗护", "duration": 60}, headers=headers,
        )
        assert cat_mismatch.status_code == 400
        assert "该宠物档案为犬" in cat_mismatch.text

        day_off = client.post("/api/staff-miniapp/calendar/beauty-day-off", json={
            "date": today,
        }, headers=headers)
        assert day_off.status_code == 200, day_off.text
        invalid_beauty = client.post("/api/staff-miniapp/appointments", json={
            "category": "beauty", "service_name": "美容洗护",
            "customer_id": own_customer["id"], "pet_id": own_customer["pets"][0]["id"],
            "appointment_date": today, "appointment_time": "18:00", "duration_minutes": 60,
        }, headers=headers)
        assert invalid_beauty.status_code == 400
        assert "犬洗护" in invalid_beauty.text
        beauty_blocked = client.post("/api/staff-miniapp/appointments", json={
            "category": "washcare", "service_name": "犬洗护",
            "customer_id": own_customer["id"], "pet_id": own_customer["pets"][0]["id"],
            "appointment_date": today, "appointment_time": "18:00", "duration_minutes": 60,
        }, headers=headers)
        assert beauty_blocked.status_code == 400
        assert "美容师休息" in beauty_blocked.text

        own = client.get("/api/staff-miniapp/customers", params={"q": "横岗"}, headers=headers)
        assert [row["name"] for row in own.json()["items"]] == ["横岗客户"]
        by_record = client.get("/api/staff-miniapp/customers", params={"q": "HC26090001"}, headers=headers)
        assert [row["name"] for row in by_record.json()["items"]] == ["横岗客户"]
        too_short = client.get("/api/staff-miniapp/customers", params={"q": "横"}, headers=headers)
        assert too_short.json()["items"] == []
        other_store = client.get("/api/staff-miniapp/customers", params={"q": "东环"}, headers=headers)
        assert other_store.json()["items"] == []

        duplicate_wechat = client.post("/api/staff-miniapp/login", json={
            "username": "staff_dh", "password": "test123456", "code": "hg",
        })
        assert duplicate_wechat.status_code == 403

        db = SessionLocal()
        try:
            user = db.query(AdminUser).filter(AdminUser.username == "staff_hg").one()
            user.miniapp_token_created_at = datetime.utcnow() - timedelta(days=31)
            db.commit()
        finally:
            db.close()
        expired = client.get("/api/staff-miniapp/me", headers=headers)
        assert expired.status_code == 401
finally:
    engine.dispose()
    TEMP_DIR.cleanup()

print("PASS: staff miniapp auth and store isolation")
