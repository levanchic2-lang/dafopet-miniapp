"""到店挂号：按时段决定普通/急诊，并单独生成收费单。

金额用 Float，和全院收费单保持一致。时间判断一律走北京时间。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Invoice, InvoiceItem, Payment, RegistrationSettings, WalkInRegistration
from app.time_utils import BEIJING_TZ, beijing_now, beijing_today, format_beijing

KIND_NORMAL = "normal"
KIND_EMERGENCY = "emergency"
STATUS_OPEN = "open"
STATUS_CONVERTED = "converted"
STATUS_NO_VISIT = "no_visit"
STATUS_CANCELLED = "cancelled"

DEFAULT_NORMAL_FEE = 10.0
DEFAULT_EMERGENCY_FEE = 200.0
DEFAULT_DAY_START = "09:00"
DEFAULT_DAY_END = "22:00"

KIND_ZH = {
    KIND_NORMAL: "普通挂号",
    KIND_EMERGENCY: "急诊挂号",
}
STATUS_ZH = {
    STATUS_OPEN: "待处理",
    STATUS_CONVERTED: "已转病历",
    STATUS_NO_VISIT: "无需就诊",
    STATUS_CANCELLED: "已取消",
}
PAY_ZH = {
    "unpaid": "未付",
    "partial": "部分已付",
    "paid": "已付",
    "refunded": "已退款",
    "none": "无费用",
}


def parse_hhmm(value: str, fallback: str = DEFAULT_DAY_START) -> tuple[int, int]:
    raw = (value or "").strip() or fallback
    try:
        hour_s, minute_s = raw.split(":", 1)
        hour, minute = int(hour_s), int(minute_s)
    except (TypeError, ValueError):
        hour_s, minute_s = fallback.split(":", 1)
        return int(hour_s), int(minute_s)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        hour_s, minute_s = fallback.split(":", 1)
        return int(hour_s), int(minute_s)
    return hour, minute


def registration_kind(
    when: datetime,
    day_start: str = DEFAULT_DAY_START,
    day_end: str = DEFAULT_DAY_END,
) -> str:
    """日间 [day_start, day_end) 为普通挂号，其余时间为急诊。

    无时区的 datetime 按北京时间的钟面理解。带时区的会先换算到北京。
    09:00 含在普通里，22:00 起算急诊。
    """
    local = when.astimezone(BEIJING_TZ) if when.tzinfo is not None else when
    minute_of_day = local.hour * 60 + local.minute
    start_h, start_m = parse_hhmm(day_start, DEFAULT_DAY_START)
    end_h, end_m = parse_hhmm(day_end, DEFAULT_DAY_END)
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m
    if start == end:
        return KIND_EMERGENCY
    if start < end:
        is_normal = start <= minute_of_day < end
    else:
        is_normal = minute_of_day >= start or minute_of_day < end
    return KIND_NORMAL if is_normal else KIND_EMERGENCY


def beijing_wall_date(when: datetime) -> str:
    local = when.astimezone(BEIJING_TZ) if when.tzinfo is not None else when
    return local.date().isoformat()


def to_utc_naive(when: datetime) -> datetime:
    if when.tzinfo is None:
        aware = when.replace(tzinfo=BEIJING_TZ)
    else:
        aware = when.astimezone(BEIJING_TZ)
    return aware.astimezone(timezone.utc).replace(tzinfo=None)


def get_registration_settings(db: Session) -> RegistrationSettings:
    row = db.get(RegistrationSettings, 1)
    if row is None:
        row = db.query(RegistrationSettings).order_by(RegistrationSettings.id.asc()).first()
    if row is None:
        row = RegistrationSettings(
            id=1,
            normal_fee=DEFAULT_NORMAL_FEE,
            emergency_fee=DEFAULT_EMERGENCY_FEE,
            day_start=DEFAULT_DAY_START,
            day_end=DEFAULT_DAY_END,
        )
        db.add(row)
        db.flush()
    return row


def quote_registration(db: Session, when: datetime | None = None) -> dict:
    settings = get_registration_settings(db)
    moment = when or beijing_now()
    kind = registration_kind(moment, settings.day_start, settings.day_end)
    normal_fee = round(float(settings.normal_fee or 0), 2)
    emergency_fee = round(float(settings.emergency_fee or 0), 2)
    fee = normal_fee if kind == KIND_NORMAL else emergency_fee
    return {
        "kind": kind,
        "fee": fee,
        "kind_label": KIND_ZH.get(kind, "挂号"),
        "normal_fee": normal_fee,
        "emergency_fee": emergency_fee,
        "day_start": settings.day_start or DEFAULT_DAY_START,
        "day_end": settings.day_end or DEFAULT_DAY_END,
        "at": moment,
        "biz_date": beijing_wall_date(moment),
        "clock": (moment.astimezone(BEIJING_TZ) if moment.tzinfo else moment).strftime("%H:%M"),
    }


def fee_for_kind(quote: dict, kind: str) -> float:
    if kind == KIND_EMERGENCY:
        return round(float(quote["emergency_fee"]), 2)
    return round(float(quote["normal_fee"]), 2)


def _next_invoice_no(db: Session, biz_date: str) -> str:
    day = (biz_date or beijing_today().isoformat()).replace("-", "")
    count = db.query(func.count(Invoice.id)).filter(Invoice.invoice_no.like(f"{day}-%")).scalar() or 0
    return f"{day}-{int(count) + 1}"


def _attach_invoice(db: Session, reg: WalkInRegistration, actor: str) -> Invoice | None:
    amount = round(float(reg.fee_amount or 0), 2)
    if amount <= 0:
        return None
    label = KIND_ZH.get(reg.kind, "挂号")
    inv = Invoice(
        invoice_no=_next_invoice_no(db, reg.biz_date),
        customer_id=reg.customer_id,
        pet_id=reg.pet_id,
        visit_id=None,
        invoice_date=reg.biz_date or beijing_today().isoformat(),
        subtotal=amount,
        discount_amount=0.0,
        total_amount=amount,
        payment_status="unpaid",
        notes=f"挂号 #{reg.id}",
        store=reg.store or "",
        created_by=actor or "",
    )
    db.add(inv)
    db.flush()
    db.add(InvoiceItem(
        invoice_id=inv.id,
        ref_type="registration",
        ref_id=reg.id,
        description=label,
        quantity=1.0,
        unit_price=amount,
        subtotal=amount,
    ))
    reg.invoice_id = inv.id
    db.flush()
    return inv


def create_walk_in_registration(
    db: Session,
    *,
    customer_id: int | None,
    pet_id: int | None,
    store: str,
    kind: str,
    fee: float,
    note: str,
    actor: str,
    when: datetime | None = None,
) -> tuple[WalkInRegistration, bool]:
    quote = quote_registration(db, when)
    chosen = kind if kind in (KIND_NORMAL, KIND_EMERGENCY) else quote["kind"]
    amount = round(float(fee), 2)
    adjusted = chosen != quote["kind"] or abs(amount - float(quote["fee"])) > 0.009
    moment = quote["at"]
    reg = WalkInRegistration(
        customer_id=customer_id,
        pet_id=pet_id,
        store=(store or "")[:40],
        registered_at=to_utc_naive(moment),
        biz_date=quote["biz_date"],
        kind=chosen,
        fee_amount=amount,
        auto_kind=quote["kind"],
        auto_fee=float(quote["fee"]),
        status=STATUS_OPEN,
        note=(note or "")[:300],
        created_by=(actor or "")[:80],
    )
    db.add(reg)
    db.flush()
    _attach_invoice(db, reg, actor)
    return reg, adjusted


def _invoice_for(db: Session, reg: WalkInRegistration) -> Invoice | None:
    if reg.invoice_id:
        inv = db.get(Invoice, reg.invoice_id)
        if inv is not None:
            return inv
    item = (
        db.query(InvoiceItem)
        .filter(InvoiceItem.ref_type == "registration", InvoiceItem.ref_id == reg.id)
        .order_by(InvoiceItem.id.desc())
        .first()
    )
    if item is None:
        return None
    return db.get(Invoice, item.invoice_id)


def _success_paid_sum(db: Session, invoice_id: int) -> float:
    total = db.query(func.coalesce(func.sum(Payment.amount), 0)).filter(
        Payment.invoice_id == invoice_id,
        Payment.status == "success",
    ).scalar()
    return round(float(total or 0), 2)


def payment_state(db: Session, reg: WalkInRegistration) -> str:
    if round(float(reg.fee_amount or 0), 2) <= 0 and not reg.invoice_id:
        return "none"
    inv = _invoice_for(db, reg)
    if inv is None:
        return "none" if reg.status == STATUS_CANCELLED else "unpaid"
    status = (inv.payment_status or "").strip()
    if status in ("paid", "partial", "refunded"):
        return status
    if status == "cancelled":
        return "none"
    paid = _success_paid_sum(db, inv.id)
    total = round(float(inv.total_amount or 0), 2)
    if paid <= 0.009:
        return "unpaid"
    if total > 0 and paid + 0.009 < total:
        return "partial"
    return "paid"


def decorate_registration(db: Session, reg: WalkInRegistration) -> None:
    state = payment_state(db, reg)
    reg.pay_state = state
    if reg.status == STATUS_CANCELLED and state == "none" and float(reg.fee_amount or 0) > 0:
        reg.pay_label = "已撤销"
    else:
        reg.pay_label = PAY_ZH.get(state, "未付")
    reg.kind_label = KIND_ZH.get(reg.kind, "挂号")
    reg.status_label = STATUS_ZH.get(reg.status, reg.status or "")
    reg.time_label = format_beijing(reg.registered_at, "%H:%M") or "—"
    reg.fee_adjusted = bool(reg.auto_kind) and (
        reg.kind != reg.auto_kind or abs(float(reg.fee_amount or 0) - float(reg.auto_fee or 0)) > 0.009
    )


def remove_unpaid_registration_fee(db: Session, reg: WalkInRegistration) -> str:
    """取消未收款的挂号费。已收款返回 blocked，不改收费单。"""
    state = payment_state(db, reg)
    if state in ("paid", "partial"):
        return "blocked"
    if state == "refunded":
        return "kept"
    items = db.query(InvoiceItem).filter(
        InvoiceItem.ref_type == "registration",
        InvoiceItem.ref_id == reg.id,
    ).all()
    invoice_ids: list[int] = []
    for item in items:
        if item.invoice_id and item.invoice_id not in invoice_ids:
            invoice_ids.append(item.invoice_id)
        db.delete(item)
    if reg.invoice_id and reg.invoice_id not in invoice_ids:
        invoice_ids.append(reg.invoice_id)
    db.flush()
    for invoice_id in invoice_ids:
        inv = db.get(Invoice, invoice_id)
        if inv is None:
            continue
        if (inv.payment_status or "") in ("paid", "partial", "refunded"):
            continue
        if _success_paid_sum(db, inv.id) > 0.009:
            continue
        left = db.query(InvoiceItem).filter(InvoiceItem.invoice_id == inv.id).all()
        if not left:
            db.delete(inv)
            continue
        subtotal = round(sum(float(it.subtotal or 0) for it in left), 2)
        inv.subtotal = subtotal
        inv.total_amount = round(subtotal - float(inv.discount_amount or 0), 2)
        inv.updated_at = datetime.utcnow()
    reg.invoice_id = None
    db.flush()
    return "removed" if state != "none" else "none"


def mark_registration_resolved(reg: WalkInRegistration, status: str, actor: str) -> None:
    reg.status = status
    reg.resolved_by = (actor or "")[:80]
    reg.resolved_at = datetime.utcnow()
    reg.updated_at = datetime.utcnow()
