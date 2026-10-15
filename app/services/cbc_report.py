"""BC-60R 血常规固定版面识别、交叉校验与医生向 AI 分析。"""
from __future__ import annotations

import base64
import json
import logging
import math
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


# 顺序与 BC-60R 屏幕一致。None 表示该物种无参考范围。
CBC_REFERENCE: dict[str, dict[str, tuple[float, float] | None]] = {
    "dog": {
        "WBC": (5.32, 16.92), "Neu#": (3.05, 12.10), "Lym#": (0.70, 4.95),
        "Mon#": (0.20, 1.38), "Eos#": (0.04, 1.28), "Bas#": (0.0, 0.13),
        "Neu%": (42.0, 84.0), "Lym%": (6.0, 40.0), "Mon%": (2.5, 12.0),
        "Eos%": (0.3, 11.0), "Bas%": (0.0, 1.0), "RBC": (5.20, 8.69),
        "HGB": (115, 201), "HCT": (35.0, 60.0), "MCV": (60.0, 77.5),
        "MCH": (20.0, 27.0), "MCHC": (300, 380), "RDW-CV": (11.3, 18.9),
        "RDW-SD": (29.1, 55.1), "PLT": (140, 520), "MPV": (7.6, 16.1),
        "PDW": (13.8, 17.9), "PCT": (0.120, 0.700), "P-LCC": (25, 180),
        "P-LCR": (10.0, 57.0), "IPF": (0.4, 17.1), "RET#": (9.0, 115.0),
        "RET%": (0.16, 1.95), "IRF": (0.0, 45.1), "LFR": (56.0, 100.0),
        "MFR": (0.0, 26.0), "HFR": (0.0, 22.0), "RHE": (20.0, 28.3),
    },
    "cat": {
        "WBC": (3.46, 17.50), "Neu#": (1.95, 11.50), "Lym#": (0.73, 7.40),
        "Mon#": (0.06, 0.98), "Eos#": (0.04, 1.48), "Bas#": (0.0, 0.25),
        "Neu%": (30.0, 83.5), "Lym%": (7.0, 60.0), "Mon%": (0.8, 8.0),
        "Eos%": (0.5, 11.5), "Bas%": (0.0, 2.3), "RBC": (6.30, 11.82),
        "HGB": (90, 160), "HCT": (26.0, 50.2), "MCV": (34.0, 55.0),
        "MCH": (11.0, 18.0), "MCHC": (285, 384), "RDW-CV": (14.2, 26.6),
        "RDW-SD": (22.0, 39.6), "PLT": (140, 595), "MPV": (8.6, 18.4),
        "PDW": (12.0, 17.5), "PCT": (0.150, 0.900), "P-LCC": None,
        "P-LCR": None, "IPF": (0.7, 28.0), "RET#": (4.0, 52.0),
        "RET%": (0.05, 0.90), "IRF": (0.0, 33.0), "LFR": (66.0, 100.0),
        "MFR": (0.0, 25.8), "HFR": (0.0, 8.5), "RHE": (14.2, 21.5),
    },
}

CBC_UNITS = {
    "WBC": "10⁹/L", "Neu#": "10⁹/L", "Lym#": "10⁹/L", "Mon#": "10⁹/L",
    "Eos#": "10⁹/L", "Bas#": "10⁹/L", "Neu%": "%", "Lym%": "%",
    "Mon%": "%", "Eos%": "%", "Bas%": "%", "RBC": "10¹²/L", "HGB": "g/L",
    "HCT": "%", "MCV": "fL", "MCH": "pg", "MCHC": "g/L", "RDW-CV": "%",
    "RDW-SD": "fL", "PLT": "10⁹/L", "MPV": "fL", "PDW": "", "PCT": "%",
    "P-LCC": "10⁹/L", "P-LCR": "%", "IPF": "%", "RET#": "10⁹/L", "RET%": "%",
    "IRF": "%", "LFR": "%", "MFR": "%", "HFR": "%", "RHE": "pg",
}

