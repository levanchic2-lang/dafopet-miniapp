"""美容附加项目默认不加时。基础时长不变，已保存的预约时长不被重算。"""

import os
import re
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="beauty-addon-duration-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["UPLOAD_DIR"] = str(Path(TEMP_DIR.name) / "uploads")
os.environ["SESSION_SECRET"] = "beauty-addon-duration-test"
os.environ["SESSION_HTTPS_ONLY"] = "0"

from fastapi.testclient import TestClient
from passlib.hash import bcrypt

from app.database import Base, SessionLocal, engine
import app.main as main_module
from app.main import (
    _BEAUTY_ADDON_EXTRA_MINUTES,
    _beauty_addon_duration_note,
    _beauty_slots_for_date,
    _calc_beauty_duration,
    _check_appointment_conflict,
    app,
    templates,
)
from app.models import AdminUser, Appointment, AppointmentCategory, AppointmentStatus


@asynccontextmanager
async def _test_lifespan(_app):
    yield


app.router.lifespan_context = _test_lifespan
Base.metadata.create_all(bind=engine)

HG = "大风动物医院（横岗店）"
DAY = (datetime.now().date() + timedelta(days=3)).isoformat()
ADDONS = "去浮毛,SPA,护发素,纯手剪,药浴,去油"
ZERO_ADDONS = "SPA,护发素,纯手剪,药浴,去油"
SIZE_EXTRA = {
    "微小型犬（4kg 以下）": 30,
    "小型犬（4–10kg）": 30,
    "中型犬（10–15kg）": 45,
    "中大型犬（15–25kg）": 60,
    "大型犬（25kg 以上）": 90,
    "小型猫（2.5kg 以下）": 30,
    "中型猫（2.5–4kg）": 30,
    "大型猫（4kg 以上）": 45,
}

# 猫犬 / 体型 / 毛长的基础分钟。附加项为 0 时，带不带附加都应等于这张表。
BASE_CASES = [
    ("犬洗护", "微小型犬（4kg 以下）", "短毛", 30),
    ("犬洗护", "微小型犬（4kg 以下）", "长毛", 30),
    ("犬造型", "微小型犬（4kg 以下）", "短毛", 90),
    ("犬洗护", "小型犬（4–10kg）", "短毛", 60),
    ("犬洗护", "小型犬（4–10kg）", "长毛", 60),
    ("犬造型", "小型犬（4–10kg）", "短毛", 120),
    ("犬洗护", "中型犬（10–15kg）", "短毛", 60),
    ("犬洗护", "中型犬（10–15kg）", "长毛", 90),
    ("犬造型", "中型犬（10–15kg）", "长毛", 150),
    ("犬洗护", "中大型犬（15–25kg）", "短毛", 90),
    ("犬洗护", "中大型犬（15–25kg）", "长毛", 120),
    ("犬造型", "中大型犬（15–25kg）", "短毛", 180),
    ("犬洗护", "大型犬（25kg 以上）", "短毛", 120),
    ("犬洗护", "大型犬（25kg 以上）", "长毛", 150),
    ("犬造型", "大型犬（25kg 以上）", "长毛", 210),
    ("猫洗护", "小型猫（2.5kg 以下）", "短毛", 60),
    ("猫洗护", "小型猫（2.5kg 以下）", "长毛", 90),
    ("猫造型", "小型猫（2.5kg 以下）", "短毛", 120),
    ("猫洗护", "中型猫（2.5–4kg）", "短毛", 90),
    ("猫洗护", "中型猫（2.5–4kg）", "长毛", 120),
    ("猫造型", "中型猫（2.5–4kg）", "长毛", 120),
    ("猫洗护", "大型猫（4kg 以上）", "短毛", 120),
    ("猫洗护", "大型猫（4kg 以上）", "长毛", 150),
    ("猫造型", "大型猫（4kg 以上）", "短毛", 150),
]


