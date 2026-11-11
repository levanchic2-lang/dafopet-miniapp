"""未登录的手机号查档不得返回客户个人信息；员工会话和已验证微信仍可看自己的档案。"""

import os
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-customer-lookup-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["UPLOAD_DIR"] = str(Path(TEMP_DIR.name) / "uploads")
os.environ["SESSION_SECRET"] = "customer-lookup-auth-test"
os.environ["SESSION_HTTPS_ONLY"] = "0"

from fastapi.testclient import TestClient
from passlib.hash import bcrypt

import app.main as main_module
from app.database import Base, SessionLocal, engine
from app.main import _COMBO_QUESTION_LABELS, app
from app.models import (
    AdminUser,
    ComboVaccineRegistration,
    ConsentTemplate,
    Customer,
    Pet,
)


@asynccontextmanager
async def _test_lifespan(_app):
    yield


app.router.lifespan_context = _test_lifespan
Base.metadata.create_all(bind=engine)

PII_KEYS = {"customer_id", "name", "name_invalid", "address", "duplicate_count", "pets"}


def _fake_code2session(code: str) -> dict:
    mapping = {
        "owner-code": "openid-owner",
        "stranger-code": "openid-stranger",
        "staff-code": "openid-staff",
    }
    if code not in mapping:
        raise RuntimeError("bad code")
    return {"openid": mapping[code]}


main_module.wechat_code2session = _fake_code2session

db = SessionLocal()
owner = Customer(name="甲客户", phone="13700001111", address="甲路1号", wechat_openid="openid-owner")
other = Customer(name="乙客户", phone="13700001111", address="乙路2号", wechat_openid="")
extra = Customer(name="备用号客户", phone="13700002222", phones_extra="13700003333", address="丙路3号")
plain = Customer(name="未绑定客户", phone="13700004444", address="丁路9号")
named_pet_owner = Customer(name="王瑞", phone="13700005555", address="戊路5号")
staff = AdminUser(
    username="lookup_staff",
    password_hash=bcrypt.hash("test123456"),
    role="staff",
    store="横岗店",
    display_name="查档员工",
    is_active=True,
)
db.add_all([owner, other, extra, plain, named_pet_owner, staff])
db.flush()
db.add_all([
    Pet(customer_id=owner.id, name="甲犬", species="dog", breed="柯基", gender="male", store="横岗店"),
    Pet(customer_id=other.id, name="乙猫", species="cat", breed="英短", gender="female", store="东环店"),
    Pet(customer_id=extra.id, name="丙猫", species="cat", store="横岗店"),
    Pet(customer_id=plain.id, name="丁狗", species="dog", breed="柴犬", store="横岗店"),
    Pet(customer_id=named_pet_owner.id, name="Biscuit", species="dog", breed="柴犬", store="横岗店"),
    ConsentTemplate(name="疫苗接种同意书", category="vaccination", body_html="<p>同意</p>", is_active=True),
])
db.commit()
owner_id = owner.id
other_id = other.id
named_pet_owner_id = named_pet_owner.id
biscuit_id = db.query(Pet).filter(Pet.customer_id == named_pet_owner.id).one().id
db.close()

client = TestClient(app, follow_redirects=False)


def _assert_redacted(body: dict, *, found: bool, bound: bool) -> None:
    assert body["found"] is found
    assert body["bound"] is bound
    if found:
        assert body["redacted"] is True
    assert PII_KEYS.isdisjoint(body)
    blob = str(body)
    for secret in ("甲客户", "乙客户", "备用号客户", "未绑定客户", "王瑞", "甲路", "乙路", "丙路", "丁路", "戊路", "甲犬", "乙猫", "丙猫", "丁狗", "Biscuit", "柯基"):
        assert secret not in blob


def _csrf() -> str:
    html = client.get("/admin/login").text
    return html.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]


def _login() -> None:
    response = client.post("/admin/login", data={
        "username": "lookup_staff",
        "password": "test123456",
        "csrf_token": _csrf(),
    }, follow_redirects=False)
    assert response.status_code == 303, response.text


