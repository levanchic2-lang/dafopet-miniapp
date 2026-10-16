"""A visit-linked hospitalization and its independent invoice belong in insurance materials."""

import json
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.database import Base
from app.models import Customer, Hospitalization, Invoice, Pet, Visit
from app.services import insurance_materials as materials


with tempfile.TemporaryDirectory(dir=ROOT / "_test") as temp_dir:
    engine = create_engine(f"sqlite:///{Path(temp_dir) / 'test.db'}")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    customer = Customer(name="保险住院测试", phone="13900000000")
    db.add(customer)
    db.flush()
    pet = Pet(customer_id=customer.id, name="北晨测试", species="cat", store="横岗店")
    db.add(pet)
    db.flush()
    visit = Visit(
        customer_id=customer.id,
        pet_id=pet.id,
        visit_date="2026-10-05",
        store="横岗店",
        status="closed",
    )
    db.add(visit)
    db.flush()
    direct_invoice = Invoice(
        invoice_no="DIRECT-1",
        customer_id=customer.id,
        pet_id=pet.id,
        visit_id=visit.id,
        invoice_date="2026-10-05",
        subtotal=100,
        total_amount=100,
        payment_status="paid",
    )
    inpatient_invoice = Invoice(
        invoice_no="INPATIENT-1",
        customer_id=customer.id,
        pet_id=pet.id,
        visit_id=None,
        invoice_date="2026-10-15",
        subtotal=800,
        total_amount=800,
        payment_status="paid",
    )
    db.add_all([direct_invoice, inpatient_invoice])
    db.flush()
    db.add(Hospitalization(
        customer_id=customer.id,
        pet_id=pet.id,
        visit_id=visit.id,
        invoice_id=inpatient_invoice.id,
        store="横岗店",
        reason="骨折住院",
        status="discharged",
        billing_mode="weight",
        billing_days=10,
        daily_rate_override=80,
        rate_label="猫住院",
    ))
    db.commit()

    original_upload_dir = materials.settings.upload_dir
    original_render_pdf = materials._render_pdf
    original_pdf_to_jpegs = materials._pdf_to_jpegs

    def fake_render_pdf(_templates, _template_name, _context, out_path, _db):
        out_path.write_bytes(b"fake pdf")

    def fake_pdf_to_jpegs(pdf_path, image_dir, stem, files, base_dir, source_kind, source_label, dpi=200):
        image_dir.mkdir(parents=True, exist_ok=True)
        image_path = image_dir / f"{stem}_第01页.jpg"
        image_path.write_bytes(b"fake jpg")
        materials._add_manifest_file(
            files, f"{source_kind}_image", f"{source_label} · 第 1 页（图片版）", image_path, base_dir,
        )

    manifest_json = "[]"
    try:
        materials.settings.upload_dir = temp_dir
        materials._render_pdf = fake_render_pdf
        materials._pdf_to_jpegs = fake_pdf_to_jpegs
        _, snapshot = materials.generate_insurance_material_snapshot(
            db=db,
            templates=object(),
            visit_id=visit.id,
            generated_by="test",
            visit_type_zh={},
            inv_status_zh={},
            inv_pay_zh={},
            detect_report_style=lambda _items: {},
            print_clinic_store=lambda _visit, _pet: "横岗店",
            payment_balance_hints=lambda _db, _payments: {},
        )
        manifest_json = snapshot.manifest_json
    finally:
        materials.settings.upload_dir = original_upload_dir
        materials._render_pdf = original_render_pdf
        materials._pdf_to_jpegs = original_pdf_to_jpegs
        db.close()
        engine.dispose()

    manifest = json.loads(manifest_json)
    labels = [row["label"] for row in manifest]
    invoice_labels = [row["label"] for row in manifest if row["kind"] == "invoice"]
    assert any("住院记录 IP" in label for label in labels), labels
    assert "收费单 DIRECT-1" in invoice_labels, invoice_labels
    assert "收费单 INPATIENT-1" in invoice_labels, invoice_labels
    assert len(invoice_labels) == 2, invoice_labels

print("PASS: visit-linked hospitalization and invoice are in insurance materials")
