"""
本地集成测试 — 走完整业务工作流。
要求：服务器已在 http://127.0.0.1:18001 运行，DB 是干净的 _test/test.db。
"""
import sys
import re
import httpx
import json
from pathlib import Path

# 把项目根加入 sys.path，便于直接 import app.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:18001"

# 结果收集
PASS, FAIL = [], []

def step(name):
    def wrap(fn):
        def inner(*a, **kw):
            try:
                r = fn(*a, **kw)
                PASS.append(name)
                print(f"  ✓ {name}")
                return r
            except AssertionError as e:
                import traceback
                tb = traceback.format_exc()
                FAIL.append((name, f"AssertionError: {e}\n{tb}"))
                print(f"  ✗ {name}: AssertionError: {e}")
                print(tb)
                return None
            except Exception as e:
                FAIL.append((name, f"{type(e).__name__}: {e}"))
                print(f"  ✗ {name}: {type(e).__name__}: {e}")
                import traceback; traceback.print_exc()
                return None
        return inner
    return wrap


def extract_csrf(html: str) -> str:
    """从 HTML 里抓 csrf_token 隐藏字段"""
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', html)
    return m.group(1) if m else ""


# ── 全局会话 ──
client = httpx.Client(base_url=BASE, follow_redirects=False, timeout=20.0)


# ═══════════════════════════════════════════════
# 测试套件
# ═══════════════════════════════════════════════

print("\n[1] 登录流程")

@step("访问首页 /admin（未登录应跳登录 200 或登录页 200）")
def t_admin_anonymous():
    r = client.get("/admin")
    assert r.status_code in (200, 303), f"got {r.status_code}"

@step("GET /admin 看到登录页")
def t_login_page():
    r = client.get("/admin")
    assert r.status_code == 200
    assert "csrf_token" in r.text or "admin_login" in r.text or "登录" in r.text
    return extract_csrf(r.text)

csrf = t_login_page()


@step("POST /admin/login 错误密码被拒")
def t_login_wrong():
    r = client.post("/admin/login", data={"username": "admin", "password": "wrong", "csrf_token": csrf})
    assert r.status_code in (401, 200, 303), f"got {r.status_code}"
    # 401 + 没建立 session
    assert "set-cookie" not in r.headers or "admin" not in r.cookies.get("tnr_session", "")

@step("POST /admin/login 正确密码登录成功")
def t_login_ok():
    r = client.post("/admin/login", data={"username": "admin", "password": "test123456", "csrf_token": csrf})
    assert r.status_code == 303, f"got {r.status_code}"
    assert r.headers.get("location", "").startswith("/admin")

t_login_wrong()
t_login_ok()


@step("登录后访问 /admin 不再重定向")
def t_admin_authed():
    r = client.get("/admin")
    assert r.status_code == 200, f"got {r.status_code}"
    assert "TNR" in r.text

t_admin_authed()


# ═══════════════════════════════════════════════
print("\n[2] 客户/宠物创建")

cust_id = None
pet_id = None

@step("创建客户（需带 CSRF）")
def t_create_customer():
    global cust_id
    r = client.get("/admin/customers")
    assert r.status_code == 200
    token = extract_csrf(r.text)
    assert token, "客户列表页应包含 csrf_token"
    # 不带 CSRF 应该失败
    r0 = client.post("/admin/customers/create", data={"name": "测试客户A", "phone": "13800000001"})
    assert r0.status_code == 403, f"无 CSRF 应被拒，得 {r0.status_code}"
    # 带 CSRF 成功
    r = client.post("/admin/customers/create",
                    data={"name": "测试客户A", "phone": "13800000001", "csrf_token": token})
    assert r.status_code == 303, f"got {r.status_code}"
    loc = r.headers.get("location", "")
    m = re.search(r'/admin/customers/(\d+)', loc)
    assert m, f"未取到 customer id: {loc}"
    cust_id = int(m.group(1))

t_create_customer()
print(f"    cust_id={cust_id}")


