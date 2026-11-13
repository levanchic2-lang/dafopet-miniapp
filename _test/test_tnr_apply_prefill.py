"""TNR 再次申请只回填个人资料。没有当次登录码一律 401，草稿和猫咪信息不返回。"""

import json
import logging
import os
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-prefill-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["UPLOAD_DIR"] = str(Path(TEMP_DIR.name) / "uploads")
os.environ["SESSION_SECRET"] = "tnr-prefill-test"
os.environ["SESSION_HTTPS_ONLY"] = "0"
os.environ["LEGACY_MINIAPP_COMPAT"] = "1"

from fastapi.testclient import TestClient

import app.main as main_module
from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Application, Customer


@asynccontextmanager
async def _test_lifespan(_app):
    yield


app.router.lifespan_context = _test_lifespan
Base.metadata.create_all(bind=engine)
engine.echo = False


def _fake_code(code: str) -> dict:
    mapping = {
        "owner-code": "openid-owner",
        "stranger-code": "openid-stranger",
        "plain-code": "openid-plain",
        "draft-code": "openid-draft",
    }
    if code not in mapping:
        raise RuntimeError("bad code")
    return {"openid": mapping[code], "session_key": "sekret-session"}


main_module.wechat_code2session = _fake_code

NEW_NAME = "新名"
NEW_PHONE = "13900002222"
NEW_ID = "110101199002021234"
NEW_ADDRESS = "广东省深圳市福田区园岭街道 新门牌2号"
NEW_STORE = "大风动物医院（横岗店）"
OLD_ID = "110101199001011234"
DRAFT_ID = "110101199008081234"
PLAIN_ID = "440301199003031234"
STRAY_ID = "440301199004041234"
CAT_NEW = "新猫"
CAT_DRAFT = "草稿猫"
CAT_PLAIN = "不该给别人的猫"
CAT_STRAY = "散猫"
HEALTH = "新流浪说明"
PROXY = "代办人张三"
PLAN = "短期术后寄养 / 笼养观察后放归"
BREED = "田园猫"
COLOR = "橘色"

db = SessionLocal()
owner = Customer(name="甲客户", phone="13700001111", address="甲路1号", wechat_openid="openid-owner")
plain = Customer(name="丙客户", phone="13700003333", address="丙路3号", wechat_openid="openid-plain")
db.add_all([owner, plain])
db.flush()
old = Application(
    applicant_name="旧名",
    phone="13700001111",
    id_number=OLD_ID,
    address="广东省深圳市罗湖区桂园街道 旧门牌1号",
    clinic_store="大风动物医院（东环店）",
    cat_nickname="旧猫",
    cat_gender="female",
    cat_breed="旧品种",
    cat_color="旧毛色",
    age_estimate="2019-01",
    health_note="旧病情",
    suture_preference="external",
    post_surgery_plan="原地放归",
    is_proxy=True,
    proxy_name="旧代办",
    proxy_phone="13800001111",
    proxy_relation="家人",
    wechat_openid="openid-owner",
    customer_id=owner.id,
    status="approved",
)
newer = Application(
    applicant_name=NEW_NAME,
    phone=NEW_PHONE,
    id_number=NEW_ID,
    address=NEW_ADDRESS,
    clinic_store=NEW_STORE,
    cat_nickname=CAT_NEW,
    cat_gender="male",
    cat_breed=BREED,
    cat_color=COLOR,
    age_estimate="2020-05",
    health_note=HEALTH,
    suture_preference="intradermal",
    post_surgery_plan=PLAN,
    is_proxy=True,
    proxy_name=PROXY,
    proxy_phone="13800003333",
    proxy_relation="朋友",
    wechat_openid="openid-owner",
    customer_id=owner.id,
    status="pending_ai",
)
draft = Application(
    applicant_name="草稿名",
    phone=NEW_PHONE,
    id_number=DRAFT_ID,
    address="广东省深圳市南山区粤海街道 草稿地址",
    clinic_store=NEW_STORE,
    cat_nickname=CAT_DRAFT,
    cat_gender="unknown",
    health_note="草稿病情",
    wechat_openid="openid-owner",
    customer_id=owner.id,
    status="draft",
)
by_customer = Application(
    applicant_name="档案申请人",
    phone="13700003333",
    id_number=PLAIN_ID,
    address="广东省东莞市城区莞城街道 松山湖1号",
    clinic_store="大风动物医院（东环店）",
    cat_nickname=CAT_PLAIN,
    cat_gender="female",
    health_note="档案猫病情",
    suture_preference="intradermal",
    post_surgery_plan="原地放归",
    proxy_name="档案代办",
    wechat_openid="openid-other",
    customer_id=plain.id,
    status="approved",
)
stray = Application(
    applicant_name="散客",
    phone="13600001111",
    id_number=STRAY_ID,
    address="广东省惠州市惠城区桥东街道 散客路8号",
    clinic_store="大风动物医院（横岗店）",
    cat_nickname=CAT_STRAY,
    cat_gender="male",
    health_note="散客流浪说明",
    wechat_openid="openid-stranger",
    status="pending_manual",
)
draft_only = Application(
    applicant_name="只有草稿",
    phone="13600002222",
    id_number="440301199009091234",
    address="草稿路",
    cat_nickname="仅草稿猫",
    cat_gender="male",
    health_note="仅草稿病情",
    wechat_openid="openid-draft",
    status="draft",
)
db.add_all([old, newer, draft, by_customer, stray, draft_only])
db.commit()
db.close()

client = TestClient(app)
LOGS: list[str] = []


