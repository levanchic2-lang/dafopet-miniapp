"""客服措辞：微信纯文本清洗、问候只在未绑定时提档案、提示词禁止 Markdown。"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="ai-cs-tone-")
os.environ["DATABASE_URL"] = f"sqlite:///{(Path(TEMP_DIR.name) / 'test.db').as_posix()}"
os.environ["UPLOAD_DIR"] = str(Path(TEMP_DIR.name) / "uploads")
os.environ["SESSION_SECRET"] = "ai-cs-tone"
os.environ["SESSION_HTTPS_ONLY"] = "0"
os.environ["AI_CS_API_KEY"] = "test-ai-cs-key"

from app.database import Base, SessionLocal, engine
import app.models  # noqa: F401
from app.models import Customer
from app.services import ai_cs_assistant, wechat_cs_push, wechat_miniapp

Base.metadata.create_all(bind=engine)


def test_sanitize_strips_markdown_and_keeps_breaks_and_numbers():
    raw = (
        "**今天 11/13 (周五)**\n"
        "17:30、18:00、18:30、19:00\n"
        "\n"
        "# 明天\n"
        "## 后天 11/15\n"
        "- 下午 15:00\n"
        "* 晚上 19:30\n"
        "1. 大米 (猫)\n"
        "2. 大橘 (猫)\n"
        "__短毛__ `5.2kg`\n"
        "| 体重 | 价格 |\n"
        "| --- | --- |\n"
        "| 5.2 | 155 |\n"
        "---\n"
        "单号 #18，电话 17820633031"
    )
    text = ai_cs_assistant.sanitize_wechat_text(raw)
    assert "**" not in text
    assert "__" not in text
    assert "`" not in text
    assert "#" not in text
    assert "|" not in text
    assert "---" not in text
    assert "今天 11/13 (周五)" in text
    assert "17:30、18:00、18:30、19:00" in text
    assert "明天" in text
    assert "后天 11/15" in text
    assert "下午 15:00" in text
    assert "晚上 19:30" in text
    assert "1. 大米 (猫)" in text
    assert "2. 大橘 (猫)" in text
    assert "短毛" in text
    assert "5.2kg" in text
    assert "5.2" in text and "155" in text
    assert "17820633031" in text
    assert "\n" in text
    assert text == ai_cs_assistant.sanitize_wechat_text(text)


def test_sanitize_keeps_plain_sentences():
    plain = "好的，5.2公斤的短毛猫，洗护大概要两个小时左右。想约哪家店呢？"
    assert ai_cs_assistant.sanitize_wechat_text(plain) == plain
    assert ai_cs_assistant.sanitize_wechat_text("") == ""
    assert ai_cs_assistant.sanitize_wechat_text("   ") == ""


def test_greeting_is_short_and_mentions_bind_only_when_unbound():
    greeting = ai_cs_assistant.GREETING
    unbound = ai_cs_assistant.GREETING_UNBOUND
    assert "人工" in greeting
    assert "绑定我的档案" not in greeting
    assert "很高兴为您服务" not in greeting
    assert "作为AI" not in greeting
    assert len(greeting) < 80
    assert unbound.startswith(greeting)
    assert "绑定我的档案" in unbound
    assert "人工" in ai_cs_assistant.RESUME_NOTE
    assert "又回来了" not in ai_cs_assistant.RESUME_NOTE
    assert "交给同事" in ai_cs_assistant.PHONE_FALLBACK
    assert "17820633031" in ai_cs_assistant.PHONE_FALLBACK
    assert "0755-28704990" in ai_cs_assistant.PHONE_FALLBACK
    assert "18026901718" in ai_cs_assistant.PHONE_FALLBACK
    assert "呼吸困难" in ai_cs_assistant.PHONE_FALLBACK

    db = SessionLocal()
    try:
        first = ai_cs_assistant.maybe_greet(db, "tone-open")
        assert first == unbound
        assert "绑定我的档案" in first
        assert ai_cs_assistant.maybe_greet(db, "tone-open") == ""

        db.add(Customer(name="已绑定", phone="13900001001", wechat_openid="tone-bound"))
        db.commit()
        bound = ai_cs_assistant.maybe_greet(db, "tone-bound")
        assert bound == greeting
        assert "绑定我的档案" not in bound
    finally:
        db.close()


def test_prompt_forbids_markdown_and_keeps_file_order():
    db = SessionLocal()
    try:
        prompt = ai_cs_assistant._system_prompt(db, "tone-nobody")
    finally:
        db.close()
    assert "不要使用 Markdown" in prompt
    assert ai_cs_assistant.SPEAKING_STYLE in prompt
    assert prompt.rfind("不要使用 Markdown") > prompt.find("【知识库】")
    assert "用于算价格档" in prompt
    assert "我先给您出确认稿" in prompt
    assert "我帮您确认一下" in prompt
    assert "3 到 4 个" in prompt
    assert "在线客服助手" in prompt
    assert prompt.find("先问是哪一只") < prompt.find("宠物确定之后") < prompt.find("只问还缺的")
    assert "早于 6 个月" in prompt
    assert "档案里大米是田园猫" in prompt
    assert "正好 5kg 是大型猫" in ai_cs_assistant.PET_FILE_RULE
    assert "2.5kg" not in ai_cs_assistant.PET_FILE_RULE
    knowledge = ai_cs_assistant.load_knowledge()
    assert "不要说「档案里大米是田园猫" in knowledge
    assert "只请客户确认" in knowledge


def test_reply_and_send_are_sanitized():
    db = SessionLocal()

    async def markdown(_messages, _tools):
        return type("Msg", (), {
            "content": "**今天 11/13 (周五)**\n17:30、18:00\n我先给您出确认稿。",
            "tool_calls": [],
        })()

    ai_cs_assistant.complete_override = markdown
    try:
        reply = asyncio.run(ai_cs_assistant.handle_user_text(db, "tone-md", "近三天哪个时间可以约"))
    finally:
        ai_cs_assistant.complete_override = None
        db.close()
    assert "**" not in reply
    assert "今天 11/13 (周五)" in reply
    assert "17:30、18:00" in reply

    async def raw(_db, _openid, _text):
        return "| 时间 | 可约 |\n| --- | --- |\n| 15:00 | 有 |\n# 标题"

    original = ai_cs_assistant.handle_user_text
    original_send = wechat_miniapp.send_customer_service_text
    sent = []

    def fake_send(openid, content):
        sent.append((openid, content))
        return {"errcode": 0}

    ai_cs_assistant.handle_user_text = raw
    wechat_miniapp.send_customer_service_text = fake_send
    try:
        asyncio.run(wechat_cs_push.run_cs_job({
            "openid": "tone-send", "kind": "text", "text": "有空吗",
        }))
    finally:
        ai_cs_assistant.handle_user_text = original
        wechat_miniapp.send_customer_service_text = original_send
    assert sent and sent[0][0] == "tone-send"
    body = sent[0][1]
    assert "**" not in body and "#" not in body and "|" not in body and "---" not in body
    assert "15:00" in body and "可约" in body and "有" in body


if __name__ == "__main__":
    test_sanitize_strips_markdown_and_keeps_breaks_and_numbers()
    test_sanitize_keeps_plain_sentences()
    test_greeting_is_short_and_mentions_bind_only_when_unbound()
    test_prompt_forbids_markdown_and_keeps_file_order()
    test_reply_and_send_are_sanitized()
    print("ai cs tone tests passed")