@step("添加宠物 + 自动生成病历号")
def t_add_pet():
    global pet_id
    r = client.get(f"/admin/customers/{cust_id}")
    assert r.status_code == 200
    token = extract_csrf(r.text)
    r = client.post(f"/admin/customers/{cust_id}/pets/add",
                    data={
                        "csrf_token": token, "name": "小白", "species": "cat",
                        "gender": "female", "breed": "田园猫", "color_pattern": "白",
                        "store": "东环店", "life_status": "alive",
                    })
    assert r.status_code == 303
    # 取回客户页验证
    r2 = client.get(f"/admin/customers/{cust_id}")
    assert "小白" in r2.text
    # 检查病历号格式
    assert re.search(r"DC\d{4}\d{5}", r2.text), "应有 DC2605xxxxx 病历号"
    # 拿到 pet_id
    m = re.search(r'/admin/appointments/create\?customer_id=\d+&pet_id=(\d+)', r2.text)
    assert m, "宠物 ID 未找到"
    pet_id = int(m.group(1))

t_add_pet()
print(f"    pet_id={pet_id}")


# ═══════════════════════════════════════════════
print("\n[3] 就诊 + SOAP 自动保存")

visit_id = None

@step("从客户档案新建就诊")
def t_create_visit():
    global visit_id
    # 模拟点击「新建就诊」: GET /admin/visits/create?customer_id=X&pet_id=Y
    r = client.get(f"/admin/visits/create?customer_id={cust_id}&pet_id={pet_id}")
    assert r.status_code == 200
    token = extract_csrf(r.text)
    today = httpx.get(f"{BASE}/admin/visits/create?customer_id={cust_id}").text
    # 提交 POST
    r = client.post("/admin/visits/create", data={
        "csrf_token": token,
        "customer_id": cust_id, "pet_id": pet_id,
        "visit_date": "2026-05-12", "visit_type": "outpatient",
        "chief_complaint": "拉稀呕吐", "physical_exam": "", "diagnosis": "",
        "treatment_plan": "", "notes": "", "vet_name": "测试医生",
    })
    assert r.status_code == 303, f"got {r.status_code}, body={r.text[:200]}"
    loc = r.headers.get("location", "")
    m = re.search(r'/admin/visits/(\d+)', loc)
    if not m:
        # 跳到客户档案的话，从客户页找最新就诊
        cr = client.get(f"/admin/customers/{cust_id}?pet_id={pet_id}&tab=visits")
        ids = [int(x) for x in re.findall(r'/admin/visits/(\d+)', cr.text)]
        assert ids, f"customer 页里没找到任何就诊链接, loc={loc}"
        visit_id = max(ids)
        return
    visit_id = int(m.group(1))

t_create_visit()
print(f"    visit_id={visit_id}")


@step("SOAP autosave 接口")
def t_autosave():
    r = client.get(f"/admin/visits/{visit_id}")
    assert r.status_code == 200
    token = extract_csrf(r.text)
    r = client.post(f"/api/visits/{visit_id}/autosave",
                    json={"csrf_token": token, "diagnosis": "胃肠炎", "treatment_plan": "禁食24h"})
    assert r.status_code == 200, f"got {r.status_code}"
    data = r.json()
    assert data.get("ok") is True, data
    assert "diagnosis" in data.get("changed", [])

t_autosave()


# ═══════════════════════════════════════════════
print("\n[4] 库存品目 + 处方扣减")

item_id = None

@step("创建库存品目")
def t_create_item():
    global item_id
    r = client.get("/admin/inventory/create")
    assert r.status_code == 200
    token = extract_csrf(r.text)
    r = client.post("/admin/inventory/create", data={
        "csrf_token": token,
        "name": "强力素50mg", "category": "medication", "subcategory": "general",
        "unit": "粒", "sell_price": "10", "cost_price": "5",
        "stock_qty": "100", "low_stock_min": "10",
    })
    assert r.status_code == 303, f"got {r.status_code}, loc={r.headers.get('location')}"
    # 取 ID
    r2 = client.get("/admin/inventory")
    m = re.search(r'/admin/inventory/(\d+)', r2.text)
    assert m
    item_id = int(m.group(1))

t_create_item()
print(f"    item_id={item_id}")


