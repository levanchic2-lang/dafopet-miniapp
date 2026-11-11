"""档案绑定验证码、禁止静默换绑、可信 IP，以及未登录不能读个人资料。"""

import logging
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-binding-auth-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["UPLOAD_DIR"] = str(Path(TEMP_DIR.name) / "uploads")
os.environ["SESSION_SECRET"] = "customer-binding-auth-test"
os.environ["SESSION_HTTPS_ONLY"] = "0"
os.environ["AI_CS_API_KEY"] = "test-ai-cs-key"

from fastapi.testclient import TestClient
from passlib.hash import bcrypt
from sqlalchemy import text

import app.main as main_module
from app.database import Base, SessionLocal, _try_sqlite_migrations, engine
from app.main import _staff_token_hash, app
from app.models import (
    AdminUser,
    Application,
    Appointment,
    AppointmentStatus,
    AuditLog,
    ComboVaccineRegistration,
    Customer,
    Pet,
    SmsVerificationCode,
)


@asynccontextmanager
async def _test_lifespan(_app):
    yield


app.router.lifespan_context = _test_lifespan
Base.metadata.create_all(bind=engine)

SENT = {"calls": 0, "code": "", "fail": False, "fail_with_code": False}


def _fake_sms(phone: str, code: str):
    SENT["calls"] += 1
    SENT["code"] = code
    SENT["phone"] = phone
    if SENT["fail"]:
        detail = f"gateway down {code}" if SENT["fail_with_code"] else "gateway down"
        return False, detail
    return True, ""


def _fake_code(code: str) -> dict:
    mapping = {
        "owner-code": "openid-owner",
        "stranger-code": "openid-stranger",
        "second-code": "openid-second",
        "banned-code": "openid-banned",
    }
    if code not in mapping:
        raise RuntimeError("bad code")
    return {"openid": mapping[code]}


main_module._send_binding_sms = _fake_sms
main_module.wechat_code2session = _fake_code

db = SessionLocal()
owner = Customer(name="甲客户", phone="13700001111", address="甲路1号", wechat_openid="")
twin = Customer(name="甲的另一份档案", phone="13700001111", address="甲路副档", wechat_openid="")
blocked = Customer(name="已绑别人", phone="13700002222", address="乙路2号", wechat_openid="openid-taken")
plain = Customer(name="未绑定客户", phone="13700003333", address="丙路3号", wechat_openid="")
staff = AdminUser(
    username="bind_staff",
    password_hash=bcrypt.hash("test123456"),
    role="staff",
    store="横岗店",
    display_name="绑定员工",
    is_active=True,
    miniapp_token_hash=_staff_token_hash("staff-token"),
    miniapp_token_created_at=datetime.utcnow(),
)
db.add_all([owner, twin, blocked, plain, staff])
db.flush()
db.add_all([
    Pet(customer_id=owner.id, name="甲犬", species="dog", breed="柯基", store="横岗店"),
    Pet(customer_id=twin.id, name="甲猫", species="cat", store="东环店"),
    Pet(customer_id=plain.id, name="丙猫", species="cat", store="横岗店"),
    Pet(customer_id=blocked.id, name="乙狗", species="dog", store="横岗店"),
])
db.flush()
app_row = Application(
    applicant_name="甲客户",
    phone="13700001111",
    address="甲路1号",
    cat_nickname="甲猫进度",
    cat_gender="female",
    health_note="咳嗽保密",
    wechat_openid="openid-owner",
    status="pending_manual",
    clinic_store="大风动物医院（横岗店）",
)
appt = Appointment(
    wechat_openid="openid-owner",
    category="tnr",
    status=AppointmentStatus.confirmed.value,
    service_name="TNR 手术安排",
    customer_name="甲客户",
    phone="13700001111",
    pet_name="甲犬",
    store="大风动物医院（横岗店）",
    appointment_date="2026-12-01",
    appointment_time="13:00",
    duration_minutes=60,
)
reg = ComboVaccineRegistration(
    customer_id=owner.id,
    pet_id=db.query(Pet).filter(Pet.customer_id == owner.id).one().id,
    clinic_store="横岗店",
    status="vaccinated",
    post_care_notice_snapshot="护理说明秘密",
)
today = datetime.now()
ban_phone = "13700004444"
ban_customer = Customer(name="封禁客户", phone=ban_phone, address="丁路4号", wechat_openid="openid-banned")
db.add_all([app_row, appt, reg, ban_customer])
db.flush()
for i in range(3):
    db.add(Appointment(
        category="tnr",
        service_name="TNR 绝育",
        customer_name="封禁客户",
        phone=ban_phone,
        pet_name="封禁猫",
        pet_gender="male",
        store="大风动物医院（横岗店）",
        appointment_date=f"{today.strftime('%Y-%m')}-{i + 5:02d}",
        appointment_time="10:00",
        duration_minutes=30,
        status="no_show",
    ))
