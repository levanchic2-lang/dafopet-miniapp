import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.services.report_llm import (
    _parse_json_object,
    generate_json_object,
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
