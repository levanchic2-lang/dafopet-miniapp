"""公开医院页、检索文件和原有 TNR 申请入口。

不创建客户记录，也不提交一份完整申请。
"""

import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="public-geo-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["UPLOAD_DIR"] = str(Path(TEMP_DIR.name) / "uploads")
os.environ["SESSION_SECRET"] = "public-geo-test-secret"
os.environ["SESSION_HTTPS_ONLY"] = "0"

from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.models import Application, BrandSettings, PublicContent
from app.public_info import (
    PUBLIC_CRAWL_ASSETS,
    PUBLIC_ORIGIN,
    PUBLIC_STORES,
    RETIRED_HENGGANG_LANDLINE,
    FAQ_ITEMS,
    build_robots_txt,
    build_sitemap_xml,
    resolve_public_origin,
    robots_allows,
)


@asynccontextmanager
async def _test_lifespan(_app):
    yield


app.router.lifespan_context = _test_lifespan
Base.metadata.create_all(bind=engine)
engine.echo = False

SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
PUBLIC_PATHS = ("/", "/stores", "/tnr", "/faq")
BLOCKED_PATHS = (
    "/admin",
    "/admin/login",
    "/admin/customers",
    "/m",
    "/m/doctor",
    "/api/apply",
    "/api/app/1/status",
    "/api/admin/customer/find-by-phone",
    "/site",
    "/site/case",
    "/site-preview",
    "/site-preview/content/1",
    "/consent/token",
    "/follow-up/token",
    "/inpatient/owner/token",
    "/inpatient/staff/token",
    "/wecom/callback",
    "/showcase",
    "/rabies",
    "/miniapp",
    "/miniapp/tnr",
    "/file/1",
    "/uploads/a.jpg",
    "/static/manifest.json",
    "/sw.js",
    "/stores/private",
    "/faq/extra",
)


def _visible(html: str) -> str:
    without_scripts = re.sub(r"<script\b[^>]*>.*?</script>", " ", html, flags=re.I | re.S)
    return re.sub(r"<[^>]+>", " ", without_scripts)


def _json_ld(html: str) -> str:
    match = re.search(
        r'<script type="application/ld\+json">(.*?)</script>',
        html,
        flags=re.S,
    )
    assert match, "页面缺少 JSON-LD"
    return match.group(1)


def _care_nodes(payload: dict) -> list[dict]:
    graph = payload.get("@graph") or [payload]
    return [node for node in graph if node.get("@type") == "VeterinaryCare"]


def test_origin_stays_on_public_site():
    assert resolve_public_origin(None) == PUBLIC_ORIGIN
    assert resolve_public_origin("https://www.dafopet.com") == PUBLIC_ORIGIN
    assert resolve_public_origin("https://api.dafopet.com") == PUBLIC_ORIGIN
    assert resolve_public_origin("http://evil.example") == PUBLIC_ORIGIN
    xml = build_sitemap_xml(origin="http://evil.example")
    assert "evil.example" not in xml
    assert f"{PUBLIC_ORIGIN}/stores" in xml


def test_store_numbers_are_the_confirmed_pair():
    by_anchor = {store["anchor"]: store for store in PUBLIC_STORES}
    henggang = by_anchor["henggang"]
    donghuan = by_anchor["donghuan"]
    assert henggang["landline_local"] == "0755-28704890"
    assert henggang["landline_intl"] == "+86-755-28704890"
    assert henggang["mobile_local"] == "17820633031"
    assert donghuan["landline_local"] == "0755-28018071"
    assert donghuan["landline_intl"] == "+86-755-28018071"
    assert donghuan["mobile_local"] == "18026901718"
    assert henggang["full_address"] == "深圳市龙岗区横岗街道华侨新村社区隆盛花园S2栋商铺A1013"
    assert donghuan["full_address"] == "深圳市龙华区龙华街道三联社区建设路150号聚豪国际108"
    assert RETIRED_HENGGANG_LANDLINE not in str(PUBLIC_STORES)
    assert "2870890" not in str(PUBLIC_STORES)


