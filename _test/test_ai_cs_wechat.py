"""小程序客服消息推送：验签、解密、去重、确认后才写预约、未绑定和白名单。"""

import asyncio
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="ai-cs-wechat-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["UPLOAD_DIR"] = str(Path(TEMP_DIR.name) / "uploads")
os.environ["SESSION_SECRET"] = "ai-cs-wechat-test"
os.environ["SESSION_HTTPS_ONLY"] = "0"
os.environ["AI_CS_API_KEY"] = "test-ai-cs-key"

from fastapi.testclient import TestClient
from passlib.hash import bcrypt

from app.database import Base, SessionLocal, engine
import app.main as main_module
from app.main import app
from app.models import AdminUser, AiCsHandoff, AiCsMessage, AiCsPending, Appointment, Customer, Pet
from app.services import ai_cs_assistant, wechat_cs_crypto, wechat_cs_push
from app.services.ai_cs_assistant import load_knowledge
from app.time_utils import beijing_today


@asynccontextmanager
async def _test_lifespan(_app):
    yield


app.router.lifespan_context = _test_lifespan
Base.metadata.create_all(bind=engine)

TOKEN = "test-token"
AES_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode().rstrip("=")
APPID = "wxd1918a78b050310d"
DAY = (beijing_today() + timedelta(days=12)).isoformat()


def wx_sign(*parts: str) -> str:
    raw = "".join(sorted(str(part) for part in parts))
    return hashlib.sha1(raw.encode()).hexdigest()


@contextmanager
def env(**kwargs):
    previous = {}
    for key, value in kwargs.items():
        previous[key] = os.environ.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def client():
    return TestClient(app)


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    if not match:
        match = re.search(r'value="([^"]+)"[^>]*name="csrf_token"', html)
    assert match, html[:500]
    return match.group(1)


def ensure_admin():
    db = SessionLocal()
    try:
        row = db.query(AdminUser).filter(AdminUser.username == "admin").first()
        if row is None:
            db.add(AdminUser(
                username="admin",
                password_hash=bcrypt.hash("test123456"),
                role="superadmin",
                store="",
                is_active=True,
            ))
            db.commit()
    finally:
        db.close()


class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _Call:
    def __init__(self, name, args, call_id="call-1"):
        self.id = call_id
        self.function = _Fn(name, json.dumps(args, ensure_ascii=False))


class _Msg:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


def test_plain_and_secure_signatures_roundtrip():
    assert wx_sign(TOKEN, "1710000000", "abc") == wechat_cs_crypto.plain_signature(TOKEN, "1710000000", "abc")
    cipher = wechat_cs_crypto.encrypt_text("ping", AES_KEY, APPID)
    assert wechat_cs_crypto.decrypt_text(cipher, AES_KEY, APPID) == "ping"
    try:
        wechat_cs_crypto.decrypt_text(cipher, AES_KEY, "wx-other")
        raise AssertionError("appid mismatch should fail")
    except wechat_cs_crypto.WeChatCsCryptoError as exc:
        assert "AppId" in str(exc)
    bad = wx_sign("other-token", "1710000000", "abc")
    assert not wechat_cs_crypto.signatures_match(
        wechat_cs_crypto.plain_signature(TOKEN, "1710000000", "abc"), bad,
    )


def test_url_verify_plain_secure_and_bad_signature():
    http = client()
    with env(WECHAT_CS_TOKEN=TOKEN, WECHAT_CS_AES_KEY=AES_KEY, WECHAT_APPID=APPID, AI_CS_ENABLED="0"):
        ok = http.get("/api/wechat/cs-push", params={
            "signature": wx_sign(TOKEN, "1710000000", "n1"),
            "timestamp": "1710000000",
            "nonce": "n1",
            "echostr": "echo-plain",
        })
        assert ok.status_code == 200 and ok.text == "echo-plain"
        denied = http.get("/api/wechat/cs-push", params={
            "signature": "0" * 40,
            "timestamp": "1710000000",
            "nonce": "n1",
            "echostr": "echo-plain",
        })
        assert denied.status_code == 403
        cipher = wechat_cs_crypto.encrypt_text("echo-secure", AES_KEY, APPID)
        secure = http.get("/api/wechat/cs-push", params={
            "signature": wx_sign(TOKEN, "1710000000", "n2"),
            "msg_signature": wx_sign(TOKEN, "1710000000", "n2", cipher),
            "timestamp": "1710000000",
            "nonce": "n2",
            "echostr": cipher,
            "encrypt_type": "aes",
        })
        assert secure.status_code == 200 and secure.text == "echo-secure"


def _post_json(http, payload, nonce, token=TOKEN):
    ts = "1710000000"
    return http.post(
        "/api/wechat/cs-push",
        params={"signature": wx_sign(token, ts, nonce), "timestamp": ts, "nonce": nonce},
        json=payload,
    )