CBC_CODES = list(CBC_REFERENCE["cat"].keys())


def _json_from_text(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                value = json.loads(text[start:end + 1])
                return value if isinstance(value, dict) else None
            except Exception:
                return None
    return None


def _image_data(path: Path) -> tuple[str, str]:
    mime = {".png": "image/png", ".webp": "image/webp"}.get(path.suffix.lower(), "image/jpeg")
    return mime, base64.b64encode(path.read_bytes()).decode("ascii")


_OCR_PROMPT = """你是迈瑞 BC-60R 动物血常规固定结果屏幕的精确录入程序。
只读取屏幕左上样本资料和左右两列结果表，不解释散点图，不做诊断，不猜测看不清的字符。
输出一个 JSON 对象，不要 Markdown：
{
  "quality_ok": true,
  "quality_issues": [],
  "sample_no": "",
  "tested_at": "YYYY-MM-DD HH:MM",
  "screen_species": "cat或dog或unknown",
  "animal_name": "",
  "age": "",
  "results": [
    {"code":"WBC", "value":4.87, "instrument_flag":"", "confidence":0.99, "raw_text":"4.87"}
  ]
}

必须遵守：
1. results 只允许以下项目并按此顺序输出：
WBC, Neu#, Lym#, Mon#, Eos#, Bas#, Neu%, Lym%, Mon%, Eos%, Bas%, PLT, MPV, PDW, PCT, P-LCC, P-LCR, IPF, RBC, HGB, HCT, MCV, MCH, MCHC, RDW-CV, RDW-SD, RET#, RET%, IRF, LFR, MFR, HFR, RHE。
2. 屏幕没有数值时 value 必须为 null；真实的 0.0 必须输出 0.0，不能当空值。
3. instrument_flag 只允许 H、L 或空串。只有明确位于该行异常标记栏里的字符才记录；边框、竖线、摩尔纹不能当 L。
4. 数字必须忠于图片，不用参考范围反推或修正；不确定时降低 confidence，不要猜。
5. quality_ok=false 的情形：结果区被反光遮挡、明显失焦、屏幕未拍全或严重倾斜导致多项无法读取。quality_issues 写具体原因。
6. 这是固定 BC-60R 界面。不要读取底部“下一样本”数字，也不要读取图表坐标刻度。
"""


async def recognize_cbc_photo(path: Path, expected_species: str = "") -> dict[str, Any]:
    if not (settings.openai_api_key or "").strip():
        return {"ok": False, "error": "未配置 OPENAI_API_KEY"}
    try:
        from openai import AsyncOpenAI
    except ImportError:
        return {"ok": False, "error": "缺少 openai 库"}
    mime, b64 = _image_data(path)
    base = (settings.openai_base_url or "").strip() or None
    # 明确设置超时，避免供应商或网络异常时报告永久停在“识别中”。
    client = AsyncOpenAI(api_key=settings.openai_api_key, base_url=base, timeout=90.0, max_retries=0)
    # CBC 可单独指定模型；未指定时优先复用当前仍在使用的 TNR 视觉模型，
    # 最后才退回通用 OPENAI_MODEL（旧端点可能已停用）。
    models = list(dict.fromkeys(m for m in (
        (getattr(settings, "cbc_vision_model", "") or "").strip(),
        (getattr(settings, "purchase_vision_model", "") or "").strip(),
        (getattr(settings, "tnr_vision_model", "") or "").strip(),
        (getattr(settings, "tnr_vision_fallback_model", "") or "").strip(),
        (getattr(settings, "openai_model", "") or "").strip(),
        "gpt-4o-mini",
    ) if m))
    content = [
        {"type": "text", "text": _OCR_PROMPT + f"\n宠物档案预期物种：{expected_species or '未知'}。仍需如实读取屏幕物种。"},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
    ]
    response = None
    used_model = ""
    errors: list[str] = []
    for model in models:
        try:
            response = await client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": content}],
                temperature=0.0, max_tokens=3000,
            )
            used_model = model
            break
        except Exception as exc:
            errors.append(f"{model}: {str(exc)[:300]}")
            logger.warning("[cbc] vision model %s failed; trying fallback: %s", model, str(exc)[:300])
    if response is None:
        return {"ok": False, "error": "视觉识别失败：" + " | ".join(errors)}
    raw = (response.choices[0].message.content or "").strip()
    data = _json_from_text(raw)
    if not data:
        return {"ok": False, "error": "视觉模型未返回有效 JSON", "raw": raw[:1000]}
    return {"ok": True, "data": data, "model": used_model, "raw": raw}