def test_public_pages_are_server_rendered():
    with TestClient(app) as client:
        pages = {}
        for path in PUBLIC_PATHS:
            response = client.get(path)
            assert response.status_code == 200, path
            assert "text/html" in response.headers["content-type"]
            assert response.headers["x-robots-tag"] == "index, follow"
            pages[path] = response.text

    for path, html in pages.items():
        assert "2870890" not in html
        assert "24小时" not in html
        assert "二十四小时" not in html
        assert "急诊" not in html
        assert "免费绝育" not in html
        assert "免费手术" not in html
        assert "aggregateRating" not in html
        assert "reviewCount" not in html
        assert "priceRange" not in html
        assert "11:00" not in html
        assert "13:00" not in html
        assert 'href="/admin' not in html
        assert 'href="/m' not in html
        assert "window.TNR_HOURS" not in html
        assert "tnr_hours.js" not in html
        assert 'href="/static/public_geo.css"' in html
        assert "资质" not in html
        assert "许可证" not in html
        assert f'rel="canonical" href="{PUBLIC_ORIGIN}{path if path != "/" else "/"}"' in html or (
            path == "/" and f'rel="canonical" href="{PUBLIC_ORIGIN}/"' in html
        )
        assert 'name="description"' in html
        assert "大风动物医院" in _visible(html)
        assert "09:00–22:00" in _visible(html)

    home = pages["/"]
    visible_home = _visible(home)
    assert 'id="f"' in home
    assert 'id="applicant_name"' in home
    assert 'name="agree_ear_tip"' in home
    assert 'name="agree_no_pet_fraud"' in home
    assert "大风动物医院（东环店）" in home
    assert "大风动物医院（横岗店）" in home
    assert 'fetch("/api/apply"' in home
    assert "id=\"shenzhen-regions-data\"" in home
    assert "/static/shenzhen_regions.json" in home
    assert "window.__syncAddressField" in home
    assert "请选择区、街道，并填写详细地址。" in home
    assert "请至少上传 1 张申请照片。" in home
    assert "agree_ear_tip" in home
    assert "agree_no_pet_fraud" in home
    assert "网页端为备用入口" in visible_home
    for store in PUBLIC_STORES:
        assert store["full_address"] in visible_home
        assert store["landline_local"] in visible_home
        assert store["mobile_local"] in visible_home

    stores = _visible(pages["/stores"])
    assert "医院业务电话" in stores
    assert "以当时通知为准" in stores

    tnr = _visible(pages["/tnr"])
    assert "dafopet" in tnr
    assert "https://dafopet.com/" in tnr
    assert "剪耳标记" in tnr
    assert "不可将家养猫冒充" in tnr
    assert "不承诺固定手术时段" in tnr
    assert "门诊、疫苗、洗护" in tnr

    faq = _visible(pages["/faq"])
    assert len(FAQ_ITEMS) == 7
    for item in FAQ_ITEMS:
        assert item["question"] in faq
        assert item["answer"] in faq

    home_ld = __import__("json").loads(_json_ld(pages["/"]))
    store_ld = __import__("json").loads(_json_ld(pages["/stores"]))
    for payload in (home_ld, store_ld):
        cares = _care_nodes(payload)
        assert len(cares) == 2
        names = {node["name"] for node in cares}
        assert names == {"大风动物医院（横岗店）", "大风动物医院（东环店）"}
        phones = {node["telephone"] for node in cares}
        assert phones == {"+86-755-28704890", "+86-755-28018071"}
        for node in cares:
            assert node["openingHours"] == "Mo-Su 09:00-22:00"
            spec = node["openingHoursSpecification"]
            assert spec["opens"] == "09:00"
            assert spec["closes"] == "22:00"
            assert len(spec["dayOfWeek"]) == 7
            assert "Asia/Shanghai" in node["description"]
            assert node["contactPoint"]["contactType"] == "医院业务电话"
            assert "latitude" not in node
            assert "longitude" not in node
            assert "aggregateRating" not in node
            assert "priceRange" not in node
            assert node["address"]["addressCountry"] == "CN"
            assert node["address"]["addressRegion"] == "广东省"
            assert node["address"]["addressLocality"] == "深圳市"
        assert {node["contactPoint"]["telephone"] for node in cares} == {
            "+86-17820633031",
            "+86-18026901718",
        }
        org = next(node for node in payload["@graph"] if node["@type"] == "Organization")
        assert org["name"] == "大风动物医院"
        assert org["foundingDate"] == "2018"
        assert len(org["department"]) == 2

    faq_ld = __import__("json").loads(_json_ld(pages["/faq"]))
    assert faq_ld["@type"] == "FAQPage"
    assert len(faq_ld["mainEntity"]) == 7
    assert faq_ld["mainEntity"][0]["name"] == FAQ_ITEMS[0]["question"]
    assert "aggregateRating" not in _json_ld(pages["/faq"])

    tnr_ld = __import__("json").loads(_json_ld(pages["/tnr"]))
    assert tnr_ld["@type"] == "WebPage"
    assert tnr_ld["url"] == f"{PUBLIC_ORIGIN}/tnr"


