"""Rabies registration uses one signature for registration and vaccine consent."""

import base64
import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-rabies-consent-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["SESSION_SECRET"] = "rabies-consent-test-session"

from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    ConsentAuditLog,
    ConsentTask,
    ConsentTemplate,
    Customer,
    Pet,
    RabiesVaccineRecord,
)
from app.services import consent_pdf


Base.metadata.create_all(bind=engine)
consent_pdf.generate_consent_pdf = lambda db, task_id: ("consent_pdfs/test.pdf", None)

with SessionLocal() as db:
    template = ConsentTemplate(
        name="疫苗接种同意书",
        category="vaccination",
        body_html="<p>{{cust_name}}同意为{{pet_name}}接种疫苗。</p>",
        is_active=True,
    )
    customer = Customer(name="测试主人", phone="13900008888", address="深圳市龙岗区")
    db.add_all([template, customer])
    db.flush()
    pet = Pet(
        customer_id=customer.id,
        name="测试犬",
        species="dog",
        breed="混种犬",
        store="横岗店",
        medical_record_no="HC-TEST-CONSENT",
    )
    db.add(pet)
    db.commit()
    customer_id, pet_id = customer.id, pet.id

client = TestClient(app, base_url="https://testserver", follow_redirects=False)
fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 900
signature = "data:image/png;base64," + base64.b64encode(fake_png).decode("ascii")
payload = {
    "owner_name": "测试主人",
    "owner_phone": "13900008888",
    "owner_address": "深圳市龙岗区",
    "animal_name": "测试犬",
    "animal_breed": "混种犬",
    "animal_dob": "2024-01",
    "animal_gender": "male",
    "animal_color": "黄白",
    "clinic_store": "横岗店",
    "owner_signature": signature,
    "customer_id": customer_id,
    "pet_id": pet_id,
}

missing_consent = client.post("/api/rabies/submit", json=payload)
assert missing_consent.status_code == 400
assert "同意" in missing_consent.json()["detail"]

payload["vaccine_consent_accepted"] = True
created = client.post("/api/rabies/submit", json=payload)
assert created.status_code == 200, created.text

with SessionLocal() as db:
    record = db.get(RabiesVaccineRecord, created.json()["id"])
    assert record and record.consent_task_id
    task = db.get(ConsentTask, record.consent_task_id)
    assert task and task.status == "signed"
    assert task.customer_id == customer_id and task.pet_id == pet_id
    assert "测试主人" in task.snapshot_html and "测试犬" in task.snapshot_html
    assert "{{cust_name}}" not in task.snapshot_html
    assert task.signature_path.startswith("consent_signatures/")
    assert db.query(ConsentAuditLog).filter_by(task_id=task.id, event="sign_success").count() == 1
    signature_path = ROOT / "uploads" / task.signature_path
    rabies_signature_path = ROOT / record.owner_signature_path

done = client.get(f"/rabies/done?id={created.json()['id']}")
assert done.status_code == 200
assert "疫苗接种同意书均已签署" in done.text
assert "请截图保留以下内容" in done.text

client.close()
for path in (signature_path, rabies_signature_path):
    if path.exists():
        path.unlink()
TEMP_DIR.cleanup()
print("rabies consent regression test passed")
