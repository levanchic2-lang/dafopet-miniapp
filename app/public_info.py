"""已确认的公开门店与流浪猫绝育说明。

页面可见文字和 JSON-LD 都从这里生成，避免两套文案各写一遍。
未确认的手术时段、名额、审核时限、资质和疗效不在这里。
"""
from __future__ import annotations

import json
from html import escape as xml_escape

CANONICAL_ORIGIN = "https://dafopet.com"
HOSPITAL_NAME = "大风动物医院"
FOUNDING = "始于 2018"
HOURS_LABEL = "每日 09:00–22:00（北京时间）"
HOURS_NOTE = "以上时间为门店对外营业时间，不是流浪猫绝育手术时段。"
OPENS = "09:00"
CLOSES = "22:00"

APPLY_TEXT = "无主流浪猫可通过微信小程序 dafopet 申请。"
FEE_FREE = (
    "基础绝育免费，包含手术费、吸入麻醉、监护、疼痛管理、长效消炎和当天止痛。"
    "其中术前检查的免费范围仅是血常规。生化及其他检查按原价六折。"
)
FEE_EXTRA = "引产加 200 元，隐睾加 200 元，住院 30 元/天，驱虫 30 元/次，流浪猫猫三联疫苗 30 元/针。两店一致。"
FEE_LIMIT = "未在上面列出的术后治疗或其他额外项目，不按免费理解。"
ELIGIBILITY = "仅限无主流浪猫。家养猫不能申请。"
STORES_INTRO = "大风动物医院在深圳设有两家门店。"

PUBLIC_PATHS = ("/stores", "/tnr")
# 这些前缀对应后台、接口、带凭证的客户页和上传文件。公开说明页不在其中。
PRIVATE_PREFIXES = (
    "/admin/",
    "/admin",
    "/m/",
    "/api/",
    "/site-preview",
    "/consent",
    "/c/",
    "/follow-up",
    "/inpatient",
    "/insurance-materials",
    "/exam-upload",
    "/file",
    "/uploads",
    "/wecom",
    "/showcase",
)

STORES = (
    {
        "slug": "henggang",
        "name": "大风动物医院（横岗店）",
        "address": "深圳市龙岗区横岗街道华侨新村社区隆盛花园S2栋商铺A1013",
        "landline": "0755-28704890",
        "mobile": "17820633031",
    },
    {
        "slug": "donghuan",
        "name": "大风动物医院（东环店）",
        "address": "深圳市龙华区龙华街道三联社区建设路150号聚豪国际108",
        "landline": "0755-28018071",
        "mobile": "18026901718",
    },
)

_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def faq_items() -> list[dict[str, str]]:
    where = "。".join(f"{store['name']}：{store['address']}" for store in STORES) + "。"
    contact = "。".join(
        f"{store['name']}座机 {store['landline']}，业务手机 {store['mobile']}"
        for store in STORES
    ) + "。"
    return [
        {"q": "两家门店分别在哪里？", "a": where},
        {"q": "如何联系医院？", "a": contact},
        {
            "q": "营业时间是什么？",
            "a": f"两家门店对外营业时间均为{HOURS_LABEL}。{HOURS_NOTE}",
        },
        {"q": "哪些猫可以申请流浪猫绝育？", "a": ELIGIBILITY},
        {"q": "如何申请流浪猫绝育？", "a": APPLY_TEXT},
        {"q": "流浪猫绝育如何收费？", "a": f"{FEE_FREE}{FEE_EXTRA}{FEE_LIMIT}"},
    ]


def tnr_paragraphs() -> list[str]:
    return [APPLY_TEXT, FEE_FREE, FEE_EXTRA, f"{FEE_LIMIT}{ELIGIBILITY}"]


def homepage_facts() -> dict[str, str]:
    """首页可抓取的短说明。地址和电话取自门店页同一份资料。"""
    by_slug = {store["slug"]: store for store in STORES}
    dong = by_slug["donghuan"]
    heng = by_slug["henggang"]
    return {
        "lead": (
            f"{HOSPITAL_NAME}在深圳设有两家门店，对外营业时间均为{HOURS_LABEL}，"
            "并非流浪猫绝育手术时段。"
        ),
        "dong_label": "东环店",
        "dong_address": dong["address"],
        "dong_landline": dong["landline"],
        "dong_mobile": dong["mobile"],
        "heng_label": "横岗店",
        "heng_address": heng["address"],
        "heng_landline": heng["landline"],
        "heng_mobile": heng["mobile"],
        "eligibility": "仅限无主流浪猫，可通过微信小程序 dafopet 申请流浪猫绝育",
        "other_rules": "其他机构公益项目的规则不能作为本院申请依据。",
    }


