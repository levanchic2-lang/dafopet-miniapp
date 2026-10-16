"""收银台小数金额、折扣和团购支付的隔离冒烟测试。"""
from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert match, "页面缺少 CSRF token"
    return match.group(1)


def _new_invoice(db, invoice_cls, customer_id: int, number: str, amount: float):
    invoice = invoice_cls(
        customer_id=customer_id,
        invoice_no=number,
        invoice_date="2026-10-16",
        subtotal=amount,
        discount_amount=0.0,
        total_amount=amount,
        payment_status="unpaid",
        store="横岗店",
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)
    return invoice.id


def main() -> None:
    temp_parent = Path(__file__).parent if os.name == "nt" else None
    with tempfile.TemporaryDirectory(prefix="cashier-decimal-", dir=temp_parent) as tmp:
        db_path = Path(tmp) / "cashier.db"
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
        os.environ["UPLOAD_DIR"] = str(Path(tmp) / "uploads")
        os.environ["ADMIN_PASSWORD"] = "test123456"
        os.environ["SESSION_SECRET"] = "cashier-decimal-smoke-secret"

        from fastapi.testclient import TestClient
        from app.database import SessionLocal
        from app.main import app
        from app.models import Customer, Invoice, Payment

        with TestClient(app, base_url="https://testserver") as client:
            login_page = client.get("/admin/login")
            token = _csrf(login_page.text)
            login = client.post(
                "/admin/login",
                data={
                    "username": "admin",
                    "password": "test123456",
                    "csrf_token": token,
                },
                follow_redirects=False,
            )
            assert login.status_code == 303

            db = SessionLocal()
            customer = Customer(name="小数收款测试", phone="13900008888")
            db.add(customer)
            db.commit()
            customer_id = customer.id
            target_invoice_id = _new_invoice(db, Invoice, customer_id, "DEC-TARGET", 100.0)
            db.close()

            page = client.get(f"/admin/invoices/{target_invoice_id}")
            token = _csrf(page.text)
            response = client.post(
                f"/admin/invoices/{target_invoice_id}/apply-discount",
                data={
                    "csrf_token": token,
                    "mode": "target",
                    "discount_target": "79.90",
                },
                follow_redirects=False,
            )
            assert response.status_code == 303

            db = SessionLocal()
            target_invoice = db.get(Invoice, target_invoice_id)
            assert target_invoice.total_amount == 79.90
            assert target_invoice.discount_amount == 20.10
            db.close()

            response = client.post(
                f"/admin/invoices/{target_invoice_id}/add-payment",
                data={
                    "csrf_token": token,
                    "method": "third_party",
                    "amount": "79.90",
                    "ref_no": "GROUP-79.90",
                },
                follow_redirects=False,
            )
            assert response.status_code == 303

            db = SessionLocal()
            target_invoice = db.get(Invoice, target_invoice_id)
            target_payment = db.query(Payment).filter_by(invoice_id=target_invoice_id).one()
            assert target_invoice.payment_status == "paid"
            assert target_payment.amount == 79.90

            pct_invoice_id = _new_invoice(db, Invoice, customer_id, "DEC-PCT", 99.99)
            batch_invoice_id = _new_invoice(db, Invoice, customer_id, "DEC-BATCH", 33.33)
            db.close()

            page = client.get(f"/admin/invoices/{pct_invoice_id}")
            token = _csrf(page.text)
            response = client.post(
                f"/admin/invoices/{pct_invoice_id}/apply-discount",
                data={"csrf_token": token, "mode": "pct", "discount_pct": "88"},
                follow_redirects=False,
            )
            assert response.status_code == 303

            response = client.post(
                "/admin/invoices/batch-apply-discount",
                data={
                    "csrf_token": token,
                    "invoice_ids": str(batch_invoice_id),
                    "return_to": str(batch_invoice_id),
                    "mode": "pct",
                    "discount_pct": "88",
                },
                follow_redirects=False,
            )
            assert response.status_code == 303

            db = SessionLocal()
            assert db.get(Invoice, pct_invoice_id).total_amount == 87.99
            assert db.get(Invoice, batch_invoice_id).total_amount == 29.33
            db.close()

            checkout = client.get(
                f"/admin/cashier/checkout?invoice_id={pct_invoice_id}&scope=pet"
            )
            assert checkout.status_code == 200
            assert 'name="round_amount"' in checkout.text
            assert 'step="0.01"' in checkout.text
            assert "结果保留到分" in checkout.text
            token = _csrf(checkout.text)

            response = client.post(
                "/admin/cashier/multi-pay",
                data={
                    "csrf_token": token,
                    "customer_id": str(customer_id),
                    "pet_id": "0",
                    "invoice_ids": str(pct_invoice_id),
                    "method": "meituan",
                    "discount_mode": "none",
                    "round_amount": "12.34",
                    "ref_no": "MT-12.34",
                },
                follow_redirects=False,
            )
            assert response.status_code == 303

            db = SessionLocal()
            payments = db.query(Payment).filter_by(invoice_id=pct_invoice_id).all()
            assert len(payments) == 1
            assert payments[0].amount == 12.34
            assert db.get(Invoice, pct_invoice_id).payment_status == "unpaid"
            db.close()

            response = client.post(
                f"/admin/invoices/{pct_invoice_id}/add-payment",
                data={
                    "csrf_token": token,
                    "method": "wechat",
                    "amount": "75.65",
                },
                follow_redirects=False,
            )
            assert response.status_code == 303

            db = SessionLocal()
            payments = db.query(Payment).filter_by(invoice_id=pct_invoice_id).all()
            assert round(sum(p.amount for p in payments), 2) == 87.99
            assert db.get(Invoice, pct_invoice_id).payment_status == "paid"
            db.close()

    print("cashier decimal smoke: ok")


if __name__ == "__main__":
    main()