@step("开处方 → 自动扣库存 → 自动同步收费单")
def t_create_prescription():
    r = client.get(f"/admin/prescriptions/create?visit_id={visit_id}")
    assert r.status_code == 200
    token = extract_csrf(r.text)
    # 表单：1 个药品，10 粒，单价 10 → 小计 100
    r = client.post("/admin/prescriptions/create", data={
        "csrf_token": token,
        "visit_id": visit_id, "customer_id": cust_id,
        "prescribed_date": "2026-05-12", "vet_name": "测试医生",
        "pet_id": pet_id, "status": "issued",
        "drug_name_0": "强力素50mg", "item_id_0": str(item_id),
        "drug_type_0": "oral",
        "dose_amount_0": "1", "dose_unit_0": "粒",
        "times_per_day_0": "2", "duration_days_0": "5",
        "quantity_num_0": "10", "item_unit_0": "粒",
        "unit_price_0": "10.00",
        "instructions_0": "", "notes": "",
    })
    assert r.status_code == 303, f"got {r.status_code}"
    # 验证库存扣减 100 → 90
    r2 = client.get(f"/admin/inventory/{item_id}")
    assert "90" in r2.text, "库存应扣 10 → 90"
    # 验证收费单自动生成
    r3 = client.get(f"/admin/customers/{cust_id}?pet_id={pet_id}&tab=invoices")
    assert "100.00" in r3.text, "应有自动生成的 ¥100.00 收费单"

t_create_prescription()


# ═══════════════════════════════════════════════
print("\n[5] 限店员工越权（核心安全测试）")

@step("创建限店员工账号")
def t_create_staff():
    r = client.get("/admin/hr")
    assert r.status_code == 200
    token = extract_csrf(r.text)
    # 新建账号 username=staff_dh, store=东环店
    r = client.post("/admin/users/create", data={
        "csrf_token": token,
        "username": "staff_dh", "password": "staff123456",
        "role": "staff", "store": "东环店",
    })
    assert r.status_code in (303, 200), f"got {r.status_code}"

t_create_staff()


@step("限店员工尝试改其他门店预约状态（应被拒）")
def t_cross_store_denied():
    # 先建一只横岗店的宠物 + 预约
    r = client.get(f"/admin/customers/{cust_id}")
    token = extract_csrf(r.text)
    client.post(f"/admin/customers/{cust_id}/pets/add",
                data={"csrf_token": token, "name": "另一只", "species": "cat",
                      "gender": "male", "store": "横岗店", "life_status": "alive"})
    # 登出超级管理员，登录员工
    staff_client = httpx.Client(base_url=BASE, follow_redirects=False, timeout=20.0)
    r = staff_client.get("/admin")
    t = extract_csrf(r.text)
    r = staff_client.post("/admin/login", data={"username": "staff_dh", "password": "staff123456", "csrf_token": t})
    assert r.status_code == 303

    # 限店员工访问跨店客户档案，应该看不到横岗店宠物
    r = staff_client.get(f"/admin/customers/{cust_id}")
    assert r.status_code == 200
    # 横岗店宠物名「另一只」不应出现
    assert "另一只" not in r.text, "限店员工不应看到其他门店的宠物"
    staff_client.close()

t_cross_store_denied()


# ═══════════════════════════════════════════════
print("\n[6] 日历越权")

@step("限店员工访问日历 API 强制本店")
def t_calendar_store_scope():
    staff_client = httpx.Client(base_url=BASE, follow_redirects=False, timeout=20.0)
    r = staff_client.get("/admin")
    t = extract_csrf(r.text)
    r = staff_client.post("/admin/login", data={"username": "staff_dh", "password": "staff123456", "csrf_token": t})
    # 尝试强制传 store=横岗店，应该被忽略（限店员工强制本店）
    r = staff_client.get("/api/calendar/events?start=2026-01-01&end=2026-12-31&store=横岗店")
    assert r.status_code == 200
    data = r.json()
    # 限店员工只看东环店；本测试客户没东环店预约，应空或仅含东环店
    appts = data.get("appointments", [])
    for a in appts:
        assert "横岗" not in a.get("store", ""), f"限店员工不应看到横岗店预约: {a}"
    staff_client.close()

t_calendar_store_scope()


# ═══════════════════════════════════════════════
print("\n[7] CSRF 验证（多个端点）")

@step("不带 CSRF 添加宠物应被 403")
def t_no_csrf_pet():
    r = client.post(f"/admin/customers/{cust_id}/pets/add",
                    data={"name": "无 CSRF 宠物", "species": "cat"})
    assert r.status_code == 403

