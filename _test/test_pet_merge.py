"""Regression test for merging duplicate pet profiles."""

import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-pet-merge-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"

from app.database import Base, SessionLocal, engine
from app.main import _find_customer_pet_by_name, _merge_pet_records, _pet_name_key
from app.models import (
    Customer,
    CustomerPackage,
    FollowUp,
    GroomingOrder,
    Invoice,
    PackageRedemption,
    Pet,
    Prescription,
    Vaccination,
    Visit,
)


Base.metadata.create_all(bind=engine)

with SessionLocal() as db:
    customer = Customer(name="颜春", phone="13802556539")
    db.add(customer)
    db.flush()

    target = Pet(
        customer_id=customer.id,
        name="cherry",
        breed="雪纳瑞",
        birthday_estimate="2024-09",
        medical_record_no="HC260600517",
    )
    source = Pet(
        customer_id=customer.id,
        name="Cherry",
        breed="雪纳瑞犬",
        birthday_estimate="2024-08-16",
        medical_record_no="HC260700077",
    )
    db.add_all([target, source])
    db.flush()
    source_id = source.id
    target_id = target.id

    visit = Visit(customer_id=customer.id, pet_id=source_id, visit_date="2026-08-20")
    invoice = Invoice(customer_id=customer.id, pet_id=source_id, invoice_no="TEST")
    package = CustomerPackage(customer_id=customer.id, pet_id=target_id, name="测试套餐")
    db.add_all([visit, invoice, package])
    db.flush()
    db.add_all([
        Prescription(customer_id=customer.id, pet_id=source_id, visit_id=visit.id),
        Vaccination(customer_id=customer.id, pet_id=source_id),
        PackageRedemption(
            customer_package_id=package.id,
            customer_id=customer.id,
            pet_id=source_id,
            visit_id=visit.id,
            invoice_id=invoice.id,
        ),
        FollowUp(visit_id=visit.id, customer_id=customer.id, pet_id=source_id),
        GroomingOrder(customer_id=customer.id, pet_id=source_id, invoice_id=invoice.id),
    ])
    db.commit()

    assert _pet_name_key("  Ｃherry ") == _pet_name_key("cherry")
    assert _find_customer_pet_by_name(db, customer.id, "CHERRY").id == target_id

    source = db.get(Pet, source_id)
    target = db.get(Pet, target_id)
    moved = _merge_pet_records(db, source, target)
    db.commit()
    db.expire_all()

    assert db.get(Pet, source_id) is None
    target = db.get(Pet, target_id)
    assert target.name == "cherry"
    assert target.medical_record_no == "HC260600517"
    assert target.birthday_estimate == "2024-08-16"
    assert moved["visits"] == 1
    assert moved["prescriptions"] == 1
    assert moved["invoices"] == 1
    assert moved["vaccinations"] == 1
    assert moved["package_redemptions"] == 1
    assert moved["follow_ups"] == 1
    assert moved["grooming_orders"] == 1

    for model in (Visit, Prescription, Invoice, Vaccination, PackageRedemption, FollowUp, GroomingOrder):
        assert db.query(model).filter(model.pet_id == source_id).count() == 0
        assert db.query(model).filter(model.pet_id == target_id).count() == 1

TEMP_DIR.cleanup()
print("pet merge regression test passed")
