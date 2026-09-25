"""其他宠物历史处方搜索与复制载荷回归测试。"""

import os
import re
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-prescription-copy-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"

from fastapi.testclient import TestClient
from passlib.hash import bcrypt

from app import models  # noqa: F401
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    AdminUser,
    Customer,
    InventoryItem,
    Pet,
    Prescription,
    PrescriptionItem,
)


def csrf(html: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert match, f"页面缺少 CSRF token: {html[:500]}"
    return match.group(1)


Base.metadata.create_all(bind=engine)
db = SessionLocal()
try:
    admin = AdminUser(
        username="prescription_copy_admin",
        password_hash=bcrypt.hash("test123456"),
        role="superadmin",
        store="横岗店",
        is_active=True,
    )
    target_owner = Customer(name="目标主人", phone="13900004444")
    source_owner = Customer(name="来源主人", phone="13900005555")
    other_store_owner = Customer(name="异店主人", phone="13900006666")
    db.add_all([admin, target_owner, source_owner, other_store_owner])
    db.flush()
    target_pet = Pet(customer_id=target_owner.id, name="目标犬", species="dog", store="横岗店")
    source_pet = Pet(customer_id=source_owner.id, name="处方来源犬", species="dog", store="横岗店")
    other_store_pet = Pet(customer_id=other_store_owner.id, name="东环来源犬", species="dog", store="东环店")
    db.add_all([target_pet, source_pet, other_store_pet])
    db.flush()
    henggang_item = InventoryItem(
        name="测试头孢", category="medication", subcategory="prescription",
        unit="片", stock_qty=100, sell_price=12, store="横岗店", is_active=True,
    )
    donghuan_item = InventoryItem(
        name="测试头孢", category="medication", subcategory="prescription",
        unit="片", stock_qty=100, sell_price=10, store="东环店", is_active=True,
    )
    db.add_all([henggang_item, donghuan_item])
    db.flush()
    source_presc = Prescription(
        customer_id=source_owner.id, pet_id=source_pet.id,
        prescribed_date="2026-09-20", vet_name="测试医生",
        status="issued", total_amount=36, created_by="test",
    )
    other_store_presc = Prescription(
        customer_id=other_store_owner.id, pet_id=other_store_pet.id,
        prescribed_date="2026-09-21", vet_name="东环医生",
        status="issued", total_amount=20, created_by="test",
    )
    db.add_all([source_presc, other_store_presc])
    db.flush()
    db.add_all([
        PrescriptionItem(
            prescription_id=source_presc.id, item_id=henggang_item.id,
            drug_name="测试头孢", drug_type="口服", dose_amount=1,
            dose_unit="片", times_per_day=2, duration_days="3",
            quantity_num=3, item_unit="片", unit_price=12, subtotal=36,
            instructions="饭后服用", print_note="如有呕吐请联系医院",
            schedule_times="8,20",
        ),
        PrescriptionItem(
            prescription_id=other_store_presc.id, item_id=donghuan_item.id,
            drug_name="测试头孢", drug_type="口服", quantity_num=2,
            item_unit="片", unit_price=10, subtotal=20,
        ),
    ])
    db.commit()
    target_pet_id = target_pet.id
    source_presc_id = source_presc.id
    other_store_presc_id = other_store_presc.id
    henggang_item_id = henggang_item.id
finally:
    db.close()

try:
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        login = client.get("/admin/login")
        response = client.post("/admin/login", data={
            "username": "prescription_copy_admin",
            "password": "test123456",
            "csrf_token": csrf(login.text),
        })
        assert response.status_code == 303, response.text

        response = client.get(
            "/api/prescriptions/copy-sources",
            params={"q": "处方来源犬", "target_pet_id": target_pet_id},
        )
        assert response.status_code == 200, response.text
        rows = response.json()["rows"]
        assert len(rows) == 1
        assert rows[0]["id"] == source_presc_id
        assert rows[0]["pet_name"] == "处方来源犬"
        assert "测试头孢" in rows[0]["item_summary"]

        response = client.get(
            f"/api/prescriptions/{source_presc_id}/copy-payload",
            params={"target_pet_id": target_pet_id},
        )
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["ok"] is True
        assert payload["pet_name"] == "处方来源犬"
        assert payload["warnings"] == []
        assert payload["items"][0]["item_id"] == henggang_item_id
        assert payload["items"][0]["times_per_day"] == 2
        assert payload["items"][0]["instructions"] == "饭后服用"
        assert payload["items"][0]["print_note"] == "如有呕吐请联系医院"
        assert payload["items"][0]["schedule_times"] == "8,20"

        response = client.get(
            "/api/prescriptions/copy-sources",
            params={"q": "东环来源犬", "target_pet_id": target_pet_id},
        )
        assert response.status_code == 200
        assert response.json()["rows"] == []

        response = client.get(
            f"/api/prescriptions/{other_store_presc_id}/copy-payload",
            params={"target_pet_id": target_pet_id},
        )
        assert response.status_code == 403
finally:
    engine.dispose()
    TEMP_DIR.cleanup()

print("PASS: cross-pet prescription copy")