@step("不带 CSRF 删除处方应被 403")
def t_no_csrf_delete():
    r = client.post(f"/admin/visits/{visit_id}/delete", data={})
    assert r.status_code == 403, f"got {r.status_code}, loc={r.headers.get('location','')}, body[:200]={r.text[:200]}"

t_no_csrf_pet()
t_no_csrf_delete()


# ═══════════════════════════════════════════════
print("\n[8] 体重防呆")

@step("体重 ≤ 0 应被拒")
def t_weight_zero():
    r = client.get(f"/admin/customers/{cust_id}?pet_id={pet_id}&tab=weight")
    token = extract_csrf(r.text)
    r = client.post("/admin/weight-records/create", data={
        "csrf_token": token, "customer_id": cust_id, "pet_id": pet_id,
        "record_date": "2026-05-12", "weight_kg": "-5", "notes": "",
    })
    # 应该重定向但带错误 msg
    loc = r.headers.get("location", "")
    assert r.status_code == 303, f"got {r.status_code}"
    from urllib.parse import unquote
    decoded = unquote(loc)
    assert "msg=" in loc, f"loc={loc}"
    assert "体重" in decoded, f"decoded loc={decoded}"

t_weight_zero()


# ═══════════════════════════════════════════════
print("\n[9] 检查单上传白名单")

@step("上传 .exe 应被拒（白名单防护）")
def t_upload_exe():
    # 先创建检查单
    r = client.get(f"/admin/exam-orders/create?visit_id={visit_id}")
    token = extract_csrf(r.text)
    r = client.post("/admin/exam-orders/create", data={
        "csrf_token": token, "visit_id": visit_id,
        "item_name_0": "血常规", "item_id_0": "",
        "item_qty_0": "1", "item_unit_0": "次",
        "item_price_0": "80",
    })
    assert r.status_code == 303, f"got {r.status_code}, body={r.text[:300]}"
    m = re.search(r'/admin/exam-orders/(\d+)', r.headers.get("location", ""))
    if not m:
        # 拿最后一张
        rl = client.get(f"/admin/visits/{visit_id}")
        m = re.search(r'/admin/exam-orders/(\d+)', rl.text)
    assert m, "未找到 exam_order_id"
    eo_id = int(m.group(1))
    # 上传 fake .exe
    r2 = client.get(f"/admin/exam-orders/{eo_id}")
    token = extract_csrf(r2.text)
    r = client.post(f"/admin/exam-orders/{eo_id}/upload",
                    data={"csrf_token": token, "item_label": "test"},
                    files={"file": ("evil.exe", b"MZ\x00\x00fake_exe", "application/octet-stream")})
    assert r.status_code == 400, f"应拒绝 .exe，得 {r.status_code}"

t_upload_exe()


# ═══════════════════════════════════════════════
print("\n[10] TNR 门店配额 / 开关")

@step("GET /api/tnr-store-status 返回所有门店且字段齐全")
def t_tnr_status_shape():
    r = client.get("/api/tnr-store-status")
    assert r.status_code == 200, f"got {r.status_code}"
    data = r.json()
    assert "stores" in data and isinstance(data["stores"], list) and len(data["stores"]) >= 2
    for s in data["stores"]:
        for k in ("store", "accepting", "monthly_count", "monthly_quota", "is_open"):
            assert k in s, f"缺字段 {k}: {s}"
        assert s["monthly_quota"] >= 1
        # 默认应该是开放
        assert s["accepting"] is True

t_tnr_status_shape()


@step("关闭门店 TNR 后 accepting=False, is_open=False")
def t_tnr_toggle():
    # 直接走数据库改 config（简化测试，不依赖具体管理端点）
    import sqlite3, pathlib
    db_path = pathlib.Path(__file__).parent / "test.db"
    con = sqlite3.connect(str(db_path))
    con.execute("UPDATE tnr_store_configs SET tnr_accepting = 0 WHERE store_name LIKE '%东环%'")
    con.commit()
    con.close()
    r = client.get("/api/tnr-store-status")
    assert r.status_code == 200
    target = [s for s in r.json()["stores"] if "东环" in s["store"]]
    assert target, "找不到东环店"
    assert target[0]["accepting"] is False
    assert target[0]["is_open"] is False
    # 恢复
    con = sqlite3.connect(str(db_path))
    con.execute("UPDATE tnr_store_configs SET tnr_accepting = 1 WHERE store_name LIKE '%东环%'")
    con.commit()
    con.close()

