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

import asyncio
import json
import logging
import re
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


_LOCAL_SECTION_RE = re.compile(r"(?:2D|M|Doppler|PW|CW|组织多普勒|频谱|血流|测量).{0,16}(?:测量|参数)?", re.I)
_LOCAL_VALUE_RE = re.compile(
    r"(?P<name>[A-Za-z][A-Za-z0-9_()'./+\- ]{0,38}|[\u4e00-\u9fff][^:\n]{0,28}?)"
    r"\s*[:：]\s*"
    r"(?P<value>-?\d+(?:\.\d+)?(?:\s*/\s*-?\d+(?:\.\d+)?)?)"
    r"\s*(?P<unit>cm/s|m/s|mmHg|mm/s|cm|mm|mL|ml|uL|μL|kg|g|bpm|ms|s|Hz|%)?",
    re.I,
)
_LOCAL_METADATA_NAMES = (
    "日期", "时间", "姓名", "病历", "动物", "年龄", "性别", "电话", "医院",
    "检查号", "报告号", "page", "patient", "owner", "doctor", "operator",
)


def parse_measurements_locally(raw_text: str) -> dict[str, Any]:
    """Parse common machine-exported ``name:value unit`` measurements.

    This is intentionally schema-free: different cardiac/abdominal machines
    can expose different fields.  It is a deterministic fallback for the
    frequent case where the text model returns an empty or invalid response.
    """
    text = (raw_text or "").replace("\r", "\n")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.split("\n")]
    device = ""
    for line in lines:
        match = re.search(r"(?:产品型号|设备型号|机型|Model|Device)\s*[:：]\s*([^\s,，;；]{2,80})", line, re.I)
        if match:
            device = match.group(1).strip()[:120]
            break

    groups: list[dict[str, Any]] = []
    by_name: dict[str, dict[str, Any]] = {}
    section = "测量数据"
    def target(name: str) -> dict[str, Any]:
        if name not in by_name:
            entry = {"group": name[:120], "rows": []}
            by_name[name] = entry
            groups.append(entry)
        return by_name[name]

    for line in lines:
        if not line:
            continue
        if _LOCAL_SECTION_RE.fullmatch(line) or (
            _LOCAL_SECTION_RE.search(line) and ":" not in line and "：" not in line and not re.search(r"\d", line)
        ):
            section = line[:120]
            continue
        matches = list(_LOCAL_VALUE_RE.finditer(line))
        if not matches:
            continue
        accepted = []
        for match in matches:
            name = re.sub(r"\s+", " ", match.group("name")).strip(" -·")
            if not name or any(word.lower() in name.lower() for word in _LOCAL_METADATA_NAMES):
                continue
            value = re.sub(r"\s+", "", match.group("value"))
            unit = (match.group("unit") or "").strip()
            accepted.append({"name": name[:80], "value": value[:40], "unit": unit[:20]})
        if accepted:
            target(section)["rows"].extend(accepted)

    # De-duplicate repeated page headers/measurement blocks while preserving order.
    for group in groups:
        seen = set()
        rows = []
        for row in group["rows"]:
            key = (row["name"].lower(), row["value"], row["unit"].lower())
            if key not in seen:
                seen.add(key)
                rows.append(row)
        group["rows"] = rows
    groups = [g for g in groups if g["rows"]]
    return {"ok": bool(groups), "groups": groups, "device": device, "source": "local"}


