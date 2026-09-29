"""员工小程序登录、令牌和门店隔离回归测试。"""

import os
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-staff-miniapp-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"

from fastapi.testclient import TestClient
from passlib.hash import bcrypt

from app import models  # noqa: F401
from app.database import Base, SessionLocal, engine
import app.main as main_module
from app.main import app
from app.models import AdminUser, Appointment, Customer, Pet, Visit


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

        own_customer = client.get("/api/staff-miniapp/customers", params={"q": "横岗"}, headers=headers).json()["items"][0]
        created = client.post("/api/staff-miniapp/appointments", json={
            "category": "outpatient", "service_name": "手机端复诊",
            "customer_id": own_customer["id"], "pet_id": own_customer["pets"][0]["id"],
            "appointment_date": today, "appointment_time": "16:00", "duration_minutes": 30,
        }, headers=headers)
        assert created.status_code == 200, created.text
        assert created.json()["appointment"]["store"] == "横岗店"

        day_off = client.post("/api/staff-miniapp/calendar/beauty-day-off", json={
            "date": today,
        }, headers=headers)
        assert day_off.status_code == 200, day_off.text
        beauty_blocked = client.post("/api/staff-miniapp/appointments", json={
            "category": "beauty", "service_name": "美容洗护",
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
