"""手动入库按最新进价回写目录成本，空进价不改目录价。

使用独立 sqlite，不读取生产库。
"""

import os
import re
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-stock-in-latest-cost-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["SESSION_SECRET"] = "stock-in-latest-cost-test"

from fastapi.testclient import TestClient
from passlib.hash import bcrypt

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import AdminUser, InventoryBatch, InventoryItem, InventoryTransaction


Base.metadata.create_all(bind=engine)


def csrf(html: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    assert match, f"页面缺少 CSRF token: {html[:500]}"
    return match.group(1)


def money(value: float) -> str:
    return f"{value:.2f}"


db = SessionLocal()
db.add(AdminUser(
    username="stock_in_admin",
    password_hash=bcrypt.hash("test123456"),
    role="superadmin",
    store="",
    is_active=True,
))
override_json = '{"东环店": {"sell": 99.5, "cost": 50}}'
item = InventoryItem(
    name="最新进价测试药品",
    category="medication",
    subcategory="general",
    is_service=False,
    unit="盒",
    sell_price=80,
    cost_price=50,
    stock_qty=4,
    store="东环店",
    store_overrides=override_json,
    notes="原备注保留",
    supplier="原供应商",
    created_by="test",
)
service = InventoryItem(
    name="最新进价测试服务",
    category="grooming",
    is_service=True,
    unit="次",
    sell_price=30,
    cost_price=10,
    stock_qty=0,
    store="东环店",
    created_by="test",
)
db.add_all([item, service])
db.commit()
item_id = item.id
service_id = service.id
db.close()

client = TestClient(app, base_url="https://testserver", follow_redirects=False)
client.__enter__()
login_page = client.get("/admin/login")
assert login_page.status_code == 200
login = client.post("/admin/login", data={
    "username": "stock_in_admin",
    "password": "test123456",
    "csrf_token": csrf(login_page.text),
})
assert login.status_code == 303, login.text


def stock_in(target_id: int, **fields):
    page = client.get(f"/admin/inventory/{target_id}")
    assert page.status_code == 200, page.text[:300]
    data = {"csrf_token": csrf(page.text), "qty": "2"}
    data.update(fields)
    return client.post(f"/admin/inventory/{target_id}/stock-in", data=data)


def reload_item(target_id: int) -> InventoryItem:
    session = SessionLocal()
    try:
        row = session.get(InventoryItem, target_id)
        session.expunge(row)
        return row
    finally:
        session.close()


# 填写本次进价：目录进价改为最新价，流水仍记下这笔单价。
response = stock_in(
    item_id,
    qty="3",
    unit_price="40",
    batch_no="LOT-40",
    expiry_date="2027-06-01",
    note="第一批进价40",
)
assert response.status_code == 303, response.text
assert f"/admin/inventory/{item_id}" in response.headers["location"]

row = reload_item(item_id)
assert row.cost_price == 40
assert row.sell_price == 80
assert row.stock_qty == 7
assert row.notes == "原备注保留"
assert row.supplier == "原供应商"
assert row.store_overrides == override_json

session = SessionLocal()
tx = session.query(InventoryTransaction).filter_by(item_id=item_id).one()
assert tx.tx_type == "in"
assert tx.qty == 3
assert tx.qty_before == 4
assert tx.qty_after == 7
assert tx.unit_price == 40
assert tx.ref_type == "manual"
assert tx.operator == "stock_in_admin"
assert tx.note == "第一批进价40"
batch = session.query(InventoryBatch).filter_by(item_id=item_id).one()
assert batch.batch_no == "LOT-40"
assert batch.quantity == 3
assert batch.expiry_date == "2027-06-01"
assert batch.notes == "第一批进价40"
session.close()

page = client.get(f"/admin/inventory/{item_id}")
assert page.status_code == 200
assert "进价" in page.text
assert f"¥{money(40)}" in page.text
assert 'name="unit_price"' in page.text
assert 'value="40.00"' in page.text
assert f"¥{money(50)}" not in page.text

listing = client.get("/admin/inventory", params={"q": "最新进价测试药品"})
assert listing.status_code == 200
assert "最新进价测试药品" in listing.text
assert f"¥{money(40)}" in listing.text

# 本次进价留空、省略、或 0：不改目录进价，流水单价回退到当前目录进价。
for label, extra in (
    ("empty", {"unit_price": ""}),
    ("omitted", {}),
    ("zero", {"unit_price": "0"}),
):
    before = reload_item(item_id)
    response = stock_in(item_id, qty="1", note=f"不改价-{label}", **extra)
    assert response.status_code == 303, (label, response.text)
    after = reload_item(item_id)
    assert after.cost_price == before.cost_price == 40, label
    assert after.stock_qty == before.stock_qty + 1, label
    assert after.store_overrides == override_json
    session = SessionLocal()
    logged = (
        session.query(InventoryTransaction)
        .filter_by(item_id=item_id, note=f"不改价-{label}")
        .one()
    )
    assert logged.unit_price == 40, label
    assert logged.qty == 1
    session.close()

# 同一批号再次入库只累加批次数量；新的正进价再次覆盖目录进价。
response = stock_in(
    item_id,
    qty="2",
    unit_price="  33.5 ",
    batch_no="LOT-40",
    expiry_date="2027-08-01",
    note="同批次改价",
)
assert response.status_code == 303, response.text
row = reload_item(item_id)
assert row.cost_price == 33.5
assert row.store_overrides == override_json
session = SessionLocal()
batch = session.query(InventoryBatch).filter_by(item_id=item_id, batch_no="LOT-40").one()
assert batch.quantity == 5  # 首笔 3 + 本次 2；中间三次未填批号，不进该批次
assert batch.expiry_date == "2027-08-01"
assert batch.notes == "同批次改价"
assert session.query(InventoryTransaction).filter_by(item_id=item_id).count() == 5
session.close()
page = client.get(f"/admin/inventory/{item_id}")
assert f"¥{money(33.5)}" in page.text
assert 'value="33.50"' in page.text

# 非法进价不入库、不改价。
before = reload_item(item_id)
for bad in ("abc", "-1"):
    response = stock_in(item_id, qty="9", unit_price=bad, note="不应入库")
    assert response.status_code == 400, (bad, response.status_code, response.text)
after = reload_item(item_id)
assert after.cost_price == before.cost_price
assert after.stock_qty == before.stock_qty
session = SessionLocal()
assert session.query(InventoryTransaction).filter_by(item_id=item_id, note="不应入库").count() == 0
session.close()

# 服务项目仍不能手动入库。
response = stock_in(service_id, qty="1", unit_price="99")
assert response.status_code == 404
service_row = reload_item(service_id)
assert service_row.cost_price == 10
assert service_row.stock_qty == 0

print("inventory stock-in latest cost: ok")
