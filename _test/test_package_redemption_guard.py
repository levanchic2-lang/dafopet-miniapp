"""预付套餐提醒与核销安全回归。"""

import os
import sys
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-package-guard-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["UPLOAD_DIR"] = str(Path(TEMP_DIR.name) / "uploads")
os.environ["SESSION_HTTPS_ONLY"] = "0"

from fastapi.testclient import TestClient
from passlib.hash import bcrypt
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
import app.main as main_module
from app.main import app
from app.models import (
    AdminUser, Customer, CustomerPackage, InventoryItem, Invoice, InvoiceItem,
    PackageApplicableItem, PackageBypassLog, PackageProduct, PackageRedemption,
    Payment, Pet, Vaccination,
)
from app.services.package_guard import (
    DuplicateSubmission, PackageGuardError, redeem_package, reverse_package_payment,
)
from app.time_utils import beijing_today


@asynccontextmanager
async def _test_lifespan(_app):
    yield


app.router.lifespan_context = _test_lifespan
Base.metadata.create_all(bind=engine)
client = TestClient(app)


def _loc(response) -> str:
    return unquote(response.headers.get("location", ""))


def _csrf(path: str) -> str:
    html = client.get(path).text
    token = html.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]
    return token


def _login():
    token = _csrf("/admin/login")
    response = client.post("/admin/login", data={
        "username": "pkg_admin", "password": "test123456", "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303, response.text


def _seed():
    db = SessionLocal()
    admin = AdminUser(
        username="pkg_admin", password_hash=bcrypt.hash("test123456"),
        role="superadmin", store="东环店", display_name="套餐管理员", is_active=True,
    )
    customer = Customer(name="套餐客户", phone="13900003333")
    db.add_all([admin, customer])
    db.flush()
    pet = Pet(customer_id=customer.id, name="豆豆", species="dog", store="东环店")
    other = Pet(customer_id=customer.id, name="咪咪", species="cat", store="东环店")
    vaccine = InventoryItem(name="犬四联疫苗", category="vaccine", store="东环店", stock_qty=8, sell_price=90, is_active=True)
    exam = InventoryItem(name="血常规", category="lab", store="东环店", stock_qty=3, sell_price=120, is_service=True, is_active=True)
    product = PackageProduct(name="三针疫苗卡", category="medical", total_uses=3, sell_price=240, unit_price=80, validity_days=365, store="", is_active=True)
    bath = PackageProduct(name="旧洗澡卡", category="bath", total_uses=5, sell_price=200, unit_price=40, validity_days=365, store="", is_active=True)
    store_card = PackageProduct(name="横岗疫苗卡", category="medical", total_uses=2, sell_price=160, unit_price=80, validity_days=365, store="横岗店", is_active=True)
    db.add_all([pet, other, vaccine, exam, product, bath, store_card])
    db.commit()
    ids = {
        "customer": customer.id, "pet": pet.id, "other": other.id,
        "vaccine_item": vaccine.id, "exam_item": exam.id,
        "product": product.id, "bath": bath.id, "store_product": store_card.id,
    }
    db.close()
    return ids


IDS = _seed()
_login()


def _card(db: Session, product_id: int, **kwargs) -> CustomerPackage:
    today = beijing_today().isoformat()
    card = CustomerPackage(
        customer_id=IDS["customer"],
        product_id=product_id,
        name=kwargs.pop("name", "三针疫苗卡"),
        total_uses=kwargs.pop("total_uses", 3),
        used_count=kwargs.pop("used_count", 0),
        sell_price=240,
        unit_price=80,
        purchase_date=today,
        expires_at=kwargs.pop("expires_at", (beijing_today() + timedelta(days=30)).isoformat()),
        status=kwargs.pop("status", "active"),
        store=kwargs.pop("store", "东环店"),
        pet_id=kwargs.pop("pet_id", IDS["pet"]),
    )
    db.add(card)
    db.flush()
    return card


def _invoice(db: Session, lines: list[tuple]) -> Invoice:
    today = beijing_today().isoformat()
    total = round(sum(line[5] for line in lines), 2)
    invoice = Invoice(
        invoice_no="PKG" + uuid.uuid4().hex[:8],
        customer_id=IDS["customer"], pet_id=IDS["pet"],
        invoice_date=today, subtotal=total, total_amount=total,
        payment_status="unpaid", store="东环店",
    )
    db.add(invoice)
    db.flush()
    for description, ref_type, ref_id, qty, unit, subtotal in lines:
        db.add(InvoiceItem(
            invoice_id=invoice.id, description=description, ref_type=ref_type, ref_id=ref_id,
            quantity=qty, unit_price=unit, subtotal=subtotal,
        ))
    db.flush()
    return invoice


def _vaccine_line(db: Session):
    vacc = Vaccination(
        pet_id=IDS["pet"], customer_id=IDS["customer"], vaccine_name="犬四联疫苗",
        inventory_item_id=IDS["vaccine_item"], vaccinated_date=beijing_today().isoformat(),
    )
    db.add(vacc)
    db.flush()
    return ("犬四联疫苗", "vaccination", vacc.id, 1, 90, 90)


def test_mapped_partial_and_ordinary_gate():
    db = SessionLocal()
    db.add(PackageApplicableItem(product_id=IDS["product"], inventory_item_id=IDS["vaccine_item"]))
    card = _card(db, IDS["product"])
    invoice = _invoice(db, [_vaccine_line(db), ("血常规", "manual", None, 1, 120, 120)])
    vaccine_line = next(line for line in invoice.items if line.ref_type == "vaccination")
    stock_before = db.get(InventoryItem, IDS["vaccine_item"]).stock_qty
    db.commit()
    invoice_id, card_id, line_id = invoice.id, card.id, vaccine_line.id
    db.close()

    token = _csrf(f"/admin/invoices/{invoice_id}")
    blocked = client.post(f"/admin/invoices/{invoice_id}/add-payment", data={
        "csrf_token": token, "method": "cash", "amount": "210", "idempotency_key": uuid.uuid4().hex,
    }, follow_redirects=False)
    assert blocked.status_code == 303, _loc(blocked)
    assert "不使用套餐" in _loc(blocked)

    token = _csrf(f"/admin/invoices/{invoice_id}")
    key = uuid.uuid4().hex
    redeemed = client.post(f"/admin/invoices/{invoice_id}/add-payment", data={
        "csrf_token": token, "method": "package", "customer_package_id": str(card_id),
        "pkg_cover": f"{line_id}:1", "idempotency_key": key,
    }, follow_redirects=False)
    assert redeemed.status_code == 303, _loc(redeemed)
    again = client.post(f"/admin/invoices/{invoice_id}/add-payment", data={
        "csrf_token": token, "method": "package", "customer_package_id": str(card_id),
        "pkg_cover": f"{line_id}:1", "idempotency_key": key,
    }, follow_redirects=False)
    assert "已经入账" in _loc(again), _loc(again)
    token = _csrf(f"/admin/invoices/{invoice_id}")
    exam_only = client.post(f"/admin/invoices/{invoice_id}/add-payment", data={
        "csrf_token": token, "method": "cash", "amount": "120", "idempotency_key": uuid.uuid4().hex,
    }, follow_redirects=False)
    assert exam_only.status_code == 303 and "不使用" not in _loc(exam_only), _loc(exam_only)

    db = SessionLocal()
    card = db.get(CustomerPackage, card_id)
    assert card.used_count == 1 and card.pet_id == IDS["pet"]
    assert db.get(InventoryItem, IDS["vaccine_item"]).stock_qty == stock_before
    redemption = db.query(PackageRedemption).filter(PackageRedemption.customer_package_id == card_id).one()
    assert redemption.used_count == 1 and redemption.status == "active"
    package_pay = db.query(Payment).filter(Payment.invoice_id == invoice_id, Payment.method == "package").one()
    assert abs(package_pay.amount - 90) < 0.01
    invoice = db.get(Invoice, invoice_id)
    assert invoice.payment_status == "paid"
    db.close()


def test_ambiguous_cards_do_not_block_unrelated_payment():
    db = SessionLocal()
    bath = _card(db, IDS["bath"], name="旧洗澡卡", total_uses=5, pet_id=None, expires_at="")
    invoice = _invoice(db, [("血常规", "manual", None, 1, 120, 120)])
    db.commit()
    invoice_id, bath_id = invoice.id, bath.id
    db.close()
    page = client.get(f"/admin/invoices/{invoice_id}").text
    assert "旧洗澡卡" in page and "不会拦住无关收费" in page
    token = _csrf(f"/admin/invoices/{invoice_id}")
    response = client.post(f"/admin/invoices/{invoice_id}/add-payment", data={
        "csrf_token": token, "method": "wechat", "amount": "120", "idempotency_key": uuid.uuid4().hex,
    }, follow_redirects=False)
    assert response.status_code == 303 and "不使用" not in _loc(response), _loc(response)
    db = SessionLocal()
    assert db.get(CustomerPackage, bath_id).used_count == 0
    db.close()


def test_legacy_pay_multi_pay_bypass_and_mobile_name():
    db = SessionLocal()
    exists = db.query(PackageApplicableItem).filter_by(
        product_id=IDS["product"], inventory_item_id=IDS["vaccine_item"],
    ).first()
    if not exists:
        db.add(PackageApplicableItem(product_id=IDS["product"], inventory_item_id=IDS["vaccine_item"]))
    card = _card(db, IDS["product"], name="三针疫苗卡")
    first = _invoice(db, [_vaccine_line(db)])
    second = _invoice(db, [("血常规", "manual", None, 1, 50, 50)])
    db.commit()
    first_id, second_id, card_id = first.id, second.id, card.id
    line_id = first.items[0].id
    db.close()
    mobile = client.get(f"/m/invoices/{second_id}")
    assert mobile.status_code == 200, mobile.text[:300]
    assert "三针疫苗卡" in mobile.text
    assert "checked" not in mobile.text.split('name="customer_package_id"', 1)[-1][:220]

    token = _csrf(f"/admin/cashier/multi-pay?customer_id={IDS['customer']}")
    combined = client.post("/admin/cashier/multi-pay", data={
        "csrf_token": token, "customer_id": str(IDS["customer"]), "method": "cash",
        "invoice_ids": [str(first_id), str(second_id)], "idempotency_key": uuid.uuid4().hex,
    }, follow_redirects=False)
    assert "不使用套餐" in _loc(combined), _loc(combined)

    token = _csrf(f"/admin/invoices/{first_id}")
    legacy = client.post(f"/admin/invoices/{first_id}/pay", data={
        "csrf_token": token, "payment_method": "cash", "idempotency_key": uuid.uuid4().hex,
    }, follow_redirects=False)
    assert "不使用套餐" in _loc(legacy), _loc(legacy)
    token = _csrf(f"/admin/invoices/{first_id}")
    allowed = client.post(f"/admin/invoices/{first_id}/pay", data={
        "csrf_token": token, "payment_method": "cash",
        "package_bypass_reason": "客户要求本次单买",
        "idempotency_key": uuid.uuid4().hex,
    }, follow_redirects=False)
    assert allowed.status_code == 303 and "不使用" not in _loc(allowed), _loc(allowed)
    db = SessionLocal()
    assert db.query(PackageBypassLog).filter(PackageBypassLog.invoice_id == first_id).count() == 1
    assert db.get(CustomerPackage, card_id).used_count == 0
    db.close()

    token = _csrf(f"/admin/cashier/multi-pay?customer_id={IDS['customer']}")
    # 第二张是无关项目，合并时若把第一张已匹配金额一并收，必须有原因。
    blocked = client.post("/admin/cashier/multi-pay", data={
        "csrf_token": token, "customer_id": str(IDS["customer"]), "method": "cash",
        "invoice_ids": [str(second_id)], "idempotency_key": uuid.uuid4().hex,
    }, follow_redirects=False)
    assert blocked.status_code == 303
    # 只收第二张无关账单，不需要原因。
    token = _csrf(f"/admin/invoices/{second_id}")
    ok = client.post("/admin/cashier/multi-pay", data={
        "csrf_token": token, "customer_id": str(IDS["customer"]), "method": "alipay",
        "invoice_ids": [str(second_id)], "round_amount": "50",
        "idempotency_key": uuid.uuid4().hex,
    }, follow_redirects=False)
    assert ok.status_code == 303 and "不能" not in _loc(ok), _loc(ok)


def test_expired_other_pet_store_and_unassigned_confirmation():
    db = SessionLocal()
    yesterday = (beijing_today() - timedelta(days=1)).isoformat()
    expired = _card(db, IDS["product"], name="过期卡", expires_at=yesterday)
    other = _card(db, IDS["product"], name="别的宠物", pet_id=IDS["other"])
    restricted = _card(db, IDS["store_product"], name="横岗疫苗卡", store="横岗店")
    open_card = _card(db, IDS["product"], name="未指定宠物", pet_id=None, total_uses=2)
    invoice = _invoice(db, [_vaccine_line(db)])
    line_id = invoice.items[0].id
    db.commit()
    invoice_id, open_id = invoice.id, open_card.id
    expired_id, other_id, restricted_id = expired.id, other.id, restricted.id
    db.close()
    token = _csrf(f"/admin/invoices/{invoice_id}")

    def post_pkg(card_id, **extra):
        data = {
            "csrf_token": token, "method": "package", "customer_package_id": str(card_id),
            "pkg_cover": f"{line_id}:1", "idempotency_key": uuid.uuid4().hex,
        }
        data.update(extra)
        return client.post(f"/admin/invoices/{invoice_id}/add-payment", data=data, follow_redirects=False)

    expired_loc = _loc(post_pkg(expired_id))
    assert "过期" in expired_loc or "不能" in expired_loc, expired_loc
    other_loc = _loc(post_pkg(other_id))
    assert "宠物" in other_loc, other_loc
    store_loc = _loc(post_pkg(restricted_id))
    assert "横岗" in store_loc, store_loc
    need_confirm = _loc(post_pkg(open_id))
    assert "确认" in need_confirm, need_confirm
    done = post_pkg(open_id, confirm_unassigned_pet="1")
    assert done.status_code == 303 and "确认本次" not in _loc(done), _loc(done)
    db = SessionLocal()
    saved = db.get(CustomerPackage, open_id)
    assert saved.pet_id is None and saved.used_count == 1
    db.close()


def test_reversal_keeps_the_other_use_and_rollback():
    db = SessionLocal()
    card = _card(db, IDS["bath"], name="两次卡", total_uses=2, pet_id=IDS["pet"])
    invoice = _invoice(db, [_vaccine_line(db), ("加强针", "manual", None, 1, 90, 90)])
    lines = list(invoice.items)
    db.commit()
    pay1 = redeem_package(
        db, invoice, card.id, [(lines[0].id, 1)],
        operator="pkg_admin", op_store="东环店", idempotency_key=uuid.uuid4().hex,
        confirm_unassigned_pet=False, confirm_unmapped=True,
    )
    pay2 = redeem_package(
        db, invoice, card.id, [(lines[1].id, 1)],
        operator="pkg_admin", op_store="东环店", idempotency_key=uuid.uuid4().hex,
        confirm_unassigned_pet=False, confirm_unmapped=True,
    )
    db.commit()
    reverse_package_payment(db, pay1, "pkg_admin")
    db.commit()
    db.expire_all()
    card = db.get(CustomerPackage, card.id)
    rows = db.query(PackageRedemption).filter(PackageRedemption.customer_package_id == card.id).order_by(PackageRedemption.id).all()
    assert card.used_count == 1 and card.status == "active"
    assert [row.status for row in rows] == ["reversed", "active"]
    assert rows[0].payment_id == pay1.id and rows[1].payment_id == pay2.id

    boom = _card(db, IDS["bath"], name="回滚卡", total_uses=1, pet_id=IDS["pet"])
    boom_invoice = _invoice(db, [("洗澡", "manual", None, 1, 40, 40)])
    boom_id = boom.id
    db.commit()
    try:
        redeem_package(
            db, boom_invoice, boom_id, [(boom_invoice.items[0].id, 1)],
            operator="pkg_admin", op_store="东环店", idempotency_key=uuid.uuid4().hex,
            confirm_unassigned_pet=False, confirm_unmapped=True,
        )
        raise RuntimeError("模拟后续失败")
    except RuntimeError:
        db.rollback()
    assert db.get(CustomerPackage, boom_id).used_count == 0
    db.close()


def test_concurrent_last_use():
    db = SessionLocal()
    card = _card(db, IDS["product"], name="最后一次", total_uses=1, used_count=0, pet_id=IDS["pet"])
    invoice = _invoice(db, [_vaccine_line(db)])
    line_id = invoice.items[0].id
    card_id, invoice_id = card.id, invoice.id
    db.commit()
    db.close()
    barrier = threading.Barrier(2)
    results = []

    def worker():
        local = SessionLocal()
        try:
            barrier.wait(timeout=5)
            inv = local.get(Invoice, invoice_id)
            redeem_package(
                local, inv, card_id, [(line_id, 1)],
                operator="pkg_admin", op_store="东环店", idempotency_key=uuid.uuid4().hex,
                confirm_unassigned_pet=False, confirm_unmapped=False,
            )
            local.commit()
            results.append("ok")
        except PackageGuardError:
            local.rollback()
            results.append("blocked")
        finally:
            local.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    db = SessionLocal()
    assert db.get(CustomerPackage, card_id).used_count == 1
    assert sorted(results) == ["blocked", "ok"]
    db.close()


def test_duplicate_service_call_and_similar_name_not_matched():
    db = SessionLocal()
    card = _card(db, IDS["bath"], name="名字很像犬四联", total_uses=1, pet_id=IDS["pet"])
    invoice = _invoice(db, [_vaccine_line(db)])
    db.commit()
    try:
        redeem_package(
            db, invoice, card.id, [(invoice.items[0].id, 1)],
            operator="pkg_admin", op_store="东环店", idempotency_key="same-key",
            confirm_unassigned_pet=False, confirm_unmapped=False,
        )
        refused = False
    except PackageGuardError:
        refused = True
        db.rollback()
    assert refused
    redeem_package(
        db, invoice, card.id, [(invoice.items[0].id, 1)],
        operator="pkg_admin", op_store="东环店", idempotency_key="same-key",
        confirm_unassigned_pet=False, confirm_unmapped=True,
    )
    db.commit()
    try:
        redeem_package(
            db, invoice, card.id, [(invoice.items[0].id, 1)],
            operator="pkg_admin", op_store="东环店", idempotency_key="same-key",
            confirm_unassigned_pet=False, confirm_unmapped=True,
        )
        raised = False
    except DuplicateSubmission:
        raised = True
        db.rollback()
    assert raised
    assert db.get(CustomerPackage, card.id).used_count == 1
    db.close()


if __name__ == "__main__":
    test_mapped_partial_and_ordinary_gate()
    test_ambiguous_cards_do_not_block_unrelated_payment()
    test_legacy_pay_multi_pay_bypass_and_mobile_name()
    test_expired_other_pet_store_and_unassigned_confirmation()
    test_reversal_keeps_the_other_use_and_rollback()
    test_concurrent_last_use()
    test_duplicate_service_call_and_similar_name_not_matched()
    print("package guard tests passed")
