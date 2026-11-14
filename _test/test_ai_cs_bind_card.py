"""未绑定客户：客服会话里发绑定卡片，失败改文字，30 分钟内不重复发。"""

import asyncio
import os
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="ai-cs-bind-card-")
os.environ["DATABASE_URL"] = f"sqlite:///{(Path(TEMP_DIR.name) / 'test.db').as_posix()}"
os.environ["UPLOAD_DIR"] = str(Path(TEMP_DIR.name) / "uploads")
os.environ["SESSION_SECRET"] = "ai-cs-bind-card"
os.environ["SESSION_HTTPS_ONLY"] = "0"
os.environ["AI_CS_API_KEY"] = "test-ai-cs-key"
os.environ.pop("AI_CS_BIND_CARD_THUMB_PATH", None)

from sqlalchemy import text

from app.database import Base, SessionLocal, engine, _try_sqlite_migrations
import app.models  # noqa: F401
from app.models import AiCsMediaCache, AiCsMessage, Customer
from app.services import ai_cs_assistant, wechat_cs_push, wechat_miniapp

Base.metadata.create_all(bind=engine)


class _Fn:
    def __init__(self, name, arguments="{}"):
        self.name = name
        self.arguments = arguments


class _Call:
    def __init__(self, name, arguments="{}"):
        self.id = "call-1"
        self.function = _Fn(name, arguments)


class _Msg:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _Patch:
    def __init__(self, **funcs):
        self.funcs = funcs
        self.saved = {}

    def __enter__(self):
        for name, func in self.funcs.items():
            self.saved[name] = getattr(wechat_miniapp, name)
            setattr(wechat_miniapp, name, func)
        return self

    def __exit__(self, *_args):
        for name, func in self.saved.items():
            setattr(wechat_miniapp, name, func)
        ai_cs_assistant.complete_override = None
        ai_cs_assistant.clear_bind_offer()
        os.environ.pop("AI_CS_BIND_CARD_THUMB_PATH", None)


def _sent_recorder():
    sent = []

    def send_text(openid, content):
        sent.append(("text", openid, content))
        return {"errcode": 0}

    def send_card(openid, *, title, pagepath, thumb_media_id):
        sent.append(("card", openid, title, pagepath, thumb_media_id))
        return {"errcode": 0}

    def upload(_path):
        sent.append(("upload", _path))
        return "media-cached"

    return sent, send_text, send_card, upload


def test_card_payload_and_upload_use_wechat_api():
    captured = {}

    class FakeResponse:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    class FakeClient:
        def __init__(self, timeout=8.0):
            captured["timeout"] = timeout

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def post(self, url, params=None, json=None, files=None):
            captured.setdefault("posts", []).append({
                "url": url, "params": params, "json": json, "files": files,
            })
            if "media/upload" in url:
                return FakeResponse({"type": "image", "media_id": "MEDIA-1", "created_at": 1})
            return FakeResponse({"errcode": 0, "errmsg": "ok"})

    original_client = wechat_miniapp.httpx.Client
    original_id = wechat_miniapp.settings.wechat_appid
    original_secret = wechat_miniapp.settings.wechat_appsecret
    wechat_miniapp.settings.wechat_appid = "wx-test"
    wechat_miniapp.settings.wechat_appsecret = "secret"
    wechat_miniapp._token_cache.access_token = "tok-ready"
    wechat_miniapp._token_cache.expires_at = time.time() + 3600
    wechat_miniapp.httpx.Client = FakeClient
    try:
        sent = wechat_miniapp.send_customer_service_miniprogram_page(
            "openid-card",
            title="点这里绑定我的档案",
            pagepath="pages/bind/bind?from=cs",
            thumb_media_id="MEDIA-1",
        )
        media = wechat_miniapp.upload_temp_image(str(ai_cs_assistant.bind_card_thumb_path()))
    finally:
        wechat_miniapp.httpx.Client = original_client
        wechat_miniapp.settings.wechat_appid = original_id
        wechat_miniapp.settings.wechat_appsecret = original_secret
        wechat_miniapp._token_cache.access_token = ""
        wechat_miniapp._token_cache.expires_at = 0
    assert sent["errcode"] == 0
    card_post = captured["posts"][0]
    assert card_post["url"].endswith("/cgi-bin/message/custom/send")
    assert card_post["params"]["access_token"] == "tok-ready"
    body = card_post["json"]
    assert body["touser"] == "openid-card"
    assert body["msgtype"] == "miniprogrampage"
    assert body["miniprogrampage"] == {
        "title": "点这里绑定我的档案",
        "pagepath": "pages/bind/bind?from=cs",
        "thumb_media_id": "MEDIA-1",
    }
    assert media == "MEDIA-1"
    upload_post = captured["posts"][1]
    assert "media/upload" in upload_post["url"]
    assert upload_post["params"]["type"] == "image"
    assert "media" in upload_post["files"]


