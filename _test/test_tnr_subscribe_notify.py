"""TNR 订阅消息：授权台账、代约带 openid、改约/取消、手术前提醒。不调用微信。"""

import os
import sys
import tempfile
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-subscribe-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["UPLOAD_DIR"] = str(Path(TEMP_DIR.name) / "uploads")
os.environ["SESSION_SECRET"] = "tnr-subscribe-test"
os.environ["SESSION_HTTPS_ONLY"] = "0"
os.environ["WECHAT_APPID"] = "wx-test"
os.environ["WECHAT_APPSECRET"] = "secret-test"
os.environ["WECHAT_TMPL_APPLICATION_RESULT"] = "tmpl-result"
os.environ["WECHAT_TMPL_REJECTION"] = "tmpl-reject"
os.environ["WECHAT_TMPL_PENDING_MANUAL"] = "tmpl-manual"
os.environ["WECHAT_TMPL_APPOINTMENT"] = "tmpl-appt"
os.environ["WECHAT_TMPL_SURGERY_DONE"] = "tmpl-done"
os.environ["WECHAT_TMPL_SURGERY_REMINDER"] = "tmpl-remind"

from fastapi.testclient import TestClient
from passlib.hash import bcrypt

from app.config import settings
from app.database import Base, SessionLocal, engine
import app.main as main_module
import app.services.wechat_miniapp as wxmini
import app.services.wechat_subscribe as sub
from app.main import _staff_token_hash, app
from app.models import (
    AdminUser,
    Application,
    Appointment,
    AppointmentStatus,
    Customer,
    NotificationLog,
    Pet,
    WechatSubscribeGrant,
)
from app.time_utils import beijing_today


@asynccontextmanager
async def _test_lifespan(_app):
    yield


app.router.lifespan_context = _test_lifespan
sub.start_surgery_reminder_scheduler = lambda: None
Base.metadata.create_all(bind=engine)

settings.wechat_appid = "wx-test"
settings.wechat_appsecret = "secret-test"
settings.wechat_tmpl_application_result = "tmpl-result"
settings.wechat_tmpl_rejection = "tmpl-reject"
settings.wechat_tmpl_pending_manual = "tmpl-manual"
settings.wechat_tmpl_appointment = "tmpl-appt"
settings.wechat_tmpl_surgery_done = "tmpl-done"
settings.wechat_tmpl_surgery_reminder = "tmpl-remind"

DH = "大风动物医院（东环店）"


def _fake_wechat(code: str) -> dict:
    if isinstance(code, str) and code.startswith("as-"):
        return {"openid": code[3:]}
    raise RuntimeError("bad code")


main_module.wechat_code2session = _fake_wechat
OPENID = "openid-subscribe-test"
SENDS: list[dict] = []


def _fake_send(payload):
    SENDS.append(payload)
    return {"errcode": 0, "errmsg": "ok"}


_ORIGINAL_SEND = wxmini._post_subscribe_send


def _fake_43101(_payload):
    raise RuntimeError("subscribe send failed: {'errcode': 43101, 'errmsg': 'user refuse'}")


wxmini._post_subscribe_send = _fake_send
try:
    import app.services.wecom_notify as wecom_notify
    wecom_notify.notify_appointment_created = lambda *_args, **_kwargs: None
except Exception:
    pass


def _day(offset: int = 2) -> str:
    return (beijing_today() + timedelta(days=offset)).isoformat()


