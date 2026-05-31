"""小暖医生导出的「药品/疫苗/驱虫/消耗品/商品」Excel 解析 + 字段映射。

小暖原生导出 37 列结构（药品、疫苗、驱虫共用）：
  所属目录 / 编码 / 名称 / 条形码 / 药品成分 / 拼音简写 / 英文名 / 通用名 /
  品牌 / 默认生产商 / 默认生产商编码 / 供应商 / 供应商编码 / 使用方式 /
  投药方式 / 有批次 / 是否管控 / 销售价格 / 会员价格 / 参与打折 / 成本价格 /
  入库参考价 / 可订 / 可销 / 状态 / 可盘 / 出入库单位 / 投药单位 /
  药品规格换算 / 包装规格 / 是否零库存销售 / 现有库存 / 标识 / 备注 /
  ID / 麻醉药品 / 处方药品
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("inventory_import")

# 「所属目录」中文 → 我们 InventoryItem.category
_CATEGORY_MAP = {
    "药品": "medication",
    "疫苗": "vaccine",
    "驱虫": "antiparasitic",
    "驱虫药": "antiparasitic",
    "消耗品": "consumable",
    "耗材": "consumable",
    "商品": "product",
    "产品": "product",
    "服务": "service",  # 是 InventoryItem.is_service=True
    "处置": "service",
    "美容": "grooming",
    "化验": "lab",
    "检验": "lab",
}


def _norm_str(v) -> str:
    """把 NaN / 空字符 / 浮点数（小暖把数字也当字符串）都规范成 str。"""
    if v is None:
        return ""
    s = str(v).strip()
    if s.lower() == "nan" or s == "<NA>":
        return ""
    # 去除 "可 销" 中间的多余空格
    s = " ".join(s.split())
    return s


def _norm_float(v, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _is_yes(v) -> bool:
    s = _norm_str(v)
    return s in ("是", "Y", "y", "yes", "true", "1", "可销", "可 销", "可订", "可 订")


def parse_xls_to_records(path: str) -> tuple[list[dict[str, Any]], list[str]]:
    """读 xls/xlsx → 标准化的 InventoryItem 字段 dict 列表。

    返回 (records, warnings)
    """
    import pandas as pd
    warnings: list[str] = []
    try:
        df = pd.read_excel(path)
    except Exception as e:
        try:
            df = pd.read_excel(path, engine="xlrd")
        except Exception as e2:
            raise RuntimeError(f"无法读取 Excel: {e} / xlrd: {e2}")

    # 列名校验
    expected = ["名称", "所属目录", "销售价格"]
    missing = [c for c in expected if c not in df.columns]
    if missing:
        raise RuntimeError(f"Excel 缺少关键列：{missing}。请确认是小暖原生导出格式。")

    records: list[dict[str, Any]] = []
    for idx, row in df.iterrows():
        name = _norm_str(row.get("名称"))
        if not name:
            warnings.append(f"第 {idx + 2} 行无名称，跳过")
            continue

        # 类别 — 「所属目录」可能是 "药品" / "驱虫" / "驱虫>" 之类，取顶层关键字
        raw_cat = _norm_str(row.get("所属目录")).rstrip(">")
        category = "medication"  # default
        for key, val in _CATEGORY_MAP.items():
            if key in raw_cat:
                category = val
                break

        # is_service: category=service
        is_service = (category == "service")

        # is_controlled: 是否管控 OR 麻醉药品
        is_controlled = _is_yes(row.get("是否管控")) or _is_yes(row.get("麻醉药品"))

        # is_active: 可销 == "可 销" OR "可销"
        sell_raw = _norm_str(row.get("可销"))
        is_active = (sell_raw in ("可销", "可 销")) or _is_yes(row.get("状态"))
        if not sell_raw:
            is_active = True  # 默认启用

        # supplier — 数字 ID（如 "1.0"）暂存为「小暖#1」
        supplier_raw = _norm_str(row.get("供应商"))
        supplier = ""
        if supplier_raw:
            try:
                supplier = f"小暖#{int(float(supplier_raw))}"
            except (ValueError, TypeError):
                supplier = supplier_raw

        # unit / unit2
        unit = _norm_str(row.get("出入库单位")) or "个"
        unit2 = _norm_str(row.get("包装规格"))
        if unit2 and unit2 == unit:
            unit2 = ""  # 主副相同则副留空

        # notes 拼接所有有信息但我们没字段的列
        notes_parts = []
        if _norm_str(row.get("药品成分")):
            notes_parts.append(f"成分：{_norm_str(row.get('药品成分'))}")
        if _norm_str(row.get("品牌")):
            notes_parts.append(f"品牌：{_norm_str(row.get('品牌'))}")
        if _norm_str(row.get("默认生产商")):
            notes_parts.append(f"生产商#{_norm_str(row.get('默认生产商'))}")
        if _norm_str(row.get("拼音简写")):
            notes_parts.append(f"拼音：{_norm_str(row.get('拼音简写'))}")
        if _norm_str(row.get("英文名")):
            notes_parts.append(f"英文：{_norm_str(row.get('英文名'))}")
        if _norm_str(row.get("通用名")):
            notes_parts.append(f"通用名：{_norm_str(row.get('通用名'))}")
        if _is_yes(row.get("处方药品")):
            notes_parts.append("处方药")
        # 用户填的备注列
        if _norm_str(row.get("备注")):
            notes_parts.append(_norm_str(row.get("备注")))
        # 老系统 ID 追溯
        legacy_id = _norm_str(row.get("ID"))
        if legacy_id:
            notes_parts.append(f"[小暖ID:{legacy_id}]")
        notes = " · ".join(notes_parts)

        # subcategory — 「标识」字段，如 "驱虫药" / "兽药+保健" / "疫苗制品"
        subcategory = _norm_str(row.get("标识"))[:60]

        # stock_qty — 小暖可能为负（透支），导入按 max(0, x)
        stock_qty = max(0.0, _norm_float(row.get("现有库存"), 0.0))

        rec = {
            "name": name[:200],
            "category": category,
            "subcategory": subcategory,
            "is_service": is_service,
            "is_controlled": is_controlled,
            "unit": unit[:20],
            "unit2": unit2[:20] if unit2 else "",
            "unit2_ratio": 1.0,  # 老系统没换算，统一为 1
            "sell_price": _norm_float(row.get("销售价格"), 0.0),
            "cost_price": _norm_float(row.get("成本价格"), 0.0),
            "stock_qty": stock_qty,
            "supplier": supplier[:200],
            "notes": notes[:1000],
            "is_active": is_active,
            "_legacy_id": legacy_id,  # 内部用，匹配是否已导入过
        }
        records.append(rec)

    return records, warnings


def preview_import(db, records: list[dict[str, Any]], store: str = "") -> dict:
    """试运行：按 (name, category, store) 检查冲突，返回统计 + 前 20 条预览。"""
    from app.models import InventoryItem
    new_count = 0
    update_count = 0
    samples = []
    for r in records:
        existing = db.query(InventoryItem).filter(
            InventoryItem.name == r["name"],
            InventoryItem.category == r["category"],
            InventoryItem.store == store,
        ).first()
        action = "update" if existing else "new"
        if existing:
            update_count += 1
        else:
            new_count += 1
        if len(samples) < 20:
            samples.append({**r, "_action": action, "_existing_id": existing.id if existing else None})
    return {
        "total": len(records),
        "new_count": new_count,
        "update_count": update_count,
        "samples": samples,
    }


def commit_import(db, records: list[dict[str, Any]], store: str = "", strategy: str = "skip") -> dict:
    """正式导入。

    strategy:
      skip   - 已存在则跳过（默认，最安全）
      update - 已存在则更新价格/库存/供应商，不动 notes
      overwrite - 完全覆盖
    """
    from app.models import InventoryItem
    created = 0
    updated = 0
    skipped = 0
    for r in records:
        existing = db.query(InventoryItem).filter(
            InventoryItem.name == r["name"],
            InventoryItem.category == r["category"],
            InventoryItem.store == store,
        ).first()
        if existing:
            if strategy == "skip":
                skipped += 1
                continue
            if strategy == "update":
                existing.sell_price = r["sell_price"]
                existing.cost_price = r["cost_price"]
                existing.stock_qty = r["stock_qty"]
                existing.supplier = r["supplier"] or existing.supplier
                existing.is_controlled = r["is_controlled"] or existing.is_controlled
                existing.unit = r["unit"] or existing.unit
                if r["unit2"]:
                    existing.unit2 = r["unit2"]
                if r["subcategory"] and not existing.subcategory:
                    existing.subcategory = r["subcategory"]
            else:  # overwrite
                for k in ("sell_price", "cost_price", "stock_qty", "supplier",
                          "is_controlled", "is_service", "unit", "unit2",
                          "subcategory", "notes", "is_active"):
                    setattr(existing, k, r[k])
            updated += 1
        else:
            item = InventoryItem(
                name=r["name"],
                category=r["category"],
                subcategory=r["subcategory"],
                is_service=r["is_service"],
                is_controlled=r["is_controlled"],
                unit=r["unit"],
                unit2=r["unit2"],
                unit2_ratio=r["unit2_ratio"],
                sell_price=r["sell_price"],
                cost_price=r["cost_price"],
                stock_qty=r["stock_qty"],
                supplier=r["supplier"],
                notes=r["notes"],
                is_active=r["is_active"],
                store=store,
            )
            db.add(item)
            created += 1
    db.commit()
    return {"created": created, "updated": updated, "skipped": skipped}
