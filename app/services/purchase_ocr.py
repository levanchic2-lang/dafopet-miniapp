"""
进货单照片识别：多模态大模型从图片中提取商品清单。
输出结构化 JSON 数组，每项含 name / spec / qty / unit / unit_price / batch_no / expiry_date。
"""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

PURCHASE_OCR_PROMPT = """你是动物医院进货单识别助手。请从图片中提取所有商品行，输出 JSON 数组（无 markdown 包裹），每个对象的结构：
{
  "name": "商品名（含品牌/规格的话尽量完整，如 强力素50mg）",
  "spec": "规格描述（如 50mg×10片/盒，可空）",
  "qty": 数量（数字，可空时为 0），
  "unit": "单位（如 盒/瓶/支/片）",
  "unit_price": 进价单价（数字，¥/单位，可空时为 0），
  "amount": 行小计（数字，可空时为 0），
  "batch_no": "批号（若图上有打印，否则为空字符串）",
  "expiry_date": "有效期 YYYY-MM-DD（若图上是月份则补 01 日，否则空字符串）",
  "manufacturer": "厂商（可选）"
}
要求：
1. 输出必须是 JSON 数组，根元素 [ ... ]，不要任何说明文字。
2. 表头行（"品名/规格/数量/..."）不要算成商品。
3. 合计行（"总计/合计/小计"）不要算成商品。
4. 数字识别不出来就填 0。日期识别不出来就空字符串。
5. 模糊不清的字段宁可留空也不要乱猜。
6. 同一商品多个批次出现多行 → 输出多行。
"""


def _encode_image_b64(path: Path) -> tuple[str, str]:
    """返回 (mime, base64)。"""
    suf = path.suffix.lower()
    mime = {
        ".png": "image/png",
        ".webp": "image/webp",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
    }.get(suf, "image/jpeg")
    return mime, base64.standard_b64encode(path.read_bytes()).decode("ascii")


async def recognize_purchase_photo(image_paths: list[Path]) -> dict[str, Any]:
    """调多模态模型识别进货单图片。
    返回 {ok: bool, items: list[dict], raw: str, error?: str}
    """
    if not settings.openai_api_key:
        return {"ok": False, "items": [], "raw": "", "error": "未配置 OPENAI_API_KEY"}
    if not image_paths:
        return {"ok": False, "items": [], "raw": "", "error": "未上传图片"}

    try:
        from openai import AsyncOpenAI
    except ImportError:
        return {"ok": False, "items": [], "raw": "", "error": "缺少 openai 库（pip install openai）"}

    base = (settings.openai_base_url or "").strip() or None
    client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=base)

    content: list[dict] = [{"type": "text", "text": PURCHASE_OCR_PROMPT}]
    for p in image_paths:
        mime, b64 = _encode_image_b64(p)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })

    try:
        resp = await client.chat.completions.create(
            model=settings.openai_model or "gpt-4o-mini",
            messages=[{"role": "user", "content": content}],
            temperature=0.0,
            max_tokens=4096,
        )
    except Exception as e:
        logger.warning("[purchase_ocr] API call failed: %s", e)
        return {"ok": False, "items": [], "raw": "", "error": f"调用模型失败：{e}"}

    raw = (resp.choices[0].message.content or "").strip()
    # 模型偶尔会包 ```json ... ``` 进 markdown，剥掉
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    # 解析 JSON 数组
    try:
        data = json.loads(raw)
    except Exception as e:
        logger.warning("[purchase_ocr] JSON parse failed: %s; raw=%s", e, raw[:300])
        return {"ok": False, "items": [], "raw": raw, "error": f"模型输出不是有效 JSON：{e}"}

    if not isinstance(data, list):
        return {"ok": False, "items": [], "raw": raw, "error": "模型输出不是数组"}

    items: list[dict] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        items.append({
            "name":         str(row.get("name", "")).strip(),
            "spec":         str(row.get("spec", "")).strip(),
            "qty":          float(row.get("qty") or 0),
            "unit":         str(row.get("unit", "")).strip(),
            "unit_price":   float(row.get("unit_price") or 0),
            "amount":       float(row.get("amount") or 0),
            "batch_no":     str(row.get("batch_no", "")).strip(),
            "expiry_date":  str(row.get("expiry_date", "")).strip()[:10],
            "manufacturer": str(row.get("manufacturer", "")).strip(),
        })

    return {"ok": True, "items": items, "raw": raw}


# ─── 匹配：识别出的品名 → 已有 InventoryItem ──────────────────
def _normalize(s: str) -> str:
    """去空格、统一大小写、常见单位统一，用于模糊匹配。"""
    if not s:
        return ""
    out = s.strip().lower()
    for ch in (" ", "\t", "　", "·", "-", "_"):
        out = out.replace(ch, "")
    # 单位单复数 + 中英
    return out


def match_item_by_name(name: str, all_items: list) -> tuple[int, float]:
    """返回 (item_id, confidence)。confidence 0~1，1=完全一致。
    无匹配返回 (0, 0)。"""
    from difflib import SequenceMatcher
    target = _normalize(name)
    if not target:
        return (0, 0.0)
    best = (0, 0.0)
    for it in all_items:
        n = _normalize(it.name or "")
        if not n:
            continue
        if n == target:
            return (it.id, 1.0)
        ratio = SequenceMatcher(None, n, target).ratio()
        # 子串包含给加分
        if target in n or n in target:
            ratio = max(ratio, 0.9)
        if ratio > best[1]:
            best = (it.id, ratio)
    return best if best[1] >= 0.7 else (0, best[1])
