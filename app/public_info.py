"""已批准的公开医院资料。

只包含用户确认过的门店事实和现有 TNR 申请说明。
不读取客户、病历或预约记录，也不写数据库。
横岗座机只使用 0755-28704890。0755-2870890 是已作废的笔误，禁止出现。
"""

from __future__ import annotations

import json
import re
from html import escape


PUBLIC_ORIGIN = "https://dafopet.com"
HOSPITAL_NAME = "大风动物医院"
HOURS_TEXT = "每日 09:00–22:00（北京时间）"
HOURS_SCHEMA = "Mo-Su 09:00-22:00"
OPENS = "09:00"
CLOSES = "22:00"

# 已知作废号码。渲染和结构化数据都不得包含它。
RETIRED_HENGGANG_LANDLINE = "0755-2870890"

_WEEKDAYS = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)


def _full_address(locality: str, district: str, street: str) -> str:
    return f"{locality}{district}{street}"


def _store(
    *,
    anchor: str,
    name: str,
    locality: str,
    district: str,
    street: str,
    landline_local: str,
    landline_intl: str,
    mobile_local: str,
    mobile_intl: str,
) -> dict[str, str]:
    return {
        "anchor": anchor,
        "name": name,
        "locality": locality,
        "district": district,
        "street": street,
        "full_address": _full_address(locality, district, street),
        "landline_local": landline_local,
        "landline_intl": landline_intl,
        "landline_tel": "tel:+86" + landline_local[1:].replace("-", ""),
        "mobile_local": mobile_local,
        "mobile_intl": mobile_intl,
        "mobile_tel": "tel:+86" + mobile_local,
        "hours_text": HOURS_TEXT,
    }


PUBLIC_STORES: tuple[dict[str, str], ...] = (
    _store(
        anchor="henggang",
        name="大风动物医院（横岗店）",
        locality="深圳市",
        district="龙岗区",
        street="横岗街道华侨新村社区隆盛花园S2栋商铺A1013",
        landline_local="0755-28704890",
        landline_intl="+86-755-28704890",
        mobile_local="17820633031",
        mobile_intl="+86-17820633031",
    ),
    _store(
        anchor="donghuan",
        name="大风动物医院（东环店）",
        locality="深圳市",
        district="龙华区",
        street="龙华街道三联社区建设路150号聚豪国际108",
        landline_local="0755-28018071",
        landline_intl="+86-755-28018071",
        mobile_local="18026901718",
        mobile_intl="+86-18026901718",
    ),
)

HOME_LEAD = "始于 2018"
HOME_PARAGRAPHS: tuple[str, ...] = (
    "大风动物医院在深圳设有两家门店：位于龙岗区的横岗店，以及位于龙华区的东环店。",
    "我们为到院宠物提供诊疗相关服务，并为符合条件的流浪猫提供绝育（TNR）申请通道。具体能否办理、如何安排，需以到院核对和医生判断为准。",
    f"门店对外营业时间（两店相同）：{HOURS_TEXT}。",
    "查找门店、联系电话和申请方式，请查看「门店信息」与「预约与申请」。",
)

STORE_NOTE = (
    "以上时间为门店对外营业时间。流浪猫绝育（TNR）需先申请并经预审，"
    "到院具体时间由医院与申请人约定，以当时通知为准。"
)

TNR_INTRO = "以下路径是现有申请方式，不是新的办理流程。"
TNR_STEPS: tuple[str, ...] = (
    "推荐使用微信小程序：在微信中搜索小程序「dafopet」。小程序可查看申请进度、接收审核通知，并预约到院时间。",
    "也可用网页申请：打开 https://dafopet.com/ ，按页面填写申请。网页端为备用入口，适合无法使用微信小程序的情况。",
    "申请时请如实填写信息，并上传能反映猫咪室外或流浪场景的清晰照片；可同时上传短视频辅助说明。",
    "通过预审后，医院将与申请人联系，约定到院时间。请保持手机畅通。",
    "申请人需知悉并同意：术后按规范进行剪耳标记，作为已绝育流浪猫的标识；所申请猫只须为流浪或无主场景，不可将家养猫冒充申请。",
    "图像识别结论仅供辅助参考，最终以到院核对与医生判断为准。当前服务范围仅限广东省深圳市。",
)
TNR_NO_PROMISE = "本页不承诺免费、不承诺固定手术时段、不承诺审核通过时限。"
TNR_OTHER = "门诊、疫苗、洗护等其他预约方式，请以医院届时公布的小程序或电话指引为准。联系电话见「门店信息」。"