_STRUCT_SYSTEM = """你是宠物医院超声测量数据整理助手。
任务：把超声机导出的测量数据文本，整理成「分组」结构的 JSON，并顺带识别设备/探头信息。测量项目不固定，原文有多少就整理多少，不要增删、不要编造数值。

输出严格的 JSON 对象（不要 markdown 包裹），结构：
{"device": "设备型号 / 探头信息（从原文里找，如 机型名、Probe、探头频率等；没有就空串）",
 "groups": [
  {"group": "分组名（如 2D测量·主动脉与主动脉瓣 / M测量·左室 / Doppler·二尖瓣；原文有层级就拼起来）",
   "rows": [{"name":"指标名(如 LA Diam)", "value":"数值(如 0.96，纯数字或比值，去掉单位)", "unit":"单位(如 cm/cm/s/mmHg/%，没有就空串)"}]}
]}

要求：
1. 严格忠于原文数值，一个不漏一个不加；识别不出的字段跳过，不要瞎填。
2. 把同一大类(2D测量/M测量/Doppler测量等)下的小标题(如 左室/二尖瓣/主动脉)拼进 group 名，用「·」连接。
3. name 用原文指标名（中英文都保留原样，如 "LVIDd"、"MV E/A"、"EF(Teich)"）。
4. value 只放数值/比值本身，把单位拆到 unit。比值类(如 LA/Ao:1.07)的 unit 留空。
5. 不要把页眉页脚、宠物信息、"Page"、医院名当成测量项。
6. device 只填设备/探头相关信息（机型、探头型号、探头频率等），不确定就留空串，不要把医院名/客户名/日期当设备。
7. 若完全没有可识别的测量数据，groups 返回 []（device 仍按原文识别）。
"""


async def structure_measurements(raw_text: str, exam_type: str = "cardiac") -> dict[str, Any]:
    """杂乱 PDF 文字 → 动态分组测量 JSON。返回 {ok, groups, error?}"""
    local = parse_measurements_locally(raw_text)
    if local.get("ok"):
        return local

    from app.services.report_llm import (
        generate_json_object,
        report_llm_configured,
        report_text_client_model,
    )
    if not report_llm_configured():
        return {"ok": False, "groups": [], "error": "未配置文字生成模型（DEEPSEEK_API_KEY / OPENAI_API_KEY）"}
    if not (raw_text or "").strip():
        return {"ok": False, "groups": [], "error": "PDF 未提取到文字（可能是扫描件 / 图片型 PDF）"}
    try:
        from openai import AsyncOpenAI  # noqa: F401
    except ImportError:
        return {"ok": False, "groups": [], "error": "缺少 openai 库"}

    user = f"【检查类型】{_EXAM_TYPE_LABEL.get(exam_type, '通用超声')}\n\n【机器导出测量文本】\n{raw_text[:24000]}"
    client, model, _, is_reasoner = report_text_client_model()
    data, raw, error = await generate_json_object(
        client=client,
        model=model,
        messages=[{"role": "system", "content": _STRUCT_SYSTEM},
                  {"role": "user", "content": user}],
        temperature=0.0,
        max_tokens=8000 if is_reasoner else 3000,
        task="ultrasound_measurement_structure",
    )
    if data is None:
        return {"ok": False, "groups": [], "error": error, "raw": raw}

    groups_in = data.get("groups") if isinstance(data, dict) else None
    if not isinstance(groups_in, list):
        return {"ok": False, "groups": [], "error": "模型输出缺少 groups 数组"}

    device = str(data.get("device", "")).strip()[:120] if isinstance(data, dict) else ""
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
    return {"ok": True, "groups": groups, "device": device}