db.commit()
owner_id = owner.id
twin_id = twin.id
blocked_id = blocked.id
plain_id = plain.id
app_id = app_row.id
appt_id = appt.id
reg_id = reg.id
db.close()

client = TestClient(app, follow_redirects=False)
LOGS: list[str] = []


class _Capture(logging.Handler):
    def emit(self, record):
        LOGS.append(record.getMessage())


_handler = _Capture()
logging.getLogger("app.main").addHandler(_handler)
logging.getLogger("app.main").setLevel(logging.INFO)


def _age(seconds: int) -> None:
    now = main_module._bind_now() - timedelta(seconds=seconds)
    with SessionLocal() as session:
        for row in session.query(SmsVerificationCode).all():
            row.created_at = now
        session.commit()


def _csrf() -> str:
    html = client.get("/admin/login").text
    return html.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]


def _login() -> str:
    token = _csrf()
    response = client.post("/admin/login", data={
        "username": "bind_staff",
        "password": "test123456",
        "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303, response.text
    return token


def _logout() -> None:
    response = client.get("/admin/logout", follow_redirects=False)
    assert response.status_code == 303, response.text


# 旧库补表
with engine.begin() as conn:
    conn.execute(text("DROP TABLE sms_verification_codes"))
_try_sqlite_migrations()
with engine.connect() as conn:
    cols = {row[1] for row in conn.execute(text("PRAGMA table_info(sms_verification_codes)"))}
assert {"code_hash", "phone", "expires_at", "attempt_count", "used", "client_ip"} <= cols

# 未建档：成功外观，不发短信，不说有没有档案
before = SENT["calls"]
missing = client.post("/api/customer-binding/send-code", json={"phone": "13900000999"})
assert missing.status_code == 200
missing_body = missing.json()
assert missing_body == {"ok": True, "message": main_module._BIND_SENT_MESSAGE}
assert SENT["calls"] == before
assert "dev_code" not in missing_body
assert "未找到" not in missing.text

# 已建档：同样的成功外观，验证码只进哈希
known = client.post("/api/customer-binding/send-code", json={"phone": "13700001111"})
assert known.json() == missing_body
assert "dev_code" not in known.text
assert SENT["code"] not in known.text
assert "甲客户" not in known.text and "甲路" not in known.text
with SessionLocal() as session:
    saved = (
        session.query(SmsVerificationCode)
        .filter(SmsVerificationCode.phone == "13700001111", SmsVerificationCode.used == False)  # noqa: E712
        .one()
    )
    assert saved.code_hash and SENT["code"] not in saved.code_hash
    assert len(saved.code_hash) == 64
    assert saved.attempt_count == 0
    assert saved.expires_at - saved.created_at == timedelta(minutes=5)

# 60 秒内不能重发
cooled = client.post("/api/customer-binding/send-code", json={"phone": "13700001111"})
assert cooled.json()["ok"] is False
assert "秒后再获取" in cooled.json()["error"]

# 错误次数到 5 次后，正确验证码也作废。客户端自报的 openid 无效。
code = SENT["code"]
for _ in range(4):
    wrong = client.post("/api/customer-binding/verify", json={
        "phone": "13700001111", "code": "000000", "openid": "openid-stranger", "js_code": "owner-code",
    })
    assert wrong.json()["ok"] is False
    assert wrong.json()["error"] == "验证码不正确或已失效"
fifth = client.post("/api/customer-binding/verify", json={
    "phone": "13700001111", "code": "111111", "js_code": "owner-code",
})
assert fifth.json()["error"] == "验证码已失效，请重新获取"
burned = client.post("/api/customer-binding/verify", json={
    "phone": "13700001111", "code": code, "openid": "openid-stranger", "js_code": "owner-code",
})
assert burned.json()["ok"] is False
with SessionLocal() as session:
    assert session.get(Customer, owner_id).wechat_openid == ""
    assert session.get(Customer, twin_id).wechat_openid == ""

# 重新发码后，同一手机号下两份未绑定档案一起绑上
_age(120)
main_module._BIND_PHONE_LIMIT = 20
main_module._BIND_IP_LIMIT = 50
sent = client.post("/api/customer-binding/send-code", json={"phone": "13700001111"})
assert sent.json()["ok"] is True, sent.text
bound = client.post("/api/customer-binding/verify", json={
    "phone": "13700001111", "code": SENT["code"], "openid": "openid-stranger", "js_code": "owner-code",
})
assert bound.json()["ok"] is True, bound.text
assert bound.json()["bound_count"] == 2
assert set(bound.json()["customer_ids"]) == {owner_id, twin_id}
assert "甲路" not in bound.text
with SessionLocal() as session:
    assert session.get(Customer, owner_id).wechat_openid == "openid-owner"
    assert session.get(Customer, twin_id).wechat_openid == "openid-owner"
    assert session.query(Customer).count() == 5
    audit = session.query(AuditLog).filter(AuditLog.action == "customer_binding").one()
    assert "13700001111" not in (audit.detail or "")

replay = client.post("/api/customer-binding/verify", json={
    "phone": "13700001111", "code": SENT["code"], "js_code": "second-code",
})
assert replay.json()["ok"] is False

# 已绑其他微信的档案拒绝覆盖；同号里还能绑的继续绑
with SessionLocal() as session:
    row = session.get(Customer, twin_id)
    row.wechat_openid = "openid-taken"
    session.commit()
_age(7200)
resent = client.post("/api/customer-binding/send-code", json={"phone": "13700001111"})
assert resent.json()["ok"] is True, resent.text
partial = client.post("/api/customer-binding/verify", json={
    "phone": "13700001111", "code": SENT["code"], "js_code": "owner-code",
})
assert partial.json()["ok"] is True, partial.text
assert partial.json()["bound_count"] == 1
assert partial.json()["skipped_count"] == 1
assert "前台" in partial.json()["message"]
with SessionLocal() as session:
    assert session.get(Customer, owner_id).wechat_openid == "openid-owner"
    assert session.get(Customer, twin_id).wechat_openid == "openid-taken"

_age(7200)
only_blocked = client.post("/api/customer-binding/send-code", json={"phone": "13700002222"})
assert only_blocked.json()["ok"] is True
refused = client.post("/api/customer-binding/verify", json={
    "phone": "13700002222", "code": SENT["code"], "js_code": "owner-code",
})
assert refused.json() == {"ok": False, "error": "该档案已绑定其他微信，请联系前台协助解绑。"}
with SessionLocal() as session:
    assert session.get(Customer, blocked_id).wechat_openid == "openid-taken"

# 过期
_age(7200)
fresh = client.post("/api/customer-binding/send-code", json={"phone": "13700003333"})
assert fresh.json()["ok"] is True
with SessionLocal() as session:
    row = session.query(SmsVerificationCode).filter(
        SmsVerificationCode.phone == "13700003333", SmsVerificationCode.used == False,  # noqa: E712
    ).one()
    row.expires_at = main_module._bind_now() - timedelta(seconds=1)
    session.commit()
expired = client.post("/api/customer-binding/verify", json={
    "phone": "13700003333", "code": SENT["code"], "js_code": "owner-code",
})
assert expired.json()["error"] == "验证码已失效，请重新获取"

# 短信失败：明确报错，日志里没有验证码
_age(7200)
SENT["fail"] = True
SENT["fail_with_code"] = True
LOGS.clear()
failed = client.post("/api/customer-binding/send-code", json={"phone": "13700003333"})
assert failed.json() == {"ok": False, "error": "短信发送失败，请稍后再试"}
assert SENT["code"] not in failed.text
assert SENT["code"] not in "\n".join(LOGS)
assert any("短信发送失败" in line for line in LOGS)
SENT["fail"] = False
SENT["fail_with_code"] = False

# 每号、每 IP 限额。IP 用 X-Real-IP，不认 X-Forwarded-For。
_age(7200)
main_module._BIND_COOLDOWN = timedelta(0)
main_module._BIND_PHONE_LIMIT = 2
main_module._BIND_IP_LIMIT = 50
assert client.post("/api/customer-binding/send-code", json={"phone": "13900000001"}).json()["ok"] is True
assert client.post("/api/customer-binding/send-code", json={"phone": "13900000001"}).json()["ok"] is True
phone_limited = client.post("/api/customer-binding/send-code", json={"phone": "13900000001"})
assert phone_limited.json()["ok"] is False
assert "频繁" in phone_limited.json()["error"]

_age(7200)
main_module._BIND_PHONE_LIMIT = 20
main_module._BIND_IP_LIMIT = 2
assert client.post(
    "/api/customer-binding/send-code",
    json={"phone": "13900000011"},
    headers={"X-Real-IP": "203.0.113.8", "X-Forwarded-For": "198.51.100.1"},
).json()["ok"] is True
assert client.post(
    "/api/customer-binding/send-code",
    json={"phone": "13900000012"},
    headers={"X-Real-IP": "203.0.113.8", "X-Forwarded-For": "198.51.100.2"},
).json()["ok"] is True
ip_limited = client.post(
    "/api/customer-binding/send-code",
    json={"phone": "13900000013"},
    headers={"X-Real-IP": "203.0.113.8", "X-Forwarded-For": "203.0.113.9"},
)
assert ip_limited.json()["ok"] is False
other_ip = client.post(
    "/api/customer-binding/send-code",
    json={"phone": "13900000014"},
    headers={"X-Real-IP": "203.0.113.9", "X-Forwarded-For": "203.0.113.8"},
)
assert other_ip.json()["ok"] is True
main_module._BIND_COOLDOWN = timedelta(seconds=60)
main_module._BIND_PHONE_LIMIT = 5
main_module._BIND_IP_LIMIT = 30

# 后台和员工小程序都能解绑，并留下审计
token = _login()
unbind = client.post(f"/admin/customers/{blocked_id}/unbind-wechat", data={"csrf_token": token})
assert unbind.status_code == 303
assert "unbind-wechat" not in (unbind.headers.get("location") or "")
with SessionLocal() as session:
    assert session.get(Customer, blocked_id).wechat_openid == ""
    admin_audit = session.query(AuditLog).filter(AuditLog.action == "customer_unbind_wechat").one()
    assert admin_audit.actor == "bind_staff"
    assert "openid-taken" not in (admin_audit.detail or "")
_logout()

with SessionLocal() as session:
    session.get(Customer, blocked_id).wechat_openid = "openid-taken"
    session.commit()
staff_unbind = client.post(
    f"/api/staff-miniapp/customers/{blocked_id}/unbind-wechat",
    headers={"Authorization": "Bearer staff-token"},
)
assert staff_unbind.status_code == 200, staff_unbind.text
assert staff_unbind.json()["wechat_bound"] is False
with SessionLocal() as session:
    assert session.get(Customer, blocked_id).wechat_openid == ""
    staff_audit = (
        session.query(AuditLog)
        .filter(AuditLog.action == "customer_unbind_wechat")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert "staff_miniapp" in (staff_audit.detail or "")
anon_unbind = client.post(f"/api/staff-miniapp/customers/{blocked_id}/unbind-wechat")
assert anon_unbind.status_code == 401

# 个人资料接口：没有微信登录码就不能看，也只能看自己的档案
profile_spoof = client.get("/api/wechat/my-profile", params={"openid": "openid-owner"})
assert profile_spoof.status_code == 200
assert profile_spoof.json()["bound"] is False
assert "更新" in profile_spoof.json()["message"]
assert "甲客户" not in profile_spoof.text and "甲路" not in profile_spoof.text and "甲犬" not in profile_spoof.text
stranger_profile = client.get("/api/wechat/my-profile", params={"code": "stranger-code"})
assert stranger_profile.json()["bound"] is False
assert "甲犬" not in stranger_profile.text
owner_profile = client.get("/api/wechat/my-profile", params={"code": "owner-code"})
assert owner_profile.status_code == 200
body = owner_profile.json()
assert body["bound"] is True
assert [row["id"] for row in body["customers"]] == [owner_id]
assert [pet["name"] for pet in body["pets"]] == ["甲犬"]
assert "甲猫" not in owner_profile.text
assert "甲路" not in owner_profile.text

status_anon = client.get(f"/api/app/{app_id}/status")
assert status_anon.status_code == 403
assert "更新" in status_anon.json()["detail"]
assert "甲路" not in status_anon.text and "咳嗽" not in status_anon.text
status_stranger = client.get(f"/api/app/{app_id}/status", params={"code": "stranger-code"})
assert status_stranger.status_code == 403
assert "咳嗽" not in status_stranger.text
status_owner = client.get(f"/api/app/{app_id}/status", params={"code": "owner-code"})
assert status_owner.status_code == 200
assert status_owner.json()["address"] == "甲路1号"
assert status_owner.json()["health_note"] == "咳嗽保密"
apps_spoof = client.post("/api/wechat/my-apps", json={"openid": "openid-owner"})
assert apps_spoof.status_code == 403
assert "更新" in apps_spoof.json()["detail"]
assert "咳嗽" not in apps_spoof.text
apps_owner = client.post("/api/wechat/my-apps", json={"code": "owner-code", "openid": "openid-stranger"})
assert apps_owner.status_code == 200
assert apps_owner.json()["items"][0]["health_note_brief"].startswith("咳嗽")

appt_spoof = client.post(f"/api/wechat/appointments/{appt_id}/get", json={"openid": "openid-owner"})
assert appt_spoof.status_code == 403
assert "更新" in appt_spoof.json()["detail"]
assert "13700001111" not in appt_spoof.text and "甲客户" not in appt_spoof.text
appt_stranger = client.post(f"/api/wechat/appointments/{appt_id}/get", json={"code": "stranger-code"})
assert appt_stranger.status_code == 403
appt_owner = client.post("/api/wechat/my-appointments", json={"code": "owner-code"})
assert appt_owner.status_code == 200
assert appt_owner.json()["items"][0]["customer_name"] == "甲客户"
assert appt_owner.json()["items"][0]["phone_masked"].endswith("1111")
assert "13700001111" not in appt_owner.text
tnr_spoof = client.get("/api/wechat/my-tnr-status", params={"openid": "openid-owner"})
assert tnr_spoof.status_code == 200
assert tnr_spoof.json()["has_approved"] is False
assert "甲猫进度" not in tnr_spoof.text

reg_phone = client.get(f"/api/vaccine-registration/{reg_id}", params={"phone": "13700001111"})
assert reg_phone.status_code == 403
assert "更新" in reg_phone.json()["detail"]
assert "护理说明" not in reg_phone.text and "甲犬" not in reg_phone.text
reg_stranger = client.get(f"/api/vaccine-registration/{reg_id}", params={"code": "stranger-code"})
assert reg_stranger.status_code == 403
assert "护理说明" not in reg_stranger.text and "甲客户" not in reg_stranger.text
reg_owner = client.get(f"/api/vaccine-registration/{reg_id}", params={"code": "owner-code"})
assert reg_owner.status_code == 200
assert reg_owner.json()["pet_name"] == "甲犬"
assert reg_owner.json()["post_care_notice"] == "护理说明秘密"

ban_anon = client.get("/api/tnr-store-status", params={"phone": ban_phone})
assert ban_anon.status_code == 200
assert ban_anon.json()["is_banned"] is False
assert not ban_anon.json()["ban_until"]
assert "封禁客户" not in ban_anon.text
assert ban_anon.json()["stores"]
ban_stranger = client.get("/api/tnr-store-status", params={"phone": ban_phone, "code": "stranger-code"})
assert ban_stranger.json()["is_banned"] is False
ban_owner = client.get("/api/tnr-store-status", params={"phone": ban_phone, "code": "banned-code"})
assert ban_owner.json()["is_banned"] is True
assert ban_owner.json()["ban_until"]
_login()
ban_staff = client.get("/api/tnr-store-status", params={"phone": ban_phone})
assert ban_staff.json()["is_banned"] is True
status_staff = client.get(f"/api/app/{app_id}/status")
assert status_staff.status_code == 200
assert status_staff.json()["applicant_name"] == "甲客户"
reg_staff = client.get(f"/api/vaccine-registration/{reg_id}")
assert reg_staff.status_code == 200
_logout()

# 旧版顾客端：只带 openid 仍能提交和取消 TNR，但不能把档案读回去
os.environ["LEGACY_MINIAPP_COMPAT"] = "1"
with SessionLocal() as session:
    row = session.get(Application, app_id)
    row.status = "approved"
    row.cat_nickname = "甲猫进度"
    session.commit()
legacy_tnr = client.get("/api/wechat/my-tnr-status", params={"openid": "openid-owner"})
assert legacy_tnr.status_code == 200, legacy_tnr.text
assert legacy_tnr.json()["has_approved"] is True
assert legacy_tnr.json()["approved_apps"][0]["id"] == app_id
assert legacy_tnr.json()["approved_apps"][0]["cat_nickname"] == ""
assert "甲猫进度" not in legacy_tnr.text
assert "甲路" not in legacy_tnr.text
book_day = (datetime.now().date() + timedelta(days=3)).isoformat()
legacy_book = client.post("/api/appointments/create", json={
    "openid": "openid-owner",
    "category": "tnr",
    "service_name": "TNR 手术安排",
    "customer_name": "甲客户",
    "phone": "13700001111",
    "pet_name": "甲犬",
    "pet_gender": "female",
    "store": "大风动物医院（横岗店）",
    "appointment_date": book_day,
    "appointment_time": "13:00",
    "duration_minutes": 60,
    "related_application_id": app_id,
})
assert legacy_book.status_code == 200, legacy_book.text
legacy_id = legacy_book.json()["appointment"]["id"]
legacy_cancel = client.post(f"/api/wechat/appointments/{legacy_id}/cancel", json={"openid": "openid-owner"})
assert legacy_cancel.status_code == 200, legacy_cancel.text
assert legacy_cancel.json()["ok"] is True
assert "甲客户" not in legacy_cancel.text
legacy_list = client.post("/api/wechat/my-appointments", json={"openid": "openid-owner"})
assert legacy_list.status_code == 403
assert "更新" in legacy_list.json()["detail"]
assert "甲客户" not in legacy_list.text

_age(7200)
main_module._BIND_COOLDOWN = timedelta(0)
assert client.post("/api/customer-binding/send-code", json={"phone": "13700003333"}).json()["ok"] is True
legacy_bind = client.post("/api/customer-binding/verify", json={
    "phone": "13700003333", "code": SENT["code"], "openid": "openid-owner",
})
assert legacy_bind.json()["ok"] is True, legacy_bind.text
with SessionLocal() as session:
    assert session.get(Customer, plain_id).wechat_openid == "openid-owner"
main_module._BIND_COOLDOWN = timedelta(seconds=60)

os.environ["LEGACY_MINIAPP_COMPAT"] = "0"
blocked_book = client.post("/api/appointments/create", json={
    "openid": "openid-owner",
    "category": "tnr",
    "service_name": "TNR 手术安排",
    "customer_name": "甲客户",
    "phone": "13700001111",
    "pet_name": "甲犬",
    "pet_gender": "female",
    "store": "大风动物医院（横岗店）",
    "appointment_date": (datetime.now().date() + timedelta(days=4)).isoformat(),
    "appointment_time": "14:00",
    "duration_minutes": 60,
    "related_application_id": app_id,
})
assert blocked_book.status_code == 401
assert "甲客户" not in blocked_book.text
os.environ["LEGACY_MINIAPP_COMPAT"] = "1"

client.close()
TEMP_DIR.cleanup()
print("customer binding auth test passed")