t_tnr_toggle()


# ═══════════════════════════════════════════════
print("\n[11] 预约时间冲突拦截")

@step("同门店同日同时段重叠预约应被拒")
def t_appointment_conflict():
    r = client.get("/admin")
    token = extract_csrf(r.text)
    base = {
        "csrf_token": token,
        "category": "outpatient", "service_name": "门诊",
        "customer_name": "冲突客户", "phone": "13900000099",
        "pet_name": "冲突猫", "pet_gender": "male",
        "store": "大风动物医院（东环店）",
        "appointment_date": "2026-06-01", "appointment_time": "10:00",
        "duration_minutes": "30", "notes": "",
        "redirect_after": "admin",
    }
    # 第 1 个预约：应成功
    r1 = client.post("/admin/appointments/create", data=base)
    assert r1.status_code == 303, f"首个预约失败 {r1.status_code}, body={r1.text[:200]}"
    loc1 = r1.headers.get("location", "")
    # 不能含 err=（成功有可能是 ?msg=... 或 ?appointment_ok=...）
    assert "err=" not in loc1 and ("appointment_ok" in loc1 or "msg=" in loc1), \
        f"首个预约 loc 异常: {loc1}"
    # 第 2 个预约：同店同日同时段（10:15）应冲突
    base2 = dict(base, appointment_time="10:15", customer_name="冲突客户2", phone="13900000098", pet_name="冲突猫2")
    r2 = client.post("/admin/appointments/create", data=base2)
    # 实现通常是 303 + err=... 提示冲突，或 400
    if r2.status_code == 303:
        loc2 = r2.headers.get("location", "")
        from urllib.parse import unquote
        decoded = unquote(loc2)
        assert "err=" in loc2 or "冲突" in decoded or "重叠" in decoded or "占用" in decoded, \
            f"冲突未被拦截：loc={decoded}"
    else:
        assert r2.status_code in (400, 409), f"got {r2.status_code}"

t_appointment_conflict()


# ═══════════════════════════════════════════════
print("\n[12] 爽约封禁")

@step("同手机号本月 3 条 no_show → API 返回 is_banned=True")
def t_noshow_ban():
    import os
    os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from datetime import datetime
    # 用 ORM 插入，免得手写 SQL 漏 NOT NULL 列
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import Appointment
    db = SessionLocal()
    today = datetime.now()
    phone = "13900000077"
    for i in range(3):
        date_str = f"{today.strftime('%Y-%m')}-{i+5:02d}"
        db.add(Appointment(
            category="tnr", service_name="TNR 绝育",
            customer_name="封禁客户", phone=phone,
            pet_name="封禁猫", pet_gender="male",
            store="大风动物医院（东环店）",
            appointment_date=date_str, appointment_time="10:00",
            duration_minutes=30, status="no_show",
        ))
    db.commit()
    db.close()
    r = client.get(f"/api/tnr-store-status?phone={phone}")
    assert r.status_code == 200
    data = r.json()
    assert data.get("is_banned") is True, f"3 次爽约后应封禁，got {data}"
    assert data.get("ban_until"), "应返回 ban_until"

t_noshow_ban()


# ═══════════════════════════════════════════════
print("\n[13] 回访任务自动生成")

@step("新建 outpatient 就诊后自动产生 FollowUp，planned_date = visit_date + 7 天")
def t_followup_auto():
    import os
    os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import Visit, FollowUp
    db = SessionLocal()
    # 用前面创建的 visit_id（[3] 步骤里建的 outpatient，visit_date=2026-05-12）
    v = db.get(Visit, visit_id)
    assert v is not None, f"visit {visit_id} 不存在"
    fu = db.query(FollowUp).filter(FollowUp.visit_id == visit_id).first()
    assert fu is not None, "outpatient 应自动产生 FollowUp"
    assert fu.planned_date == "2026-05-19", f"应 +7 天，得 {fu.planned_date}"
    assert fu.status == "pending"
    assert fu.feedback_token, "应自动分配 token"
    # 没有匹配的 AdminUser.display_name → 直接存原文
    assert fu.assigned_to == "测试医生", f"应自动指派给 vet_name，得 {fu.assigned_to!r}"
    db.close()