def test_robots_and_sitemap_expose_only_public_pages():
    with TestClient(app) as client:
        robots = client.get("/robots.txt")
        sitemap = client.get("/sitemap.xml")
        assert robots.status_code == 200
        assert "text/plain" in robots.headers["content-type"]
        assert sitemap.status_code == 200
        assert "xml" in sitemap.headers["content-type"]
        closed_site = client.get("/site")
        assert closed_site.status_code == 404

        body = robots.text
        assert "User-agent: *" in body
        assert "Disallow: /admin" in body
        assert "Disallow: /api" in body
        assert "Disallow: /m/" in body
        assert "Disallow: /site-preview" in body
        assert "Disallow: /site" in body
        assert "Disallow: /uploads" in body
        assert "Disallow: /" in body
        assert f"Sitemap: {PUBLIC_ORIGIN}/sitemap.xml" in body
        assert "Allow: /site\n" not in body
        for path in PUBLIC_PATHS + ("/sitemap.xml",) + PUBLIC_CRAWL_ASSETS:
            assert robots_allows(body, path), path
            assert robots_allows(body, path + "?v=1"), path
        for path in BLOCKED_PATHS + (
            "/static/manifest.json",
            "/static/tnr_hours.js",
            "/static/uk_minimal.css",
            "/static/public_geo.css.bak",
            "/api/regions/shenzhen",
            "/api/geocode/regeo",
            "/admin/login",
        ):
            assert not robots_allows(body, path), path
        rules = build_robots_txt(allow_cms=False)
        css_rule = "Allow: /static/public_geo.css$"
        assert css_rule in rules
        assert len("/static/public_geo.css$") > len("/static")
        assert robots_allows(rules, "/static/public_geo.css")
        assert not robots_allows(rules, "/static/manifest.json")

        root = ET.fromstring(sitemap.text)
        locs = [node.text for node in root.findall("sm:url/sm:loc", SITEMAP_NS)]
        assert locs == [
            f"{PUBLIC_ORIGIN}/",
            f"{PUBLIC_ORIGIN}/stores",
            f"{PUBLIC_ORIGIN}/tnr",
            f"{PUBLIC_ORIGIN}/faq",
        ]
        joined = "\n".join(locs)
        assert "/admin" not in joined
        assert "/api" not in joined
        assert "/site" not in joined
        assert "2870890" not in sitemap.text

        db = SessionLocal()
        try:
            brand = db.get(BrandSettings, 1)
            assert brand is not None
            brand.public_domain = "http://evil.example"
            brand.public_site_enabled = True
            brand.allow_indexing = True
            db.add(PublicContent(
                content_type="article",
                title="草稿不应出现",
                slug="draft-secret",
                status="draft",
            ))
            db.add(PublicContent(
                content_type="article",
                title="已发布说明",
                slug="public-note",
                status="published",
            ))
            db.commit()
        finally:
            db.close()

        opened = client.get("/robots.txt")
        opened_map = client.get("/sitemap.xml")
        assert "Allow: /site" in opened.text
        assert "Disallow: /site\n" not in opened.text
        assert robots_allows(opened.text, "/site")
        assert robots_allows(opened.text, "/site/public-note")
        assert not robots_allows(opened.text, "/site-preview")
        assert not robots_allows(opened.text, "/admin")
        assert "evil.example" not in opened_map.text
        opened_root = ET.fromstring(opened_map.text)
        opened_locs = [node.text for node in opened_root.findall("sm:url/sm:loc", SITEMAP_NS)]
        assert f"{PUBLIC_ORIGIN}/site" in opened_locs
        assert f"{PUBLIC_ORIGIN}/site/public-note" in opened_locs
        assert all("draft-secret" not in loc for loc in opened_locs)
        assert all(loc.startswith(PUBLIC_ORIGIN) for loc in opened_locs)


def test_existing_tnr_routes_remain():
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/apply" in paths
    assert "/api/apply/create" in paths
    assert "/api/apply/{app_id}/upload-media" in paths
    assert "/api/apply/{app_id}/finalize" in paths
    assert "/api/app/{app_id}/status" in paths

    with TestClient(app) as client:
        before = SessionLocal()
        try:
            assert before.query(Application).count() == 0
        finally:
            before.close()

        rejected = client.post("/api/apply", data={
            "applicant_name": "测试",
            "phone": "13800138000",
            "address": "广东省深圳市南山区测试路1号",
            "cat_gender": "unknown",
            "agree_ear_tip": "false",
            "agree_no_pet_fraud": "false",
        })
        assert rejected.status_code == 400
        created = client.post("/api/apply/create", data={
            "applicant_name": "测试",
            "phone": "13800138000",
            "address": "广东省深圳市南山区测试路1号",
            "cat_gender": "unknown",
            "agree_ear_tip": "false",
            "agree_no_pet_fraud": "false",
        })
        assert created.status_code == 400

        after = SessionLocal()
        try:
            assert after.query(Application).count() == 0
        finally:
            after.close()

        missing = client.get("/api/app/1/status")
        assert missing.status_code == 404
        assert "applicant_name" not in missing.text

        wrong_method = client.get("/api/apply")
        assert wrong_method.status_code == 405

        admin = client.get("/admin")
        assert admin.status_code == 200
        assert "登录" in admin.text
        login = client.get("/admin/login")
        assert login.status_code == 200
        assert "医院后台登录" in login.text
        assert 'type="password"' in login.text
        assert 'action="/admin/login"' in login.text or "admin/login" in login.text

        db = SessionLocal()
        try:
            brand = db.get(BrandSettings, 1)
            if brand is not None:
                brand.public_site_enabled = False
                brand.allow_indexing = False
                db.commit()
        finally:
            db.close()
        preview = client.get("/site")
        assert preview.status_code == 404
        preview_path = client.get("/site-preview", follow_redirects=False)
        assert preview_path.status_code in (401, 403, 303)


if __name__ == "__main__":
    test_origin_stays_on_public_site()
    test_store_numbers_are_the_confirmed_pair()
    test_public_pages_are_server_rendered()
    test_robots_and_sitemap_expose_only_public_pages()
    test_existing_tnr_routes_remain()
    print("ok")
