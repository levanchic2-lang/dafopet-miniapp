"""拍摄模式：默认关闭；打开后只遮当前会话的院名和客户姓名/电话；保存不能写坏档案。"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.filming_mode import (  # noqa: E402
    allow_person_token,
    build_unique_masks,
    mask_html,
    mask_json_text,
    mask_person_name,
    mask_phone_value,
    restore_masked_field,
    unmask_request_body,
)

PASS, FAIL = [], []


def step(name):
    def wrap(fn):
        def inner(*args, **kwargs):
            try:
                result = fn(*args, **kwargs)
                PASS.append(name)
                print(f"  ✓ {name}")
                return result
            except Exception as exc:
                FAIL.append((name, f"{type(exc).__name__}: {exc}"))
                print(f"  ✗ {name}: {type(exc).__name__}: {exc}")
                return None
        return inner
    return wrap


NAMES = ("李小姐", "测试客户A")
PHONES = ("15712346365",)
NAME_SET = frozenset(NAMES)


def sample_html() -> str:
    return """<!doctype html>
<html><head><title>李小姐 · 大风动物医院</title></head>
<body>
<script>var store = '大风动物医院（横岗店）';</script>
<header>大风动物医院 <small>始于 2018</small></header>
<p>主人 李小姐 · 15712346365 · 宠物 小白</p>
<a href="/admin/customers?store=东环店">东环店</a>
<a href="/admin/customers?store=横岗店&q=1">横岗店</a>
<button name="store" value="东环店">东&nbsp;环&nbsp;店</button>
<span>当前店 东环</span>
<span>宠物 东环猫 在横岗街道</span>
<p>始于2018</p>
<p>since 2018</p>
<p>est. 始 于 2018</p>
<p>Veterinary Care · Est. 2018</p>
<p>Dafo Animal Hospital · 搜索小程序 dafopet</p>
<select>
  <option value="大风动物医院（东环店）">大风动物医院（东环店）</option>
</select>
<input type="hidden" name="store" value="大风动物医院（东环店）">
<input name="name" value="李小姐">
<input name="phone" value="15712346365">
<input type="hidden" name="phone" value="15712346365">
<input name="pet_name" value="小白">
<textarea name="phones_extra">15712346365</textarea>
<textarea name="notes">小白很好</textarea>
</body></html>"""


print("\n[拍摄模式] 纯函数")


@step("姓名打码保留姓")
def t_name():
    assert mask_person_name("李小姐") == "李**"
    assert mask_person_name("张三") == "张*"


@step("手机打码保留前三后四")
def t_phone():
    assert mask_phone_value("15712346365") == "157****6365"
    assert mask_phone_value("157-1234-6365") == "157****6365"


@step("页面替换院名，脚本和提交用的门店值不动")
def t_html_brand():
    out = mask_html(sample_html(), NAMES, PHONES, NAME_SET)
    assert "XX动物医院" in out
    assert "Dafo Animal Hospital" not in out
    assert "XX Animal Hospital" in out
    assert "xxpet" in out
    assert "var store = '大风动物医院（横岗店）'" in out
    assert 'value="大风动物医院（东环店）"' in out
    assert ">XX动物医院（一店）</option>" in out
    assert 'href="/admin/customers?store=东环店"' in out
    assert 'href="/admin/customers?store=横岗店&q=1"' in out
    assert ">一店</a>" in out
    assert ">二店</a>" in out
    assert 'value="东环店"' in out
    assert "东&nbsp;环&nbsp;店" not in out
    assert ">一店</button>" in out
    assert "当前店 一店" in out
    assert "东环猫" in out
    assert "横岗街道" in out
    assert "始于" not in out
    assert "since 2018" not in out.lower()
    assert "Est. 2018" not in out
    assert "2018" not in out


@step("客户姓名电话打码，宠物名和隐藏域保留")
def t_html_people():
    out = mask_html(sample_html(), NAMES, PHONES, NAME_SET)
    assert "李**" in out
    assert "157****6365" in out
    assert "小白" in out
    assert 'name="pet_name" value="小白"' in out
    # 看得见的输入框改成打码且不可提交
    assert re.search(r'<input name="name" value="李\*\*" disabled readonly', out)
    assert re.search(r'<input name="phone" value="157\*\*\*\*6365" disabled readonly', out)
    # 隐藏域仍是真实电话，画面上看不到，提交预约时用得到
    assert '<input type="hidden" name="phone" value="15712346365">' in out
    # 备注框不是客户身份字段，正文不动
    assert "<textarea name=\"notes\">小白很好</textarea>" in out
    assert "157****6365" in out
    assert re.search(r"<textarea name=\"phones_extra\" disabled readonly>157\*\*\*\*6365</textarea>", out)


@step("标题里的院名和姓名也被替换")
def t_title():
    out = mask_html(sample_html(), NAMES, PHONES, NAME_SET)
    assert "<title>李** · XX动物医院</title>" in out
    assert "大风动物医院" not in out.split("</title>", 1)[0]


@step("JSON 里的姓名电话和院名打码，域名留着")
def t_json():
    raw = '{"name":"李小姐","phone":"15712346365","pet":"小白","store":"大风动物医院（东环店）","url":"https://dafopet.com/admin"}'
    out = mask_json_text(raw, NAMES, PHONES)
    assert "李**" in out
    assert "157****6365" in out
    assert "小白" in out
    assert "XX动物医院（一店）" in out
    assert "东环" not in out
    assert "dafopet.com" in out


@step("打码值不会覆盖原姓名电话")
def t_restore():
    assert restore_masked_field("李**", "李小姐", filming=True) == "李小姐"
    assert restore_masked_field("157****6365", "15712346365", filming=True) == "15712346365"
    assert restore_masked_field("", "李小姐", filming=True) == "李小姐"
    assert restore_masked_field("王先生", "李小姐", filming=True) == "王先生"
    assert restore_masked_field("李**", "李小姐", filming=False) == "李**"
    from app.services.filming_mode import unmask_store
    assert unmask_store("一店") == "东环店"
    assert unmask_store("二店") == "横岗店"
    assert unmask_store("XX动物医院（一店）") == "大风动物医院（东环店）"
    assert unmask_store("东环店") == "东环店"
    assert unmask_store("大风动物医院（横岗店）") == "大风动物医院（横岗店）"


def doctor_page() -> str:
    return """<!doctype html>
