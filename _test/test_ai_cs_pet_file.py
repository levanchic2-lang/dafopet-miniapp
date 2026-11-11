"""客服宠物档案：多来源最新体重、占位生日、多宠先问再加载，以及预约沿用确认后的毛长和体重。"""

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="ai-cs-pet-file-")
os.environ["DATABASE_URL"] = f"sqlite:///{(Path(TEMP_DIR.name) / 'test.db').as_posix()}"
os.environ["UPLOAD_DIR"] = str(Path(TEMP_DIR.name) / "uploads")
os.environ["SESSION_SECRET"] = "ai-cs-pet-file"
os.environ["SESSION_HTTPS_ONLY"] = "0"
os.environ["AI_CS_API_KEY"] = "test-ai-cs-key"

from app.database import Base, SessionLocal, engine
import app.models  # noqa: F401
from app.models import (
    AnesthesiaMonitorSheet,
    Application,
    Appointment,
    AppointmentStatus,
    Customer,
    DewormingRecord,
    GroomingOrder,
    Hospitalization,
    Pet,
    WeightRecord,
)
from app.services import ai_cs_assistant
from app.time_utils import beijing_today

Base.metadata.create_all(bind=engine)

DAY = (beijing_today() + timedelta(days=3)).isoformat()
PHONE = "13812348080"
ADDRESS = "秘密路88号"
APP_ADDRESS = "申请秘密地址"
HIDDEN_BREED = "测试斑猫"
OTHER_BREED = "另一秘密品种"


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


def _customer(db, openid, name, phone=PHONE):
    row = Customer(
        name=name,
        phone=phone,
        wechat_openid=openid,
        address=ADDRESS,
        id_number="440300199001011234",
    )
    db.add(row)
    db.flush()
    return row


def _pet(db, customer, name, **extra):
    row = Pet(
        customer_id=customer.id,
        name=name,
        species=extra.pop("species", "cat"),
        breed=extra.pop("breed", ""),
        gender=extra.pop("gender", "female"),
        birthday_estimate=extra.pop("birthday_estimate", ""),
        is_neutered=extra.pop("is_neutered", False),
        store="东环店",
    )
    db.add(row)
    db.flush()
    return row


def _seed(db):
    owner = _customer(db, "cs-file", "大米主人")
    rice = _pet(
        db, owner, "大米", breed="田园猫", gender="female",
        birthday_estimate="0001-01-01", is_neutered=True,
    )
    db.add(WeightRecord(pet_id=rice.id, record_date="0001-01-01", weight_kg=1.1, created_at=datetime(2026, 11, 11, 1, 0)))
    db.add(WeightRecord(pet_id=rice.id, record_date="2026-01-01", weight_kg=6.6))
    db.add(DewormingRecord(
        pet_id=rice.id, customer_id=owner.id, deworm_date="2026-09-03",
        weight_kg=4.4, status="active", product_name="体内外",
    ))
    db.add(DewormingRecord(
        pet_id=rice.id, customer_id=owner.id, deworm_date="2026-10-20",
        weight_kg=8.8, status="voided", product_name="作废",
    ))
    db.add(Hospitalization(
        pet_id=rice.id, customer_id=owner.id, admission_weight_kg=3.2,
        admitted_at=datetime(2026, 7, 31, 16, 0),
        staff_token="hosp-rice-staff", owner_token="hosp-rice-owner",
    ))
    db.add(AnesthesiaMonitorSheet(
        pet_id=rice.id, customer_id=owner.id, monitor_date="2026-06-01", weight_kg=7.7,
    ))
    db.add(Application(
        applicant_name="大米主人", phone="13900009999", address=APP_ADDRESS,
        cat_gender="female", weight_estimate="约9.9公斤",
        customer_id=owner.id, pet_id=rice.id,
        created_at=datetime(2026, 9, 3, 3, 0),
    ))
    db.add(GroomingOrder(
        pet_id=rice.id, customer_id=owner.id, groom_date="2026-08-01",
        coat_length="short", status="active",
    ))
    db.add(GroomingOrder(
        pet_id=rice.id, customer_id=owner.id, groom_date="2026-10-01",
        coat_length="long", status="voided",
    ))
    db.add(Appointment(
        customer_id=owner.id, pet_id=rice.id, pet_name="大米",
        category="beauty", status=AppointmentStatus.confirmed.value,
        service_name="猫洗护", store="大风动物医院（东环店）",
        appointment_date="2026-07-01", appointment_time="14:00",
        coat_length="长毛", phone=PHONE,
    ))

    blank_owner = _customer(db, "cs-blank", "空档案", phone="13700001111")
    _pet(db, blank_owner, "无名", breed="", birthday_estimate="0001/01/01", gender="unknown")

    stale_owner = _customer(db, "cs-stale", "旧体重", phone="13700002222")
    stale = _pet(db, stale_owner, "旧旧", birthday_estimate="2024-05-02")
    db.add(WeightRecord(pet_id=stale.id, record_date="2024-01-01", weight_kg=2.2))

    estimate_owner = _customer(db, "cs-estimate", "只有预估", phone="13700003333")
    estimate = _pet(db, estimate_owner, "估估", birthday_estimate="2024-05-02")
    db.add(Application(
        applicant_name="只有预估", phone="13900008888", address=APP_ADDRESS,
        cat_gender="male", weight_estimate="约3.3公斤",
        customer_id=estimate_owner.id, pet_id=estimate.id,
        created_at=datetime(2026, 8, 2, 3, 0),
    ))

    multi = _customer(db, "cs-multi", "多宠家庭", phone="13700004444")
    first = _pet(db, multi, "大米", breed=HIDDEN_BREED, birthday_estimate="2023-01-01")
    second = _pet(db, multi, "小白", breed=OTHER_BREED, species="dog", gender="male")
    db.add(WeightRecord(pet_id=first.id, record_date="2026-09-01", weight_kg=8.88))
    db.commit()
    return {
        "rice": rice.id,
        "multi_first": first.id,
        "multi_second": second.id,
    }