class _Capture(logging.Handler):
    def emit(self, record):
        LOGS.append(record.getMessage())


_handler = _Capture()
_logger = logging.getLogger("app.main")
_logger.addHandler(_handler)
_logger.setLevel(logging.INFO)
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

FORBIDDEN = (
    CAT_NEW, CAT_DRAFT, CAT_PLAIN, CAT_STRAY, "仅草稿猫", "旧猫", "甲犬",
    HEALTH, "旧病情", "草稿病情", "档案猫病情", "散客流浪说明", "仅草稿病情",
    PROXY, "旧代办", "档案代办",
    PLAN, "原地放归",
    BREED, COLOR, "旧品种", "旧毛色",
    "intradermal",
    "2020-05", "2019-01",
)


def _body_text(response) -> str:
    try:
        return json.dumps(response.json(), ensure_ascii=False)
    except Exception:
        return response.text


def _quiet(text: str, *secrets: str) -> None:
    for secret in secrets:
        if secret and secret in text:
            raise AssertionError("响应或日志带出了不该出现的内容")


def _personal(body: dict) -> None:
    assert set(body.keys()) == {"found", "applicant_name", "phone", "id_number", "address", "clinic_store"}
    assert body["found"] is True


# 没有当次登录码：即使旧版兼容开着、只带缓存 openid，也拒绝，并且不回任何证件号
for legacy in ("1", "0"):
    os.environ["LEGACY_MINIAPP_COMPAT"] = legacy
    missing = client.post("/api/wechat/tnr-prefill")
    assert missing.status_code == 401, missing.text
    _quiet(_body_text(missing), NEW_ID, OLD_ID, DRAFT_ID, PLAIN_ID, STRAY_ID)
    empty = client.post("/api/wechat/tnr-prefill", json={})
    assert empty.status_code == 401, empty.text
    cached = client.post("/api/wechat/tnr-prefill", json={"openid": "openid-owner"})
    assert cached.status_code == 401, cached.text
    _quiet(_body_text(cached), NEW_ID, NEW_NAME, NEW_PHONE, CAT_NEW, HEALTH)
    spoof = client.post("/api/wechat/tnr-prefill", json={
        "openid": "openid-owner",
        "id_number": NEW_ID,
        "applicant_name": NEW_NAME,
    })
    assert spoof.status_code == 401, spoof.text
    _quiet(_body_text(spoof), NEW_ID, NEW_NAME)

os.environ["LEGACY_MINIAPP_COMPAT"] = "1"
bad = client.post("/api/wechat/tnr-prefill", json={"code": "not-a-code", "openid": "openid-owner"})
assert bad.status_code == 401, bad.text
_quiet(_body_text(bad), NEW_ID, "sekret-session", "not-a-code")

before = len(LOGS)
owner_res = client.post("/api/wechat/tnr-prefill", json={"js_code": "owner-code", "openid": "openid-stranger"})
assert owner_res.status_code == 200, owner_res.text
owner_body = owner_res.json()
_personal(owner_body)
assert owner_body["applicant_name"] == NEW_NAME
assert owner_body["phone"] == NEW_PHONE
assert owner_body["id_number"] == NEW_ID
assert owner_body["address"] == NEW_ADDRESS
assert owner_body["clinic_store"] == NEW_STORE
_quiet(_body_text(owner_res), *FORBIDDEN, DRAFT_ID, OLD_ID, "草稿名", "旧名", "甲客户")
_quiet("\n".join(LOGS[before:]), NEW_ID, NEW_PHONE, NEW_NAME, NEW_ADDRESS, "sekret-session", "owner-code")

# 登录码优先于请求里自报的 openid
stranger_res = client.post("/api/wechat/tnr-prefill", json={"code": "stranger-code", "openid": "openid-owner"})
assert stranger_res.status_code == 200, stranger_res.text
stranger_body = stranger_res.json()
_personal(stranger_body)
assert stranger_body["applicant_name"] == "散客"
assert stranger_body["phone"] == "13600001111"
assert stranger_body["id_number"] == STRAY_ID
_quiet(_body_text(stranger_res), NEW_ID, NEW_NAME, CAT_STRAY, CAT_NEW, HEALTH, "散客流浪说明")

# 没绑客户档案也能按这次微信拿到自己已提交的申请；上面的散客就是这种情况
plain_res = client.post("/api/wechat/tnr-prefill", json={"code": "plain-code"})
assert plain_res.status_code == 200, plain_res.text
plain_body = plain_res.json()
_personal(plain_body)
assert plain_body["applicant_name"] == "档案申请人"
assert plain_body["id_number"] == PLAIN_ID
assert plain_body["address"] == "广东省东莞市城区莞城街道 松山湖1号"
_quiet(_body_text(plain_res), CAT_PLAIN, "档案猫病情", "档案代办", NEW_ID)

draft_res = client.post("/api/wechat/tnr-prefill", json={"code": "draft-code"})
assert draft_res.status_code == 200, draft_res.text
assert draft_res.json() == {"found": False}
_quiet(_body_text(draft_res), "440301199009091234", "只有草稿", "仅草稿猫", "仅草稿病情")

unknown = client.post("/api/wechat/tnr-prefill", json={"code": "stranger-code"})
assert unknown.status_code == 200
# 散客有已提交申请，上面已经断言过。这里再确认猫咪字段没有混进来。
assert "cat_nickname" not in unknown.json()
assert "health_note" not in unknown.json()
assert "images" not in unknown.json()
assert "videos" not in unknown.json()

client.close()
TEMP_DIR.cleanup()
print("tnr prefill test passed")