<html><body>
<header class="uk-ribbon-user"><span>梁天兵</span> · 横岗店 · <span style="font-style:italic;">超级管理员</span></header>
<div class="ukm-topbar-sub">横岗店 · 梁天兵</div>
<h1>梁天兵</h1>
<p>主诊 梁天兵</p>
<td>开单人 梁天兵</td>
<td>操作人 李明</td>
<select name="assigned_to">
  <option value="">请选择负责人</option>
  <option value="梁天兵">梁天兵 · 医生</option>
  <option value="李明">李明 · 助理</option>
</select>
<select name="vet_name">
  <option value="梁天兵" selected>梁天兵</option>
  <option value="李明">李明</option>
</select>
<input name="vet_name" value="梁天兵" list="vet-list">
<datalist id="vet-list">
  <option value="梁天兵">
  <option value="李明">
</datalist>
<input type="hidden" name="vet_name" value="梁天兵">
<input name="name" value="李小姐">
<textarea name="notes">今天梁天兵看诊，李小姐陪同</textarea>
<script>var doctor = "梁天兵";</script>
</body></html>"""


DOCTORS = ("梁天兵", "李明", "李华", "李小姐")
DOCTOR_MASKS = build_unique_masks(DOCTORS)
DOCTOR_SET = frozenset(DOCTORS)


def visible_text(page: str) -> str:
    page = re.sub(r"(?is)<script\b[^>]*>.*?</script>", "", page)
    page = re.sub(r"(?is)<style\b[^>]*>.*?</style>", "", page)
    return re.sub(r"<[^>]+>", "", page)


@step("系统账号和职位词不拿来打码")
def t_skip_accounts():
    assert allow_person_token("梁天兵")
    assert allow_person_token("liangtb")
    assert not allow_person_token("admin")
    assert not allow_person_token("医生")
    assert not allow_person_token("二店")
    assert not allow_person_token("超级管理员")


@step("同姓医生打码互不重复")
def t_unique_masks():
    masks = build_unique_masks(["李明", "李华", "梁天兵"])
    assert masks["梁天兵"] == "梁**"
    assert len(set(masks.values())) == 3
    assert masks["李明"] != masks["李华"]


@step("导航和主诊显示打码，下拉提交值仍是真名")
def t_doctor_display():
    out = mask_html(doctor_page(), DOCTORS, PHONES, DOCTOR_SET, DOCTOR_MASKS)
    text = visible_text(out)
    assert "今天梁天兵看诊，李小姐陪同" in text
    shown = text.replace("今天梁天兵看诊，李小姐陪同", "")
    assert "梁天兵" not in shown
    assert "李明" not in shown
    assert "李小姐" not in shown
    assert "梁**" in text
    assert "超级管理员" in text
    assert 'value="梁天兵"' in out
    assert 'value="李明"' in out
    assert ">梁** · 医生</option>" in out
    assert ">李*2 · 助理</option>" in out or ">李* · 助理</option>" in out
    assert '<input type="hidden" name="vet_name" value="梁天兵">' in out
    assert 'name="vet_name" value="梁**"' in out
    assert 'value="梁天兵"' not in out.split("<datalist", 1)[1].split("</datalist>", 1)[0]
    assert 'var doctor = "梁天兵"' in out
    assert "今天梁天兵看诊，李小姐陪同" in out
    assert re.search(r'<input name="name" value="李\*\*" disabled readonly', out)
    assert 'value="李小姐"' not in out


@step("医生姓名打码提交后还原，客户姓名字段不动")
def t_unmask_submit():
    unmasks = {mask: real for real, mask in DOCTOR_MASKS.items()}
    form = unmask_request_body(
        "vet_name=梁**&name=李**&notes=今天梁天兵看诊&csrf_token=abc".encode("utf-8"),
        "application/x-www-form-urlencoded",
        unmasks,
    ).decode("utf-8")
    assert "vet_name=梁天兵" in form or "vet_name=%E6%A2%81%E5%A4%A9%E5%85%B5" in form
    from urllib.parse import parse_qs
    parsed = parse_qs(form)
    assert parsed["vet_name"] == ["梁天兵"]
    assert parsed["name"] == ["李**"]
    assert parsed["notes"] == ["今天梁天兵看诊"]
    import json
    raw = json.dumps({"vet_name": "梁**", "name": "李**", "note": "梁**"}, ensure_ascii=False).encode("utf-8")
    restored = json.loads(unmask_request_body(raw, "application/json", unmasks))
    assert restored["vet_name"] == "梁天兵"
    assert restored["name"] == "李**"
    assert restored["note"] == "梁**"
    boundary = "----film"
    multipart = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="vet_name"\r\n\r\n'
        "梁**\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="a.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
        "梁**\r\n"
        f"--{boundary}--\r\n"
    ).encode("utf-8")
    restored_mp = unmask_request_body(
        multipart, f"multipart/form-data; boundary={boundary}", unmasks,
    )
    assert b'name="vet_name"' in restored_mp
    assert "梁天兵".encode("utf-8") in restored_mp
    file_part = restored_mp.split(b'filename="a.png"', 1)[1]
    assert "梁**".encode("utf-8") in file_part
    assert "梁天兵".encode("utf-8") not in file_part


t_name()
t_phone()
t_html_brand()
t_html_people()
t_title()
t_json()
t_restore()
t_skip_accounts()
t_unique_masks()
t_doctor_display()
t_unmask_submit()


def extract_csrf(page: str) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page)
    return match.group(1) if match else ""


def run_http():
    import httpx

    base = os.environ.get("FILMING_BASE", "http://127.0.0.1:18001")
    print(f"\n[拍摄模式] HTTP {base}")

    def login(client: httpx.Client):
        page = client.get("/admin/login")
        if page.status_code != 200:
            page = client.get("/admin")
        token = extract_csrf(page.text)
        assert token, "登录页没有 csrf"
        resp = client.post(
            "/admin/login",
            data={"username": "admin", "password": "test123456", "csrf_token": token},
        )
        assert resp.status_code == 303, resp.status_code

    @step("默认关闭：客户页仍是真实院名和姓名电话")
    def t_default_off():
        with httpx.Client(base_url=base, follow_redirects=False, timeout=30.0) as client:
            login(client)
            home = client.get("/admin/customers")
            assert home.status_code == 200
            token = extract_csrf(home.text)
            created = client.post(
                "/admin/customers/create",
                data={
                    "csrf_token": token,
                    "name": "李小姐",
                    "phone": "15712346365",
                    "source": "manual",
                },
            )
            assert created.status_code == 303, created.status_code
            loc = created.headers.get("location", "")
            match = re.search(r"/admin/customers/(\d+)", loc)
            assert match, loc
            cust_id = match.group(1)
            page = client.get(f"/admin/customers/{cust_id}")
            assert page.status_code == 200
            assert "大风动物医院" in page.text
            assert "李小姐" in page.text
            assert "15712346365" in page.text
            assert '<span class="film-badge">' not in page.text
            assert "拍摄模式" in page.text  # 开关本身
            return cust_id

    cust_id = t_default_off()
    if not cust_id:
        return

    @step("打开后当前会话打码，另一会话不变")
    def t_on_and_other_session():
        with httpx.Client(base_url=base, follow_redirects=False, timeout=30.0) as filming, \
             httpx.Client(base_url=base, follow_redirects=False, timeout=30.0) as other:
            login(filming)
            login(other)
            page = filming.get(f"/admin/customers/{cust_id}")
            token = extract_csrf(page.text)
            turned = filming.post(
                "/admin/filming-mode",
                data={"csrf_token": token, "next": f"/admin/customers/{cust_id}"},
            )
            assert turned.status_code == 303
            masked = filming.get(f"/admin/customers/{cust_id}")
            assert masked.status_code == 200
            body = masked.text
            assert '<span class="film-badge">拍摄模式</span>' in body
            assert "XX动物医院" in body
            assert "李**" in body
            assert "157****6365" in body
            assert "大风动物医院" not in body, "打码页仍出现医院全名"
            assert "李小姐" not in body, "打码页仍出现客户姓名"
            assert "15712346365" not in body, "打码页仍出现完整手机号"
            assert "始于" not in body
            bench = filming.get("/admin/customers")
            assert bench.status_code == 200
            assert ">一店</a>" in bench.text
            assert ">二店</a>" in bench.text
            assert "store=东环店" in bench.text
            assert "store=横岗店" in bench.text
            assert "始于" not in bench.text
            plain = other.get(f"/admin/customers/{cust_id}")
            assert "大风动物医院" in plain.text
            assert "李小姐" in plain.text
            assert "15712346365" in plain.text
            assert '<span class="film-badge">' not in plain.text
            return token

    token = t_on_and_other_session()
    if not token:
        return

    @step("拍摄模式下保存客户不会把打码姓名电话写进档案")
    def t_save_keeps_real():
        with httpx.Client(base_url=base, follow_redirects=False, timeout=30.0) as filming, \
             httpx.Client(base_url=base, follow_redirects=False, timeout=30.0) as other:
            login(filming)
            login(other)
            page = filming.get(f"/admin/customers/{cust_id}")
            token_now = extract_csrf(page.text)
            # 上一会话可能已关掉。这里保证是开的：没有角标就再拨一次。
            if '<span class="film-badge">' not in page.text:
                filming.post(
                    "/admin/filming-mode",
                    data={"csrf_token": token_now, "next": f"/admin/customers/{cust_id}"},
                )
                page = filming.get(f"/admin/customers/{cust_id}")
                token_now = extract_csrf(page.text)
            saved = filming.post(
                f"/admin/customers/{cust_id}/edit",
                data={
                    "csrf_token": token_now,
                    "name": "李**",
                    "phone": "157****6365",
                    "phones_extra": "138****1111",
                    "address": "拍摄路 1 号",
                    "notes": "拍摄备注可保存",
                    "source": "manual",
                },
            )
            assert saved.status_code == 303, saved.status_code
            plain = other.get(f"/admin/customers/{cust_id}")
            assert plain.status_code == 200
            assert "李小姐" in plain.text
            assert "15712346365" in plain.text
            assert "138****1111" not in plain.text
            assert "拍摄备注可保存" in plain.text
            assert "拍摄路 1 号" in plain.text

    t_save_keeps_real()

    @step("拍摄模式遮住登录医生姓名，提交病历时还原")
    def t_doctor_navbar_and_save():
        from passlib.hash import bcrypt

        from app.database import SessionLocal
        from app.models import AdminUser, Staff, Visit

        db = SessionLocal()
        try:
            if not db.query(AdminUser).filter(AdminUser.username == "梁天兵").first():
                db.add(AdminUser(
                    username="梁天兵",
                    password_hash=bcrypt.hash("test123456"),
                    role="superadmin",
                    store="横岗店",
                    display_name="梁天兵",
                ))
            if not db.query(Staff).filter(Staff.name == "梁天兵").first():
                db.add(Staff(name="梁天兵", store="横岗店", position="医生", status="active"))
            db.commit()
        finally:
            db.close()

        def visible(page: str) -> str:
            page = re.sub(r"(?is)<script\b[^>]*>.*?</script>", "", page)
            page = re.sub(r"(?is)<style\b[^>]*>.*?</style>", "", page)
            return re.sub(r"<[^>]+>", "", page)

        with httpx.Client(base_url=base, follow_redirects=False, timeout=30.0) as doctor, \
             httpx.Client(base_url=base, follow_redirects=False, timeout=30.0) as other:
            page = doctor.get("/admin/login")
            token = extract_csrf(page.text)
            logged = doctor.post(
                "/admin/login",
                data={"username": "梁天兵", "password": "test123456", "csrf_token": token},
            )
            assert logged.status_code == 303, logged.status_code
            home = doctor.get("/admin/customers")
            assert home.status_code == 200, home.status_code
            assert "梁天兵" in visible(home.text), visible(home.text)[:400]
            # 手机「我」页用 m_uk 顶栏和标题展示登录人。今日页副标题是日期，本来就不放姓名。
            me = doctor.get("/m/me")
            assert me.status_code == 200, me.status_code
            assert "梁天兵" in visible(me.text), visible(me.text)[:400]

            token = extract_csrf(home.text)
            turned = doctor.post(
                "/admin/filming-mode",
                data={"csrf_token": token, "next": "/admin/customers"},
            )
            assert turned.status_code == 303, turned.status_code
            masked = doctor.get("/admin/customers")
            assert masked.status_code == 200, masked.status_code
            assert '<span class="film-badge">拍摄模式</span>' in masked.text
            assert "梁天兵" not in visible(masked.text), visible(masked.text)[:500]
            assert "梁**" in visible(masked.text), visible(masked.text)[:500]
            masked_me = doctor.get("/m/me")
            assert "梁天兵" not in visible(masked_me.text), visible(masked_me.text)[:500]
            assert "梁**" in visible(masked_me.text), visible(masked_me.text)[:500]
            assert "横岗店" not in visible(masked_me.text)
            assert "二店" in visible(masked_me.text)

            login(other)
            token = extract_csrf(masked.text)
            created = doctor.post(
                "/admin/visits/create",
                data={
                    "csrf_token": token,
                    "customer_id": "0",
                    "pet_id": "0",
                    "visit_date": "2026-11-09",
                    "visit_type": "outpatient",
                    "vet_name": "梁**",
                    "chief_complaint": "拍摄测试",
                },
            )
            assert created.status_code == 303, created.text[:300]
            db = SessionLocal()
            try:
                visit = db.query(Visit).order_by(Visit.id.desc()).first()
                assert visit is not None
                assert visit.vet_name == "梁天兵", visit.vet_name
                assert visit.created_by == "梁天兵"
                visit_id = visit.id
            finally:
                db.close()
            filmed_visit = doctor.get(f"/admin/visits/{visit_id}")
            assert filmed_visit.status_code == 200, filmed_visit.status_code
            assert "梁天兵" not in visible(filmed_visit.text), visible(filmed_visit.text)[:800]
            assert "梁**" in visible(filmed_visit.text)
            plain_visit = other.get(f"/admin/visits/{visit_id}")
            assert plain_visit.status_code == 200, plain_visit.status_code
            assert "梁天兵" in visible(plain_visit.text), visible(plain_visit.text)[:800]

    t_doctor_navbar_and_save()


if os.environ.get("FILMING_HTTP", "0") == "1":
    try:
        run_http()
    except Exception as exc:
        FAIL.append(("HTTP 拍摄模式", f"{type(exc).__name__}: {exc}"))
        print(f"  ✗ HTTP 拍摄模式: {type(exc).__name__}: {exc}")

print(f"\n拍摄模式测试：通过 {len(PASS)}，失败 {len(FAIL)}")
for name, err in FAIL:
    print(f"  - {name}: {err}")
sys.exit(1 if FAIL else 0)