def _chart(result, name):
    for profile in result["profiles"]:
        for pet in profile["pets"]:
            if pet["name"] == name:
                return pet
    raise AssertionError(name)


def test_prompt_asks_which_pet_before_using_the_file():
    text = ai_cs_assistant._system_prompt(SessionLocal(), "cs-file")
    ask = text.find("先问是哪一只")
    chosen = text.find("宠物确定之后")
    missing = text.find("只问还缺的")
    assert 0 <= ask < chosen < missing
    assert "早于 6 个月" in text
    assert "档案里大米是田园猫" in text
    assert "不要再问" in text
    assert "4.4" in text
    assert "deworming_records" in text
    knowledge = ai_cs_assistant.load_knowledge()
    assert "先问是哪一只" in knowledge
    assert "宠物确定之后" in knowledge
    assert "只问还缺的" in knowledge or "只请客户确认" in knowledge


def test_newest_weight_placeholder_birthday_and_missing_fields():
    db = SessionLocal()
    try:
        result = ai_cs_assistant.execute_tool(db, "cs-file", "list_profiles_and_pets", {})
        blob = json.dumps(result, ensure_ascii=False)
        pet = _chart(result, "大米")
        assert result["ask_which_pet_first"] is False
        assert pet["species"] == "猫"
        assert pet["breed"] == "田园猫"
        assert pet["gender"] == "母"
        assert pet["is_neutered"] is True
        assert pet["age"] == ""
        assert pet["birthday"] == ""
        assert "0001-01-01" not in blob
        assert pet["weight_kg"] == 4.4
        assert pet["weight_date"] == "2026-09-03"
        assert pet["weight_source"] == "deworming_records"
        assert pet["weight_older_than_6_months"] is False
        assert pet["coat_length"] == "短毛"
        assert pet["coat_date"] == "2026-08-01"
        assert pet["coat_source"] == "grooming_orders"
        assert ADDRESS not in blob and APP_ADDRESS not in blob
        assert PHONE not in blob and "13900009999" not in blob
        assert "440300199001011234" not in blob

        blank = ai_cs_assistant.execute_tool(db, "cs-blank", "list_profiles_and_pets", {})
        empty = _chart(blank, "无名")
        assert empty["breed"] == ""
        assert empty["age"] == ""
        assert empty["birthday"] == ""
        assert empty["weight_kg"] is None
        assert empty["weight_source"] == ""
        assert empty["coat_length"] == ""
        assert "0001" not in json.dumps(empty, ensure_ascii=False)

        stale = _chart(ai_cs_assistant.execute_tool(db, "cs-stale", "list_profiles_and_pets", {}), "旧旧")
        assert stale["weight_kg"] == 2.2
        assert stale["weight_date"] == "2024-01-01"
        assert stale["weight_older_than_6_months"] is True
        age, birthday = ai_cs_assistant.pet_age_fields("2024-05-02")
        assert stale["age"] == age and stale["birthday"] == birthday and age

        estimate = _chart(
            ai_cs_assistant.execute_tool(db, "cs-estimate", "list_profiles_and_pets", {}),
            "估估",
        )
        assert estimate["weight_kg"] == 3.3
        assert estimate["weight_source"] == "applications"
        assert estimate["weight_date"] == "2026-08-02"
    finally:
        db.close()


