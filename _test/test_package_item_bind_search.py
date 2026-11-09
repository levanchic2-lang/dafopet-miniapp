"""套餐可抵项目：按名称搜索并点选精确品目后再绑定。"""

import os
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-package-bind-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["UPLOAD_DIR"] = str(Path(TEMP_DIR.name) / "uploads")
os.environ["SESSION_HTTPS_ONLY"] = "0"

from fastapi.testclient import TestClient
from passlib.hash import bcrypt

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import AdminUser, InventoryItem, PackageApplicableItem, PackageProduct


@asynccontextmanager
async def _test_lifespan(_app):
    yield


app.router.lifespan_context = _test_lifespan
Base.metadata.create_all(bind=engine)
client = TestClient(app)


def _csrf(path: str) -> str:
    html = client.get(path).text
    return html.split('name="csrf_token" value="', 1)[1].split('"', 1)[0]


def _login(username: str):
    client.post("/admin/logout", follow_redirects=False)
    token = _csrf("/admin/login")
    response = client.post("/admin/login", data={
        "username": username, "password": "test123456", "csrf_token": token,
    }, follow_redirects=False)
    assert response.status_code == 303, response.text


def _seed():
    db = SessionLocal()
    db.add_all([
        AdminUser(username="bind_admin", password_hash=bcrypt.hash("test123456"), role="superadmin", store="", display_name="目录管理员", is_active=True),
        AdminUser(username="east_admin", password_hash=bcrypt.hash("test123456"), role="superadmin", store="东环店", display_name="东环超管", is_active=True),
        AdminUser(username="bind_staff", password_hash=bcrypt.hash("test123456"), role="staff", store="东环店", display_name="前台", is_active=True),
    ])
    db.flush()
    rows = [
        InventoryItem(name="洗澡", category="grooming", subcategory="washcare", unit="次", store="东环店", is_service=True, sell_price=45, is_active=True),
        InventoryItem(name="洗澡", category="grooming", subcategory="washcare", unit="次", store="横岗店", is_service=True, sell_price=45, is_active=True),
        InventoryItem(name="洗澡", category="product", subcategory="general", unit="瓶", unit2="箱", unit2_ratio=12, store="东环店", is_service=False, stock_qty=6, sell_price=38, is_active=True),
        InventoryItem(name="100%洗澡", category="grooming", subcategory="washcare", unit="次", store="", is_service=True, is_active=True),
        InventoryItem(name="驱虫", category="antiparasitic", subcategory="internal", unit="片", store="东环店", is_service=False, stock_qty=4, is_active=True),
        InventoryItem(name="已下架洗澡", category="grooming", unit="次", store="东环店", is_service=True, is_active=False),
        InventoryItem(name='洗澡<script>alert(1)</script>', category="grooming", unit="次", store="东环店", is_service=True, is_active=True),
    ]
    db.add_all(rows)
    db.flush()
    product = PackageProduct(name="45元洗澡卡4送一", category="grooming", total_uses=5, sell_price=180, unit_price=45, validity_days=365, store="", is_active=True)
    other = PackageProduct(name="驱虫年卡", category="deworming", total_uses=4, sell_price=200, unit_price=50, validity_days=365, store="", is_active=True)
    db.add_all([product, other])
    db.flush()
    kept = PackageApplicableItem(product_id=other.id, inventory_item_id=rows[4].id)
    db.add(kept)
    db.commit()
    ids = {
        "east_bath": rows[0].id,
        "west_bath": rows[1].id,
        "bottle": rows[2].id,
        "percent": rows[3].id,
        "deworm": rows[4].id,
        "inactive": rows[5].id,
        "script": rows[6].id,
        "bath_card": product.id,
        "other_card": other.id,
    }
    db.close()
    return ids


IDS = _seed()


def test_search_lists_same_name_without_binding():
    _login("bind_admin")
    before = SessionLocal()
    assert before.query(PackageApplicableItem).filter_by(product_id=IDS["bath_card"]).count() == 0
    before.close()
    found = client.get("/admin/packages/item-search", params={"q": "洗澡"}).json()["items"]
    names = [row["name"] for row in found]
    assert "已下架洗澡" not in names
    by_id = {row["id"]: row for row in found}
    assert IDS["east_bath"] in by_id and IDS["west_bath"] in by_id and IDS["bottle"] in by_id
    assert by_id[IDS["east_bath"]]["store"] == "东环店"
    assert by_id[IDS["west_bath"]]["store"] == "横岗店"
    assert by_id[IDS["east_bath"]]["unit"] == "次"
    assert by_id[IDS["east_bath"]]["category"] == "美容"
    assert by_id[IDS["bottle"]]["unit"] == "瓶"
    assert by_id[IDS["bottle"]]["spec"] == "1箱=12瓶"
    assert by_id[IDS["bottle"]]["category"] == "商品"
    assert by_id[IDS["percent"]]["store"] == "两店通用"
    percent_only = client.get("/admin/packages/item-search", params={"q": "%"}).json()["items"]
    assert [row["id"] for row in percent_only] == [IDS["percent"]]
    assert client.get("/admin/packages/item-search", params={"q": "_"}).json()["items"] == []
    page = client.get(f"/admin/packages/{IDS['bath_card']}/items")
    assert page.status_code == 200
    assert "按名称搜索" in page.text
    assert 'name="inventory_item_id" inputmode' not in page.text
    assert "例如 2522" not in page.text
    after = SessionLocal()
    assert after.query(PackageApplicableItem).filter_by(product_id=IDS["bath_card"]).count() == 0
    after.close()


