"""东环店 TNR 从 13:00 起可约；横岗店仍是 11:00–18:00。已有早场预约不改。"""

import os
import re
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-store-hours-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["UPLOAD_DIR"] = str(Path(TEMP_DIR.name) / "uploads")
os.environ["SESSION_SECRET"] = "tnr-store-hours-test"
os.environ["SESSION_HTTPS_ONLY"] = "0"

from fastapi.testclient import TestClient
from passlib.hash import bcrypt

from app.database import Base, SessionLocal, engine
import app.main as main_module
from app.main import (
    _check_tnr_constraints,
    _tnr_hours_public,
    _tnr_time_window,
    app,
)
from app.models import AdminUser, Application, Appointment, AppointmentCategory


@asynccontextmanager
async def _test_lifespan(_app):
    yield


app.router.lifespan_context = _test_lifespan
Base.metadata.create_all(bind=engine)

DH = "大风动物医院（东环店）"
HG = "大风动物医院（横岗店）"
DAY = (datetime.now().date() + timedelta(days=1)).isoformat()
OPENID = "openid-tnr-hours"


def _fake_wechat(code: str) -> dict:
    if isinstance(code, str) and code.startswith("as-"):
        return {"openid": code[3:]}
    raise RuntimeError("bad code")


main_module.wechat_code2session = _fake_wechat


def _detail(response) -> str:
    body = response.json()
    detail = body.get("detail", body)
    if isinstance(detail, list):
        return str(detail)
    return str(detail)