def test_multiple_pets_load_file_only_after_one_is_chosen():
    db = SessionLocal()
    try:
        listed = ai_cs_assistant.execute_tool(db, "cs-multi", "list_profiles_and_pets", {})
        blob = json.dumps(listed, ensure_ascii=False)
        assert listed["ask_which_pet_first"] is True
        assert "先问是哪一只" in listed["next_step"]
        assert HIDDEN_BREED not in blob and OTHER_BREED not in blob
        assert "8.88" not in blob
        assert "weight_kg" not in _chart(listed, "大米")
        prompt = ai_cs_assistant._system_prompt(db, "cs-multi")
        assert "先问是哪一只" in prompt
        assert HIDDEN_BREED not in prompt and "8.88" not in prompt

        chosen = ai_cs_assistant.execute_tool(
            db, "cs-multi", "list_profiles_and_pets", {"pet_id": IDS["multi_first"]},
        )
        chosen_blob = json.dumps(chosen, ensure_ascii=False)
        assert chosen["ask_which_pet_first"] is False
        assert chosen["selected_pet_id"] == IDS["multi_first"]
        pet = _chart(chosen, "大米")
        assert pet["breed"] == HIDDEN_BREED
        assert pet["weight_kg"] == 8.88
        assert pet["species"] == "猫"
        assert OTHER_BREED not in chosen_blob
        other = _chart(chosen, "小白")
        assert "breed" not in other and "weight_kg" not in other

        foreign = ai_cs_assistant.execute_tool(
            db, "cs-file", "list_profiles_and_pets", {"pet_id": IDS["multi_first"]},
        )
        foreign_blob = json.dumps(foreign, ensure_ascii=False)
        assert "不在" in foreign.get("message", "")
        assert HIDDEN_BREED not in foreign_blob
        assert _chart(foreign, "大米")["weight_kg"] == 4.4
    finally:
        db.close()


def test_mocked_llm_books_with_file_weight_and_coat():
    db = SessionLocal()
    seen = {}

    async def stage(messages, tools):
        seen["prompt"] = messages[0]["content"]
        names = [item["function"]["name"] for item in tools]
        assert "list_profiles_and_pets" in names
        return _Msg(tool_calls=[_Call("stage_booking", {
            "pet_id": IDS["rice"],
            "store": "东环店",
            "date": DAY,
            "time": "14:00",
            "kind": "beauty",
            "service_name": "猫洗护",
            "pet_size": "大型猫（4kg 以上）",
            "addons": "去浮毛",
        })])

    ai_cs_assistant.complete_override = stage
    try:
        staged = asyncio.run(ai_cs_assistant.handle_user_text(db, "cs-file", "帮大米约洗护，要去底绒"))
        prompt = seen["prompt"]
        assert prompt.find("先问是哪一只") < prompt.find("宠物确定之后") < prompt.find("只问还缺的")
        assert "4.4" in prompt and "短毛" in prompt
        assert "确认" in staged
        assert "4.4" in staged and "短毛" in staged
        assert db.query(Appointment).filter(
            Appointment.wechat_openid == "cs-file",
            Appointment.appointment_date == DAY,
        ).count() == 0

        done = asyncio.run(ai_cs_assistant.handle_user_text(db, "cs-file", "确认"))
        made = db.query(Appointment).filter(
            Appointment.wechat_openid == "cs-file",
            Appointment.appointment_date == DAY,
        ).one()
        assert made.coat_length == "短毛"
        assert made.duration_minutes == 150
        assert made.service_name == "猫洗护"
        assert "已经帮您约好" in done

        overridden = ai_cs_assistant.execute_tool(db, "cs-file", "stage_booking", {
            "pet_id": IDS["rice"],
            "store": "东环店",
            "date": DAY,
            "time": "17:00",
            "kind": "beauty",
            "service_name": "猫洗护",
            "pet_size": "大型猫（4kg 以上）",
            "coat_length": "短毛",
            "addons": "去浮毛",
            "weight_kg": 6.6,
        })
        assert overridden["ok"] is True
        assert "6.6" in overridden["script"]
        pending = db.get(ai_cs_assistant.AiCsPending, "cs-file")
        payload = json.loads(pending.payload_json)
        assert round(float(payload["weight_kg"]), 2) == 6.6
        assert payload["coat_length"] == "短毛"
    finally:
        ai_cs_assistant.complete_override = None
        db.close()


if __name__ == "__main__":
    db = SessionLocal()
    try:
        IDS = _seed(db)
    finally:
        db.close()
    test_prompt_asks_which_pet_before_using_the_file()
    test_newest_weight_placeholder_birthday_and_missing_fields()
    test_multiple_pets_load_file_only_after_one_is_chosen()
    test_mocked_llm_books_with_file_weight_and_coat()
    print("ai cs pet file tests passed")
