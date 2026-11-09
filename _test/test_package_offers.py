"""从品目开套餐：命名、去重、分摊、按品目扣次。老套餐不迁移，目录不再手建。"""

import os
import sys
import tempfile
import uuid
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-package-offer-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["UPLOAD_DIR"] = str(Path(TEMP_DIR.name) / "uploads")
os.environ["SESSION_HTTPS_ONLY"] = "0"

from fastapi.testclient import TestClient
from passlib.hash import bcrypt
from sqlalchemy.orm import Session

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import (
    AdminUser, Customer, CustomerPackage, CustomerPackageComponent, InventoryItem,
    Invoice, InvoiceItem, PackageProduct, PackageRedemption, Payment, Pet, Vaccination,
)
from app.services.package_guard import PackageGuardError, redeem_package, reverse_package_payment
from app.services.package_offer import (
    OfferError, OfferSpec, build_offer_name, cents_for_span, get_or_create_offer,
    money_cents, remaining_refund_amount, resolve_offer_store,
)
from app.time_utils import beijing_today


@asynccontextmanager
async def _test_lifespan(_app):
    yield


app.router.lifespan_context = _test_lifespan
Base.metadata.create_all(bind=engine)
client = TestClient(app)


def _csrf(path: str) -> str:
    html = client.get(path).text
    return html.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]