def test_upload_or_card_failure_falls_back_without_breaking_reply():
    db = SessionLocal()
    sent, send_text, _send_card, _upload = _sent_recorder()

    def broken_upload(_path):
        raise RuntimeError("upload down")

    try:
        with _Patch(upload_temp_image=broken_upload, send_customer_service_text=send_text,
                    send_customer_service_miniprogram_page=_send_card):
            result = ai_cs_assistant.deliver_bind_card(db, "fallback-upload")
        assert result == "fallback"
        assert sent and sent[0][0] == "text"
        assert "绑定我的档案" in sent[0][2]
        assert "首页" in sent[0][2]
        logged = db.query(AiCsMessage).filter(AiCsMessage.openid == "fallback-upload").one()
        assert logged.msg_type == "bind_card"
        assert "绑定我的档案" in logged.content

        def broken_card(openid, *, title, pagepath, thumb_media_id):
            raise RuntimeError("45047 out of response count limit")

        sent.clear()
        with _Patch(upload_temp_image=lambda _path: "media-ok", send_customer_service_text=send_text,
                    send_customer_service_miniprogram_page=broken_card):
            again = ai_cs_assistant.deliver_bind_card(db, "fallback-send")
        assert again == "fallback"
        assert sent[0][0] == "text" and "绑定我的档案" in sent[0][2]

        def broken_text(openid, content):
            sent.append(("text", openid, content))
            if "卡片没发出去" in content:
                raise RuntimeError("text down")
            return {"errcode": 0}

        sent.clear()
        with _Patch(upload_temp_image=broken_upload, send_customer_service_text=broken_text,
                    send_customer_service_miniprogram_page=broken_card):
            reply = asyncio.run(wechat_cs_push.run_cs_job({
                "openid": "fallback-job", "kind": "text", "text": "我想预约疫苗",
            }))
        assert reply is None
        assert sent[0] == ("text", "fallback-job", sent[0][2])
        assert ai_cs_assistant.BIND_CARD_ASK in sent[0][2]
        assert any("卡片没发出去" in item[2] for item in sent if item[0] == "text")
        rows = db.query(AiCsMessage).filter(AiCsMessage.openid == "fallback-job").all()
        assert any(row.msg_type == "bind_card_error" for row in rows)
        assert any(row.msg_type == "unbound" and ai_cs_assistant.BIND_CARD_ASK in row.content for row in rows)
    finally:
        db.close()


