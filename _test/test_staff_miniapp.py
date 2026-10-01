"""员工小程序登录、令牌和门店隔离回归测试。"""

import os
import sys
import tempfile
import base64
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-staff-miniapp-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
UPLOAD_DIR = Path(TEMP_DIR.name) / "uploads"
os.environ["UPLOAD_DIR"] = str(UPLOAD_DIR)

from fastapi.testclient import TestClient
from passlib.hash import bcrypt

from app import models  # noqa: F401
from app.database import Base, SessionLocal, engine
import app.main as main_module
from app.main import app
from app.services.anesthesia_dispatch import AUTO_CLOSE_NOTE, auto_close_stale_monitors
from app.services.inpatient_dispatch import OVERDUE_GRACE_MIN
from app.models import (
    AdminUser, AnesthesiaMedicationEvent, AnesthesiaMonitorEntry, AnesthesiaMonitorSheet,
    Application, Appointment, Coupon, Customer, CustomerPackage, Deposit,
    DewormingRecord, ExamOrder, ExamReport, InventoryBatch, InventoryItem, Invoice,
    Hospitalization, InpatientTemporaryMedication, MediaFile, MedicationAdminLog,
    Payment, Pet, Prescription, PrescriptionItem, Staff, Vaccination, Visit, Wallet,
)


@asynccontextmanager
async def _test_lifespan(_app):
    """接口回归不启动调度器，也不创建正式数据目录中的启动锁。"""
    yield