def test_base_durations_ignore_addons():
    assert set(_BEAUTY_ADDON_EXTRA_MINUTES) == {"去浮毛", "去底绒", "SPA", "护发素", "纯手剪", "药浴", "去油"}
    assert _BEAUTY_ADDON_EXTRA_MINUTES["去浮毛"] is _BEAUTY_ADDON_EXTRA_MINUTES["去底绒"]
    assert _BEAUTY_ADDON_EXTRA_MINUTES["SPA"] == 0
    assert "去底绒" in _beauty_addon_duration_note()
    assert "其余附加项不额外占用时间" in _beauty_addon_duration_note()
    for service, size, coat, expected in BASE_CASES:
        plain = _calc_beauty_duration(service, size, coat, "")
        with_zero = _calc_beauty_duration(service, size, coat, ZERO_ADDONS)
        assert plain == expected, (service, size, coat, plain, expected)
        assert with_zero == expected, (service, size, coat, with_zero)
        extra = SIZE_EXTRA[size]
        assert _calc_beauty_duration(service, size, coat, "去浮毛") == expected + extra
        assert _calc_beauty_duration(service, size, coat, "去底绒") == expected + extra
        assert _calc_beauty_duration(service, size, coat, "去浮毛,去底绒") == expected + extra
    assert _calc_beauty_duration("犬洗护", "小型犬（4–10kg）", "短毛", "  SPA , ,不存在的项目 ") == 60
    assert _calc_beauty_duration("犬洗护", "大型犬（25kg 以上）", "短毛", "去浮毛", weight_kg=8) == 120 + 30
    assert _calc_beauty_duration("猫洗护", "大型猫（4kg 以上）", "短毛", "去底绒", weight_kg=2.5) == 120 + 30
    assert _calc_beauty_duration("猫洗护", "小型猫（2.5kg 以下）", "短毛", "去浮毛", weight_kg=6) == 60 + 45
    assert _calc_beauty_duration("犬洗护", "小型犬（4–10kg）", "短毛", "去浮毛", weight_kg=10) == 60 + 45
    assert _calc_beauty_duration("犬洗护", "小型犬（4–10kg）", "短毛", "去浮毛", weight_kg=20) == 60 + 60
    assert _calc_beauty_duration("犬洗护", "小型犬（4–10kg）", "短毛", "去浮毛", weight_kg=30) == 60 + 60
    assert _calc_beauty_duration("犬洗护", "小型犬（4–10kg）", "短毛", "去浮毛", weight_kg=31) == 60 + 90


def test_addon_minutes_are_configurable_per_item():
    original = _BEAUTY_ADDON_EXTRA_MINUTES["药浴"]
    try:
        _BEAUTY_ADDON_EXTRA_MINUTES["药浴"] = 15
        base = _calc_beauty_duration("猫造型", "小型猫（2.5kg 以下）", "短毛", "")
        assert base == 120
        assert _calc_beauty_duration("猫造型", "小型猫（2.5kg 以下）", "短毛", "药浴") == 135
        assert _calc_beauty_duration("猫造型", "小型猫（2.5kg 以下）", "短毛", "药浴,SPA,药浴") == 150
        note = _beauty_addon_duration_note()
        assert "药浴 +15 分钟" in note
        assert "其余附加项不额外占用时间" in note
    finally:
        _BEAUTY_ADDON_EXTRA_MINUTES["药浴"] = original


def test_existing_appointment_keeps_stored_duration_for_slots_and_conflicts():
    """旧单按当时保存的 90 分钟占位。新单同样的附加项只算基础 60 分钟。"""
    db = SessionLocal()
    try:
        row = Appointment(
            category=AppointmentCategory.beauty.value,
            status=AppointmentStatus.confirmed.value,
            service_name="犬洗护",
            customer_name="旧客户",
            phone="13800001111",
            pet_name="豆豆",
            pet_gender="male",
            store=HG,
            appointment_date=DAY,
            appointment_time="13:00",
            duration_minutes=90,
            pet_size="小型犬（4–10kg）",
            coat_length="短毛",
            addon_services="SPA",
            source="admin",
        )
        db.add(row)
        db.commit()
        stored_id = row.id

        slots = _beauty_slots_for_date(db, HG, DAY, "犬洗护", 60)
        assert "14:00" not in slots
        assert "14:30" in slots
        blocked = _check_appointment_conflict(
            db, store=HG, appointment_date=DAY, appointment_time="14:00",
            duration_minutes=60, category="beauty", service_name="犬洗护",
        )
        free = _check_appointment_conflict(
            db, store=HG, appointment_date=DAY, appointment_time="14:30",
            duration_minutes=60, category="beauty", service_name="犬洗护",
        )
        assert blocked is not None and blocked.id == stored_id
        assert free is None
        db.refresh(row)
        assert row.duration_minutes == 90
        assert row.addon_services == "SPA"
    finally:
        db.close()


