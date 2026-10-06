from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

import httpx

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


_VOLCENGINE_STANDARD_SUBMIT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
_VOLCENGINE_STANDARD_QUERY = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
_VOLCENGINE_FLASH_ENDPOINT = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/recognize/flash"
_VETERINARY_HOTWORDS = [
    "呋塞米", "匹莫苯丹", "螺内酯", "贝那普利", "马罗匹坦", "奥美拉唑",
    "头孢噻呋", "多西环素", "美洛昔康", "非罗考昔", "加巴喷丁", "泼尼松龙",
    "犬瘟", "细小", "猫瘟", "猫传腹", "猫白血病", "猫免疫缺陷病毒",
    "膀胱结石", "尿闭", "肾衰", "胰腺炎", "胆囊黏液囊肿", "肥厚型心肌病",
    "二尖瓣", "三尖瓣", "肺动脉高压", "气管塌陷", "髌骨脱位", "十字韧带",
    "血常规", "生化", "电解质", "SDMA", "心脏彩超", "腹部B超", "X光",
]


def _speech_config() -> dict[str, str] | None:
    provider = (getattr(settings, "clinical_speech_provider", "openai") or "openai").strip().lower()
    key = (getattr(settings, "clinical_speech_api_key", "") or "").strip()
    base = (getattr(settings, "clinical_speech_base_url", "") or "").strip()
    if provider in {"volcengine", "doubao", "volc"}:
        if not key:
            return None
        resource_id = (
            getattr(settings, "clinical_speech_resource_id", "")
            or "volc.seedasr.auc"
        ).strip()
        default_endpoint = (
            _VOLCENGINE_FLASH_ENDPOINT
            if resource_id == "volc.bigasr.auc_turbo"
            else _VOLCENGINE_STANDARD_SUBMIT
        )
        return {
            "provider": "volcengine",
            "key": key,
            "base_url": base or default_endpoint,
            "resource_id": resource_id,
        }
    if not key:
        fallback_base = (settings.openai_base_url or "").strip()
        if not fallback_base or "api.openai.com" in fallback_base.lower():
            key = (settings.openai_api_key or "").strip()
            base = fallback_base
    if not key:
        return None
    model = (getattr(settings, "clinical_speech_model", "") or "gpt-4o-mini-transcribe").strip()
    return {
        "provider": "openai",
        "key": key,
        "base_url": base,
        "model": model,
    }


def speech_transcription_configured() -> bool:
    return _speech_config() is not None


async def transcribe_visit_audio(audio_path: str) -> dict[str, Any]:
    config = _speech_config()
    if not config:
        return {"ok": False, "error": "服务器尚未配置接诊录音转写服务"}
    path = Path(audio_path)
    if not path.is_file():
        return {"ok": False, "error": "录音文件不存在"}
    if config["provider"] == "volcengine":
        return await _transcribe_with_volcengine(path, config)

    try:
        from openai import AsyncOpenAI
    except ImportError:
        return {"ok": False, "error": "服务器缺少语音转写组件"}

    client = AsyncOpenAI(api_key=config["key"], base_url=config["base_url"] or None)
    try:
        with path.open("rb") as audio:
            response = await client.audio.transcriptions.create(
                model=config["model"],
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


async def _transcribe_with_volcengine(path: Path, config: dict[str, str]) -> dict[str, Any]:
    audio_format = path.suffix.lower().lstrip(".") or "m4a"
    if audio_format == "mp4":
        audio_format = "m4a"
    context = json.dumps(
        {"hotwords": [{"word": word} for word in _VETERINARY_HOTWORDS]},
        ensure_ascii=False,
    )
    body = {
        "audio": {
            "data": base64.b64encode(path.read_bytes()).decode("ascii"),
            "format": audio_format,
        },
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": False,
            "show_utterances": True,
            "corpus": {"context": context},
        },
    }
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": config["key"],
        "X-Api-Resource-Id": config["resource_id"],
        "X-Api-Request-Id": str(uuid.uuid4()),
        "X-Api-Sequence": "-1",
    }
    if config["resource_id"] == "volc.bigasr.auc_turbo":
        return await _volcengine_flash(config["base_url"], headers, body)
    return await _volcengine_standard(config["base_url"], headers, body)


def _volcengine_error(response: httpx.Response) -> str:
    message = response.headers.get("X-Api-Message", "").strip()
    code = response.headers.get("X-Api-Status-Code", "").strip()
    if not message:
        try:
            payload = response.json()
            message = str(
                payload.get("message")
                or (payload.get("header") or {}).get("message")
                or ""
            ).strip()
        except Exception:
            message = ""
    return message or code or f"HTTP {response.status_code}"


def _volcengine_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except Exception:
        return ""
    result = payload.get("result") or (payload.get("body") or {}).get("result") or {}
    return str(result.get("text") or "").strip()


async def _volcengine_flash(
    endpoint: str,
    headers: dict[str, str],
    body: dict[str, Any],
) -> dict[str, Any]:
    try:
        timeout = httpx.Timeout(180.0, connect=15.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(endpoint, headers=headers, json=body)
    except Exception as exc:
        logger.warning("[visit_consultation] volcengine flash request failed: %s", exc)
        return {"ok": False, "error": f"录音转写失败：{exc}"}
    status_code = response.headers.get("X-Api-Status-Code", "")
    if response.is_error or status_code != "20000000":
        error = _volcengine_error(response)
        logger.warning("[visit_consultation] volcengine flash rejected: %s", error)
        return {"ok": False, "error": f"录音转写失败：{error}"}
    text = _volcengine_text(response)
    return {"ok": True, "text": text} if text else {
        "ok": False,
        "error": "录音未识别出有效文字",
    }


async def _volcengine_standard(
    endpoint: str,
    headers: dict[str, str],
    body: dict[str, Any],
) -> dict[str, Any]:
    try:
        timeout = httpx.Timeout(180.0, connect=15.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            submitted = await client.post(endpoint, headers=headers, json=body)
            submit_code = submitted.headers.get("X-Api-Status-Code", "")
            if submitted.is_error or submit_code != "20000000":
                error = _volcengine_error(submitted)
                logger.warning("[visit_consultation] volcengine submit rejected: %s", error)
                return {"ok": False, "error": f"录音提交失败：{error}"}

            query_headers = {
                "Content-Type": "application/json",
                "X-Api-Key": headers["X-Api-Key"],
                "X-Api-Resource-Id": headers["X-Api-Resource-Id"],
                "X-Api-Request-Id": headers["X-Api-Request-Id"],
            }
            log_id = submitted.headers.get("X-Tt-Logid", "")
            if log_id:
                query_headers["X-Tt-Logid"] = log_id
            for _ in range(180):
                await asyncio.sleep(2)
                response = await client.post(
                    _VOLCENGINE_STANDARD_QUERY,
                    headers=query_headers,
                    json={},
                )
                status_code = response.headers.get("X-Api-Status-Code", "")
                if status_code in {"20000001", "20000002"}:
                    continue
                if response.is_error or status_code != "20000000":
                    error = _volcengine_error(response)
                    logger.warning("[visit_consultation] volcengine query failed: %s", error)
                    return {"ok": False, "error": f"录音转写失败：{error}"}
                text = _volcengine_text(response)
                return {"ok": True, "text": text} if text else {
                    "ok": False,
                    "error": "录音未识别出有效文字",
                }
    except Exception as exc:
        logger.warning("[visit_consultation] volcengine standard request failed: %s", exc)
        return {"ok": False, "error": f"录音转写失败：{exc}"}
    return {"ok": False, "error": "录音转写等待超时，请稍后重新整理"}


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