app.router.lifespan_context = _test_lifespan
Base.metadata.create_all(bind=engine)
assert main_module._default_schedule_for_freq("每日1次", 22, 1.0) == "22"
assert main_module._default_schedule_for_freq("每日2次", 22, 2.0) == "10,20"
assert main_module._default_schedule_for_freq("1.0", 9) == "9"
assert main_module._default_schedule_for_freq("2", 9) == "10,20"
assert OVERDUE_GRACE_MIN == 15
db = SessionLocal()
try:
    henggang = AdminUser(
        username="staff_hg", password_hash=bcrypt.hash("test123456"), role="staff",
        store="横岗店", display_name="横岗医生", mobile_role="doctor", is_active=True,
    )
    donghuan = AdminUser(
        username="staff_dh", password_hash=bcrypt.hash("test123456"), role="staff",
        store="东环店", display_name="东环员工", mobile_role="nurse", is_active=True,
    )
    hg_customer = Customer(name="横岗客户", phone="13900001111")
    dh_customer = Customer(name="东环客户", phone="13900002222")
    db.add_all([henggang, donghuan, hg_customer, dh_customer])
    db.flush()
    hg_pet = Pet(customer_id=hg_customer.id, name="横岗犬", species="dog", store="横岗店",
                 medical_record_no="HC26090001")
    dh_pet = Pet(customer_id=dh_customer.id, name="东环猫", species="cat", store="东环店")
    db.add_all([hg_pet, dh_pet])
    db.flush()
    today = datetime.now().date().isoformat()
    hg_visit = Visit(customer_id=hg_customer.id, pet_id=hg_pet.id, visit_date=today, store="横岗店",
                     chief_complaint="咳嗽", diagnosis="上呼吸道感染", vet_name="横岗医生")
    dh_visit = Visit(customer_id=dh_customer.id, pet_id=dh_pet.id, visit_date=today, store="东环店")
    db.add_all([
        hg_visit, dh_visit,
        Appointment(customer_id=hg_customer.id, pet_id=hg_pet.id, customer_name="横岗客户",
                    pet_name="横岗犬", appointment_date=today, appointment_time="23:59",
                    store="大风动物医院（横岗店）", status="confirmed"),
        Appointment(customer_id=dh_customer.id, pet_id=dh_pet.id, customer_name="东环客户",
                    pet_name="东环猫", appointment_date=today, appointment_time="11:00",
                    store="大风动物医院（东环店）", status="confirmed"),
    ])
    db.flush()
    db.add(Staff(name="横岗医生", store="横岗店", position="医生", status="active"))
    hg_hosp = Hospitalization(
        customer_id=hg_customer.id, pet_id=hg_pet.id, visit_id=hg_visit.id,
        store="横岗店", status="admitted", reason="测试住院",
        admitted_at=datetime.combine(datetime.now().date(), datetime.min.time()).replace(hour=3),
        staff_token="test-hg-staff", owner_token="test-hg-owner",
    )
    dh_hosp = Hospitalization(
        customer_id=dh_customer.id, pet_id=dh_pet.id, visit_id=dh_visit.id,
        store="东环店", status="admitted", reason="测试住院",
        staff_token="test-dh-staff", owner_token="test-dh-owner",
    )
    hg_drug = InventoryItem(
        name="横岗住院测试药", category="medication", is_service=False,
        unit="ml", stock_qty=20, sell_price=10, order_type="prescription",
        store="横岗店", is_active=True,
    )
    dh_drug = InventoryItem(
        name="东环住院测试药", category="medication", is_service=False,
        unit="ml", stock_qty=20, store="东环店", is_active=True,
    )
    hg_unified_exam = InventoryItem(
        name="横岗统一检查", category="lab", is_service=True,
        unit="次", stock_qty=0, sell_price=50, order_type="exam",
        store="横岗店", is_active=True,
    )
    db.add_all([hg_hosp, dh_hosp, hg_drug, dh_drug, hg_unified_exam])
    db.flush()
    hg_presc = Prescription(
        visit_id=hg_visit.id, customer_id=hg_customer.id, pet_id=hg_pet.id,
        prescribed_date=today, vet_name="横岗医生", status="issued",
        created_at=datetime.combine(datetime.now().date(), datetime.min.time()).replace(hour=4, minute=29),
    )
    dh_presc = Prescription(
        visit_id=dh_visit.id, customer_id=dh_customer.id, pet_id=dh_pet.id,
        prescribed_date=today, vet_name="东环医生", status="issued",
    )
    db.add_all([hg_presc, dh_presc]); db.flush()
    hg_pi = PrescriptionItem(
        prescription_id=hg_presc.id, item_id=hg_drug.id, drug_name=hg_drug.name,
        drug_type="静脉注射", dosage="0.5ml", dose_amount=0.5, dose_unit="ml",
        frequency="BID", duration_days="3", schedule_times="09:00,21:00",
    )
    dh_pi = PrescriptionItem(
        prescription_id=dh_presc.id, item_id=dh_drug.id, drug_name=dh_drug.name,
        dosage="1ml", frequency="QD", duration_days="1", schedule_times="10:00",
    )
    db.add_all([hg_pi, dh_pi]); db.flush()
    numeric_frequency_item = PrescriptionItem(
        prescription_id=hg_presc.id, item_id=hg_drug.id, drug_name="数字频次测试药",
        dosage="0.2ml", dose_amount=0.2, dose_unit="ml",
        frequency="每日1次", times_per_day=1.0, duration_days="1", schedule_times="",
    )
    db.add(numeric_frequency_item); db.flush()
    assert main_module._generate_med_logs_for_prescription(db, hg_presc) >= 1
    tomorrow = datetime.combine(datetime.now().date() + timedelta(days=1), datetime.min.time())
    hg_pi_pending = db.query(MedicationAdminLog).filter(
        MedicationAdminLog.prescription_item_id == hg_pi.id,
        MedicationAdminLog.status == "pending",
        MedicationAdminLog.scheduled_at < tomorrow,
    ).count()
    assert hg_pi_pending == 2, (
        f"BID first-day tasks={hg_pi_pending}, created_at={hg_presc.created_at}, "
        f"admitted_at={hg_hosp.admitted_at}"
    )
    assert db.query(MedicationAdminLog).filter_by(
        prescription_item_id=numeric_frequency_item.id, status="pending",
    ).count() == 1
    expected_pending = db.query(MedicationAdminLog).filter_by(
        prescription_id=hg_presc.id, status="pending",
    ).count()
    db.add(MedicationAdminLog(
        hospitalization_id=hg_hosp.id, prescription_id=hg_presc.id,
        prescription_item_id=999999, scheduled_at=datetime.now(), status="pending",
    ))
    db.flush()
    assert main_module._generate_med_logs_for_prescription(db, hg_presc) == expected_pending
    assert db.query(MedicationAdminLog).filter_by(
        prescription_id=hg_presc.id, status="pending",
    ).count() == expected_pending
    db.query(MedicationAdminLog).filter_by(prescription_id=hg_presc.id).delete(
        synchronize_session=False,
    )
    db.delete(numeric_frequency_item); db.flush()
    db.add_all([
        MedicationAdminLog(
            hospitalization_id=hg_hosp.id, prescription_id=hg_presc.id,
            prescription_item_id=hg_pi.id, scheduled_at=datetime.now() - timedelta(minutes=1),
        ),
        MedicationAdminLog(
            hospitalization_id=dh_hosp.id, prescription_id=dh_presc.id,
            prescription_item_id=dh_pi.id, scheduled_at=datetime.combine(datetime.now().date(), datetime.min.time()).replace(hour=10),
        ),
    ])
    db.flush()
    assert main_module._cancel_pending_medications_on_discharge(db, dh_hosp, "测试员工") == 1
    cancelled_log = db.query(MedicationAdminLog).filter_by(
        hospitalization_id=dh_hosp.id,
    ).one()
    assert cancelled_log.status == "cancelled"
    assert "办理出院自动取消" in cancelled_log.notes
    assert db.get(Prescription, dh_presc.id).status == "issued"
    assert db.query(MedicationAdminLog).filter_by(
        hospitalization_id=hg_hosp.id, status="pending",
    ).count() == 1
    report_dir = UPLOAD_DIR / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_path = report_dir / "hg-report.pdf"
    report_path.write_bytes(b"%PDF-1.4\n%%EOF")
    exam = ExamOrder(visit_id=hg_visit.id, items_json='[{"name":"血常规"}]', status="completed")
    invoice = Invoice(invoice_no="TEST-HG-1", customer_id=hg_customer.id, pet_id=hg_pet.id,
                      visit_id=hg_visit.id, invoice_date=today, total_amount=300, payment_status="partial",
                      store="横岗店")
    wallet = Wallet(customer_id=hg_customer.id, balance=500, lifetime_recharge=500)
    db.add_all([exam, invoice, wallet])
    db.flush()
    db.add_all([
        ExamReport(exam_order_id=exam.id, file_path=str(report_path), original_name="血常规.pdf",
                   file_type="pdf", item_label="血常规"),
        Payment(invoice_id=invoice.id, customer_id=hg_customer.id, amount=100, status="success", store="横岗店"),
        Vaccination(customer_id=hg_customer.id, pet_id=hg_pet.id, vaccine_name="狂犬疫苗",
                    vaccine_type="rabies", vaccinated_date=today, status="active"),
        DewormingRecord(customer_id=hg_customer.id, pet_id=hg_pet.id, product_name="驱虫药",
                        deworm_type="combo", deworm_date=today, status="active"),
        CustomerPackage(customer_id=hg_customer.id, pet_id=hg_pet.id, name="洗护卡",
                        total_uses=10, used_count=2, status="active", store="横岗店"),
        Deposit(customer_id=hg_customer.id, pet_id=hg_pet.id, amount=200, applied_amount=50,
                status="held", store="横岗店"),
        Coupon(code="TEST-HG-COUPON", customer_id=hg_customer.id, title="测试券",
               kind="cash", face_value=30, status="issued", store="横岗店"),
        AnesthesiaMonitorSheet(
            customer_id=hg_customer.id, pet_id=hg_pet.id,
            monitor_date="2026-01-01", start_time="10:00",
            status="open", store="横岗店", created_by="历史测试",
            created_at=datetime(2026, 1, 1, 2, 0),
            updated_at=datetime(2026, 1, 1, 2, 0),
        ),
    ])
    db.commit()
