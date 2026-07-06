"""
X 光 / 放射报告 AI 文字稿生成。

核心原则：**AI 不读片、不下影像诊断**，只把医生已经勾选好的结构化所见 +
测量值 + 主观描述，整理润色成「X线所见 / 提示 / 建议」三段中文报告。

输入 payload：
  region / region_label / projection / image_quality / item_label
  pet_species / pet_breed / pet_sex / pet_age / pet_weight
  findings: [{structure, items:[{tag, desc}], note}]   # 已带标签解释
  measurements: [{name, value, unit}]
  vet_findings: 医生综合主观描述

输出：{ok, findings, conclusion, advice, error?}
"""
from __future__ import annotations
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = """你是宠物医院资深放射科医生，正在根据【医生本人已经判读好的结构化结果】撰写正式 X 光报告。
重要前提：影像判读已由医生完成，你的任务是【整理与润色成规范中文报告】，不是你自己去诊断或推翻医生的判读。

输出严格的 JSON 对象（不要 markdown 包裹）：{"findings":"…","conclusion":"…","advice":"…"}

要求：
1. 「X线所见」(findings)：按解剖结构分条/分段，把医生勾选的征象写成规范放射学描述。
   - 用提供的「标签解释」把征象写得专业准确（如勾了"肺泡型"，就描述为肺泡型浸润、可见支气管充气征等）。
   - 勾了"正常"的结构，简洁写"未见明显异常"，不要堆砌。
   - 医生在某结构写了备注（note），要结合进该结构的描述（如部位、范围）。
   - 有测量值（VHS、Norberg角等）要写进相应结构。
2. 「提示(结论)」(conclusion)：1-4 句，给出影像层面的总体判读/倾向（如"符合心源性肺水肿表现""左侧第6肋骨折，胸腔少量积液"）。忠于医生勾选，不夸大、不新增医生没勾的诊断。
3. 「建议」(advice)：2-4 条，结合所见给随访/进一步检查/治疗方向建议（如复查窗口、结合超声/血检、必要时 CT 或转诊）。
4. 必须融合医生的「综合主观描述」(vet_findings)，不可忽略。
5. **只依据医生提供的勾选与描述**，不要凭空增加结构或征象；医生没提的部位不要编。
6. 若图像质量标注为非"满意"（体位欠正/曝光不足等），在建议里温和提示"图像质量可能影响判读，必要时复拍"。
7. 中性专业语气、宠物放射学常用表述；不要写"以上为示例""根据您提供的"这类话；末尾不要免责声明（模板已有）。
8. 若为【髋关节早筛】：结合 DI（分离指数）、Norberg 角、FCI 分级判读双侧髋关节松弛度与发育不良风险。
   - DI 经验阈值：<0.3 通常低风险，≥0.3 风险随值升高（**阈值因品种而异，须注明仅供参考**）。
   - 左右髋分别评估、以较差侧为准；结论给出风险等级。
   - 建议里给复查窗口与（若用于种用犬）是否适合繁育的方向性提示，但强调最终以医师 + 品种参考为准。
"""


_REGION_LABEL = {
    "thorax": "胸部", "thoracoabdomen": "胸腹部", "abdomen": "腹部", "head": "头部",
    "msk": "肌骨/四肢", "joint": "关节",
    "spine": "脊椎（颈/胸/腰）", "hip_screen": "髋关节早筛",
}


def _format_payload(payload: dict) -> str:
    species_map = {"dog": "犬", "cat": "猫"}
    sp = species_map.get(payload.get("pet_species") or "", payload.get("pet_species") or "—")
    lines = [
        f"【检查部位】{payload.get('region_label') or _REGION_LABEL.get(payload.get('region') or '', '—')}",
        f"【归属检查项】{payload.get('item_label') or '—'}",
        f"【投照体位】{payload.get('projection') or '—'}",
        f"【图像质量】{payload.get('image_quality') or '—'}",
        f"【动物】{sp} · {payload.get('pet_breed') or '—'} · {payload.get('pet_sex') or '—'} · {payload.get('pet_age') or '—'} · {payload.get('pet_weight') or '—'}",
        "",
        "【医生勾选的结构化所见】",
    ]
    findings = payload.get("findings") or []
    if findings:
        for f in findings:
            sname = (f.get("structure") or "").strip()
            items = f.get("items") or []
            tag_strs = []
            for it in items:
                tag = (it.get("tag") or "").strip()
                desc = (it.get("desc") or "").strip()
                tag_strs.append(f"{tag}（{desc}）" if desc else tag)
            note = (f.get("note") or "").strip()
            line = f"  · {sname}：{ '、'.join(tag_strs) if tag_strs else '—' }"
            if note:
                line += f"；备注：{note}"
            lines.append(line)
    else:
        lines.append("  （医生未勾选结构化项目）")

    measurements = payload.get("measurements") or []
    meas = [m for m in measurements if (m.get("name") or "").strip() and (str(m.get("value") or "")).strip()]
    if meas:
        lines.append("")
        lines.append("【测量值】")
        for m in meas:
            unit = (m.get("unit") or "").strip()
            lines.append(f"  · {m.get('name')}：{m.get('value')}{(' ' + unit) if unit else ''}")

    vf = (payload.get("vet_findings") or "").strip()
    lines.append("")
    lines.append("【医生综合主观描述】")
    lines.append(vf if vf else "（医生未填写额外描述）")
    return "\n".join(lines)


async def draft_xray_text(payload: dict) -> dict[str, Any]:
    from app.services.report_llm import report_llm_configured, report_text_client_model
    if not report_llm_configured():
        return {"ok": False, "error": "未配置文字生成模型（DEEPSEEK_API_KEY / OPENAI_API_KEY）"}
    try:
        from openai import AsyncOpenAI  # noqa: F401
    except ImportError:
        return {"ok": False, "error": "缺少 openai 库"}

    user_text = _format_payload(payload)
    client, model, _, is_reasoner = report_text_client_model()
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": _SYSTEM_PROMPT},
                      {"role": "user", "content": user_text}],
            temperature=0.4,
            max_tokens=8000 if is_reasoner else 1400,
        )
    except Exception as e:
        logger.warning("[xray_ai] API failed: %s", e)
        return {"ok": False, "error": f"调用模型失败：{e}"}

    raw = (resp.choices[0].message.content or "").strip()
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
        logger.warning("[xray_ai] JSON parse failed: %s; raw=%s", e, raw[:300])
        return {"ok": False, "error": f"模型输出不是有效 JSON：{e}", "raw": raw}
    if not isinstance(data, dict):
        return {"ok": False, "error": "模型输出不是 JSON 对象"}
    return {
        "ok": True,
        "findings": str(data.get("findings", "")).strip(),
        "conclusion": str(data.get("conclusion", "")).strip(),
        "advice": str(data.get("advice", "")).strip(),
    }