def _application(db, nickname="小花", status="approved", openid=OPENID, phone="13800002222") -> Application:
    row = Application(
        applicant_name="测试申请人",
        phone=phone,
        address="深圳",
        cat_nickname=nickname,
        cat_gender="female",
        clinic_store=DH,
        status=status,
        wechat_openid=openid,
        id_number="440301199001011234",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _logs(db, event_type: str) -> list[NotificationLog]:
    return (
        db.query(NotificationLog)
        .filter(NotificationLog.event_type == event_type)
        .order_by(NotificationLog.id)
        .all()
    )


def test_grant_ledger_accept_reject_and_43101():
    db = SessionLocal()
    try:
        saved = sub.record_subscribe_results(
            db, OPENID,
            {"tmpl-result": "accept", "tmpl-reject": "reject", "tmpl-manual": "accept"},
            scene="apply",
        )
        db.commit()
        assert len(saved) == 3
        result = db.query(WechatSubscribeGrant).filter_by(openid=OPENID, template_id="tmpl-result").one()
        assert result.available_count == 1
        assert result.last_status == "accept"
        rejected = db.query(WechatSubscribeGrant).filter_by(openid=OPENID, template_id="tmpl-reject").one()
        assert rejected.available_count == 0
        assert rejected.last_status == "reject"
        assert sub.is_unauthorized(OPENID, db.query(WechatSubscribeGrant).filter_by(openid=OPENID).all()) is False

        app_row = _application(db)
        before = len(SENDS)
        ok = wxmini.push_application_result(
            db, application_id=app_row.id, openid=OPENID,
            applicant_name="测试申请人", status_text="审核已通过",
        )
        assert ok is True
        assert len(SENDS) == before + 1
        db.refresh(result)
        assert result.available_count == 0
        again = wxmini.push_application_result(
            db, application_id=app_row.id, openid=OPENID,
            applicant_name="测试申请人", status_text="审核已通过",
        )
        assert again is False
        assert len(SENDS) == before + 1

        wxmini._post_subscribe_send = _fake_43101
        try:
            failed = wxmini.push_pending_manual_notice(
                db, application_id=app_row.id, openid=OPENID, applicant_name="测试申请人",
            )
        finally:
            wxmini._post_subscribe_send = _fake_send
        assert failed is False
        manual = db.query(WechatSubscribeGrant).filter_by(openid=OPENID, template_id="tmpl-manual").one()
        assert manual.available_count == 0
        assert manual.last_status == "unauthorized"
        failure = _logs(db, "pending_manual_notice")[-1]
        assert failure.success is False
        assert failure.event_type == "pending_manual_notice"
        assert '"type": "pending_manual_notice"' in failure.payload
    finally:
        db.close()


def test_token_invalid_refreshes_once():
    calls = []

    class FakeResponse:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    class FakeClient:
        def __init__(self, timeout=8.0):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url, params=None):
            calls.append("token")
            return FakeResponse({"access_token": f"tok-{len(calls)}", "expires_in": 7200})

        def post(self, url, params=None, json=None):
            calls.append(params.get("access_token"))
            if len([item for item in calls if str(item).startswith("tok-")]) == 1:
                return FakeResponse({"errcode": 40001, "errmsg": "invalid credential"})
            return FakeResponse({"errcode": 0, "errmsg": "ok"})

    wxmini._token_cache.access_token = ""
    wxmini._token_cache.expires_at = 0
    original = wxmini.httpx.Client
    wxmini.httpx.Client = FakeClient
    wxmini._post_subscribe_send = _ORIGINAL_SEND
    try:
        data = wxmini._post_subscribe_send({"touser": "o", "template_id": "t", "data": {}})
    finally:
        wxmini.httpx.Client = original
        wxmini._post_subscribe_send = _fake_send
        wxmini._token_cache.access_token = ""
        wxmini._token_cache.expires_at = 0
    assert data["errcode"] == 0, calls
    tokens = [item for item in calls if str(item).startswith("tok-")]
    assert tokens == ["tok-1", "tok-3"], calls