FAQ_ITEMS: tuple[dict[str, str], ...] = (
    {
        "question": "两家店分别在哪里？",
        "answer": (
            "横岗店在深圳市龙岗区横岗街道华侨新村社区隆盛花园S2栋商铺A1013。"
            "东环店在深圳市龙华区龙华街道三联社区建设路150号聚豪国际108。"
        ),
    },
    {
        "question": "怎么联系医院？",
        "answer": (
            "横岗店座机 0755-28704890，手机 17820633031。"
            "东环店座机 0755-28018071，手机 18026901718。"
        ),
    },
    {
        "question": "营业时间是？",
        "answer": f"两家店对外营业时间均为{HOURS_TEXT}。这是门店营业时间，不等于某一项服务随时可以办理。",
    },
    {
        "question": "如何申请流浪猫绝育？",
        "answer": "请通过微信小程序「dafopet」或网页 https://dafopet.com/ 提交申请。预审通过后，医院会联系您约定到院时间。",
    },
    {
        "question": "申请 TNR 需要准备什么？",
        "answer": "需要填写申请人信息，并上传能反映猫咪室外或流浪场景的清晰照片；可选短视频。须同意术后剪耳标记，并承诺申请的是流浪或无主猫，而非家养宠。",
    },
    {
        "question": "家养猫可以走 TNR 申请吗？",
        "answer": "不可以。申请页已明确：不可将家养猫冒充流浪猫申请。",
    },
    {
        "question": "提交后多久能到院？",
        "answer": "以预审结果和医院联系时约定的时间为准。本页不承诺具体等待天数。",
    },
)

PUBLIC_PAGES: dict[str, dict[str, str]] = {
    "home": {
        "key": "home",
        "path": "/",
        "title": "大风动物医院",
        "description": (
            "大风动物医院，深圳龙岗横岗店与龙华东环店。"
            "营业时间每日 09:00–22:00（北京时间）。"
            "流浪猫绝育（TNR）可通过微信小程序 dafopet 或网站申请。"
        ),
    },
    "stores": {
        "key": "stores",
        "path": "/stores",
        "title": "门店信息 · 大风动物医院",
        "description": (
            "大风动物医院横岗店位于深圳龙岗，东环店位于深圳龙华。"
            "页面列明两店地址、座机、医院业务手机和每日 09:00–22:00（北京时间）的营业时间。"
        ),
    },
    "tnr": {
        "key": "tnr",
        "path": "/tnr",
        "title": "预约与申请 · 大风动物医院",
        "description": (
            "流浪猫绝育（TNR）可通过微信小程序 dafopet，或在 https://dafopet.com/ 填写现有网页申请。"
            "预审通过后，医院联系申请人约定到院时间。"
        ),
    },
    "faq": {
        "key": "faq",
        "path": "/faq",
        "title": "常见问题 · 大风动物医院",
        "description": "大风动物医院两家门店的位置、联系电话、营业时间，以及流浪猫绝育（TNR）的现有申请方式。",
    },
}

PUBLIC_INDEX_HEADERS = {"X-Robots-Tag": "index, follow"}

PUBLIC_NAV: tuple[dict[str, str], ...] = (
    {"key": "home", "href": "/", "label": "首页"},
    {"key": "stores", "href": "/stores", "label": "门店信息"},
    {"key": "tnr", "href": "/tnr", "label": "预约与申请"},
    {"key": "faq", "href": "/faq", "label": "常见问题"},
)


def template_facts() -> dict:
    return {
        "hospital_name": HOSPITAL_NAME,
        "home_lead": HOME_LEAD,
        "home_paragraphs": HOME_PARAGRAPHS,
        "stores": PUBLIC_STORES,
        "store_note": STORE_NOTE,
        "tnr_intro": TNR_INTRO,
        "tnr_steps": TNR_STEPS,
        "tnr_no_promise": TNR_NO_PROMISE,
        "tnr_other": TNR_OTHER,
        "faq_items": FAQ_ITEMS,
        "hours_text": HOURS_TEXT,
    }


def resolve_public_origin(configured: str | None = None) -> str:
    """公开规范域名固定为 https://dafopet.com。

    品牌设置里的其他域名不写入 canonical 或 sitemap，避免把内网、接口域名或空值发到检索结果。
    www 与裸域视为同一站点，规范地址用裸域。
    """
    raw = (configured or "").strip().rstrip("/")
    if raw in {PUBLIC_ORIGIN, "https://www.dafopet.com"}:
        return PUBLIC_ORIGIN
    return PUBLIC_ORIGIN


