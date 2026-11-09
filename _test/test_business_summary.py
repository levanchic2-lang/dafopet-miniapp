"""经营复盘只读汇总：鉴权、门店、按天/按周、导入病历、以及返回里没有个人信息。"""

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-business-summary-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["SESSION_SECRET"] = "business-summary-test"
os.environ["SESSION_HTTPS_ONLY"] = "0"
os.environ["REPORTS_READONLY_TOKEN"] = "test-readonly-token"

from fastapi.testclient import TestClient

from app.config import settings
from app.database import SessionLocal, init_db
from app.main import _pwd_ctx, app
from app.models import (
    AdminUser,
    AnesthesiaOrder,
    AuditLog,
    Customer,
    CustomerPackage,
    Deposit,
    DewormingRecord,
    GroomingOrder,
    Hospitalization,
    Invoice,
    InvoiceItem,
    PackageRedemption,
    Payment,
    Pet,
    Vaccination,
    Visit,
    WalkInRegistration,
    Wallet,
    WalletTransaction,
)

TOKEN = "test-readonly-token"
URL = "/api/reports/business-summary"
SECRETS = [
    "机密客户张三",
    "PHONE13900001111",
    "机密宠物豆豆",
    "HCSECRET999",
    "DXSECRETXYZ",
    "SOAPSECRETQQQ",
    "NOTEABC",
    "GROOMSECRETRRR",
    "HOSPSECRETSSS",
    "ANESSECRETTTT",
    "PAYSECRETUUU",
    "WALLETSECRETVVV",
    "AUDITSECRETWWW",
    "PKGSECRETYYY",
]
BANNED_KEYS = {
    "id", "name", "phone", "phones_extra", "customer_name", "pet_name",
    "customer_id", "pet_id", "visit_id", "invoice_id", "invoice_no",
    "payment_id", "deposit_id", "wallet_id", "id_number", "address",
    "openid", "wechat_openid", "microchip_id", "medical_record_no",
    "chief_complaint", "physical_exam", "diagnosis", "treatment_plan",
    "notes", "note", "operator", "vet_name", "groomer_name", "assistant_name",
    "created_by", "detail", "phone_number",
}
DEFINITION_KEYS = [
    "revenue.paid_invoice_amount",
    "revenue.paid_invoice_count",
    "revenue.avg_ticket",
    "revenue.finance_cash_amount",
    "revenue.business_noncash_amount",
    "revenue.by_method",
    "revenue.by_category",
    "revenue.not_in_paid_revenue",
    "operations.walk_in_registration_count",
    "operations.grooming_order_count",
    "operations.hospitalization_admit_count",
    "operations.anesthesia_order_count",
    "operations.vaccination_count",
    "operations.deworming_count",
    "operations.package_sold_count",
    "operations.package_redeem_count",
    "operations.native_visit_count",
    "customers.new_customer_count",
    "customers.returning_customer_count",
]


def bj_utc(year, month, day, hour=10, minute=0):
    return datetime(year, month, day, hour, minute) - timedelta(hours=8)


def cents(value):
    return int(round(float(value) * 100))


def auth_headers(token=TOKEN):
    return {"X-Reports-Token": token}


def summary(client, token=TOKEN, **params):
    return client.get(URL, headers=auth_headers(token) if token is not None else {}, params=params)


def body_of(resp):
    assert resp.status_code == 200, resp.text[:500]
    return resp.json()


def day_bucket(payload, day):
    for item in payload["buckets"]:
        if item["period_start"] == day and item["period_end"] == day:
            return item
    raise AssertionError(day)


def week_bucket(payload, monday):
    for item in payload["buckets"]:
        if item.get("week_start") == monday:
            return item
    raise AssertionError(monday)