def test_rate_limit_persists_across_sessions_and_thumb_refreshes():
    uploads = []

    def upload(path):
        uploads.append(path)
        return "media-" + str(len(uploads))

    db = SessionLocal()
    try:
        db.query(AiCsMediaCache).delete()
        db.commit()
        with _Patch(upload_temp_image=upload):
            first = ai_cs_assistant.prepare_bind_card_thumb(db)
            second = ai_cs_assistant.prepare_bind_card_thumb(db)
        assert first == second == "media-1"
        assert len(uploads) == 1
        cached = db.get(AiCsMediaCache, ai_cs_assistant.BIND_CARD_CACHE_KEY)
        assert cached.media_id == "media-1"
        cached.expires_at = ai_cs_assistant.beijing_naive() - timedelta(minutes=1)
        db.commit()
        with _Patch(upload_temp_image=upload):
            refreshed = ai_cs_assistant.prepare_bind_card_thumb(db)
        assert refreshed == "media-2"
        assert len(uploads) == 2

        sent, send_text, send_card, _upload = _sent_recorder()

        def counting_upload(path):
            uploads.append(path)
            return "media-live"

        before_uploads = len(uploads)
        with _Patch(upload_temp_image=counting_upload, send_customer_service_text=send_text,
                    send_customer_service_miniprogram_page=send_card):
            assert ai_cs_assistant.deliver_bind_card(db, "rate-user") == "card"
        assert [item[0] for item in sent] == ["card"]
        assert sent[0][2] == "点这里绑定我的档案"
        assert sent[0][3] == "pages/bind/bind?from=cs"
        assert sent[0][4] == "media-2"
        assert len(uploads) == before_uploads

        again = SessionLocal()
        try:
            with _Patch(upload_temp_image=counting_upload, send_customer_service_text=send_text,
                        send_customer_service_miniprogram_page=send_card):
                skipped = ai_cs_assistant.deliver_bind_card(again, "rate-user")
            assert skipped == "skipped_rate"
            assert [item[0] for item in sent] == ["card"]
            row = (
                again.query(AiCsMessage)
                .filter(AiCsMessage.openid == "rate-user", AiCsMessage.msg_type == "bind_card")
                .one()
            )
            row.created_at = ai_cs_assistant.beijing_naive() - timedelta(minutes=31)
            again.commit()
            sent.clear()
            with _Patch(upload_temp_image=counting_upload, send_customer_service_text=send_text,
                        send_customer_service_miniprogram_page=send_card):
                resent = ai_cs_assistant.deliver_bind_card(again, "rate-user")
            assert resent == "card"
            assert [item[0] for item in sent] == ["card"]
            assert sent[0][4] == "media-2"
        finally:
            again.close()
    finally:
        db.close()


def test_unbound_trigger_sends_card_and_next_message_sees_bind():
    sent, send_text, send_card, upload = _sent_recorder()

    async def hours(_messages, _tools):
        return _Msg("东环店和横岗店都看到晚上。")

    hops = {"n": 0}

    async def lookup_then_answer(_messages, _tools):
        hops["n"] += 1
        if hops["n"] == 1:
            return _Msg("", [_Call("list_upcoming_appointments")])
        return _Msg("我先对一下您的档案。")

    async def bound_answer(_messages, _tools):
        return _Msg("想约哪家店呀？")

    db = SessionLocal()
    try:
        prompt = ai_cs_assistant._system_prompt(db, "nobody-yet")
        assert ai_cs_assistant.BIND_CARD_ASK in prompt
        assert "打开小程序首页" not in prompt
        with _Patch(upload_temp_image=upload, send_customer_service_text=send_text,
                    send_customer_service_miniprogram_page=send_card):
            asyncio.run(wechat_cs_push.run_cs_job({
                "openid": "enter-user", "kind": "greeting", "text": "",
            }))
            kinds = [item[0] for item in sent]
            assert kinds.index("text") < kinds.index("card")
            text = next(item for item in sent if item[0] == "text")
            card = next(item for item in sent if item[0] == "card")
            assert ai_cs_assistant.BIND_CARD_ASK in text[2]
            assert card[3] == "pages/bind/bind?from=cs"
            sent.clear()
            asyncio.run(wechat_cs_push.run_cs_job({
                "openid": "enter-user", "kind": "greeting", "text": "",
            }))
            assert sent == []

            asyncio.run(wechat_cs_push.run_cs_job({
                "openid": "book-user", "kind": "text", "text": "我想预约疫苗",
            }))
            assert sent[0][0] == "text" and ai_cs_assistant.BIND_CARD_ASK in sent[0][2]
            assert sent[-1][0] == "card"
            sent.clear()
            asyncio.run(wechat_cs_push.run_cs_job({
                "openid": "book-user", "kind": "text", "text": "我的宠物呢",
            }))
            assert sent and sent[0][0] == "text"
            assert ai_cs_assistant.BIND_CARD_ASK in sent[0][2]
            assert all(item[0] != "card" for item in sent)

            ai_cs_assistant.complete_override = hours
            sent.clear()
            asyncio.run(wechat_cs_push.run_cs_job({
                "openid": "ask-user", "kind": "text", "text": "你们几点下班",
            }))
            assert [item[0] for item in sent] == ["text"]
            assert "晚上" in sent[0][2]
            assert "卡片" not in sent[0][2]

            ai_cs_assistant.complete_override = lookup_then_answer
            sent.clear()
            asyncio.run(wechat_cs_push.run_cs_job({
                "openid": "tool-user", "kind": "text", "text": "帮我看看下次驱虫",
            }))
            assert sent[0][0] == "text" and "档案" in sent[0][2]
            assert sent[-1][0] == "card"

            db.add(Customer(name="刚绑上", phone="13900006661", wechat_openid="book-user"))
            db.commit()
            ai_cs_assistant.complete_override = bound_answer
            sent.clear()
            asyncio.run(wechat_cs_push.run_cs_job({
                "openid": "book-user", "kind": "text", "text": "我想预约疫苗",
            }))
            assert [item[0] for item in sent] == ["text"]
            assert "想约哪家店" in sent[0][2]
            assert ai_cs_assistant.BIND_CARD_ASK not in sent[0][2]
    finally:
        ai_cs_assistant.complete_override = None
        db.close()