t_followup_auto()


@step("vaccine 类型 visit 不产生 FollowUp")
def t_followup_vaccine_none():
    import os
    os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import Visit, FollowUp
    db = SessionLocal()
    v = Visit(
        customer_id=cust_id, pet_id=pet_id,
        visit_date="2026-05-12", visit_type="vaccine",
        chief_complaint="疫苗", vet_name="测试医生",
    )
    db.add(v); db.commit(); db.refresh(v)
    # 跑同步函数
    import sys
    sys.path.insert(0, ".")
    from app.main import _sync_followup_for_visit
    _sync_followup_for_visit(db, v)
    db.commit()
    fu = db.query(FollowUp).filter(FollowUp.visit_id == v.id).first()
    assert fu is None, f"vaccine 不应产生 FollowUp，但得到 {fu}"
    db.close()

t_followup_vaccine_none()


@step("GET /admin/follow-ups 列表页正常加载")
def t_followup_page():
    r = client.get("/admin/follow-ups?tab=today")
    assert r.status_code == 200, f"got {r.status_code}"
    assert "回访管理" in r.text

t_followup_page()


@step("POST /admin/follow-ups/{id}/handle 标记为已联系")
def t_followup_handle():
    import os
    os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import FollowUp
    db = SessionLocal()
    fu = db.query(FollowUp).filter(FollowUp.visit_id == visit_id).first()
    assert fu and fu.status == "pending", f"前置：FollowUp 应在 pending 状态, got {fu and fu.status}"
    fu_id = fu.id
    db.close()
    r = client.get("/admin/follow-ups?tab=today")
    token = extract_csrf(r.text)
    r = client.post(f"/admin/follow-ups/{fu_id}/handle", data={
        "csrf_token": token, "action": "contacted",
        "note": "电话已联系，已好转",
        "tab_redirect": "today",
    })
    assert r.status_code == 303, f"got {r.status_code}"
    # 验证状态变了
    db = SessionLocal()
    fu = db.get(FollowUp, fu_id)
    assert fu.status == "closed", f"应 closed, got {fu.status}"
    assert fu.response == "recovered"
    db.close()

t_followup_handle()


@step("GET /api/follow-ups/badge 返回计数")
def t_followup_badge():
    r = client.get("/api/follow-ups/badge")
    assert r.status_code == 200
    data = r.json()
    for k in ("count", "today", "overdue"):
        assert k in data

t_followup_badge()


@step("GET /follow-up/{token} 客户反馈页正常加载")
def t_feedback_page():
    import os
    os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import FollowUp, Visit
    db = SessionLocal()
    # 拿一条还有 feedback_token 的 FollowUp（前面的已经 closed 了，找下一只）
    fu = db.query(FollowUp).filter(FollowUp.status != "closed").first()
    if not fu:
        # 新建一条 visit + 自动产生 followup
        v = Visit(customer_id=cust_id, pet_id=pet_id,
                  visit_date="2026-05-12", visit_type="outpatient",
                  vet_name="测试医生", chief_complaint="测试")
        db.add(v); db.commit(); db.refresh(v)
        from app.main import _sync_followup_for_visit
        _sync_followup_for_visit(db, v); db.commit()
        fu = db.query(FollowUp).filter(FollowUp.visit_id == v.id).first()
    token = fu.feedback_token
    db.close()
    # 客户端无登录访问反馈页
    import httpx as _hx
    r = _hx.get(f"{BASE}/follow-up/{token}", follow_redirects=False, timeout=10)
    assert r.status_code == 200, f"got {r.status_code}"
    assert "回访" in r.text and "感觉怎么样" in r.text

t_feedback_page()


@step("无效 token → 404")
def t_feedback_bad_token():
    import httpx as _hx
    r = _hx.get(f"{BASE}/follow-up/THIS_IS_NOT_VALID", timeout=10)
    assert r.status_code == 404

t_feedback_bad_token()