def assert_public(payload):
    text = json.dumps(payload, ensure_ascii=False)
    for secret in SECRETS:
        assert secret not in text
    def walk(value):
        if isinstance(value, dict):
            for key, item in value.items():
                assert key.lower() not in BANNED_KEYS, key
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
    walk(payload)
    for key in DEFINITION_KEYS:
        assert payload["definitions"][key].strip()
    joined = " ".join(payload["limitations"])
    assert "东环" in joined
    assert "套餐" in joined
    assert "导入" in joined or "warmsoft" in joined


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    if not match:
        match = re.search(r'value="([^"]+)"[^>]*name="csrf_token"', html)
    assert match, html[:500]
    return match.group(1)


def add_invoice(db, customer_id, pet_id, store, paid_at, amount, method, ref_type, status="paid", notes=""):
    inv = Invoice(
        customer_id=customer_id,
        pet_id=pet_id,
        store=store,
        invoice_date="2026-11-08",
        subtotal=amount,
        total_amount=amount,
        payment_status=status,
        payment_method=method,
        paid_at=paid_at,
        notes=notes,
    )
    db.add(inv)
    db.flush()
    db.add(InvoiceItem(
        invoice_id=inv.id,
        ref_type=ref_type,
        description="收费项目",
        quantity=1,
        unit_price=amount,
        subtotal=amount,
    ))
    return inv


def add_payment(db, invoice, method, amount, status="success", created_at=None, store=None, note=""):
    db.add(Payment(
        invoice_id=invoice.id,
        customer_id=invoice.customer_id,
        method=method,
        amount=amount,
        status=status,
        store=store if store is not None else invoice.store,
        created_at=created_at or invoice.paid_at or bj_utc(2026, 11, 8),
        note=note,
    ))


