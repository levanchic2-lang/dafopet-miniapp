"""Insurance claim marker and material worklist flow."""

import os
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB_PATH = Path(os.environ.get("INSURANCE_WORKLIST_TEST_DB", str(ROOT / "_test" / "insurance_worklist.db")))
if DB_PATH.exists():
    DB_PATH.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["ADMIN_PASSWORD"] = "insurance-test-password"
os.environ["SESSION_SECRET"] = "insurance-test-session-secret"

from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Customer, InsuranceMaterialShare, InsuranceMaterialSnapshot, Pet, Visit


Base.metadata.create_all(bind=engine)
db = SessionLocal()
customer = Customer(name="保险待办测试", phone="13900004444")
db.add(customer)
db.flush()
pet = Pet(customer_id=customer.id, name="待理赔猫", species="cat", store="横岗店")
db.add(pet)
db.flush()
visit = Visit(
    customer_id=customer.id,
    pet_id=pet.id,
    visit_date="2026-09-12",
    store="横岗店",
    vet_name="测试医生",
    status="closed",
)
db.add(visit)
db.commit()
visit_id = visit.id
customer_id = customer.id
pet_id = pet.id
db.close()

client = TestClient(app, base_url="https://testserver", follow_redirects=False)
login_page = client.get("/admin/login")
csrf = re.search(r'name="csrf_token" value="([^"]+)"', login_page.text).group(1)
login = client.post("/admin/login", data={
    "username": "admin", "password": "insurance-test-password", "csrf_token": csrf,
})
assert login.status_code == 303

visit_page = client.get(f"/admin/visits/{visit_id}")
assert visit_page.status_code == 200
assert "标记需报保险" in visit_page.text
csrf = re.search(r'name="csrf_token" value="([^"]+)"', visit_page.text).group(1)

marked = client.post(f"/admin/visits/{visit_id}/insurance-claim-needed", data={
    "csrf_token": csrf,
    "enabled": "1",
})
assert marked.status_code == 303

pending_page = client.get("/admin/insurance-materials?status=pending")
assert pending_page.status_code == 200
assert "保险待办测试" in pending_page.text
assert "待理赔猫" in pending_page.text
assert "待准备" in pending_page.text
assert client.get("/api/admin/insurance-materials/pending-count").json()["count"] == 1

db = SessionLocal()
share = InsuranceMaterialShare(
    token="insurance-worklist-test-token",
    customer_id=customer_id,
    pet_id=pet_id,
    visit_id=visit_id,
    title="测试保险材料包",
    status="active",
    generation_status="completed",
    store="横岗店",
)
db.add(share)
db.flush()
db.add(InsuranceMaterialSnapshot(
    share_id=share.id,
    version=1,
    manifest_json="[]",
    zip_path="test.zip",
    file_count=1,
))
db.commit()
db.close()

completed_page = client.get("/admin/insurance-materials?status=completed")
assert completed_page.status_code == 200
assert "保险待办测试" in completed_page.text
assert "已生成材料包" in completed_page.text
assert client.get("/api/admin/insurance-materials/pending-count").json()["count"] == 0

visit_page = client.get(f"/admin/visits/{visit_id}")
assert "已生成保险材料包，材料准备已完成" in visit_page.text

print("PASS: insurance material worklist")
