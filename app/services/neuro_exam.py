"""Structured canine/feline neurologic examination and deterministic localization hints."""
from __future__ import annotations

from typing import Any


MENTATION = ["清醒", "嗜睡", "昏睡", "昏迷"]
GAIT_OPTIONS = ["正常", "轻度共济失调", "重度共济失调", "跛行", "瘫痪"]
POSTURE_OPTIONS = ["头倾", "环行", "颈背强直", "驼背", "低头", "侧弯"]
POSTURAL_ITEMS = [
    ("proprioception", "本体定位（翻爪）", ["RF", "LF", "RH", "LH"]),
    ("hopping", "单支跃步", ["RF", "LF", "RH", "LH"]),
    ("wheelbarrow", "手推车", ["前肢", "四轮推"]),
    ("extensor_thrust", "伸肌姿势冲动", ["后肢"]),
    ("placing", "视觉/触觉放置", ["前肢", "后肢"]),
]
CN_ITEMS = [
    ("menace", "视觉/瞳孔", "威胁反应", "II→皮层/小脑→VII"),
    ("dazzle", "视觉/瞳孔", "惊耀反射", "II→中脑→VII"),
    ("plr", "视觉/瞳孔", "PLR 直接/间接", "II→预顶盖→III"),
    ("pupil", "视觉/瞳孔", "瞳孔大小/对称", "交感/副交感"),
    ("corneal", "角膜/眼睑/鼻黏膜", "角膜反射", "V1→VII"),
    ("palpebral", "角膜/眼睑/鼻黏膜", "睑反射", "V1/V2→VII"),
    ("nasal", "角膜/眼睑/鼻黏膜", "鼻黏膜刺激", "V→对侧前脑"),
    ("eye_position", "眼位/动眼/前庭", "静止眼位/斜视", "III/IV/VI/VIII"),
    ("vor", "眼位/动眼/前庭", "生理性眼震 VOR", "III/IV/VI/VIII"),
    ("positional_strabismus", "眼位/动眼/前庭", "体位性斜视", "前庭"),
    ("facial_sensation", "三叉神经", "面部痛觉/触觉", "V"),
    ("jaw_tone", "三叉神经", "咬肌张力/咀嚼", "V3 运动"),
    ("facial_symmetry", "面神经", "面部对称/耳位/鼻唇沟", "VII"),
    ("blink_strength", "面神经", "眨眼/闭眼力", "VII"),
    ("lip_pinch", "面神经", "唇夹痛反应", "V→VII"),
    ("tear_secretion", "面神经", "泪液分泌", "VII/副交感"),
    ("hearing", "听觉/前庭", "对声反应", "VIII"),
    ("spontaneous_nystagmus", "听觉/前庭", "自发性眼震", "VIII/前庭通路"),
    ("induced_nystagmus", "听觉/前庭", "诱发性眼震", "VIII/前庭通路"),
    ("vestibular_posture", "听觉/前庭", "头倾/头滚/跌倒", "VIII/前庭通路"),
    ("gag_swallow", "咽/喉", "咽反射/吞咽", "IX/X"),
    ("voice_cough", "咽/喉", "声音/呕吐/咳嗽", "X"),
    ("neck_strength", "副神经", "颈肩肌力量/萎缩", "XI"),
    ("tongue", "舌下神经", "舌运动/偏斜/萎缩", "XII"),
    ("horner", "交感", "Horner 四联征", "交感"),
    ("middle_ear", "中耳评估", "耳后疼痛/外耳炎", "VII/交感/VIII"),
]
SPINAL_ITEMS = [
    ("biceps", "二头肌", "C6-C8", ["左", "右"]),
    ("triceps", "三头肌", "C7-T1", ["左", "右"]),
    ("ecr", "腕伸肌", "C7-T2", ["左", "右"]),
    ("thoracic_withdrawal", "前肢撤回", "C6-T2", ["左", "右"]),
    ("patellar", "髌腱反射", "L4-L6", ["左", "右"]),
    ("cranial_tibial", "胫前肌反射", "L6-L7", ["左", "右"]),
    ("gastrocnemius", "跟腱/腓肠肌反射", "S1-S2", ["左", "右"]),
    ("pelvic_withdrawal", "后肢撤回", "L6-S1", ["左", "右"]),
    ("plantar", "跖反射", "L6-S2", ["左", "右"]),
]
LOCALIZATION_OPTIONS = [
    "未见明确神经定位", "大脑/前脑", "脑干", "小脑", "中枢性前庭",
    "外周性前庭", "C1-C5", "C6-T2", "T3-L3", "L4-S3", "外周神经/神经肌肉接头",
]

