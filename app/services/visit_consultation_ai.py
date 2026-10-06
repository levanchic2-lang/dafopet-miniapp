from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from app.config import settings


logger = logging.getLogger(__name__)


CONSULTATION_SYSTEM = """你是宠物医院医生的病历文书助手。请把接诊现场的医患对话转写整理成病历草稿。

必须遵守：
1. 只能整理录音中明确说过的事实，严禁补写、推测或自行诊断。
2. 主人描述的症状、时间线、饮食、排便、既往史和用药史放入 chief_complaint 或 notes。
3. 只有医生明确说出的观察、测量、触诊、听诊等内容才能放入 physical_exam。
4. 只有医生明确表达的判断、考虑、怀疑或确诊内容才能放入 diagnosis；没有则留空。
5. 只有医生明确表达的检查、治疗、用药、护理、复查和给主人医嘱才能放入 treatment_plan。
6. 无法确定说话人或内容不清楚时不要猜，放入 notes 并注明“录音中表述不清，待医生确认”。
7. 保留重要阴性信息，例如“未呕吐”“没有腹泻”，不要省略。
8. 每个字段使用简洁、完整、可直接进入病历的中文纯文本，不使用 Markdown。
9. 输出必须是合法 JSON 对象，不要附加解释。

JSON 结构：
{
  "chief_complaint": "主诉、现病史和本次就诊原因",
  "physical_exam": "医生明确说出的体格检查",
  "diagnosis": "医生明确说出的评估或诊断",
  "treatment_plan": "检查、治疗计划和给主人医嘱",
  "notes": "既往史、用药史、饮食生活情况及需要医生复核的内容"
}"""


def _speech_config() -> tuple[str, str | None, str] | None:
    key = (getattr(settings, "clinical_speech_api_key", "") or "").strip()
    base = (getattr(settings, "clinical_speech_base_url", "") or "").strip()
    if not key:
        fallback_base = (settings.openai_base_url or "").strip()
        if not fallback_base or "api.openai.com" in fallback_base.lower():
            key = (settings.openai_api_key or "").strip()
            base = fallback_base
    if not key:
        return None
    model = (getattr(settings, "clinical_speech_model", "") or "gpt-4o-mini-transcribe").strip()
    return key, base or None, model


def speech_transcription_configured() -> bool:
    return _speech_config() is not None


async def transcribe_visit_audio(audio_path: str) -> dict[str, Any]:
    config = _speech_config()
    if not config:
        return {"ok": False, "error": "服务器尚未配置接诊录音转写服务"}
    try:
        from openai import AsyncOpenAI
    except ImportError:
        return {"ok": False, "error": "服务器缺少语音转写组件"}

    key, base_url, model = config
    path = Path(audio_path)
    if not path.is_file():
        return {"ok": False, "error": "录音文件不存在"}
    client = AsyncOpenAI(api_key=key, base_url=base_url)
    try:
        with path.open("rb") as audio:
            response = await client.audio.transcriptions.create(
                model=model,
                file=audio,
                language="zh",
                response_format="text",
            )
    except Exception as exc:
        logger.warning("[visit_consultation] transcription failed: %s", exc)
        return {"ok": False, "error": f"录音转写失败：{exc}"}
    text = response if isinstance(response, str) else getattr(response, "text", "")
    text = str(text or "").strip()
    if not text:
        return {"ok": False, "error": "录音未识别出有效文字"}
    return {"ok": True, "text": text}


def _plain(value: Any) -> str:
    text = str(value or "").strip().replace("**", "").replace("__", "")
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:12000].strip()


async def organize_visit_transcript(
    transcript: str,
    *,
    pet_context: dict[str, Any],
    existing_visit: dict[str, Any],
) -> dict[str, Any]:
    from app.services.report_llm import (
        generate_json_object,
        report_llm_configured,
        report_text_client_model,
    )

    if not report_llm_configured():
        return {"ok": False, "error": "服务器尚未配置病历整理模型"}
    client, model, _, is_reasoner = report_text_client_model()
    payload = {
        "pet": pet_context,
        "existing_visit": existing_visit,
        "transcript": (transcript or "").strip(),
    }
    data, raw, error = await generate_json_object(
        client=client,
        model=model,
        messages=[
            {"role": "system", "content": CONSULTATION_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
        ],
        temperature=0.1,
        max_tokens=8000 if is_reasoner else 2600,
        task="visit_consultation",
    )
    if data is None:
        return {"ok": False, "error": error, "raw": raw}
    return {
        "ok": True,
        "chief_complaint": _plain(data.get("chief_complaint")),
        "physical_exam": _plain(data.get("physical_exam")),
        "diagnosis": _plain(data.get("diagnosis")),
        "treatment_plan": _plain(data.get("treatment_plan")),
        "notes": _plain(data.get("notes")),
    }