def test_surgery_reminder_uses_application_openid_once():
    db = SessionLocal()
    try:
        app_row = _application(db, nickname="提醒猫", phone="13800003333", openid="openid-reminder")
        day = _day(1)
        row = Appointment(
            category="tnr",
            status=AppointmentStatus.confirmed.value,
            service_name="TNR 手术安排",
            customer_name="测试申请人",
            phone="13800003333",
            pet_name="提醒猫",
            store=DH,
            appointment_date=day,
            appointment_time="13:00",
            wechat_openid="",
            related_application_id=app_row.id,
            source="admin",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        before = len(SENDS)
        sent = sub.run_surgery_reminders(db, today=beijing_today())
        assert sent == 1
        assert len(SENDS) == before + 1
        assert SENDS[-1]["touser"] == "openid-reminder"
        assert SENDS[-1]["template_id"] == "tmpl-remind"
        db.refresh(row)
        assert row.wechat_openid == "openid-reminder"
        logs = _logs(db, "surgery_reminder_day_before")
        assert logs and logs[-1].success is True
        assert logs[-1].appointment_id == row.id
        again = sub.run_surgery_reminders(db, today=beijing_today())
        assert again == 0
        assert len(SENDS) == before + 1
    finally:
        db.close()


def test_customer_cancel_and_update_push_without_duplicate():
    db = SessionLocal()
    try:
        app_row = _application(db, nickname="取消猫", phone="13800004444", openid="openid-cancel")
        client = TestClient(app)
        created = client.post("/api/appointments/create", json={
            "code": "as-openid-cancel",
            "category": "tnr",
            "service_name": "TNR 手术安排",
            "customer_name": "测试申请人",
            "phone": "13800004444",
            "pet_name": "取消猫",
            "pet_gender": "female",
            "store": DH,
            "appointment_date": _day(3),
            "appointment_time": "13:00",
            "related_application_id": app_row.id,
            "duration_minutes": 60,
        })
        assert created.status_code == 200, created.text
        appointment_id = created.json()["appointment"]["id"]
        confirm_logs = [item for item in SENDS if item["template_id"] == "tmpl-appt" and item["touser"] == "openid-cancel"]
        assert len(confirm_logs) == 1
        db.refresh(app_row)
        assert app_row.status == "scheduled"

        pending = db.get(Appointment, appointment_id)
        pending.status = AppointmentStatus.pending.value
        db.commit()
        updated = client.post(f"/api/wechat/appointments/{appointment_id}/update", json={
            "code": "as-openid-cancel",
            "appointment_date": _day(4),
            "appointment_time": "14:00",
            "store": DH,
        })
        assert updated.status_code == 200, updated.text
        reschedule_logs = _logs(db, "appointment_status")
        assert any("appointment_rescheduled" in (item.payload or "") and item.success for item in reschedule_logs)

        cancelled = client.post(f"/api/wechat/appointments/{appointment_id}/cancel", json={"code": "as-openid-cancel"})
        assert cancelled.status_code == 200, cancelled.text
        cancel_logs = [item for item in _logs(db, "appointment_status") if "appointment_cancelled" in (item.payload or "")]
        assert len(cancel_logs) == 1 and cancel_logs[0].success is True
        again = client.post(f"/api/wechat/appointments/{appointment_id}/cancel", json={"code": "as-openid-cancel"})
        assert again.status_code == 400
        assert len([item for item in _logs(db, "appointment_status") if "appointment_cancelled" in (item.payload or "")]) == 1
    finally:
        db.close()


def test_staff_and_admin_proxy_carry_openid_and_schedule():
    db = SessionLocal()
    try:
        phone = "13800005555"
        app_row = _application(db, nickname="代约猫", phone=phone, openid="openid-proxy", status="approved")
        customer = Customer(name="测试申请人", phone=phone, wechat_openid="")
        db.add(customer)
        db.flush()
        pet = Pet(customer_id=customer.id, name="代约猫", species="cat", gender="female", store="东环店")
        db.add(pet)
        user = AdminUser(
            username="proxy_staff",
            password_hash=bcrypt.hash("test123456"),
            role="staff",
            store="东环店",
            is_active=True,
            miniapp_token_hash=_staff_token_hash("staff-token-proxy"),
            miniapp_token_created_at=main_module.datetime.utcnow(),
        )
        db.add(user)
        db.commit()
        client = TestClient(app)
        created = client.post("/api/staff-miniapp/appointments", json={
            "customer_id": customer.id,
            "pet_id": pet.id,
            "category": "tnr",
            "service_name": "TNR 手术安排",
            "appointment_date": _day(5),
            "appointment_time": "13:30",
            "duration_minutes": 60,
        }, headers={"Authorization": "Bearer staff-token-proxy"})
        assert created.status_code == 200, created.text
        appointment_id = created.json()["appointment"]["id"]
        row = db.get(Appointment, appointment_id)
        db.refresh(app_row)
        assert row.wechat_openid == "openid-proxy"
        assert row.related_application_id == app_row.id
        assert app_row.status == "scheduled"
        assert any(item["touser"] == "openid-proxy" and item["template_id"] == "tmpl-appt" for item in SENDS)

        admin = AdminUser(
            username="proxy_admin",
            password_hash=bcrypt.hash("test123456"),
            role="superadmin",
            store="",
            is_active=True,
        )
        db.add(admin)
        other = _application(db, nickname="后台猫", phone="13800006666", openid="openid-admin", status="pre_approved")
        db.commit()
        page = client.get("/admin/login")
        import re
        token = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text).group(1)
        login = client.post("/admin/login", data={
            "username": "proxy_admin",
            "password": "test123456",
            "csrf_token": token,
        }, follow_redirects=False)
        assert login.status_code == 303, login.text
        form = client.get("/admin/appointments/create")
        csrf = re.search(r'name="csrf_token"\s+value="([^"]+)"', form.text).group(1)
        saved = client.post("/admin/appointments/create", data={
            "csrf_token": csrf,
            "category": "tnr",
            "service_name": "TNR 手术安排",
            "customer_name": "测试申请人",
            "phone": "13800006666",
            "pet_name": "后台猫",
            "pet_gender": "female",
            "store": DH,
            "appointment_date": _day(6),
            "appointment_time": "15:00",
            "duration_minutes": "60",
            "related_application_id": str(other.id),
            "no_archive": "1",
        }, follow_redirects=False)
        assert saved.status_code == 303, saved.headers.get("location", "")
        admin_row = (
            db.query(Appointment)
            .filter(Appointment.phone == "13800006666", Appointment.pet_name == "后台猫")
            .one()
        )
        db.refresh(other)
        assert admin_row.wechat_openid == "openid-admin"
        assert other.status == "scheduled"
        assert any(item["touser"] == "openid-admin" for item in SENDS)
    finally:
        db.close()