def _login(username: str):
    client.post("/admin/logout", follow_redirects=False)
    token = _csrf("/admin/login")
    response = client.post("/admin/login", data={
        "username": username, "password": "test123456", "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303, response.text


def _seed():
    db = SessionLocal()
    db.add_all([
        AdminUser(username="offer_admin", password_hash=bcrypt.hash("test123456"), role="superadmin", store="东环店", display_name="套餐超管", is_active=True),
        AdminUser(username="offer_staff", password_hash=bcrypt.hash("test123456"), role="staff", store="东环店", display_name="东环前台", is_active=True),
    ])
    db.commit()
    db.close()


_seed()


def test_name_and_cent_allocation():
    assert build_offer_name("猫三联", "针", 3, 0, [("抗体检测", 1, "次")], 18000) == "猫三联 3针 + 赠抗体检测1次 · ¥180"
    assert build_offer_name("洗澡", "次", 3, 1, [], 6500) == "洗澡 买3送1 · ¥65"
    assert cents_for_span(10000, 3, 0, 3) == 10000
    assert cents_for_span(10000, 3, 0, 1) == 3334
    assert cents_for_span(10000, 3, 1, 2) == 6666
    assert money_cents(180) == 18000
    assert money_cents("65.5") == 6550


def _spec(primary, gift_item, price, store="东环店"):
    return OfferSpec(
        primary_item_id=primary,
        buy_count=3,
        gift_count=0,
        gifts=[(gift_item, 1)],
        price=price,
        validity_days=365,
        store=store,
    )


def test_dedup_allocation_and_component_redemption():
    db = SessionLocal()
    customer = Customer(name="套餐新客", phone="13900004444")
    db.add(customer)
    db.flush()
    pet = Pet(customer_id=customer.id, name="小白", species="cat", store="东环店")
    vaccine = InventoryItem(name="猫三联", category="vaccine", unit="针", store="东环店", sell_price=61, is_active=True)
    antibody = InventoryItem(name="抗体检测", category="lab", unit="次", store="东环店", sell_price=40, is_service=True, is_active=True)
    other_store = InventoryItem(name="猫三联", category="vaccine", unit="针", store="横岗店", sell_price=61, is_active=True)
    db.add_all([pet, vaccine, antibody, other_store])
    db.flush()
    with pytest_raises(OfferError):
        resolve_offer_store(is_superadmin=False, staff_store="", session_store="", explicit=None)
    assert resolve_offer_store(is_superadmin=False, staff_store="东环店", session_store="东环店", explicit="横岗店") == "东环店"
    try:
        get_or_create_offer(db, _spec(other_store.id, antibody.id, 180, store="东环店"))
        raise AssertionError("不该把横岗品目放进东环套餐")
    except OfferError:
        pass
    first, created = get_or_create_offer(db, _spec(vaccine.id, antibody.id, 180))
    again, reused = get_or_create_offer(db, _spec(vaccine.id, antibody.id, 180))
    other_price, _created_price = get_or_create_offer(db, _spec(vaccine.id, antibody.id, 160))
    assert created and not reused
    assert first.id == again.id
    assert other_price.id != first.id
    assert first.name == "猫三联 3针 + 赠抗体检测1次 · ¥180"
    assert first.total_uses == 4
    card = CustomerPackage(
        customer_id=customer.id, pet_id=pet.id, product_id=first.id, name=first.name,
        total_uses=first.total_uses, used_count=0, sell_price=180, unit_price=first.unit_price,
        purchase_date=beijing_today().isoformat(),
        expires_at=(beijing_today() + timedelta(days=365)).isoformat(),
        status="active", store="东环店",
    )
    db.add(card)
    db.flush()
    from app.services.package_offer import attach_components_if_structured
    attach_components_if_structured(db, card)
    parts = db.query(CustomerPackageComponent).filter_by(customer_package_id=card.id).order_by(CustomerPackageComponent.id).all()
    assert [part.quantity for part in parts] == [3, 1]
    assert sum(part.amount_cents for part in parts) == 18000
    assert remaining_refund_amount(db, card) == 180
    invoice = _invoice(db, customer.id, pet.id, [
        _vaccine_line(db, customer.id, pet.id, vaccine.id, "猫三联", 61),
        _vaccine_line(db, customer.id, pet.id, antibody.id, "抗体检测", 40),
    ])
    db.commit()
    invoice_id = invoice.id
    card_id = card.id
    antibody_line_id = next(line.id for line in invoice.items if "抗体" in (line.description or ""))
    vaccine_line_id = next(line.id for line in invoice.items if "猫三联" in (line.description or ""))
    try:
        redeem_package(
            db, invoice, card.id, [(antibody_line_id, 2)],
            operator="offer_staff", op_store="东环店", idempotency_key=uuid.uuid4().hex,
            confirm_unassigned_pet=False, confirm_unmapped=False,
        )
        raise AssertionError("赠品只有 1 次，不能扣 2 次")
    except PackageGuardError:
        db.rollback()
    db = SessionLocal()
    invoice = db.get(Invoice, invoice_id)
    card = db.get(CustomerPackage, card_id)
    payment = redeem_package(
        db, invoice, card.id, [(antibody_line_id, 1)],
        operator="offer_staff", op_store="东环店", idempotency_key=uuid.uuid4().hex,
        confirm_unassigned_pet=False, confirm_unmapped=False,
    )
    db.commit()
    assert payment.amount == 40
    db.refresh(card)
    assert card.used_count == 1
    antibody_part = db.query(CustomerPackageComponent).filter_by(customer_package_id=card.id, role="gift").one()
    vaccine_part = db.query(CustomerPackageComponent).filter_by(customer_package_id=card.id, role="primary").one()
    assert antibody_part.used_count == 1 and vaccine_part.used_count == 0
    try:
        redeem_package(
            db, invoice, card.id, [(antibody_line_id, 1)],
            operator="offer_staff", op_store="东环店", idempotency_key=uuid.uuid4().hex,
            confirm_unassigned_pet=False, confirm_unmapped=False,
        )
        raise AssertionError("抗体次数已经用完")
    except PackageGuardError:
        db.rollback()
    db = SessionLocal()
    invoice = db.get(Invoice, invoice_id)
    card = db.get(CustomerPackage, card_id)
    vaccine_line = db.get(InvoiceItem, vaccine_line_id)
    payment = redeem_package(
        db, invoice, card.id, [(vaccine_line.id, 1)],
        operator="offer_staff", op_store="东环店", idempotency_key=uuid.uuid4().hex,
        confirm_unassigned_pet=False, confirm_unmapped=False,
    )
    assert payment.amount == 61
    reverse_package_payment(db, payment, "offer_admin")
    db.expire_all()
    card = db.get(CustomerPackage, card_id)
    vaccine_part = db.query(CustomerPackageComponent).filter_by(customer_package_id=card.id, role="primary").one()
    assert vaccine_part.used_count == 0
    assert card.used_count == 1
    db.commit()
    db.close()


def test_archive_hides_idle_only():
    from app.services.package_offer import archive_idle, is_in_use, products_with_remaining
    db = SessionLocal()
    idle = PackageProduct(name="旧闲置卡", total_uses=4, sell_price=100, is_active=False, store="东环店")
    on_sale = PackageProduct(name="在售卡", total_uses=4, sell_price=100, is_active=True, store="东环店")
    held = PackageProduct(name="还有次数", total_uses=4, sell_price=80, is_active=False, store="东环店")
    db.add_all([idle, on_sale, held])
    db.flush()
    customer = db.query(Customer).first()
    db.add(CustomerPackage(
        customer_id=customer.id, product_id=held.id, name=held.name, total_uses=4, used_count=1,
        sell_price=80, status="active", purchase_date=beijing_today().isoformat(), store="东环店",
    ))
    db.flush()
    assert archive_idle(db) >= 1
    db.refresh(idle)
    db.refresh(held)
    db.refresh(on_sale)
    assert idle.archived_at is not None
    assert held.archived_at is None and on_sale.archived_at is None
    remaining = products_with_remaining(db)
    assert is_in_use(on_sale, remaining) and is_in_use(held, remaining)
    assert not is_in_use(idle, remaining)
    db.commit()
    db.close()


def test_http_staff_store_catalog_cannot_create():
    _login("offer_staff")
    assert client.get("/admin/packages/migration", follow_redirects=False).status_code == 404
    assert client.get("/m/packages").status_code == 200
    assert "迁移预演" not in client.get("/m/packages").text
    db = SessionLocal()
    east = InventoryItem(name="东环洗澡", category="grooming", subcategory="washcare", unit="次", store="东环店", sell_price=45, is_service=True, is_active=True)
    west = InventoryItem(name="横岗洗澡", category="grooming", subcategory="washcare", unit="次", store="横岗店", sell_price=45, is_service=True, is_active=True)
    customer = Customer(name="到店客户", phone="13900005555")
    db.add_all([east, west, customer])
    db.commit()
    east_id, west_id, customer_id = east.id, west.id, customer.id
    before_products = db.query(PackageProduct).count()
    db.close()
    page = client.get(f"/m/customer/{customer_id}/packages/sell")
    assert page.status_code == 200
    assert "按套餐 / 活动卖" in page.text
    token = _csrf(f"/admin/customers/{customer_id}")
    denied = client.post(f"/admin/customers/{customer_id}/packages/offer", data={
        "csrf_token": token, "as_package": "1", "primary_item_id": str(west_id),
        "buy_count": "3", "gift_count": "1", "price": "65", "validity_days": "365",
        "store": "横岗店", "pay_method": "cash", "pet_id": "0",
    }, follow_redirects=False)
    assert denied.status_code == 303
    assert "东环店" in unquote(denied.headers["location"]) or "不能放进" in unquote(denied.headers["location"])
    sold = client.post(f"/admin/customers/{customer_id}/packages/offer", data={
        "csrf_token": token, "as_package": "1", "primary_item_id": str(east_id),
        "buy_count": "3", "gift_count": "1", "price": "65", "validity_days": "0",
        "pay_method": "cash", "pet_id": "0",
    }, follow_redirects=False)
    assert sold.status_code == 303, unquote(sold.headers.get("location", ""))
    assert "洗澡" in unquote(sold.headers["location"])
    db = SessionLocal()
    assert db.query(PackageProduct).count() == before_products + 1
    product = db.query(PackageProduct).filter(PackageProduct.primary_item_id == east_id).one()
    assert product.store == "东环店"
    assert product.name == "东环洗澡 买3送1 · ¥65"
    card = db.query(CustomerPackage).filter_by(product_id=product.id).one()
    assert card.sell_price == 65 and card.total_uses == 4
    parts = db.query(CustomerPackageComponent).filter_by(customer_package_id=card.id).all()
    assert sum(part.amount_cents for part in parts) == 6500
    db.close()
    _login("offer_admin")
    catalog = client.get("/admin/packages")
    assert catalog.status_code == 200
    assert "开单生成" in catalog.text
    assert "老套餐" in catalog.text
    assert "绑定可抵项目" in catalog.text
    assert "新建套餐" not in catalog.text
    assert "从品目开套餐" not in catalog.text
    assert "迁移预演" not in catalog.text
    token = _csrf("/admin/packages")
    before = SessionLocal()
    before_count = before.query(PackageProduct).count()
    legacy = before.query(PackageProduct).filter(PackageProduct.offer_kind == "").first()
    legacy_id = legacy.id if legacy is not None else None
    legacy_name = legacy.name if legacy is not None else None
    before.close()
    blocked = client.post("/admin/packages/create", data={
        "csrf_token": token, "name": "手打新套餐", "category": "beauty",
        "total_uses": "4", "sell_price": "100", "unit_price": "25",
        "validity_days": "365", "store": "东环店",
    }, follow_redirects=False)
    assert blocked.status_code == 303
    assert "不再新建" in unquote(blocked.headers["location"])
    blocked_offer = client.post("/admin/packages/offers", data={
        "csrf_token": token, "as_package": "1", "primary_item_id": "1",
        "buy_count": "3", "gift_count": "0", "price": "100", "validity_days": "365",
        "store": "东环店",
    }, follow_redirects=False)
    assert blocked_offer.status_code == 303
    assert "不再新建" in unquote(blocked_offer.headers["location"])
    after = SessionLocal()
    assert after.query(PackageProduct).count() == before_count
    assert after.query(PackageProduct).filter(PackageProduct.name == "手打新套餐").count() == 0
    if legacy_id is not None:
        saved = after.get(PackageProduct, legacy_id)
        assert (saved.offer_kind or "") == ""
        assert saved.name == legacy_name
    after.close()
    _login("offer_staff")
    staff_token = _csrf("/admin/packages")
    staff_blocked = client.post("/admin/packages/create", data={
        "csrf_token": staff_token, "name": "员工手打", "total_uses": "2",
        "sell_price": "10", "validity_days": "30", "store": "东环店",
    }, follow_redirects=False)
    assert staff_blocked.status_code == 303
    assert "不再新建" in unquote(staff_blocked.headers["location"])


def pytest_raises(exc_type):
    class _Ctx:
        def __enter__(self):
            return None
        def __exit__(self, etype, value, tb):
            if etype is None:
                raise AssertionError(f"应该抛出 {exc_type.__name__}")
            return issubclass(etype, exc_type)
    return _Ctx()


def _invoice(db: Session, customer_id: int, pet_id: int, lines: list) -> Invoice:
    total = round(sum(line[5] for line in lines), 2)
    invoice = Invoice(
        invoice_no="OFF" + uuid.uuid4().hex[:8],
        customer_id=customer_id, pet_id=pet_id,
        invoice_date=beijing_today().isoformat(), subtotal=total, total_amount=total,
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


def _vaccine_line(db: Session, customer_id: int, pet_id: int, item_id: int, name: str, price: float):
    row = Vaccination(
        pet_id=pet_id, customer_id=customer_id, vaccine_name=name,
        inventory_item_id=item_id, vaccinated_date=beijing_today().isoformat(),
    )
    db.add(row)
    db.flush()
    return (name, "vaccination", row.id, 1, price, price)


if __name__ == "__main__":
    test_name_and_cent_allocation()
    test_dedup_allocation_and_component_redemption()
    test_archive_hides_idle_only()
    test_http_staff_store_catalog_cannot_create()
    print("package offer tests passed")
