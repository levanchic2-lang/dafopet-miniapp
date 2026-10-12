"""钱包直接充值 / 撤销 / 报表的无端口冒烟测试。"""
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


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="wallet-smoke-", dir=Path(__file__).parent) as tmp:
        db_path = Path(tmp) / "wallet.db"
        os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"
        os.environ["UPLOAD_DIR"] = str(Path(tmp) / "uploads")
        os.environ["ADMIN_PASSWORD"] = "test123456"
        os.environ["SESSION_SECRET"] = "wallet-smoke-test-secret"

        from fastapi.testclient import TestClient
        from app.database import SessionLocal
        from app.main import app
        from app.models import Customer, InvoiceItem, Wallet, WalletTransaction

        with TestClient(app) as client:
            login_page = client.get("/admin")
            token = _csrf(login_page.text)
            login = client.post("/admin/login", data={
                "username": "admin", "password": "test123456", "csrf_token": token,
            }, follow_redirects=False)
            assert login.status_code == 303

            db = SessionLocal()
            customer = Customer(name="钱包测试客户", phone="13900009999")
            db.add(customer)
            db.commit()
            customer_id = customer.id
            db.close()

            page = client.get(f"/admin/customers/{customer_id}?tab=wallet")
            token = _csrf(page.text)
            response = client.post(f"/admin/wallets/{customer_id}/recharge", data={
                "csrf_token": token,
                "amount": "500",
                "bonus": "50",
                "pay_method": "wechat",
                "ref_no": "WX-TEST-001",
                "note": "冒烟测试充值",
            }, follow_redirects=False)
            assert response.status_code == 303

            db = SessionLocal()
            wallet = db.query(Wallet).filter(Wallet.customer_id == customer_id).one()
            recharge = db.query(WalletTransaction).filter(
                WalletTransaction.wallet_id == wallet.id,
                WalletTransaction.type == "recharge",
            ).one()
            assert wallet.balance == 550
            assert wallet.balance_principal == 500
            assert wallet.balance_bonus == 50
            assert recharge.invoice_id is None
            assert recharge.pay_method == "wechat"
            assert recharge.ref_no == "WX-TEST-001"
            assert db.query(InvoiceItem).filter(InvoiceItem.ref_type == "wallet_recharge").count() == 0
            recharge_id = recharge.id
            db.close()

            page = client.get(f"/admin/customers/{customer_id}?tab=wallet")
            token = _csrf(page.text)
            response = client.post(f"/admin/wallet-transactions/{recharge_id}/reverse", data={
                "csrf_token": token, "note": "冒烟测试撤销",
            }, follow_redirects=False)
            assert response.status_code == 303

            db = SessionLocal()
            wallet = db.query(Wallet).filter(Wallet.customer_id == customer_id).one()
            recharge = db.get(WalletTransaction, recharge_id)
            reversal = db.query(WalletTransaction).filter(
                WalletTransaction.reversal_of_id == recharge_id,
                WalletTransaction.type == "reversal",
            ).one()
            assert wallet.balance == 0
            assert wallet.balance_principal == 0
            assert wallet.balance_bonus == 0
            assert wallet.lifetime_recharge == 0
            assert recharge.status == "reversed"
            assert reversal.amount == -550
            assert reversal.bonus_amount == -50
            db.close()

            report = client.get("/admin/reports/revenue?preset=today")
            assert report.status_code == 200
            assert "钱包充值净额" in report.text
            assert "钱包退款 / 充值撤销" in report.text

    print("wallet recharge smoke: ok")


if __name__ == "__main__":
    main()