def test_claim_and_report_link_openid_and_status_page_flags():
    db = SessionLocal()
    original_code = main_module.wechat_code2session
    try:
        app_row = _application(db, nickname="历史猫", phone="13800007777", openid="", status="approved")
        app_row.wechat_openid = ""
        row = Appointment(
            category="tnr",
            status=AppointmentStatus.confirmed.value,
            service_name="TNR 手术安排",
            customer_name="测试申请人",
            phone="13800007777",
            pet_name="历史猫",
            store=DH,
            appointment_date=_day(7),
            appointment_time="13:00",
            wechat_openid="",
            related_application_id=app_row.id,
            source="admin",
        )
        db.add(row)
        db.commit()
        client = TestClient(app)
        main_module.wechat_code2session = lambda code: {"openid": "openid-claimed"} if code == "claim-code" else original_code(code)
        spoofed = client.post("/api/wechat/claim-apps", json={
            "openid": "openid-attacker",
            "phone": "13800007777",
            "id_number": "440301199001011234",
        })
        assert spoofed.status_code == 401, spoofed.text
        db.refresh(app_row)
        assert (app_row.wechat_openid or "") == ""
        claimed = client.post("/api/wechat/claim-apps", json={
            "code": "claim-code",
            "openid": "openid-attacker",
            "phone": "13800007777",
            "id_number": "440301199001011234",
        })
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["updated"] == 1
        assert claimed.json()["appointments_linked"] == 1
        db.refresh(row)
        assert row.wechat_openid == "openid-claimed"

        reported = client.post("/api/wechat/subscribe-report", json={
            "openid": "openid-claimed",
            "scene": "status",
            "results": {"tmpl-appt": "accept", "tmpl-remind": "ban"},
            "main_switch": "on",
        })
        assert reported.status_code == 200, reported.text
        body = reported.json()["status"]
        assert body["needs_prompt"] is True
        assert body["open_settings"] is True
        sub.record_subscribe_results(
            db, "openid-claimed",
            {"tmpl-result": "accept", "tmpl-reject": "accept", "tmpl-manual": "accept", "tmpl-done": "accept"},
            scene="status",
        )
        # ban 仍然挡住总判断
        grants = db.query(WechatSubscribeGrant).filter_by(openid="openid-claimed").all()
        assert sub.is_unauthorized("openid-claimed", grants) is True
        for grant in grants:
            if grant.template_id == "tmpl-remind":
                grant.last_status = "accept"
                grant.available_count = 1
        db.commit()
        grants = db.query(WechatSubscribeGrant).filter_by(openid="openid-claimed").all()
        assert sub.is_unauthorized("openid-claimed", grants) is False

        sub.annotate_notify_flags(db, [app_row, row])
        assert app_row.notify_unauthorized is False
        failed = wxmini.push_surgery_done(
            db, application_id=app_row.id, openid="openid-claimed", cat_name="历史猫",
        )
        assert failed is True
        wxmini._post_subscribe_send = _fake_43101
        try:
            wxmini.push_surgery_done(
                db, application_id=app_row.id, openid="openid-claimed", cat_name="历史猫",
                event_key="surgery_done_retry_probe",
            )
        finally:
            wxmini._post_subscribe_send = _fake_send
        sub.annotate_notify_flags(db, [app_row])
        assert app_row.notify_last_failed is True
        assert "上次推送失败" in app_row.notify_alert
    finally:
        main_module.wechat_code2session = original_code
        db.close()


