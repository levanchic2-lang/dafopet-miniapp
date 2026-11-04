"""到店挂号：时段边界、收费单、无需就诊、取消、转病历、门店隔离。"""

import os
import re
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-walk-in-registration-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["SESSION_SECRET"] = "walk-in-registration-test"
os.environ["SESSION_HTTPS_ONLY"] = "0"

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.database import SessionLocal, _try_sqlite_migrations, engine, init_db
from app.main import _pwd_ctx, _sync_visit_invoice, app
from app.models import (
    AdminUser,
    AuditLog,
    Customer,
    Invoice,
    InvoiceItem,
    Pet,
    WalkInRegistration,
)
from app.services import registration as registration_svc
from app.time_utils import BEIJING_TZ, beijing_now as real_beijing_now


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    if not match:
        match = re.search(r'value="([^"]+)"[^>]*name="csrf_token"', html)
    assert match, html[:800]
    return match.group(1)


def fee_input(html: str) -> str:
    match = re.search(r'id="fee-input"[^>]*value="([^"]+)"', html)
    assert match, html
    return match.group(1)


def kind_input(html: str) -> str:
    match = re.search(r'id="kind-input"\s+value="([^"]+)"', html)
    assert match, html
    return match.group(1)


BOUNDARY = [
    (8, 59, "emergency", 200.0),
    (9, 0, "normal", 10.0),
    (21, 59, "normal", 10.0),
    (22, 0, "emergency", 200.0),
    (23, 30, "emergency", 200.0),
    (0, 30, "emergency", 200.0),
]


def test_kind_boundaries():
    for hour, minute, expected, _fee in BOUNDARY:
        aware = datetime(2026, 6, 15, hour, minute, tzinfo=BEIJING_TZ)
        naive = datetime(2026, 6, 15, hour, minute)
        assert registration_svc.registration_kind(aware) == expected, (hour, minute, "aware")
        assert registration_svc.registration_kind(naive) == expected, (hour, minute, "naive")
    assert registration_svc.registration_kind(datetime(2026, 6, 15, 9, 0, 30)) == "normal"
    assert registration_svc.registration_kind(datetime(2026, 6, 15, 21, 59, 59)) == "normal"
    assert registration_svc.registration_kind(datetime(2026, 6, 15, 22, 0, 1)) == "emergency"
    assert registration_svc.registration_kind(
        datetime(2026, 6, 15, 21, 0), day_start="09:00", day_end="21:00"
    ) == "emergency"
    assert registration_svc.registration_kind(
        datetime(2026, 6, 15, 20, 59), day_start="09:00", day_end="21:00"
    ) == "normal"


def freeze(hour: int, minute: int):
    moment = datetime(2026, 6, 15, hour, minute, tzinfo=BEIJING_TZ)
    registration_svc.beijing_now = lambda: moment
    return moment


def restore_clock():
    registration_svc.beijing_now = real_beijing_now