def seed():
    init_db()
    with SessionLocal() as db:
        db.add(AdminUser(
            username="summary_admin",
            password_hash=_pwd_ctx.hash("test123456"),
            role="superadmin",
            store="",
        ))
        customer_a = Customer(name="机密客户张三", phone="PHONE13900001111", is_internal=False)
        customer_b = Customer(name="老客乙", phone="13800000002", is_internal=False)
        customer_d = Customer(name="新客丁", phone="13800000004", is_internal=False)
        customer_f = Customer(name="只充值的己", phone="13800000006", is_internal=False)
        customer_g = Customer(name="员工内购庚", phone="13800000007", is_internal=True)
        customer_h = Customer(name="无流水辛", phone="13800000008", is_internal=False)
        db.add_all([customer_a, customer_b, customer_d, customer_f, customer_g, customer_h])
        db.flush()
        pet_a = Pet(
            customer_id=customer_a.id, name="机密宠物豆豆", species="cat",
            store="横岗店", medical_record_no="HCSECRET999",
        )
        pet_b = Pet(customer_id=customer_b.id, name="乙宠物", species="dog", store="东环店")
        pet_d = Pet(customer_id=customer_d.id, name="丁宠物", species="cat", store="横岗店")
        pet_h = Pet(customer_id=customer_h.id, name="辛宠物", species="cat", store="横岗店")
        db.add_all([pet_a, pet_b, pet_d, pet_h])
        db.flush()

        # 老客乙的系统内第一张已付发票在查询区间之前。
        first_b = add_invoice(db, customer_b.id, pet_b.id, "横岗店", bj_utc(2026, 10, 1), 50, "cash", "prescription")
        add_payment(db, first_b, "cash", 50)

        invoice_a = add_invoice(
            db, customer_a.id, pet_a.id, "横岗店", bj_utc(2026, 11, 8), 100, "mixed",
            "prescription", notes="病历NOTEABC",
        )
        add_payment(db, invoice_a, "shouqianba", 60)
        add_payment(db, invoice_a, "cash", 40)
        add_payment(
            db, invoice_a, "cash", 5, status="cancelled",
            created_at=bj_utc(2026, 11, 8), note="PAYSECRETUUU",
        )

        invoice_a2 = add_invoice(db, customer_a.id, pet_a.id, "横岗店", bj_utc(2026, 11, 9, 11), 30, "wallet", "grooming")
        add_payment(db, invoice_a2, "wallet", 30)
        # 发票门店为空，按宠物档案回退到横岗店。
        invoice_fallback = add_invoice(db, customer_a.id, pet_a.id, "", bj_utc(2026, 11, 9, 13), 7, "wechat", "exam_order")
        add_payment(db, invoice_fallback, "wechat", 7, store="横岗店")

        # 北京时间 11-09 00:30，对应 UTC 仍是 11-08。必须进周一那天，不能进周日。
        invoice_d = add_invoice(
            db, customer_d.id, pet_d.id, "横岗店",
            datetime(2026, 11, 8, 16, 30), 15, "cash", "vaccination",
        )
        add_payment(db, invoice_d, "cash", 15, created_at=datetime(2026, 11, 8, 16, 30))

        invoice_b = add_invoice(db, customer_b.id, pet_b.id, "东环店", bj_utc(2026, 11, 9, 12), 40, "wechat", "prescription")
        add_payment(db, invoice_b, "wechat", 40, store="东环店")

        invoice_g = add_invoice(db, customer_g.id, pet_a.id, "横岗店", bj_utc(2026, 11, 8, 15), 999, "cash", "prescription")
        add_payment(db, invoice_g, "cash", 999)

        recharge_d = add_invoice(db, customer_d.id, pet_d.id, "横岗店", bj_utc(2026, 11, 9, 8), 200, "cash", "wallet_recharge")
        add_payment(db, recharge_d, "cash", 200)
        recharge_f = add_invoice(db, customer_f.id, None, "", bj_utc(2026, 11, 9, 8, 10), 50, "cash", "wallet_recharge")
        add_payment(db, recharge_f, "cash", 50, store="")

        invoice_h = add_invoice(db, customer_h.id, pet_h.id, "横岗店", bj_utc(2026, 1, 15), 9, "wechat", "manual")

        refunded = add_invoice(
            db, customer_a.id, pet_a.id, "横岗店", None, 12, "cash", "manual", status="refunded",
        )
        refunded.paid_at = None
        refunded.updated_at = bj_utc(2026, 11, 9, 15)

        db.add_all([
            Visit(customer_id=customer_a.id, pet_id=pet_a.id, visit_date="2026-11-08", store="横岗店",
                  data_origin="", chief_complaint="SOAPSECRETQQQ", diagnosis="原生诊断不该出现"),
            Visit(customer_id=customer_a.id, pet_id=pet_a.id, visit_date="2026-11-08", store="横岗店",
                  data_origin=None),
            Visit(customer_id=customer_b.id, pet_id=pet_b.id, visit_date="2026-11-08", store="东环店",
                  data_origin=""),
            Visit(customer_id=customer_a.id, pet_id=pet_a.id, visit_date="2026-11-08", store="",
                  data_origin=""),
            Visit(customer_id=customer_a.id, pet_id=pet_a.id, visit_date="2026-11-08", store="横岗店",
                  data_origin="warmsoft", diagnosis="DXSECRETXYZ"),
            Visit(customer_id=customer_a.id, pet_id=pet_a.id, visit_date="2026-11-08", store="",
                  data_origin="import", diagnosis="导入病历正文"),
            GroomingOrder(customer_id=customer_a.id, pet_id=pet_a.id, groom_date="2026-11-08",
                          store="横岗店", status="active", notes="GROOMSECRETRRR"),
            GroomingOrder(customer_id=customer_b.id, pet_id=pet_b.id, groom_date="2026-11-09",
                          store="东环店", status="active"),
            GroomingOrder(customer_id=customer_a.id, pet_id=pet_a.id, groom_date="2026-11-09",
                          store="横岗店", status="voided"),
            Hospitalization(customer_id=customer_a.id, pet_id=pet_a.id, store="横岗店",
                            admitted_at=bj_utc(2026, 11, 9, 8), status="admitted",
                            reason="HOSPSECRETSSS", staff_token="hosp-open", owner_token="owner-open"),
            Hospitalization(customer_id=customer_a.id, pet_id=pet_a.id, store="横岗店",
                            admitted_at=bj_utc(2026, 11, 9, 9), status="cancelled",
                            staff_token="hosp-cancel", owner_token="owner-cancel"),
            AnesthesiaOrder(customer_id=customer_a.id, pet_id=pet_a.id, anesth_date="2026-11-09",
                            store="横岗店", status="issued", notes="ANESSECRETTTT"),
            AnesthesiaOrder(customer_id=customer_a.id, pet_id=pet_a.id, anesth_date="2026-11-09",
                            store="横岗店", status="issued"),
            AnesthesiaOrder(customer_id=customer_b.id, pet_id=pet_b.id, anesth_date="2026-11-09",
                            store="东环店", status="issued"),
            AnesthesiaOrder(customer_id=customer_a.id, pet_id=pet_a.id, anesth_date="2026-11-09",
                            store="横岗店", status="voided"),
            WalkInRegistration(customer_id=customer_a.id, pet_id=pet_a.id, store="横岗店",
                               biz_date="2026-11-09", status="open", fee_amount=10),
            WalkInRegistration(customer_id=customer_a.id, pet_id=pet_a.id, store="横岗店",
                               biz_date="2026-11-09", status="converted", fee_amount=10),
            WalkInRegistration(customer_id=customer_a.id, pet_id=pet_a.id, store="横岗店",
                               biz_date="2026-11-09", status="cancelled", fee_amount=10),
            Vaccination(customer_id=customer_a.id, pet_id=pet_a.id, vaccinated_date="2026-11-09", status="active"),
            Vaccination(customer_id=customer_a.id, pet_id=pet_a.id, vaccinated_date="2026-11-09", status="voided"),
            DewormingRecord(customer_id=customer_b.id, pet_id=pet_b.id, deworm_date="2026-11-09", status="active"),
        ])
        wallet_d = Wallet(customer_id=customer_d.id, balance=200)
        wallet_f = Wallet(customer_id=customer_f.id, balance=50)
        db.add_all([wallet_d, wallet_f])
        db.flush()
        db.add_all([
            WalletTransaction(wallet_id=wallet_d.id, customer_id=customer_d.id, type="recharge",
                              amount=200, bonus_amount=0, store="横岗店",
                              created_at=bj_utc(2026, 11, 9, 8), note="WALLETSECRETVVV"),
            WalletTransaction(wallet_id=wallet_f.id, customer_id=customer_f.id, type="recharge",
                              amount=50, bonus_amount=0, store="",
                              created_at=bj_utc(2026, 11, 9, 8, 10)),
            WalletTransaction(wallet_id=wallet_d.id, customer_id=customer_d.id, type="refund",
                              amount=-10, bonus_amount=0, store="横岗店",
                              created_at=bj_utc(2026, 11, 9, 18)),
        ])
        package = CustomerPackage(
            customer_id=customer_a.id, pet_id=pet_a.id, name="洗澡卡",
            purchase_date="2026-11-09", sell_price=80, store="", status="active",
            note="PKGSECRETYYY",
        )
        refunded_package = CustomerPackage(
            customer_id=customer_a.id, purchase_date="2026-11-09", sell_price=999,
            store="", status="refunded",
        )
        db.add_all([package, refunded_package])
        db.flush()
        db.add_all([
            PackageRedemption(customer_package_id=package.id, customer_id=customer_a.id,
                              used_count=2, store="横岗店", status="active",
                              created_at=bj_utc(2026, 11, 9, 16)),
            PackageRedemption(customer_package_id=package.id, customer_id=customer_a.id,
                              used_count=1, store="", status="active",
                              created_at=bj_utc(2026, 11, 9, 16, 10)),
            PackageRedemption(customer_package_id=package.id, customer_id=customer_a.id,
                              used_count=4, store="横岗店", status="reversed",
                              created_at=bj_utc(2026, 11, 9, 16, 20)),
        ])
        deposit = Deposit(customer_id=customer_a.id, pet_id=pet_a.id, amount=20, store="横岗店",
                          status="held", created_at=bj_utc(2026, 11, 9, 9))
        db.add(deposit)
        db.flush()
        db.add(AuditLog(
            action="deposit_refund",
            detail=json.dumps({"amount": 6, "deposit_id": deposit.id, "note": "AUDITSECRETWWW"}),
            created_at=bj_utc(2026, 11, 9, 19),
        ))
        db.commit()


