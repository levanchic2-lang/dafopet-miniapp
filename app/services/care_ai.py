from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


CARE_SUMMARY_SYSTEM = """你是宠物医院医生的文书助手。你的任务是把医生已经记录的病历、处方、检查结果，整理成给主人看的诊后说明。

你必须遵守：
1. 不新增诊断，不推测系统资料里没有的疾病。
2. 不替医生下最终诊断。
3. 不承诺疗效，不保证恢复时间。
4. 不建议主人自行调整药量、停药或换药。
5. 如果资料不足，要写“需结合医生复查判断”，不要编造。
6. 语言要通俗、具体、温和，像医生对主人解释。
7. 输出只用于医生编辑确认，不能写“我是AI”。
8. 输出必须是普通纯文本，不要使用 Markdown。禁止出现 **、__、###、- 项目符号、表格、代码块。
9. 药品名、检查名、重点提醒都不要加粗，不要加星号；直接写中文自然句。

请按固定结构输出：
【本次就诊原因】
【医生关注的问题】
【已做检查和结果说明】
【本次用药说明】
【回家护理要点】
【需要尽快联系医院的情况】
【复查建议】"""


CARE_PLAN_SYSTEM = """你是宠物医院医生的复诊计划助手。你的任务是根据医生已确认的病历、处方、检查结果和诊后说明，生成可执行的复诊/回访计划草稿。

你必须遵守：
1. 不新增诊断。
2. 不替医生决定治疗方案。
3. 不让客户自行调整药量。
4. 每个任务都要有明确目的、时间、要问的问题、异常触发条件。
5. 任务数量控制在 1-4 个，避免制造无意义待办。
6. 高风险情况要提示“转医生/建议到店”。
7. 输出必须是合法 JSON，不要输出 Markdown。
8. JSON 字符串内容也必须是普通纯文本，禁止出现 **、__、###、Markdown 列表或表格。

任务类型只能使用：
phone, message, photo_check, recheck_visit, medication_check

风险等级只能使用：
low, medium, high

优先级只能使用：
low, normal, high, urgent

输出 JSON 结构：
{
  "title": "复诊计划标题",
  "reason": "为什么需要这个计划",
  "risk_level": "low|medium|high",
  "plan_text": "给医生看的计划说明",
  "tasks": [
    {
      "days_after": 3,
      "task_type": "photo_check",
      "title": "任务标题",
      "question_text": "回访时问什么",
      "risk_trigger": "什么情况升级给医生或建议到店",
      "priority": "low|normal|high|urgent"
    }
  ]
}"""


_CARE_SUMMARY_FOOTER = (
    "\n\n以上内容为本次就诊后的护理与复查说明，具体诊断和治疗方案以医生确认的病历记录为准。"
    "如症状加重或出现异常，请及时联系医院或到店复查。"
)


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


def _plain_text(raw: str) -> str:
    """Remove common Markdown artifacts before storing client-visible drafts."""
    text = _strip_md(raw)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"(?m)^\s*[-*]\s+", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _format_payload(payload: dict[str, Any], doctor_instruction: str = "") -> str:
    lines = ["【结构化病历资料】", json.dumps(payload, ensure_ascii=False, indent=2)]
    if (doctor_instruction or "").strip():
        lines += ["", "【医生补充要求】", doctor_instruction.strip()]
    return "\n".join(lines)


async def draft_client_care_summary(payload: dict[str, Any], doctor_instruction: str = "") -> dict[str, Any]:
    from app.services.report_llm import report_llm_configured, report_text_client_model

    if not report_llm_configured():
        return {"ok": False, "error": "未配置文字生成模型（DEEPSEEK_API_KEY / OPENAI_API_KEY）"}
    try:
        from openai import AsyncOpenAI  # noqa: F401
    except ImportError:
        return {"ok": False, "error": "缺少 openai 库"}

    client, model, _, is_reasoner = report_text_client_model()
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": CARE_SUMMARY_SYSTEM},
                {"role": "user", "content": _format_payload(payload, doctor_instruction)},
            ],
            temperature=0.35,
            max_tokens=8000 if is_reasoner else 2200,
        )
    except Exception as e:
        logger.warning("[care_ai] summary API failed: %s", e)
        return {"ok": False, "error": f"调用模型失败：{e}"}

    text = _plain_text(resp.choices[0].message.content or "")
    if text and "以上内容为本次就诊后的护理与复查说明" not in text:
        text += _CARE_SUMMARY_FOOTER
    return {"ok": True, "text": text}


async def draft_care_plan(payload: dict[str, Any], doctor_instruction: str = "") -> dict[str, Any]:
    from app.services.report_llm import report_llm_configured, report_text_client_model

    if not report_llm_configured():
        return {"ok": False, "error": "未配置文字生成模型（DEEPSEEK_API_KEY / OPENAI_API_KEY）"}
    try:
        from openai import AsyncOpenAI  # noqa: F401
    except ImportError:
        return {"ok": False, "error": "缺少 openai 库"}

    client, model, _, is_reasoner = report_text_client_model()
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": CARE_PLAN_SYSTEM},
                {"role": "user", "content": _format_payload(payload, doctor_instruction)},
            ],
            temperature=0.25,
            max_tokens=8000 if is_reasoner else 1800,
        )
    except Exception as e:
        logger.warning("[care_ai] plan API failed: %s", e)
        return {"ok": False, "error": f"调用模型失败：{e}"}

    raw = _strip_md(resp.choices[0].message.content or "")
    try:
        data = json.loads(raw)
    except Exception as e:
        logger.warning("[care_ai] plan JSON parse failed: %s; raw=%s", e, raw[:300])
        return {"ok": False, "error": f"模型输出不是有效 JSON：{e}", "raw": raw}
    if not isinstance(data, dict):
        return {"ok": False, "error": "模型输出不是 JSON 对象", "raw": raw}
    tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else []
    cleaned = []
    for t in tasks[:4]:
        if not isinstance(t, dict):
            continue
        task_type = str(t.get("task_type") or "message").strip()
        if task_type not in {"phone", "message", "photo_check", "recheck_visit", "medication_check"}:
            task_type = "message"
        priority = str(t.get("priority") or "normal").strip()
        if priority not in {"low", "normal", "high", "urgent"}:
            priority = "normal"
        try:
            days_after = int(t.get("days_after") or 0)
        except Exception:
            days_after = 0
        cleaned.append({
            "days_after": max(0, min(days_after, 365)),
            "task_type": task_type,
            "title": _plain_text(str(t.get("title") or "复诊回访"))[:80],
            "question_text": _plain_text(str(t.get("question_text") or "")),
            "risk_trigger": _plain_text(str(t.get("risk_trigger") or "")),
            "priority": priority,
        })
    risk_level = str(data.get("risk_level") or "low").strip()
    if risk_level not in {"low", "medium", "high"}:
        risk_level = "low"
    return {
        "ok": True,
        "title": _plain_text(str(data.get("title") or "复诊计划"))[:160],
        "reason": _plain_text(str(data.get("reason") or "")),
        "risk_level": risk_level,
        "plan_text": _plain_text(str(data.get("plan_text") or "")),
        "tasks": cleaned,
    }
