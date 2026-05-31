"""门店级价格覆盖（方案 H — JSON 覆盖列）。

InventoryItem.store_overrides 是 JSON 字符串，格式：
    {"东环店": {"sell": 99.5, "cost": 50},
     "横岗店": {"sell": 105, "cost": 52}}

调用约定：
    eff = effective_sell_price(item, "东环店")  # 没配置 → 回退到 item.sell_price
    set_override(item, "东环店", sell=99.5, cost=50)  # 写入 / 更新
    clear_override(item, "东环店")                    # 删除该店覆盖
"""
from __future__ import annotations

import json
from typing import Any


def parse_overrides(item) -> dict[str, dict[str, float]]:
    """读 InventoryItem 的 store_overrides，返回 dict（解析失败返回空 dict）。"""
    raw = (getattr(item, "store_overrides", "") or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def effective_sell_price(item, store: str = "") -> float:
    """取当前 store 对应的有效售价；store 为空或无覆盖时返回默认价。"""
    if not store:
        return float(item.sell_price or 0.0)
    ov = parse_overrides(item).get(store, {})
    val = ov.get("sell")
    if val is None or val == "":
        return float(item.sell_price or 0.0)
    try:
        return float(val)
    except (TypeError, ValueError):
        return float(item.sell_price or 0.0)


def effective_cost_price(item, store: str = "") -> float:
    """同 effective_sell_price，对应成本价。"""
    if not store:
        return float(item.cost_price or 0.0)
    ov = parse_overrides(item).get(store, {})
    val = ov.get("cost")
    if val is None or val == "":
        return float(item.cost_price or 0.0)
    try:
        return float(val)
    except (TypeError, ValueError):
        return float(item.cost_price or 0.0)


def has_override(item, store: str) -> bool:
    return store in parse_overrides(item)


def set_override(item, store: str, sell: float | None = None, cost: float | None = None) -> None:
    """新增 / 更新某门店的价格覆盖。sell/cost 任一非 None 即写入。"""
    if not store:
        return
    overrides = parse_overrides(item)
    cur = overrides.get(store, {})
    if sell is not None and sell != "":
        try:
            cur["sell"] = float(sell)
        except (TypeError, ValueError):
            pass
    if cost is not None and cost != "":
        try:
            cur["cost"] = float(cost)
        except (TypeError, ValueError):
            pass
    if cur:
        overrides[store] = cur
        item.store_overrides = json.dumps(overrides, ensure_ascii=False)


def clear_override(item, store: str) -> None:
    """删除某门店的覆盖（之后回退到默认价）。"""
    overrides = parse_overrides(item)
    if store in overrides:
        del overrides[store]
        item.store_overrides = json.dumps(overrides, ensure_ascii=False) if overrides else ""


def overrides_summary(item) -> str:
    """生成一行人类可读的摘要，用于库存列表 tooltip。
    例：东环店 ¥99.5 / 横岗店 ¥105
    """
    parts = []
    for store, cur in parse_overrides(item).items():
        s = cur.get("sell")
        if s is not None:
            parts.append(f"{store} ¥{float(s):g}")
    return " / ".join(parts)
