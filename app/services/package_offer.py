"""从品目开套餐。

员工选定主品目后填写买几次、送几次、赠品、成交价和有效期。
名称按固定规则生成。完全相同的组合复用已有目录，不另建一条。
成交价按次数分到每一次上，分摊后的分必须加总等于成交价。
已售卡如果写了分项，核销就按每个品目自己的剩余次数扣；
没有分项的旧卡仍走原来的共享次数池。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    CustomerPackage,
    CustomerPackageComponent,
    InventoryItem,
    PackageApplicableItem,
    PackageOfferAlias,
    PackageOfferGift,
    PackageProduct,
)
from app.time_utils import beijing_now, beijing_today


class OfferError(Exception):
    pass


class ComponentUseError(Exception):
    pass


def now_local() -> datetime:
    return beijing_now().replace(tzinfo=None)


def money_cents(amount) -> int:
    if isinstance(amount, Decimal):
        quantized = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    else:
        quantized = Decimal(str(amount if amount is not None else 0)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    return int(quantized * 100)


def format_yuan(cents: int) -> str:
    cents = int(cents or 0)
    if cents % 100 == 0:
        return str(cents // 100)
    return f"{cents / 100:.2f}"


def cents_for_span(amount_cents: int, quantity: int, start: int, count: int) -> int:
    """把一笔金额按次数摊开。前面的次数先吃掉余数，整段加总等于金额。"""
    quantity = int(quantity or 0)
    count = int(count or 0)
    start = int(start or 0)
    amount_cents = int(amount_cents or 0)
    if quantity <= 0 or count <= 0 or amount_cents <= 0:
        return 0
    if start < 0 or start + count > quantity:
        raise OfferError("分摊次数超出了这项的总次数。")
    base = amount_cents // quantity
    extra = amount_cents % quantity
    total = 0
    for index in range(start, start + count):
        total += base + (1 if index < extra else 0)
    return total


def package_category_for_item(item: InventoryItem) -> str:
    cat = (item.category or "").strip()
    sub = (item.subcategory or "").strip()
    if cat == "grooming" and sub == "washcare":
        return "bath"
    if cat == "grooming":
        return "beauty"
    if cat in {"vaccine", "medication", "lab", "imaging", "microscopy", "antiparasitic"}:
        return "medical"
    return "other"


def build_offer_name(
    primary_name: str,
    primary_unit: str,
    buy: int,
    gift: int,
    gift_labels: list[tuple[str, int, str]],
    price_cents: int,
) -> str:
    unit = (primary_unit or "").strip() or "次"
    if int(gift or 0) > 0:
        head = f"{primary_name} 买{int(buy)}送{int(gift)}"
    else:
        head = f"{primary_name} {int(buy)}{unit}"
    if gift_labels:
        bits = []
        for name, qty, gunit in gift_labels:
            bits.append(f"赠{name}{int(qty)}{(gunit or '次').strip() or '次'}")
        head += " + " + " + ".join(bits)
    suffix = f" · ¥{format_yuan(price_cents)}"
    text_value = head + suffix
    if len(text_value) <= 120:
        return text_value
    keep = 120 - len(suffix) - 1
    if keep < 1:
        return text_value[:120]
    return head[:keep] + "…" + suffix


def offer_fingerprint(
    primary_item_id: int,
    buy: int,
    gift: int,
    gifts: list[tuple[int, int]],
    price_cents: int,
    validity_days: int,
    store: str,
) -> str:
    gift_part = ",".join(f"{int(i)}:{int(q)}" for i, q in sorted(gifts))
    raw = "|".join([
        "v1",
        str(int(primary_item_id)),
        str(int(buy)),
        str(int(gift)),
        gift_part,
        str(int(price_cents)),
        str(int(validity_days)),
        (store or "").strip(),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class OfferSpec:
    primary_item_id: int
    buy_count: int
    gift_count: int
    gifts: list[tuple[int, int]] = field(default_factory=list)
    alias_item_ids: list[int] = field(default_factory=list)
    price: float = 0
    validity_days: int = 365
    store: str = ""

    @property
    def price_cents(self) -> int:
        return money_cents(self.price)

    @property
    def total_uses(self) -> int:
        return int(self.buy_count) + int(self.gift_count) + sum(int(q) for _, q in self.gifts)


def normalize_pairs(raw_ids: list, raw_qtys: list) -> list[tuple[int, int]]:
    merged: dict[int, int] = {}
    for index, raw_id in enumerate(raw_ids):
        text_id = str(raw_id or "").strip()
        if not text_id:
            continue
        if not text_id.isdigit():
            raise OfferError("赠送项目要先点选具体品目。")
        item_id = int(text_id)
        raw_qty = raw_qtys[index] if index < len(raw_qtys) else "1"
        try:
            qty = int(str(raw_qty or "1").strip() or "1")
        except ValueError as exc:
            raise OfferError("赠送次数要填整数。") from exc
        if qty <= 0:
            continue
        merged[item_id] = merged.get(item_id, 0) + qty
    return sorted(merged.items())


def parse_id_list(raw: str) -> list[int]:
    found: list[int] = []
    for part in (raw or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise OfferError("其他可抵品目要填品目编号，多个用逗号隔开。")
        item_id = int(part)
        if item_id not in found:
            found.append(item_id)
    return found


def _load_item(db: Session, item_id: int) -> InventoryItem:
    item = db.get(InventoryItem, int(item_id))
    if item is None:
        raise OfferError(f"品目 #{item_id} 不存在。")
    return item


def assert_item_in_store(item: InventoryItem, store: str) -> None:
    item_store = (item.store or "").strip()
    offer_store = (store or "").strip()
    if offer_store and item_store and item_store != offer_store:
        raise OfferError(f"「{item.name}」属于{item_store}，不能放进{offer_store}的套餐。")


def validate_spec(db: Session, spec: OfferSpec, *, require_active: bool = True) -> dict:
    if spec.buy_count < 1 or spec.buy_count > 99:
        raise OfferError("买几次要填 1 到 99。")
    if spec.gift_count < 0 or spec.gift_count > 99:
        raise OfferError("送几次要填 0 到 99。")
    if spec.validity_days < 0 or spec.validity_days > 3650:
        raise OfferError("有效期按天填，0 表示不限期，最多 3650 天。")
    if spec.price < 0 or spec.price_cents > 100_000_000:
        raise OfferError("成交价不能是负数。")
    if spec.store not in ("", "东环店", "横岗店"):
        raise OfferError("门店范围只能是东环店、横岗店，或两店通用。")
    if len(spec.gifts) > 6:
        raise OfferError("附加赠送项目最多 6 种。")
    primary = _load_item(db, spec.primary_item_id)
    if require_active and not primary.is_active:
        raise OfferError(f"「{primary.name}」已下架，不能拿来开新套餐。")
    assert_item_in_store(primary, spec.store)
    gift_items = []
    seen = {primary.id}
    for item_id, qty in spec.gifts:
        if qty < 1 or qty > 99:
            raise OfferError("每种赠品的次数要在 1 到 99。")
        if item_id == primary.id:
            raise OfferError("赠送项目和主品目相同。同一项目的赠送请填在「送几次」里。")
        if item_id in seen:
            raise OfferError("同一个赠送项目不要重复选。")
        item = _load_item(db, item_id)
        if require_active and not item.is_active:
            raise OfferError(f"赠品「{item.name}」已下架。")
        assert_item_in_store(item, spec.store)
        gift_items.append((item, qty))
        seen.add(item.id)
    aliases = []
    for item_id in spec.alias_item_ids:
        if item_id in seen:
            raise OfferError("其他可抵品目不能和主品目或赠品是同一条。")
        item = _load_item(db, item_id)
        if require_active and not item.is_active:
            raise OfferError(f"「{item.name}」已下架。")
        aliases.append(item)
        seen.add(item.id)
    if spec.total_uses < 1 or spec.total_uses > 200:
        raise OfferError("买、送和赠品加起来要在 1 到 200 次。")
    labels = [(item.name, qty, item.unit or "次") for item, qty in gift_items]
    name = build_offer_name(primary.name, primary.unit or "次", spec.buy_count, spec.gift_count, labels, spec.price_cents)
    fingerprint = offer_fingerprint(
        primary.id, spec.buy_count, spec.gift_count, spec.gifts,
        spec.price_cents, spec.validity_days, spec.store,
    )
    return {
        "primary": primary,
        "gifts": gift_items,
        "aliases": aliases,
        "name": name,
        "fingerprint": fingerprint,
    }


def resolve_offer_store(*, is_superadmin: bool, staff_store: str, session_store: str, explicit: str | None) -> str:
    if not is_superadmin:
        store = (staff_store or "").strip()
        if store not in ("东环店", "横岗店"):
            raise OfferError("员工账号缺少有效门店归属，请联系管理员。")
        return store
    if explicit is None:
        chosen = (session_store or "").strip()
    else:
        chosen = (explicit or "").strip()
    if chosen not in ("", "东环店", "横岗店"):
        raise OfferError("请选择门店范围：东环店、横岗店，或两店通用。")
    return chosen


def _reactivate(product: PackageProduct) -> None:
    product.is_active = True
    product.archived_at = None


def _has_legacy_cards(db: Session, product_id: int) -> bool:
    cards = db.query(CustomerPackage.id).filter(CustomerPackage.product_id == product_id).all()
    if not cards:
        return False
    card_ids = [row[0] for row in cards]
    structured = {
        row[0]
        for row in db.query(CustomerPackageComponent.customer_package_id).filter(
            CustomerPackageComponent.customer_package_id.in_(card_ids)
        ).distinct().all()
    }
    return any(card_id not in structured for card_id in card_ids)


def _ensure_applicable(db: Session, product: PackageProduct, item_ids: list[int]) -> list[int]:
    existing = {
        int(row[0])
        for row in db.query(PackageApplicableItem.inventory_item_id).filter(
            PackageApplicableItem.product_id == product.id
        ).all()
        if row[0]
    }
    added = []
    for item_id in item_ids:
        if item_id in existing:
            continue
        db.add(PackageApplicableItem(product_id=product.id, inventory_item_id=item_id))
        existing.add(item_id)
        added.append(item_id)
    return added


def natural_item_ids(spec: OfferSpec) -> list[int]:
    ids = [spec.primary_item_id]
    ids.extend(item_id for item_id, _qty in spec.gifts)
    ids.extend(spec.alias_item_ids)
    seen = []
    for item_id in ids:
        if item_id not in seen:
            seen.append(item_id)
    return seen


def get_or_create_offer(db: Session, spec: OfferSpec, *, operator: str = "") -> tuple[PackageProduct, bool]:
    checked = validate_spec(db, spec, require_active=True)
    fingerprint = checked["fingerprint"]
    existing = db.query(PackageProduct).filter(PackageProduct.fingerprint == fingerprint).order_by(PackageProduct.id.asc()).first()
    if existing is not None:
        _reactivate(existing)
        if not _has_legacy_cards(db, existing.id):
            _ensure_applicable(db, existing, natural_item_ids(spec))
        return existing, False

    primary: InventoryItem = checked["primary"]
    total = spec.total_uses
    product = PackageProduct(
        name=checked["name"],
        category=package_category_for_item(primary),
        total_uses=total,
        sell_price=round(spec.price_cents / 100, 2),
        unit_price=round(spec.price_cents / 100 / total, 2) if total else 0,
        validity_days=int(spec.validity_days),
        is_active=True,
        notes="由品目自动生成，名称不手填。",
        store=spec.store,
        primary_item_id=primary.id,
        buy_count=int(spec.buy_count),
        gift_count=int(spec.gift_count),
        fingerprint=fingerprint,
        offer_kind="offer",
    )
    nested = db.begin_nested()
    try:
        db.add(product)
        db.flush()
        nested.commit()
    except IntegrityError:
        nested.rollback()
        existing = db.query(PackageProduct).filter(PackageProduct.fingerprint == fingerprint).first()
        if existing is None:
            raise
        _reactivate(existing)
        return existing, False
    for item, qty in checked["gifts"]:
        db.add(PackageOfferGift(product_id=product.id, inventory_item_id=item.id, quantity=int(qty)))
    for item in checked["aliases"]:
        db.add(PackageOfferAlias(
            product_id=product.id,
            inventory_item_id=item.id,
            kind="primary",
            gift_item_id=0,
        ))
    _ensure_applicable(db, product, natural_item_ids(spec))
    db.flush()
    return product, True


def structure_total(db: Session, product: PackageProduct) -> int:
    if not product.primary_item_id:
        return int(product.total_uses or 0)
    gifts = db.query(PackageOfferGift).filter(PackageOfferGift.product_id == product.id).all()
    return int(product.buy_count or 0) + int(product.gift_count or 0) + sum(int(row.quantity or 0) for row in gifts)


def _match_ids(item_id: int, extra: list[int]) -> list[int]:
    ids = []
    for value in [item_id, *extra]:
        if value and value not in ids:
            ids.append(int(value))
    return ids


def attach_components_if_structured(db: Session, card: CustomerPackage) -> None:
    """按目录上的买送和赠品，给这张新卡写下分项。旧卡没有主品目时什么都不做。"""
    if not card.product_id or not card.id:
        return
    exists = db.query(CustomerPackageComponent.id).filter(
        CustomerPackageComponent.customer_package_id == card.id
    ).first()
    if exists:
        return
    product = db.get(PackageProduct, card.product_id)
    if product is None or not product.primary_item_id:
        return
    primary = db.get(InventoryItem, product.primary_item_id)
    if primary is None:
        return
    gifts = db.query(PackageOfferGift).filter(PackageOfferGift.product_id == product.id).order_by(PackageOfferGift.id).all()
    aliases = db.query(PackageOfferAlias).filter(PackageOfferAlias.product_id == product.id).all()
    primary_qty = int(product.buy_count or 0) + int(product.gift_count or 0)
    if primary_qty <= 0:
        return
    expected = primary_qty + sum(int(row.quantity or 0) for row in gifts)
    if int(card.total_uses or 0) != expected:
        return
    primary_alias = [row.inventory_item_id for row in aliases if (row.kind or "") == "primary"]
    parts = [{
        "item": primary,
        "role": "primary",
        "qty": primary_qty,
        "match_ids": _match_ids(primary.id, primary_alias),
    }]
    for gift in gifts:
        item = db.get(InventoryItem, gift.inventory_item_id)
        if item is None or int(gift.quantity or 0) <= 0:
            return
        gift_alias = [
            row.inventory_item_id for row in aliases
            if (row.kind or "") == "gift" and int(row.gift_item_id or 0) == int(gift.inventory_item_id)
        ]
        parts.append({
            "item": item,
            "role": "gift",
            "qty": int(gift.quantity),
            "match_ids": _match_ids(item.id, gift_alias),
        })
    amounts = []
    cursor = 0
    total_cents = money_cents(card.sell_price or 0)
    quantities = [part["qty"] for part in parts]
    total_qty = sum(quantities)
    for qty in quantities:
        amounts.append(cents_for_span(total_cents, total_qty, cursor, qty))
        cursor += qty
    used_left = int(card.used_count or 0)
    for part, amount in zip(parts, amounts):
        take = min(used_left, part["qty"])
        used_left -= take
        consumed = cents_for_span(amount, part["qty"], 0, take)
        db.add(CustomerPackageComponent(
            customer_package_id=card.id,
            inventory_item_id=part["item"].id,
            role=part["role"],
            item_name=(part["item"].name or "")[:200],
            quantity=part["qty"],
            used_count=take,
            amount_cents=amount,
            consumed_cents=consumed,
            match_ids_json=json.dumps(part["match_ids"]),
        ))
    db.flush()


def component_rows(db: Session, card_id: int) -> list[CustomerPackageComponent]:
    return db.query(CustomerPackageComponent).filter(
        CustomerPackageComponent.customer_package_id == card_id
    ).order_by(CustomerPackageComponent.id).all()


def match_ids_of(row: CustomerPackageComponent) -> list[int]:
    try:
        raw = json.loads(row.match_ids_json or "[]")
    except Exception:
        raw = []
    ids = []
    if isinstance(raw, list):
        for value in raw:
            if str(value).isdigit() and int(value) not in ids:
                ids.append(int(value))
    if row.inventory_item_id and int(row.inventory_item_id) not in ids:
        ids.append(int(row.inventory_item_id))
    return ids


def find_component(rows: list[CustomerPackageComponent], item_id: int | None) -> CustomerPackageComponent | None:
    if not item_id:
        return None
    for row in rows:
        if int(item_id) in match_ids_of(row):
            return row
    return None


def component_quota_map(db: Session, card_id: int) -> dict[int, int] | None:
    rows = component_rows(db, card_id)
    if not rows:
        return None
    quota: dict[int, int] = {}
    for row in rows:
        left = max(0, int(row.quantity or 0) - int(row.used_count or 0))
        for item_id in match_ids_of(row):
            quota[item_id] = left
    return quota


def component_notice(db: Session, card_id: int) -> str:
    rows = component_rows(db, card_id)
    if not rows:
        return ""
    bits = []
    for row in rows:
        left = max(0, int(row.quantity or 0) - int(row.used_count or 0))
        bits.append(f"{row.item_name or '项目'}剩{left}次")
    return "按品目计次：" + "、".join(bits)


def annotate_structured_cover(db: Session, card_id: int, parts: list[dict]) -> bool:
    rows = component_rows(db, card_id)
    if not rows:
        return False
    planned: dict[int, int] = {}
    for part in parts:
        comp = find_component(rows, part.get("inventory_item_id"))
        if comp is None:
            raise ComponentUseError(f"「{part.get('description') or '该明细'}」不在这张卡的品目里，不能用次数把它收掉。")
        qty = int(part.get("qty") or 0)
        already = planned.get(comp.id, 0)
        left = int(comp.quantity or 0) - int(comp.used_count or 0) - already
        if qty > left:
            remain = max(0, int(comp.quantity or 0) - int(comp.used_count or 0))
            raise ComponentUseError(f"「{comp.item_name or '该项目'}」这张卡只剩 {remain} 次。")
        part["component_id"] = comp.id
        try:
            part["allocated_cents"] = cents_for_span(
                int(comp.amount_cents or 0), int(comp.quantity or 0), int(comp.used_count or 0) + already, qty,
            )
        except OfferError as exc:
            raise ComponentUseError(str(exc)) from exc
        planned[comp.id] = already + qty
    return True


def apply_structured_uses(db: Session, parts: list[dict]) -> None:
    grouped: dict[int, dict] = {}
    for part in parts:
        component_id = part.get("component_id")
        if not component_id:
            continue
        bucket = grouped.setdefault(int(component_id), {"id": int(component_id), "qty": 0, "cents": 0})
        bucket["qty"] += int(part.get("qty") or 0)
        bucket["cents"] += int(part.get("allocated_cents") or 0)
    for bucket in grouped.values():
        result = db.execute(text(
            "UPDATE customer_package_components "
            "SET used_count = used_count + :qty, consumed_cents = consumed_cents + :cents "
            "WHERE id = :id AND used_count + :qty <= quantity "
            "AND consumed_cents + :cents <= amount_cents"
        ), bucket)
        if not result.rowcount:
            raise ComponentUseError("这个品目的剩余次数刚刚变了，请刷新后重试。")


def restore_structured_uses(db: Session, covered_json: str) -> None:
    try:
        parts = json.loads(covered_json or "[]")
    except Exception:
        parts = []
    if not isinstance(parts, list):
        return
    grouped: dict[int, dict] = {}
    for part in parts:
        if not isinstance(part, dict) or not part.get("component_id"):
            continue
        if "allocated_cents" not in part:
            continue
        component_id = int(part["component_id"])
        bucket = grouped.setdefault(component_id, {"id": component_id, "qty": 0, "cents": 0})
        bucket["qty"] += int(part.get("qty") or 0)
        bucket["cents"] += int(part.get("allocated_cents") or 0)
    for bucket in grouped.values():
        result = db.execute(text(
            "UPDATE customer_package_components "
            "SET used_count = used_count - :qty, consumed_cents = consumed_cents - :cents "
            "WHERE id = :id AND used_count >= :qty AND consumed_cents >= :cents"
        ), bucket)
        if not result.rowcount:
            raise ComponentUseError("这张卡的分项次数对不上，不能自动把这次扣次退回去。")


def remaining_refund_amount(db: Session, card: CustomerPackage) -> float:
    rows = component_rows(db, card.id)
    if rows:
        cents = sum(max(0, int(row.amount_cents or 0) - int(row.consumed_cents or 0)) for row in rows)
        return round(cents / 100, 2)
    total = int(card.total_uses or 0)
    remaining = max(0, total - int(card.used_count or 0))
    if total <= 0 or remaining <= 0:
        return 0.0
    return round(float(card.sell_price or 0) * remaining / total, 2)


def preview_offer(db: Session, spec: OfferSpec) -> dict:
    checked = validate_spec(db, spec, require_active=True)
    existing = db.query(PackageProduct).filter(PackageProduct.fingerprint == checked["fingerprint"]).first()
    return {
        "name": checked["name"],
        "reused": existing is not None,
        "existing_id": existing.id if existing else None,
        "total_uses": spec.total_uses,
    }


def search_offer_items(db: Session, keyword: str, store: str, limit: int = 20) -> list[InventoryItem]:
    keyword = (keyword or "").strip()[:80]
    if not keyword:
        return []
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    query = db.query(InventoryItem).filter(InventoryItem.is_active.is_(True))
    if store:
        query = query.filter(or_(InventoryItem.store == store, InventoryItem.store == ""))
    return (
        query.filter(InventoryItem.name.like(f"%{escaped}%", escape="\\"))
        .order_by(InventoryItem.name, InventoryItem.store, InventoryItem.id)
        .limit(limit)
        .all()
    )


def products_with_remaining(db: Session) -> set[int]:
    today = beijing_today().isoformat()
    rows = db.query(CustomerPackage).filter(
        CustomerPackage.status == "active",
        CustomerPackage.product_id.isnot(None),
    ).all()
    found = set()
    for row in rows:
        remaining = int(row.total_uses or 0) - int(row.used_count or 0)
        if remaining <= 0:
            continue
        expires = (row.expires_at or "")[:10]
        if expires and expires < today:
            continue
        found.add(int(row.product_id))
    return found


def archive_idle(db: Session) -> int:
    """下架且没有客户剩余次数的目录行打上归档时间。在售的不动。"""
    remaining = products_with_remaining(db)
    rows = db.query(PackageProduct).filter(
        PackageProduct.archived_at.is_(None),
        PackageProduct.is_active.is_(False),
    ).all()
    now = now_local()
    count = 0
    for row in rows:
        if row.id in remaining:
            continue
        row.archived_at = now
        count += 1
    if count:
        db.flush()
    return count


def is_in_use(product: PackageProduct, remaining_ids: set[int]) -> bool:
    if product.id in remaining_ids:
        return True
    return bool(product.is_active) and product.archived_at is None


def sellable_products(db: Session, store_short: str) -> list[PackageProduct]:
    query = db.query(PackageProduct).filter(
        PackageProduct.is_active.is_(True),
        PackageProduct.archived_at.is_(None),
    )
    if store_short:
        query = query.filter(or_(PackageProduct.store == store_short, PackageProduct.store == ""))
    return query.order_by(PackageProduct.id.desc()).all()


def offer_brief(db: Session, product: PackageProduct) -> str:
    if not product.primary_item_id:
        return ""
    primary = db.get(InventoryItem, product.primary_item_id)
    primary_name = primary.name if primary else f"品目 #{product.primary_item_id}"
    gifts = db.query(PackageOfferGift).filter(PackageOfferGift.product_id == product.id).order_by(PackageOfferGift.id).all()
    gift_bits = []
    for gift in gifts:
        item = db.get(InventoryItem, gift.inventory_item_id)
        gift_bits.append(f"赠{(item.name if item else '赠品')}×{int(gift.quantity or 0)}")
    head = f"{primary_name} 买{int(product.buy_count or 0)}送{int(product.gift_count or 0)}"
    if gift_bits:
        head += "，" + "、".join(gift_bits)
    return head


def expires_on(purchase_date, validity_days: int) -> str:
    if not validity_days or int(validity_days) <= 0:
        return ""
    return (purchase_date + timedelta(days=int(validity_days))).isoformat()
