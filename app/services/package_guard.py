"""预付套餐提醒与核销闸门。

规则（不改旧卡上的宠物、门店、次数和价格）：
- 目录里人工勾选的库存品目才算「能抵」。名字相似不算。
- 卡上写了宠物，就只能这只宠物用。没写宠物不等于全家都能用，核销时必须当次确认。
- 目录限定了门店，就只能该店核销。目录是通用（门店为空）时两店都可以，
  不把「售出门店」再收紧成新限制。目录已删除时，才退回卡上记录的售出门店。
- 未配置可抵项目的卡只提醒，不拦截无关项目的正常收费。
  若员工仍要核销，必须点名具体明细并勾选人工确认。
- 已经精确对上的未收项目，普通收款不能把这部分一起收掉，除非改走核销，或写下不使用的原因。
- 核销不扣库存。次数用条件更新，重复点击用提交号挡住。
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    CustomerPackage,
    ExamOrder,
    GroomingOrder,
    InventoryItem,
    Invoice,
    InvoiceItem,
    PackageApplicableItem,
    PackageBypassLog,
    PackageProduct,
    PackageRedemption,
    Payment,
    PaymentIdempotency,
    Pet,
    PrescriptionItem,
    SalesOrderItem,
    Vaccination,
    DewormingRecord,
)
from app.time_utils import beijing_today


class PackageGuardError(Exception):
    pass


class DuplicateSubmission(PackageGuardError):
    pass


def _today() -> str:
    return beijing_today().isoformat()


def _date_key(value: str) -> str:
    return (value or "").strip()[:10]


def is_expired(expires_at: str, today: str | None = None) -> bool:
    key = _date_key(expires_at)
    if not key:
        return False
    if len(key) != 10 or key[4] != "-" or key[7] != "-":
        return True
    return key < (today or _today())


def card_store_limit(db: Session, card: CustomerPackage) -> str:
    """返回限制门店短名。空字符串表示按原约定两店通用。"""
    if card.product_id:
        product = db.get(PackageProduct, card.product_id)
        if product is not None:
            return (product.store or "").strip()
    return (card.store or "").strip()


def store_allowed(limit: str, op_store: str) -> bool:
    limit = (limit or "").strip()
    if not limit:
        return True
    return (op_store or "").strip() == limit


def mapped_item_ids(db: Session, card: CustomerPackage) -> set[int]:
    if not card.product_id:
        return set()
    rows = db.query(PackageApplicableItem.inventory_item_id).filter(
        PackageApplicableItem.product_id == card.product_id
    ).all()
    return {int(row[0]) for row in rows if row[0]}


def remaining_uses(card: CustomerPackage) -> int:
    return max(0, int(card.total_uses or 0) - int(card.used_count or 0))


def _pet_name(db: Session, pet_id: int | None) -> str:
    if not pet_id:
        return ""
    pet = db.get(Pet, pet_id)
    return (pet.name or "").strip() if pet else ""


def list_package_notices(db: Session, customer_id: int | None, pet_id: int | None, op_store: str) -> list[dict]:
    """给档案、病历、开单页看的固定提醒。不弹窗，也不说「这次一定能用」。"""
    if not customer_id:
        return []
    today = _today()
    cards = db.query(CustomerPackage).filter(
        CustomerPackage.customer_id == customer_id,
        CustomerPackage.status == "active",
    ).order_by(CustomerPackage.id.desc()).all()
    notices = []
    for card in cards:
        left = remaining_uses(card)
        if left <= 0:
            continue
        expired = is_expired(card.expires_at, today)
        if expired:
            continue
        limit = card_store_limit(db, card)
        mapped = mapped_item_ids(db, card)
        pet_label = _pet_name(db, card.pet_id) or "购卡时未指定宠物"
        if limit:
            store_label = f"仅{limit}"
        else:
            store_label = "两店通用"
        usable = True
        reason = ""
        if card.pet_id and pet_id and int(card.pet_id) != int(pet_id):
            usable = False
            reason = "这张卡指定了别的宠物，本次不能用。"
        elif card.pet_id and not pet_id:
            usable = False
            reason = "这张卡指定了宠物。打开对应宠物后再核销。"
        elif not card.pet_id:
            reason = "购卡时没有指定宠物。不代表每只宠物都能用，核销时要确认本次宠物。"
        if not store_allowed(limit, op_store):
            usable = False
            reason = f"这张卡只限{limit}核销，当前门店不能扣次。"
        if not mapped:
            mapping_label = "未配置可抵项目。系统不按名字判断，也不会拦住无关收费。"
        else:
            mapping_label = f"已绑定 {len(mapped)} 个具体项目。只有账单里出现这些项目才需要选择核销或不使用。"
        notices.append({
            "id": card.id,
            "name": card.name or "套餐",
            "remaining": left,
            "total": int(card.total_uses or 0),
            "used": int(card.used_count or 0),
            "expires_at": card.expires_at or "不限期",
            "pet_label": pet_label,
            "store_label": store_label,
            "usable_here": usable,
            "needs_pet_confirm": not card.pet_id,
            "mapped": bool(mapped),
            "mapping_label": mapping_label,
            "reason": reason,
        })
    return notices


def _name_after_bracket(description: str) -> str:
    text_value = description or ""
    if "] " in text_value:
        return text_value.split("] ", 1)[1].strip()
    return text_value.strip()


def resolve_inventory_item_id(db: Session, line: InvoiceItem) -> int | None:
    """只走单据上保存的品目编号。对不上或有歧义就返回空，绝不按名字去库存里猜。"""
    ref_type = (line.ref_type or "").strip()
    ref_id = int(line.ref_id or 0)
    if not ref_id:
        return None
    if ref_type == "vaccination":
        row = db.get(Vaccination, ref_id)
        return int(row.inventory_item_id) if row and row.inventory_item_id else None
    if ref_type == "deworming":
        row = db.get(DewormingRecord, ref_id)
        return int(row.inventory_item_id) if row and row.inventory_item_id else None
    target = _name_after_bracket(line.description or "")
    if ref_type == "prescription":
        rows = db.query(PrescriptionItem).filter(
            PrescriptionItem.prescription_id == ref_id,
            PrescriptionItem.drug_name == target,
        ).all()
        ids = {int(row.item_id) for row in rows if row.item_id}
        return next(iter(ids)) if len(ids) == 1 else None
    if ref_type == "sales_order":
        rows = db.query(SalesOrderItem).filter(
            SalesOrderItem.order_id == ref_id,
            SalesOrderItem.item_name == target,
        ).all()
        ids = {int(row.item_id) for row in rows if row.item_id}
        return next(iter(ids)) if len(ids) == 1 else None
    if ref_type == "grooming":
        order = db.get(GroomingOrder, ref_id)
        return _single_json_item_id(getattr(order, "services_json", ""), target)
    if ref_type == "exam_order":
        order = db.get(ExamOrder, ref_id)
        return _single_json_item_id(getattr(order, "items_json", ""), target)
    return None


def _single_json_item_id(raw: str, target_name: str) -> int | None:
    try:
        rows = json.loads(raw or "[]")
    except Exception:
        return None
    if not isinstance(rows, list):
        return None
    hits: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        if target_name != name and not target_name.startswith(name + " · ") and not target_name.startswith(name + "·"):
            continue
        item_id = row.get("item_id")
        if str(item_id or "").isdigit() and int(item_id) > 0:
            hits.add(int(item_id))
    if len(hits) == 1:
        return next(iter(hits))
    return None


def _active_cover_rows(db: Session, invoice_id: int) -> list[PackageRedemption]:
    return db.query(PackageRedemption).filter(
        PackageRedemption.invoice_id == invoice_id,
        PackageRedemption.status == "active",
    ).all()


def covered_qty_and_amount(db: Session, invoice_id: int) -> tuple[dict[int, int], dict[int, float]]:
    qty: dict[int, int] = {}
    amount: dict[int, float] = {}
    for row in _active_cover_rows(db, invoice_id):
        try:
            parts = json.loads(row.covered_json or "[]")
        except Exception:
            parts = []
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            line_id = int(part.get("invoice_item_id") or 0)
            if not line_id:
                continue
            qty[line_id] = qty.get(line_id, 0) + int(part.get("qty") or 0)
            amount[line_id] = round(amount.get(line_id, 0.0) + float(part.get("amount") or 0), 2)
    return qty, amount


def _portion_amount(line: InvoiceItem, qty: int, covered_qty: int, covered_amount: float) -> float:
    full_qty = float(line.quantity or 0)
    subtotal = round(float(line.subtotal or 0), 2)
    if qty <= 0:
        return 0.0
    remaining_qty = full_qty - covered_qty
    if abs(qty - remaining_qty) < 1e-9 and covered_qty == 0:
        return subtotal
    if abs(qty - remaining_qty) < 1e-9 and covered_qty > 0:
        return round(max(0.0, subtotal - covered_amount), 2)
    return round(float(line.unit_price or 0) * qty, 2)


def _candidate_cards(db: Session, invoice: Invoice, op_store: str) -> list[CustomerPackage]:
    if not invoice.customer_id:
        return []
    today = _today()
    cards = db.query(CustomerPackage).filter(
        CustomerPackage.customer_id == invoice.customer_id,
        CustomerPackage.status == "active",
    ).order_by(CustomerPackage.id.desc()).all()
    ready = []
    for card in cards:
        if remaining_uses(card) <= 0 or is_expired(card.expires_at, today):
            continue
        if not store_allowed(card_store_limit(db, card), op_store):
            continue
        if card.pet_id and invoice.pet_id and int(card.pet_id) != int(invoice.pet_id):
            continue
        if card.pet_id and not invoice.pet_id:
            continue
        ready.append(card)
    return ready


def invoice_redeem_context(db: Session, invoice: Invoice, op_store: str) -> dict:
    covered_qty, covered_amount = covered_qty_and_amount(db, invoice.id)
    cards = _candidate_cards(db, invoice, op_store)
    card_rows = []
    for card in cards:
        mapped = mapped_item_ids(db, card)
        limit = card_store_limit(db, card)
        card_rows.append({
            "id": card.id,
            "name": card.name or "套餐",
            "remaining": remaining_uses(card),
            "total": int(card.total_uses or 0),
            "expires_at": card.expires_at or "不限期",
            "pet_id": card.pet_id,
            "pet_label": _pet_name(db, card.pet_id) or "购卡时未指定宠物",
            "store_label": f"仅{limit}" if limit else "两店通用",
            "mapped_ids": sorted(mapped),
            "mapped": bool(mapped),
            "needs_pet_confirm": not card.pet_id,
        })
    lines = []
    protected = 0.0
    for line in list(invoice.items or []):
        item_id = resolve_inventory_item_id(db, line)
        full_units = int(float(line.quantity or 0)) if float(line.quantity or 0) >= 1 else 0
        already = int(covered_qty.get(line.id, 0))
        open_units = max(0, full_units - already)
        matching = []
        for card in card_rows:
            if not card["mapped"]:
                continue
            if not item_id or item_id not in set(card["mapped_ids"]):
                continue
            if card["remaining"] <= 0:
                continue
            matching.append(card["id"])
        use_pool = sum(card["remaining"] for card in card_rows if card["id"] in matching)
        protect_units = min(open_units, use_pool) if matching else 0
        protect_amount = _portion_amount(line, protect_units, already, covered_amount.get(line.id, 0.0)) if protect_units else 0.0
        protected = round(protected + protect_amount, 2)
        lines.append({
            "id": line.id,
            "description": line.description or "未命名项目",
            "quantity": float(line.quantity or 0),
            "open_units": open_units,
            "unit_price": round(float(line.unit_price or 0), 2),
            "subtotal": round(float(line.subtotal or 0), 2),
            "inventory_item_id": item_id,
            "matching_card_ids": matching,
            "protect_amount": protect_amount,
        })
    outstanding = _outstanding(db, invoice)
    protected = round(min(protected, outstanding), 2)
    return {
        "cards": card_rows,
        "lines": lines,
        "protected_amount": protected,
        "unprotected_amount": round(max(0.0, outstanding - protected), 2),
        "notices": list_package_notices(db, invoice.customer_id, invoice.pet_id, op_store),
    }


def _outstanding(db: Session, invoice: Invoice) -> float:
    paid = db.query(Payment).filter(
        Payment.invoice_id == invoice.id,
        Payment.status == "success",
    ).all()
    paid_sum = round(sum(float(row.amount or 0) for row in paid), 2)
    return round(max(0.0, float(invoice.total_amount or 0) - paid_sum), 2)


def protected_amount(db: Session, invoice: Invoice, op_store: str) -> float:
    return float(invoice_redeem_context(db, invoice, op_store)["protected_amount"])


def assert_ordinary_amount(db: Session, invoice: Invoice, op_store: str, amount: float, reason: str) -> float:
    """普通收款可以先收套餐盖不到的部分。一旦收进已匹配项目，必须有不使用原因。"""
    state = invoice_redeem_context(db, invoice, op_store)
    unprotected = float(state["unprotected_amount"])
    protected = float(state["protected_amount"])
    if amount > unprotected + 0.009 and protected > 0.009:
        if not (reason or "").strip():
            names = "、".join(card["name"] for card in state["cards"] if card["mapped"]) or "已绑定项目的套餐"
            raise PackageGuardError(
                f"还有 ¥{protected:.2f} 属于「{names}」能抵的项目。"
                "请先选套餐扣次，或填写本次不使用套餐的原因。未绑定项目的其他卡不会拦住无关收费。"
            )
    return protected


def find_idempotent(db: Session, key: str) -> PaymentIdempotency | None:
    key = (key or "").strip()
    if not key:
        return None
    return db.query(PaymentIdempotency).filter(PaymentIdempotency.idempotency_key == key).first()


def claim_idempotency(db: Session, key: str, invoice_id: int | None, batch_no: str = "") -> None:
    key = (key or "").strip()[:80]
    if not key:
        return
    db.add(PaymentIdempotency(
        idempotency_key=key,
        invoice_id=invoice_id,
        batch_no=(batch_no or "")[:40],
    ))
    try:
        db.flush()
    except IntegrityError as exc:
        raise DuplicateSubmission("这笔收款已经提交过，请勿重复点击。") from exc


def attach_idempotent_payment(db: Session, key: str, payment_id: int) -> None:
    key = (key or "").strip()
    if not key:
        return
    row = find_idempotent(db, key)
    if row and not row.payment_id:
        row.payment_id = payment_id


def parse_cover_requests(raw_values: list[str]) -> list[tuple[int, int]]:
    parsed: list[tuple[int, int]] = []
    for raw in raw_values:
        text_value = str(raw or "").strip()
        if not text_value or ":" not in text_value:
            continue
        left, right = text_value.split(":", 1)
        if not left.isdigit():
            continue
        try:
            qty = int(right)
        except ValueError:
            raise PackageGuardError("抵扣数量必须是整数次。")
        if qty <= 0:
            continue
        parsed.append((int(left), qty))
    return parsed


def _load_card_for_update(db: Session, card_id: int) -> CustomerPackage:
    card = db.get(CustomerPackage, card_id)
    if not card:
        raise PackageGuardError("套餐不存在。")
    return card


def _conditional_consume(db: Session, card: CustomerPackage, uses: int, invoice_pet_id: int | None) -> None:
    today = _today()
    result = db.execute(text(
        "UPDATE customer_packages "
        "SET used_count = used_count + :uses, "
        "status = CASE WHEN used_count + :uses >= total_uses THEN 'exhausted' ELSE status END, "
        "updated_at = CURRENT_TIMESTAMP "
        "WHERE id = :id AND status = 'active' AND used_count + :uses <= total_uses "
        "AND (expires_at IS NULL OR expires_at = '' OR substr(expires_at, 1, 10) >= :today) "
        "AND (pet_id IS NULL OR pet_id = :pet_id)"
    ), {
        "uses": int(uses),
        "id": card.id,
        "today": today,
        "pet_id": int(invoice_pet_id or 0),
    })
    if not result.rowcount:
        raise PackageGuardError("套餐次数不足、已过期，或不能用于这只宠物。请刷新后重试。")
    db.expire(card)


def redeem_package(
    db: Session,
    invoice: Invoice,
    card_id: int,
    covers: list[tuple[int, int]],
    *,
    operator: str,
    op_store: str,
    idempotency_key: str,
    confirm_unassigned_pet: bool,
    confirm_unmapped: bool,
) -> Payment:
    """在当前事务里扣次、写核销流水和收款。调用方负责提交；失败由调用方回滚。不扣库存。"""
    if not covers:
        raise PackageGuardError("请点名这张卡要抵扣的明细和次数。一次扣次不会自动收完整张混合账单。")
    if find_idempotent(db, idempotency_key):
        raise DuplicateSubmission("这笔收款已经提交过，请勿重复点击。")
    card = _load_card_for_update(db, card_id)
    if card.customer_id != invoice.customer_id:
        raise PackageGuardError("这张套餐不属于该客户。")
    if card.status != "active" or remaining_uses(card) <= 0:
        raise PackageGuardError("套餐已用完或已停用。")
    if is_expired(card.expires_at):
        raise PackageGuardError("套餐已过有效期，不能核销。")
    limit = card_store_limit(db, card)
    if not store_allowed(limit, op_store):
        raise PackageGuardError(f"这张套餐只限{limit or '原门店'}核销。")
    if card.pet_id and int(card.pet_id) != int(invoice.pet_id or 0):
        raise PackageGuardError("这张套餐指定了别的宠物，不能在本账单核销。")
    if not card.pet_id:
        if not invoice.pet_id:
            raise PackageGuardError("账单没有宠物。未指定宠物的旧卡不能在这里直接核销。")
        if not confirm_unassigned_pet:
            raise PackageGuardError("这张卡购卡时没有指定宠物。请确认本次就是这只宠物后再核销。系统不会把它改成所有宠物通用。")
    mapped = mapped_item_ids(db, card)
    if not mapped and not confirm_unmapped:
        raise PackageGuardError("这张卡还没配置可抵项目。系统不能凭名字判断。若人工确认这些明细属于该卡，请勾选确认后再核销。")

    covered_qty, covered_amount = covered_qty_and_amount(db, invoice.id)
    line_map = {line.id: line for line in list(invoice.items or [])}
    parts = []
    total_uses = 0
    total_amount = 0.0
    for line_id, qty in covers:
        line = line_map.get(line_id)
        if not line or line.invoice_id != invoice.id:
            raise PackageGuardError("选择的明细不属于这张账单。")
        item_id = resolve_inventory_item_id(db, line)
        if mapped and (not item_id or item_id not in mapped):
            raise PackageGuardError(f"「{line.description or '该明细'}」不在这张卡已绑定的项目里，不能用次数把它收掉。")
        full_units = int(float(line.quantity or 0)) if float(line.quantity or 0) >= 1 else 0
        open_units = max(0, full_units - int(covered_qty.get(line.id, 0)))
        if qty > open_units:
            raise PackageGuardError(f"「{line.description or '该明细'}」最多还能用套餐抵 {open_units} 次。")
        amount = _portion_amount(line, qty, int(covered_qty.get(line.id, 0)), covered_amount.get(line.id, 0.0))
        if amount <= 0:
            raise PackageGuardError(f"「{line.description or '该明细'}」没有可抵扣金额。")
        parts.append({
            "invoice_item_id": line.id,
            "inventory_item_id": item_id,
            "description": line.description or "",
            "qty": qty,
            "amount": amount,
        })
        total_uses += qty
        total_amount = round(total_amount + amount, 2)
        covered_qty[line.id] = int(covered_qty.get(line.id, 0)) + qty
        covered_amount[line.id] = round(covered_amount.get(line.id, 0.0) + amount, 2)
    if total_uses <= 0:
        raise PackageGuardError("请选择至少 1 次。")
    if total_uses > remaining_uses(card):
        raise PackageGuardError(f"这张卡只剩 {remaining_uses(card)} 次，不够本次选择的 {total_uses} 次。")
    outstanding = _outstanding(db, invoice)
    if total_amount > outstanding + 0.009:
        raise PackageGuardError("套餐抵扣金额超过这张账单未收金额。请减少次数，或先处理折扣。")

    claim_idempotency(db, idempotency_key, invoice.id)
    _conditional_consume(db, card, total_uses, invoice.pet_id)
    db.refresh(card)
    note_bits = [f"{part['description']}×{part['qty']}" for part in parts]
    payment = Payment(
        invoice_id=invoice.id,
        customer_id=invoice.customer_id,
        method="package",
        amount=total_amount,
        ref_id=card.id,
        status="success",
        store=(op_store or invoice.store or "")[:40],
        operator=(operator or "")[:80],
        note=("套餐抵扣：" + "、".join(note_bits))[:500],
    )
    db.add(payment)
    db.flush()
    db.add(PackageRedemption(
        customer_package_id=card.id,
        customer_id=card.customer_id,
        pet_id=invoice.pet_id or card.pet_id,
        visit_id=invoice.visit_id,
        invoice_id=invoice.id,
        payment_id=payment.id,
        used_count=total_uses,
        remaining_after=max(0, int(card.total_uses or 0) - int(card.used_count or 0)),
        store=(op_store or invoice.store or "")[:40],
        operator=(operator or "")[:80],
        note=payment.note,
        status="active",
        covered_json=json.dumps(parts, ensure_ascii=False),
        idempotency_key=(idempotency_key or "")[:80],
        pet_confirmed=bool(confirm_unassigned_pet and not card.pet_id),
        confirm_note="当次确认未指定宠物的卡用于本账单宠物" if confirm_unassigned_pet and not card.pet_id else (
            "人工确认未配置项目的明细" if confirm_unmapped and not mapped else ""
        ),
    ))
    attach_idempotent_payment(db, idempotency_key, payment.id)
    return payment


def log_bypass(
    db: Session,
    invoice: Invoice,
    payment: Payment | None,
    *,
    operator: str,
    op_store: str,
    reason: str,
    protected: float,
    paid_amount: float,
) -> None:
    if protected <= 0.009 or not (reason or "").strip():
        return
    db.add(PackageBypassLog(
        invoice_id=invoice.id,
        payment_id=payment.id if payment else None,
        customer_id=invoice.customer_id,
        pet_id=invoice.pet_id,
        reason=(reason or "").strip()[:500],
        protected_amount=round(protected, 2),
        paid_amount=round(paid_amount, 2),
        store=(op_store or "")[:40],
        operator=(operator or "")[:80],
        detail_json=json.dumps({"invoice_no": invoice.invoice_no or str(invoice.id)}, ensure_ascii=False),
    ))


def reverse_package_payment(db: Session, payment: Payment, operator: str) -> PackageRedemption | None:
    """只冲回这一笔收款对应的那一次扣次，原流水改为已冲正，不删除同卡同单的其他记录。"""
    if payment.method != "package" or not payment.ref_id:
        return None
    redemption = db.query(PackageRedemption).filter(
        PackageRedemption.payment_id == payment.id,
        PackageRedemption.status == "active",
    ).order_by(PackageRedemption.id.asc()).first()
    if redemption is None:
        redemption = db.query(PackageRedemption).filter(
            PackageRedemption.invoice_id == payment.invoice_id,
            PackageRedemption.customer_package_id == payment.ref_id,
            PackageRedemption.status == "active",
            PackageRedemption.payment_id.is_(None),
        ).order_by(PackageRedemption.id.asc()).first()
    if redemption is None:
        return None
    uses = max(1, int(redemption.used_count or 1))
    result = db.execute(text(
        "UPDATE customer_packages "
        "SET used_count = used_count - :uses, "
        "status = CASE WHEN status = 'exhausted' AND used_count - :uses < total_uses THEN 'active' ELSE status END, "
        "updated_at = CURRENT_TIMESTAMP "
        "WHERE id = :id AND status IN ('active', 'exhausted') AND used_count >= :uses"
    ), {"uses": uses, "id": redemption.customer_package_id})
    if not result.rowcount:
        raise PackageGuardError("这张套餐已退款或已撤销，不能自动把次数加回去。请先处理套餐本身。")
    redemption.status = "reversed"
    redemption.reversed_at = datetime.utcnow()
    redemption.reversed_by = (operator or "")[:80]
    db.flush()
    return redemption


def item_label(db: Session, item_id: int) -> str:
    item = db.get(InventoryItem, item_id)
    if not item:
        return f"品目 #{item_id}"
    store = (item.store or "").strip() or "通用"
    return f"{item.name} · {store}"
