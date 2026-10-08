"""住院接续既有处方疗程与整项停药的回归测试。"""

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-inpatient-course-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["UPLOAD_DIR"] = str(Path(TEMP_DIR.name) / "uploads")

from app.database import Base, SessionLocal, engine
from app.models import (
    Customer,
    Hospitalization,
    MedicationAdminLog,
    Pet,
    Prescription,
    PrescriptionItem,
    Visit,
)
import app.main as main_module


Base.metadata.create_all(bind=engine)
db = SessionLocal()
try:
    customer = Customer(name="疗程测试客户", phone="13900000000")
    db.add(customer)
    db.flush()
    pet = Pet(customer_id=customer.id, name="塔塔测试", species="dog", store="横岗店")
    db.add(pet)
    db.flush()
    visit = Visit(
        customer_id=customer.id,
        pet_id=pet.id,
        visit_date="2026-10-05",
        store="横岗店",
    )
    db.add(visit)
    db.flush()
    hospitalization = Hospitalization(
        customer_id=customer.id,
        pet_id=pet.id,
        visit_id=visit.id,
        store="横岗店",
        status="admitted",
        reason="持续呕吐、腹泻",
        # 数据库存 UTC：2026-10-08 11:52 对应北京时间 10 月 8 日 19:52。
        admitted_at=datetime(2026, 10, 8, 11, 52),
        staff_token="course-test-staff",
        owner_token="course-test-owner",
    )
    prescription = Prescription(
        visit_id=visit.id,
        customer_id=customer.id,
        pet_id=pet.id,
        prescribed_date="2026-10-05",
        status="issued",
        # 数据库存 UTC：北京时间 10 月 5 日 10:00 开方。
        created_at=datetime(2026, 10, 5, 2, 0),
    )
    db.add_all([hospitalization, prescription])
    db.flush()
    item = PrescriptionItem(
        prescription_id=prescription.id,
        drug_name="长江-美罗硝",
        dose_amount=1,
        dose_unit="粒",
        frequency="BID",
        times_per_day=2,
        duration_days="5",
        schedule_times="10:00,20:00",
    )
    db.add(item)
    db.flush()
    db.expire(prescription, ["items"])

    created = main_module._generate_med_logs_for_prescription(db, prescription)
    rows = db.query(MedicationAdminLog).order_by(MedicationAdminLog.scheduled_at).all()

    assert created == 3
    assert [(row.scheduled_at.strftime("%Y-%m-%d %H:%M"), row.day_index, row.dose_index) for row in rows] == [
        ("2026-10-08 20:00", 4, 2),
        ("2026-10-09 10:00", 5, 1),
        ("2026-10-09 20:00", 5, 2),
    ]

    stopped = main_module._discontinue_pending_medication(
        db,
        rows[0],
        "梁天兵",
        "医生确认停药：持续呕吐、腹泻，本次住院不再使用",
    )
    assert len(stopped) == 3
    assert all(row.status == "cancelled" for row in stopped)
    assert all(row.administered_by == "梁天兵" for row in stopped)
    assert all("医生确认停药" in row.notes for row in stopped)

    # 已停药时点不能因后续保存处方而重新生成。
    assert main_module._generate_med_logs_for_prescription(db, prescription) == 0
    assert db.query(MedicationAdminLog).filter_by(status="pending").count() == 0

    # 入院前已结束的疗程不应生成任何补服任务。
    finished_prescription = Prescription(
        visit_id=visit.id,
        customer_id=customer.id,
        pet_id=pet.id,
        prescribed_date="2026-10-05",
        status="issued",
        created_at=datetime(2026, 10, 5, 2, 0),
    )
    db.add(finished_prescription)
    db.flush()
    db.add(PrescriptionItem(
        prescription_id=finished_prescription.id,
        drug_name="三日疗程测试药",
        frequency="QD",
        times_per_day=1,
        duration_days="3",
        schedule_times="10:00",
    ))
    db.flush()
    db.expire(finished_prescription, ["items"])
    assert main_module._generate_med_logs_for_prescription(db, finished_prescription) == 0
finally:
    db.close()
    engine.dispose()
    TEMP_DIR.cleanup()

print("inpatient medication course regression tests passed")
