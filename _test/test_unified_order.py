"""Unified ordering splits mixed items and updates inventory atomically."""

import json
import os
import re
import sys
from datetime import date
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
    ConsentTask,
    ConsentTemplate,
    CageRateRule,
    Customer,
    DewormingRecord,
    ExamOrder,
    Hospitalization,
    InventoryBatch,
    InventoryItem,
    Invoice,
    Pet,
    Prescription,
    SalesOrder,
    UnifiedOrderBatch,
    UnifiedOrderTemplate,
    Vaccination,
    Visit,
    WeightRecord,
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
db.add(ConsentTemplate(
    name="横岗店疫苗接种同意书", category="vaccination",
    body_html="<p>{{cust_name}}同意为{{pet_name}}接种疫苗。</p>", is_active=True,
))


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
inpatient = add_item("普通住院费", "inpatient", 30, 0, "nursing", True)
db.add(InventoryBatch(item_id=vaccine.id, batch_no="V202609", quantity=5, is_depleted=False))
db.commit()
visit_id = visit.id
pet_id = pet.id
customer_id = customer.id
ids = {"rx": rx.id, "exam": exam.id, "product": product.id, "vaccine": vaccine.id, "deworm": deworm.id, "inpatient": inpatient.id}
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
    {"item_id": ids["inpatient"], "order_type": "inpatient", "quantity": 3, "unit_price": 30},
]
template_create = client.post("/api/unified-order-templates/create", json={
    "csrf_token": csrf, "name": "测试混合模板", "items": rows,
})
assert template_create.status_code == 200 and template_create.json()["ok"] is True
template_id = template_create.json()["id"]
template_get = client.get(f"/api/unified-order-templates/{template_id}")
assert template_get.status_code == 200 and len(template_get.json()["items"]) == 6
assert template_get.json()["items"][0]["unit_price"] == 2
missing_vet = client.post(f"/admin/visits/{visit_id}/unified-order", data={
    "csrf_token": csrf, "items_json": json.dumps(rows[:1]), "order_date": "2026-09-06",
})
assert missing_vet.status_code == 303
assert "%E5%BC%80%E5%85%B7%E5%A4%84%E6%96%B9%E5%89%8D%E5%BF%85%E9%A1%BB%E9%80%89%E6%8B%A9%E5%8C%BB%E7%94%9F" in missing_vet.headers["location"]
db = SessionLocal()
assert db.query(Prescription).filter_by(visit_id=visit_id).count() == 0
db.close()
response = client.post(f"/admin/visits/{visit_id}/unified-order", data={
    "csrf_token": csrf, "items_json": json.dumps(rows), "order_date": "2026-09-06", "vet_name": "测试医生",
    "request_vaccine_consent": "1",
})
assert response.status_code == 303, response.text
assert response.headers["location"].startswith("/admin/unified-orders/")
batch_page = client.get(response.headers["location"])
assert batch_page.status_code == 200
assert "统一改单" in batch_page.text
assert "测试处方药" in batch_page.text
assert "测试检查" in batch_page.text
assert "测试商品" in batch_page.text
assert "测试猫三联" in batch_page.text
assert "测试驱虫" in batch_page.text
assert "普通住院费" in batch_page.text
visit_page = client.get(f"/admin/visits/{visit_id}")
assert visit_page.status_code == 200
assert "统一改单" in visit_page.text

db = SessionLocal()
assert db.query(Prescription).filter_by(visit_id=visit_id).count() == 1
assert db.query(ExamOrder).filter_by(visit_id=visit_id).count() == 1
assert db.query(SalesOrder).filter_by(visit_id=visit_id).count() == 1
first_vaccination = db.query(Vaccination).filter_by(pet_id=pet_id).one()
assert first_vaccination.consent_task_id is not None
assert db.query(ConsentTask).filter_by(pet_id=pet_id).count() == 1
assert db.query(DewormingRecord).filter_by(pet_id=pet_id).count() == 1
hospitalization = db.query(Hospitalization).filter_by(pet_id=pet_id, billing_mode="simple").one()
assert hospitalization.status == "discharged"
assert hospitalization.billing_days == 3
assert hospitalization.daily_rate_override == 30
assert hospitalization.invoice_id is not None
assert db.query(UnifiedOrderTemplate).count() == 1
assert db.query(UnifiedOrderBatch).filter_by(visit_id=visit_id).count() == 1
assert sorted(round(x.total_amount, 2) for x in db.query(Invoice).all()) == [30.0, 74.0, 80.0, 90.0]
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

