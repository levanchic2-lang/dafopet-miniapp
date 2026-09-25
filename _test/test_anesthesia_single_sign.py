"""麻醉单只要求麻醉医师签署，特殊药品复核留在管控药流程。"""

import os
import re
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-anesthesia-single-sign-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"

from fastapi.testclient import TestClient
from passlib.hash import bcrypt

from app import models  # noqa: F401
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import AdminUser, AnesthesiaOrder, Customer, Pet, Visit


def csrf(html: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert match, f"页面缺少 CSRF token: {html[:500]}"
    return match.group(1)


Base.metadata.create_all(bind=engine)
db = SessionLocal()
try:
    admin = AdminUser(
        username="anesthesia_single_sign_admin",
        password_hash=bcrypt.hash("test123456"),
        role="superadmin",
        store="横岗店",
        is_active=True,
    )
    customer = Customer(name="麻醉单签测试主人", phone="13900003333")
    db.add_all([admin, customer])
    db.flush()
    pet = Pet(customer_id=customer.id, name="麻醉单签测试犬", species="dog", store="横岗店")
    db.add(pet)
    db.flush()
    visit = Visit(
        customer_id=customer.id,
        pet_id=pet.id,
        visit_date="2026-09-25",
        visit_type="surgery",
        status="open",
        vet_name="测试医生",
    )
    db.add(visit)
    db.commit()
    visit_id, customer_id, pet_id = visit.id, customer.id, pet.id
finally:
    db.close()

try:
    with TestClient(app, base_url="https://testserver", follow_redirects=False) as client:
        login = client.get("/admin/login")
        response = client.post("/admin/login", data={
            "username": "anesthesia_single_sign_admin",
            "password": "test123456",
            "csrf_token": csrf(login.text),
        })
        assert response.status_code == 303, response.text

        page = client.get(f"/admin/visits/{visit_id}/anesthesia/new")
        assert page.status_code == 200
        assert 'name="cosigner"' not in page.text
        assert "双 签 确 认" not in page.text

        response = client.post("/admin/anesthesia/create", data={
            "csrf_token": csrf(page.text),
            "visit_id": str(visit_id),
            "customer_id": str(customer_id),
            "pet_id": str(pet_id),
            "anesth_date": "2026-09-25",
            "vet_name": "测试医生",
            "drug_name[]": "吸入麻醉",
            "item_id[]": "0",
            "route[]": "吸入",
            "total_qty[]": "1",
            "total_unit[]": "次",
            "unit_price[]": "180",
            "is_service[]": "1",
        })
        assert response.status_code == 303, response.text

    db = SessionLocal()
    order = db.query(AnesthesiaOrder).one()
    assert order.vet_name == "测试医生"
    assert order.cosigner == ""
    assert order.total_amount == 180
    db.close()
finally:
    engine.dispose()
    TEMP_DIR.cleanup()

print("PASS: anesthesia order single-sign flow")
