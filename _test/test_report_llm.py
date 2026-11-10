import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services.report_llm import (
    REPORT_LLM_MAX_RETRIES,
    REPORT_LLM_TIMEOUT,
    _parse_json_object,
    generate_json_object,
    llm_call_error,
    report_text_client_model,
)


class _FakeCompletions:
    def __init__(self, contents):
        self.contents = list(contents)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self.contents.pop(0)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
        )


class _FakeClient:
    def __init__(self, contents):
        self.chat = SimpleNamespace(completions=_FakeCompletions(contents))


class ReportLlmTests(unittest.IsolatedAsyncioTestCase):
    def test_medical_model_uses_v4_pro_over_legacy_model(self):
        with patch("app.services.report_llm.settings.deepseek_api_key", "test-key"), \
             patch("app.services.report_llm.settings.deepseek_report_model", "deepseek-v4-pro"), \
             patch("app.services.report_llm.settings.deepseek_model", "deepseek-chat"):
            _, model, provider, is_reasoner = report_text_client_model()
        self.assertEqual(model, "deepseek-v4-pro")
        self.assertEqual(provider, "deepseek")
        self.assertTrue(is_reasoner)

    def test_report_client_has_a_finite_timeout(self):
        with patch("app.services.report_llm.settings.deepseek_api_key", "test-key"), \
             patch("app.services.report_llm.settings.deepseek_report_model", "deepseek-v4-pro"), \
             patch("openai.AsyncOpenAI") as client_cls:
            report_text_client_model()
        self.assertEqual(client_cls.call_args.kwargs["timeout"], REPORT_LLM_TIMEOUT)
        self.assertEqual(client_cls.call_args.kwargs["max_retries"], REPORT_LLM_MAX_RETRIES)
        self.assertEqual(REPORT_LLM_TIMEOUT.connect, 15.0)
        self.assertLessEqual(REPORT_LLM_TIMEOUT.read, 180.0)
        self.assertGreater(REPORT_LLM_TIMEOUT.read, 0)

    def test_fallback_client_uses_the_same_timeout(self):
        with patch("app.services.report_llm.settings.deepseek_api_key", ""), \
             patch("app.services.report_llm.settings.openai_api_key", "test-key"), \
             patch("app.services.report_llm.settings.openai_base_url", "https://example.invalid/v1"), \
             patch("app.services.report_llm.settings.openai_model", "doubao-test"), \
             patch("app.services.report_llm.settings.wecom_agent_model", ""), \
             patch("openai.AsyncOpenAI") as client_cls:
            _, model, provider, _ = report_text_client_model()
        self.assertEqual(provider, "doubao")
        self.assertEqual(model, "doubao-test")
        self.assertEqual(client_cls.call_args.kwargs["timeout"], REPORT_LLM_TIMEOUT)
        self.assertEqual(client_cls.call_args.kwargs["max_retries"], 0)

    def test_timeout_error_is_a_retry_message(self):
        self.assertEqual(llm_call_error(TimeoutError()), "模型响应超时，请稍后重新生成")
        self.assertIn("调用模型失败", llm_call_error(RuntimeError("boom")))

    def test_json_parser_accepts_fence_and_short_wrapper(self):
        self.assertEqual(_parse_json_object("```json\n{\"ok\": true}\n```"), {"ok": True})
        self.assertEqual(_parse_json_object("结果如下：{\"ok\": true}"), {"ok": True})

    async def test_json_generation_retries_after_malformed_response(self):
        client = _FakeClient(["{\"findings\": \"未闭合\"", "{\"findings\":\"正常\",\"conclusion\":\"正常\"}"])
        with patch("app.services.report_llm.asyncio.sleep", return_value=None):
            data, raw, error = await generate_json_object(
                client=client,
                model="deepseek-v4-pro",
                messages=[{"role": "user", "content": "生成报告"}],
                max_tokens=1000,
                task="test_report",
            )
        self.assertEqual(data["findings"], "正常")
        self.assertIn("conclusion", raw)
        self.assertEqual(error, "")
        self.assertEqual(len(client.chat.completions.calls), 2)


if __name__ == "__main__":
    unittest.main()