def test_auth(client):
    params = {"date_from": "2026-11-08", "date_to": "2026-11-09", "grain": "day", "store": "全部"}
    missing = client.get(URL, params=params)
    assert missing.status_code == 401, missing.text
    wrong = summary(client, token="wrong-token", **params)
    assert wrong.status_code == 401, wrong.text
    longer = summary(client, token="test-readonly-token-extra", **params)
    assert longer.status_code == 401, longer.text

    saved = os.environ["REPORTS_READONLY_TOKEN"]
    os.environ["REPORTS_READONLY_TOKEN"] = "  "
    try:
        unconfigured = summary(client, token=saved, **params)
        assert unconfigured.status_code == 503, unconfigured.text
        assert "test-readonly-token" not in unconfigured.text
    finally:
        os.environ["REPORTS_READONLY_TOKEN"] = saved

    page = client.get("/admin/login")
    logged = client.post("/admin/login", data={
        "username": "summary_admin",
        "password": "test123456",
        "csrf_token": csrf_from(page.text),
    }, follow_redirects=False)
    assert logged.status_code == 303, logged.text
    still_blocked = client.get(URL, params=params)
    assert still_blocked.status_code == 401, still_blocked.text
    allowed = summary(client, **params)
    assert allowed.status_code == 200, allowed.text

    client.get("/admin/logout", follow_redirects=False)
    revenue = client.get("/admin/reports/revenue", headers=auth_headers())
    assert revenue.status_code in (302, 303, 307), revenue.status_code
    assert "login" in revenue.headers.get("location", "")
    assert "paid_invoice_amount" not in revenue.text
    again = summary(client, **params)
    assert again.status_code == 200
    follow = client.get("/admin/reports/revenue")
    assert follow.status_code in (302, 303, 307)


