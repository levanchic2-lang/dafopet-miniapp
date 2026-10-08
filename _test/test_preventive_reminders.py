"""疫苗/驱虫自动订阅提醒回归测试。"""

import os
import sys
import tempfile
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-preventive-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["UPLOAD_DIR"] = str(Path(TEMP_DIR.name) / "uploads")

from app.database import Base, SessionLocal, engine
import app.main as main_module
from app.models import Customer, DewormingRecord, FollowUp, Pet, Vaccination
from app.services.followup_dispatch import sync_preventive_followups


Base.metadata.create_all(bind=engine)
db = SessionLocal()
try:
    customer = Customer(name="提醒客户", phone="13900009999", wechat_openid="openid-test")
    db.add(customer)
    db.flush()
    pet = Pet(customer_id=customer.id, name="豆豆", species="cat", store="横岗店")
    db.add(pet)
    db.flush()
    today = date.today().isoformat()
    vaccination = Vaccination(
        customer_id=customer.id, pet_id=pet.id, vaccine_type="rabies",
        vaccine_name="狂犬疫苗", next_due_date=today, status="active",
    )
    deworming = DewormingRecord(
        customer_id=customer.id, pet_id=pet.id, deworm_type="combo",
        product_name="体内外同驱", next_due_date=today, status="active",
    )
    legacy_task = FollowUp(
        customer_id=customer.id, pet_id=pet.id, source_type="preventive_vaccine",
        source_id=1, planned_date=today, status="due", channel="manual",
    )
    db.add_all([vaccination, deworming, legacy_task])
    db.commit()

    calls = []

    def fake_push(_db, **kwargs):
        calls.append(kwargs)
        return True

    main_module.push_preventive_reminder = fake_push
    result = main_module._run_vaccine_reminders(db)
    assert result == {"sent": 1, "sent_records": 2, "skipped": 0, "errors": 0}
    assert len(calls) == 1
    assert {ref["type"] for ref in calls[0]["source_refs"]} == {"vaccination", "deworming"}
    assert db.get(Vaccination, vaccination.id).reminder_sent_at is not None
    assert db.get(DewormingRecord, deworming.id).reminder_sent_at is not None

    repeated = main_module._run_vaccine_reminders(db)
    assert repeated["sent"] == 0
    assert len(calls) == 1

    cleanup = sync_preventive_followups(db)
    assert cleanup["created"] == 0
    assert cleanup["closed"] == 1
    db.refresh(legacy_task)
    assert legacy_task.status == "closed"
    assert legacy_task.channel == "miniapp_auto"

    vaccination.reminder_sent_at = None
    deworming.reminder_sent_at = None
    db.commit()
    main_module.push_preventive_reminder = lambda *_args, **_kwargs: False
    failed = main_module._run_vaccine_reminders(db)
    assert failed["sent"] == 0
    assert failed["errors"] == 1
    assert db.get(Vaccination, vaccination.id).reminder_sent_at is None
    assert db.get(DewormingRecord, deworming.id).reminder_sent_at is None
finally:
    db.close()
    engine.dispose()
    TEMP_DIR.cleanup()

print("preventive reminder tests passed")
