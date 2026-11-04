"""拍摄模式：默认关闭；打开后只遮当前会话的院名和客户姓名/电话；保存不能写坏档案。"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.services.filming_mode import (  # noqa: E402
    mask_html,
    mask_json_text,
    mask_person_name,
    mask_phone_value,
    restore_masked_field,
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
    assert ">XX动物医院（东环店）</option>" in out


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
    assert "XX动物医院（东环店）" in out
    assert "dafopet.com" in out


@step("打码值不会覆盖原姓名电话")
def t_restore():
    assert restore_masked_field("李**", "李小姐", filming=True) == "李小姐"
    assert restore_masked_field("157****6365", "15712346365", filming=True) == "15712346365"
    assert restore_masked_field("", "李小姐", filming=True) == "李小姐"
    assert restore_masked_field("王先生", "李小姐", filming=True) == "王先生"
    assert restore_masked_field("李**", "李小姐", filming=False) == "李**"


t_name()
t_phone()
t_html_brand()
t_html_people()
t_title()
t_json()
t_restore()


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