def normalize_results(raw_results: list[dict[str, Any]], species: str) -> list[dict[str, Any]]:
    species = species if species in CBC_REFERENCE else "cat"
    by_code = {}
    aliases = {"BAS#": "Bas#", "BAS%": "Bas%", "NEU#": "Neu#", "LYM#": "Lym#",
               "MON#": "Mon#", "EOS#": "Eos#", "NEU%": "Neu%", "LYM%": "Lym%",
               "MON%": "Mon%", "EOS%": "Eos%"}
    for item in raw_results or []:
        if not isinstance(item, dict):
            continue
        raw_code = str(item.get("code", "")).strip()
        code = aliases.get(raw_code.upper(), raw_code)
        if code not in CBC_CODES:
            continue
        value = item.get("value")
        try:
            value = float(value) if value is not None and str(value).strip() != "" else None
            if value is not None and not math.isfinite(value):
                value = None
        except Exception:
            value = None
        flag = str(item.get("instrument_flag", "")).strip().upper()
        if flag not in ("H", "L"):
            flag = ""
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.0))))
        except Exception:
            confidence = 0.0
        by_code[code] = {"code": code, "value": value, "instrument_flag": flag,
                         "confidence": confidence, "raw_text": str(item.get("raw_text", ""))[:40]}
    out = []
    for code in CBC_CODES:
        row = by_code.get(code, {"code": code, "value": None, "instrument_flag": "", "confidence": 0.0, "raw_text": ""})
        ref = CBC_REFERENCE[species].get(code)
        row["unit"] = CBC_UNITS[code]
        row["range_low"] = ref[0] if ref else None
        row["range_high"] = ref[1] if ref else None
        if row["value"] is None or ref is None:
            row["range_status"] = "na"
        elif row["value"] < ref[0]:
            row["range_status"] = "low"
        elif row["value"] > ref[1]:
            row["range_status"] = "high"
        else:
            row["range_status"] = "normal"
        out.append(row)
    return out


