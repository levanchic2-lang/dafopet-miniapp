"""把旧的手填套餐拆成主品目、次数、赠品和价格。

只读预演什么都不写。正式执行要另调 apply_migration。
已售卡的次数、成交价、核销流水一律不改。对不上的旧名称停在「需人工确认」，
不拿相似名字去猜。执行前后用同一套公式对账，不一致就整批回滚。
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import (
    CustomerPackage,
    InventoryItem,
    PackageApplicableItem,
    PackageMigrationLog,
    PackageMigrationOverride,
    PackageOfferAlias,
    PackageOfferGift,
    PackageProduct,
    PackageRedemption,
    Payment,
)
from app.services.package_offer import (
    build_offer_name,
    money_cents,
    now_local,
    offer_fingerprint,
)
from app.time_utils import beijing_now


class MigrationError(Exception):
    pass


_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_NUM = r"([0-9]+|[一二两三四五六七八九]|十)"
_BUY_GIFT = re.compile(rf"买\s*{_NUM}\s*送\s*{_NUM}")
_BARE_GIFT = re.compile(rf"{_NUM}\s*送\s*{_NUM}")
_DOSE = re.compile(rf"{_NUM}\s*针")
_TIMES = re.compile(r"([0-9]+)\s*次")


def _parse_num(token: str) -> int | None:
    if token.isdigit():
        value = int(token)
        return value if value > 0 else None
    return _CN_NUM.get(token)


def extract_name_prices(name: str) -> list[float]:
    text_value = (name or "").strip()
    found: list[float] = []

    def add(value: float) -> None:
        rounded = round(float(value), 2)
        if rounded not in found:
            found.append(rounded)

    for match in re.finditer(r"(\d+(?:\.\d+)?)\s*元", text_value):
        add(float(match.group(1)))
    leading = re.match(r"(\d+(?:\.\d+)?)(?!\s*(?:针|次|送))", text_value)
    if leading:
        add(float(leading.group(1)))
    if not re.search(rf"{_NUM}\s*送\s*{_NUM}\s*$", text_value):
        trailing = re.search(r"(\d+(?:\.\d+)?)\s*$", text_value)
        if trailing:
            add(float(trailing.group(1)))
    return found


def parse_counts(name: str, total_uses: int) -> dict:
    text_value = name or ""
    reasons: list[str] = []
    buy_gift = _BUY_GIFT.search(text_value)
    bare = None if buy_gift else _BARE_GIFT.search(text_value)
    dose = _DOSE.search(text_value)
    times = _TIMES.search(text_value)
    signals = []
    buy = 0
    gift = 0
    source = ""
    if buy_gift:
        buy = _parse_num(buy_gift.group(1)) or 0
        gift = _parse_num(buy_gift.group(2)) or 0
        source = "买送"
        signals.append("买送")
    elif bare:
        buy = _parse_num(bare.group(1)) or 0
        gift = _parse_num(bare.group(2)) or 0
        source = "送"
        signals.append("送")
    if dose and not buy_gift and not bare:
        buy = _parse_num(dose.group(1)) or 0
        gift = 0
        source = "针"
        signals.append("针")
    elif dose and (buy_gift or bare):
        signals.append("针")
    if times and not signals:
        buy = _parse_num(times.group(1)) or 0
        gift = 0
        source = "次"
        signals.append("次")
    elif times and signals:
        signals.append("次")
    if len(signals) > 1:
        reasons.append("名称里同时有多种次数写法，不能确定以哪一种为准。")
    if not signals:
        if "送" in text_value:
            reasons.append("名称里有赠送，但看不出买几次、送几次。")
        elif int(total_uses or 0) >= 1:
            buy = int(total_uses)
            gift = 0
            source = "目录次数"
        else:
            reasons.append("目录上没有可用的总次数。")
    if buy < 1 and not reasons:
        reasons.append("没有识别出主项目的购买次数。")
    return {"buy": buy, "gift": gift, "source": source, "reasons": reasons}


def primary_token(name: str) -> str:
    text_value = name or ""
    text_value = re.sub(r"\d+(?:\.\d+)?\s*元", "", text_value)
    text_value = re.sub(rf"买\s*{_NUM}\s*送\s*{_NUM}", "", text_value)
    text_value = re.sub(rf"{_NUM}\s*送\s*{_NUM}", "", text_value)
    text_value = re.sub(rf"{_NUM}\s*针", "", text_value)
    text_value = re.sub(r"[0-9]+\s*次", "", text_value)
    text_value = re.sub(r"送[^\d送买元\s]{1,10}", "", text_value)
    text_value = re.sub(r"(套餐|卡|活动|团购|元)", "", text_value)
    text_value = re.sub(r"\d+(?:\.\d+)?", "", text_value)
    text_value = re.sub(r"[\s·•\-+＋/／]+", "", text_value)
    return text_value.strip()


def gift_phrases(name: str) -> list[tuple[str, int]]:
    text_value = name or ""
    text_value = _BUY_GIFT.sub("", text_value)
    text_value = _BARE_GIFT.sub("", text_value)
    found: list[tuple[str, int]] = []
    for match in re.finditer(r"送([^\d送买元\s]{1,10})", text_value):
        phrase = re.split(r"套餐|卡", match.group(1))[0].strip()
        if not phrase:
            continue
        qty_match = re.search(rf"送{re.escape(phrase)}\s*([0-9]+)\s*次", name or "")
        qty = int(qty_match.group(1)) if qty_match else 1
        if phrase not in [item[0] for item in found]:
            found.append((phrase, qty))
    return found


def _same_name_group(items: list) -> list | None:
    names = {(item.name or "").strip() for item in items}
    names.discard("")
    if len(names) == 1:
        return list(items)
    if len(names) == 0:
        return []
    return None


def match_items(token: str, items: list) -> list | None:
    token = (token or "").strip()
    if len(token) < 2:
        return []
    exact = [item for item in items if (item.name or "").strip() == token]
    if exact:
        return _same_name_group(exact)
    contained = [item for item in items if token in ((item.name or "").strip())]
    if contained:
        return _same_name_group(contained)
    reverse = [item for item in items if (item.name or "").strip() and (item.name or "").strip() in token]
    if reverse:
        return _same_name_group(reverse)
    return []


def pick_in_scope(group, store: str):
    if group is None:
        return None, "对应多个不同名称的品目，不能猜是哪一个。"
    if not group:
        return None, "没有对上库存里的品目。"
    store = (store or "").strip()
    if store:
        scoped = [item for item in group if (item.store or "") in ("", store)]
    else:
        scoped = list(group)
    if len(scoped) == 1:
        return scoped[0], ""
    if not scoped:
        return None, "品目不在这个套餐的门店范围内。"
    stores = {(item.store or "") or "两店通用" for item in scoped}
    if len({(item.name or "").strip() for item in scoped}) == 1 and len(stores) > 1:
        return None, "两店都有同名品目。请指定主品目，并把另一店的编号填进其他可抵品目。"
    return None, "同一个名字对上了多条品目，不能自动点选。"


def _prices_conflict(name: str, sell_price: float) -> str:
    prices = extract_name_prices(name)
    if not prices:
        return ""
    if any(abs(price - float(sell_price or 0)) > 0.009 for price in prices):
        shown = "、".join(f"¥{price:g}" for price in prices)
        return f"名称里的价格（{shown}）和目录售价 ¥{float(sell_price or 0):g} 不一致。"
    return ""


def _generated(primary, buy: int, gift: int, gifts: list[dict], price: float) -> str:
    labels = []
    for gift_row in gifts:
        labels.append((gift_row["name"], gift_row["qty"], gift_row.get("unit") or "次"))
    return build_offer_name(
        primary.name,
        getattr(primary, "unit", "") or "次",
        buy,
        gift,
        labels,
        money_cents(price),
    )


def propose_legacy(product, items: list, bound_ids: list[int]) -> dict:
    """只根据名称、目录数字和已绑定品目给出拆解。不写数据库。"""
    reasons: list[str] = []
    notes: list[str] = []
    counts = parse_counts(product.name or "", int(product.total_uses or 0))
    reasons.extend(counts["reasons"])
    buy = int(counts["buy"] or 0)
    gift = int(counts["gift"] or 0)
    price_reason = _prices_conflict(product.name or "", float(product.sell_price or 0))
    if price_reason:
        reasons.append(price_reason)
    token = primary_token(product.name or "")
    phrases = gift_phrases(product.name or "")
    bound = [item for item in items if item.id in set(bound_ids)]
    scope = items
    if bound:
        primary_group = match_items(token, bound) if token else []
        if primary_group is None or not primary_group:
            if len({(item.name or "").strip() for item in bound}) == 1 and not phrases and not reasons:
                primary_group = bound
                notes.append("名称没有独立品名，主品目沿用已经绑定的那一个。")
            elif token and match_items(token, scope):
                reasons.append(f"名称指向「{token}」，但和已经绑定的品目不是同一条。")
        else:
            notes.append("主品目取自已经绑定、并且和名称对得上的品目。")
    else:
        primary_group = match_items(token, scope) if token else []
        if not token:
            reasons.append("从名称里提不出主品目。")
    primary, primary_reason = pick_in_scope(primary_group if primary_group is not None else None, product.store or "")
    if primary is None and primary_reason and "没有对上" not in "".join(reasons):
        if token:
            reasons.append(f"主品目「{token}」{primary_reason}")
        elif primary_reason and "提不出" not in "".join(reasons):
            reasons.append(primary_reason)

    gifts = []
    for phrase, qty in phrases:
        group = match_items(phrase, scope)
        if bound:
            bound_group = match_items(phrase, bound)
            if bound_group:
                group = bound_group
            elif group:
                notes.append(f"赠品「{phrase}」此前没有绑定。只会影响之后新售出的卡，已售旧卡的可抵项目不变。")
        chosen, gift_reason = pick_in_scope(group, product.store or "")
        if chosen is None:
            reasons.append(f"赠品「{phrase}」{gift_reason}")
            continue
        if primary is not None and chosen.id == primary.id:
            reasons.append(f"赠品「{phrase}」和主品目是同一条，不能再单独计次。")
            continue
        gifts.append({
            "item_id": chosen.id,
            "qty": qty,
            "name": chosen.name,
            "unit": getattr(chosen, "unit", "") or "次",
        })

    computed = buy + gift + sum(int(row["qty"]) for row in gifts)
    changes_total = computed != int(product.total_uses or 0)
    if changes_total and buy >= 1:
        reasons.append(
            f"拆出来是 {computed} 次，目录总次数是 {int(product.total_uses or 0)}。"
            "自动迁移不会改目录次数，请确认抗体或赠送算不算一次。"
        )
    generated = ""
    fingerprint = ""
    if primary is not None and buy >= 1 and not any("多种次数" in reason for reason in reasons):
        generated = _generated(primary, buy, gift, gifts, float(product.sell_price or 0))
        fingerprint = offer_fingerprint(
            primary.id, buy, gift, [(row["item_id"], row["qty"]) for row in gifts],
            money_cents(product.sell_price or 0), int(product.validity_days or 0), product.store or "",
        )
    status = "auto" if not reasons and primary is not None and buy >= 1 and not changes_total else "needs_review"
    return {
        "status": status,
        "reasons": reasons,
        "notes": notes,
        "primary_item_id": primary.id if primary is not None else None,
        "primary_name": primary.name if primary is not None else "",
        "primary_unit": (getattr(primary, "unit", "") or "次") if primary is not None else "",
        "buy_count": buy,
        "gift_count": gift,
        "gifts": gifts,
        "alias_item_ids": [],
        "generated_name": generated,
        "price": round(float(product.sell_price or 0), 2),
        "validity_days": int(product.validity_days or 0),
        "computed_total": computed,
        "changes_template_total": bool(changes_total and status != "auto"),
        "count_source": counts["source"],
        "token": token,
        "fingerprint": fingerprint,
    }


def _override_gifts(raw: str) -> list[dict]:
    try:
        rows = json.loads(raw or "[]")
    except Exception:
        return []
    gifts = []
    if not isinstance(rows, list):
        return []
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = int(row.get("item_id") or 0)
        qty = int(row.get("qty") or 0)
        if item_id > 0 and qty > 0:
            gifts.append({"item_id": item_id, "qty": qty})
    return gifts


def propose_override(product, override, items_by_id: dict) -> dict:
    reasons = []
    primary = items_by_id.get(int(override.primary_item_id or 0))
    if primary is None:
        reasons.append("确认的主品目不存在。")
    buy = int(override.buy_count or 0)
    gift = int(override.gift_count or 0)
    if buy < 1:
        reasons.append("确认的购买次数至少是 1。")
    if gift < 0:
        reasons.append("确认的赠送次数不能是负数。")
    gifts = []
    for row in _override_gifts(override.gifts_json):
        item = items_by_id.get(row["item_id"])
        if item is None:
            reasons.append(f"赠品品目 #{row['item_id']} 不存在。")
            continue
        if primary is not None and item.id == primary.id:
            reasons.append("赠品不能和主品目是同一条。")
            continue
        gifts.append({
            "item_id": item.id,
            "qty": row["qty"],
            "name": item.name,
            "unit": item.unit or "次",
        })
    alias_ids = []
    for part in (override.alias_item_ids or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or int(part) not in items_by_id:
            reasons.append(f"其他可抵品目 #{part or '空'} 不存在。")
            continue
        alias_ids.append(int(part))
    computed = buy + gift + sum(row["qty"] for row in gifts)
    changes_total = computed != int(product.total_uses or 0)
    notes = []
    if changes_total and not reasons:
        notes.append(
            f"确认后的次数是 {computed}，目录现在是 {int(product.total_uses or 0)}。"
            "执行时会改目录上的总次数，已售卡上的次数不动。"
        )
    generated = ""
    fingerprint = ""
    if primary is not None and buy >= 1 and not reasons:
        generated = _generated(primary, buy, gift, gifts, float(product.sell_price or 0))
        fingerprint = offer_fingerprint(
            primary.id, buy, gift, [(row["item_id"], row["qty"]) for row in gifts],
            money_cents(product.sell_price or 0), int(product.validity_days or 0), product.store or "",
        )
    return {
        "status": "confirmed" if not reasons else "needs_review",
        "reasons": reasons,
        "notes": notes,
        "primary_item_id": primary.id if primary is not None else None,
        "primary_name": primary.name if primary is not None else "",
        "primary_unit": (primary.unit or "次") if primary is not None else "",
        "buy_count": buy,
        "gift_count": gift,
        "gifts": gifts,
        "alias_item_ids": alias_ids,
        "generated_name": generated,
        "price": round(float(product.sell_price or 0), 2),
        "validity_days": int(product.validity_days or 0),
        "computed_total": computed,
        "changes_template_total": changes_total,
        "count_source": "人工确认",
        "token": "",
        "fingerprint": fingerprint,
        "override_id": override.id,
    }


def _pending_override(product: PackageProduct, override) -> bool:
    if override is None or (product.offer_kind or "") == "offer":
        return False
    if (product.offer_kind or "") != "migrated":
        return True
    if override.applied_at is None:
        return True
    if product.migrated_at and override.confirmed_at and override.confirmed_at > product.migrated_at:
        return True
    return False


def entitlement_totals(db: Session) -> dict:
    cards = db.query(CustomerPackage).order_by(CustomerPackage.id).all()
    redemptions = db.query(PackageRedemption).order_by(PackageRedemption.id).all()
    payments = db.query(Payment).filter(Payment.method == "package").order_by(Payment.id).all()
    remaining_uses = 0
    remaining_cents = 0
    included = 0
    unlinked_uses = 0
    unlinked_cents = 0
    per_product: dict[int, dict] = {}
    card_rows = []
    for card in cards:
        card_rows.append([
            card.id, int(card.total_uses or 0), int(card.used_count or 0),
            str(card.sell_price), card.status or "", card.name or "", card.product_id,
        ])
        if (card.status or "") in ("refunded", "cancelled"):
            continue
        total = int(card.total_uses or 0)
        remaining = max(0, total - int(card.used_count or 0))
        cents = 0
        if total > 0 and remaining > 0:
            cents = money_cents(Decimal(str(card.sell_price or 0)) * remaining / total)
        remaining_uses += remaining
        remaining_cents += cents
        included += 1
        if card.product_id:
            bucket = per_product.setdefault(int(card.product_id), {"uses": 0, "cents": 0, "cards": 0})
            bucket["uses"] += remaining
            bucket["cents"] += cents
            bucket["cards"] += 1
        else:
            unlinked_uses += remaining
            unlinked_cents += cents
    redeem_rows = [[
        row.id, row.customer_package_id, int(row.used_count or 0), row.status or "",
        row.invoice_id, row.payment_id,
    ] for row in redemptions]
    pay_rows = [[row.id, row.invoice_id, str(row.amount), row.status or "", row.ref_id] for row in payments]
    return {
        "remaining_uses": remaining_uses,
        "remaining_cents": remaining_cents,
        "included_cards": included,
        "card_count": len(cards),
        "redemption_count": len(redemptions),
        "package_payment_count": len(payments),
        "unlinked_uses": unlinked_uses,
        "unlinked_cents": unlinked_cents,
        "per_product": per_product,
        "card_checksum": hashlib.sha256(json.dumps(card_rows, ensure_ascii=False).encode()).hexdigest(),
        "redemption_checksum": hashlib.sha256(json.dumps(redeem_rows, ensure_ascii=False).encode()).hexdigest(),
        "payment_checksum": hashlib.sha256(json.dumps(pay_rows, ensure_ascii=False).encode()).hexdigest(),
    }


def _totals_match(before: dict, after: dict) -> bool:
    keys = (
        "remaining_uses", "remaining_cents", "card_checksum",
        "redemption_checksum", "payment_checksum", "card_count", "redemption_count",
    )
    return all(before[key] == after[key] for key in keys)


def build_report(db: Session) -> dict:
    """只读。调用方不要在这次查询后提交。"""
    products = db.query(PackageProduct).order_by(PackageProduct.id.asc()).all()
    items = db.query(InventoryItem).order_by(InventoryItem.id.asc()).all()
    items_by_id = {item.id: item for item in items}
    bound_map: dict[int, list[int]] = {}
    for link in db.query(PackageApplicableItem).order_by(PackageApplicableItem.id).all():
        bound_map.setdefault(int(link.product_id), []).append(int(link.inventory_item_id))
    overrides = {
        int(row.product_id): row
        for row in db.query(PackageMigrationOverride).all()
        if row.product_id
    }
    accounts = entitlement_totals(db)
    rows = []
    for product in products:
        override = overrides.get(product.id)
        base = {
            "product_id": product.id,
            "old_name": product.name or "",
            "store": product.store or "",
            "sell_price": round(float(product.sell_price or 0), 2),
            "total_uses": int(product.total_uses or 0),
            "validity_days": int(product.validity_days or 0),
            "is_active": bool(product.is_active),
            "offer_kind": product.offer_kind or "",
            "remaining_uses": accounts["per_product"].get(product.id, {}).get("uses", 0),
            "remaining_cents": accounts["per_product"].get(product.id, {}).get("cents", 0),
            "remaining_cards": accounts["per_product"].get(product.id, {}).get("cards", 0),
        }
        if (product.offer_kind or "") in ("offer", "migrated") and product.primary_item_id and not _pending_override(product, override):
            base.update({
                "status": "already",
                "reasons": [],
                "notes": ["已经是按品目记下的组合。"],
                "primary_item_id": product.primary_item_id,
                "primary_name": items_by_id[product.primary_item_id].name if product.primary_item_id in items_by_id else "",
                "buy_count": int(product.buy_count or 0),
                "gift_count": int(product.gift_count or 0),
                "gifts": [],
                "alias_item_ids": [],
                "generated_name": product.name or "",
                "computed_total": int(product.total_uses or 0),
                "changes_template_total": False,
                "count_source": product.offer_kind,
                "token": "",
                "fingerprint": product.fingerprint or "",
            })
            gift_rows = db.query(PackageOfferGift).filter(PackageOfferGift.product_id == product.id).all()
            base["gifts"] = [{
                "item_id": row.inventory_item_id,
                "qty": int(row.quantity or 0),
                "name": items_by_id[row.inventory_item_id].name if row.inventory_item_id in items_by_id else "",
                "unit": items_by_id[row.inventory_item_id].unit if row.inventory_item_id in items_by_id else "次",
            } for row in gift_rows]
        elif override is not None and _pending_override(product, override):
            base.update(propose_override(product, override, items_by_id))
        else:
            proposal = propose_legacy(product, items, bound_map.get(product.id, []))
            base.update(proposal)
        rows.append(base)
    return {
        "generated_at": beijing_now().strftime("%Y-%m-%d %H:%M"),
        "rows": rows,
        "accounts": {
            "remaining_uses": accounts["remaining_uses"],
            "remaining_cents": accounts["remaining_cents"],
            "included_cards": accounts["included_cards"],
            "card_count": accounts["card_count"],
            "redemption_count": accounts["redemption_count"],
            "package_payment_count": accounts["package_payment_count"],
            "unlinked_uses": accounts["unlinked_uses"],
            "unlinked_cents": accounts["unlinked_cents"],
            "card_checksum": accounts["card_checksum"],
            "redemption_checksum": accounts["redemption_checksum"],
            "payment_checksum": accounts["payment_checksum"],
        },
        "auto_count": sum(1 for row in rows if row["status"] == "auto"),
        "confirmed_count": sum(1 for row in rows if row["status"] == "confirmed"),
        "review_count": sum(1 for row in rows if row["status"] == "needs_review"),
        "already_count": sum(1 for row in rows if row["status"] == "already"),
    }


def render_text(report: dict) -> str:
    accounts = report["accounts"]
    lines = [
        "套餐迁移预演（只读，未修改任何数据）",
        f"生成时间：{report['generated_at']}",
        "",
        "对账",
        f"已购未用次数：{accounts['remaining_uses']}",
        f"已购未用金额：¥{accounts['remaining_cents'] / 100:.2f}",
        f"计入的已购卡：{accounts['included_cards']} 张（不含已退款、已撤销）",
        f"全部已购卡：{accounts['card_count']} 张",
        f"核销记录：{accounts['redemption_count']} 条",
        f"套餐收款：{accounts['package_payment_count']} 笔",
        f"未挂目录的未用次数：{accounts['unlinked_uses']}",
        f"未挂目录的未用金额：¥{accounts['unlinked_cents'] / 100:.2f}",
        "未用金额按每张卡「成交价 × 剩余次数 / 总次数」四舍五入到分后相加。",
        "预演不改这些数。正式迁移也不改已购卡、核销和收款，所以迁移后必须与这里一致。",
        "",
        f"可自动拆解 {report['auto_count']} 条，已人工确认 {report['confirmed_count']} 条，"
        f"需人工确认 {report['review_count']} 条，已是新结构 {report['already_count']} 条。",
    ]
    for row in report["rows"]:
        lines.append("")
        lines.append(f"#{row['product_id']} {row['old_name']}  [{row['status']}]")
        lines.append(
            f"  目录：¥{row['sell_price']:g} / {row['total_uses']} 次 / "
            f"{'不限期' if not row['validity_days'] else str(row['validity_days']) + ' 天'} / "
            f"{row['store'] or '两店通用'}"
        )
        lines.append(
            f"  已购未用：{row['remaining_uses']} 次 / ¥{row['remaining_cents'] / 100:.2f}"
            f"（{row['remaining_cards']} 张）"
        )
        if row.get("primary_name") or row.get("primary_item_id"):
            lines.append(
                f"  主品目：{row.get('primary_name') or '未识别'} #{row.get('primary_item_id') or '-'}"
                f"  买 {row.get('buy_count') or 0} 送 {row.get('gift_count') or 0}"
            )
        gifts = row.get("gifts") or []
        if gifts:
            shown = "、".join(f"{gift['name']}#{gift['item_id']}×{gift['qty']}" for gift in gifts)
            lines.append(f"  赠品：{shown}")
        else:
            lines.append("  赠品：无")
        if row.get("generated_name"):
            lines.append(f"  生成名称：{row['generated_name']}")
        for reason in row.get("reasons") or []:
            lines.append(f"  需确认：{reason}")
        for note in row.get("notes") or []:
            lines.append(f"  说明：{note}")
    lines.append("")
    return "\n".join(lines)


def _snapshot(db: Session, product: PackageProduct) -> dict:
    gifts = db.query(PackageOfferGift).filter(PackageOfferGift.product_id == product.id).all()
    aliases = db.query(PackageOfferAlias).filter(PackageOfferAlias.product_id == product.id).all()
    applicable = db.query(PackageApplicableItem.inventory_item_id).filter(
        PackageApplicableItem.product_id == product.id
    ).all()
    return {
        "name": product.name or "",
        "notes": product.notes or "",
        "primary_item_id": product.primary_item_id,
        "buy_count": int(product.buy_count or 0),
        "gift_count": int(product.gift_count or 0),
        "fingerprint": product.fingerprint or "",
        "offer_kind": product.offer_kind or "",
        "migrated_at": product.migrated_at.isoformat(sep=" ") if product.migrated_at else "",
        "migration_batch": product.migration_batch or "",
        "archived_at": product.archived_at.isoformat(sep=" ") if product.archived_at else "",
        "sell_price": float(product.sell_price or 0),
        "total_uses": int(product.total_uses or 0),
        "unit_price": float(product.unit_price or 0),
        "validity_days": int(product.validity_days or 0),
        "is_active": bool(product.is_active),
        "gifts": [{"item_id": row.inventory_item_id, "qty": int(row.quantity or 0)} for row in gifts],
        "aliases": [{
            "item_id": row.inventory_item_id,
            "kind": row.kind or "primary",
            "gift_item_id": int(row.gift_item_id or 0),
        } for row in aliases],
        "applicable_ids": [int(row[0]) for row in applicable if row[0]],
    }


def datetime_from_iso(value: str):
    text_value = (value or "").strip()
    if not text_value:
        return None
    return datetime.fromisoformat(text_value)


def _replace_children(db: Session, product: PackageProduct, gifts: list[dict], aliases: list[dict]) -> None:
    db.query(PackageOfferGift).filter(PackageOfferGift.product_id == product.id).delete(synchronize_session=False)
    db.query(PackageOfferAlias).filter(PackageOfferAlias.product_id == product.id).delete(synchronize_session=False)
    for gift in gifts:
        db.add(PackageOfferGift(
            product_id=product.id,
            inventory_item_id=int(gift["item_id"]),
            quantity=int(gift["qty"]),
        ))
    for alias in aliases:
        db.add(PackageOfferAlias(
            product_id=product.id,
            inventory_item_id=int(alias["item_id"]),
            kind=alias.get("kind") or "primary",
            gift_item_id=int(alias.get("gift_item_id") or 0),
        ))


def _apply_one(db: Session, product: PackageProduct, row: dict, batch_id: str, operator: str, override) -> None:
    before = _snapshot(db, product)
    natural_ids = []
    if row.get("primary_item_id"):
        natural_ids.append(int(row["primary_item_id"]))
    natural_ids.extend(int(gift["item_id"]) for gift in row.get("gifts") or [])
    natural_ids.extend(int(item_id) for item_id in row.get("alias_item_ids") or [])
    has_cards = db.query(CustomerPackage.id).filter(CustomerPackage.product_id == product.id).first() is not None
    applicable_changed = False
    if not has_cards:
        db.query(PackageApplicableItem).filter(PackageApplicableItem.product_id == product.id).delete(synchronize_session=False)
        for item_id in dict.fromkeys(natural_ids):
            db.add(PackageApplicableItem(product_id=product.id, inventory_item_id=item_id))
        applicable_changed = True
    fingerprint = row.get("fingerprint") or ""
    taken = None
    if fingerprint:
        taken = db.query(PackageProduct).filter(
            PackageProduct.fingerprint == fingerprint,
            PackageProduct.id != product.id,
        ).first()
    stored_fingerprint = "" if taken else fingerprint
    old_name = product.name or ""
    new_name = (row.get("generated_name") or old_name)[:120]
    if old_name and old_name != new_name:
        product.notes = ((product.notes or "") + f"\n[迁移前名称] {old_name}").strip()
    product.name = new_name
    product.primary_item_id = row.get("primary_item_id")
    product.buy_count = int(row.get("buy_count") or 0)
    product.gift_count = int(row.get("gift_count") or 0)
    product.fingerprint = stored_fingerprint
    product.offer_kind = "migrated"
    product.migrated_at = now_local()
    product.migration_batch = batch_id
    if row.get("status") == "confirmed" and row.get("changes_template_total"):
        computed = int(row.get("computed_total") or 0)
        if computed >= 1:
            product.total_uses = computed
            product.unit_price = round(float(product.sell_price or 0) / computed, 2)
    _replace_children(
        db,
        product,
        [{"item_id": gift["item_id"], "qty": gift["qty"]} for gift in row.get("gifts") or []],
        [{"item_id": item_id, "kind": "primary", "gift_item_id": 0} for item_id in row.get("alias_item_ids") or []],
    )
    if override is not None and row.get("status") == "confirmed":
        override.applied_at = now_local()
    note = "已拆成主品目和赠品。已售卡的次数与成交价未改。"
    if has_cards:
        note += " 已有客户购买，没有改可抵绑定，避免旧卡能抵的项目变掉。"
    if taken is not None:
        note += f" 相同组合已在 #{taken.id}，新开单复用那一条，本条继续挂已售卡。"
    if row.get("changes_template_total") and row.get("status") == "confirmed":
        note += " 目录总次数已按人工确认调整。"
    db.add(PackageMigrationLog(
        batch_id=batch_id,
        product_id=product.id,
        action="applied",
        before_json=json.dumps({**before, "applicable_changed": applicable_changed}, ensure_ascii=False),
        after_json=json.dumps({
            "name": product.name,
            "fingerprint": product.fingerprint,
            "used_override": bool(override is not None and row.get("status") == "confirmed"),
            "canonical_id": taken.id if taken is not None else None,
        }, ensure_ascii=False),
        note=note,
        operator=(operator or "")[:80],
        created_at=now_local(),
    ))


def apply_migration(db: Session, operator: str = "") -> dict:
    before = entitlement_totals(db)
    report = build_report(db)
    batch_id = now_local().strftime("%Y%m%d%H%M%S") + uuid.uuid4().hex[:4]
    overrides = {
        int(row.product_id): row
        for row in db.query(PackageMigrationOverride).all()
        if row.product_id
    }
    applied = 0
    for row in report["rows"]:
        if row["status"] not in ("auto", "confirmed"):
            continue
        product = db.get(PackageProduct, row["product_id"])
        if product is None:
            continue
        _apply_one(db, product, row, batch_id, operator, overrides.get(product.id) if row["status"] == "confirmed" else None)
        applied += 1
    db.flush()
    after = entitlement_totals(db)
    if not _totals_match(before, after):
        db.rollback()
        raise MigrationError("对账不一致，这一批已经回滚，没有留下修改。")
    return {
        "batch_id": batch_id,
        "applied": applied,
        "skipped_review": report["review_count"],
        "skipped_done": report["already_count"],
        "before": _public_accounts(before),
        "after": _public_accounts(after),
    }


def _public_accounts(raw: dict) -> dict:
    return {
        "remaining_uses": raw["remaining_uses"],
        "remaining_cents": raw["remaining_cents"],
        "card_checksum": raw["card_checksum"],
        "redemption_checksum": raw["redemption_checksum"],
        "payment_checksum": raw["payment_checksum"],
    }


def latest_batch_id(db: Session) -> str:
    row = db.query(PackageMigrationLog).filter(
        PackageMigrationLog.action == "applied"
    ).order_by(PackageMigrationLog.id.desc()).first()
    return row.batch_id if row else ""


def rollback_migration(db: Session, batch_id: str, operator: str = "") -> dict:
    batch_id = (batch_id or "").strip()
    if not batch_id:
        batch_id = latest_batch_id(db)
    if not batch_id:
        raise MigrationError("还没有可以回退的迁移。")
    logs = db.query(PackageMigrationLog).filter(
        PackageMigrationLog.batch_id == batch_id,
        PackageMigrationLog.action == "applied",
    ).order_by(PackageMigrationLog.id.asc()).all()
    if not logs:
        rolled = db.query(PackageMigrationLog.id).filter(
            PackageMigrationLog.batch_id == batch_id,
            PackageMigrationLog.action == "rolled_back",
        ).first()
        if rolled:
            raise MigrationError("这批已经回退过。")
        raise MigrationError("没有这一批可回退的记录。")
    before = entitlement_totals(db)
    for log in logs:
        product = db.get(PackageProduct, log.product_id) if log.product_id else None
        if product is None:
            raise MigrationError(f"套餐 #{log.product_id} 已经不在了，不能安全回退。")
        snapshot = json.loads(log.before_json or "{}")
        product.name = snapshot.get("name") or ""
        product.notes = snapshot.get("notes") or ""
        product.primary_item_id = snapshot.get("primary_item_id")
        product.buy_count = int(snapshot.get("buy_count") or 0)
        product.gift_count = int(snapshot.get("gift_count") or 0)
        product.fingerprint = snapshot.get("fingerprint") or ""
        product.offer_kind = snapshot.get("offer_kind") or ""
        product.migrated_at = datetime_from_iso(snapshot.get("migrated_at") or "")
        product.migration_batch = snapshot.get("migration_batch") or ""
        product.archived_at = datetime_from_iso(snapshot.get("archived_at") or "")
        product.sell_price = float(snapshot.get("sell_price") or 0)
        product.total_uses = int(snapshot.get("total_uses") or 0)
        product.unit_price = float(snapshot.get("unit_price") or 0)
        product.validity_days = int(snapshot.get("validity_days") or 0)
        product.is_active = bool(snapshot.get("is_active", True))
        _replace_children(db, product, snapshot.get("gifts") or [], snapshot.get("aliases") or [])
        if snapshot.get("applicable_changed"):
            db.query(PackageApplicableItem).filter(PackageApplicableItem.product_id == product.id).delete(synchronize_session=False)
            for item_id in snapshot.get("applicable_ids") or []:
                db.add(PackageApplicableItem(product_id=product.id, inventory_item_id=int(item_id)))
        after_info = {}
        try:
            after_info = json.loads(log.after_json or "{}")
        except Exception:
            after_info = {}
        if after_info.get("used_override"):
            override = db.query(PackageMigrationOverride).filter(
                PackageMigrationOverride.product_id == product.id
            ).first()
            if override is not None:
                override.applied_at = None
        log.action = "rolled_back"
        log.note = ((log.note or "") + f"\n[回退] {operator} {now_local().strftime('%Y-%m-%d %H:%M')}").strip()
    db.flush()
    after = entitlement_totals(db)
    if not _totals_match(before, after):
        db.rollback()
        raise MigrationError("回退时对账不一致，已取消回退。")
    return {"batch_id": batch_id, "count": len(logs), "before": _public_accounts(before), "after": _public_accounts(after)}


def save_override(
    db: Session,
    product: PackageProduct,
    *,
    primary_item_id: int,
    buy_count: int,
    gift_count: int,
    gifts: list[tuple[int, int]],
    alias_item_ids: list[int],
    note: str,
    operator: str,
) -> PackageMigrationOverride:
    if (product.offer_kind or "") == "offer":
        raise MigrationError("这条是新结构开出来的，不用走旧套餐确认。")
    row = db.query(PackageMigrationOverride).filter(PackageMigrationOverride.product_id == product.id).first()
    if row is None:
        row = PackageMigrationOverride(product_id=product.id)
        db.add(row)
    row.primary_item_id = primary_item_id
    row.buy_count = int(buy_count)
    row.gift_count = int(gift_count)
    row.gifts_json = json.dumps([{"item_id": item_id, "qty": qty} for item_id, qty in gifts], ensure_ascii=False)
    row.alias_item_ids = ",".join(str(item_id) for item_id in alias_item_ids)
    row.note = (note or "")[:500]
    row.confirmed_by = (operator or "")[:80]
    row.confirmed_at = now_local()
    row.applied_at = None
    db.flush()
    return row


def clear_override(db: Session, product_id: int) -> None:
    db.query(PackageMigrationOverride).filter(PackageMigrationOverride.product_id == product_id).delete(synchronize_session=False)

