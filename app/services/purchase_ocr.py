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
  "name": "商品名（纯商品名，不要含剂量/规格，例：拜耳:夫速宁礼舒替尼片，不要写 ...10mg）",
  "spec": "规格/剂量（例：10mg、5ml、50mg×10片，从商品名末尾或规格列提取）",
  "pack_size": 包装内含的最小单位数（数字，例：30片/盒 填 30；可空时为 0）,
  "main_unit": "最小单位（如 片/支/ml/粒，按商品形态判断）",
  "pack_unit": "包装单位（如 盒/瓶/袋；若商品就按散件卖则与 main_unit 相同）",
  "qty": 数量（按 pack_unit 计的进货件数，例：进了 3 盒填 3），
  "unit": "本行的单位（取 pack_unit 即可）",
  "unit_price": 进价单价（数字，¥/pack_unit，例：260 元/盒填 260），
  "amount": 行小计（数字，可空时为 0），
  "batch_no": "批号（若图上有打印，否则为空字符串）",
  "expiry_date": "有效期 YYYY-MM-DD（若图上是月份则补 01 日，否则空字符串）",
  "manufacturer": "厂商（可选）"
}

字段拆分举例：
  「拜耳:夫速宁礼舒替尼片 10mg 30片/盒  2盒  260元/盒」
   → name="拜耳:夫速宁礼舒替尼片", spec="10mg", pack_size=30, main_unit="片", pack_unit="盒", qty=2, unit="盒", unit_price=260
  「英特威猫三联疫苗 1ml/支 10支/盒 5盒 150元/支」
   → name="英特威猫三联疫苗", spec="1ml", pack_size=10, main_unit="支", pack_unit="盒", qty=5, unit="盒", unit_price=1500
   （注意：若进价是按支标的而 qty 按盒计，就把 unit_price × pack_size 换算成 ¥/盒）

要求：
1. 输出必须是 JSON 数组，根元素 [ ... ]，不要任何说明文字。
2. 表头行（"品名/规格/数量/..."）不要算成商品。
3. 合计行（"总计/合计/小计"）不要算成商品。
4. 数字识别不出来就填 0。日期识别不出来就空字符串。
5. 模糊不清的字段宁可留空也不要乱猜。
6. 同一商品多个批次出现多行 → 输出多行。
7. name 要尽可能"干净" —— 不带 10mg/5ml 这种剂量后缀，剂量放到 spec。
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
            "pack_size":    float(row.get("pack_size") or 0),
            "main_unit":    str(row.get("main_unit", "")).strip(),
            "pack_unit":    str(row.get("pack_unit", "")).strip(),
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
    """去空格、统一大小写、剥离常见标点，用于模糊匹配。"""
    if not s:
        return ""
    out = s.strip().lower()
    for ch in (" ", "\t", "　", "·", "-", "_"):
        out = out.replace(ch, "")
    return out


def _extract_alternates(s: str) -> list[str]:
    """提取字符串里括号 / 引号内的内容作为备选名。
    例：'口腔抗菌剂（口炎康）' → ['口炎康']
        '宠尔康[复方氟康唑]' → ['复方氟康唑']
    """
    if not s:
        return []
    import re
    alts: list[str] = []
    # 中英文括号 / 方括号 / 直角引号
    for pattern in (r"[（(]([^（()）]+)[)）]", r"[【\[]([^【】\[\]]+)[】\]]", r"「([^「」]+)」"):
        for m in re.finditer(pattern, s):
            inner = m.group(1).strip()
            if inner and len(inner) >= 2:
                alts.append(inner)
    return alts


def _strip_brand_prefix(s: str) -> str:
    """去掉开头的"厂家："或"品牌-"前缀，便于跨厂家匹配。
    例：'萌邦：宠尔康（复方氟康唑乳膏）' → '宠尔康（复方氟康唑乳膏）'
    """
    if not s:
        return ""
    for sep in ("：", ":", "·", " ", "—", "-"):
        idx = s.find(sep)
        # 前缀必须比较短（≤ 6 字符），否则可能是正文中的标点
        if 0 < idx <= 6:
            return s[idx + 1:].strip()
    return s


