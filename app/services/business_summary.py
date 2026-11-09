"""只读经营汇总。

金额在接口内先换成「分」再输出元。DateTime 按 UTC 理解，分桶用北京日历日。
本模块只查询，不写库，也不把客户、宠物、病历正文放进返回值。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
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
    WalletTransaction,
)
from app.time_utils import to_beijing

TOKEN_ENV = "REPORTS_READONLY_TOKEN"
MAX_DAYS = 366

# 与病历编辑锁使用的批量导入来源一致。空字符串不是导入。
IMPORTED_VISIT_ORIGINS = frozenset({
    "warmsoft",
    "warmsoft_import",
    "warmsoft_import_dh",
    "import",
})

CASH_METHODS = frozenset({
    "cash", "wechat", "alipay", "shouqianba", "meituan", "third_party",
})
NONCASH_METHODS = frozenset({"wallet", "package", "deposit", "coupon"})

PAY_LABELS = {
    "cash": "现金",
    "wechat": "微信",
    "alipay": "支付宝",
    "shouqianba": "收钱吧",
    "meituan": "美团",
    "third_party": "抖音团购",
    "salary_deduction": "工资抵扣",
    "card": "刷卡",
    "groupbuy": "团购",
    "prepaid": "预付款",
    "wallet": "钱包",
    "package": "套餐核销",
    "deposit": "押金抵扣",
    "coupon": "优惠券",
    "free": "赠送 / 零结算",
    "mixed": "混合支付",
    "other": "其他",
}

CATEGORY_LABELS = {
    "prescription": "处方",
    "exam_order": "检查单",
    "sales_order": "销售单",
    "vaccination": "疫苗",
    "deworming": "驱虫",
    "grooming": "美容",
    "hospitalization": "住院笼费",
    "anesthesia": "麻醉",
    "registration": "挂号",
    "manual": "手工录入",
    "wallet_recharge": "钱包充值",
}

STORE_ALIASES = {
    "": "all",
    "全部": "all",
    "all": "all",
    "横岗店": "横岗店",
    "东环店": "东环店",
    "大风动物医院（横岗店）": "横岗店",
    "大风动物医院（东环店）": "东环店",
    "大风动物医院(横岗店)": "横岗店",
    "大风动物医院(东环店)": "东环店",
}

BANNED_KEYS = frozenset({
    "id",
    "name",
    "phone",
    "phones_extra",
    "customer_name",
    "pet_name",
    "customer_id",
    "pet_id",
    "visit_id",
    "invoice_id",
    "invoice_no",
    "payment_id",
    "deposit_id",
    "wallet_id",
    "application_id",
    "id_number",
    "address",
    "openid",
    "wechat_openid",
    "microchip_id",
    "medical_record_no",
    "chief_complaint",
    "physical_exam",
    "diagnosis",
    "treatment_plan",
    "notes",
    "note",
    "reason",
    "operator",
    "vet_name",
    "groomer_name",
    "assistant_name",
    "created_by",
    "actor",
    "ip",
    "user_agent",
    "detail",
    "phone_number",
})

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class SummaryQueryError(ValueError):
    """调用方参数不合法。路由把它变成 400。"""


class _Row:
    def __init__(self, day: date, store: str, **kwargs):
        self.day = day
        self.store = store
        for key, value in kwargs.items():
            setattr(self, key, value)


def read_reports_readonly_token() -> str:
    """每次请求现读环境变量。空白视为没配置，不用代码里的默认密钥。"""
    if TOKEN_ENV in os.environ:
        return (os.environ.get(TOKEN_ENV) or "").strip()
    return (getattr(settings, "reports_readonly_token", "") or "").strip()


def require_reports_readonly_token(supplied: str | None) -> None:
    expected = read_reports_readonly_token()
    if not expected:
        raise HTTPException(status_code=503, detail="经营汇总只读密钥未配置")
    provided = supplied or ""
    # 先哈希再比较，长度不同时也保持常量时间。
    if not secrets.compare_digest(
        hashlib.sha256(provided.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    ):
        raise HTTPException(status_code=401, detail="经营汇总只读密钥无效")


def build_business_summary(
    db: Session,
    *,
    date_from: str,
    date_to: str,
    store: str,
    grain: str,
) -> dict:
    d0, d1, store_filter, grain_name = _parse_query(date_from, date_to, store, grain)
    payload = _assemble(db, d0, d1, store_filter, grain_name)
    _ensure_public(payload)
    return payload


def _parse_query(date_from: str, date_to: str, store: str, grain: str):
    d0 = _parse_day(date_from, "date_from")
    d1 = _parse_day(date_to, "date_to")
    if d0 > d1:
        raise SummaryQueryError("date_from 不能晚于 date_to")
    if (d1 - d0).days + 1 > MAX_DAYS:
        raise SummaryQueryError("日期范围最多 366 天")
    grain_name = (grain or "").strip().lower()
    if grain_name not in ("day", "week"):
        raise SummaryQueryError("grain 必须是 day 或 week")
    store_filter = STORE_ALIASES.get((store or "").strip())
    if store_filter is None:
        raise SummaryQueryError("store 只能是 横岗店、东环店 或 全部")
    return d0, d1, store_filter, grain_name


def _parse_day(raw: str, field: str) -> date:
    text = (raw or "").strip()
    if not _DATE_RE.fullmatch(text):
        raise SummaryQueryError(f"{field} 必须是北京日历日，格式 YYYY-MM-DD")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise SummaryQueryError(f"{field} 不是有效日期") from None


def _assemble(db: Session, d0: date, d1: date, store_filter: str, grain: str) -> dict:
    internal_ids = {
        row[0]
        for row in db.query(Customer.id).filter(Customer.is_internal.is_(True)).all()
    }
    recharge_ids = {
        row[0]
        for row in db.query(InvoiceItem.invoice_id).filter(
            InvoiceItem.ref_type == "wallet_recharge"
        ).distinct().all()
        if row[0] is not None
    }
    paid_rows = db.query(
        Invoice.id,
        Invoice.customer_id,
        Invoice.pet_id,
        Invoice.store,
        Invoice.paid_at,
        Invoice.total_amount,
        Invoice.payment_method,
    ).filter(
        Invoice.payment_status == "paid",
        Invoice.paid_at.isnot(None),
    ).all()
    refunded_rows = db.query(
        Invoice.id,
        Invoice.customer_id,
        Invoice.pet_id,
        Invoice.store,
        Invoice.updated_at,
        Invoice.total_amount,
    ).filter(Invoice.payment_status == "refunded").all()
    pet_ids = set()
    for row in list(paid_rows) + list(refunded_rows):
        if not _canonical_store(row.store) and row.pet_id:
            pet_ids.add(row.pet_id)
    pet_store = _pet_stores(db, pet_ids)

    qualifying = []
    first_info: dict[int, tuple] = {}
    for row in paid_rows:
        if row.customer_id is None or row.customer_id in internal_ids:
            continue
        if row.id in recharge_ids:
            continue
        day = _beijing_day(row.paid_at)
        if day is None:
            continue
        resolved = _resolve_store(row.store, row.pet_id, pet_store)
        inv = _Row(
            day,
            resolved,
            id=row.id,
            customer_id=row.customer_id,
            cents=_to_cents(row.total_amount),
            payment_method=(row.payment_method or "").strip() or "other",
            paid_at=_as_utc_datetime(row.paid_at) or datetime.min,
        )
        qualifying.append(inv)
        current = first_info.get(row.customer_id)
        sort_key = (day, inv.paid_at, row.id)
        if current is None or sort_key < current[0]:
            first_info[row.customer_id] = (sort_key, resolved)

    in_range = [inv for inv in qualifying if d0 <= inv.day <= d1]
    invoice_ids = [inv.id for inv in in_range]
    payments = _success_payments(db, invoice_ids)
    item_map = _invoice_items(db, invoice_ids)
    missing_payment = sum(1 for inv in in_range if not payments.get(inv.id))

    revenue_view = _store_view(in_range, store_filter)
    window_start, window_end = _utc_window(d0, d1)
    s0, s1 = d0.isoformat(), d1.isoformat()

    wallet_rows = db.query(
        WalletTransaction.type,
        WalletTransaction.amount,
        WalletTransaction.bonus_amount,
        WalletTransaction.store,
        WalletTransaction.created_at,
        WalletTransaction.customer_id,
    ).filter(
        WalletTransaction.created_at >= window_start,
        WalletTransaction.created_at < window_end,
    ).all()
    recharges = []
    wallet_refunds = []
    for row in wallet_rows:
        if row.customer_id in internal_ids:
            continue
        day = _beijing_day(row.created_at)
        if day is None or not (d0 <= day <= d1):
            continue
        store_name = _canonical_store(row.store)
        kind = (row.type or "").strip()
        if kind == "recharge":
            principal = max(0, _to_cents(row.amount) - _to_cents(row.bonus_amount))
            recharges.append(_Row(day, store_name, cents=principal))
        elif kind in ("refund", "reversal") and float(row.amount or 0) < 0:
            cash = max(0, -(_to_cents(row.amount) - _to_cents(row.bonus_amount)))
            wallet_refunds.append(_Row(day, store_name, cents=cash))

    deposits = []
    for row in db.query(
        Deposit.amount, Deposit.store, Deposit.created_at, Deposit.customer_id,
    ).filter(
        Deposit.created_at >= window_start,
        Deposit.created_at < window_end,
    ).all():
        if row.customer_id in internal_ids:
            continue
        day = _beijing_day(row.created_at)
        if day is None or not (d0 <= day <= d1):
            continue
        deposits.append(_Row(day, _canonical_store(row.store), cents=_to_cents(row.amount)))

    deposit_store = {}
    refund_events = []
    audits = db.query(AuditLog.created_at, AuditLog.detail).filter(
        AuditLog.action == "deposit_refund",
        AuditLog.created_at >= window_start,
        AuditLog.created_at < window_end,
    ).all()
    parsed_refunds = []
    for created_at, detail in audits:
        day = _beijing_day(created_at)
        if day is None or not (d0 <= day <= d1):
            continue
        data = _parse_json_object(detail)
        dep_id = data.get("deposit_id")
        try:
            cents = _to_cents(data.get("amount") or 0)
        except (TypeError, ValueError):
            continue
        parsed_refunds.append((day, dep_id, cents))
    dep_ids = [dep_id for _, dep_id, _ in parsed_refunds if isinstance(dep_id, int)]
    if dep_ids:
        for dep_id, dep_store in _query_in(
            db, (Deposit.id, Deposit.store), Deposit.id, dep_ids,
        ):
            deposit_store[dep_id] = _canonical_store(dep_store)
    for day, dep_id, cents in parsed_refunds:
        refund_events.append(_Row(day, deposit_store.get(dep_id, ""), cents=cents))

    cancelled = []
    for row in db.query(
        Payment.amount, Payment.store, Payment.created_at, Payment.customer_id,
    ).filter(
        Payment.status == "cancelled",
        Payment.created_at >= window_start,
        Payment.created_at < window_end,
    ).all():
        if row.customer_id in internal_ids:
            continue
        day = _beijing_day(row.created_at)
        if day is None or not (d0 <= day <= d1):
            continue
        cancelled.append(_Row(day, _canonical_store(row.store), cents=_to_cents(row.amount)))

    refunded = []
    for row in refunded_rows:
        if row.customer_id in internal_ids:
            continue
        day = _beijing_day(row.updated_at)
        if day is None or not (d0 <= day <= d1):
            continue
        refunded.append(_Row(
            day,
            _resolve_store(row.store, row.pet_id, pet_store),
            cents=_to_cents(row.total_amount),
        ))

    grooming = _dated_rows(
        db.query(GroomingOrder.groom_date, GroomingOrder.store, GroomingOrder.status).filter(
            GroomingOrder.groom_date >= s0,
            GroomingOrder.groom_date <= s1,
        ).all(),
        lambda row: row.groom_date,
        lambda row: row.store,
        lambda row: (row.status or "").strip().lower() not in {"voided", "cancelled"},
        d0, d1,
    )
    hospitalizations = []
    for row in db.query(
        Hospitalization.admitted_at, Hospitalization.store, Hospitalization.status,
    ).filter(
        Hospitalization.admitted_at >= window_start,
        Hospitalization.admitted_at < window_end,
    ).all():
        if (row.status or "").strip().lower() == "cancelled":
            continue
        day = _beijing_day(row.admitted_at)
        if day is None or not (d0 <= day <= d1):
            continue
        hospitalizations.append(_Row(day, _canonical_store(row.store)))
    anesthesia = _dated_rows(
        db.query(
            AnesthesiaOrder.anesth_date, AnesthesiaOrder.store, AnesthesiaOrder.status,
        ).filter(
            AnesthesiaOrder.anesth_date >= s0,
            AnesthesiaOrder.anesth_date <= s1,
        ).all(),
        lambda row: row.anesth_date,
        lambda row: row.store,
        lambda row: (row.status or "").strip().lower() not in {"voided", "cancelled"},
        d0, d1,
    )
    walk_ins = _dated_rows(
        db.query(
            WalkInRegistration.biz_date, WalkInRegistration.store, WalkInRegistration.status,
        ).filter(
            WalkInRegistration.biz_date >= s0,
            WalkInRegistration.biz_date <= s1,
        ).all(),
        lambda row: row.biz_date,
        lambda row: row.store,
        lambda row: (row.status or "").strip().lower() != "cancelled",
        d0, d1,
    )
    vaccinations = _pet_store_rows(
        db.query(Vaccination.vaccinated_date, Vaccination.status, Pet.store).outerjoin(
            Pet, Pet.id == Vaccination.pet_id,
        ).filter(
            Vaccination.vaccinated_date >= s0,
            Vaccination.vaccinated_date <= s1,
        ).all(),
        d0, d1, lambda status: (status or "").strip().lower() != "voided",
    )
    dewormings = _pet_store_rows(
        db.query(DewormingRecord.deworm_date, DewormingRecord.status, Pet.store).outerjoin(
            Pet, Pet.id == DewormingRecord.pet_id,
        ).filter(
            DewormingRecord.deworm_date >= s0,
            DewormingRecord.deworm_date <= s1,
        ).all(),
        d0, d1, lambda status: (status or "").strip().lower() != "voided",
    )
    packages = []
    for row in db.query(
        CustomerPackage.purchase_date,
        CustomerPackage.store,
        CustomerPackage.sell_price,
        CustomerPackage.status,
    ).filter(
        CustomerPackage.purchase_date >= s0,
        CustomerPackage.purchase_date <= s1,
    ).all():
        if (row.status or "").strip().lower() in {"refunded", "cancelled"}:
            continue
        day = _parse_biz_day(row.purchase_date)
        if day is None or not (d0 <= day <= d1):
            continue
        packages.append(_Row(day, _canonical_store(row.store), cents=_to_cents(row.sell_price), count=1))
    redemptions = []
    for row in db.query(
        PackageRedemption.created_at,
        PackageRedemption.store,
        PackageRedemption.used_count,
        PackageRedemption.status,
    ).filter(
        PackageRedemption.created_at >= window_start,
        PackageRedemption.created_at < window_end,
    ).all():
        if (row.status or "").strip().lower() in {"reversed", "cancelled"}:
            continue
        day = _beijing_day(row.created_at)
        if day is None or not (d0 <= day <= d1):
            continue
        redemptions.append(_Row(
            day, _canonical_store(row.store), count=int(row.used_count or 0),
        ))

    visits = db.query(Visit.visit_date, Visit.store, Visit.data_origin).filter(
        Visit.visit_date >= s0,
        Visit.visit_date <= s1,
    ).all()
    native_visits = []
    imported_count = 0
    for visit_date, visit_store, origin in visits:
        day = _parse_biz_day(visit_date)
        if day is None or not (d0 <= day <= d1):
            continue
        if _is_imported_visit(origin):
            imported_count += 1
            continue
        native_visits.append(_Row(day, _canonical_store(visit_store)))

    views = {
        "paid_invoice": revenue_view,
        "wallet_recharge_principal": _store_view(recharges, store_filter),
        "wallet_cash_refund": _store_view(wallet_refunds, store_filter),
        "deposit_received": _store_view(deposits, store_filter),
        "deposit_refund": _store_view(refund_events, store_filter),
        "cancelled_payment": _store_view(cancelled, store_filter),
        "refunded_invoice": _store_view(refunded, store_filter),
        "walk_in_registration": _store_view(walk_ins, store_filter),
        "grooming_order": _store_view(grooming, store_filter),
        "hospitalization_admit": _store_view(hospitalizations, store_filter),
        "anesthesia_order": _store_view(anesthesia, store_filter),
        "vaccination": _store_view(vaccinations, store_filter),
        "deworming": _store_view(dewormings, store_filter),
        "package_sold": _store_view(packages, store_filter),
        "package_redeem": _store_view(redemptions, store_filter),
        "native_visit": _store_view(native_visits, store_filter),
    }

    order, accs, customer_sets, meta = _empty_buckets(d0, d1, grain)
    _fill_invoices(
        accs, customer_sets, revenue_view, payments, item_map, grain, d0, d1,
    )
    _fill_cents(accs, views["wallet_recharge_principal"], "wallet_recharge_cents", grain, d0, d1)
    _fill_cents(accs, views["wallet_cash_refund"], "wallet_refund_cents", grain, d0, d1)
    _fill_cents(accs, views["deposit_received"], "deposit_in_cents", grain, d0, d1)
    _fill_cents(accs, views["deposit_refund"], "deposit_refund_cents", grain, d0, d1)
    _fill_count_cents(accs, views["cancelled_payment"], "cancelled_count", "cancelled_cents", grain, d0, d1)
    _fill_count_cents(accs, views["refunded_invoice"], "refunded_count", "refunded_cents", grain, d0, d1)
    _fill_count(accs, views["walk_in_registration"], "walk_in", grain, d0, d1)
    _fill_count(accs, views["grooming_order"], "grooming", grain, d0, d1)
    _fill_count(accs, views["hospitalization_admit"], "hosp", grain, d0, d1)
    _fill_count(accs, views["anesthesia_order"], "anesth", grain, d0, d1)
    _fill_count(accs, views["vaccination"], "vacc", grain, d0, d1)
    _fill_count(accs, views["deworming"], "deworm", grain, d0, d1)
    _fill_package(accs, views["package_sold"], grain, d0, d1)
    _fill_redeem(accs, views["package_redeem"], grain, d0, d1)
    _fill_count(accs, views["native_visit"], "native", grain, d0, d1)

    total_acc = _new_acc()
    total_customers: set[int] = set()
    for key in order:
        _finalize_payments(accs[key])
        _add_acc(total_acc, accs[key])
        total_customers.update(customer_sets[key])
    _finalize_payments(total_acc)

    buckets = []
    for key in order:
        start, end, monday = meta[key]
        buckets.append(_render_period(
            accs[key], start, end, monday, customer_sets[key], first_info, store_filter, views,
        ))
    totals = _render_period(
        total_acc, d0, d1, None, total_customers, first_info, store_filter, views,
        include_period=False,
    )
    return {
        "schema_version": 1,
        "read_only": True,
        "range": {
            "from": d0.isoformat(),
            "to": d1.isoformat(),
            "timezone": "Asia/Shanghai",
            "grain": grain,
            "week_starts_on": "monday",
            "inclusive": True,
            "max_days": MAX_DAYS,
            "empty_periods_included": True,
        },
        "store_filter": store_filter,
        "definitions": _definitions(),
        "limitations": _limitations(),
        "metric_quality": _metric_quality(views),
        "totals": totals,
        "buckets": buckets,
        "data_quality": _data_quality(
            in_range, missing_payment, packages, redemptions, native_visits,
            vaccinations, dewormings, recharges, imported_count,
        ),
    }


def _fill_invoices(accs, customer_sets, view, payments, item_map, grain, d0, d1):
    if view["mode"] != "count":
        return
    for inv in view["chosen"]:
        if not (d0 <= inv.day <= d1):
            continue
        key = _key_for(inv.day, grain)
        acc = accs.get(key)
        if acc is None:
            continue
        acc["paid_count"] += 1
        acc["paid_cents"] += inv.cents
        plist = payments.get(inv.id) or []
        if plist:
            acc["has_success_payment"] = True
            for method, cents in plist:
                _add_method(acc, method, cents, 1)
        else:
            acc["bare"].append((inv.cents, inv.payment_method))
        _add_categories(acc, item_map.get(inv.id) or [], inv.cents)
        customer_sets[key].add(inv.customer_id)


def _fill_cents(accs, view, field, grain, d0, d1):
    if view["mode"] != "count":
        return
    for row in view["chosen"]:
        if d0 <= row.day <= d1 and (key := _key_for(row.day, grain)) in accs:
            accs[key][field] += row.cents


def _fill_count(accs, view, field, grain, d0, d1):
    if view["mode"] != "count":
        return
    for row in view["chosen"]:
        if d0 <= row.day <= d1 and (key := _key_for(row.day, grain)) in accs:
            accs[key][field] += 1


def _fill_count_cents(accs, view, count_field, cents_field, grain, d0, d1):
    if view["mode"] != "count":
        return
    for row in view["chosen"]:
        if d0 <= row.day <= d1 and (key := _key_for(row.day, grain)) in accs:
            accs[key][count_field] += 1
            accs[key][cents_field] += row.cents


def _fill_package(accs, view, grain, d0, d1):
    if view["mode"] != "count":
        return
    for row in view["chosen"]:
        if d0 <= row.day <= d1 and (key := _key_for(row.day, grain)) in accs:
            accs[key]["package_count"] += row.count
            accs[key]["package_cents"] += row.cents


def _fill_redeem(accs, view, grain, d0, d1):
    if view["mode"] != "count":
        return
    for row in view["chosen"]:
        if d0 <= row.day <= d1 and (key := _key_for(row.day, grain)) in accs:
            accs[key]["redeem"] += row.count


def _render_period(acc, start, end, monday, customer_ids, first_info, store_filter, views, include_period=True):
    paid_available = views["paid_invoice"]["mode"] == "count"
    if paid_available:
        new_count, returning_count = _customer_counts(
            customer_ids, first_info, start, end, store_filter,
        )
        paid_count = acc["paid_count"]
        paid_amount = _yuan(acc["paid_cents"])
        avg_ticket = _avg_yuan(acc["paid_cents"], acc["paid_count"])
        cash_cents, noncash_cents, other_cents = _method_kinds(acc["methods"])
        by_method = _method_list(acc["methods"])
        by_category = _category_list(acc["categories"])
    else:
        new_count = returning_count = None
        paid_count = paid_amount = avg_ticket = None
        cash_cents = noncash_cents = other_cents = None
        by_method = by_category = None
    body = {
        "revenue": {
            "paid_invoice_count": paid_count,
            "paid_invoice_amount": paid_amount,
            "avg_ticket": avg_ticket,
            "finance_cash_amount": None if cash_cents is None else _yuan(cash_cents),
            "business_noncash_amount": None if noncash_cents is None else _yuan(noncash_cents),
            "other_method_amount": None if other_cents is None else _yuan(other_cents),
            "by_method": by_method,
            "by_category": by_category,
            "not_in_paid_revenue": {
                "subtracted_from_paid_revenue": False,
                "wallet_recharge_principal": _optional_yuan(views["wallet_recharge_principal"], acc["wallet_recharge_cents"]),
                "wallet_cash_refund": _optional_yuan(views["wallet_cash_refund"], acc["wallet_refund_cents"]),
                "deposit_received": _optional_yuan(views["deposit_received"], acc["deposit_in_cents"]),
                "deposit_refund": _optional_yuan(views["deposit_refund"], acc["deposit_refund_cents"]),
                "cancelled_payment_count": _optional_count(views["cancelled_payment"], acc["cancelled_count"]),
                "cancelled_payment_amount": _optional_yuan(views["cancelled_payment"], acc["cancelled_cents"]),
                "refunded_invoice_count": _optional_count(views["refunded_invoice"], acc["refunded_count"]),
                "refunded_invoice_amount": _optional_yuan(views["refunded_invoice"], acc["refunded_cents"]),
            },
        },
        "operations": {
            "walk_in_registration_count": _optional_count(views["walk_in_registration"], acc["walk_in"]),
            "grooming_order_count": _optional_count(views["grooming_order"], acc["grooming"]),
            "hospitalization_admit_count": _optional_count(views["hospitalization_admit"], acc["hosp"]),
            "anesthesia_order_count": _optional_count(views["anesthesia_order"], acc["anesth"]),
            "vaccination_count": _optional_count(views["vaccination"], acc["vacc"]),
            "deworming_count": _optional_count(views["deworming"], acc["deworm"]),
            "package_sold_count": _optional_count(views["package_sold"], acc["package_count"]),
            "package_sold_amount": _optional_yuan(views["package_sold"], acc["package_cents"]),
            "package_redeem_count": _optional_count(views["package_redeem"], acc["redeem"]),
            "native_visit_count": _optional_count(views["native_visit"], acc["native"]),
        },
        "customers": {
            "new_customer_count": new_count,
            "returning_customer_count": returning_count,
        },
    }
    if not include_period:
        return body
    period = {
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
    }
    if monday is not None:
        period["week_start"] = monday.isoformat()
    period.update(body)
    return period


def _customer_counts(customer_ids, first_info, start: date, end: date, store_filter: str):
    new_count = 0
    returning_count = 0
    for cid in customer_ids:
        info = first_info.get(cid)
        if info is None:
            continue
        first_day = info[0][0]
        first_store = info[1]
        if first_day < start:
            returning_count += 1
        elif start <= first_day <= end and (store_filter == "all" or first_store == store_filter):
            new_count += 1
    return new_count, returning_count


def _optional_yuan(view, cents):
    if view["mode"] != "count":
        return None
    return _yuan(cents)


def _optional_count(view, count):
    if view["mode"] != "count":
        return None
    return int(count)


def _new_acc():
    return {
        "paid_count": 0,
        "paid_cents": 0,
        "methods": {},
        "categories": {},
        "has_success_payment": False,
        "bare": [],
        "wallet_recharge_cents": 0,
        "wallet_refund_cents": 0,
        "deposit_in_cents": 0,
        "deposit_refund_cents": 0,
        "cancelled_count": 0,
        "cancelled_cents": 0,
        "refunded_count": 0,
        "refunded_cents": 0,
        "walk_in": 0,
        "grooming": 0,
        "hosp": 0,
        "anesth": 0,
        "vacc": 0,
        "deworm": 0,
        "package_count": 0,
        "package_cents": 0,
        "redeem": 0,
        "native": 0,
    }


def _add_acc(total, part):
    total["paid_count"] += part["paid_count"]
    total["paid_cents"] += part["paid_cents"]
    if part["has_success_payment"]:
        total["has_success_payment"] = True
    for method, (cents, count) in part["methods"].items():
        _add_method(total, method, cents, count)
    for ref_type, cents in part["categories"].items():
        total["categories"][ref_type] = total["categories"].get(ref_type, 0) + cents
    total["bare"].extend(part["bare"])
    for field in (
        "wallet_recharge_cents", "wallet_refund_cents", "deposit_in_cents",
        "deposit_refund_cents", "cancelled_count", "cancelled_cents",
        "refunded_count", "refunded_cents", "walk_in", "grooming", "hosp",
        "anesth", "vacc", "deworm", "package_count", "package_cents",
        "redeem", "native",
    ):
        total[field] += part[field]


def _finalize_payments(acc):
    if not acc["has_success_payment"]:
        for cents, method in acc["bare"]:
            _add_method(acc, method, cents, 1)
    acc["bare"].clear()


def _add_method(acc, method: str, cents: int, count: int):
    slot = acc["methods"].setdefault(method or "other", [0, 0])
    slot[0] += cents
    slot[1] += count


def _add_categories(acc, items, net_cents: int):
    gross = sum(cents for _, cents in items)
    if gross <= 0:
        if net_cents:
            acc["categories"]["manual"] = acc["categories"].get("manual", 0) + net_cents
        return
    shares = []
    allocated = 0
    for ref_type, cents in items:
        share = int((Decimal(cents) * Decimal(net_cents) / Decimal(gross)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP,
        ))
        shares.append([ref_type or "manual", share])
        allocated += share
    drift = net_cents - allocated
    if shares and drift:
        shares.sort(key=lambda item: abs(item[1]), reverse=True)
        shares[0][1] += drift
    for ref_type, share in shares:
        if share:
            acc["categories"][ref_type] = acc["categories"].get(ref_type, 0) + share


def _method_kinds(methods):
    cash = noncash = other = 0
    for method, (cents, _count) in methods.items():
        if method in CASH_METHODS:
            cash += cents
        elif method in NONCASH_METHODS:
            noncash += cents
        else:
            other += cents
    return cash, noncash, other


def _method_list(methods):
    rows = []
    for method, (cents, count) in methods.items():
        if method in CASH_METHODS:
            kind = "cash"
        elif method in NONCASH_METHODS:
            kind = "noncash"
        else:
            kind = "other"
        rows.append({
            "method": method,
            "label": PAY_LABELS.get(method, "其他"),
            "kind": kind,
            "amount": _yuan(cents),
            "count": count,
        })
    rows.sort(key=lambda row: (-row["amount"], row["method"]))
    return rows


def _category_list(categories):
    rows = [
        {
            "ref_type": ref_type,
            "label": CATEGORY_LABELS.get(ref_type, ref_type or "其他"),
            "amount": _yuan(cents),
        }
        for ref_type, cents in categories.items()
        if cents
    ]
    rows.sort(key=lambda row: (-row["amount"], row["ref_type"]))
    return rows


def _empty_buckets(d0: date, d1: date, grain: str):
    order = []
    accs = {}
    customer_sets = {}
    meta = {}
    for key, start, end, monday in _iter_buckets(d0, d1, grain):
        order.append(key)
        accs[key] = _new_acc()
        customer_sets[key] = set()
        meta[key] = (start, end, monday)
    return order, accs, customer_sets, meta


def _iter_buckets(d0: date, d1: date, grain: str):
    if grain == "day":
        cur = d0
        while cur <= d1:
            yield cur.isoformat(), cur, cur, None
            cur += timedelta(days=1)
        return
    monday = d0 - timedelta(days=d0.weekday())
    while monday <= d1:
        start = max(monday, d0)
        end = min(monday + timedelta(days=6), d1)
        yield monday.isoformat(), start, end, monday
        monday += timedelta(days=7)


def _key_for(day: date, grain: str) -> str:
    if grain == "day":
        return day.isoformat()
    monday = day - timedelta(days=day.weekday())
    return monday.isoformat()


def _row_units(row) -> int:
    return int(getattr(row, "count", 1) or 0)


def _store_view(records, store_filter: str) -> dict:
    missing = [row for row in records if not row.store]
    missing_units = sum(_row_units(row) for row in missing)
    if store_filter == "all":
        if records and len(missing) == len(records):
            attribution = "unavailable"
        elif missing:
            attribution = "partial"
        else:
            attribution = "ok"
        return {
            "mode": "count",
            "chosen": list(records),
            "missing_count": missing_units,
            "attribution": attribution,
            "included_missing_store": bool(missing),
        }
    matched = [row for row in records if row.store == store_filter]
    other = [row for row in records if row.store and row.store != store_filter]
    if records and not matched and not other:
        return {
            "mode": "unavailable",
            "chosen": [],
            "missing_count": missing_units,
            "attribution": "unavailable",
            "included_missing_store": False,
        }
    return {
        "mode": "count",
        "chosen": matched,
        "missing_count": missing_units,
        "attribution": "partial" if missing else "ok",
        "included_missing_store": False,
    }


def _metric_quality(views: dict) -> dict:
    text = {
        "paid_invoice": "已付发票",
        "wallet_recharge_principal": "钱包充值本金",
        "wallet_cash_refund": "钱包现金退款",
        "deposit_received": "押金收取",
        "deposit_refund": "押金退款",
        "cancelled_payment": "作废支付",
        "refunded_invoice": "整单退款发票",
        "walk_in_registration": "到店挂号",
        "grooming_order": "美容单",
        "hospitalization_admit": "住院入院",
        "anesthesia_order": "麻醉单",
        "vaccination": "疫苗记录",
        "deworming": "驱虫记录",
        "package_sold": "套餐售卖",
        "package_redeem": "套餐核销",
        "native_visit": "原生就诊",
    }
    quality = {key: _quality_payload(views[key], label) for key, label in text.items()}
    quality["new_customer"] = dict(quality["paid_invoice"])
    quality["new_customer"]["explanation"] = (
        "新客沿用已付发票的门店口径：只把系统内第一张已付发票落在本区间、且该张发票归属当前门店筛选的客户算进来。"
        + quality["paid_invoice"]["explanation"]
    )
    quality["returning_customer"] = dict(quality["paid_invoice"])
    quality["returning_customer"]["explanation"] = (
        "老客是本桶里有已付发票、且系统内第一张已付发票早于本桶起点的客户。第一张发票可以在别的门店。"
        + quality["paid_invoice"]["explanation"]
    )
    return quality


def _quality_payload(view: dict, noun: str) -> dict:
    missing = view["missing_count"]
    if view["mode"] != "count":
        explanation = f"本区间{noun}的门店都是空的，不能归到所选门店，所以没有给数字。"
    elif view["attribution"] == "unavailable":
        explanation = f"{noun}给出的是全部门店合计，但记录上的门店都是空的，不能拆成横岗店或东环店。"
    elif view["attribution"] == "partial" and view["included_missing_store"]:
        explanation = f"合计包含没有门店的{noun} {missing}，不能把这个数拆回某一家店。"
    elif view["attribution"] == "partial":
        explanation = f"只统计门店等于筛选条件的{noun}。另有没有门店的 {missing} 没有算进这个数字。"
    else:
        explanation = f"按记录上的门店统计{noun}。"
    return {
        "available": view["mode"] == "count",
        "store_attribution": view["attribution"],
        "missing_store_count": missing,
        "included_missing_store": view["included_missing_store"],
        "explanation": explanation,
    }


def _data_quality(in_range, missing_payment, packages, redemptions, native_visits, vaccinations, dewormings, recharges, imported_count):
    def _sum_store(rows, store_name):
        chosen = [row for row in rows if row.store == store_name]
        return len(chosen), sum(row.cents for row in chosen)

    east_count, east_cents = _sum_store(in_range, "东环店")
    heng_count, heng_cents = _sum_store(in_range, "横岗店")
    return {
        "scope": "date_range_all_stores",
        "explanation": "这里的计数覆盖整个日期范围的全部门店，用来看数据缺口，不是当前门店筛选后的结果。",
        "amount_storage": "float",
        "amount_output": "yuan_rounded_half_up_to_cent",
        "east_store_paid_invoice_count": east_count,
        "east_store_paid_invoice_amount": _yuan(east_cents),
        "henggang_paid_invoice_count": heng_count,
        "henggang_paid_invoice_amount": _yuan(heng_cents),
        "paid_invoices_missing_store": sum(1 for row in in_range if not row.store),
        "paid_invoices_without_success_payment": missing_payment,
        "imported_visit_count_excluded": imported_count,
        "native_visits_missing_store": sum(1 for row in native_visits if not row.store),
        "vaccination_missing_store": sum(1 for row in vaccinations if not row.store),
        "deworming_missing_store": sum(1 for row in dewormings if not row.store),
        "package_sold_missing_store": sum(1 for row in packages if not row.store),
        "package_sold_with_store": sum(1 for row in packages if row.store),
        "package_sold_missing_store_amount": _yuan(sum(row.cents for row in packages if not row.store)),
        "package_redeem_missing_store": sum(row.count for row in redemptions if not row.store),
        "wallet_recharge_missing_store": sum(1 for row in recharges if not row.store),
    }


def _definitions() -> dict:
    return {
        "revenue.paid_invoice_amount": (
            "已付发票 total_amount 之和，对齐电脑端 /admin/reports/revenue。"
            "只要 payment_status=paid，按 paid_at 转成北京日。剔除员工内购客户，也整张剔除含 wallet_recharge 明细的发票。"
            "customer_id 为空的发票同样不计入，因为营收页的 NOT IN 子查询会把它们滤掉。"
            "门店用发票 store，空了才用宠物档案 store，筛选时不回退支付流水的 store。"
            "这是业务收入，含钱包、套餐、押金、优惠券抵扣，不等于现金进账。"
        ),
        "revenue.paid_invoice_count": "上述已付发票的张数。一张发票多笔支付仍算 1 张。",
        "revenue.avg_ticket": "已付发票金额除以已付张数，四舍五入到分。张数为 0 时是 0。不是支付笔数的平均值。",
        "revenue.finance_cash_amount": (
            "这些已付发票上 status=success 的支付流水里，现金类方式合计："
            "cash、wechat、alipay、shouqianba、meituan、third_party。"
            "工资抵扣不在现金类里。某个桶完全没有成功支付时，才回退用发票 payment_method 和 total_amount。"
        ),
        "revenue.business_noncash_amount": "成功支付里的 wallet、package、deposit、coupon 合计。这是业务抵扣，不是新的现金。",
        "revenue.other_method_amount": "成功支付里不属于现金类、也不属于钱包/套餐/押金/券的金额，例如工资抵扣、赠送或未识别方式。",
        "revenue.by_method": "支付方式拆分。count 是成功支付笔数，不是发票张数。kind=cash 或 noncash 或 other。",
        "revenue.by_category": (
            "按 invoice_items.ref_type 把折扣后的发票实收摊到收费类型，口径同营收页。"
            "同一张发票各项摊完后等于该张 total_amount。钱包充值发票已整张排除，不会出现在这里。"
        ),
        "revenue.not_in_paid_revenue": (
            "这些数字都不从 paid_invoice_amount 里扣，也不加进去。"
            "subtracted_from_paid_revenue 固定是 false。"
            "整单退款会把发票改成 refunded 并清空 paid_at，所以它已经不在已付金额里，不会在退款日再记一笔负数。"
            "作废支付按 payments.created_at 的北京日计数；支付表没有作废时间，这个日期是原收款时间。"
            "钱包现金退只统计 type 为 refund 或 reversal 且 amount<0 的本金部分。"
            "押金退来自 audit_logs.action=deposit_refund 的金额，按日志时间的北京日。"
            "手机端 /m/reports/revenue 按支付流水汇总，而且近 7/30 天的过滤没有统一加 8 小时，不要和这里直接对比。"
            "业绩页 /admin/reports/performance 没有剔除钱包充值发票，和这里也不相同。"
        ),
        "operations.walk_in_registration_count": (
            "walk_in_registrations 里状态不是 cancelled 的条数，按 biz_date。这是到店挂号数，不是发票上的挂号费金额。"
            "这张表从 2026-11-04 才开始有数据，更早的区间会接近 0，不能当成历史挂号量。"
        ),
        "operations.grooming_order_count": "美容单张数，按 groom_date，排除 voided 和 cancelled。不是美容收入；收入看收费类型里的美容。",
        "operations.hospitalization_admit_count": "住院入院数。按 admitted_at 的北京日，排除 cancelled。含在院和已出院，不是笼费金额。",
        "operations.anesthesia_order_count": (
            "麻醉单张数，手术的代理指标，不是完整手术台账。按 anesth_date，排除 voided 和 cancelled。"
            "不要把这个字段理解成手术完成台数。"
        ),
        "operations.vaccination_count": "疫苗接种记录数，按 vaccinated_date，排除 voided。门店来自宠物档案 store，疫苗表自己没有门店。不是收费类型里的疫苗金额。",
        "operations.deworming_count": "驱虫记录数，按 deworm_date，排除 voided。门店来自宠物档案 store。",
        "operations.package_sold_count": (
            "套餐售卖张数，按 purchase_date，排除 refunded 和 cancelled。"
            "customer_packages.store 在生产数据里是空的，筛选具体门店时如果无法归属就不会给数字。"
            "全部门店合计也不能拆店。售卖金额可能和已付发票重复，不要再加进营收。"
        ),
        "operations.package_sold_amount": "上述套餐的 sell_price 之和。门店归属规则和张数相同。",
        "operations.package_redeem_count": (
            "套餐核销次数，是 package_redemptions.used_count 之和，不是行数。"
            "排除 reversed 和 cancelled，按 created_at 的北京日。store 为空的次数在筛选具体门店时不算进该店。"
        ),
        "operations.native_visit_count": (
            "本系统原生就诊数，按 visit_date。排除 data_origin 为 warmsoft、warmsoft_import、warmsoft_import_dh、import 的导入病历。"
            "data_origin 为空的算原生。导入条数只在 data_quality.imported_visit_count_excluded 里给总数，不进这个字段。"
        ),
        "customers.new_customer_count": (
            "新客：系统内第一张合格已付发票的北京日落在本桶内，并且这张发票的门店符合筛选。"
            "合格发票与营收相同，不含员工内购和钱包充值。这不是医院真实第一次到店，客户档案 created_at 可以早到 2018。"
            "同一天如果第一张发票在 A 店、又在 B 店消费，只算 A 店新客。"
            "各桶新客相加等于整个区间的新客；老客不能这样加。"
        ),
        "customers.returning_customer_count": (
            "老客：本桶内有合格已付发票，且系统内第一张合格已付发票早于本桶 period_start。"
            "周桶的起点是该桶实际覆盖的第一天；查询从周中开始时，起点不是更早的那个周一。"
            "按整个区间算的老客，不等于各天或各周老客相加，因为同一个人可以在多天重复出现。"
        ),
    }


def _limitations() -> list[str]:
    return [
        "东环店目前几乎没有收银数据，发票和支付基本都记在横岗店。筛选东环店时营收可能是 0，这不代表门店没有线下营业。",
        "套餐售卖的 store 为空，不能按门店拆。接口在无法归属时返回 null，而不是 0。",
        "到店挂号从 2026-11-04 才开始落库，不能回看更早的挂号量。",
        "没有独立手术完成表。anesthesia_order_count 只是麻醉单，作为手术代理。",
        "新客和老客按系统内首次已付发票判断，不是客户档案创建日，也不是医院真实首次到院。",
        "金额在数据库里是 FLOAT。接口先按分四舍五入再输出元，和营收页的原始浮点相加可能差 1 分。",
        "整单退款会从原来的已付金额里消失，不会在退款当天记负数。作废支付没有单独的作废时间。",
        "导入病历（warmsoft 以及 warmsoft_import、warmsoft_import_dh、import）不进原生就诊数。",
        "本接口对齐电脑端营收页的已付发票。手机端收款报表按支付流水，时间过滤也没有统一加 8 小时。业绩页没有剔除钱包充值发票。",
    ]


def _dated_rows(rows, date_of, store_of, keep, d0, d1):
    out = []
    for row in rows:
        if not keep(row):
            continue
        day = _parse_biz_day(date_of(row))
        if day is None or not (d0 <= day <= d1):
            continue
        out.append(_Row(day, _canonical_store(store_of(row))))
    return out


def _pet_store_rows(rows, d0, d1, keep_status):
    out = []
    for biz_date, status, pet_store in rows:
        if not keep_status(status):
            continue
        day = _parse_biz_day(biz_date)
        if day is None or not (d0 <= day <= d1):
            continue
        out.append(_Row(day, _canonical_store(pet_store)))
    return out


def _success_payments(db: Session, invoice_ids: list[int]) -> dict[int, list[tuple[str, int]]]:
    grouped: dict[int, list[tuple[str, int]]] = {}
    for invoice_id, method, amount in _query_in(
        db,
        (Payment.invoice_id, Payment.method, Payment.amount),
        Payment.invoice_id,
        invoice_ids,
        extra_filter=lambda query: query.filter(Payment.status == "success"),
    ):
        name = (method or "").strip() or "other"
        grouped.setdefault(invoice_id, []).append((name, _to_cents(amount)))
    return grouped


def _invoice_items(db: Session, invoice_ids: list[int]) -> dict[int, list[tuple[str, int]]]:
    grouped: dict[int, list[tuple[str, int]]] = {}
    for invoice_id, ref_type, subtotal in _query_in(
        db,
        (InvoiceItem.invoice_id, InvoiceItem.ref_type, InvoiceItem.subtotal),
        InvoiceItem.invoice_id,
        invoice_ids,
    ):
        name = (ref_type or "").strip() or "manual"
        grouped.setdefault(invoice_id, []).append((name, _to_cents(subtotal)))
    return grouped


def _pet_stores(db: Session, pet_ids: set[int]) -> dict[int, str]:
    found = {}
    for pet_id, store in _query_in(db, (Pet.id, Pet.store), Pet.id, list(pet_ids)):
        found[pet_id] = store or ""
    return found


def _query_in(db: Session, columns, column, ids, extra_filter=None):
    clean = [item for item in dict.fromkeys(ids) if item is not None]
    rows = []
    for start in range(0, len(clean), 400):
        query = db.query(*columns).filter(column.in_(clean[start:start + 400]))
        if extra_filter is not None:
            query = extra_filter(query)
        rows.extend(query.all())
    return rows


def _resolve_store(store, pet_id, pet_store: dict[int, str]) -> str:
    resolved = _canonical_store(store)
    if resolved:
        return resolved
    if pet_id:
        return _canonical_store(pet_store.get(pet_id, ""))
    return ""


def _canonical_store(raw) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    mapped = STORE_ALIASES.get(text)
    if mapped and mapped != "all":
        return mapped
    return text


def _is_imported_visit(origin) -> bool:
    return (origin or "").strip().lower() in IMPORTED_VISIT_ORIGINS


def _parse_biz_day(raw) -> date | None:
    text = (raw or "").strip()[:10]
    if not _DATE_RE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _as_utc_datetime(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip().replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text[:26], fmt)
            except ValueError:
                continue
    return None


def _beijing_day(value) -> date | None:
    parsed = _as_utc_datetime(value)
    if parsed is None:
        return None
    converted = to_beijing(parsed)
    return converted.date() if converted else None


def _utc_window(d0: date, d1: date):
    start = datetime(d0.year, d0.month, d0.day) - timedelta(days=1)
    end = datetime(d1.year, d1.month, d1.day) + timedelta(days=2)
    return start, end


def _parse_json_object(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _to_cents(value) -> int:
    amount = Decimal(str(float(value or 0))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return int(amount * 100)


def _yuan(cents: int) -> float:
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(int(cents)), 100)
    return float(f"{sign}{whole}.{frac:02d}")


def _avg_yuan(cents: int, count: int) -> float:
    if count <= 0:
        return 0.0
    value = (Decimal(int(cents)) / Decimal(int(count)) / Decimal(100)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP,
    )
    return float(value)


def _ensure_public(value, path: str = "$") -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() in BANNED_KEYS:
                raise RuntimeError(f"汇总结果含不允许的字段 {path}.{key}")
            _ensure_public(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _ensure_public(item, f"{path}[{index}]")
        return
    raise RuntimeError(f"汇总结果含不允许的类型 {path}")