def test_dedupe_and_secure_xml_and_whitelist():
    http = client()
    jobs = []

    async def capture(job):
        jobs.append(job)

    original = wechat_cs_push.run_cs_job
    wechat_cs_push.run_cs_job = capture
    try:
        with env(WECHAT_CS_TOKEN=TOKEN, WECHAT_CS_AES_KEY=AES_KEY, WECHAT_APPID=APPID,
                 AI_CS_ENABLED="1", AI_CS_TEST_OPENIDS=""):
            body = {
                "ToUserName": "gh_test",
                "FromUserName": "openid-dedupe",
                "CreateTime": 1710000001,
                "MsgType": "text",
                "Content": "你好",
                "MsgId": 9001,
            }
            first = _post_json(http, body, "n3")
            second = _post_json(http, body, "n4")
            assert first.status_code == 200 and first.text == "success"
            assert second.status_code == 200 and second.text == "success"
            assert len(jobs) == 1 and jobs[0]["text"] == "你好"

            inner = (
                "<xml><ToUserName><![CDATA[gh_test]]></ToUserName>"
                "<FromUserName><![CDATA[openid-secure]]></FromUserName>"
                "<CreateTime>1710000002</CreateTime><MsgType><![CDATA[text]]></MsgType>"
                "<Content><![CDATA[加密你好]]></Content><MsgId>9002</MsgId></xml>"
            )
            cipher = wechat_cs_crypto.encrypt_text(inner, AES_KEY, APPID)
            envelope = (
                "<xml><ToUserName><![CDATA[gh_test]]></ToUserName>"
                f"<Encrypt><![CDATA[{cipher}]]></Encrypt></xml>"
            )
            ts, nonce = "1710000000", "n5"
            secure = http.post(
                "/api/wechat/cs-push",
                params={
                    "msg_signature": wx_sign(TOKEN, ts, nonce, cipher),
                    "timestamp": ts,
                    "nonce": nonce,
                    "encrypt_type": "aes",
                },
                content=envelope.encode(),
                headers={"content-type": "text/xml"},
            )
            assert secure.status_code == 200 and secure.text == "success"
            assert jobs[-1]["openid"] == "openid-secure" and jobs[-1]["text"] == "加密你好"
        with env(WECHAT_CS_TOKEN=TOKEN, AI_CS_ENABLED="1", AI_CS_TEST_OPENIDS="only-this"):
            gated = _post_json(http, {
                "ToUserName": "gh_test", "FromUserName": "someone-else",
                "CreateTime": 1710000003, "MsgType": "text", "Content": "我要预约", "MsgId": 9003,
            }, "n6")
            assert gated.status_code == 200
            assert gated.json()["MsgType"] == "transfer_customer_service"
            allowed = _post_json(http, {
                "ToUserName": "gh_test", "FromUserName": "only-this",
                "CreateTime": 1710000004, "MsgType": "text", "Content": "只有我", "MsgId": 9004,
            }, "n7")
            assert allowed.text == "success"
        with env(WECHAT_CS_TOKEN=TOKEN, AI_CS_ENABLED="0", AI_CS_TEST_OPENIDS=""):
            off = _post_json(http, {
                "ToUserName": "gh_test", "FromUserName": "only-this",
                "CreateTime": 1710000005, "MsgType": "text", "Content": "关闭了", "MsgId": 9005,
            }, "n8")
            assert off.json()["MsgType"] == "transfer_customer_service"
            human = _post_json(http, {
                "ToUserName": "gh_test", "FromUserName": "only-this",
                "CreateTime": 1710000006, "MsgType": "text", "Content": "转人工", "MsgId": 9006,
            }, "n9")
            assert human.json()["MsgType"] == "transfer_customer_service"
    finally:
        wechat_cs_push.run_cs_job = original

    db = SessionLocal()
    try:
        handoff = db.query(AiCsHandoff).filter(AiCsHandoff.openid == "only-this").one()
        assert handoff.method == "wechat_transfer"
        assert handoff.staff_notify_status == "pending_step5"
        gated_row = db.query(AiCsMessage).filter(AiCsMessage.openid == "someone-else").one()
        assert "没有交给" in gated_row.content
        assert "我要预约" not in gated_row.content
    finally:
        db.close()
    assert all(job["openid"] != "someone-else" for job in jobs)
    assert any(job["openid"] == "only-this" and job["text"] == "只有我" for job in jobs)


def _seed_customer(openid, name, phone, pets):
    db = SessionLocal()
    try:
        existing = db.query(Customer).filter(Customer.wechat_openid == openid).all()
        if existing:
            return existing[0].id, [pet.id for pet in db.query(Pet).filter(Pet.customer_id == existing[0].id).all()]
        row = Customer(name=name, phone=phone, wechat_openid=openid)
        db.add(row)
        db.flush()
        pet_ids = []
        for pet_name, species in pets:
            pet = Pet(
                customer_id=row.id, name=pet_name, species=species, gender="male",
                birthday_estimate="2020-01-01", store="东环店",
            )
            db.add(pet)
            db.flush()
            pet_ids.append(pet.id)
        db.commit()
        return row.id, pet_ids
    finally:
        db.close()