def _candidate_names(it) -> list[str]:
    """一个品目的所有可比对名称：name + aliases。"""
    names = [(it.name or "").strip()]
    raw = getattr(it, "aliases", "") or ""
    if raw:
        try:
            arr = json.loads(raw)
            if isinstance(arr, list):
                for a in arr:
                    s = str(a or "").strip()
                    if s:
                        names.append(s)
        except Exception:
            pass
    return [n for n in names if n]


def match_item_by_name(name: str, all_items: list) -> tuple[int, float]:
    """返回 (item_id, confidence)。0~1，1=完全一致。无匹配返回 (0, 0)。
    匹配策略：
      1. 候选名集合：item.name + aliases + 各自括号内备选名
      2. 目标名集合：name + 剥前缀名 + 括号内备选名
      3. 任一对完全相等 → 1.0；剥前缀后相等 → 0.95
      4. 任一对子串包含 → 0.9（短名 ≥ 2 字才算）
      5. SequenceMatcher 各对比，取最高
    阈值：0.7
    """
    from difflib import SequenceMatcher

    def _all_forms(s: str) -> list[str]:
        """生成一个名字的所有可比对形式（去重）"""
        forms = []
        if not s:
            return forms
        nf = _normalize(s)
        if nf:
            forms.append(nf)
        ns = _normalize(_strip_brand_prefix(s))
        if ns and ns not in forms:
            forms.append(ns)
        # 括号内备选
        for alt in _extract_alternates(s):
            na = _normalize(alt)
            if na and na not in forms:
                forms.append(na)
            nas = _normalize(_strip_brand_prefix(alt))
            if nas and nas not in forms:
                forms.append(nas)
        return forms

    target_forms = _all_forms(name)
    if not target_forms:
        return (0, 0.0)
    best = (0, 0.0)
    for it in all_items:
        for cand in _candidate_names(it):
            cand_forms = _all_forms(cand)
            if not cand_forms:
                continue
            # 完全相等
            for tf in target_forms:
                for cf in cand_forms:
                    if tf == cf:
                        # 第一个完全相等就 1.0（即使是 alias 也算高质量匹配）
                        return (it.id, 1.0)
            # 子串包含（要求短的那个 ≥ 2 字符避免误命中如"水"）
            sub_hit = False
            for tf in target_forms:
                for cf in cand_forms:
                    short, long_ = (tf, cf) if len(tf) <= len(cf) else (cf, tf)
                    if len(short) >= 2 and short in long_:
                        sub_hit = True
                        break
                if sub_hit:
                    break
            # SequenceMatcher 最高 ratio
            max_ratio = 0.0
            for tf in target_forms:
                for cf in cand_forms:
                    r = SequenceMatcher(None, tf, cf).ratio()
                    if r > max_ratio:
                        max_ratio = r
            if sub_hit:
                max_ratio = max(max_ratio, 0.9)
            if max_ratio > best[1]:
                best = (it.id, max_ratio)
    return best if best[1] >= 0.7 else (0, best[1])


def add_alias_to_item(item, new_name: str) -> bool:
    """在 InventoryItem.aliases JSON 数组里追加一个名字（去重，并避开 item.name 本身）。
    返回 True 表示新增了一条；False 表示没变化。
    调用方负责后续 db.commit()。
    """
    new_name = (new_name or "").strip()
    if not new_name:
        return False
    if _normalize(new_name) == _normalize(item.name or ""):
        return False  # 跟 name 完全一致，没必要存
    raw = getattr(item, "aliases", "") or ""
    aliases: list = []
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                aliases = [str(a).strip() for a in parsed if str(a).strip()]
        except Exception:
            aliases = []
    # 去重（normalize 后比较）
    norm_set = {_normalize(a) for a in aliases}
    if _normalize(new_name) in norm_set:
        return False
    aliases.append(new_name)
    # 上限 8 个，避免无限增长
    if len(aliases) > 8:
        aliases = aliases[-8:]
    item.aliases = json.dumps(aliases, ensure_ascii=False)
    return True


def dedup_key(name: str, spec: str = "") -> str:
    """同一批 OCR 结果内的去重键：剥前缀 + normalize + 规格。
    用于把"同一次上传"里重复的多行合并入库。"""
    return _normalize(_strip_brand_prefix(name)) + "|" + _normalize(spec)