def _logout() -> None:
    response = client.get("/admin/logout", follow_redirects=False)
    assert response.status_code == 303, response.text


# ── 未登录：只剩是否存在、是否已绑定 ──
missing_resp = client.get("/api/customer/lookup", params={"phone": "13900000000"})
assert missing_resp.headers["cache-control"] == "private, no-store"
missing = missing_resp.json()
_assert_redacted(missing, found=False, bound=False)

short = client.get("/api/customer/lookup", params={"phone": "123"}).json()
_assert_redacted(short, found=False, bound=False)

plain_hit = client.get("/api/customer/lookup", params={"phone": "13700004444"}).json()
_assert_redacted(plain_hit, found=True, bound=False)

bound_hit = client.get("/api/customer/lookup", params={"phone": "13700001111"}).json()
_assert_redacted(bound_hit, found=True, bound=True)

extra_hit = client.get("/api/customer/lookup", params={"phone": "13700003333"}).json()
_assert_redacted(extra_hit, found=True, bound=False)

# 直接塞 openid 不能换到档案；必须是当次 wx.login code 换出的 openid
spoof = client.get("/api/customer/lookup", params={"phone": "13700001111", "openid": "openid-owner"}).json()
_assert_redacted(spoof, found=True, bound=True)

bad_code = client.get("/api/customer/lookup", params={"phone": "13700001111", "code": "nope"}).json()
_assert_redacted(bad_code, found=True, bound=True)

stranger = client.get("/api/customer/lookup", params={"phone": "13700001111", "code": "stranger-code"}).json()
_assert_redacted(stranger, found=True, bound=True)

owned = client.get("/api/customer/lookup", params={"phone": "13700001111", "code": "owner-code"}).json()
assert owned["found"] is True and owned["redacted"] is False
assert owned["name"] == "甲客户"
assert owned["address"] == "甲路1号"
assert owned["customer_id"] == owner_id
assert [row["name"] for row in owned["pets"]] == ["甲犬"]
assert "乙客户" not in str(owned)
assert "乙猫" not in str(owned)
assert "乙路" not in str(owned)

bogus_bearer = client.get(
    "/api/customer/lookup",
    params={"phone": "13700004444"},
    headers={"Authorization": "Bearer not-a-real-token"},
).json()
_assert_redacted(bogus_bearer, found=True, bound=False)

# 同名宠物不再回显名字拒收，提交成功后仍只有一条档案
questionnaire = {key: {"value": "no", "detail": ""} for key in _COMBO_QUESTION_LABELS}
created = client.post("/api/vaccine-registration/create", json={
    "phone": "13700005555",
    "pet_id": 0,
    "owner_name": "王瑞",
    "pet_name": "biscuit",
    "pet_species": "dog",
    "clinic_store": "横岗店",
    "immunization_stage": "annual",
    "requested_date": "2026-09-26",
    "questionnaire": questionnaire,
})
assert created.status_code == 200, created.text
assert created.json()["pet_name"] == "Biscuit"
with SessionLocal() as check:
    pets = check.query(Pet).filter(Pet.customer_id == named_pet_owner_id).all()
    assert [pet.id for pet in pets] == [biscuit_id]
    reg = check.query(ComboVaccineRegistration).filter(ComboVaccineRegistration.customer_id == named_pet_owner_id).one()
    assert reg.pet_id == biscuit_id

# ── 后台会话与员工小程序令牌：完整档案，含同号的另一条 ──
_login()
session_hit = client.get("/api/customer/lookup", params={"phone": "13700001111"}).json()
assert session_hit["redacted"] is False
assert session_hit["duplicate_count"] == 2
assert {row["name"] for row in session_hit["pets"]} == {"甲犬", "乙猫"}
assert session_hit["customer_id"] in {owner_id, other_id}
extra_full = client.get("/api/customer/lookup", params={"phone": "13700003333"}).json()
assert extra_full["name"] == "备用号客户"
assert [row["name"] for row in extra_full["pets"]] == ["丙猫"]
_logout()

