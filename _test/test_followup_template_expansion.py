"""第一批专科随访模板的静态回归测试。"""

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.data.vet_seed import (
    DISEASES,
    TEMPLATES,
    TEMPLATE_KEYWORD_PATCHES,
    TEMPLATE_SUPERSEDES,
    apply_template_keyword_patch,
)


EXPECTED_ROUNDS = {
    "耳道疾病": [4, 14],
    "血液寄生虫及蜱媒病": [3, 7, 28],
    "免疫介导性血液病": [1, 4, 14],
    "急性肾损伤/尿闭出院": [1, 3, 7],
    "胰腺及肝胆疾病": [2, 7, 14],
    "危急眼科疾病": [1, 3, 7],
}


templates_by_name = {tpl["name"]: tpl for tpl in TEMPLATES}
assert len(templates_by_name) == len(TEMPLATES), "模板名称必须唯一"

for name, expected_offsets in EXPECTED_ROUNDS.items():
    template = templates_by_name[name]
    assert template["is_active"] if "is_active" in template else True
    assert [round_["day_offset"] for round_ in template["rounds"]] == expected_offsets
    assert template["keywords"], f"{name} 缺少自动匹配关键词"
    for round_ in template["rounds"]:
        assert round_["questions"], f"{name} 的 {round_['round_name']} 没有问题"
        for question in round_["questions"]:
            assert question.get("key") and question.get("type") and question.get("label")


def matched_names(diagnosis):
    normalized = diagnosis.lower()
    matches = []
    for template in sorted(TEMPLATES, key=lambda item: item["priority"], reverse=True):
        keywords = [part.strip().lower() for part in template["keywords"].split(",") if part.strip()]
        if any(keyword in normalized for keyword in keywords):
            matches.append(template["name"])
    matched_set = set(matches)
    suppressed = {
        broad_name
        for specific_name in matched_set
        for broad_name in TEMPLATE_SUPERSEDES.get(specific_name, ())
    }
    return [name for name in matches if name not in suppressed]


MATCH_CASES = {
    "外耳炎": ("耳道疾病", "皮肤系统疾病"),
    "耳道马拉色菌感染": ("耳道疾病", "皮肤系统疾病"),
    "猫血液支原体感染": ("血液寄生虫及蜱媒病", "呼吸系统疾病"),
    "免疫介导性溶血性贫血": ("免疫介导性血液病", None),
    "急性肾损伤": ("急性肾损伤/尿闭出院", "肾病慢病管理"),
    "尿道堵塞": ("急性肾损伤/尿闭出院", "泌尿系统疾病"),
    "急性胰腺炎": ("胰腺及肝胆疾病", "消化系统疾病"),
    "角膜溃疡": ("危急眼科疾病", "眼科疾病"),
}

for diagnosis, (expected, forbidden) in MATCH_CASES.items():
    matches = matched_names(diagnosis)
    assert expected in matches, f"{diagnosis} 未命中 {expected}: {matches}"
    if forbidden:
        assert forbidden not in matches, f"{diagnosis} 不应再命中 {forbidden}: {matches}"


skin_keywords = "皮炎,耳螨,自定义皮肤关键词,Otitis"
patched_skin = apply_template_keyword_patch(
    skin_keywords,
    TEMPLATE_KEYWORD_PATCHES["皮肤系统疾病"],
)
assert patched_skin == "皮炎,自定义皮肤关键词"

respiratory_keywords = "鼻支,支原体,自定义呼吸关键词"
patched_respiratory = apply_template_keyword_patch(
    respiratory_keywords,
    TEMPLATE_KEYWORD_PATCHES["呼吸系统疾病"],
)
assert "支原体" not in patched_respiratory.split(",")
assert "猫支原体感染" in patched_respiratory.split(",")
assert "自定义呼吸关键词" in patched_respiratory.split(",")

disease_names = {row[0] for row in DISEASES}
assert "猫血液支原体感染" in disease_names
assert "无形体感染" in disease_names

print("follow-up template expansion tests passed")