def page_context(key: str, origin: str | None = None) -> dict[str, str]:
    page = dict(PUBLIC_PAGES[key])
    base = resolve_public_origin(origin)
    path = page["path"]
    page["canonical"] = base + "/" if path == "/" else base + path
    return page


def _same_phone(local: str, intl: str) -> bool:
    local_digits = re.sub(r"\D", "", local)
    intl_digits = re.sub(r"\D", "", intl)
    if local_digits.startswith("0"):
        local_digits = "86" + local_digits[1:]
    else:
        local_digits = "86" + local_digits
    return bool(local_digits) and local_digits == intl_digits


def _opening_hours_spec() -> dict:
    return {
        "@type": "OpeningHoursSpecification",
        "dayOfWeek": [f"https://schema.org/{day}" for day in _WEEKDAYS],
        "opens": OPENS,
        "closes": CLOSES,
    }


def veterinary_care(store: dict[str, str], origin: str | None = None) -> dict:
    base = resolve_public_origin(origin)
    url = f"{base}/stores#{store['anchor']}"
    return {
        "@type": "VeterinaryCare",
        "@id": url,
        "name": store["name"],
        "url": url,
        "description": f"{store['name']}。对外营业时间{HOURS_TEXT}（Asia/Shanghai）。",
        "parentOrganization": {"@id": f"{base}/#organization"},
        "address": {
            "@type": "PostalAddress",
            "streetAddress": f"{store['district']}{store['street']}",
            "addressLocality": store["locality"],
            "addressRegion": "广东省",
            "addressCountry": "CN",
        },
        "telephone": store["landline_intl"],
        "openingHours": HOURS_SCHEMA,
        "openingHoursSpecification": _opening_hours_spec(),
        "contactPoint": {
            "@type": "ContactPoint",
            "name": "医院业务电话",
            "contactType": "医院业务电话",
            "telephone": store["mobile_intl"],
            "availableLanguage": "zh-CN",
        },
    }


def organization_node(origin: str | None = None) -> dict:
    base = resolve_public_origin(origin)
    return {
        "@type": "Organization",
        "@id": f"{base}/#organization",
        "name": HOSPITAL_NAME,
        "url": f"{base}/",
        "foundingDate": "2018",
        "department": [{"@id": f"{base}/stores#{store['anchor']}"} for store in PUBLIC_STORES],
    }


def home_json_ld(origin: str | None = None) -> dict:
    base = resolve_public_origin(origin)
    return {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebSite",
                "@id": f"{base}/#website",
                "name": HOSPITAL_NAME,
                "url": f"{base}/",
                "inLanguage": "zh-CN",
                "publisher": {"@id": f"{base}/#organization"},
            },
            organization_node(base),
            *[veterinary_care(store, base) for store in PUBLIC_STORES],
        ],
    }


def stores_json_ld(origin: str | None = None) -> dict:
    base = resolve_public_origin(origin)
    return {
        "@context": "https://schema.org",
        "@graph": [
            organization_node(base),
            *[veterinary_care(store, base) for store in PUBLIC_STORES],
        ],
    }


def tnr_json_ld(origin: str | None = None) -> dict:
    base = resolve_public_origin(origin)
    page = page_context("tnr", base)
    return {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "@id": page["canonical"],
        "url": page["canonical"],
        "name": "预约与申请",
        "description": page["description"],
        "inLanguage": "zh-CN",
        "isPartOf": {"@type": "WebSite", "name": HOSPITAL_NAME, "url": f"{base}/"},
        "about": {"@type": "Organization", "name": HOSPITAL_NAME, "url": f"{base}/"},
    }


def faq_json_ld(origin: str | None = None) -> dict:
    base = resolve_public_origin(origin)
    page = page_context("faq", base)
    return {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "@id": page["canonical"],
        "url": page["canonical"],
        "inLanguage": "zh-CN",
        "mainEntity": [
            {
                "@type": "Question",
                "name": item["question"],
                "acceptedAnswer": {"@type": "Answer", "text": item["answer"]},
            }
            for item in FAQ_ITEMS
        ],
    }


def dumps_json_ld(data: dict) -> str:
    raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return raw.replace("<", r"\u003c").replace(">", r"\u003e").replace("&", r"\u0026")


