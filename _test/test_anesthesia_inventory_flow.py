"""麻醉实际给药闭环的独立路由测试。"""

import os
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "_test" / "anesthesia_flow.db"
if DB_PATH.exists():
    DB_PATH.unlink()
os.environ["DATABASE_URL"] = "sqlite:///./_test/anesthesia_flow.db"

from fastapi.testclient import TestClient
from passlib.hash import bcrypt

from app import models  # noqa: F401
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    AdminUser,
    AnesthesiaMedicationEvent,
    AnesthesiaMonitorSheet,
    AnesthesiaOpenVial,
    Customer,
    InventoryBatch,
    InventoryItem,
    InventoryTransaction,
    InvoiceItem,
    NarcoticsLedger,
    Pet,
    Visit,
)


def csrf(html: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert match, f"页面缺少 CSRF token: {html[:500]}"
    return match.group(1)


Base.metadata.create_all(bind=engine)
db = SessionLocal()
admin = AdminUser(
    username="anesthesia_admin",
    password_hash=bcrypt.hash("test123456"),
    role="superadmin",
    store="",
    is_active=True,
)
customer = Customer(name="麻醉测试主人", phone="13900001111")
db.add_all([admin, customer]); db.flush()
pet = Pet(customer_id=customer.id, name="麻醉测试犬", species="dog", store="横岗店")
db.add(pet); db.flush()
visit = Visit(
    customer_id=customer.id, pet_id=pet.id, visit_date="2026-08-13",
    visit_type="surgery", status="open", vet_name="测试医生",
)
item = InventoryItem(
    name="测试丙泊酚注射液", category="medication", subcategory="controlled",
    is_controlled=True, is_service=False, unit="ml", unit2="瓶",
    unit2_ratio=20, stock_qty=20, manufacturer="测试生产企业",
    store="横岗店", created_by="test",
)
db.add_all([visit, item]); db.flush()
batch = InventoryBatch(
    item_id=item.id, batch_no="PROP-TEST-01", quantity=20,
    expiry_date="2027-12-31", received_date="2026-08-13",
)
sheet = AnesthesiaMonitorSheet(
    visit_id=visit.id, customer_id=customer.id, pet_id=pet.id,
    monitor_date="2026-08-13", procedure="绝育术", store="横岗店",
    status="open", created_by="anesthesia_admin",
)
db.add_all([batch, sheet]); db.commit()
item_id, batch_id, sheet_id = item.id, batch.id, sheet.id
invoice_items_before = db.query(InvoiceItem).count()
db.close()

with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
    login = client.get("/admin/login")
    token = csrf(login.text)
    response = client.post("/admin/login", data={
        "username": "anesthesia_admin",
        "password": "test123456",
        "csrf_token": token,
    })
    assert response.status_code == 303, response.text

    page = client.get(f"/m/anesthesia-monitor/{sheet_id}")
    assert page.status_code == 200
    token = csrf(page.text)

    response = client.post(f"/admin/anesthesia-monitor/{sheet_id}/open-vial", data={
        "csrf_token": token,
        "next_url": f"/m/anesthesia-monitor/{sheet_id}",
        "item_id": item_id,
        "batch_id": batch_id,
        "opened_qty": "10",
        "notes": "测试开瓶",
    })
    assert response.status_code == 303, response.text

    db = SessionLocal()
    vial = db.query(AnesthesiaOpenVial).filter_by(opened_sheet_id=sheet_id).one()
    vial_id = vial.id
    assert db.get(InventoryItem, item_id).stock_qty == 20
    assert db.get(InventoryBatch, batch_id).quantity == 20
    db.close()

    response = client.post(f"/admin/anesthesia-monitor/{sheet_id}/medication", data={
        "csrf_token": token,
        "next_url": f"/m/anesthesia-monitor/{sheet_id}",
        "open_vial_id": vial_id,
        "qty": "3",
        "route": "IV",
        "time_hhmm": "10:15",
        "note": "首次诱导",
    })
    assert response.status_code == 303, response.text

    db = SessionLocal()
    event = db.query(AnesthesiaMedicationEvent).filter_by(
        sheet_id=sheet_id, event_type="administer"
    ).one()
    event_id = event.id
    assert event.review_status == "pending"
    assert db.get(InventoryItem, item_id).stock_qty == 17
    assert db.get(InventoryBatch, batch_id).quantity == 17
    assert db.get(AnesthesiaOpenVial, vial_id).used_qty == 3
    assert db.query(InvoiceItem).count() == invoice_items_before
    assert db.query(InventoryTransaction).filter_by(
        ref_type="anesthesia_monitor", ref_id=event_id
    ).count() == 1
    assert db.query(NarcoticsLedger).filter_by(
        medication_event_id=event_id, source="monitor_admin"
    ).count() == 1
    event.operator = "测试助理"
    db.commit(); db.close()

    review = client.get("/admin/anesthesia-review?date=2026-08-13&store=横岗店")
    assert review.status_code == 200, (review.status_code, review.headers.get("location"), review.text[:500])
    review_token = csrf(review.text)
    response = client.post("/admin/anesthesia-review/events", data={
        "csrf_token": review_token,
        "date": "2026-08-13",
        "store": "横岗店",
        "event_ids": str(event_id),
    })
    assert response.status_code == 303

    db = SessionLocal()
    assert db.get(AnesthesiaMedicationEvent, event_id).review_status == "reviewed"
    assert db.get(InventoryItem, item_id).stock_qty == 17
    db.close()

    response = client.post(f"/admin/anesthesia-medication-events/{event_id}/void", data={
        "csrf_token": review_token,
        "reason": "测试录入错误",
    })
    assert response.status_code == 303

    db = SessionLocal()
    assert db.get(AnesthesiaMedicationEvent, event_id).review_status == "voided"
    assert db.get(InventoryItem, item_id).stock_qty == 20
    assert db.get(InventoryBatch, batch_id).quantity == 20
    assert db.get(AnesthesiaOpenVial, vial_id).used_qty == 0
    db.close()

    response = client.post(f"/admin/anesthesia-monitor/{sheet_id}/medication", data={
        "csrf_token": review_token,
        "next_url": f"/m/anesthesia-monitor/{sheet_id}",
        "open_vial_id": vial_id,
        "qty": "3",
        "route": "IV",
        "time_hhmm": "10:20",
        "note": "重新记录实际诱导",
    })
    assert response.status_code == 303

    response = client.post(f"/admin/anesthesia/open-vials/{vial_id}/destroy", data={
        "csrf_token": review_token,
        "qty": "7",
        "cosigner": "测试助理",
        "reason": "下班前当日残余销毁",
    })
    assert response.status_code == 303

db = SessionLocal()
vial = db.get(AnesthesiaOpenVial, vial_id)
assert vial.status == "closed"
assert vial.used_qty == 3 and vial.destroyed_qty == 7
assert db.get(InventoryItem, item_id).stock_qty == 10
assert db.get(InventoryBatch, batch_id).quantity == 10
assert db.query(AnesthesiaMedicationEvent).filter_by(
    open_vial_id=vial_id, event_type="destroy"
).count() == 1
assert db.query(InvoiceItem).count() == invoice_items_before
db.close()

print("PASS: anesthesia actual-medication inventory flow")
