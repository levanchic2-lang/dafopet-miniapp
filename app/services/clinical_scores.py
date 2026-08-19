from __future__ import annotations

import json
from typing import Any


SCALES: dict[str, dict[str, Any]] = {
    "cmps_sf": {
        "name": "犬格拉斯哥复合疼痛量表简表（CMPS-SF）",
        "short_name": "CMPS-SF",
        "assessment_type": "pain",
        "species": "dog",
        "source": "Glasgow Composite Measure Pain Scale - Short Form",
        "groups": [
            ("vocalization", "发声", ["安静", "哭叫/呜咽", "呻吟", "尖叫"]),
            ("wound_attention", "关注伤口", ["不关注", "看向伤口", "舔伤口", "摩擦伤口", "啃咬伤口"]),
            ("mobility", "活动能力", ["正常", "跛行", "缓慢/勉强", "僵硬", "拒绝移动"]),
            ("touch_response", "触诊反应", ["无反应", "看向触诊处", "退缩", "低吼/保护", "咬人", "哭叫"]),
            ("demeanor", "精神与互动", ["愉快/有活力", "安静", "漠不关心", "紧张/焦虑", "抑郁/无反应"]),
            ("posture_activity", "姿势与活动", ["舒适", "不安/变换姿势", "蜷缩/紧张", "姿势异常", "僵硬不动"]),
        ],
    },
    "fgs": {
        "name": "猫面部表情疼痛量表（Feline Grimace Scale）",
        "short_name": "FGS",
        "assessment_type": "pain",
        "species": "cat",
        "source": "Feline Grimace Scale",
        "groups": [
            ("ears", "耳位", ["耳朵朝前", "耳朵略向外", "耳朵扁平向外"]),
            ("orbital", "眼眶紧缩", ["眼睛睁开", "眼睛部分闭合", "眼睛明显眯起"]),
            ("muzzle", "口鼻紧张", ["口鼻放松", "轻度紧张", "明显紧张/扁平"]),
            ("whiskers", "胡须位置", ["自然弯曲", "轻度变直/前移", "明显变直并前移"]),
            ("head", "头部位置", ["头高于肩线", "头与肩线平齐", "头低于肩线或下垂"]),
        ],
    },
    "pvas10": {
        "name": "犬瘙痒视觉模拟评分（pVAS 0-10）",
        "short_name": "pVAS10",
        "assessment_type": "pruritus",
        "species": "dog",
        "source": "Owner-assessed pruritus visual analogue scale",
    },
    "vascat": {
        "name": "猫瘙痒视觉模拟评分（VAScat）",
        "short_name": "VAScat",
        "assessment_type": "pruritus",
        "species": "cat",
        "source": "VAScat owner-assessed feline pruritus scale",
    },
}


def scale_for(assessment_type: str, species: str) -> str:
    species = (species or "").lower()
    if assessment_type == "pain":
        return "fgs" if species == "cat" else "cmps_sf"
    return "vascat" if species == "cat" else "pvas10"


def _number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"请完成“{label}”评分")
    if number < 0 or number > 10:
        raise ValueError(f"“{label}”必须在 0-10 之间")
    return number