def build_robots_txt(*, allow_cms: bool) -> str:
    """默认拒绝抓取，只放行四张公开页和 sitemap。

    规则按 RFC 9309：最长匹配优先。`$` 表示路径结束。
    `/site` 仅在既有「正式公开网站 + 允许索引」开关同时打开时放行；预览路径始终关闭。
    """
    lines = [
        "User-agent: *",
        "Allow: /$",
        "Allow: /stores$",
        "Allow: /tnr$",
        "Allow: /faq$",
        "Allow: /sitemap.xml$",
        "Disallow: /admin",
        "Disallow: /m$",
        "Disallow: /m/",
        "Disallow: /api",
        "Disallow: /site-preview",
        "Disallow: /consent",
        "Disallow: /follow-up",
        "Disallow: /inpatient",
        "Disallow: /wecom",
        "Disallow: /showcase",
        "Disallow: /rabies",
        "Disallow: /miniapp",
        "Disallow: /file",
        "Disallow: /uploads",
        "Disallow: /static",
        "Disallow: /sw.js$",
    ]
    if allow_cms:
        lines.append("Allow: /site")
    else:
        lines.append("Disallow: /site")
    lines.append("Disallow: /")
    lines.append(f"Sitemap: {PUBLIC_ORIGIN}/sitemap.xml")
    return "\n".join(lines) + "\n"


def build_sitemap_xml(extra_paths: list[str] | None = None, origin: str | None = None) -> str:
    base = resolve_public_origin(origin)
    paths = [page["path"] for page in PUBLIC_PAGES.values()]
    for extra in extra_paths or []:
        if extra not in paths and extra.startswith("/"):
            paths.append(extra)
    body = []
    for path in paths:
        loc = base + "/" if path == "/" else base + path
        body.append(f"<url><loc>{escape(loc)}</loc></url>")
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(body)
        + "</urlset>"
    )


def robots_allows(robots_txt: str, path: str) -> bool:
    """按最长路径规则判断。长度相同的规则，文件中先出现的生效。"""
    if "?" in path:
        path = path.split("?", 1)[0]
    if not path.startswith("/"):
        path = "/" + path
    rules = _robot_rules(robots_txt)
    best: tuple[int, int, bool] | None = None
    for index, (allowed, pattern) in enumerate(rules):
        if not pattern or not _robot_match(pattern, path):
            continue
        rank = (len(pattern), -index)
        if best is None or rank > (best[0], best[1]):
            best = (rank[0], rank[1], allowed)
    if best is None:
        return True
    return best[2]


def _robot_rules(robots_txt: str) -> list[tuple[bool, str]]:
    rules: list[tuple[bool, str]] = []
    matched_group = False
    seen_agent = False
    for line in robots_txt.splitlines():
        raw = line.split("#", 1)[0].strip()
        if not raw or ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            if seen_agent and matched_group:
                break
            seen_agent = True
            matched_group = value == "*"
            continue
        if matched_group and key == "allow":
            rules.append((True, value))
        elif matched_group and key == "disallow":
            rules.append((False, value))
    return rules


def _robot_match(pattern: str, path: str) -> bool:
    if pattern.endswith("$"):
        return path == pattern[:-1]
    return path.startswith(pattern)


def assert_public_facts() -> None:
    """启动时拦住作废号码和两套电话写错。"""
    blob = json.dumps(
        {
            "stores": PUBLIC_STORES,
            "faq": FAQ_ITEMS,
            "steps": TNR_STEPS,
            "home": HOME_PARAGRAPHS,
        },
        ensure_ascii=False,
    )
    if RETIRED_HENGGANG_LANDLINE.replace("-", "") in re.sub(r"\D", "", blob):
        raise RuntimeError("公开资料含已作废的横岗座机")
    if "2870890" in blob:
        raise RuntimeError("公开资料含已作废的横岗座机")
    for store in PUBLIC_STORES:
        if not _same_phone(store["landline_local"], store["landline_intl"]):
            raise RuntimeError(f"{store['name']} 座机的本地写法与国际写法不是同一号码")
        if not _same_phone(store["mobile_local"], store["mobile_intl"]):
            raise RuntimeError(f"{store['name']} 手机的本地写法与国际写法不是同一号码")
        if store["landline_local"] == RETIRED_HENGGANG_LANDLINE:
            raise RuntimeError("横岗座机使用了已作废号码")


assert_public_facts()