def test_bind_selected_id_only_and_reject_bad_selection():
    _login("bind_admin")
    token = _csrf(f"/admin/packages/{IDS['bath_card']}/items")
    missing = client.post(f"/admin/packages/{IDS['bath_card']}/items", data={
        "csrf_token": token, "action": "add", "inventory_item_id": "",
    }, follow_redirects=False)
    assert "请先从搜索结果里点选一个品目" in unquote(missing.headers["location"])
    bad = client.post(f"/admin/packages/{IDS['bath_card']}/items", data={
        "csrf_token": token, "action": "add", "inventory_item_id": "999999",
    }, follow_redirects=False)
    assert "不存在" in unquote(bad.headers["location"])
    inactive = client.post(f"/admin/packages/{IDS['bath_card']}/items", data={
        "csrf_token": token, "action": "add", "inventory_item_id": str(IDS["inactive"]),
    }, follow_redirects=False)
    assert "已下架" in unquote(inactive.headers["location"])
    chosen = client.post(f"/admin/packages/{IDS['bath_card']}/items", data={
        "csrf_token": token, "action": "add", "inventory_item_id": str(IDS["west_bath"]),
    }, follow_redirects=False)
    assert unquote(chosen.headers["location"]).endswith("msg=已绑定：洗澡")
    page = client.get(f"/admin/packages/{IDS['bath_card']}/items")
    assert "横岗店" in page.text
    db = SessionLocal()
    linked = [row.inventory_item_id for row in db.query(PackageApplicableItem).filter_by(product_id=IDS["bath_card"])]
    assert linked == [IDS["west_bath"]]
    other = {row.inventory_item_id for row in db.query(PackageApplicableItem).filter_by(product_id=IDS["other_card"])}
    assert other == {IDS["deworm"]}
    db.close()
    again = client.post(f"/admin/packages/{IDS['bath_card']}/items", data={
        "csrf_token": token, "action": "add", "inventory_item_id": str(IDS["west_bath"]),
    }, follow_redirects=False)
    assert "已经绑定过" in unquote(again.headers["location"])
    db = SessionLocal()
    assert db.query(PackageApplicableItem).filter_by(product_id=IDS["bath_card"]).count() == 1
    db.close()


def test_store_scope_and_permissions_keep_existing_binding():
    _login("east_admin")
    found = client.get("/admin/packages/item-search", params={"q": "洗澡"}).json()["items"]
    found_ids = {row["id"] for row in found}
    assert IDS["east_bath"] in found_ids
    assert IDS["bottle"] in found_ids
    assert IDS["percent"] in found_ids
    assert IDS["west_bath"] not in found_ids
    token = _csrf(f"/admin/packages/{IDS['bath_card']}/items")
    denied = client.post(f"/admin/packages/{IDS['bath_card']}/items", data={
        "csrf_token": token, "action": "add", "inventory_item_id": str(IDS["west_bath"]),
    }, follow_redirects=False)
    assert "当前门店不能绑定" in unquote(denied.headers["location"])
    _login("bind_staff")
    assert client.get(f"/admin/packages/{IDS['bath_card']}/items").status_code == 403
    assert client.get("/admin/packages/item-search", params={"q": "洗澡"}).status_code == 403
    _login("bind_admin")
    db = SessionLocal()
    assert db.query(PackageApplicableItem).filter_by(product_id=IDS["other_card"], inventory_item_id=IDS["deworm"]).count() == 1
    assert db.query(PackageApplicableItem).filter_by(product_id=IDS["bath_card"]).count() == 1
    db.close()
    token = _csrf(f"/admin/packages/{IDS['bath_card']}/items")
    removed = client.post(f"/admin/packages/{IDS['bath_card']}/items", data={
        "csrf_token": token, "action": "remove", "inventory_item_id": str(IDS["west_bath"]),
    }, follow_redirects=False)
    assert "已移除绑定" in unquote(removed.headers["location"])
    db = SessionLocal()
    assert db.query(PackageApplicableItem).filter_by(product_id=IDS["bath_card"]).count() == 0
    assert {row.inventory_item_id for row in db.query(PackageApplicableItem).filter_by(product_id=IDS["other_card"])} == {IDS["deworm"]}
    db.close()


def test_page_escapes_item_name():
    _login("bind_admin")
    token = _csrf(f"/admin/packages/{IDS['bath_card']}/items")
    client.post(f"/admin/packages/{IDS['bath_card']}/items", data={
        "csrf_token": token, "action": "add", "inventory_item_id": str(IDS["script"]),
    }, follow_redirects=True)
    page = client.get(f"/admin/packages/{IDS['bath_card']}/items").text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


if __name__ == "__main__":
    test_search_lists_same_name_without_binding()
    test_bind_selected_id_only_and_reject_bad_selection()
    test_store_scope_and_permissions_keep_existing_binding()
    test_page_escapes_item_name()
    print("package bind search tests passed")