finally:
    db.close()

main_module.wechat_code2session = lambda code: {"openid": f"openid-{code}"}

try:
    with TestClient(app, base_url="https://testserver") as client:
        wrong = client.post("/api/staff-miniapp/login", json={
            "username": "staff_hg", "password": "wrong", "code": "hg",
        })
        assert wrong.status_code == 401

        login = client.post("/api/staff-miniapp/login", json={
            "username": "staff_hg", "password": "test123456", "code": "hg",
        })
        assert login.status_code == 200, login.text
        payload = login.json()
        token = payload["token"]
        assert payload["profile"]["store"] == "横岗店"
        headers = {"Authorization": f"Bearer {token}"}

        me = client.get("/api/staff-miniapp/me", headers=headers)
        assert me.status_code == 200
        assert me.json()["profile"]["display_name"] == "横岗医生"
        assert me.json()["profile"]["medication_reminder_configured"] is False

        dashboard = client.get("/api/staff-miniapp/dashboard", headers=headers)
        assert dashboard.status_code == 200, dashboard.text
        assert dashboard.json()["stats"]["appointments"] == 1
        assert dashboard.json()["stats"]["visits"] == 1
        assert "pending" not in dashboard.json()["stats"]
        assert dashboard.json()["tasks"] == []
        assert dashboard.json()["stats"]["anesthesia_open"] == 0
        assert dashboard.json()["stats"]["inpatient_med_due"] == 1
        assert dashboard.json()["next_appointment"]["pet_name"] == "横岗犬"

        reminders = client.get("/api/staff-miniapp/medication-reminders", headers=headers)
        assert reminders.status_code == 200, reminders.text
        assert reminders.json()["count"] == 1
        assert reminders.json()["animal_count"] == 1
        assert reminders.json()["groups"][0]["pet_name"] == "横岗犬"

        meds = client.get("/api/staff-miniapp/inpatient-medications", headers=headers)
        assert meds.status_code == 200, meds.text
        assert [row["pet_name"] for row in meds.json()["items"]] == ["横岗犬"]
        assert meds.json()["items"][0]["drug_name"] == "横岗住院测试药"
        assert [row["name"] for row in meds.json()["inventory"]] == ["横岗住院测试药"]
        med_id = meds.json()["items"][0]["id"]
        batch_completed = client.post(
            "/api/staff-miniapp/inpatient-medications/batch-check",
            json={"ids": [med_id]}, headers=headers,
        )
        assert batch_completed.status_code == 200, batch_completed.text
        assert batch_completed.json()["count"] == 1
        assert batch_completed.json()["items"][0]["dose_actual"] == "0.5ml"
        assert client.post(
            f"/api/staff-miniapp/inpatient-medications/{med_id}/uncheck", headers=headers,
        ).status_code == 200
        completed_med = client.post(
            f"/api/staff-miniapp/inpatient-medications/{med_id}/check",
            json={"dose_actual": "0.5ml"}, headers=headers,
        )
        assert completed_med.status_code == 200, completed_med.text
        assert completed_med.json()["item"]["status"] == "done"
        completed_view = client.get(
            "/api/staff-miniapp/inpatient-medications", params={"view": "completed"}, headers=headers,
        )
        assert [row["id"] for row in completed_view.json()["items"]] == [med_id]
        assert client.post(
            f"/api/staff-miniapp/inpatient-medications/{med_id}/uncheck", headers=headers,
        ).status_code == 200
        temp_created = client.post(
            "/api/staff-miniapp/inpatient-medications/temporary",
            json={
                "hospitalization_id": meds.json()["hospitalizations"][0]["id"],
                "inventory_item_id": meds.json()["inventory"][0]["id"],
                "dose_actual": "0.2ml", "route": "静脉注射",
                "ordered_by": "横岗医生", "notes": "先用后补",
            }, headers=headers,
        )
        assert temp_created.status_code == 200, temp_created.text
        temporary_view = client.get(
            "/api/staff-miniapp/inpatient-medications", params={"view": "temporary"}, headers=headers,
        )
        assert len(temporary_view.json()["temporary"]) == 1
        db = SessionLocal()
        try:
            # 临时用药只留证据，不提前扣库存；正式补处方时再统一扣减。
            assert db.query(InventoryItem).filter_by(name="横岗住院测试药").one().stock_qty == 20
            assert db.query(InpatientTemporaryMedication).count() == 1
            other_med_id = db.query(MedicationAdminLog.id).join(Hospitalization).filter(
                Hospitalization.store == "东环店",
            ).scalar()
        finally:
            db.close()
        assert client.post(
            f"/api/staff-miniapp/inpatient-medications/{other_med_id}/check",
            json={}, headers=headers,
        ).status_code == 403

        anesthesia_list = client.get("/api/staff-miniapp/anesthesia-monitors", headers=headers)
        assert anesthesia_list.status_code == 200, anesthesia_list.text
        assert anesthesia_list.json()["active"] == []
        assert auto_close_stale_monitors(datetime(2026, 1, 4, 2, 1)) == 1
        db = SessionLocal()
        try:
            stale = db.query(AnesthesiaMonitorSheet).filter_by(created_by="历史测试").one()
            assert stale.status == "closed"
            assert stale.recovery_status == "苏醒良好"
            assert stale.end_time == "10:00"
            assert AUTO_CLOSE_NOTE in stale.recovery_notes
        finally:
            db.close()
        closed_monitors = client.get(
            "/api/staff-miniapp/anesthesia-monitors", params={"view": "closed"}, headers=headers,
        )
        assert closed_monitors.status_code == 200, closed_monitors.text
        assert closed_monitors.json()["monitors"][0]["auto_closed"] is True
        assert closed_monitors.json()["monitors"][0]["recovery_status"] == "苏醒良好"

        calendar = client.get("/api/staff-miniapp/calendar", params={"start": today, "days": 3}, headers=headers)
        assert calendar.status_code == 200, calendar.text
        assert [row["pet_name"] for row in calendar.json()["appointments"]] == ["横岗犬"]
        assert calendar.json()["appointments"][0]["customer_id"]
        own_appointment_id = calendar.json()["appointments"][0]["id"]

        moved = client.post(
            f"/api/staff-miniapp/appointments/{own_appointment_id}/reschedule",
            json={"date": today, "time": "20:15"}, headers=headers,
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["appointment"]["time"] == "20:15"

        arrived = client.post(
            f"/api/staff-miniapp/appointments/{own_appointment_id}/status",
            json={"status": "arrived"}, headers=headers,
        )
        assert arrived.status_code == 200, arrived.text
        assert arrived.json()["appointment"]["status"] == "arrived"

        db = SessionLocal()
        try:
            other_appointment_id = (
                db.query(Appointment.id)
                .filter(Appointment.store == "大风动物医院（东环店）")
                .scalar()
            )
        finally:
            db.close()
        forbidden = client.post(
            f"/api/staff-miniapp/appointments/{other_appointment_id}/status",
            json={"status": "arrived"}, headers=headers,
        )
        assert forbidden.status_code == 403

        db = SessionLocal()
        try:
            hg_tnr = Application(
                applicant_name="横岗申请人", phone="13900001111", clinic_store="横岗店",
                address="深圳", cat_nickname="横岗TNR猫", cat_gender="female",
                status="scheduled",
            )
            dh_tnr = Application(
                applicant_name="东环申请人", phone="13900002222", clinic_store="东环店",
                address="深圳", cat_nickname="东环TNR猫", cat_gender="male",
                status="scheduled",
            )
            db.add_all([hg_tnr, dh_tnr])
            db.flush()
            image_dir = UPLOAD_DIR / str(hg_tnr.id)
            image_dir.mkdir(parents=True, exist_ok=True)
            image_path = image_dir / "application.png"
            png_bytes = base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
            )
            image_path.write_bytes(png_bytes)
            media = MediaFile(
                application_id=hg_tnr.id, kind="application_image",
                stored_path=str(image_path), original_name="application.png",
            )
            db.add_all([
                media,
                Appointment(
                    related_application_id=hg_tnr.id, category="tnr", status="confirmed",
                    service_name="TNR 手术安排", customer_name="横岗申请人", phone="13900001111",
                    pet_name="横岗TNR猫", appointment_date=today, appointment_time="12:00",
                    store="大风动物医院（横岗店）", duration_minutes=60,
                ),
                Appointment(
                    related_application_id=dh_tnr.id, category="tnr", status="confirmed",
                    service_name="TNR 手术安排", customer_name="东环申请人", phone="13900002222",
                    pet_name="东环TNR猫", appointment_date=today, appointment_time="13:00",
                    store="大风动物医院（东环店）", duration_minutes=60,
                ),
            ])
            db.commit()
            hg_tnr_id, dh_tnr_id, media_id = hg_tnr.id, dh_tnr.id, media.id
        finally:
            db.close()

        tnr_today = client.get("/api/staff-miniapp/tnr/today", headers=headers)
        assert tnr_today.status_code == 200, tnr_today.text
        assert [item["cat_name"] for item in tnr_today.json()["items"]] == ["横岗TNR猫"]
        assert tnr_today.json()["items"][0]["application_media_ids"] == [media_id]
        media_response = client.get(f"/api/staff-miniapp/tnr/media/{media_id}", headers=headers)
        assert media_response.status_code == 200
        cross_store_tnr = client.post(f"/api/staff-miniapp/tnr/{dh_tnr_id}/verify", headers=headers)
        assert cross_store_tnr.status_code == 403
        verified_tnr = client.post(f"/api/staff-miniapp/tnr/{hg_tnr_id}/verify", headers=headers)
        assert verified_tnr.status_code == 200, verified_tnr.text
        assert verified_tnr.json()["item"]["verified"] is True
        assert verified_tnr.json()["item"]["before_count"] == 1
        uploaded_tnr = client.post(
            f"/api/staff-miniapp/tnr/{hg_tnr_id}/upload",
            data={"kind": "after", "media_type": "image"},
            files={"file": ("after.png", png_bytes, "image/png")}, headers=headers,
        )
        assert uploaded_tnr.status_code == 200, uploaded_tnr.text
        assert uploaded_tnr.json()["item"]["after_count"] == 1
        assert uploaded_tnr.json()["item"]["can_complete"] is True
        completed_tnr = client.post(f"/api/staff-miniapp/tnr/{hg_tnr_id}/complete", headers=headers)
        assert completed_tnr.status_code == 200, completed_tnr.text
        assert completed_tnr.json()["item"]["completed"] is True
        db = SessionLocal()
        try:
            tnr_appointment = db.query(Appointment).filter(
                Appointment.related_application_id == hg_tnr_id,
            ).one()
            assert tnr_appointment.status == "completed"
        finally:
            db.close()

        own_customer = client.get("/api/staff-miniapp/customers", params={"q": "横岗"}, headers=headers).json()["items"][0]
        customer_detail = client.get(
            f"/api/staff-miniapp/customers/{own_customer['id']}", headers=headers,
        )
        assert customer_detail.status_code == 200, customer_detail.text
        assert customer_detail.json()["summary"]["wallet_balance"] == 500
        assert customer_detail.json()["summary"]["deposit_available"] == 150
        assert customer_detail.json()["summary"]["unpaid_total"] == 200
        assert customer_detail.json()["packages"][0]["remaining"] == 8
        own_pet_id = own_customer["pets"][0]["id"]
        pet_detail = client.get(f"/api/staff-miniapp/pets/{own_pet_id}", headers=headers)
        assert pet_detail.status_code == 200, pet_detail.text
        assert pet_detail.json()["visits"][0]["diagnosis"] == "上呼吸道感染"
        assert pet_detail.json()["reports"][0]["label"] == "血常规"
        assert pet_detail.json()["vaccinations"][0]["name"] == "狂犬疫苗"
        assert pet_detail.json()["dewormings"][0]["name"] == "驱虫药"
        report_id = pet_detail.json()["reports"][0]["id"]
        report_file = client.get(f"/api/staff-miniapp/reports/{report_id}/file", headers=headers)
        assert report_file.status_code == 200
        db = SessionLocal()
        try:
            other_customer_id = db.query(Customer.id).filter(Customer.name == "东环客户").scalar()
            other_pet_id = db.query(Pet.id).filter(Pet.name == "东环猫").scalar()
        finally:
            db.close()
        assert client.get(f"/api/staff-miniapp/customers/{other_customer_id}", headers=headers).status_code == 403
        assert client.get(f"/api/staff-miniapp/pets/{other_pet_id}", headers=headers).status_code == 403
        material_visits = client.get("/api/staff-miniapp/visit-materials", headers=headers)
        assert material_visits.status_code == 200, material_visits.text
        assert [row["pet_name"] for row in material_visits.json()["items"]] == ["横岗犬"]
        own_visit_id = material_visits.json()["items"][0]["id"]
        unified_context = client.get(
            f"/api/staff-miniapp/visits/{own_visit_id}/unified-order", headers=headers,
        )
        assert unified_context.status_code == 200, unified_context.text
        assert unified_context.json()["pet"]["name"] == "横岗犬"
        drug_search = client.get(
            "/api/staff-miniapp/unified-order/items", params={"q": "横岗住院测试药"}, headers=headers,
        )
        exam_search = client.get(
            "/api/staff-miniapp/unified-order/items", params={"q": "横岗统一检查"}, headers=headers,
        )
        assert drug_search.status_code == 200 and len(drug_search.json()["items"]) == 1
        assert exam_search.status_code == 200 and len(exam_search.json()["items"]) == 1
        drug_item = drug_search.json()["items"][0]
        exam_item = exam_search.json()["items"][0]
        db = SessionLocal()
        try:
            prescription_count_before = db.query(Prescription).filter_by(visit_id=own_visit_id).count()
            stock_before = float(db.get(InventoryItem, drug_item["id"]).stock_qty or 0)
        finally:
            db.close()
        mobile_order = client.post(
            f"/api/staff-miniapp/visits/{own_visit_id}/unified-order",
            json={
                "order_date": today, "vet_name": "横岗医生",
                "items": [
                    {"item_id": drug_item["id"], "order_type": "prescription", "quantity": 2,
                     "unit_price": 10, "drug_type": "intravenous", "dose_amount": 1,
                     "dose_unit": "ml", "times_per_day": 2, "duration_days": 1},
                    {"item_id": exam_item["id"], "order_type": "exam", "quantity": 1,
                     "unit_price": 50},
                ],
            }, headers=headers,
        )
        assert mobile_order.status_code == 200, mobile_order.text
        assert mobile_order.json()["batch_id"] > 0
        db = SessionLocal()
        try:
            assert db.query(Prescription).filter_by(visit_id=own_visit_id).count() == prescription_count_before + 1
            assert db.query(ExamOrder).filter_by(visit_id=own_visit_id).count() == 2
            assert db.get(InventoryItem, drug_item["id"]).stock_qty == stock_before - 2
        finally:
            db.close()
        db = SessionLocal()
        try:
            other_visit_id = db.query(Visit.id).filter(Visit.store == "东环店").scalar()
        finally:
            db.close()
        assert client.get(f"/api/staff-miniapp/visits/{other_visit_id}/materials", headers=headers).status_code == 403
        uploaded_material = client.post(
            f"/api/staff-miniapp/visits/{own_visit_id}/materials/upload",
            data={"stage": "before", "media_type": "image", "notes": "治疗前状态"},
            files={"file": ("before.png", png_bytes, "image/png")}, headers=headers,
        )
        assert uploaded_material.status_code == 200, uploaded_material.text
        material_id = uploaded_material.json()["media"]["id"]
        material_detail = client.get(f"/api/staff-miniapp/visits/{own_visit_id}/materials", headers=headers)
        assert material_detail.status_code == 200
        assert material_detail.json()["media"][0]["stage_label"] == "治疗前"
        assert material_detail.json()["media"][0]["notes"] == "治疗前状态"
        material_file = client.get(f"/api/staff-miniapp/visit-materials/{material_id}/file", headers=headers)
        assert material_file.status_code == 200
        deleted_material = client.post(
            f"/api/staff-miniapp/visits/{own_visit_id}/materials/{material_id}/delete", headers=headers,
        )
        assert deleted_material.status_code == 200
        assert client.get(f"/api/staff-miniapp/visit-materials/{material_id}/file", headers=headers).status_code == 404
        assert client.post(f"/api/staff-miniapp/visits/{other_visit_id}/anesthesia-monitor", json={}, headers=headers).status_code == 403
        started_monitor = client.post(
            f"/api/staff-miniapp/visits/{own_visit_id}/anesthesia-monitor", json={"procedure": "测试手术"}, headers=headers,
        )
        assert started_monitor.status_code == 200, started_monitor.text
        monitor_id = started_monitor.json()["id"]
        active_after_start = client.get("/api/staff-miniapp/anesthesia-monitors", headers=headers)
        assert [row["id"] for row in active_after_start.json()["active"]] == [monitor_id]
        db = SessionLocal()
        try:
            drug = InventoryItem(
                name="测试右美托咪定", category="medication", subcategory="controlled",
                is_controlled=True, is_service=False, unit="ml", unit2="瓶", unit2_ratio=10,
                stock_qty=10, manufacturer="测试厂家", store="横岗店", created_by="test",
            )
            db.add(drug); db.flush()
            batch = InventoryBatch(item_id=drug.id, batch_no="DEX-01", quantity=10, expiry_date="2028-01-01")
            db.add(batch); db.commit(); drug_id, batch_id = drug.id, batch.id
        finally:
            db.close()
        monitor_detail = client.get(f"/api/staff-miniapp/anesthesia-monitors/{monitor_id}", headers=headers)
        assert monitor_detail.status_code == 200, monitor_detail.text
        assert monitor_detail.json()["sheet"]["pet"]["name"] == "横岗犬"
        assert monitor_detail.json()["sheet"]["inventory"][0]["name"] == "测试右美托咪定"
        assert client.post(f"/api/staff-miniapp/anesthesia-monitors/{monitor_id}/header", json={
            "procedure": "犬绝育术", "asa_grade": "I", "agent": "异氟烷", "weight_kg": 8.5,
        }, headers=headers).status_code == 200
        opened = client.post(f"/api/staff-miniapp/anesthesia-monitors/{monitor_id}/open-vial", json={
            "item_id": drug_id, "batch_id": batch_id, "opened_qty": 10,
        }, headers=headers)
        assert opened.status_code == 200, opened.text
        vial_id = opened.json()["id"]
        medication = client.post(f"/api/staff-miniapp/anesthesia-monitors/{monitor_id}/medications", json={
            "open_vial_id": vial_id, "qty": 0.1, "dose_text": "5μg",
            "phase": "premedication", "route": "IV", "note": "术前镇静",
        }, headers=headers)
        assert medication.status_code == 200, medication.text
        vital = client.post(f"/api/staff-miniapp/anesthesia-monitors/{monitor_id}/entries", json={
            "hr": 100, "rr": 15, "spo2": 98, "etco2": 38, "temperature_c": 37.8,
            "bp_sys": 110, "bp_dia": 70, "bp_map": 83, "agent_pct": 1.8,
            "o2_flow": 1.0, "depth": "adequate",
        }, headers=headers)
        assert vital.status_code == 200, vital.text
        finished = client.post(f"/api/staff-miniapp/anesthesia-monitors/{monitor_id}/finish", json={
            "end_time": "12:00", "extubation_time": "12:08",
            "recovery_status": "苏醒良好", "recovery_notes": "自主呼吸平稳",
        }, headers=headers)
        assert finished.status_code == 200, finished.text
        closed_after_finish = client.get(
            "/api/staff-miniapp/anesthesia-monitors", params={"view": "closed"}, headers=headers,
        )
        assert monitor_id in [row["id"] for row in closed_after_finish.json()["monitors"]]
        db = SessionLocal()
        try:
            sheet = db.get(AnesthesiaMonitorSheet, monitor_id)
            assert sheet.status == "closed" and sheet.extubation_time == "12:08"
            assert sheet.recovery_status == "苏醒良好"
            assert db.get(InventoryItem, drug_id).stock_qty == 9.9
            med = db.query(AnesthesiaMedicationEvent).filter_by(sheet_id=monitor_id).one()
            assert med.phase == "premedication" and med.dose_text == "5μg"
            assert db.query(AnesthesiaMonitorEntry).filter_by(sheet_id=monitor_id).count() == 1
        finally:
            db.close()
        created = client.post("/api/staff-miniapp/appointments", json={
            "category": "outpatient", "service_name": "手机端复诊",
            "customer_id": own_customer["id"], "pet_id": own_customer["pets"][0]["id"],
            "appointment_date": today, "appointment_time": "16:00", "duration_minutes": 30,
        }, headers=headers)
        assert created.status_code == 200, created.text
        assert created.json()["appointment"]["store"] == "横岗店"

        tomorrow = (datetime.now().date() + timedelta(days=1)).isoformat()
        beauty_created = client.post("/api/staff-miniapp/appointments", json={
            "category": "grooming", "service_name": "犬造型",
            "customer_id": own_customer["id"], "pet_id": own_customer["pets"][0]["id"],
            "appointment_date": tomorrow, "appointment_time": "18:00", "duration_minutes": 90,
        }, headers=headers)
        assert beauty_created.status_code == 200, beauty_created.text
        assert beauty_created.json()["appointment"]["category"] == "grooming"
        assert beauty_created.json()["appointment"]["service_name"] == "犬造型"
        beauty_id = beauty_created.json()["appointment"]["id"]
        beauty_edited = client.post(
            f"/api/staff-miniapp/appointments/{beauty_id}/service",
            json={"service_name": "犬洗护", "duration": 90}, headers=headers,
        )
        assert beauty_edited.status_code == 200, beauty_edited.text
        assert beauty_edited.json()["appointment"]["category"] == "washcare"
        cat_mismatch = client.post(
            f"/api/staff-miniapp/appointments/{beauty_id}/service",
            json={"service_name": "猫洗护", "duration": 60}, headers=headers,
        )
        assert cat_mismatch.status_code == 400
        assert "该宠物档案为犬" in cat_mismatch.text

        day_off = client.post("/api/staff-miniapp/calendar/beauty-day-off", json={
            "date": today,
        }, headers=headers)
        assert day_off.status_code == 200, day_off.text
        invalid_beauty = client.post("/api/staff-miniapp/appointments", json={
            "category": "beauty", "service_name": "美容洗护",
            "customer_id": own_customer["id"], "pet_id": own_customer["pets"][0]["id"],
            "appointment_date": today, "appointment_time": "18:00", "duration_minutes": 60,
        }, headers=headers)
        assert invalid_beauty.status_code == 400
        assert "犬洗护" in invalid_beauty.text
        beauty_blocked = client.post("/api/staff-miniapp/appointments", json={
            "category": "washcare", "service_name": "犬洗护",
            "customer_id": own_customer["id"], "pet_id": own_customer["pets"][0]["id"],
            "appointment_date": today, "appointment_time": "18:00", "duration_minutes": 60,
        }, headers=headers)
        assert beauty_blocked.status_code == 400
        assert "美容师休息" in beauty_blocked.text

        own = client.get("/api/staff-miniapp/customers", params={"q": "横岗"}, headers=headers)
        assert [row["name"] for row in own.json()["items"]] == ["横岗客户"]
        by_record = client.get("/api/staff-miniapp/customers", params={"q": "HC26090001"}, headers=headers)
        assert [row["name"] for row in by_record.json()["items"]] == ["横岗客户"]
        too_short = client.get("/api/staff-miniapp/customers", params={"q": "横"}, headers=headers)
        assert too_short.json()["items"] == []
        other_store = client.get("/api/staff-miniapp/customers", params={"q": "东环"}, headers=headers)
        assert other_store.json()["items"] == []

        duplicate_wechat = client.post("/api/staff-miniapp/login", json={
            "username": "staff_dh", "password": "test123456", "code": "hg",
        })
        assert duplicate_wechat.status_code == 403

        db = SessionLocal()
        try:
            user = db.query(AdminUser).filter(AdminUser.username == "staff_hg").one()
            user.miniapp_token_created_at = datetime.utcnow() - timedelta(days=31)
            db.commit()
        finally:
            db.close()
        expired = client.get("/api/staff-miniapp/me", headers=headers)
        assert expired.status_code == 401
finally:
    engine.dispose()
    TEMP_DIR.cleanup()

print("PASS: staff miniapp auth and store isolation")