def test_validation(client):
    ok = {"date_from": "2026-11-08", "date_to": "2026-11-09", "grain": "day"}
    assert summary(client, grain="month", date_from="2026-11-08", date_to="2026-11-09").status_code == 400
    assert summary(client, grain="", date_from="2026-11-08", date_to="2026-11-09").status_code == 400
    assert summary(client, store="龙华店", **ok).status_code == 400
    assert summary(client, date_from="2026-11-09", date_to="2026-11-08", grain="day").status_code == 400
    assert summary(client, date_from="2026-02-31", date_to="2026-03-01", grain="day").status_code == 400
    too_long = summary(client, date_from="2024-01-01", date_to="2025-01-01", grain="day")
    assert too_long.status_code == 400, too_long.text
    leap = body_of(summary(client, date_from="2024-01-01", date_to="2024-12-31", grain="day", store="全部"))
    assert len(leap["buckets"]) == 366
    assert leap["totals"]["revenue"]["paid_invoice_count"] == 0


def test_daily_all_and_stores(client):
    everyone = body_of(summary(
        client, date_from="2026-11-08", date_to="2026-11-09", grain="day", store="全部",
    ))
    assert everyone["store_filter"] == "all"
    assert everyone["range"]["grain"] == "day"
    assert everyone["range"]["timezone"] == "Asia/Shanghai"
    assert len(everyone["buckets"]) == 2
    assert "week_start" not in everyone["buckets"][0]
    assert_public(everyone)
    assert "PHONE13900001111" not in everyone and "PHONE13900001111" not in json.dumps(everyone)

    nov8 = day_bucket(everyone, "2026-11-08")
    nov9 = day_bucket(everyone, "2026-11-09")
    assert cents(nov8["revenue"]["paid_invoice_amount"]) == 10000
    assert nov8["revenue"]["paid_invoice_count"] == 1
    assert cents(nov8["revenue"]["avg_ticket"]) == 10000
    assert cents(nov8["revenue"]["finance_cash_amount"]) == 10000
    assert cents(nov8["revenue"]["business_noncash_amount"]) == 0
    methods8 = {row["method"]: row for row in nov8["revenue"]["by_method"]}
    assert cents(methods8["shouqianba"]["amount"]) == 6000
    assert methods8["shouqianba"]["kind"] == "cash"
    assert cents(methods8["cash"]["amount"]) == 4000
    assert nov8["revenue"]["by_category"][0]["ref_type"] == "prescription"
    assert nov8["operations"]["native_visit_count"] == 4
    assert nov8["operations"]["grooming_order_count"] == 1
    assert nov8["customers"]["new_customer_count"] == 1
    assert nov8["customers"]["returning_customer_count"] == 0
    assert nov8["revenue"]["not_in_paid_revenue"]["cancelled_payment_count"] == 1
    assert cents(nov8["revenue"]["not_in_paid_revenue"]["cancelled_payment_amount"]) == 500
    assert nov8["revenue"]["not_in_paid_revenue"]["subtracted_from_paid_revenue"] is False

    assert cents(nov9["revenue"]["paid_invoice_amount"]) == 9200
    assert nov9["revenue"]["paid_invoice_count"] == 4
    assert cents(nov9["revenue"]["avg_ticket"]) == 2300
    assert cents(nov9["revenue"]["finance_cash_amount"]) == 6200
    assert cents(nov9["revenue"]["business_noncash_amount"]) == 3000
    methods9 = {row["method"]: row for row in nov9["revenue"]["by_method"]}
    assert cents(methods9["wechat"]["amount"]) == 4700
    assert methods9["wechat"]["count"] == 2
    assert cents(methods9["wallet"]["amount"]) == 3000
    assert methods9["wallet"]["kind"] == "noncash"
    assert cents(methods9["cash"]["amount"]) == 1500
    categories9 = {row["ref_type"]: cents(row["amount"]) for row in nov9["revenue"]["by_category"]}
    assert categories9 == {"prescription": 4000, "grooming": 3000, "vaccination": 1500, "exam_order": 700}
    assert "wallet_recharge" not in categories9
    side9 = nov9["revenue"]["not_in_paid_revenue"]
    assert cents(side9["wallet_recharge_principal"]) == 25000
    assert cents(side9["wallet_cash_refund"]) == 1000
    assert cents(side9["deposit_received"]) == 2000
    assert cents(side9["deposit_refund"]) == 600
    assert side9["refunded_invoice_count"] == 1
    assert cents(side9["refunded_invoice_amount"]) == 1200
    assert nov9["operations"]["walk_in_registration_count"] == 2
    assert nov9["operations"]["grooming_order_count"] == 1
    assert nov9["operations"]["hospitalization_admit_count"] == 1
    assert nov9["operations"]["anesthesia_order_count"] == 3
    assert nov9["operations"]["vaccination_count"] == 1
    assert nov9["operations"]["deworming_count"] == 1
    assert nov9["operations"]["package_sold_count"] == 1
    assert cents(nov9["operations"]["package_sold_amount"]) == 8000
    assert nov9["operations"]["package_redeem_count"] == 3
    assert nov9["operations"]["native_visit_count"] == 0
    assert nov9["customers"]["new_customer_count"] == 1
    assert nov9["customers"]["returning_customer_count"] == 2

    totals = everyone["totals"]
    assert cents(totals["revenue"]["paid_invoice_amount"]) == 19200
    assert totals["revenue"]["paid_invoice_count"] == 5
    assert cents(totals["revenue"]["avg_ticket"]) == 3840
    assert totals["customers"]["new_customer_count"] == 2
    assert totals["customers"]["returning_customer_count"] == 1
    assert totals["operations"]["native_visit_count"] == 4
    assert everyone["data_quality"]["imported_visit_count_excluded"] == 2
    assert everyone["data_quality"]["scope"] == "date_range_all_stores"
    assert everyone["metric_quality"]["package_sold"]["store_attribution"] == "unavailable"
    assert everyone["metric_quality"]["package_sold"]["available"] is True
    assert everyone["metric_quality"]["native_visit"]["included_missing_store"] is True

    heng = body_of(summary(
        client, date_from="2026-11-08", date_to="2026-11-09", grain="day",
        store="大风动物医院（横岗店）",
    ))
    assert heng["store_filter"] == "横岗店"
    heng8 = day_bucket(heng, "2026-11-08")
    heng9 = day_bucket(heng, "2026-11-09")
    assert cents(heng8["revenue"]["paid_invoice_amount"]) == 10000
    assert heng8["operations"]["native_visit_count"] == 2
    assert cents(heng9["revenue"]["paid_invoice_amount"]) == 5200
    assert heng9["revenue"]["paid_invoice_count"] == 3
    assert cents(heng9["revenue"]["avg_ticket"]) == 1733
    assert cents(heng9["revenue"]["finance_cash_amount"]) == 2200
    assert cents(heng9["revenue"]["business_noncash_amount"]) == 3000
    assert heng9["customers"]["new_customer_count"] == 1
    assert heng9["customers"]["returning_customer_count"] == 1
    assert heng9["operations"]["package_sold_count"] is None
    assert heng9["operations"]["package_sold_amount"] is None
    assert heng["metric_quality"]["package_sold"]["available"] is False
    assert heng9["operations"]["package_redeem_count"] == 2
    assert heng["metric_quality"]["package_redeem"]["store_attribution"] == "partial"
    assert heng["metric_quality"]["package_redeem"]["included_missing_store"] is False
    assert cents(heng9["revenue"]["not_in_paid_revenue"]["wallet_recharge_principal"]) == 20000
    assert heng9["operations"]["deworming_count"] == 0
    assert heng9["operations"]["anesthesia_order_count"] == 2
    assert heng["totals"]["operations"]["native_visit_count"] == 2
    assert heng["data_quality"]["imported_visit_count_excluded"] == 2
    assert heng["data_quality"]["east_store_paid_invoice_count"] == 1
    assert cents(heng["data_quality"]["east_store_paid_invoice_amount"]) == 4000
    assert heng["data_quality"]["package_sold_missing_store"] == 1
    assert cents(heng["data_quality"]["package_sold_missing_store_amount"]) == 8000
    assert heng["data_quality"]["native_visits_missing_store"] == 1
    assert_public(heng)

    dong = body_of(summary(
        client, date_from="2026-11-08", date_to="2026-11-09", grain="day", store="东环店",
    ))
    dong8 = day_bucket(dong, "2026-11-08")
    dong9 = day_bucket(dong, "2026-11-09")
    assert dong8["revenue"]["paid_invoice_count"] == 0
    assert dong8["operations"]["native_visit_count"] == 1
    assert cents(dong9["revenue"]["paid_invoice_amount"]) == 4000
    assert dong9["customers"]["new_customer_count"] == 0
    assert dong9["customers"]["returning_customer_count"] == 1
    assert dong9["operations"]["grooming_order_count"] == 1
    assert dong9["operations"]["deworming_count"] == 1
    assert dong9["operations"]["anesthesia_order_count"] == 1
    assert dong9["operations"]["package_redeem_count"] == 0
    assert dong9["operations"]["package_sold_count"] is None
    assert cents(dong["totals"]["revenue"]["paid_invoice_amount"]) + cents(
        heng["totals"]["revenue"]["paid_invoice_amount"]
    ) == cents(everyone["totals"]["revenue"]["paid_invoice_amount"])