# 改单后同日重开沿用原同意书，不新增任务，也不阻断疫苗记录保存。
direct_page = client.get(f"/admin/vaccinations/create?pet_id={pet_id}&customer_id={customer_id}")
assert direct_page.status_code == 200
assert "同时发起疫苗接种同意书" in direct_page.text
csrf_direct = re.search(r'name="csrf_token" value="([^"]+)"', direct_page.text).group(1)
reopened = client.post("/admin/vaccinations/create", data={
    "csrf_token": csrf_direct, "pet_id": pet_id, "customer_id": customer_id,
    "vaccine_type": "combo_3", "vaccine_name": "测试猫三联",
    "vaccinated_date": "2026-09-06", "dose_number": "1",
    "request_vaccine_consent": "1", "is_free": "1",
})
assert reopened.status_code == 303
assert reopened.headers["location"].startswith("/admin/vaccinations/")
db = SessionLocal()
vaccinations = db.query(Vaccination).filter_by(pet_id=pet_id).order_by(Vaccination.id).all()
assert len(vaccinations) == 2
assert vaccinations[0].consent_task_id == vaccinations[1].consent_task_id
assert db.query(ConsentTask).filter_by(pet_id=pet_id).count() == 1
consent = db.get(ConsentTask, vaccinations[0].consent_task_id)
consent.status = "signed"
consent_token = consent.token
db.commit()
db.close()
signed_page = client.get(f"/consent/{consent_token}")
assert signed_page.status_code == 200
assert "疫苗注射后的注意事项" in signed_page.text
assert "请截图保存" in signed_page.text

# 正式住院必须有关联到本病例的今日体重，且按体重规则锁定日费率。
inpatient_page = client.get(f"/admin/inpatient/new?pet_id={pet_id}&visit_id={visit_id}")
assert inpatient_page.status_code == 200
assert "暂不能办理住院" in inpatient_page.text
csrf_inpatient = re.search(r'name="csrf_token" value="([^"]+)"', inpatient_page.text).group(1)
db = SessionLocal()
hospitalization_count_before = db.query(Hospitalization).filter_by(visit_id=visit_id).count()
db.close()
blocked_admit = client.post("/admin/inpatient/admit", data={
    "csrf_token": csrf_inpatient, "visit_id": visit_id,
})
assert blocked_admit.status_code == 303 and "err=" in blocked_admit.headers["location"]
db = SessionLocal()
assert db.query(Hospitalization).filter_by(visit_id=visit_id).count() == hospitalization_count_before
db.add(WeightRecord(
    pet_id=pet_id, visit_id=visit_id, record_date=date.today().isoformat(),
    weight_kg=4.25, created_by="test",
))
db.add(CageRateRule(
    store="横岗店", label="猫住院", species="cat", min_weight_kg=0,
    max_weight_kg=None, daily_rate=35, is_active=True,
))
db.commit()
db.close()

ready_page = client.get(f"/admin/inpatient/new?visit_id={visit_id}")
assert ready_page.status_code == 200
assert "确认办理住院" in ready_page.text and "¥35.00" in ready_page.text
created = client.post("/admin/inpatient/admit", data={
    "csrf_token": csrf_inpatient, "visit_id": visit_id,
    "reason": "术后住院观察",
})
assert created.status_code == 303 and created.headers["location"].startswith("/admin/inpatient/")
hosp_id = int(created.headers["location"].split("/admin/inpatient/")[1].split("?")[0])
db = SessionLocal()
hosp = db.get(Hospitalization, hosp_id)
assert hosp.status == "admitted" and hosp.billing_mode == "weight"
assert hosp.admission_weight_kg == 4.25 and hosp.daily_rate_override == 35
assert hosp.invoice_id is None
boarding_pet = Pet(customer_id=customer_id, name="寄养测试猫", species="cat", store="横岗店")
db.add(boarding_pet)
db.flush()
db.add(WeightRecord(
    pet_id=boarding_pet.id, visit_id=None, record_date=date.today().isoformat(),
    weight_kg=3.8, created_by="test",
))
db.commit()
boarding_pet_id = boarding_pet.id
db.close()

boarding_page = client.get(f"/admin/inpatient/new?mode=boarding&pet_id={boarding_pet_id}")
assert boarding_page.status_code == 200
assert "寄养 / 单纯住院" in boarding_page.text and "确认办理住院" in boarding_page.text
boarding_created = client.post("/admin/inpatient/admit", data={
    "csrf_token": csrf_inpatient, "pet_id": boarding_pet_id,
    "admission_mode": "boarding", "reason": "单纯寄养，不用药",
})
assert boarding_created.status_code == 303
boarding_hosp_id = int(boarding_created.headers["location"].split("/admin/inpatient/")[1].split("?")[0])
db = SessionLocal()
boarding_hosp = db.get(Hospitalization, boarding_hosp_id)
assert boarding_hosp.status == "admitted" and boarding_hosp.visit_id is None
assert boarding_hosp.admission_weight_kg == 3.8 and boarding_hosp.daily_rate_override == 35
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
    "vet_name": "测试医生",
})
assert failed.status_code == 303
assert "err=" in failed.headers["location"]
db = SessionLocal()
assert db.query(Prescription).filter_by(visit_id=visit_id).count() == 1
assert db.get(InventoryItem, ids["rx"]).stock_qty == 98
assert db.query(UnifiedOrderBatch).filter_by(visit_id=visit_id).count() == 1
db.close()
print("PASS: unified ordering")
