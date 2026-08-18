"""麻醉单收费同步的独立回归测试。"""

import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-anesthesia-invoice-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"

from app.database import Base, SessionLocal, engine
from app.main import _sync_visit_invoice
from app.models import (
    AnesthesiaOrder,
    AnesthesiaOrderItem,
    Customer,
    InvoiceItem,
    Pet,
    Visit,
)


Base.metadata.create_all(bind=engine)
db = SessionLocal()
try:
    customer = Customer(name="麻醉计费测试主人", phone="13900002222")
    db.add(customer)
    db.flush()
    pet = Pet(customer_id=customer.id, name="麻醉计费测试犬", species="dog", store="横岗店")
    db.add(pet)
    db.flush()
    visit = Visit(
        customer_id=customer.id,
        pet_id=pet.id,
        visit_date="2026-08-18",
        visit_type="surgery",
        status="open",
        vet_name="测试医生",
    )
    db.add(visit)
    db.flush()
    order = AnesthesiaOrder(
        visit_id=visit.id,
        customer_id=customer.id,
        pet_id=pet.id,
        anesth_date="2026-08-18",
        status="issued",
        store="横岗店",
        total_amount=180,
        created_by="test",
    )
    db.add(order)
    db.flush()
    db.add(AnesthesiaOrderItem(
        order_id=order.id,
        drug_name="吸入麻醉",
        total_qty=1,
        total_unit="次",
        unit_price=180,
        subtotal=180,
        is_service=True,
    ))
    db.commit()

    invoice = _sync_visit_invoice(db, visit.id, "test")
    db.commit()
    assert invoice is not None
    assert float(invoice.total_amount) == 180
    rows = db.query(InvoiceItem).filter(
        InvoiceItem.invoice_id == invoice.id,
        InvoiceItem.ref_type == "anesthesia",
        InvoiceItem.ref_id == order.id,
    ).all()
    assert len(rows) == 1
    assert rows[0].description == f"[麻醉#{order.id}] 吸入麻醉"

    invoice_again = _sync_visit_invoice(db, visit.id, "test")
    db.commit()
    assert invoice_again.id == invoice.id
    assert db.query(InvoiceItem).filter(
        InvoiceItem.invoice_id == invoice.id,
        InvoiceItem.ref_type == "anesthesia",
        InvoiceItem.ref_id == order.id,
    ).count() == 1
finally:
    db.close()
    engine.dispose()
    TEMP_DIR.cleanup()

print("PASS: anesthesia order invoice sync")
