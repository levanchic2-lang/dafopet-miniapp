"""
医疗报告与分析类文字生成的统一 LLM 客户端。

策略：
- 配置了 DEEPSEEK_API_KEY → 用 DeepSeek（OpenAI 兼容）做「文字判读/起草」
  （B超、X光整理、显微镜、量表分析、诊后说明等纯文本推理任务）。
- 否则回退到原豆包文本模型（WECOM_AGENT_MODEL > OPENAI_MODEL）。

注意：视觉类任务（TNR 审核、进货单识别）不走这里——DeepSeek 无视觉能力。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


_DEEPSEEK_MODEL_ALIASES = {
    # 旧配置名。当前报告生成接口返回的可用模型名为 deepseek-v4-pro / deepseek-v4-flash。
    "deepseek-reasoner": "deepseek-v4-pro",
    "deepseek-r1": "deepseek-v4-pro",
    "deepseek-chat": "deepseek-v4-flash",
}


def _normalize_deepseek_model(model: str) -> str:
    key = (model or "").strip()
    return _DEEPSEEK_MODEL_ALIASES.get(key.lower(), key or "deepseek-v4-flash")


def report_llm_configured() -> bool:
    """是否有可用于报告文字生成的模型（DeepSeek 或 豆包文本）。"""
    return bool((settings.deepseek_api_key or "").strip()
                or (settings.openai_api_key or "").strip())


def report_text_client_model():
    """返回 (AsyncOpenAI client, model_id, provider, is_reasoner)。
    优先 DeepSeek；未配置则回退豆包文本模型。
    is_reasoner=True 表示带思维链的推理模型（如 deepseek-reasoner / R1），
    调用方需把 max_tokens 给足（思维链 + 正式回答共用额度），否则正式回答会被截断成空。"""
    from openai import AsyncOpenAI

    dk = (settings.deepseek_api_key or "").strip()
    if dk:
        base = (settings.deepseek_base_url or "").strip() or "https://api.deepseek.com"
        # 医疗内容固定优先走专用 Pro 配置；deepseek_model 仅兼容旧部署。
        configured = (getattr(settings, "deepseek_report_model", "") or "").strip() \
            or (settings.deepseek_model or "").strip()
        model = _normalize_deepseek_model(configured or "deepseek-v4-pro")
        is_reasoner = ("reason" in model.lower()) or ("r1" in model.lower()) or model.lower().endswith("-pro")
        return AsyncOpenAI(api_key=dk, base_url=base), model, "deepseek", is_reasoner

    base = (settings.openai_base_url or "").strip() or None
    model = (getattr(settings, "wecom_agent_model", "") or "").strip() \
        or (settings.openai_model or "gpt-4o-mini")
    is_reasoner = ("reason" in model.lower()) or ("think" in model.lower())
    return AsyncOpenAI(api_key=settings.openai_api_key, base_url=base), model, "doubao", is_reasoner


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    """Parse a JSON object while tolerating markdown fences or brief prose wrappers."""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = lines[1:] if lines else lines
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(text[start:end + 1])
            return data if isinstance(data, dict) else None
        except Exception:
            pass
    return None


async def generate_json_object(
    *,
    client,
    model: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    temperature: float | None = None,
    task: str = "medical_report",
    attempts: int = 2,
) -> tuple[dict[str, Any] | None, str, str]:
    """Generate a JSON object with one automatic retry and observable timing.

    Returns ``(data, raw, error)``. The retry explicitly asks for a complete JSON
    object, which handles the most common truncated/malformed report response.
    """
    last_raw = ""
    last_error = ""
    for attempt in range(max(1, attempts)):
        call_messages = [dict(item) for item in messages]
        if attempt:
            call_messages.append({
                "role": "user",
                "content": "上一次输出不是完整有效的 JSON。请重新生成，只输出一个完整 JSON 对象，不要 Markdown、解释或前后缀。",
            })
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": call_messages,
            "max_tokens": max_tokens,
        }
        if temperature is not None:
            kwargs["temperature"] = temperature
        started = time.monotonic()
        try:
            response = await client.chat.completions.create(**kwargs)
            last_raw = (response.choices[0].message.content or "").strip()
            data = _parse_json_object(last_raw)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if data is not None:
                logger.info(
                    "[llm] task=%s model=%s attempt=%s elapsed_ms=%s ok=1",
                    task, model, attempt + 1, elapsed_ms,
                )
                return data, last_raw, ""
            last_error = "模型输出不是完整有效的 JSON 对象"
            logger.warning(
                "[llm] task=%s model=%s attempt=%s elapsed_ms=%s ok=0 parse_error raw=%s",
                task, model, attempt + 1, elapsed_ms, last_raw[:300],
            )
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            last_error = f"调用模型失败：{exc}"
            logger.warning(
                "[llm] task=%s model=%s attempt=%s elapsed_ms=%s ok=0 error=%s",
                task, model, attempt + 1, elapsed_ms, exc,
            )
        if attempt + 1 < attempts:
            await asyncio.sleep(0.5 if attempt else 1.0)
    return None, last_raw, last_error or "模型生成失败"
