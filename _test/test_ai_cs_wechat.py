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
from app.models import AdminUser, AiCsHandoff, AiCsHandoffSession, AiCsMessage, AiCsPending, Appointment, Customer, Pet
from app.services import ai_cs_assistant, wechat_cs_crypto, wechat_cs_push, wechat_miniapp
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
        assert ai_cs_assistant.BIND_CARD_ASK in reply
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


def _plant_session(openid: str, minutes_ago: float) -> int:
    db = SessionLocal()
    try:
        now = ai_cs_assistant.beijing_naive()
        when = now - timedelta(minutes=minutes_ago)
        row = AiCsHandoffSession(
            openid=openid,
            started_at=when,
            last_customer_msg_at=when,
            ended_at=None,
            end_reason="",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    finally:
        db.close()


def _session_state(openid: str):
    db = SessionLocal()
    try:
        row = (
            db.query(AiCsHandoffSession)
            .filter(AiCsHandoffSession.openid == openid)
            .order_by(AiCsHandoffSession.id.desc())
            .first()
        )
        if row is None:
            return None
        return {
            "id": row.id,
            "ended_at": row.ended_at,
            "end_reason": row.end_reason or "",
            "last": row.last_customer_msg_at,
        }
    finally:
        db.close()


def _handoff_rows(openid: str):
    db = SessionLocal()
    try:
        rows = (
            db.query(AiCsHandoff)
            .filter(AiCsHandoff.openid == openid)
            .order_by(AiCsHandoff.id.asc())
            .all()
        )
        return [
            (row.reason, row.method, row.staff_notify_status, row.note or "")
            for row in rows
        ]
    finally:
        db.close()


def _messages(openid: str):
    db = SessionLocal()
    try:
        rows = (
            db.query(AiCsMessage)
            .filter(AiCsMessage.openid == openid)
            .order_by(AiCsMessage.id.asc())
            .all()
        )
        return [(row.role, row.msg_type, row.content or "") for row in rows]
    finally:
        db.close()


@contextmanager
def capture_send():
    sent = []
    original = wechat_miniapp.send_customer_service_text

    def fake(openid, content):
        sent.append((openid, content))
        return {"errcode": 0}

    wechat_miniapp.send_customer_service_text = fake
    try:
        yield sent
    finally:
        wechat_miniapp.send_customer_service_text = original


def _login(http, username="admin", password="test123456"):
    page = http.get("/admin/login")
    logged = http.post("/admin/login", data={
        "username": username,
        "password": password,
        "csrf_token": csrf_from(page.text),
    }, follow_redirects=False)
    assert logged.status_code == 303
    return http


def _push(http, openid, content, msg_id, msg_type="text", title=""):
    payload = {
        "ToUserName": "gh_test",
        "FromUserName": openid,
        "CreateTime": 1710000000 + int(msg_id),
        "MsgType": msg_type,
        "MsgId": msg_id,
    }
    if msg_type == "text":
        payload["Content"] = content
    elif title:
        payload["Title"] = title
    return _post_json(http, payload, f"n{msg_id}")


def test_queued_followups_transfer_without_ai():
    http = client()
    jobs = []

    async def capture(job):
        jobs.append(job)

    original = wechat_cs_push.run_cs_job
    wechat_cs_push.run_cs_job = capture
    try:
        with env(WECHAT_CS_TOKEN=TOKEN, AI_CS_ENABLED="1", AI_CS_TEST_OPENIDS="cs-queue"):
            first = _push(http, "cs-queue", "人工", 9401)
            second = _push(http, "cs-queue", "那我的预约呢", 9402)
            image = _push(http, "cs-queue", "", 9403, msg_type="image")
    finally:
        wechat_cs_push.run_cs_job = original
    assert first.json()["MsgType"] == "transfer_customer_service"
    assert second.json()["MsgType"] == "transfer_customer_service"
    assert image.json()["MsgType"] == "transfer_customer_service"
    assert jobs == []
    state = _session_state("cs-queue")
    assert state is not None and state["ended_at"] is None and state["end_reason"] == ""
    handoffs = _handoff_rows("cs-queue")
    assert len(handoffs) == 1
    assert handoffs[0][0] == "customer_requested"
    assert handoffs[0][1] == "wechat_transfer"
    assert handoffs[0][2] == "pending_step5"
    logged = _messages("cs-queue")
    assert ("user", "handoff", "人工") in logged
    assert any(row[2] == "那我的预约呢" for row in logged)
    assert any("图片" in row[2] for row in logged)
    assert all(row[0] != "assistant" for row in logged)


def test_handoff_idle_expiry_and_resume_notice():
    _plant_session("cs-idle-fresh", 10)
    _plant_session("cs-idle-stale", 45)
    calls = {"n": 0}

    async def answer(messages, tools):
        calls["n"] += 1
        last = messages[-1]["content"]
        if "第一句" in last:
            return _Msg(content="明天下午可以")
        return _Msg(content="好的，还有空")

    original_job = wechat_cs_push.run_cs_job
    jobs = []

    async def capture(job):
        jobs.append(job)

    ai_cs_assistant.complete_override = answer
    try:
        with env(
            WECHAT_CS_TOKEN=TOKEN,
            AI_CS_ENABLED="1",
            AI_CS_TEST_OPENIDS="cs-idle-fresh,cs-idle-stale",
            AI_CS_HANDOFF_IDLE_MINUTES="30",
        ):
            assert ai_cs_assistant.handoff_idle_minutes() == 30
            http = client()
            wechat_cs_push.run_cs_job = capture
            held = _push(http, "cs-idle-fresh", "还在排队吗", 9501)
            assert held.json()["MsgType"] == "transfer_customer_service"
            assert jobs == [] and calls["n"] == 0
            fresh = _session_state("cs-idle-fresh")
            assert fresh["ended_at"] is None
            wechat_cs_push.run_cs_job = original_job
            with capture_send() as sent:
                first = _push(http, "cs-idle-stale", "第一句想继续", 9502)
                assert first.status_code == 200 and first.text == "success"
                assert calls["n"] == 1
                assert sent[0][0] == "cs-idle-stale"
                assert sent[0][1].startswith(ai_cs_assistant.RESUME_NOTE)
                assert "明天下午可以" in sent[0][1]
                assert "人工" in sent[0][1]
                second = _push(http, "cs-idle-stale", "再问一句", 9503)
                assert second.text == "success"
                assert calls["n"] == 2
                assert "又回来了" not in sent[1][1]
                assert "好的，还有空" in sent[1][1]
    finally:
        wechat_cs_push.run_cs_job = original_job
        ai_cs_assistant.complete_override = None
    stale = _session_state("cs-idle-stale")
    assert stale["end_reason"] == "idle" and stale["ended_at"] is not None
    assert _session_state("cs-idle-fresh")["ended_at"] is None


def test_handoff_idle_window_follows_env():
    with env(AI_CS_HANDOFF_IDLE_MINUTES="abc"):
        assert ai_cs_assistant.handoff_idle_minutes() == 30
    with env(AI_CS_HANDOFF_IDLE_MINUTES="0"):
        assert ai_cs_assistant.handoff_idle_minutes() == 30
    _plant_session("cs-idle-short", 3)
    _plant_session("cs-idle-long", 8)
    original = wechat_cs_push.run_cs_job
    jobs = []

    async def capture(job):
        jobs.append(job)

    wechat_cs_push.run_cs_job = capture
    try:
        with env(
            WECHAT_CS_TOKEN=TOKEN,
            AI_CS_ENABLED="1",
            AI_CS_TEST_OPENIDS="cs-idle-short,cs-idle-long",
            AI_CS_HANDOFF_IDLE_MINUTES="5",
        ):
            assert ai_cs_assistant.handoff_idle_minutes() == 5
            http = client()
            short = _push(http, "cs-idle-short", "还没到点", 9511)
            long = _push(http, "cs-idle-long", "已经过了", 9512)
    finally:
        wechat_cs_push.run_cs_job = original
    assert short.json()["MsgType"] == "transfer_customer_service"
    assert long.text == "success"
    assert len(jobs) == 1 and jobs[0]["openid"] == "cs-idle-long" and jobs[0]["text"] == "已经过了"
    assert _session_state("cs-idle-short")["ended_at"] is None
    assert _session_state("cs-idle-long")["end_reason"] == "idle"


def test_admin_page_hides_idle_sessions():
    ensure_admin()
    _plant_session("cs-idle-page", 45)
    http = _login(client())
    with env(AI_CS_HANDOFF_IDLE_MINUTES="30"):
        page = http.get("/admin/ai-cs")
    assert page.status_code == 200
    assert "cs-idle-page" not in page.text
    assert _session_state("cs-idle-page")["end_reason"] == "idle"


def test_staff_end_button_restores_ai():
    ensure_admin()
    db = SessionLocal()
    try:
        if db.query(AdminUser).filter(AdminUser.username == "cs-staff").first() is None:
            db.add(AdminUser(
                username="cs-staff",
                password_hash=bcrypt.hash("test123456"),
                role="staff",
                store="东环店",
                is_active=True,
            ))
            db.commit()
    finally:
        db.close()
    desktop_id = _plant_session("cs-staff-desktop", 1)
    mobile_id = _plant_session("cs-staff-mobile", 1)
    anon = client()
    denied = anon.post(
        f"/admin/ai-cs/sessions/{desktop_id}/end",
        data={"csrf_token": "nope"},
        follow_redirects=False,
    )
    assert denied.status_code == 303 and "login" in denied.headers.get("location", "")
    assert _session_state("cs-staff-desktop")["ended_at"] is None

    http = _login(client(), "cs-staff")
    page = http.get("/admin/ai-cs")
    assert page.status_code == 200
    assert "正在人工接待" in page.text
    assert "cs-staff-desktop" in page.text
    assert "结束人工、恢复AI" in page.text
    assert f"/admin/ai-cs/sessions/{desktop_id}/end" in page.text
    token = csrf_from(page.text)
    bad = http.post(
        f"/admin/ai-cs/sessions/{desktop_id}/end",
        data={"csrf_token": "x" * len(token)},
        follow_redirects=False,
    )
    assert bad.status_code == 403
    assert _session_state("cs-staff-desktop")["ended_at"] is None
    done = http.post(
        f"/admin/ai-cs/sessions/{desktop_id}/end",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert done.status_code == 303
    assert "ai-cs" in done.headers.get("location", "")
    assert _session_state("cs-staff-desktop")["end_reason"] == "staff"
    again = http.get("/admin/ai-cs")
    assert f"/admin/ai-cs/sessions/{desktop_id}/end" not in again.text
    assert "已结束人工，AI客服已恢复" in again.text or "已结束人工" in http.get(done.headers["location"]).text

    mobile = http.get("/m/ai-cs")
    assert mobile.status_code == 200
    assert "cs-staff-mobile" in mobile.text
    assert f"/m/ai-cs/sessions/{mobile_id}/end" in mobile.text
    ended = http.post(
        f"/m/ai-cs/sessions/{mobile_id}/end",
        data={"csrf_token": csrf_from(mobile.text)},
        follow_redirects=False,
    )
    assert ended.status_code == 303
    assert _session_state("cs-staff-mobile")["end_reason"] == "staff"

    calls = {"n": 0}

    async def answer(messages, tools):
        calls["n"] += 1
        return _Msg(content="我来继续帮您看时间")

    ai_cs_assistant.complete_override = answer
    try:
        with env(WECHAT_CS_TOKEN=TOKEN, AI_CS_ENABLED="1", AI_CS_TEST_OPENIDS="cs-staff-desktop"):
            with capture_send() as sent:
                resumed = _push(client(), "cs-staff-desktop", "那现在还能约吗", 9601)
    finally:
        ai_cs_assistant.complete_override = None
    assert resumed.text == "success"
    assert calls["n"] == 1
    assert sent and sent[0][1].startswith(ai_cs_assistant.RESUME_NOTE)
    assert "我来继续帮您看时间" in sent[0][1]
    assert _session_state("cs-staff-desktop")["end_reason"] == "staff"


def test_low_confidence_opens_transfer_session():
    calls = {"n": 0}

    async def empty(messages, tools):
        calls["n"] += 1
        return _Msg(content="")

    ai_cs_assistant.complete_override = empty
    try:
        with env(WECHAT_CS_TOKEN=TOKEN, AI_CS_ENABLED="1", AI_CS_TEST_OPENIDS="cs-low"):
            with capture_send() as sent:
                http = client()
                first = _push(http, "cs-low", "这个病严重吗", 9701)
                assert first.text == "success"
                assert calls["n"] == 1
                assert sent and "17820633031" in sent[0][1]
                follow = _push(http, "cs-low", "那你转给同事了吗", 9702)
    finally:
        ai_cs_assistant.complete_override = None
    assert follow.json()["MsgType"] == "transfer_customer_service"
    assert calls["n"] == 1
    assert len(sent) == 1
    handoffs = _handoff_rows("cs-low")
    assert len(handoffs) == 1
    assert handoffs[0][0] == "low_confidence"
    assert handoffs[0][1] == "wechat_transfer"
    assert handoffs[0][2] == "pending_step5"
    assert "模型没有给出可靠回复" in handoffs[0][3]
    state = _session_state("cs-low")
    assert state["ended_at"] is None
    logged = _messages("cs-low")
    assert any(row[0] == "assistant" and "交给同事" in row[2] for row in logged)


def test_request_human_exhaustion_and_llm_error_transfer():
    db = SessionLocal()
    try:
        async def ask_human(messages, tools):
            return _Msg(tool_calls=[_Call("request_human", {"reason": "知识库没有"})])

        async def always_tool(messages, tools):
            return _Msg(tool_calls=[_Call("list_services", {}, call_id="hop")])

        async def broken(messages, tools):
            raise RuntimeError("down")

        async def false_success(messages, tools):
            return _Msg(content="已经帮您约好了")

        ai_cs_assistant.complete_override = ask_human
        asked = asyncio.run(ai_cs_assistant.handle_user_text(db, "cs-ask-human", "手术多少钱"))
        ai_cs_assistant.complete_override = always_tool
        tired = asyncio.run(ai_cs_assistant.handle_user_text(db, "cs-tool-hops", "帮我查一下"))
        ai_cs_assistant.complete_override = broken
        failed = asyncio.run(ai_cs_assistant.handle_user_text(db, "cs-llm-down", "在吗"))
        ai_cs_assistant.complete_override = false_success
        lied = asyncio.run(ai_cs_assistant.handle_user_text(db, "cs-false-ok", "就这样"))
    finally:
        ai_cs_assistant.complete_override = None
        db.close()

    for reply in (asked, tired, failed, lied):
        assert "交给同事" in reply
    ask_rows = _handoff_rows("cs-ask-human")
    assert ask_rows[0][0] == "知识库没有"
    assert ask_rows[0][1] == "wechat_transfer"
    assert ask_rows[0][2] == "pending_step5"
    assert _session_state("cs-ask-human")["ended_at"] is None
    tired_rows = _handoff_rows("cs-tool-hops")
    assert tired_rows[0][0] == "low_confidence"
    assert tired_rows[0][1] == "wechat_transfer"
    assert "工具轮次用尽" in tired_rows[0][3]
    assert _session_state("cs-tool-hops")["ended_at"] is None
    failed_rows = _handoff_rows("cs-llm-down")
    assert failed_rows[0][0] == "llm_unavailable"
    assert failed_rows[0][1] == "wechat_transfer"
    assert "RuntimeError" in failed_rows[0][3]
    assert _session_state("cs-llm-down")["ended_at"] is None
    lied_rows = _handoff_rows("cs-false-ok")
    assert lied_rows[0][0] == "low_confidence" and lied_rows[0][1] == "wechat_transfer"

    calls = {"n": 0}

    async def boom(db_session, openid, text):
        calls["n"] += 1
        raise RuntimeError("boom")

    original_text = ai_cs_assistant.handle_user_text
    ai_cs_assistant.handle_user_text = boom
    try:
        with capture_send() as sent:
            asyncio.run(wechat_cs_push.run_cs_job({
                "openid": "cs-job-err", "kind": "text", "text": "你好",
            }))
        with env(WECHAT_CS_TOKEN=TOKEN, AI_CS_ENABLED="1", AI_CS_TEST_OPENIDS="cs-job-err"):
            follow = _push(client(), "cs-job-err", "人呢", 9802)
    finally:
        ai_cs_assistant.handle_user_text = original_text
    assert calls["n"] == 1
    assert sent and "17820633031" in sent[0][1]
    assert follow.json()["MsgType"] == "transfer_customer_service"
    job_rows = _handoff_rows("cs-job-err")
    assert job_rows[0][0] == "job_error"
    assert job_rows[0][1] == "wechat_transfer"
    assert job_rows[0][2] == "pending_step5"
    assert _session_state("cs-job-err")["ended_at"] is None


def test_non_whitelisted_and_disabled_do_not_use_sessions():
    http = client()
    jobs = []

    async def capture(job):
        jobs.append(job)

    planted = ai_cs_assistant.beijing_naive() - timedelta(minutes=1)
    db = SessionLocal()
    try:
        db.add(AiCsHandoffSession(
            openid="cs-outsider-planted",
            started_at=planted,
            last_customer_msg_at=planted,
            ended_at=None,
            end_reason="",
        ))
        db.commit()
    finally:
        db.close()

    original = wechat_cs_push.run_cs_job
    wechat_cs_push.run_cs_job = capture
    try:
        with env(WECHAT_CS_TOKEN=TOKEN, AI_CS_ENABLED="1", AI_CS_TEST_OPENIDS="cs-allowed-only"):
            human = _push(http, "cs-outsider", "我要人工", 9901)
            follow = _push(http, "cs-outsider", "还想问价格", 9902)
            planted_msg = _push(http, "cs-outsider-planted", "你好呀", 9903)
        with env(WECHAT_CS_TOKEN=TOKEN, AI_CS_ENABLED="0", AI_CS_TEST_OPENIDS="cs-ai-off"):
            off_human = _push(http, "cs-ai-off", "转人工", 9904)
            off_next = _push(http, "cs-ai-off", "在吗", 9905)
    finally:
        wechat_cs_push.run_cs_job = original

    for response in (human, follow, planted_msg, off_human, off_next):
        assert response.json()["MsgType"] == "transfer_customer_service"
    assert jobs == []
    assert _session_state("cs-outsider") is None
    outsider = _handoff_rows("cs-outsider")
    assert len(outsider) == 1
    assert outsider[0][1] == "wechat_transfer"
    assert outsider[0][0] == "customer_requested"
    gated = _messages("cs-outsider")
    assert len(gated) == 1
    assert "没有交给" in gated[0][2]
    assert "还想问价格" not in gated[0][2]
    planted_state = _session_state("cs-outsider-planted")
    assert planted_state["ended_at"] is None and planted_state["end_reason"] == ""
    assert abs((planted_state["last"] - planted).total_seconds()) < 5
    assert _session_state("cs-ai-off") is None
    off_rows = _handoff_rows("cs-ai-off")
    assert len(off_rows) == 1 and off_rows[0][1] == "wechat_transfer"
    assert _messages("cs-ai-off") == []


def test_handoff_session_migration_is_additive():
    from sqlalchemy import text

    from app.database import _try_sqlite_migrations

    _try_sqlite_migrations()
    _try_sqlite_migrations()
    with engine.connect() as conn:
        cols = {row[1] for row in conn.execute(text("PRAGMA table_info(ai_cs_handoff_sessions)"))}
    assert {"openid", "started_at", "last_customer_msg_at", "ended_at", "end_reason"} <= cols