def _json_for_script(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _store_entity(store: dict[str, str]) -> dict:
    url = f"{CANONICAL_ORIGIN}/stores#{store['slug']}"
    return {
        "@type": "VeterinaryCare",
        "@id": url,
        "name": store["name"],
        "url": url,
        "description": HOURS_NOTE,
        "parentOrganization": {
            "@type": "MedicalOrganization",
            "name": HOSPITAL_NAME,
            "foundingDate": "2018",
            "url": f"{CANONICAL_ORIGIN}/stores",
        },
        "address": {
            "@type": "PostalAddress",
            "streetAddress": store["address"],
            "addressCountry": "CN",
        },
        "telephone": store["landline"],
        "contactPoint": [
            {"@type": "ContactPoint", "telephone": store["landline"], "contactType": "座机"},
            {"@type": "ContactPoint", "telephone": store["mobile"], "contactType": "业务手机"},
        ],
        "openingHoursSpecification": {
            "@type": "OpeningHoursSpecification",
            "dayOfWeek": [f"https://schema.org/{day}" for day in _WEEKDAYS],
            "opens": OPENS,
            "closes": CLOSES,
        },
    }


def stores_jsonld() -> dict:
    return {
        "@context": "https://schema.org",
        "@graph": [_store_entity(store) for store in STORES],
    }


def faq_jsonld() -> dict:
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "url": f"{CANONICAL_ORIGIN}/tnr",
        "inLanguage": "zh-CN",
        "mainEntity": [
            {
                "@type": "Question",
                "name": item["q"],
                "acceptedAnswer": {"@type": "Answer", "text": item["a"]},
            }
            for item in faq_items()
        ],
    }


def page_context(request, page: str) -> dict:
    if page == "stores":
        title = "门店信息 · 大风动物医院"
        description = "大风动物医院横岗店与东环店的地址、座机、业务手机和每日营业时间。"
        path = "/stores"
        json_ld = _json_for_script(stores_jsonld())
    elif page == "tnr":
        title = "流浪猫绝育说明 · 大风动物医院"
        description = "无主流浪猫可通过微信小程序 dafopet 申请。本页说明基础绝育的免费范围和两店一致的附加项目。"
        path = "/tnr"
        json_ld = _json_for_script(faq_jsonld())
    else:
        raise ValueError(page)
    return {
        "request": request,
        "page": page,
        "title": title,
        "description": description,
        "canonical": f"{CANONICAL_ORIGIN}{path}",
        "json_ld": json_ld,
        "hospital_name": HOSPITAL_NAME,
        "founding": FOUNDING,
        "intro": STORES_INTRO,
        "hours_label": HOURS_LABEL,
        "hours_note": HOURS_NOTE,
        "stores": STORES,
        "paragraphs": tnr_paragraphs(),
        "faq": faq_items(),
    }


def build_robots_txt(*, site_indexable: bool, brand_domain: str) -> str:
    lines = ["User-agent: *", *(f"Disallow: {prefix}" for prefix in PRIVATE_PREFIXES)]
    if site_indexable:
        lines.append("Allow: /site")
    else:
        lines.append("Disallow: /site")
    # /site 这个前缀也会命中 /sitemap.xml。站点地图本身要能被抓取。
    lines.append("Allow: /sitemap.xml")
    lines.append(f"Sitemap: {CANONICAL_ORIGIN}/sitemap.xml")
    brand = (brand_domain or "").strip().rstrip("/")
    if site_indexable and brand.startswith("https://") and brand != CANONICAL_ORIGIN:
        lines.append(f"Sitemap: {brand}/sitemap.xml")
    return "\n".join(lines) + "\n"


def build_sitemap_xml(extra_locs: list[str]) -> str:
    locs = [f"{CANONICAL_ORIGIN}{path}" for path in PUBLIC_PATHS]
    for loc in extra_locs:
        if loc not in locs:
            locs.append(loc)
    body = "".join(f"<url><loc>{xml_escape(loc)}</loc></url>" for loc in locs)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{body}</urlset>"
    )