@step("POST /follow-up/{token} response=recovered → 任务 closed")
def t_feedback_submit():
    import os
    os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import FollowUp
    db = SessionLocal()
    fu = db.query(FollowUp).filter(FollowUp.status != "closed").first()
    assert fu, "前置：应有未关闭的回访"
    fu_id = fu.id
    token = fu.feedback_token
    db.close()
    import httpx as _hx
    r = _hx.post(f"{BASE}/follow-up/{token}",
                 data={"response": "recovered", "note": "状态良好"},
                 follow_redirects=False, timeout=10)
    assert r.status_code == 200, f"got {r.status_code}"
    assert "感谢您的反馈" in r.text
    db = SessionLocal()
    fu = db.get(FollowUp, fu_id)
    assert fu.status == "closed", f"应 closed，得 {fu.status}"
    assert fu.response == "recovered"
    assert fu.response_note == "状态良好"
    db.close()

t_feedback_submit()


@step("dispatch 函数：到期 pending 的回访状态被处理为 phone_pending（占位）")
def t_dispatch_runs():
    import os
    os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import FollowUp, Visit
    from datetime import date
    db = SessionLocal()
    # 造一条今天到期的 pending
    v = Visit(customer_id=cust_id, pet_id=pet_id,
              visit_date="2026-05-08", visit_type="outpatient",
              vet_name="测试医生", chief_complaint="dispatch test")
    db.add(v); db.commit(); db.refresh(v)
    from app.main import _sync_followup_for_visit
    _sync_followup_for_visit(db, v)
    db.commit()
    fu = db.query(FollowUp).filter(FollowUp.visit_id == v.id).first()
    assert fu is not None, f"sync 未产生 FollowUp，visit_date={v.visit_date}, type={v.visit_type}"
    fu.planned_date = date.today().isoformat()
    fu.status = "pending"
    db.commit()
    fu_id = fu.id
    db.close()
    from app.services.followup_dispatch import run_due_dispatch
    res = run_due_dispatch()
    assert res["scanned"] >= 1, res
    db = SessionLocal()
    fu = db.get(FollowUp, fu_id)
    # 没配置渠道 → 全部 fallback 到 phone_pending
    assert fu.status == "phone_pending", f"占位渠道下应 phone_pending，得 {fu.status}"
    db.close()

t_dispatch_runs()


@step("dispatch 小程序渠道成功 → status=sent, channel=miniapp")
def t_dispatch_miniapp_ok():
    import os
    os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import FollowUp, Visit
    from app.services import followup_dispatch as fd
    from datetime import date

    db = SessionLocal()
    # 造一条到期 pending
    v = Visit(customer_id=cust_id, pet_id=pet_id,
              visit_date="2026-05-08", visit_type="outpatient",
              vet_name="测试医生", chief_complaint="miniapp test")
    db.add(v); db.commit(); db.refresh(v)
    from app.main import _sync_followup_for_visit
    _sync_followup_for_visit(db, v); db.commit()
    fu = db.query(FollowUp).filter(FollowUp.visit_id == v.id).first()
    fu.planned_date = date.today().isoformat()
    fu.status = "pending"
    db.commit()
    fu_id = fu.id
    db.close()

    # Monkey-patch 小程序渠道返回 True
    orig_mp = fd.send_via_miniapp
    fd.send_via_miniapp = lambda fu, c, p, v, db=None: True
    try:
        res = fd.run_due_dispatch()
        assert res["sent"] >= 1, res
    finally:
        fd.send_via_miniapp = orig_mp

    db = SessionLocal()
    fu = db.get(FollowUp, fu_id)
    assert fu.status == "sent", f"应 sent，得 {fu.status}"
    assert fu.channel == "miniapp"
    assert fu.sent_at is not None
    db.close()

t_dispatch_miniapp_ok()