def test_bind_lookup_is_not_stale_inside_one_session():
    db = SessionLocal()
    try:
        row = Customer(name="待绑", phone="13900006662", wechat_openid="")
        db.add(row)
        db.commit()
        customer_id = row.id
        loaded = db.query(Customer).filter(Customer.id == customer_id).one()
        assert loaded.wechat_openid == ""
        other = SessionLocal()
        try:
            other.get(Customer, customer_id).wechat_openid = "just-bound"
            other.commit()
        finally:
            other.close()
        found = ai_cs_assistant.customers_for_openid(db, "just-bound")
        assert len(found) == 1
        assert found[0].id == customer_id
        assert found[0].wechat_openid == "just-bound"
        assert ai_cs_assistant.describe_profiles(db, "just-bound")["bound"] is True
    finally:
        db.close()


def test_home_and_bind_pages_expose_contact_buttons():
    home = (ROOT / "miniapp/pages/index/index.wxml").read_text(encoding="utf-8")
    assert home.count('open-type="contact"') == 3
    assert 'class="cs-float"' in home
    assert "在线客服" in home
    assert home.count('session-from="home"') == 3
    assert home.count('show-message-card="{{false}}"') == 3
    bind_wxml = (ROOT / "miniapp/pages/bind/bind.wxml").read_text(encoding="utf-8")
    bind_js = (ROOT / "miniapp/pages/bind/bind.js").read_text(encoding="utf-8")
    assert "回到客服" in bind_wxml
    assert 'open-type="contact"' in bind_wxml
    assert "绑定成功，可以回到客服继续聊" in bind_wxml
    assert "绑定成功，可以回到客服继续聊" in bind_js
    assert '=== "cs"' in bind_js
    assert "wx.reLaunch" in bind_js
    assert "wxLoginCode" in bind_js


def test_media_cache_migration_is_additive():
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS ai_cs_media_cache"))
    _try_sqlite_migrations()
    _try_sqlite_migrations()
    with engine.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(ai_cs_media_cache)"))}
    assert {"cache_key", "media_id", "source_stamp", "expires_at"} <= cols


if __name__ == "__main__":
    test_card_payload_and_upload_use_wechat_api()
    test_upload_or_card_failure_falls_back_without_breaking_reply()
    test_rate_limit_persists_across_sessions_and_thumb_refreshes()
    test_unbound_trigger_sends_card_and_next_message_sees_bind()
    test_bind_lookup_is_not_stale_inside_one_session()
    test_home_and_bind_pages_expose_contact_buttons()
    test_media_cache_migration_is_additive()
    print("ai cs bind card tests passed")
