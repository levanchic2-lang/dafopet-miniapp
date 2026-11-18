"""公开门店页、绝育说明、抓取配置，以及原申请首页仍在。"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from html import unescape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TEMP_DIR = tempfile.TemporaryDirectory(prefix="tnr-public-geo-")
DB_PATH = Path(TEMP_DIR.name) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH.as_posix()}"
os.environ["SESSION_SECRET"] = "public-geo-test-secret"
os.environ["SESSION_HTTPS_ONLY"] = "0"

from fastapi.testclient import TestClient

from app.database import SessionLocal, init_db
from app.main import app

init_db()
from app.models import Application, BrandSettings, PublicContent
from app.public_info import (
    APPLY_TEXT,
    ELIGIBILITY,
    FEE_EXTRA,
    FEE_FREE,
    FEE_LIMIT,
    HOURS_LABEL,
    HOURS_NOTE,
    STORES,
    faq_items,
)

client = TestClient(app, base_url="https://testserver", follow_redirects=False)
NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
FAILS: list[str] = []

FORBIDDEN = (
    "0755-2870890",
    "13865632224",
    "13823137494",
    "不承诺免费",
    "麻醉未确认",
    "仅含术前血常规",
    "政府资助",
    "无需审核",
    "免费名额",
    "资质",
    "疗效",
    "11:00",
    "13:00",
    "18:00",
    "网页申请",
    "备用入口",
    "24小时",
    "24 小时",
    "家猫可申请",
    "家养猫可以",
    "TNR_HOURS",
    "AggregateRating",
    "priceRange",
)


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"  ✓ {name}")
        return
    FAILS.append(f"{name}: {detail}")
    print(f"  ✗ {name}: {detail}")


def visible(html: str) -> str:
    text = re.sub(r"(?is)<script\b[^>]*>.*?</script>", "", html)
    text = re.sub(r"(?is)<style\b[^>]*>.*?</style>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text)


def jsonld_blocks(html: str) -> list[str]:
    return re.findall(
        r'<script type="application/ld\+json">(.*?)</script>',
        html,
        flags=re.S,
    )


def robots_allows(body: str, path: str) -> bool:
    best: tuple[int, bool] | None = None
    for line in body.splitlines():
        if line.startswith("Allow: "):
            allow, prefix = True, line[len("Allow: "):].strip()
        elif line.startswith("Disallow: "):
            allow, prefix = False, line[len("Disallow: "):].strip()
        else:
            continue
        if prefix.endswith("$"):
            matched = path == prefix[:-1]
            length = len(prefix[:-1])
        else:
            matched = path.startswith(prefix)
            length = len(prefix)
        if matched and (best is None or length > best[0]):
            best = (length, allow)
    return True if best is None else best[1]


def sitemap_locs(xml_text: str) -> list[str]:
    root = ET.fromstring(xml_text)
    return [node.text or "" for node in root.findall("sm:url/sm:loc", NS)]


print("\n[公开资料]")

stores = client.get("/stores")
tnr = client.get("/tnr")
home = client.get("/")
robots = client.get("/robots.txt")
sitemap = client.get("/sitemap.xml")
css = client.get("/static/public_info.css")

check("门店页 200", stores.status_code == 200, str(stores.status_code))
check("绝育说明 200", tnr.status_code == 200, str(tnr.status_code))
check("首页 200", home.status_code == 200, str(home.status_code))
check("样式 200", css.status_code == 200, str(css.status_code))

stores_html = stores.text
tnr_html = tnr.text
home_html = home.text
stores_text = visible(stores_html)
tnr_text = visible(tnr_html)

check(
    "门店页标题与规范地址",
    "<title>门店信息 · 大风动物医院</title>" in stores_html
    and 'rel="canonical" href="https://dafopet.com/stores"' in stores_html
    and 'name="robots" content="index,follow"' in stores_html,
    stores_html[stores_html.find("<title>"):stores_html.find("</title>") + 8],
)
check(
    "绝育说明标题与规范地址",
    "<title>流浪猫绝育说明 · 大风动物医院</title>" in tnr_html
    and 'rel="canonical" href="https://dafopet.com/tnr"' in tnr_html,
    "",
)
check("规范地址不跟测试主机走", "testserver" not in stores_html and "testserver" not in tnr_html, "")

for store in STORES:
    check(
        f"可见资料 {store['name']}",
        store["name"] in stores_text
        and store["address"] in stores_text
        and store["landline"] in stores_text
        and store["mobile"] in stores_text
        and HOURS_LABEL in stores_text,
        "",
    )

check("营业时间说明", HOURS_NOTE in stores_text and HOURS_NOTE in tnr_text, "")
check("始于 2018", "始于 2018" in stores_text and "始于 2018" in tnr_text, "")
check("申请入口只写小程序", APPLY_TEXT in tnr_text and "dafopet" in tnr_text, "")
check("免费范围", FEE_FREE in tnr_text and "吸入麻醉" in tnr_text and "血常规" in tnr_text, "")
check("附加项目", FEE_EXTRA in tnr_text and FEE_LIMIT in tnr_text, "")
check("资格", ELIGIBILITY in tnr_text, "")

for item in faq_items():
    check(f"问答可见 {item['q']}", item["q"] in tnr_text and item["a"] in tnr_text, "")

import json
store_ld = json.loads(jsonld_blocks(stores_html)[0])
faq_ld = json.loads(jsonld_blocks(tnr_html)[0])
graph = store_ld["@graph"]
check("两家门店结构化资料", len(graph) == 2 and {row["name"] for row in graph} == {s["name"] for s in STORES}, "")
for row, store in zip(graph, STORES):
    phones = [point["telephone"] for point in row["contactPoint"]]
    hours = row["openingHoursSpecification"]
    same = (
        row["address"]["streetAddress"] == store["address"]
        and row["telephone"] == store["landline"]
        and phones == [store["landline"], store["mobile"]]
        and hours["opens"] == "09:00"
        and hours["closes"] == "22:00"
        and len(hours["dayOfWeek"]) == 7
        and row["description"] == HOURS_NOTE
        and row["parentOrganization"]["foundingDate"] == "2018"
        and store["address"] in stores_text
        and store["landline"] in stores_text
        and store["mobile"] in stores_text
    )
    check(f"结构化与可见一致 {store['name']}", same, "")

faq_entities = faq_ld["mainEntity"]
faq_same = len(faq_entities) == len(faq_items()) and all(
    entity["name"] == item["q"] and entity["acceptedAnswer"]["text"] == item["a"]
    for entity, item in zip(faq_entities, faq_items())
)
check("问答结构化与可见一致", faq_ld["@type"] == "FAQPage" and faq_same, "")

for label, html in (("门店页", stores_html), ("绝育说明", tnr_html)):
    found = [item for item in FORBIDDEN if item in html]
    check(f"{label}没有未确认承诺", not found, "、".join(found))
    shown = visible(html)
    check(f"{label}可见正文没有英文导航", "Monday" not in shown and "FAQ" not in shown, "")

check("门店页链到说明", 'href="/tnr"' in stores_html, "")
check("说明页链到门店", 'href="/stores"' in tnr_html, "")
check(
    "公开页不加载后台脚本",
    "tnr_hours.js" not in stores_html and "voice_input.js" not in tnr_html,
    "",
)
check("样式没有圆角和阴影", "border-radius" not in css.text and "box-shadow" not in css.text, "")
check("公开页没有第三方统计", "gtag" not in stores_html and "hm.baidu" not in tnr_html, "")

check(
    "首页仍是申请表",
    'id="f"' in home_html
    and 'name="applicant_name"' in home_html
    and 'name="agree_ear_tip"' in home_html
    and 'name="agree_no_pet_fraud"' in home_html
    and 'fetch("/api/apply"' in home_html
    and "推荐使用微信小程序申请" in home_html
    and "网页端为备用入口" in home_html
    and "shenzhen-regions-data" in home_html
    and "window.TNR_HOURS" in home_html
    and ">登录</a>" in home_html,
    "",
)
check(
    "首页只增加资料链接",
    'href="/stores">门店信息与流浪猫绝育说明</a>' in home_html
    and "吸入麻醉" not in home_html
    and "geo_base.html" not in home_html,
    "",
)

with SessionLocal() as db:
    before = db.query(Application).count()
rejected = client.post(
    "/api/apply",
    data={
        "applicant_name": "不应入库",
        "phone": "13800000000",
        "address": "广东省深圳市龙岗区测试路 1 号",
        "cat_gender": "female",
    },
)
with SessionLocal() as db:
    after = db.query(Application).count()
check("未同意的申请仍被拒绝", rejected.status_code == 400 and "请勾选同意" in rejected.text, rejected.text[:180])
check("未同意的申请不写库", before == after, f"{before} -> {after}")

login = client.get("/admin/login")
check("员工登录页仍在", login.status_code == 200 and "登录" in login.text, str(login.status_code))

robots_text = robots.text
check("robots 有站点地图", "Sitemap: https://dafopet.com/sitemap.xml" in robots_text, robots_text)
check("robots 不整站拒绝", "\nDisallow: /\n" not in f"\n{robots_text}" and "Disallow: /$" not in robots_text, "")
for path in ("/", "/stores", "/tnr", "/static/public_info.css", "/miniapp", "/sitemap.xml"):
    check(f"可抓取 {path}", robots_allows(robots_text, path), "")
for path in (
    "/admin",
    "/admin/customers",
    "/m/doctor",
    "/api/apply",
    "/site-preview",
    "/site",
    "/site/published-note",
    "/consent/token",
    "/c/token",
    "/follow-up/token",
    "/inpatient/owner/token",
    "/insurance-materials/token",
    "/exam-upload/token",
    "/file/1",
    "/uploads/a.jpg",
    "/wecom/callback",
    "/showcase",
):
    check(f"不收录 {path}", not robots_allows(robots_text, path), "")

default_locs = sitemap_locs(sitemap.text)
check(
    "默认站点地图只有两张公开页",
    default_locs == [
        "https://dafopet.com/stores",
        "https://dafopet.com/tnr",
    ],
    str(default_locs),
)

with SessionLocal() as db:
    brand = db.get(BrandSettings, 1)
    assert brand is not None
    brand.public_site_enabled = True
    brand.allow_indexing = True
    brand.public_domain = "https://brand.example"
    db.add(PublicContent(slug="published-note", title="已发布", status="published"))
    db.add(PublicContent(slug="draft-note", title="草稿", status="draft"))
    db.commit()

indexed = client.get("/robots.txt").text
indexed_locs = sitemap_locs(client.get("/sitemap.xml").text)
check("原有公开站开启后仍放行 /site", robots_allows(indexed, "/site/published-note"), "")
check("预览路径仍然关闭", not robots_allows(indexed, "/site-preview"), "")
check(
    "规范地址不改成其他域名",
    "https://dafopet.com/stores" in indexed_locs
    and "https://brand.example/stores" not in indexed_locs
    and "https://brand.example/site" in indexed_locs
    and "https://brand.example/site/published-note" in indexed_locs
    and "draft-note" not in "".join(indexed_locs)
    and not any("/consent" in loc or "/admin" in loc for loc in indexed_locs),
    str(indexed_locs),
)
check(
    "其他域名另给一条站点地图",
    "Sitemap: https://dafopet.com/sitemap.xml" in indexed
    and "Sitemap: https://brand.example/sitemap.xml" in indexed,
    "",
)

print(f"\n公开资料测试：失败 {len(FAILS)}")
for item in FAILS:
    print(f"  - {item}")
sys.exit(1 if FAILS else 0)
