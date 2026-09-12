"""Manual rabies entry creates and deletes linked vaccination/inventory atomically."""

import os
import re
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-rabies-manual-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["ADMIN_PASSWORD"] = "rabies-manual-test-password"
os.environ["SESSION_SECRET"] = "rabies-manual-test-session"

from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    AuditLog,
    Customer,
    InventoryBatch,
    InventoryItem,
    InventoryTransaction,
    Pet,
    RabiesVaccineRecord,
    Vaccination,
)


Base.metadata.create_all(bind=engine)
with SessionLocal() as db:
    customer = Customer(name="测试主人", phone="13900009999", address="测试地址")
    db.add(customer)
    db.flush()
    pet = Pet(
        customer_id=customer.id,
        name="A",
        species="dog",
        breed="测试犬",
        store="横岗店",
        medical_record_no="HC-TEST-RABIES",
    )
    item = InventoryItem(
        name="测试狂犬疫苗",
        category="vaccine",
        subcategory="rabies",
        unit="头份",
        stock_qty=3,
        store="横岗店",
        is_active=True,
        is_service=False,
    )
    db.add_all([pet, item])
    db.flush()
    batch = InventoryBatch(item_id=item.id, batch_no="RABIES-TEST", quantity=3, is_depleted=False)
    db.add(batch)
    db.commit()
    customer_id, pet_id, item_id, batch_id = customer.id, pet.id, item.id, batch.id

client = TestClient(app, base_url="https://testserver", follow_redirects=False)
login_page = client.get("/admin/login")
csrf = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
login = client.post("/admin/login", data={
    "username": "admin",
    "password": "rabies-manual-test-password",
    "csrf_token": csrf,
})
assert login.status_code == 303

list_page = client.get("/admin/rabies?tab=rabies")
assert list_page.status_code == 200
assert "人工补录" in list_page.text

new_page = client.get("/admin/rabies/new")
assert new_page.status_code == 200
csrf = re.search(r'name="csrf_token" value="([^"]+)"', new_page.text).group(1)
created = client.post("/admin/rabies/new", data={
    "csrf_token": csrf,
    "customer_id": customer_id,
    "pet_id": pet_id,
    "owner_name": "测试主人",
    "owner_phone": "13900009999",
    "owner_address": "测试地址",
    "animal_name": "A",
    "animal_breed": "测试犬",
    "animal_gender": "female",
    "cert_no": "D-TEST-0001",
    "vaccine_date": "2026-09-12",
    "vaccine_manufacturer": "测试狂犬疫苗",
    "vaccine_batch_no": "RABIES-TEST",
    "staff_name": "测试医生",
    "inventory_item_id": item_id,
})
assert created.status_code == 303, created.text[:500]

with SessionLocal() as db:
    record = db.query(RabiesVaccineRecord).filter_by(cert_no="D-TEST-0001").one()
    vaccination = db.query(Vaccination).filter_by(rabies_record_id=record.id).one()
    assert record.customer_id == customer_id and record.pet_id == pet_id
    assert vaccination.pet_id == pet_id and vaccination.batch_no == "RABIES-TEST"
    assert db.get(InventoryItem, item_id).stock_qty == 2
    assert db.get(InventoryBatch, batch_id).quantity == 2
    assert db.query(InventoryTransaction).filter_by(
        ref_type="vaccination", ref_id=vaccination.id, tx_type="out"
    ).count() == 1
    assert db.query(AuditLog).filter_by(action="rabies_manual_create").count() == 1
    record_id, vaccination_id = record.id, vaccination.id

deleted = client.post(f"/admin/rabies/{record_id}/delete", data={"csrf_token": csrf})
assert deleted.status_code == 303

with SessionLocal() as db:
    assert db.get(RabiesVaccineRecord, record_id) is None
    assert db.get(Vaccination, vaccination_id) is None
    assert db.get(InventoryItem, item_id).stock_qty == 3
    assert db.get(InventoryBatch, batch_id).quantity == 3

client.close()
TEMP_DIR.cleanup()
print("manual rabies entry regression test passed")