_POSTURAL_LABEL = {"0": "正常", "1": "减弱", "2": "缺失", "x": "未检查"}
_CN_LABEL = {"0": "正常", "1": "减弱", "2": "缺失", "x": "未检查"}
_REFLEX_LABEL = {"0": "正常", "1": "减弱", "2": "亢进", "3": "缺失", "x": "未检查"}


def form_config() -> dict[str, Any]:
    return {
        "mentation": MENTATION,
        "gait_options": GAIT_OPTIONS,
        "posture_options": POSTURE_OPTIONS,
        "postural_items": POSTURAL_ITEMS,
        "cn_items": CN_ITEMS,
        "spinal_items": SPINAL_ITEMS,
        "localization_options": LOCALIZATION_OPTIONS,
    }


def _side_abnormal(values: dict[str, Any], keys: list[str]) -> bool:
    return any(str(values.get(k, "x")) in {"1", "2"} for k in keys)


def _reflex_state(findings: dict[str, Any], item_keys: set[str]) -> set[str]:
    states: set[str] = set()
    for key, sides in (findings.get("spinal_reflexes") or {}).items():
        if key not in item_keys or not isinstance(sides, dict):
            continue
        states.update(str(v) for v in sides.values() if str(v) != "x")
    return states


def generate_neuro_result(findings: dict[str, Any]) -> dict[str, str]:
    """Generate a narrative and non-diagnostic localization candidates from selections."""
    lines: list[str] = []
    evidence: list[str] = []
    candidates: list[str] = []

    history = findings.get("history") or {}
    history_bits = []
    for key, label in (("onset", "起病"), ("course", "病程"), ("pain", "疼痛评分")):
        if history.get(key):
            suffix = "/10" if key == "pain" else ""
            history_bits.append(f"{label}{history[key]}{suffix}")
    for key, label in (("trauma", "外伤史"), ("seizures", "癫痫/发作史"), ("exposure", "毒物/药物暴露史")):
        if history.get(key) in {"有", "无"}:
            history_bits.append(f"{label}{history[key]}")
    if history_bits:
        lines.append("病程信息：" + "，".join(history_bits) + "。")

    mentation = findings.get("mentation") or "未检查"
    lines.append(f"意识状态：{mentation}。")
    gait = findings.get("gait") or {}
    lines.append(f"步态：前肢{gait.get('thoracic') or '未检查'}，后肢{gait.get('pelvic') or '未检查'}。")
    posture = findings.get("posture") or []
    lines.append("姿势：" + ("、".join(posture) if posture else "未见明显异常") + "。")

    postural_abnormal = []
    postural = findings.get("postural_reactions") or {}
    for key, label, limbs in POSTURAL_ITEMS:
        vals = postural.get(key) or {}
        abnormal = [f"{limb}{_POSTURAL_LABEL.get(str(vals.get(limb, 'x')), '未检查')}" for limb in limbs if str(vals.get(limb, "x")) in {"1", "2"}]
        if abnormal:
            postural_abnormal.append(f"{label}（{'、'.join(abnormal)}）")
    lines.append("体位反应：" + ("；".join(postural_abnormal) if postural_abnormal else "已检查项目未见减弱或缺失") + "。")

    cn_abnormal = []
    cranial = findings.get("cranial_nerves") or {}
    for key, _category, label, pathway in CN_ITEMS:
        vals = cranial.get(key) or {}
        abnormal = [f"{side}{_CN_LABEL.get(str(vals.get(side, 'x')), '未检查')}" for side in ("左", "右") if str(vals.get(side, "x")) in {"1", "2"}]
        if abnormal:
            cn_abnormal.append(f"{label}（{'、'.join(abnormal)}；{pathway}）")
    nystagmus = findings.get("nystagmus_type") or "未见"
    if nystagmus != "未见":
        cn_abnormal.append(f"眼震：{nystagmus}")
    lines.append("脑神经：" + ("；".join(cn_abnormal) if cn_abnormal else "已检查项目未见明显异常") + "。")

    reflex_abnormal = []
    spinal = findings.get("spinal_reflexes") or {}
    for key, label, segment, sides in SPINAL_ITEMS:
        vals = spinal.get(key) or {}
        abnormal = [f"{side}{_REFLEX_LABEL.get(str(vals.get(side, 'x')), '未检查')}" for side in sides if str(vals.get(side, "x")) in {"1", "2", "3"}]
        if abnormal:
            reflex_abnormal.append(f"{label} {segment}（{'、'.join(abnormal)}）")
    if findings.get("perineal") in {"1", "2", "3"}:
        reflex_abnormal.append(f"会阴反射{_REFLEX_LABEL.get(str(findings['perineal']))}")
    lines.append("脊髓反射：" + ("；".join(reflex_abnormal) if reflex_abnormal else "已检查项目未见减弱、亢进或缺失") + "。")

    pain_locations = findings.get("pain_locations") or []
    pain_severity = findings.get("pain_severity") or "无"
    deep_pain = findings.get("deep_pain") or "未检查"
    lines.append(f"疼痛与深痛：脊柱触诊{pain_severity}" + (f"（{'、'.join(pain_locations)}）" if pain_locations else "") + f"；深痛{deep_pain}；尾张力{findings.get('tail_tone') or '未检查'}；膀胱张力{findings.get('bladder_tone') or '未检查'}。")
    grade = findings.get("motor_grade")
    if str(grade).isdigit():
        lines.append(f"运动/步行分级：{grade}/5。")

    front_def = any(_side_abnormal(postural.get(k) or {}, ["RF", "LF", "前肢"]) for k, _, _ in POSTURAL_ITEMS)
    hind_def = any(_side_abnormal(postural.get(k) or {}, ["RH", "LH", "后肢"]) for k, _, _ in POSTURAL_ITEMS)
    front_reflex = _reflex_state(findings, {"biceps", "triceps", "ecr", "thoracic_withdrawal"})
    hind_reflex = _reflex_state(findings, {"patellar", "cranial_tibial", "gastrocnemius", "pelvic_withdrawal"})

    if mentation in {"昏睡", "昏迷"} or (mentation == "嗜睡" and cn_abnormal):
        candidates.append("脑干或前脑")
        evidence.append("意识状态异常并伴脑神经体征")
    if nystagmus in {"垂直", "变向", "体位性变向"}:
        candidates.append("中枢性前庭")
        evidence.append(f"出现{nystagmus}眼震")
    if nystagmus in {"水平固定方向", "旋转固定方向"} and "头倾" in posture:
        candidates.append("外周性前庭")
        evidence.append("固定方向眼震伴头倾")

    menace = cranial.get("menace") or {}
    plr = cranial.get("plr") or {}
    for side in ("左", "右"):
        if str(menace.get(side)) in {"1", "2"} and str(plr.get(side)) == "0":
            candidates.append("对侧前脑或同侧小脑通路")
            evidence.append(f"{side}侧威胁反应异常但 PLR 正常")
    if front_def and hind_def:
        if front_reflex & {"1", "3"}:
            candidates.append("C6-T2")
            evidence.append("四肢体位反应异常且前肢反射减弱/缺失")
        else:
            candidates.append("C1-C5")
            evidence.append("四肢体位反应异常且前肢反射未见下运动神经元改变")
    elif hind_def and not front_def:
        if hind_reflex & {"1", "3"}:
            candidates.append("L4-S3")
            evidence.append("后肢体位反应异常且后肢反射减弱/缺失")
        else:
            candidates.append("T3-L3")
            evidence.append("后肢体位反应异常且后肢反射正常/亢进")
    if (front_reflex | hind_reflex) and (front_reflex | hind_reflex) <= {"1", "3"}:
        candidates.append("外周神经/神经肌肉接头")
        evidence.append("多肢反射广泛减弱或缺失")

    for key, label in (("jaw_tone", "V3"), ("facial_symmetry", "VII"), ("gag_swallow", "IX/X"), ("tongue", "XII")):
        vals = cranial.get(key) or {}
        bad_sides = [side for side in ("左", "右") if str(vals.get(side)) in {"1", "2"}]
        if bad_sides:
            candidates.append(f"脑神经 {label}")
            evidence.append(f"{''.join(bad_sides)}侧{next((x[2] for x in CN_ITEMS if x[0] == key), label)}异常")

    candidates = list(dict.fromkeys(candidates))
    evidence = list(dict.fromkeys(evidence))
    if candidates:
        hint = "候选定位：" + "；".join(candidates) + "。\n依据：" + "；".join(evidence) + "。"
        conclusion = "本次神经学检查存在异常体征，优先考虑" + "、".join(candidates) + "；需由主诊医生结合病史、影像及实验室检查确认。"
    else:
        hint = "当前已勾选结果未形成明确的神经解剖定位；请结合未检查项目及临床病史复核。"
        conclusion = "当前已完成项目未提示明确神经解剖定位，最终结论由主诊医生结合完整检查确认。"
    return {
        "description": "\n".join(lines),
        "localization_hint": hint,
        "suggested_localization": candidates[0] if candidates else "未见明确神经定位",
        "conclusion": conclusion,
    }