login = client.post("/api/staff-miniapp/login", json={
    "username": "lookup_staff",
    "password": "test123456",
    "code": "staff-code",
})
assert login.status_code == 200, login.text
token = login.json()["token"]
staff_hit = client.get(
    "/api/customer/lookup",
    params={"phone": "13700004444"},
    headers={"Authorization": f"Bearer {token}"},
).json()
assert staff_hit["redacted"] is False
assert staff_hit["name"] == "未绑定客户"
assert staff_hit["address"] == "丁路9号"
assert [row["name"] for row in staff_hit["pets"]] == ["丁狗"]

# ── 未登录限流；已登录员工不受这道限制 ──
main_module._CUSTOMER_LOOKUP_HITS.clear()
main_module._CUSTOMER_LOOKUP_IP_LIMIT = 2
main_module._CUSTOMER_LOOKUP_PHONE_LIMIT = 8
assert client.get("/api/customer/lookup", params={"phone": "13600000001"}).status_code == 200
assert client.get("/api/customer/lookup", params={"phone": "13600000002"}).status_code == 200
blocked = client.get("/api/customer/lookup", params={"phone": "13600000003"})
assert blocked.status_code == 429
assert blocked.json()["detail"] == "查询过于频繁，请稍后再试"
_login()
still = client.get("/api/customer/lookup", params={"phone": "13700004444"})
assert still.status_code == 200
assert still.json()["name"] == "未绑定客户"
_logout()
assert client.get("/api/customer/lookup", params={"phone": "13600000004"}).status_code == 429

main_module._CUSTOMER_LOOKUP_HITS.clear()
main_module._CUSTOMER_LOOKUP_IP_LIMIT = 100
main_module._CUSTOMER_LOOKUP_PHONE_LIMIT = 2
assert client.get("/api/customer/lookup", params={"phone": "13600000009"}).status_code == 200
assert client.get("/api/customer/lookup", params={"phone": "136-0000-0009"}).status_code == 200
phone_blocked = client.get("/api/customer/lookup", params={"phone": "13600000009"})
assert phone_blocked.status_code == 429
assert client.get("/api/customer/lookup", params={"phone": "13600000008"}).status_code == 200

# 限流只认 nginx 写入的 X-Real-IP，不认客户端自己写的 X-Forwarded-For 第一跳
main_module._CUSTOMER_LOOKUP_HITS.clear()
main_module._CUSTOMER_LOOKUP_IP_LIMIT = 1
main_module._CUSTOMER_LOOKUP_PHONE_LIMIT = 8
assert client.get(
    "/api/customer/lookup",
    params={"phone": "13600000021"},
    headers={"X-Forwarded-For": "198.51.100.1"},
).status_code == 200
spoofed_bucket = client.get(
    "/api/customer/lookup",
    params={"phone": "13600000022"},
    headers={"X-Forwarded-For": "198.51.100.2"},
)
assert spoofed_bucket.status_code == 429
main_module._CUSTOMER_LOOKUP_HITS.clear()
real_ip = client.get(
    "/api/customer/lookup",
    params={"phone": "13600000023"},
    headers={"X-Real-IP": "203.0.113.8", "X-Forwarded-For": "198.51.100.9"},
)
assert real_ip.status_code == 200
same_real = client.get(
    "/api/customer/lookup",
    params={"phone": "13600000024"},
    headers={"X-Real-IP": "203.0.113.8", "X-Forwarded-For": "203.0.113.1"},
)
assert same_real.status_code == 429
other_real = client.get(
    "/api/customer/lookup",
    params={"phone": "13600000025"},
    headers={"X-Real-IP": "203.0.113.9", "X-Forwarded-For": "203.0.113.8"},
)
assert other_real.status_code == 200

client.close()
TEMP_DIR.cleanup()
print("customer lookup auth test passed")