def test_weeks_and_fallback(client):
    weeks = body_of(summary(
        client, date_from="2026-11-02", date_to="2026-11-15", grain="week", store="全部",
    ))
    assert [item["week_start"] for item in weeks["buckets"]] == ["2026-11-02", "2026-11-09"]
    first = week_bucket(weeks, "2026-11-02")
    second = week_bucket(weeks, "2026-11-09")
    assert first["period_start"] == "2026-11-02"
    assert first["period_end"] == "2026-11-08"
    assert second["period_start"] == "2026-11-09"
    assert second["period_end"] == "2026-11-15"
    assert cents(first["revenue"]["paid_invoice_amount"]) == 10000
    assert first["customers"]["new_customer_count"] == 1
    assert first["customers"]["returning_customer_count"] == 0
    assert cents(second["revenue"]["paid_invoice_amount"]) == 9200
    assert second["customers"]["new_customer_count"] == 1
    assert second["customers"]["returning_customer_count"] == 2
    assert weeks["totals"]["customers"]["new_customer_count"] == 2
    assert weeks["totals"]["customers"]["returning_customer_count"] == 1
    assert (
        first["customers"]["returning_customer_count"]
        + second["customers"]["returning_customer_count"]
    ) != weeks["totals"]["customers"]["returning_customer_count"]

    heng_weeks = body_of(summary(
        client, date_from="2026-11-02", date_to="2026-11-15", grain="week", store="横岗店",
    ))
    heng_second = week_bucket(heng_weeks, "2026-11-09")
    assert cents(heng_second["revenue"]["paid_invoice_amount"]) == 5200
    assert heng_second["customers"]["new_customer_count"] == 1
    assert heng_second["customers"]["returning_customer_count"] == 1
    dong_weeks = body_of(summary(
        client, date_from="2026-11-02", date_to="2026-11-15", grain="week", store="东环店",
    ))
    dong_first = week_bucket(dong_weeks, "2026-11-02")
    assert dong_first["revenue"]["paid_invoice_count"] == 0
    assert dong_first["operations"]["native_visit_count"] == 1
    assert cents(week_bucket(dong_weeks, "2026-11-09")["revenue"]["paid_invoice_amount"]) == 4000

    fallback = body_of(summary(
        client, date_from="2026-01-15", date_to="2026-01-15", grain="day", store="横岗店",
    ))
    only = day_bucket(fallback, "2026-01-15")
    assert cents(only["revenue"]["paid_invoice_amount"]) == 900
    assert only["revenue"]["by_method"] == [{
        "method": "wechat",
        "label": "微信",
        "kind": "cash",
        "amount": 9.0,
        "count": 1,
    }] or (
        cents(only["revenue"]["by_method"][0]["amount"]) == 900
        and only["revenue"]["by_method"][0]["method"] == "wechat"
        and only["revenue"]["by_method"][0]["count"] == 1
    )
    assert only["customers"]["new_customer_count"] == 1
    assert fallback["data_quality"]["paid_invoices_without_success_payment"] == 1
    assert_public(fallback)


def main():
    client = TestClient(app, base_url="https://testserver", follow_redirects=False)
    client.__enter__()
    try:
        seed()
        test_auth(client)
        print("  鉴权通过")
        test_validation(client)
        print("  参数校验通过")
        test_daily_all_and_stores(client)
        print("  按天和门店通过")
        test_weeks_and_fallback(client)
        print("  按周、时区边界和支付回退通过")
    finally:
        client.__exit__(None, None, None)
    print("business summary ok")


if __name__ == "__main__":
    main()