def calculate(scale_code: str, answers: dict[str, Any]) -> dict[str, Any]:
    if scale_code not in SCALES:
        raise ValueError("不支持的评分量表")
    if not isinstance(answers, dict):
        raise ValueError("评分数据格式无效")

    if scale_code == "cmps_sf":
        values: list[int] = []
        mobility_omitted = str(answers.get("mobility", "")) == "x"
        for key, label, options in SCALES[scale_code]["groups"]:
            raw = answers.get(key)
            if key == "mobility" and mobility_omitted:
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise ValueError(f"请完成“{label}”评分")
            if value < 0 or value >= len(options):
                raise ValueError(f"“{label}”评分无效")
            values.append(value)
        score = float(sum(values))
        maximum = 20.0 if mobility_omitted else 24.0
        threshold = 5.0 if mobility_omitted else 6.0
        interpretation = (
            f"总分 {score:g}/{maximum:g}，达到常用镇痛干预复核阈值（≥{threshold:g}）。请由兽医结合临床状态立即复核。"
            if score >= threshold
            else f"总分 {score:g}/{maximum:g}，未达到常用镇痛干预复核阈值；仍需结合临床表现并按计划复评。"
        )
    elif scale_code == "fgs":
        values = []
        for key, label, _ in SCALES[scale_code]["groups"]:
            raw = answers.get(key)
            if str(raw) == "x":
                continue
            try:
                value = int(raw)
            except (TypeError, ValueError):
                raise ValueError(f"请完成或标记“{label}”为无法评估")
            if value not in (0, 1, 2):
                raise ValueError(f"“{label}”评分无效")
            values.append(value)
        if len(values) < 4:
            raise ValueError("猫面部评分至少需要 4 个可评估动作单元")
        score = float(sum(values))
        maximum = float(len(values) * 2)
        ratio = score / maximum if maximum else 0.0
        interpretation = (
            f"总分 {score:g}/{maximum:g}（标准化 {ratio:.2f}），达到常用镇痛复核阈值。请由兽医结合临床状态立即复核。"
            if ratio >= 0.39
            else f"总分 {score:g}/{maximum:g}（标准化 {ratio:.2f}），未达到常用镇痛复核阈值；仍需结合临床表现并按计划复评。"
        )
    elif scale_code == "pvas10":
        score = _number(answers.get("score"), "瘙痒程度")
        maximum = 10.0
        interpretation = f"主人评估本次犬瘙痒评分为 {score:g}/10。该量表主要用于同一动物连续复评和趋势比较。"
    else:
        licking = _number(answers.get("licking"), "舔舐/过度理毛")
        scratching = _number(answers.get("scratching"), "抓挠")
        score = max(licking, scratching)
        maximum = 10.0
        interpretation = (
            f"主人评估：舔舐/过度理毛 {licking:g}/10，抓挠 {scratching:g}/10；"
            f"本次 VAScat 记录值取较高项，为 {score:g}/10，主要用于连续复评和趋势比较。"
        )

    normalized = round(score / maximum, 4) if maximum else 0.0
    return {
        "score": score,
        "score_max": maximum,
        "normalized_score": normalized,
        "interpretation": interpretation,
    }


def deterministic_analysis(scale_code: str, result: dict[str, Any], previous: list[dict[str, Any]]) -> str:
    text = result["interpretation"]
    if previous:
        last = previous[0]
        delta = round(float(result["normalized_score"]) - float(last.get("normalized_score") or 0), 3)
        direction = "升高" if delta > 0.02 else "下降" if delta < -0.02 else "基本稳定"
        text += f" 与上次评估相比，标准化评分{direction}（变化 {delta:+.2f}）。"
    text += " 建议结合病史、体格检查、行为和治疗反应判断，并记录下一次复评时间。"
    return text


async def ai_analysis(
    scale_code: str,
    answers: dict[str, Any],
    result: dict[str, Any],
    previous: list[dict[str, Any]],
    pet_context: dict[str, Any],
) -> str:
    fallback = deterministic_analysis(scale_code, result, previous)
    try:
        from app.services.report_llm import report_llm_configured, report_text_client_model
        if not report_llm_configured():
            return fallback
        client, model, _, is_reasoner = report_text_client_model()
        prompt = {
            "量表": SCALES[scale_code]["name"],
            "宠物": pet_context,
            "本次答案": answers,
            "固定规则计算结果": result,
            "既往同量表记录": previous[:5],
        }
        messages = [
            {"role": "system", "content": "你是兽医临床记录助手。仅解释量表结果和变化趋势，不得修改分数，不得下诊断，不得给出具体药名或剂量。输出120-220字中文，包含：本次结果、与既往趋势、需要医生关注的观察点、建议复评。不要使用Markdown符号。"},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
        ]
        kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if not is_reasoner:
            kwargs["temperature"] = 0.2
        response = await client.chat.completions.create(**kwargs)
        content = (response.choices[0].message.content or "").strip()
        return content or fallback
    except Exception:
        return fallback
