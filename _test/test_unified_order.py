"""Unified ordering splits mixed items and updates inventory atomically."""

import json
import os
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB_PATH = Path(os.environ.get("UNIFIED_TEST_DB", str(ROOT / "_test" / "unified_order.db")))
if DB_PATH.exists():
    DB_PATH.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["ADMIN_PASSWORD"] = "unified-test-password"
os.environ["SESSION_SECRET"] = "unified-test-session-secret"

from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    Customer,
    DewormingRecord,
    ExamOrder,
    InventoryBatch,
    InventoryItem,
    Invoice,
    Pet,
    Prescription,
    SalesOrder,
    UnifiedOrderTemplate,
    Vaccination,
    Visit,
)


Base.metadata.create_all(bind=engine)
db = SessionLocal()
customer = Customer(name="统一开单测试", phone="13900003333")
db.add(customer)
db.flush()
pet = Pet(customer_id=customer.id, name="测试猫", species="cat", store="横岗店")
db.add(pet)
db.flush()
visit = Visit(customer_id=customer.id, pet_id=pet.id, visit_date="2026-09-06", store="横岗店", status="open")
db.add(visit)


def add_item(name, order_type, price, stock=0, category="medication", is_service=False):
    item = InventoryItem(
        name=name, order_type=order_type, sell_price=price, stock_qty=stock,
        category=category, unit="个", store="横岗店", is_service=is_service,
    )
    db.add(item)
    db.flush()
    return item


rx = add_item("测试处方药", "prescription", 2, 100)
exam = add_item("测试检查", "exam", 50, 0, "lab", True)
product = add_item("测试商品", "product", 20, 10, "product")
vaccine = add_item("测试猫三联", "vaccine", 80, 5, "vaccine")
deworm = add_item("测试驱虫", "deworming", 15, 10, "antiparasitic")
db.add(InventoryBatch(item_id=vaccine.id, batch_no="V202609", quantity=5, is_depleted=False))
db.commit()
visit_id = visit.id
pet_id = pet.id
ids = {"rx": rx.id, "exam": exam.id, "product": product.id, "vaccine": vaccine.id, "deworm": deworm.id}
db.close()

client = TestClient(app, base_url="https://testserver", follow_redirects=False)
login_page = client.get("/admin/login")
csrf = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
login = client.post("/admin/login", data={
    "username": "admin", "password": "unified-test-password", "csrf_token": csrf,
})
assert login.status_code == 303, f"login failed: {login.status_code} {login.text[:500]}"

page = client.get(f"/admin/visits/{visit_id}/unified-order")
assert page.status_code == 200
assert "服务项目 · 不计库存" in page.text
csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
rows = [
    {"item_id": ids["rx"], "order_type": "prescription", "quantity": 2, "unit_price": 2,
     "drug_type": "oral", "dose_amount": 1, "dose_unit": "个", "times_per_day": 2, "duration_days": 1},
    {"item_id": ids["exam"], "order_type": "exam", "quantity": 1, "unit_price": 50},
    {"item_id": ids["product"], "order_type": "product", "quantity": 1, "unit_price": 20},
    {"item_id": ids["vaccine"], "order_type": "vaccine", "quantity": 1, "unit_price": 80,
     "batch_no": "V202609", "dose_number": 1},
    {"item_id": ids["deworm"], "order_type": "deworming", "quantity": 2, "unit_price": 15,
     "deworm_type": "both"},
]
template_create = client.post("/api/unified-order-templates/create", json={
    "csrf_token": csrf, "name": "测试混合模板", "items": rows,
})
assert template_create.status_code == 200 and template_create.json()["ok"] is True
template_id = template_create.json()["id"]
template_get = client.get(f"/api/unified-order-templates/{template_id}")
assert template_get.status_code == 200 and len(template_get.json()["items"]) == 5
assert template_get.json()["items"][0]["unit_price"] == 2
response = client.post(f"/admin/visits/{visit_id}/unified-order", data={
    "csrf_token": csrf, "items_json": json.dumps(rows), "order_date": "2026-09-06", "vet_name": "测试医生",
})
assert response.status_code == 303, response.text
assert response.headers["location"].startswith(f"/admin/visits/{visit_id}?msg=")

db = SessionLocal()
assert db.query(Prescription).filter_by(visit_id=visit_id).count() == 1
assert db.query(ExamOrder).filter_by(visit_id=visit_id).count() == 1
assert db.query(SalesOrder).filter_by(visit_id=visit_id).count() == 1
assert db.query(Vaccination).filter_by(pet_id=pet_id).count() == 1
assert db.query(DewormingRecord).filter_by(pet_id=pet_id).count() == 1
assert db.query(UnifiedOrderTemplate).count() == 1
assert sorted(round(x.total_amount, 2) for x in db.query(Invoice).all()) == [30.0, 74.0, 80.0]
assert db.get(InventoryItem, ids["rx"]).stock_qty == 98
assert db.get(InventoryItem, ids["product"]).stock_qty == 9
assert db.get(InventoryItem, ids["vaccine"]).stock_qty == 4
assert db.get(InventoryItem, ids["deworm"]).stock_qty == 8
assert db.query(InventoryBatch).filter_by(item_id=ids["vaccine"]).one().quantity == 4
blocked = InventoryItem(
    name="零库存管控药", order_type="prescription", sell_price=10,
    stock_qty=0, category="medication", unit="支", store="横岗店",
    is_controlled=True,
)
db.add(blocked)
db.commit()
blocked_id = blocked.id
db.close()

# 第二次开单先扣普通药，再遇到管控药库存不足；整个请求必须回滚。
page = client.get(f"/admin/visits/{visit_id}/unified-order")
csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
failed_rows = [
    {"item_id": ids["rx"], "order_type": "prescription", "quantity": 1, "unit_price": 2},
    {"item_id": blocked_id, "order_type": "prescription", "quantity": 1, "unit_price": 10},
]
failed = client.post(f"/admin/visits/{visit_id}/unified-order", data={
    "csrf_token": csrf, "items_json": json.dumps(failed_rows), "order_date": "2026-09-06",
})
assert failed.status_code == 303
assert "err=" in failed.headers["location"]
db = SessionLocal()
assert db.query(Prescription).filter_by(visit_id=visit_id).count() == 1
assert db.get(InventoryItem, ids["rx"]).stock_qty == 98
db.close()
print("PASS: unified ordering")
