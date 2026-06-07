"""
显微镜报告 AI 文字稿生成。

输入：医生已经在表单上勾选好的结构化数据
  - template_type: skin / ear / fecal / general
  - sample_site / magnification / item_label / pet_species
  - findings: [{cat:"microbe|parasite|pathology", name, grade}]
  - extras: [{name, grade}]   医生自定义补充行
  - narrative_user: 医生已写的"镜下所见"文本（如果非空作为参考补充）

输出：{ok, narrative, conclusion, advice, error?}
"""
from __future__ import annotations
import json
import logging
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """你是宠物医院的资深显微镜检查报告医生。
任务：根据医生已经勾选好的镜检结构化结果，输出三段中文文字稿，分别填入「镜下所见」「结论」「建议」。

要求：
1. 输出严格的 JSON 对象（不要 markdown 包裹），结构：
   {"narrative": "…", "conclusion": "…", "advice": "…"}
2. 「镜下所见」（narrative）：3-6 句，描述视野中观察到的关键微生物 / 细胞 / 颗粒及其大致数量/形态特征。可结合放大倍数（如油镜下…），不要堆砌阴性项。
3. 「结论」（conclusion）：1-3 句，给出明确诊断方向或镜检判读，必要时带半定量等级如「马拉色菌感染（++）」。寄生虫阳性需写明虫种与提示。如果所有项目都为阴性 / 未检出，写「本次镜检未见明显异常微生物 / 寄生虫」之类。
4. 「建议」（advice）：2-4 条，给临床/居家处理与复查节奏建议，可包含治疗方向（抗真菌 / 抗菌 / 驱虫 / 局部清洁）+ 复查窗口（如 7/14 天复查）。
5. 用中性专业语气、宠物医学常用表述。不要瞎编没勾选的内容；不要写「以上为示例」。
6. 半定量符号说明：- 视野下未见 / + 1-5 个 / ++ 5-15 个 / +++ >15 个。
7. 若样本部位写明耳道/皮肤/粪便等，结论与建议要贴合该部位（例：耳道马拉色菌 → 提示外耳炎，建议每日清耳）。
8. 若医生在「镜下所见」字段已写了一句补充描述，请把它优雅地合并进 narrative，不要完全抛弃。
"""


def _format_payload(payload: dict) -> str:
    species_map = {"dog": "犬", "cat": "猫"}
    species = species_map.get(payload.get("pet_species") or "", payload.get("pet_species") or "—")
    tpl_label = {
        "skin": "皮肤刮片 / 真菌",
        "ear": "耳道分泌物",
        "fecal": "粪检 / 寄生虫",
        "general": "通用涂片",
    }.get(payload.get("template_type") or "general", "通用")

    lines = [
        f"【模板类型】{tpl_label}",
        f"【动物种属】{species}",
        f"【归属检查项】{payload.get('item_label') or '—'}",
        f"【标本部位】{payload.get('sample_site') or '—'}",
        f"【放大倍数】{payload.get('magnification') or '—'}",
    ]

    findings = payload.get("findings") or []
    microbes = [f for f in findings if f.get("cat") == "microbe"]
    parasites = [f for f in findings if f.get("cat") == "parasite"]
    pathology = [f for f in findings if f.get("cat") == "pathology"]

    lines.append("")
    lines.append("【镜检可见 — 微生物 / 细胞 / 颗粒（半定量）】")
    if microbes:
        for f in microbes:
            lines.append(f"  · {f.get('name')}：{f.get('grade')}")
    else:
        lines.append("  · 全部阴性 / 未勾选")

    lines.append("")
    lines.append("【寄生虫定性】")
    if parasites:
        for f in parasites:
            lines.append(f"  · {f.get('name')}：{f.get('grade')}")
    else:
        lines.append("  · 全部阴性 / 未勾选")

    if pathology:
        lines.append("")
        lines.append("【病理 / 大体描述】")
        for f in pathology:
            lines.append(f"  · {f.get('name')}：{f.get('grade')}")

    extras = payload.get("extras") or []
    extras = [e for e in extras if (e.get("name") or "").strip() and (e.get("grade") or "").strip()]
    if extras:
        lines.append("")
        lines.append("【医生自定义补充】")
        for e in extras:
            lines.append(f"  · {e.get('name')}：{e.get('grade')}")

    nu = (payload.get("narrative_user") or "").strip()
    if nu:
        lines.append("")
        lines.append("【医生已写的镜下所见补充】")
        lines.append(nu)

    return "\n".join(lines)


async def draft_microscopy_text(payload: dict) -> dict[str, Any]:
    if not settings.openai_api_key:
        return {"ok": False, "error": "未配置 OPENAI_API_KEY"}

    try:
        from openai import AsyncOpenAI
    except ImportError:
        return {"ok": False, "error": "缺少 openai 库"}

    base = (settings.openai_base_url or "").strip() or None
    client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=base)

    user_text = _format_payload(payload)

    # 模型优先级：WECOM_AGENT_MODEL（便宜版） > OPENAI_MODEL
    model = (getattr(settings, "wecom_agent_model", "") or "").strip() or \
        (settings.openai_model or "gpt-4o-mini")

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            temperature=0.4,
            max_tokens=900,
        )
    except Exception as e:
        logger.warning("[microscopy_ai] API failed: %s", e)
        return {"ok": False, "error": f"调用模型失败：{e}"}

    raw = (resp.choices[0].message.content or "").strip()
    # 剥 markdown
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    try:
        data = json.loads(raw)
    except Exception as e:
        logger.warning("[microscopy_ai] JSON parse failed: %s; raw=%s", e, raw[:300])
        return {"ok": False, "error": f"模型输出不是有效 JSON：{e}", "raw": raw}

    if not isinstance(data, dict):
        return {"ok": False, "error": "模型输出不是 JSON 对象"}

    return {
        "ok": True,
        "narrative": str(data.get("narrative", "")).strip(),
        "conclusion": str(data.get("conclusion", "")).strip(),
        "advice": str(data.get("advice", "")).strip(),
    }
