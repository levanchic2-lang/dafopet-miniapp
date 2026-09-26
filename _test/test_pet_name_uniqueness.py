"""Pet names are unique per customer regardless of case and Unicode width."""

import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-pet-name-uniqueness-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["SESSION_SECRET"] = "pet-name-uniqueness-test"

from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine, _heal_rabies_pet_links
from app.main import app, _find_customer_pet_by_name, _pet_name_key
from app.models import Customer, Pet, RabiesVaccineRecord


Base.metadata.create_all(bind=engine)
with SessionLocal() as db:
    customer = Customer(name="王瑞", phone="13376181717")
    db.add(customer)
    db.flush()
    pet = Pet(
        customer_id=customer.id,
        name="Biscuit",
        species="dog",
        breed="柴犬",
        store="横岗店",
        medical_record_no="HC-TEST-BISCUIT",
    )
    db.add(pet)
    db.flush()
    db.add(RabiesVaccineRecord(
        customer_id=customer.id,
        pet_id=pet.id,
        owner_name=customer.name,
        owner_phone=customer.phone,
        animal_name="biscuit",
        animal_breed="柴犬",
        status="staff_pending",
    ))
    db.commit()
    customer_id, pet_id = customer.id, pet.id

    assert _pet_name_key("  ＢＩＳＣＵＩＴ  ") == _pet_name_key("biscuit")
    assert _find_customer_pet_by_name(db, customer_id, "bIsCuIt").id == pet_id

_heal_rabies_pet_links()
with SessionLocal() as db:
    assert db.query(Pet).filter(Pet.customer_id == customer_id).count() == 1
    assert db.query(RabiesVaccineRecord).one().pet_id == pet_id

client = TestClient(app, base_url="https://testserver", follow_redirects=False)
duplicate = client.post("/api/vaccine-registration/create", json={
    "phone": "13376181717",
    "pet_id": 0,
    "owner_name": "王瑞",
    "pet_name": "biscuit",
    "pet_species": "dog",
    "clinic_store": "横岗店",
    "immunization_stage": "annual",
    "requested_date": "2026-09-26",
    "questionnaire": {},
})
assert duplicate.status_code == 409, duplicate.text
assert "Biscuit" in duplicate.json()["detail"]

lookup = client.get("/api/customer/lookup", params={"phone": "13376181717"})
assert lookup.status_code == 200
assert [row["name"] for row in lookup.json()["pets"]] == ["Biscuit"]

with SessionLocal() as db:
    assert db.query(Pet).filter(Pet.customer_id == customer_id).count() == 1

client.close()
TEMP_DIR.cleanup()
print("pet name uniqueness regression test passed")