_DRAFT_SYSTEM = """你是宠物医院资深超声科医生，正在书写正式超声报告。
任务：根据宠物信息、超声测量数据、医生主观描述，撰写三段中文报告，填入「超声所见」「超声提示(结论)」「建议」。

输出严格的 JSON 对象（不要 markdown 包裹）：{"findings":"…","conclusion":"…","advice":"…"}

★最重要的原则：「超声所见」是【判读】不是【复述】★
- 绝对不要把测量值逐条翻译成中文再抄一遍（错误示范：「左心房直径为0.86cm，主动脉直径为0.77cm，比值为1.12」这种纯罗列是不合格的）。
- 正确做法：综合相关指标，给出该结构的医学结论，必要时把关键数值作为佐证放进括号。要回答「这些数值说明了什么」。
  示范（心超）：
   · 左房 / LA/Ao → 判断左心房是否扩张：「左心房内径正常，LA/Ao 1.12，未见左房扩大」
   · IVSd / LVPWd → 判断室壁厚度：「室间隔及左室后壁舒张期厚度正常，未见向心性肥厚」
   · LVIDd / LVIDs → 判断左室腔大小：「左室内径正常，未见扩张」
   · EF / FS → 判断收缩功能：「左室收缩功能良好（EF 91.7%，FS 59.8%）」
   · MV E/A、TV E/A → 判断舒张功能 / 瓣膜血流：「二尖瓣血流 E/A 比值正常，未见明显反流」
   · 各瓣膜流速 / 压差异常升高 → 提示狭窄或反流，需点明。
- 腹部 / 泌尿同理：对各脏器大小、回声、结构、有无占位/结石/积液做判读，而不是复述脏器尺寸数字。

写作要求：
1. 「超声所见」(findings)：按解剖结构分段（心超：左心房与主动脉 / 左心室壁与腔径 / 收缩与舒张功能 / 各瓣膜）。每段给出明确判读（正常 / 偏大 / 增厚 / 功能降低 等），关键支撑数值放括号内即可，不要把所有数值都搬出来。语言是兽医同行看的专业报告语气。
2. 「超声提示(结论)」(conclusion)：1-4 句凝练结论（如「心脏各腔室大小及室壁厚度未见异常，瓣膜功能良好，左室收缩功能正常」或「左房轻度增大，二尖瓣轻度反流，提示早期二尖瓣退行性变」）。
3. 「建议」(advice)：2-4 条，结合判读给出随访/复查/进一步检查建议；正常者也给常规建议。
4. 必须把医生主观描述（肿瘤/囊肿/积液/占位等机器测不出的发现）自然融合进所见与结论，不可忽略。
5. 只依据给到的数值与描述判读，缺失的结构不强行描述，不编造未提供的数据。判断是否偏离正常时，结合宠物的种属/品种/体重所对应的常见参考范围（按你的临床经验，无需写出具体参考区间）。
6. 中性专业语气；不要写"以上为示例""根据您提供的"这类话；末尾不要免责声明。
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
    if not (payload.get("groups") or []) and not (payload.get("vet_findings") or "").strip():
        return {
            "ok": False,
            "error": "缺少可用测量数据和医生主观描述，已停止生成，避免产生误导性正文",
        }
    from app.services.report_llm import report_llm_configured, report_text_client_model
    if not report_llm_configured():
        return {"ok": False, "error": "未配置文字生成模型（DEEPSEEK_API_KEY / OPENAI_API_KEY）"}
    try:
        from openai import AsyncOpenAI  # noqa: F401
    except ImportError:
        return {"ok": False, "error": "缺少 openai 库"}

    user_text = _format_draft_payload(payload)
    client, model, _, is_reasoner = report_text_client_model()
    last_error = ""
    for attempt in range(2):
        retry_instruction = ""
        if attempt:
            retry_instruction = (
                "\n\n上一次输出可能被截断或不是有效 JSON。请重新完整输出，且只输出 JSON；"
                "findings 控制在 700 个汉字以内，conclusion 在 220 个汉字以内，"
                "advice 在 300 个汉字以内。"
            )
        try:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": _DRAFT_SYSTEM + retry_instruction},
                    {"role": "user", "content": user_text},
                ],
                temperature=0.3,
                max_tokens=10000 if is_reasoner else 2200,
            )
        except Exception as exc:
            last_error = f"调用模型失败：{exc}"
            logger.warning(
                "[ultrasound] draft API failed (attempt %s/2): %s",
                attempt + 1,
                exc,
            )
            if attempt == 0:
                await asyncio.sleep(1.0)
                continue
            break

        raw = _strip_md(resp.choices[0].message.content or "")
        try:
            data = json.loads(raw)
        except Exception as exc:
            last_error = "模型返回内容不完整，系统已自动重试但仍未成功"
            logger.warning(
                "[ultrasound] draft JSON parse failed (attempt %s/2): %s; raw=%s",
                attempt + 1,
                exc,
                raw[:500],
            )
            if attempt == 0:
                await asyncio.sleep(0.5)
                continue
            break

        if not isinstance(data, dict):
            last_error = "模型输出不是 JSON 对象"
        else:
            result = {
                "ok": True,
                "findings": str(data.get("findings", "")).strip(),
                "conclusion": str(data.get("conclusion", "")).strip(),
                "advice": str(data.get("advice", "")).strip(),
            }
            if result["findings"] and result["conclusion"]:
                return result
            last_error = "模型返回的超声所见或结论为空"

        if attempt == 0:
            await asyncio.sleep(0.5)

    return {"ok": False, "error": last_error or "AI文字稿生成失败，请稍后重试"}