def _application(db, nickname: str) -> Application:
    row = Application(
        applicant_name="测试申请人",
        phone="13800001111",
        address="深圳",
        cat_nickname=nickname,
        cat_gender="female",
        clinic_store=DH,
        status="approved",
        wechat_openid=OPENID,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _payload(app_id: int, store: str, when: str, day: str = DAY) -> dict:
    return {
        "code": "as-" + OPENID,
        "category": "tnr",
        "service_name": "TNR 手术安排",
        "customer_name": "测试申请人",
        "phone": "13800001111",
        "pet_name": "小花",
        "pet_gender": "female",
        "store": store,
        "appointment_date": day,
        "appointment_time": when,
        "duration_minutes": 60,
        "related_application_id": app_id,
    }


def test_window_config():
    assert _tnr_time_window(DH) == ("13:00", "18:00")
    assert _tnr_time_window("东环店") == ("13:00", "18:00")
    assert _tnr_time_window(HG) == ("11:00", "18:00")
    assert _tnr_time_window("横岗店") == ("11:00", "18:00")
    public = _tnr_hours_public()
    assert public["default_start"] == "11:00"
    assert public["by_store"]["东环店"]["start"] == "13:00"
    assert public["by_store"][DH]["start"] == "13:00"
    assert public["by_store"]["横岗店"]["start"] == "11:00"
    rendered = main_module.templates.env.from_string(
        "{{ tnr_hours()['by_store']['东环店']['start'] }}"
    ).render()
    assert rendered == "13:00"


def test_constraints_do_not_touch_other_stores_or_categories():
    db = SessionLocal()
    try:
        early = _check_tnr_constraints(
            db, category="tnr", store=DH, appointment_date=DAY,
            appointment_time="12:30",
        )
        assert early and "东环店" in early and "13:00" in early
        assert _check_tnr_constraints(
            db, category="tnr", store=DH, appointment_date=DAY,
            appointment_time="13:00",
        ) is None
        assert _check_tnr_constraints(
            db, category="tnr", store="东环店", appointment_date=DAY,
            appointment_time="12:59",
        )
        henggang_ok = _check_tnr_constraints(
            db, category="tnr", store=HG, appointment_date=DAY,
            appointment_time="11:00",
        )
        assert henggang_ok is None
        henggang_early = _check_tnr_constraints(
            db, category="tnr", store=HG, appointment_date=DAY,
            appointment_time="10:30",
        )
        assert henggang_early == "TNR 手术预约时间须在 11:00–18:00 之间，请重新选择。"
        assert _check_tnr_constraints(
            db, category="outpatient", store=DH, appointment_date=DAY,
            appointment_time="09:00",
        ) is None
        assert _check_tnr_constraints(
            db, category="surgery", store=DH, appointment_date=DAY,
            appointment_time="10:00",
        ) is None
    finally:
        db.close()


def test_existing_morning_appointment_is_kept():
    db = SessionLocal()
    try:
        row = Appointment(
            category=AppointmentCategory.tnr.value,
            status="confirmed",
            service_name="TNR 手术安排",
            customer_name="历史申请人",
            phone="13800002222",
            pet_name="早场猫",
            pet_gender="female",
            store=DH,
            appointment_date=DAY,
            appointment_time="11:00",
            duration_minutes=60,
            wechat_openid=OPENID,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        kept_id = row.id
        assert _check_tnr_constraints(
            db, category="tnr", store=DH, appointment_date=DAY,
            appointment_time="11:00", exclude_id=kept_id,
        ) is None
        moved = _check_tnr_constraints(
            db, category="tnr", store=DH, appointment_date=DAY,
            appointment_time="12:00", exclude_id=kept_id,
        )
        assert moved and "13:00" in moved
        db.refresh(row)
        assert row.appointment_time == "11:00"
        assert row.store == DH
    finally:
        db.close()


def test_http_create_and_update():
    db = SessionLocal()
    try:
        rejected_app = _application(db, "拒绝用")
        dh_app = _application(db, "东环成功")
        hg_app = _application(db, "横岗成功")
        legacy = Appointment(
            category="tnr", status="pending", service_name="TNR 手术安排",
            customer_name="历史申请人", phone="13800003333", pet_name="原早场",
            pet_gender="female", store=DH, appointment_date=DAY,
            appointment_time="11:00", duration_minutes=60, wechat_openid=OPENID,
        )
        db.add(legacy)
        db.commit()
        db.refresh(legacy)
        legacy_id = legacy.id
        rejected_id, dh_id, hg_id = rejected_app.id, dh_app.id, hg_app.id
    finally:
        db.close()

    client = TestClient(app)
    cfg = client.get("/api/appointments/config")
    assert cfg.status_code == 200, cfg.text
    assert cfg.json()["tnr_hours"]["by_store"]["东环店"]["start"] == "13:00"
    assert cfg.json()["tnr_hours"]["by_store"]["横岗店"]["start"] == "11:00"

    denied = client.post("/api/appointments/create", json=_payload(rejected_id, DH, "12:30"))
    assert denied.status_code == 400, denied.text
    message = _detail(denied)
    assert "东环店" in message and "13:00" in message

    hg_denied = client.post("/api/appointments/create", json=_payload(hg_id, HG, "10:30"))
    assert hg_denied.status_code == 400, hg_denied.text
    assert _detail(hg_denied) == "TNR 手术预约时间须在 11:00–18:00 之间，请重新选择。"

    later = (datetime.now().date() + timedelta(days=3)).isoformat()
    created = client.post("/api/appointments/create", json=_payload(dh_id, DH, "13:00", day=later))
    assert created.status_code == 200, created.text
    assert created.json()["appointment"]["appointment_time"] == "13:00"

    hg_created = client.post("/api/appointments/create", json=_payload(hg_id, HG, "11:00", day=(
        datetime.now().date() + timedelta(days=2)
    ).isoformat()))
    assert hg_created.status_code == 200, hg_created.text
    assert hg_created.json()["appointment"]["appointment_time"] == "11:00"

    same_slot = client.post(
        f"/api/wechat/appointments/{legacy_id}/update",
        json={"code": "as-" + OPENID, "appointment_date": DAY, "appointment_time": "11:00", "notes": "只改备注"},
    )
    assert same_slot.status_code == 200, same_slot.text
    assert same_slot.json()["appointment"]["appointment_time"] == "11:00"

    moved = client.post(
        f"/api/wechat/appointments/{legacy_id}/update",
        json={"code": "as-" + OPENID, "appointment_date": DAY, "appointment_time": "12:15", "store": DH},
    )
    assert moved.status_code == 400, moved.text
    assert "13:00" in _detail(moved)

    db = SessionLocal()
    try:
        row = db.get(Appointment, legacy_id)
        assert row is not None
        assert row.appointment_time == "11:00"
        assert row.notes == "只改备注"
        assert db.query(Appointment).filter(
            Appointment.store == DH,
            Appointment.appointment_time == "12:30",
        ).count() == 0
    finally:
        db.close()


def test_admin_pages_include_store_hours():
    db = SessionLocal()
    try:
        if db.query(AdminUser).filter(AdminUser.username == "hours_admin").first() is None:
            db.add(AdminUser(
                username="hours_admin",
                password_hash=bcrypt.hash("test123456"),
                role="superadmin",
                store="",
                is_active=True,
            ))
            db.commit()
    finally:
        db.close()
    client = TestClient(app)
    login_page = client.get("/admin/login")
    assert login_page.status_code == 200
    token = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page.text)
    assert token, login_page.text[:500]
    logged_in = client.post("/admin/login", data={
        "username": "hours_admin",
        "password": "test123456",
        "csrf_token": token.group(1),
    }, follow_redirects=False)
    assert logged_in.status_code == 303, logged_in.text

    desktop = client.get("/admin/appointments/create?category=tnr")
    assert desktop.status_code == 200, desktop.text[:400]
    assert "tnr_hours.js" in desktop.text
    assert "acSyncTnrHours" in desktop.text
    assert "window.TNR_HOURS" in desktop.text
    assert "13:00" in desktop.text

    mobile = client.get("/m/appointments/new?category=tnr")
    assert mobile.status_code == 200, mobile.text[:400]
    assert "syncTnrHours" in mobile.text
    assert "m_tnr_hour_hint" in mobile.text
    assert "window.TNR_HOURS" in mobile.text


if __name__ == "__main__":
    test_window_config()
    test_constraints_do_not_touch_other_stores_or_categories()
    test_existing_morning_appointment_is_kept()
    test_http_create_and_update()
    test_admin_pages_include_store_hours()
    print("ok")