def validate_results(results: list[dict[str, Any]], *, expected_species: str = "", screen_species: str = "",
                     quality_ok: bool = True, quality_issues: list[str] | None = None) -> dict[str, Any]:
    values = {r["code"]: r.get("value") for r in results}
    review_codes = {r["code"] for r in results if r.get("value") is not None and float(r.get("confidence") or 0) < 0.85}
    missing = [r["code"] for r in results if r.get("value") is None and CBC_REFERENCE.get(expected_species or "cat", {}).get(r["code"]) is not None]
    checks = []

    # 仪器明确给出 H/L、但按本系统 BC-60R 物种参考范围判断相反时，
    # 不自动替医生选边，直接把该项送入复核。
    for row in results:
        flag = row.get("instrument_flag") or ""
        range_status = row.get("range_status") or "na"
        if flag == "H" and range_status not in ("high", "na"):
            review_codes.add(row["code"])
        elif flag == "L" and range_status not in ("low", "na"):
            review_codes.add(row["code"])

    def check(name: str, target_code: str, actual: float | None, expected: float | None, rel_tol: float = 0.08, abs_tol: float = 0.08):
        if actual is None or expected is None:
            return
        tolerance = max(abs_tol, abs(expected) * rel_tol)
        ok = abs(actual - expected) <= tolerance
        checks.append({"name": name, "code": target_code, "actual": actual, "expected": round(expected, 4), "ok": ok})
        if not ok:
            review_codes.add(target_code)

    wbc = values.get("WBC")
    if wbc is not None:
        for abs_code, pct_code in (("Neu#", "Neu%"), ("Lym#", "Lym%"), ("Mon#", "Mon%"), ("Eos#", "Eos%"), ("Bas#", "Bas%")):
            pct = values.get(pct_code)
            check(f"{abs_code}=WBC×{pct_code}", abs_code, values.get(abs_code), wbc * pct / 100 if pct is not None else None, 0.12, 0.08)
    rbc, hgb, hct = values.get("RBC"), values.get("HGB"), values.get("HCT")
    check("HCT=RBC×MCV÷10", "HCT", hct, rbc * values["MCV"] / 10 if rbc is not None and values.get("MCV") is not None else None, 0.04, 0.5)
    check("MCH=HGB÷RBC", "MCH", values.get("MCH"), hgb / rbc if hgb is not None and rbc else None, 0.04, 0.3)
    check("MCHC=HGB÷HCT×100", "MCHC", values.get("MCHC"), hgb / hct * 100 if hgb is not None and hct else None, 0.04, 5.0)
    check("PCT=PLT×MPV÷10000", "PCT", values.get("PCT"), values["PLT"] * values["MPV"] / 10000 if values.get("PLT") is not None and values.get("MPV") is not None else None, 0.08, 0.015)
    check("RET#=RBC×RET%×10", "RET#", values.get("RET#"), rbc * values["RET%"] * 10 if rbc is not None and values.get("RET%") is not None else None, 0.12, 1.0)

    species_conflict = bool(expected_species in ("cat", "dog") and screen_species in ("cat", "dog") and expected_species != screen_species)
    if species_conflict:
        review_codes.add("species")
    issues = list(quality_issues or [])
    if not quality_ok and not issues:
        issues.append("图片质量不足")
    return {
        "auto_pass": bool(quality_ok and not species_conflict and not review_codes and not missing),
        "quality_ok": bool(quality_ok), "quality_issues": issues,
        "species_conflict": species_conflict, "missing_codes": missing,
        "review_codes": sorted(review_codes), "checks": checks,
    }


_ANALYSIS_SYSTEM = """你是宠物医院血常规报告的医生辅助分析助手。输出给兽医审核，也会进入宠主最终 PDF。
必须区分检测事实、病历信息和可能解释。不得把初步诊断改写成确诊，不得虚构病史或体征。
优先解读绝对计数及相关指标组合，必要时提示采样、脱水、应激、凝集等影响；给出需要结合或复查的方向。
语言专业、克制、简洁。避免“就是、一定、确诊”等确定措辞。
只输出 JSON：{"analysis":"2至5段专业辅助分析","doctor_conclusion":"供医生修改的简短结论与建议草稿"}
"""


async def draft_cbc_analysis(payload: dict[str, Any]) -> dict[str, Any]:
    from app.services.report_llm import generate_json_object, report_llm_configured, report_text_client_model
    if not report_llm_configured():
        return {"ok": False, "error": "未配置报告文字模型"}
    client, model, _, is_reasoner = report_text_client_model()
    user = "请根据以下结构化资料生成血常规辅助分析。\n" + json.dumps(payload, ensure_ascii=False, indent=2)[:30000]
    data, raw, error = await generate_json_object(
        client=client, model=model,
        messages=[{"role": "system", "content": _ANALYSIS_SYSTEM}, {"role": "user", "content": user}],
        temperature=0.2, max_tokens=5000 if is_reasoner else 2200, task="cbc_report",
    )
    if not data:
        return {"ok": False, "error": error, "raw": raw}
    return {"ok": True, "analysis": str(data.get("analysis", "")).strip(),
            "doctor_conclusion": str(data.get("doctor_conclusion", "")).strip(), "model": model}