def test_unbound_multiple_pets_and_confirm_before_booking():
    text = load_knowledge()
    assert "尚未提供" in text
    assert "只讲猫" in text or "只按猫" in text
    assert "8周龄以上的犬" in text

    db = SessionLocal()
    try:
        reply = asyncio.run(ai_cs_assistant.handle_user_text(db, "cs-unbound", "我想预约疫苗"))
        assert "绑定我的档案" in reply
        assert db.query(Appointment).filter(Appointment.wechat_openid == "cs-unbound").count() == 0

        owner, pet_ids = _seed_customer("cs-multi", "多宠", "13900007771", [("大黄", "dog"), ("小白", "cat")])
        asked = ai_cs_assistant.execute_tool(db, "cs-multi", "stage_booking", {
            "store": "东环店", "date": DAY, "time": "10:00", "kind": "outpatient", "service_name": "皮肤",
        })
        assert asked["ok"] is False and asked["code"] == "need_pet"
        assert db.get(AiCsPending, "cs-multi") is None

        other = Customer(name="另一份", phone="13900007772", wechat_openid="cs-profiles")
        first = Customer(name="第一份", phone="13900007773", wechat_openid="cs-profiles")
        db.add_all([first, other])
        db.flush()
        db.add(Pet(customer_id=first.id, name="花花", species="cat", gender="female", birthday_estimate="2020-01-01"))
        db.commit()
        profiles = ai_cs_assistant.execute_tool(db, "cs-profiles", "stage_booking", {
            "store": "东环店", "date": DAY, "time": "10:30", "kind": "outpatient", "service_name": "口腔",
        })
        assert profiles["code"] == "need_profile"
        assert "13900007773" not in json.dumps(profiles, ensure_ascii=False)

        book_owner, book_pets = _seed_customer("cs-book", "约诊人", "13900007774", [("团子", "dog")])

        async def stage(messages, tools):
            return _Msg(tool_calls=[_Call("stage_booking", {
                "customer_id": book_owner,
                "pet_id": book_pets[0],
                "store": "东环店",
                "date": DAY,
                "time": "11:00",
                "kind": "outpatient",
                "service_name": "皮肤",
            })])

        ai_cs_assistant.complete_override = stage
        try:
            staged = asyncio.run(ai_cs_assistant.handle_user_text(db, "cs-book", "帮我约皮肤"))
        finally:
            ai_cs_assistant.complete_override = None
        assert "确认" in staged
        assert "团子" in staged and "东环店" in staged and "皮肤" in staged and DAY in staged
        assert db.query(Appointment).filter(Appointment.wechat_openid == "cs-book").count() == 0
        assert db.get(AiCsPending, "cs-book") is not None

        done = asyncio.run(ai_cs_assistant.handle_user_text(db, "cs-book", "确认"))
        made = db.query(Appointment).filter(Appointment.wechat_openid == "cs-book").all()
        assert len(made) == 1
        assert made[0].source == "ai_cs"
        assert made[0].service_name == "皮肤"
        assert made[0].appointment_date == DAY
        assert made[0].appointment_time == "11:00"
        assert "已经帮您约好" in done
        assert db.get(AiCsPending, "cs-book") is None

        stored = db.query(AiCsMessage).filter(AiCsMessage.openid == "cs-book", AiCsMessage.role == "user").all()
        blob = "\n".join(row.content for row in stored)
        assert "13900007774" not in blob
    finally:
        ai_cs_assistant.complete_override = None
        db.close()
    assert owner


def test_admin_pages_are_login_only():
    ensure_admin()
    db = SessionLocal()
    try:
        db.add(AiCsMessage(
            openid="cs-admin-view", role="user", content="138****0000 想问价",
            msg_type="text", created_at=ai_cs_assistant.beijing_naive(),
        ))
        db.commit()
    finally:
        db.close()
    anon = client()
    denied = anon.get("/admin/ai-cs", follow_redirects=False)
    assert denied.status_code == 303 and "login" in denied.headers.get("location", "")
    http = client()
    page = http.get("/admin/login")
    logged = http.post("/admin/login", data={
        "username": "admin",
        "password": "test123456",
        "csrf_token": csrf_from(page.text),
    }, follow_redirects=False)
    assert logged.status_code == 303
    shown = http.get("/admin/ai-cs")
    assert shown.status_code == 200
    assert "客服记录" in shown.text and "cs-admin-view" in shown.text
    mobile = http.get("/m/ai-cs")
    assert mobile.status_code == 200 and "客服记录" in mobile.text