def test_beauty_slots_api_and_pages_use_zero_addon_time():
    client = TestClient(app)
    response = client.get(
        "/api/appointments/beauty-slots",
        params={
            "service": "犬洗护",
            "pet_size": "小型犬（4–10kg）",
            "coat_length": "短毛",
            "addons": ZERO_ADDONS,
            "date": DAY,
            "store": HG,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["duration_minutes"] == 60
    assert body["duration_display"] == "1小时"
    assert body["addon_extra_minutes"]["SPA"] == 0
    assert "14:00" not in body["available_slots"]
    assert "14:30" in body["available_slots"]

    db = SessionLocal()
    try:
        db.add(AdminUser(
            username="admin",
            password_hash=bcrypt.hash("test123456"),
            role="superadmin",
            store="",
        ))
        db.commit()
    finally:
        db.close()

    login_page = client.get("/admin/login")
    assert login_page.status_code == 200
    token = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text)
    assert token, "登录页没有 csrf"
    logged_in = client.post(
        "/admin/login",
        data={"username": "admin", "password": "test123456", "csrf_token": token.group(1)},
        follow_redirects=False,
    )
    assert logged_in.status_code == 303, logged_in.text

    pages = {
        "/admin/appointments": "预约规则",
        "/admin/appointments/create": "新建预约",
        "/m/appointments/new": "新建预约",
    }
    for path, label in pages.items():
        page = client.get(path)
        assert page.status_code == 200, (path, page.status_code, page.text[:400])
        assert "去底绒" in page.text, (path, label)
        assert "其余附加项不额外占用时间" in page.text, (path, label)
        assert "+30 分钟" not in page.text, path
        assert "addonCount * 30" not in page.text, path
        assert "checked * 30" not in page.text, path

    desktop = client.get("/admin/appointments")
    assert '"SPA": 0' in desktop.text or '"SPA":0' in desktop.text
    mobile = client.get("/m/appointments/new")
    assert "BEAUTY_ADDON_EXTRA" in mobile.text
    create_page = client.get("/admin/appointments/create")
    assert "acAddonExtraMinutes" in create_page.text

    script = templates.get_template("includes/admin_appointments_scripts.html").render()
    assert "beautyAddonExtraMinutes" in script
    assert "* 30" not in script.split("beautyAddonExtraMinutes", 1)[1].split("function calcBeautyDuration", 1)[0]


def test_customer_miniapp_local_estimate_matches():
    src = (ROOT / "miniapp" / "pages" / "appointment" / "index.js").read_text(encoding="utf-8")
    assert "checked * 30" not in src
    assert "BEAUTY_ADDON_EXTRA" in src
    assert '"药浴": 0' in src
    assert 'label: "去底绒"' in src
    assert "大型犬" in src and "90" in src
    staff = (ROOT / "miniapp" / "pages" / "staff" / "calendar" / "calendar.js").read_text(encoding="utf-8")
    assert "species === \"cat\" ? 60 : 90" in staff
    assert "addon" not in staff.lower()


if __name__ == "__main__":
    test_base_durations_ignore_addons()
    test_addon_minutes_are_configurable_per_item()
    test_existing_appointment_keeps_stored_duration_for_slots_and_conflicts()
    test_beauty_slots_api_and_pages_use_zero_addon_time()
    test_customer_miniapp_local_estimate_matches()
    print("beauty addon duration tests passed")