def test_wecom_proxy_links_application_openid():
    db = SessionLocal()
    try:
        phone = "13800008888"
        app_row = _application(db, nickname="企微猫", phone=phone, openid="openid-wecom", status="approved")
        customer = Customer(name="企微客户", phone=phone)
        db.add(customer)
        db.flush()
        pet = Pet(customer_id=customer.id, name="企微猫", species="cat", gender="female", store="东环店")
        db.add(pet)
        db.commit()
        from app.services.wecom_agent import _execute_create_appointment
        text = _execute_create_appointment(db, "nobody", {
            "category": "tnr",
            "service_name": "TNR 手术安排",
            "customer_name": "企微客户",
            "phone": phone,
            "pet_name": "企微猫",
            "pet_gender": "female",
            "store": DH,
            "appointment_date": _day(8),
            "appointment_time": "16:00",
            "duration_minutes": 60,
            "customer_id": customer.id,
            "pet_id": pet.id,
            "notes": "",
        })
        assert "已创建" in text
        row = db.query(Appointment).filter(Appointment.phone == phone, Appointment.pet_name == "企微猫").one()
        assert row.wechat_openid == "openid-wecom"
        assert row.related_application_id == app_row.id
        assert row.status == AppointmentStatus.pending.value
        db.refresh(app_row)
        assert app_row.status == "approved"
        assert any(item["touser"] == "openid-wecom" for item in SENDS)
    finally:
        db.close()


if __name__ == "__main__":
    test_grant_ledger_accept_reject_and_43101()
    test_token_invalid_refreshes_once()
    test_surgery_reminder_uses_application_openid_once()
    test_customer_cancel_and_update_push_without_duplicate()
    test_staff_and_admin_proxy_carry_openid_and_schedule()
    test_claim_and_report_link_openid_and_status_page_flags()
    test_wecom_proxy_links_application_openid()
    print("ok")