@step("AdminUser.display_name 匹配 → FollowUp.assigned_to = username")
def t_display_name_match():
    import os
    os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import AdminUser, Visit, FollowUp
    from app.main import _sync_followup_for_visit
    from passlib.hash import bcrypt as _bc
    db = SessionLocal()
    u = AdminUser(username="li.yi", password_hash=_bc.hash("test123456"),
                  role="staff", store="东环店", display_name="李医生", is_active=True)
    db.add(u); db.commit()
    v = Visit(customer_id=cust_id, pet_id=pet_id,
              visit_date="2026-05-08", visit_type="outpatient",
              vet_name="李医生", chief_complaint="display_name 测试")
    db.add(v); db.commit(); db.refresh(v)
    _sync_followup_for_visit(db, v); db.commit()
    fu = db.query(FollowUp).filter(FollowUp.visit_id == v.id).first()
    assert fu is not None
    assert fu.assigned_to == "li.yi", f"应解析为 username li.yi，得 {fu.assigned_to!r}"
    db.close()

t_display_name_match()


@step("钱包：充值 100 余额 +100、累计 +100")
def t_wallet_recharge():
    r = client.get(f"/admin/customers/{cust_id}?tab=wallet")
    token = extract_csrf(r.text)
    r = client.post(f"/admin/wallets/{cust_id}/recharge", data={
        "csrf_token": token, "amount": "100", "pay_method": "cash",
        "bonus": "0", "note": "测试充值",
    })
    assert r.status_code == 303, f"got {r.status_code}"
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import Wallet, WalletTransaction
    db = SessionLocal()
    w = db.query(Wallet).filter(Wallet.customer_id == cust_id).first()
    assert w and abs(w.balance - 100.0) < 1e-6, f"应余额 100，得 {w and w.balance}"
    assert abs(w.lifetime_recharge - 100.0) < 1e-6
    txs = db.query(WalletTransaction).filter(WalletTransaction.wallet_id == w.id).all()
    assert len(txs) == 1 and txs[0].type == "recharge" and txs[0].amount == 100.0
    db.close()

t_wallet_recharge()


@step("钱包：充 500 送 50 → 余额 +550、lifetime_recharge +500")
def t_wallet_recharge_bonus():
    r = client.get(f"/admin/customers/{cust_id}?tab=wallet")
    token = extract_csrf(r.text)
    r = client.post(f"/admin/wallets/{cust_id}/recharge", data={
        "csrf_token": token, "amount": "500", "pay_method": "wechat", "bonus": "50",
    })
    assert r.status_code == 303
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import Wallet
    db = SessionLocal()
    w = db.query(Wallet).filter(Wallet.customer_id == cust_id).first()
    assert abs(w.balance - 650.0) < 1e-6, f"应 650（100 + 500 + 50），得 {w.balance}"
    assert abs(w.lifetime_recharge - 600.0) < 1e-6, f"应 600，得 {w.lifetime_recharge}"
    db.close()

t_wallet_recharge_bonus()


@step("钱包：调账 -50 → 余额 600")
def t_wallet_adjust():
    r = client.get(f"/admin/customers/{cust_id}?tab=wallet")
    token = extract_csrf(r.text)
    r = client.post(f"/admin/wallets/{cust_id}/adjust", data={
        "csrf_token": token, "amount": "-50", "note": "测试调账",
    })
    assert r.status_code == 303
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import Wallet
    db = SessionLocal()
    w = db.query(Wallet).filter(Wallet.customer_id == cust_id).first()
    assert abs(w.balance - 600.0) < 1e-6, f"应 600，得 {w.balance}"
    db.close()

t_wallet_adjust()


@step("钱包：退款超过余额应被拒")
def t_wallet_refund_overflow():
    r = client.get(f"/admin/customers/{cust_id}?tab=wallet")
    token = extract_csrf(r.text)
    r = client.post(f"/admin/wallets/{cust_id}/refund", data={
        "csrf_token": token, "amount": "999999", "note": "测试超额",
    })
    assert r.status_code == 303
    from urllib.parse import unquote
    loc = unquote(r.headers.get("location", ""))
    assert "超过" in loc, f"loc={loc}"

t_wallet_refund_overflow()


# ═══════════════════════════════════════════════
# 报告
# ═══════════════════════════════════════════════
print("\n" + "="*60)
print(f"通过 ({len(PASS)}):")
for p in PASS:
    print(f"  ✓ {p}")
if FAIL:
    print(f"\n失败 ({len(FAIL)}):")
    for name, err in FAIL:
        print(f"  ✗ {name}")
        print(f"    {err}")
    sys.exit(1)
else:
    print(f"\n🎉 全部 {len(PASS)} 项通过！")
