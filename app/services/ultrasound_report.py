"""
B超 / 超声报告 — PDF 测量值解析 + AI 文字稿生成。

两个能力：
1. extract_pdf_text(path) → 抽取机器导出 PDF 的纯文字（电子版 PDF，pypdf）
2. structure_measurements(raw_text, exam_type) → 把杂乱文字整理成「动态分组」JSON
   （测量字段不固定，有多少存多少；兼容左右两栏排版）
3. draft_ultrasound_text(payload) → 结合宠物信息 + 测量值 + 医生主观描述
   生成「超声所见 / 超声提示(结论) / 建议」三段中文稿

模型优先级：WECOM_AGENT_MODEL（便宜版） > OPENAI_MODEL
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


_EXAM_TYPE_LABEL = {
    "cardiac": "心脏彩超（心超）",
    "abdominal": "腹部B超",
    "urogenital": "泌尿 / 生殖B超",
    "general": "通用超声",
}


def extract_pdf_text(path: Path) -> str:
    """从电子版 PDF 抽取纯文字。失败 / 扫描件返回空串。"""
    try:
        from pypdf import PdfReader
    except Exception as e:
        logger.warning("[ultrasound] pypdf 未安装：%s", e)
        return ""
    try:
        reader = PdfReader(str(path))
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        return "\n".join(parts).strip()
    except Exception as e:
        logger.warning("[ultrasound] PDF 解析失败：%s", e)
        return ""


def _client():
    from openai import AsyncOpenAI
    base = (settings.openai_base_url or "").strip() or None
    return AsyncOpenAI(api_key=settings.openai_api_key, base_url=base)


def _model() -> str:
    return (getattr(settings, "wecom_agent_model", "") or "").strip() or \
        (settings.openai_model or "gpt-4o-mini")


def _strip_md(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    return raw


_STRUCT_SYSTEM = """你是宠物医院超声测量数据整理助手。
任务：把超声机导出的测量数据文本，整理成「分组」结构的 JSON。测量项目不固定，原文有多少就整理多少，不要增删、不要编造数值。

输出严格的 JSON 对象（不要 markdown 包裹），结构：
{"groups": [
  {"group": "分组名（如 2D测量·主动脉与主动脉瓣 / M测量·左室 / Doppler·二尖瓣；原文有层级就拼起来）",
   "rows": [{"name":"指标名(如 LA Diam)", "value":"数值(如 0.96，纯数字或比值，去掉单位)", "unit":"单位(如 cm/cm/s/mmHg/%，没有就空串)"}]}
]}

要求：
1. 严格忠于原文数值，一个不漏一个不加；识别不出的字段跳过，不要瞎填。
2. 把同一大类(2D测量/M测量/Doppler测量等)下的小标题(如 左室/二尖瓣/主动脉)拼进 group 名，用「·」连接。
3. name 用原文指标名（中英文都保留原样，如 "LVIDd"、"MV E/A"、"EF(Teich)"）。
4. value 只放数值/比值本身，把单位拆到 unit。比值类(如 LA/Ao:1.07)的 unit 留空。
5. 不要把页眉页脚、宠物信息、"Page"、医院名当成测量项。
6. 若完全没有可识别的测量数据，返回 {"groups": []}。
"""


async def structure_measurements(raw_text: str, exam_type: str = "cardiac") -> dict[str, Any]:
    """杂乱 PDF 文字 → 动态分组测量 JSON。返回 {ok, groups, error?}"""
    if not settings.openai_api_key:
        return {"ok": False, "groups": [], "error": "未配置 OPENAI_API_KEY"}
    if not (raw_text or "").strip():
        return {"ok": False, "groups": [], "error": "PDF 未提取到文字（可能是扫描件 / 图片型 PDF）"}
    try:
        from openai import AsyncOpenAI  # noqa: F401
    except ImportError:
        return {"ok": False, "groups": [], "error": "缺少 openai 库"}

    user = f"【检查类型】{_EXAM_TYPE_LABEL.get(exam_type, '通用超声')}\n\n【机器导出测量文本】\n{raw_text[:8000]}"
    try:
        resp = await _client().chat.completions.create(
            model=_model(),
            messages=[{"role": "system", "content": _STRUCT_SYSTEM},
                      {"role": "user", "content": user}],
            temperature=0.0,
            max_tokens=3000,
        )
    except Exception as e:
        logger.warning("[ultrasound] structure API failed: %s", e)
        return {"ok": False, "groups": [], "error": f"调用模型失败：{e}"}

    raw = _strip_md(resp.choices[0].message.content or "")
    try:
        data = json.loads(raw)
    except Exception as e:
        logger.warning("[ultrasound] structure JSON parse failed: %s; raw=%s", e, raw[:300])
        return {"ok": False, "groups": [], "error": f"模型输出不是有效 JSON：{e}", "raw": raw}

    groups_in = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups_in, list):
        return {"ok": False, "groups": [], "error": "模型输出缺少 groups 数组"}

    groups = []
    for g in groups_in:
        if not isinstance(g, dict):
            continue
        rows = []
        for r in (g.get("rows") or []):
            if not isinstance(r, dict):
                continue
            name = str(r.get("name", "")).strip()
            if not name:
                continue
            rows.append({
                "name": name[:80],
                "value": str(r.get("value", "")).strip()[:40],
                "unit": str(r.get("unit", "")).strip()[:20],
            })
        if rows:
            groups.append({"group": str(g.get("group", "")).strip()[:120], "rows": rows})
    return {"ok": True, "groups": groups}


_DRAFT_SYSTEM = """你是宠物医院资深超声科医生。
任务：根据宠物信息、超声测量数据、以及医生的主观描述，撰写三段中文超声报告文字稿，分别填入「超声所见」「超声提示(结论)」「建议」。