def login(client: TestClient, username: str):
    client.get("/admin/logout", follow_redirects=False)
    page = client.get("/admin/login")
    assert page.status_code == 200, page.status_code
    resp = client.post("/admin/login", data={
        "username": username,
        "password": "test123456",
        "csrf_token": csrf_from(page.text),
    }, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    assert "/admin/login" not in resp.headers.get("location", "")


def post_registration(client: TestClient, pet_id: int, **fields):
    page = client.get(f"/admin/registrations/new?pet_id={pet_id}")
    assert page.status_code == 200, page.text[:500]
    data = {
        "csrf_token": csrf_from(page.text),
        "pet_id": str(pet_id),
        "kind": fields.get("kind", ""),
        "fee": fields.get("fee", ""),
        "note": fields.get("note", ""),
        "store": fields.get("store", ""),
    }
    resp = client.post("/admin/registrations/create", data=data, follow_redirects=False)
    assert resp.status_code == 303, resp.text[:800]
    return resp


def latest_registration() -> WalkInRegistration:
    db = SessionLocal()
    try:
        reg = db.query(WalkInRegistration).order_by(WalkInRegistration.id.desc()).first()
        assert reg is not None
        db.expunge(reg)
        return reg
    finally:
        db.close()


test_kind_boundaries()
print("  时段函数边界通过")

client = TestClient(app, base_url="https://testserver", follow_redirects=False)
client.__enter__()
init_db()
password_hash = _pwd_ctx.hash("test123456")
with SessionLocal() as db:
    db.add(AdminUser(username="reg_super", password_hash=password_hash, role="superadmin", store=""))
    db.add(AdminUser(username="reg_donghuan", password_hash=password_hash, role="staff", store="东环店"))
    db.add(AdminUser(username="reg_henggang", password_hash=password_hash, role="staff", store="横岗店"))
    owner = Customer(name="挂号测试主人", phone="13800001111")
    other = Customer(name="横岗挂号主人", phone="13800002222")
    db.add(owner)
    db.add(other)
    db.flush()
    pet = Pet(customer_id=owner.id, name="挂号豆豆", species="cat", store="东环店")
    other_pet = Pet(customer_id=other.id, name="挂号年糕", species="cat", store="横岗店")
    db.add(pet)
    db.add(other_pet)
    db.commit()
    pet_id = pet.id
    other_pet_id = other_pet.id
    owner_id = owner.id

with engine.begin() as conn:
    apps_before = conn.execute(text("SELECT COUNT(*) FROM applications")).scalar()
    conn.execute(text("DROP TABLE IF EXISTS walk_in_registrations"))
    conn.execute(text("DROP TABLE IF EXISTS registration_settings"))
_try_sqlite_migrations()
_try_sqlite_migrations()
with engine.begin() as conn:
    assert conn.execute(text("SELECT COUNT(*) FROM applications")).scalar() == apps_before
    conn.execute(text("SELECT id, normal_fee, day_start FROM registration_settings")).fetchall()
    conn.execute(text("SELECT id, biz_date, kind, fee_amount, status FROM walk_in_registrations")).fetchall()
    conn.execute(text(
        "INSERT INTO walk_in_registrations (customer_id, pet_id, store, biz_date, kind, fee_amount, status) "
        "VALUES (:c, :p, '东环店', '2026-01-01', 'normal', 10, 'cancelled')"
    ), {"c": owner_id, "p": pet_id})
_try_sqlite_migrations()
init_db()
with engine.begin() as conn:
    kept = conn.execute(text(
        "SELECT COUNT(*) FROM walk_in_registrations WHERE biz_date = '2026-01-01'"
    )).scalar()
    assert kept == 1, kept
print("  迁移可重复执行，且不改动旧数据")

login(client, "reg_donghuan")
created_ids = []
for hour, minute, expected_kind, expected_fee in BOUNDARY:
    freeze(hour, minute)
    page = client.get(f"/admin/registrations/new?pet_id={pet_id}")
    assert page.status_code == 200, page.text[:400]
    assert kind_input(page.text) == expected_kind
    assert float(fee_input(page.text)) == expected_fee
    post_registration(client, pet_id, store="横岗店")
    reg = latest_registration()
    assert reg.kind == expected_kind, (hour, minute, reg.kind)
    assert abs(reg.fee_amount - expected_fee) < 0.001, (hour, minute, reg.fee_amount)
    assert reg.auto_kind == expected_kind
    assert abs(reg.auto_fee - expected_fee) < 0.001
    assert reg.store == "东环店", reg.store
    assert reg.biz_date == "2026-06-15"
    assert reg.status == "open"
    created_ids.append(reg.id)
restore_clock()

with SessionLocal() as db:
    rows = db.query(WalkInRegistration).filter(WalkInRegistration.id.in_(created_ids)).all()
    invoices = []
    for reg in rows:
        inv = db.get(Invoice, reg.invoice_id)
        assert inv is not None, reg.id
        assert inv.visit_id is None
        assert inv.store == "东环店"
        assert inv.payment_status == "unpaid"
        assert abs(inv.total_amount - reg.fee_amount) < 0.001
        item = db.query(InvoiceItem).filter(InvoiceItem.invoice_id == inv.id).one()
        assert item.ref_type == "registration"
        assert item.ref_id == reg.id
        assert "挂号" in (item.description or "")
        invoices.append(inv.id)
print("  六个时点的挂号费和独立收费单通过")

normal_id = created_ids[1]  # 09:00
with SessionLocal() as db:
    normal = db.get(WalkInRegistration, normal_id)
    invoice_id = normal.invoice_id
    assert normal.kind == "normal"

checkout = client.get(f"/admin/cashier/checkout?invoice_id={invoice_id}")
assert checkout.status_code == 200, checkout.text[:500]
assert "普通挂号" in checkout.text
assert "10.00" in checkout.text
cashier = client.get("/admin/invoices?status=unpaid")
assert cashier.status_code == 200
assert "挂号豆豆" in cashier.text or "挂号测试主人" in cashier.text
assert "10.00" in cashier.text

pay_page = client.get(f"/admin/invoices/{invoice_id}")
assert pay_page.status_code == 200
paid = client.post(f"/admin/invoices/{invoice_id}/add-payment", data={
    "csrf_token": csrf_from(pay_page.text),
    "method": "cash",
    "amount": "10",
}, follow_redirects=False)
assert paid.status_code == 303, paid.text[:500]
with SessionLocal() as db:
    inv = db.get(Invoice, invoice_id)
    assert inv.payment_status == "paid", inv.payment_status
revenue = client.get("/admin/reports/revenue?preset=today")
assert revenue.status_code == 200, revenue.text[:400]
assert "<td>挂号</td>" in revenue.text, "收款统计应按挂号分类"
print("  收银台和收款统计能看到挂号费")

freeze(10, 0)
post_registration(client, pet_id, fee="15", kind="normal")
adjusted = latest_registration()
assert abs(adjusted.fee_amount - 15) < 0.001
assert adjusted.auto_kind == "normal"
assert abs(adjusted.auto_fee - 10) < 0.001
with SessionLocal() as db:
    audits = db.query(AuditLog).filter(AuditLog.action == "registration_fee_adjust").all()
    assert any(str(adjusted.id) in (row.detail or "") for row in audits)
plain_before = db_count = None
restore_clock()
with SessionLocal() as db:
    plain_count = db.query(AuditLog).filter(AuditLog.action == "registration_fee_adjust").count()
freeze(11, 0)
post_registration(client, pet_id)
plain = latest_registration()
restore_clock()
with SessionLocal() as db:
    assert db.query(AuditLog).filter(AuditLog.action == "registration_fee_adjust").count() == plain_count
    assert plain.kind == "normal"
    assert abs(plain.fee_amount - 10) < 0.001
print("  改价写入审计，默认同价不记改价")

freeze(12, 0)
post_registration(client, pet_id)
no_visit = latest_registration()
restore_clock()
page = client.get("/admin/registrations?date=2026-06-15")
token = csrf_from(page.text)
resp = client.post(f"/admin/registrations/{no_visit.id}/no-visit", data={
    "csrf_token": token,
    "next_url": "/admin/registrations?date=2026-06-15",
}, follow_redirects=False)
assert resp.status_code == 303
followed = client.get(resp.headers["location"])
assert "无需就诊" in followed.text
with SessionLocal() as db:
    row = db.get(WalkInRegistration, no_visit.id)
    inv = db.get(Invoice, row.invoice_id)
    assert row.status == "no_visit"
    assert inv is not None and inv.payment_status == "unpaid"
    assert abs(inv.total_amount - 10) < 0.001
print("  无需就诊仍保留挂号费")

freeze(13, 0)
post_registration(client, pet_id)
cancelled = latest_registration()
restore_clock()
page = client.get("/admin/registrations?date=2026-06-15")
resp = client.post(f"/admin/registrations/{cancelled.id}/cancel", data={
    "csrf_token": csrf_from(page.text),
    "next_url": "/admin/registrations?date=2026-06-15",
}, follow_redirects=False)
assert resp.status_code == 303
followed = client.get(resp.headers["location"])
assert "已撤销" in followed.text or "取消" in followed.text
with SessionLocal() as db:
    row = db.get(WalkInRegistration, cancelled.id)
    assert row.status == "cancelled"
    assert row.invoice_id is None
    assert db.get(Invoice, cancelled.invoice_id) is None
    assert db.query(InvoiceItem).filter(
        InvoiceItem.ref_type == "registration",
        InvoiceItem.ref_id == cancelled.id,
    ).count() == 0
print("  未付款取消会撤掉挂号费")

freeze(14, 0)
post_registration(client, pet_id)
paid_reg = latest_registration()
restore_clock()
pay_page = client.get(f"/admin/invoices/{paid_reg.invoice_id}")
paid = client.post(f"/admin/invoices/{paid_reg.invoice_id}/add-payment", data={
    "csrf_token": csrf_from(pay_page.text),
    "method": "cash",
    "amount": "10",
}, follow_redirects=False)
assert paid.status_code == 303
page = client.get("/admin/registrations?date=2026-06-15")
blocked = client.post(f"/admin/registrations/{paid_reg.id}/cancel", data={
    "csrf_token": csrf_from(page.text),
}, follow_redirects=False)
assert blocked.status_code == 303
assert f"/admin/invoices/{paid_reg.invoice_id}" in blocked.headers.get("location", "")
with SessionLocal() as db:
    row = db.get(WalkInRegistration, paid_reg.id)
    inv = db.get(Invoice, paid_reg.invoice_id)
    assert row.status == "open"
    assert inv.payment_status == "paid"
print("  已收款取消不会删单，转到原有收费单")

freeze(15, 0)
post_registration(client, pet_id)
linked = latest_registration()
restore_clock()
page = client.get("/admin/registrations?date=2026-06-15")
converted = client.post(f"/admin/registrations/{linked.id}/convert", data={
    "csrf_token": csrf_from(page.text),
}, follow_redirects=False)
assert converted.status_code == 303
assert "/admin/visits/" in converted.headers.get("location", "")
with SessionLocal() as db:
    row = db.get(WalkInRegistration, linked.id)
    assert row.status == "converted"
    assert row.visit_id
    visit_id = row.visit_id
    from app.models import Visit
    visit = db.get(Visit, visit_id)
    assert visit.pet_id == pet_id
    assert visit.customer_id == owner_id
    assert visit.store == "东环店"
    assert visit.visit_date == "2026-06-15"
    _sync_visit_invoice(db, visit_id, "test")
    db.commit()
    inv = db.get(Invoice, linked.invoice_id)
    assert inv is not None and inv.visit_id is None
    assert abs(inv.total_amount - 10) < 0.001
    item = db.query(InvoiceItem).filter(InvoiceItem.invoice_id == inv.id).one()
    assert item.ref_type == "registration"
listed = client.get("/admin/registrations?date=2026-06-15")
assert listed.status_code == 200
assert "已转病历" in listed.text
assert f"/admin/visits/{visit_id}" in listed.text
print("  转病历会挂上病历，且病历同步不冲掉挂号费")

login(client, "reg_henggang")
hidden = client.get("/admin/registrations?date=2026-06-15")
assert hidden.status_code == 200
assert "挂号豆豆" not in hidden.text
forced = client.get("/admin/registrations?date=2026-06-15&store=东环店")
assert "挂号豆豆" not in forced.text
other_store_invoice = client.get("/admin/invoices?status=unpaid")
assert "挂号豆豆" not in other_store_invoice.text
denied = client.post(f"/admin/registrations/{linked.id}/no-visit", data={
    "csrf_token": csrf_from(hidden.text),
}, follow_redirects=False)
assert denied.status_code == 403
freeze(16, 0)
post_registration(client, other_pet_id, store="东环店")
heng_reg = latest_registration()
restore_clock()
assert heng_reg.store == "横岗店"
print("  员工只能看本店挂号，也不能改到别的店")

login(client, "reg_super")
all_rows = client.get("/admin/registrations?date=2026-06-15&store=all")
assert "挂号豆豆" in all_rows.text and "挂号年糕" in all_rows.text
only_dong = client.get("/admin/registrations?date=2026-06-15&store=东环店")
assert "挂号豆豆" in only_dong.text and "挂号年糕" not in only_dong.text
only_heng = client.get("/admin/registrations?date=2026-06-15&store=横岗店")
assert "挂号年糕" in only_heng.text and "挂号豆豆" not in only_heng.text
print("  超级管理员可以按门店切换")

login(client, "reg_donghuan")
staff_settings = client.post("/admin/registration-settings", data={
    "csrf_token": csrf_from(client.get("/admin/registrations").text),
    "normal_fee": "1",
    "emergency_fee": "2",
    "day_start": "09:00",
    "day_end": "22:00",
}, follow_redirects=False)
assert staff_settings.status_code == 403

login(client, "reg_super")
settings_page = client.get("/admin/registration-settings")
assert settings_page.status_code == 200
assert "普通挂号费" in settings_page.text
saved = client.post("/admin/registration-settings", data={
    "csrf_token": csrf_from(settings_page.text),
    "normal_fee": "10",
    "emergency_fee": "180",
    "day_start": "09:00",
    "day_end": "21:00",
}, follow_redirects=False)
assert saved.status_code == 303
freeze(21, 0)
post_registration(client, pet_id, store="东环店")
changed = latest_registration()
assert changed.kind == "emergency"
assert abs(changed.fee_amount - 180) < 0.001
assert changed.store == "东环店"
freeze(20, 59)
post_registration(client, pet_id, store="东环店")
still_day = latest_registration()
assert still_day.kind == "normal"
assert abs(still_day.fee_amount - 10) < 0.001
restore_clock()
with SessionLocal() as db:
    audit = db.query(AuditLog).filter(AuditLog.action == "registration_settings").count()
    assert audit >= 1
print("  挂号设置只有超级管理员能改，两店共用")

login(client, "reg_donghuan")
customer_page = client.get(f"/admin/customers/{owner_id}?pet_id={pet_id}")
assert customer_page.status_code == 200, customer_page.text[:400]
assert f"/admin/registrations/new?customer_id={owner_id}&pet_id={pet_id}" in customer_page.text
mobile_pet = client.get(f"/m/pet/{pet_id}")
assert mobile_pet.status_code == 200, mobile_pet.text[:400]
assert f"/m/registrations/new?pet_id={pet_id}" in mobile_pet.text
mobile_customer = client.get(f"/m/customer/{owner_id}")
assert mobile_customer.status_code == 200
assert "/m/registrations/new?customer_id=" in mobile_customer.text
mobile_list = client.get("/m/registrations?date=2026-06-15")
assert mobile_list.status_code == 200
assert "挂号豆豆" in mobile_list.text
assert "挂号年糕" not in mobile_list.text
hub = client.get("/m/medical")
assert "/m/registrations" in hub.text
home = client.get("/m")
assert "/m/registrations" in home.text
print("  桌面和手机入口都有挂号")

client.__exit__(None, None, None)
restore_clock()
TEMP_DIR.cleanup()
print("walk-in registration tests passed")
