"""Sales order payment status follows all linked invoices without stock changes."""

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DB_PATH = ROOT / "_test" / "sales_order_payment_sync.db"
if DB_PATH.exists():
    DB_PATH.unlink()
os.environ["DATABASE_URL"] = "sqlite:///./_test/sales_order_payment_sync.db"

from app.database import Base, SessionLocal, _heal_sales_order_payment_statuses, engine
from app.main import _invoice_recompute_status
from app.models import (
    Customer,
    InventoryTransaction,
    Invoice,
    InvoiceItem,
    Payment,
    Pet,
    SalesOrder,
)


Base.metadata.create_all(bind=engine)
db = SessionLocal()
customer = Customer(name="销售状态测试", phone="13900002222")
db.add(customer)
db.flush()
pet = Pet(customer_id=customer.id, name="测试宠物", species="cat", store="横岗店")
db.add(pet)
db.flush()
order = SalesOrder(
    customer_id=customer.id,
    pet_id=pet.id,
    order_date="2026-08-13",
    status="pending",
    total_amount=100,
    created_by="test",
)
db.add(order)
db.flush()

invoices = []
for amount in (40, 60):
    inv = Invoice(
        invoice_no=f"TEST-{amount}",
        customer_id=customer.id,
        pet_id=pet.id,
        invoice_date="2026-08-13",
        subtotal=amount,
        total_amount=amount,
        payment_status="unpaid",
        store="横岗店",
        created_by="test",
    )
    db.add(inv)
    db.flush()
    db.add(InvoiceItem(
        invoice_id=inv.id,
        ref_type="sales_order",
        ref_id=order.id,
        description="测试商品",
        quantity=1,
        unit_price=amount,
        subtotal=amount,
    ))
    invoices.append(inv)
db.commit()

stock_tx_before = db.query(InventoryTransaction).count()

db.add(Payment(
    invoice_id=invoices[0].id,
    customer_id=customer.id,
    method="cash",
    amount=40,
    status="success",
    store="横岗店",
    operator="test",
))
db.flush()
_invoice_recompute_status(db, invoices[0])
db.commit()
assert db.get(SalesOrder, order.id).status == "pending"

second_payment = Payment(
    invoice_id=invoices[1].id,
    customer_id=customer.id,
    method="cash",
    amount=60,
    status="success",
    store="横岗店",
    operator="test",
)
db.add(second_payment)
db.flush()
_invoice_recompute_status(db, invoices[1])
db.commit()
assert db.get(SalesOrder, order.id).status == "paid"

second_payment.status = "cancelled"
db.flush()
_invoice_recompute_status(db, invoices[1])
db.commit()
assert db.get(SalesOrder, order.id).status == "pending"
assert db.query(InventoryTransaction).count() == stock_tx_before

# Startup repair: historical pending order + fully paid linked invoices.
invoices[1].payment_status = "paid"
db.get(SalesOrder, order.id).status = "pending"
db.commit()
_heal_sales_order_payment_statuses()
db.expire_all()
assert db.get(SalesOrder, order.id).status == "paid"
assert db.query(InventoryTransaction).count() == stock_tx_before

db.close()
print("PASS: sales order payment status sync")