输出严格的 JSON 对象（不要 markdown 包裹）：{"findings":"…","conclusion":"…","advice":"…"}

要求：
1. 「超声所见」(findings)：按解剖结构分条/分段客观描述。心超侧重各腔室大小、室壁厚度与运动、瓣膜形态与血流、收缩功能(EF/FS)；腹部侧重各脏器大小/回声/结构。结合测量值是否偏离该品种/体型常见范围给出客观判断，但用描述性语言，不要硬下"正常/异常"标签词堆砌。
2. 「超声提示(结论)」(conclusion)：1-4 句，给出超声层面的判读/倾向性结论（如「左房轻度增大，二尖瓣轻度返流」「未见明显异常」）。
3. 「建议」(advice)：2-4 条，临床随访/进一步检查/复查节奏（如心脏病分级随访、复查窗口、结合症状与其他检查）。
4. 必须把医生的主观描述（肿瘤/囊肿/积液/占位等机器测不出的发现）自然地融合进「超声所见」与「结论」，不可忽略。
5. 忠于给到的数值与描述，不要编造未提供的结构或数值；测量缺失的部分不强行描述。
6. 中性专业语气，宠物医学常用表述；不要写"以上为示例""根据您提供的"这类话。
7. 末尾不需要免责声明（报告模板里已有）。
"""


def _format_draft_payload(payload: dict) -> str:
    species_map = {"dog": "犬", "cat": "猫"}
    sp = species_map.get(payload.get("pet_species") or "", payload.get("pet_species") or "—")
    lines = [
        f"【检查类型】{_EXAM_TYPE_LABEL.get(payload.get('exam_type') or 'cardiac', '通用超声')}",
        f"【归属检查项】{payload.get('item_label') or '—'}",
        f"【动物种属】{sp}",
        f"【品种】{payload.get('pet_breed') or '—'}",
        f"【性别】{payload.get('pet_sex') or '—'}",
        f"【年龄】{payload.get('pet_age') or '—'}",
        f"【体重】{payload.get('pet_weight') or '—'}",
        f"【设备】{payload.get('device') or '—'}",
        "",
        "【超声测量数据】",
    ]
    groups = payload.get("groups") or []
    if groups:
        for g in groups:
            lines.append(f"〔{g.get('group') or '测量'}〕")
            for r in (g.get("rows") or []):
                nm = (r.get("name") or "").strip()
                if not nm:
                    continue
                val = (r.get("value") or "").strip()
                unit = (r.get("unit") or "").strip()
                lines.append(f"  · {nm}：{val}{(' ' + unit) if unit else ''}")
    else:
        lines.append("  （无结构化测量数据）")

    vf = (payload.get("vet_findings") or "").strip()
    lines.append("")
    lines.append("【医生主观描述 / 重点发现】")
    lines.append(vf if vf else "（医生未填写额外描述）")
    return "\n".join(lines)


async def draft_ultrasound_text(payload: dict) -> dict[str, Any]:
    """生成超声所见/结论/建议三段。返回 {ok, findings, conclusion, advice, error?}"""
    if not settings.openai_api_key:
        return {"ok": False, "error": "未配置 OPENAI_API_KEY"}
    try:
        from openai import AsyncOpenAI  # noqa: F401
    except ImportError:
        return {"ok": False, "error": "缺少 openai 库"}

    user_text = _format_draft_payload(payload)
    try:
        resp = await _client().chat.completions.create(
            model=_model(),
            messages=[{"role": "system", "content": _DRAFT_SYSTEM},
                      {"role": "user", "content": user_text}],
            temperature=0.4,
            max_tokens=1400,
        )
    except Exception as e:
        logger.warning("[ultrasound] draft API failed: %s", e)
        return {"ok": False, "error": f"调用模型失败：{e}"}

    raw = _strip_md(resp.choices[0].message.content or "")
    try:
        data = json.loads(raw)
    except Exception as e:
        logger.warning("[ultrasound] draft JSON parse failed: %s; raw=%s", e, raw[:300])
        return {"ok": False, "error": f"模型输出不是有效 JSON：{e}", "raw": raw}
    if not isinstance(data, dict):
        return {"ok": False, "error": "模型输出不是 JSON 对象"}
    return {
        "ok": True,
        "findings": str(data.get("findings", "")).strip(),
        "conclusion": str(data.get("conclusion", "")).strip(),
        "advice": str(data.get("advice", "")).strip(),
    }
