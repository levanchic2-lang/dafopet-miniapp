"""
报告类文字生成的统一 LLM 客户端。

策略：
- 配置了 DEEPSEEK_API_KEY → 用 DeepSeek（OpenAI 兼容）做「文字判读/起草」
  （B超报告、显微镜/粪检报告等纯文本推理任务）。
- 否则回退到原豆包文本模型（WECOM_AGENT_MODEL > OPENAI_MODEL）。

注意：视觉类任务（TNR 审核、进货单识别）不走这里——DeepSeek 无视觉能力。
"""
from __future__ import annotations

from app.config import settings


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
        model = (settings.deepseek_model or "").strip() or "deepseek-chat"
        is_reasoner = ("reason" in model.lower()) or ("r1" in model.lower())
        return AsyncOpenAI(api_key=dk, base_url=base), model, "deepseek", is_reasoner

    base = (settings.openai_base_url or "").strip() or None
    model = (getattr(settings, "wecom_agent_model", "") or "").strip() \
        or (settings.openai_model or "gpt-4o-mini")
    is_reasoner = ("reason" in model.lower()) or ("think" in model.lower())
    return AsyncOpenAI(api_key=settings.openai_api_key, base_url=base), model, "doubao", is_reasoner
