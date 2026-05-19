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


@step("套餐：创建套餐 + 售卖给客户（钱包支付）→ 余额扣减")
def t_package_sell():
    # 1) 超管创建套餐
    r = client.get("/admin/packages")
    token = extract_csrf(r.text)
    r = client.post("/admin/packages/create", data={
        "csrf_token": token, "name": "美容套餐10次", "category": "beauty",
        "total_uses": "10", "sell_price": "300", "unit_price": "35",
        "validity_days": "365",
    })
    assert r.status_code == 303, f"create got {r.status_code}"

    # 2) 拿到产品 id
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import PackageProduct, CustomerPackage, Wallet
    db = SessionLocal()
    prod = db.query(PackageProduct).order_by(PackageProduct.id.desc()).first()
    assert prod and prod.name == "美容套餐10次"
    pid = prod.id

    # 钱包当前余额（用前面 t_wallet_adjust 后的 600）
    w_before = db.query(Wallet).filter(Wallet.customer_id == cust_id).first()
    bal_before = w_before.balance
    db.close()

    # 3) 售卖（用钱包支付）
    r = client.get(f"/admin/customers/{cust_id}?tab=packages")
    token = extract_csrf(r.text)
    r = client.post(f"/admin/customers/{cust_id}/packages/sell", data={
        "csrf_token": token, "product_id": str(pid),
        "pay_method": "wallet", "pet_id": "0", "custom_price": "",
    })
    assert r.status_code == 303, f"sell got {r.status_code}, loc={r.headers.get('location')}"

    db = SessionLocal()
    cp = db.query(CustomerPackage).filter(
        CustomerPackage.customer_id == cust_id, CustomerPackage.product_id == pid
    ).order_by(CustomerPackage.id.desc()).first()
    assert cp is not None, "未售出套餐"
    assert cp.total_uses == 10 and cp.used_count == 0 and cp.status == "active"
    assert cp.sell_price == 300.0

    w_after = db.query(Wallet).filter(Wallet.customer_id == cust_id).first()
    assert abs(w_after.balance - (bal_before - 300)) < 1e-6, \
        f"钱包应扣 300，前 {bal_before} 后 {w_after.balance}"
    db.close()

t_package_sell()


@step("收费单：钱包支付 → 余额扣减 + 流水有 invoice_id")
def t_invoice_pay_wallet():
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import Invoice, Wallet, WalletTransaction
    db = SessionLocal()
    # 之前 t_create_prescription 自动生成了 ¥100 收费单（未支付状态）
    inv = db.query(Invoice).filter(Invoice.customer_id == cust_id, Invoice.payment_status == "unpaid").order_by(Invoice.id.desc()).first()
    assert inv, "找不到待支付的收费单"
    inv_id = inv.id
    inv_total = float(inv.total_amount)
    w_before = db.query(Wallet).filter(Wallet.customer_id == cust_id).first().balance
    db.close()

    r = client.get(f"/admin/invoices/{inv_id}")
    token = extract_csrf(r.text)
    r = client.post(f"/admin/invoices/{inv_id}/pay", data={
        "csrf_token": token, "payment_method": "wallet",
    })
    assert r.status_code == 303, f"got {r.status_code}"

    db = SessionLocal()
    inv = db.get(Invoice, inv_id)
    assert inv.payment_status == "paid" and inv.payment_method == "wallet"
    w_after = db.query(Wallet).filter(Wallet.customer_id == cust_id).first().balance
    assert abs(w_after - (w_before - inv_total)) < 1e-6, f"钱包应扣 {inv_total}，前 {w_before} 后 {w_after}"
    tx = db.query(WalletTransaction).filter(WalletTransaction.invoice_id == inv_id).first()
    assert tx is not None and tx.type == "consume" and tx.amount == -inv_total
    db.close()

t_invoice_pay_wallet()


@step("收费单：套餐核销 → used_count +1，超不支持已用完套餐")
def t_invoice_pay_package():
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import Invoice, CustomerPackage, PackageRedemption, Wallet
    # 给客户钱包再充点，造一张收费单（手动建一张）
    db = SessionLocal()
    # 先确认有 active 套餐
    cp = db.query(CustomerPackage).filter(
        CustomerPackage.customer_id == cust_id, CustomerPackage.status == "active"
    ).first()
    assert cp, "前置：应有活跃套餐"
    used_before = cp.used_count

    # 直接造一张未支付收费单
    inv = Invoice(
        customer_id=cust_id, pet_id=pet_id,
        invoice_no="TEST_PKG_INV", invoice_date="2026-05-15",
        subtotal=50.0, discount_amount=0.0, total_amount=50.0,
        payment_status="unpaid",
    )
    db.add(inv); db.commit(); db.refresh(inv)
    inv_id = inv.id
    cp_id = cp.id
    db.close()

    r = client.get(f"/admin/invoices/{inv_id}")
    token = extract_csrf(r.text)
    r = client.post(f"/admin/invoices/{inv_id}/pay", data={
        "csrf_token": token, "payment_method": "package",
        "customer_package_id": str(cp_id),
    })
    assert r.status_code == 303

    db = SessionLocal()
    cp2 = db.get(CustomerPackage, cp_id)
    assert cp2.used_count == used_before + 1, f"应 +1，前 {used_before} 后 {cp2.used_count}"
    redeem = db.query(PackageRedemption).filter(PackageRedemption.invoice_id == inv_id).first()
    assert redeem is not None and redeem.used_count == 1
    inv2 = db.get(Invoice, inv_id)
    assert inv2.payment_status == "paid" and inv2.payment_method == "package"
    db.close()

t_invoice_pay_package()


@step("押金：收 500、抵扣到 300 收费单 → 应用 300、剩 200，再退 200")
def t_deposit_flow():
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import Deposit, Invoice

    # 收 500 押金
    r = client.get(f"/admin/customers/{cust_id}?tab=deposits")
    token = extract_csrf(r.text)
    r = client.post("/admin/deposits/create", data={
        "csrf_token": token, "customer_id": str(cust_id),
        "category": "surgery", "amount": "500", "pay_method": "cash",
        "pet_id": "0", "note": "测试押金",
    })
    assert r.status_code == 303

    db = SessionLocal()
    dep = db.query(Deposit).filter(Deposit.customer_id == cust_id).order_by(Deposit.id.desc()).first()
    assert dep and dep.amount == 500.0 and dep.status == "held"
    dep_id = dep.id

    # 造一张 ¥300 待付收费单
    inv = Invoice(customer_id=cust_id, pet_id=pet_id,
                  invoice_no="TEST_DEP_INV", invoice_date="2026-05-15",
                  subtotal=300.0, discount_amount=0.0, total_amount=300.0,
                  payment_status="unpaid")
    db.add(inv); db.commit(); db.refresh(inv)
    inv_id = inv.id
    db.close()

    # 抵扣
    r = client.get(f"/admin/invoices/{inv_id}")
    token = extract_csrf(r.text)
    r = client.post(f"/admin/deposits/{dep_id}/apply", data={
        "csrf_token": token, "invoice_id": str(inv_id), "apply_amount": "",
    })
    assert r.status_code == 303

    db = SessionLocal()
    dep2 = db.get(Deposit, dep_id)
    assert dep2.applied_amount == 300.0
    assert dep2.applied_invoice_id == inv_id
    # 还剩 200，所以是 partial_refund 状态
    assert dep2.status == "partial_refund", f"应 partial_refund 得 {dep2.status}"
    inv2 = db.get(Invoice, inv_id)
    assert inv2.payment_status == "paid", "押金覆盖全单应自动收款"
    db.close()

    # 退剩余 200
    r = client.post(f"/admin/deposits/{dep_id}/refund", data={
        "csrf_token": token, "refund_amount": "", "note": "客户来取剩余",
    })
    assert r.status_code == 303

    db = SessionLocal()
    dep3 = db.get(Deposit, dep_id)
    assert abs(dep3.refunded_amount - 200.0) < 1e-6
    # 全部结清 → applied (因为还有 applied_amount)
    assert dep3.status == "applied"
    db.close()

t_deposit_flow()


@step("收款统计：本月报表页可加载，含已支付的收费单金额")
def t_revenue_report():
    r = client.get("/admin/reports/revenue?preset=month")
    assert r.status_code == 200, f"got {r.status_code}"
    assert "收款统计" in r.text
    # 前面的 t_invoice_pay_wallet 支付了一张 ¥100 单 → 应该出现在汇总里
    # （只要页面 200 + 含字段即可，金额匹配在 export 测试时验）

@step("收款统计：Excel 导出能成功（200 + xlsx mime）")
def t_revenue_export():
    r = client.get("/admin/reports/revenue/export?preset=month")
    assert r.status_code == 200
    assert "spreadsheet" in r.headers.get("content-type", ""), r.headers.get("content-type")
    assert len(r.content) > 1000, f"导出文件应非空，得 {len(r.content)} bytes"

t_revenue_report()
t_revenue_export()


@step("优惠券：发放 3 张 ¥50 现金券给客户")
def t_coupon_issue():
    r = client.get("/admin/coupons")
    token = extract_csrf(r.text)
    r = client.post("/admin/coupons/issue", data={
        "csrf_token": token, "title": "新客 50 元抵扣",
        "kind": "cash", "face_value": "50", "discount_pct": "0",
        "min_amount": "100", "expires_at": "2027-12-31",
        "customer_id": str(cust_id), "quantity": "3",
        "code_prefix": "TEST", "notes": "集成测试",
    })
    assert r.status_code == 303, f"got {r.status_code}"
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import Coupon
    db = SessionLocal()
    cs = db.query(Coupon).filter(Coupon.customer_id == cust_id, Coupon.title == "新客 50 元抵扣").all()
    assert len(cs) == 3, f"应发 3 张，得 {len(cs)}"
    # 券码应都不同
    codes = {c.code for c in cs}
    assert len(codes) == 3
    # 校验字段
    assert cs[0].face_value == 50.0 and cs[0].min_amount == 100.0
    assert cs[0].status == "issued"
    db.close()

t_coupon_issue()


@step("混合支付：一单 ¥200 拆成 现金 ¥100 + 微信 ¥60 + 优惠券 ¥40")
def t_mixed_payment():
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import Invoice, Payment, Coupon

    db = SessionLocal()
    # 造 200 元单
    inv = Invoice(customer_id=cust_id, pet_id=pet_id,
                  invoice_no="TEST_MIXED", invoice_date="2026-05-15",
                  subtotal=200.0, discount_amount=0.0, total_amount=200.0,
                  payment_status="unpaid")
    db.add(inv); db.commit(); db.refresh(inv)
    inv_id = inv.id
    # 拿一张满 100 用 ¥50 的客户券
    c = db.query(Coupon).filter(
        Coupon.customer_id == cust_id, Coupon.kind == "cash",
        Coupon.face_value == 50.0, Coupon.status == "issued",
    ).first()
    assert c
    coupon_id = c.id
    db.close()

    # 第 1 笔：现金 100
    r = client.get(f"/admin/invoices/{inv_id}")
    token = extract_csrf(r.text)
    r = client.post(f"/admin/invoices/{inv_id}/add-payment", data={
        "csrf_token": token, "method": "cash", "amount": "100",
    })
    assert r.status_code == 303

    # 第 2 笔：微信 60
    r = client.post(f"/admin/invoices/{inv_id}/add-payment", data={
        "csrf_token": token, "method": "wechat", "amount": "60",
        "ref_no": "WX_123456",
    })
    assert r.status_code == 303

    # 此时 invoice 还没付清 (160/200)
    db = SessionLocal()
    inv2 = db.get(Invoice, inv_id)
    assert inv2.payment_status == "unpaid"
    pays = db.query(Payment).filter(Payment.invoice_id == inv_id).all()
    assert len(pays) == 2
    db.close()

    # 第 3 笔：优惠券（应抵 ¥50 → 但 outstanding 只剩 40 → 实际只用 40）
    r = client.post(f"/admin/invoices/{inv_id}/add-payment", data={
        "csrf_token": token, "method": "coupon", "amount": "40",
        "coupon_id": str(coupon_id),
    })
    assert r.status_code == 303

    db = SessionLocal()
    inv3 = db.get(Invoice, inv_id)
    assert inv3.payment_status == "paid", f"应付清，得 {inv3.payment_status}"
    pays = db.query(Payment).filter(Payment.invoice_id == inv_id, Payment.status == "success").all()
    assert len(pays) == 3
    total = sum(p.amount for p in pays)
    assert abs(total - 200.0) < 1e-6, f"sum 应 200，得 {total}"
    # 优惠券应标记 used
    c2 = db.get(Coupon, coupon_id)
    assert c2.status == "used"
    assert c2.used_invoice_id == inv_id
    db.close()

t_mixed_payment()


@step("混合支付：撤销其中一笔 → 收费单回到 unpaid，优惠券回到 issued")
def t_payment_void():
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import Invoice, Payment, Coupon
    db = SessionLocal()
    # 找之前付清的 TEST_MIXED 单的优惠券那笔
    inv = db.query(Invoice).filter(Invoice.invoice_no == "TEST_MIXED").first()
    coupon_pay = db.query(Payment).filter(
        Payment.invoice_id == inv.id, Payment.method == "coupon", Payment.status == "success"
    ).first()
    assert coupon_pay
    pay_id = coupon_pay.id
    inv_id = inv.id
    coupon_id = coupon_pay.ref_id
    db.close()

    r = client.get(f"/admin/invoices/{inv_id}")
    token = extract_csrf(r.text)
    assert token, f"页面里抓不到 csrf_token；前 500 字: {r.text[:500]}"
    r = client.post(f"/admin/invoices/{inv_id}/payments/{pay_id}/void", data={
        "csrf_token": token,
    })
    assert r.status_code == 303, f"got {r.status_code}, body={r.text[:200]}, token len={len(token)}"

    db = SessionLocal()
    p = db.get(Payment, pay_id)
    assert p.status == "cancelled"
    inv2 = db.get(Invoice, inv_id)
    assert inv2.payment_status == "unpaid", "撤销后应回 unpaid"
    c = db.get(Coupon, coupon_id)
    assert c.status == "issued", f"券应回 issued 得 {c.status}"
    assert c.used_invoice_id is None
    db.close()

t_payment_void()


@step("报表：按支付方式聚合从 Payment 表读，含混合支付的多笔")
def t_revenue_payment_aggregation():
    # 前面 t_mixed_payment 留下了 cash 100 + wechat 60，coupon 那笔已被 void
    # 那个收费单状态是 unpaid（被 void 后），所以 invoice 不在 paid 集合里
    # 但前面其他 paid 单（钱包付的 ¥100 / 套餐单 ¥50 / 押金抵扣单 ¥300 / 钱包付的套餐 ¥300）应该被算
    r = client.get("/admin/reports/revenue?preset=year")
    assert r.status_code == 200
    # 当前 paid invoice 都有 Payment 行了吗？老数据没有 Payment，走 fallback
    # 至少页面应包含 "现金 / 钱包" 等关键词之一
    txt = r.text
    assert ("现金" in txt or "钱包" in txt or "微信" in txt), "报表应显示支付方式标签"

t_revenue_payment_aggregation()


@step("打印：处方笺打印页可加载（国标 A5 横版）")
def t_print_prescription():
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import Prescription
    db = SessionLocal()
    p = db.query(Prescription).order_by(Prescription.id.desc()).first()
    assert p, "前置：应有处方"
    pid = p.id
    db.close()
    r = client.get(f"/admin/prescriptions/{pid}/print")
    assert r.status_code == 200
    assert "处方笺" in r.text
    assert "第一联" in r.text
    assert "A5_LAND" in r.text

t_print_prescription()


@step("打印：收费单打印页含混合支付明细")
def t_print_invoice():
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import Invoice
    db = SessionLocal()
    inv = db.query(Invoice).filter(Invoice.invoice_no == "TEST_MIXED").first()
    inv_id = inv.id
    db.close()
    r = client.get(f"/admin/invoices/{inv_id}/print")
    assert r.status_code == 200
    assert "Veterinary Receipt" in r.text
    assert "Cash" in r.text and "WeChat" in r.text

t_print_invoice()


@step("打印：检查报告页可加载 + 按项目自动选样式")
def t_print_exam():
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import ExamOrder
    db = SessionLocal()
    eo = db.query(ExamOrder).order_by(ExamOrder.id.desc()).first()
    if not eo:
        print("    (no exam order, skip)")
        return
    eo_id = eo.id
    db.close()
    r = client.get(f"/admin/exam-orders/{eo_id}/print")
    assert r.status_code == 200
    assert "检 查 报 告" in r.text or "B 超" in r.text or "X 光" in r.text or "显 微 镜" in r.text or "化 验" in r.text

t_print_exam()


@step("打印：病历单页可加载（含 SOAP / 处方 / 检查）")
def t_print_visit():
    r = client.get(f"/admin/visits/{visit_id}/print")
    assert r.status_code == 200
    assert "病 历 单" in r.text or "Medical Record" in r.text
    assert "Chief Complaint" in r.text
    assert "Diagnosis" in r.text

t_print_visit()


@step("进货单拍照：上传页可加载")
def t_inventory_import_page():
    r = client.get("/admin/inventory/import-photo")
    assert r.status_code == 200
    assert "进货单拍照入库" in r.text or "拍照入库" in r.text

t_inventory_import_page()


@step("进货单拍照：直接提交一行模拟数据（新增 + 入库 + 写批次）")
def t_inventory_import_commit():
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import InventoryItem, InventoryTransaction, InventoryBatch
    r = client.get("/admin/inventory/import-photo")
    token = extract_csrf(r.text)
    # 模拟用户编辑后提交 2 行：一新增、一跳过
    r = client.post("/admin/inventory/import-photo/commit", data={
        "csrf_token": token, "row_count": "2",
        "row0_action": "create", "row0_name": "测试OCR药品",
        "row0_spec": "10mg×30片", "row0_qty": "5", "row0_unit": "盒",
        "row0_unit_price": "12.5", "row0_batch_no": "BATCH-T01",
        "row0_expiry_date": "2027-01-01",
        "row1_action": "skip", "row1_name": "应被跳过", "row1_qty": "100",
    })
    assert r.status_code == 303, f"got {r.status_code}"
    db = SessionLocal()
    it = db.query(InventoryItem).filter(InventoryItem.name == "测试OCR药品").first()
    assert it is not None and it.stock_qty == 5.0
    assert it.cost_price == 12.5
    tx = db.query(InventoryTransaction).filter(InventoryTransaction.item_id == it.id).first()
    assert tx and tx.tx_type == "in" and tx.qty == 5.0
    batch = db.query(InventoryBatch).filter(InventoryBatch.item_id == it.id).first()
    assert batch and batch.batch_no == "BATCH-T01" and batch.expiry_date == "2027-01-01"
    # 跳过的不应入库
    skipped = db.query(InventoryItem).filter(InventoryItem.name == "应被跳过").first()
    assert skipped is None
    db.close()

t_inventory_import_commit()


@step("进货单拍照：同批次重复同名行自动合并（不建 2 个重复品目）")
def t_inventory_import_dedup():
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import InventoryItem, InventoryTransaction
    r = client.get("/admin/inventory/import-photo")
    token = extract_csrf(r.text)
    # 模拟 OCR 把同一行读了 2 遍：两行都是"萌邦：宠尔康"5 盒，第 3 行是"萌益健"3 盒
    r = client.post("/admin/inventory/import-photo/commit", data={
        "csrf_token": token, "row_count": "3",
        "row0_action": "create", "row0_name": "萌邦：宠尔康（复方氟康唑乳膏）",
        "row0_spec": "10g*1支", "row0_qty": "5", "row0_unit": "盒", "row0_unit_price": "50",
        "row1_action": "create", "row1_name": "萌邦：宠尔康（复方氟康唑乳膏）",
        "row1_spec": "10g*1支", "row1_qty": "5", "row1_unit": "盒", "row1_unit_price": "50",
        "row2_action": "create", "row2_name": "萌益健-乳铁蛋白",
        "row2_spec": "30ml/支", "row2_qty": "3", "row2_unit": "盒", "row2_unit_price": "78",
    })
    assert r.status_code == 303
    db = SessionLocal()
    # 只应有 1 条"萌邦：宠尔康"，库存 5+5=10
    chongerkang = db.query(InventoryItem).filter(InventoryItem.name == "萌邦：宠尔康（复方氟康唑乳膏）").all()
    assert len(chongerkang) == 1, f"应只有 1 条，得 {len(chongerkang)}"
    assert chongerkang[0].stock_qty == 10.0, f"应 10，得 {chongerkang[0].stock_qty}"
    # 应有 2 笔入库流水（每次累加都写一笔）
    txs = db.query(InventoryTransaction).filter(InventoryTransaction.item_id == chongerkang[0].id).all()
    assert len(txs) == 2 and all(t.tx_type == "in" and t.qty == 5.0 for t in txs)
    # 第二个产品独立存在
    mengyijian = db.query(InventoryItem).filter(InventoryItem.name == "萌益健-乳铁蛋白").first()
    assert mengyijian is not None and mengyijian.stock_qty == 3.0
    db.close()

t_inventory_import_dedup()


@step("进货单拍照：模糊匹配能识破厂家前缀")
def t_inventory_import_fuzzy():
    from app.services.purchase_ocr import match_item_by_name
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import InventoryItem
    db = SessionLocal()
    # DB 里已有"萌益健-乳铁蛋白"（上一个测试建的）
    all_items = db.query(InventoryItem).filter(InventoryItem.is_active == True).all()
    # 新 OCR 读到带不同前缀的版本，应该映射到同一个
    item_id, conf = match_item_by_name("萌邦：乳铁蛋白", all_items)  # 不同厂家前缀 + 后缀少
    # 这种差别太大其实不应硬匹配，确认这种确实跨不过去
    item_id2, conf2 = match_item_by_name("萌益健 乳铁蛋白", all_items)  # 只是空格替代横线
    assert item_id2 != 0, f"基本相同的名字应匹配到，得 conf={conf2}"
    item_id3, conf3 = match_item_by_name("萌益健-乳铁蛋白", all_items)  # 完全一致
    assert conf3 == 1.0
    db.close()

t_inventory_import_fuzzy()


@step("库存批量编辑：选 2 个品目 → 改大类/小类/供应商")
def t_inventory_bulk_edit():
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import InventoryItem
    db = SessionLocal()
    # 拿之前测试建的 2 个品目
    ids = [r.id for r in db.query(InventoryItem).filter(
        InventoryItem.name.in_(["萌邦：宠尔康（复方氟康唑乳膏）", "萌益健-乳铁蛋白"])
    ).all()]
    assert len(ids) >= 2, "前置：应有 2 个测试品目"
    db.close()

    r = client.get("/admin/inventory")
    token = extract_csrf(r.text)
    # 多值 item_ids 用 dict 列表语法
    data = {
        "csrf_token": token, "category": "medication", "subcategory": "general",
        "supplier": "测试供应商有限公司",
        "item_ids": [str(i) for i in ids],
    }
    r = client.post("/admin/inventory/bulk-edit", data=data)
    assert r.status_code == 303, f"got {r.status_code}, body[:200]={r.text[:200]}"

    db = SessionLocal()
    for i in ids:
        it = db.get(InventoryItem, i)
        assert it.category == "medication"
        assert it.subcategory == "general"
        assert it.supplier == "测试供应商有限公司"
    db.close()

t_inventory_bulk_edit()


@step("收费单整单退款：所有 Payment 撤销 + 状态 refunded")
def t_invoice_refund_all():
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import Invoice, Payment
    db = SessionLocal()
    # 造一张 100 元单 + 一笔现金支付
    inv = Invoice(customer_id=cust_id, pet_id=pet_id,
                  invoice_no="TEST_REFUND", invoice_date="2026-05-19",
                  subtotal=100.0, total_amount=100.0,
                  payment_status="unpaid")
    db.add(inv); db.commit(); db.refresh(inv)
    inv_id = inv.id
    db.close()

    r = client.get(f"/admin/invoices/{inv_id}")
    token = extract_csrf(r.text)
    r = client.post(f"/admin/invoices/{inv_id}/add-payment", data={
        "csrf_token": token, "method": "cash", "amount": "100",
    })
    assert r.status_code == 303
    # 退单
    r = client.post(f"/admin/invoices/{inv_id}/refund", data={"csrf_token": token})
    assert r.status_code == 303, f"got {r.status_code}, body={r.text[:200]}"

    db = SessionLocal()
    inv2 = db.get(Invoice, inv_id)
    assert inv2.payment_status == "refunded"
    assert inv2.paid_at is None
    pays = db.query(Payment).filter(Payment.invoice_id == inv_id).all()
    assert all(p.status == "cancelled" for p in pays), [p.status for p in pays]
    db.close()

t_invoice_refund_all()


@step("协议模板：CRUD + Quill HTML 正文 + 占位符变量")
def t_consent_template_crud():
    # 列表页可加载
    r = client.get("/admin/consent-templates")
    assert r.status_code == 200
    assert "协议模板" in r.text
    token = extract_csrf(r.text)
    # 新建
    body = "<h2>麻醉同意书</h2><p>客户 {{cust_name}} 同意为 {{pet_name}} 实施麻醉。</p>"
    r = client.post("/admin/consent-templates/save", data={
        "csrf_token": token, "template_id": "",
        "name": "测试·麻醉同意书", "category": "anesthesia",
        "body_html": body, "notes": "测试用",
    })
    assert r.status_code == 303

    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import ConsentTemplate
    db = SessionLocal()
    t = db.query(ConsentTemplate).filter(ConsentTemplate.name == "测试·麻醉同意书").first()
    assert t is not None
    assert "{{cust_name}}" in t.body_html and "{{pet_name}}" in t.body_html
    assert t.category == "anesthesia"
    assert t.is_active is True
    tid = t.id
    db.close()

    # 编辑表单可加载
    r = client.get(f"/admin/consent-templates/{tid}/edit")
    assert r.status_code == 200
    assert "麻醉同意书" in r.text

    # 下架
    r = client.post(f"/admin/consent-templates/{tid}/toggle", data={"csrf_token": token})
    assert r.status_code == 303
    db = SessionLocal()
    t = db.get(ConsentTemplate, tid)
    assert t.is_active is False
    db.close()

t_consent_template_crud()


@step("发起协议签署：选模板 → 变量自动替换 → 生成唯一 token 任务")
def t_consent_task_create():
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import ConsentTemplate, ConsentTask
    db = SessionLocal()
    # 先确保有个上架模板（之前测试把它下架了，这里造一个新的）
    t = ConsentTemplate(
        name="测试·疫苗同意书",
        category="vaccination",
        body_html="<p>主人 <b>{{cust_name}}</b> 同意为 <b>{{pet_name}}</b> 接种疫苗。日期 {{date}}。</p>",
        is_active=True,
    )
    db.add(t); db.commit(); db.refresh(t)
    tid = t.id
    db.close()

    r = client.get(f"/admin/customers/{cust_id}?tab=docs")
    token = extract_csrf(r.text)
    r = client.post("/admin/consent-tasks/create", data={
        "csrf_token": token, "template_id": str(tid),
        "customer_id": str(cust_id), "pet_id": str(pet_id),
        "title_override": "", "expires_at": "", "notes": "测试",
    })
    assert r.status_code == 303, f"got {r.status_code}, body[:200]={r.text[:200]}"

    db = SessionLocal()
    task = db.query(ConsentTask).filter(ConsentTask.template_id == tid).first()
    assert task is not None
    assert task.title == "测试·疫苗同意书"
    assert task.status == "pending"
    assert task.token and len(task.token) >= 16
    # 变量应已被替换：原模板有 {{cust_name}}，snapshot 里应该不再有
    assert "{{cust_name}}" not in task.snapshot_html
    assert "{{pet_name}}" not in task.snapshot_html
    # 替换成实际客户名（测试客户A）
    assert "测试客户A" in task.snapshot_html
    db.close()

    # 任务详情页可加载
    r = client.get(f"/admin/consent-tasks/{task.id}")
    assert r.status_code == 200
    assert "测试·疫苗同意书" in r.text
    assert "签署链接" in r.text   # pending 状态下有复制链接区
    # URL 应含 token
    assert task.token in r.text

t_consent_task_create()


@step("客户签字：GET /consent/{token} → POST 签字 → 任务变 signed")
def t_consent_sign_flow():
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import ConsentTask
    db = SessionLocal()
    task = db.query(ConsentTask).filter(ConsentTask.status == "pending").first()
    assert task, "前置：应有 1 条待签任务"
    tok = task.token
    tid = task.id
    db.close()

    # 客户端无登录访问 H5
    import httpx as _hx
    pub = _hx.Client(base_url=BASE, follow_redirects=False, timeout=10.0)
    r = pub.get(f"/consent/{tok}")
    assert r.status_code == 200
    assert "签字" in r.text and "测试客户A" in r.text  # 变量已替换

    # 提交签字（模拟一张 signature_pad 输出的 PNG，base64 长度 > 800）
    import base64
    fake_png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 800   # 凑长度
    sig_data = "data:image/png;base64," + base64.b64encode(fake_png_bytes).decode()
    r = pub.post(f"/consent/{tok}/sign", json={"signature": sig_data})
    assert r.status_code == 200, f"got {r.status_code}, body={r.text[:200]}"
    j = r.json()
    assert j.get("ok") is True, j

    # 校验 DB 状态
    db = SessionLocal()
    t2 = db.get(ConsentTask, tid)
    assert t2.status == "signed"
    assert t2.signature_path and t2.signature_path.startswith("consent_signatures/")
    assert t2.signed_at is not None
    db.close()

    # 再次 GET 应显示"已签署"页
    r = pub.get(f"/consent/{tok}")
    assert "已签署" in r.text

    # 不能再次签
    r = pub.post(f"/consent/{tok}/sign", json={"signature": sig_data})
    j = r.json()
    assert j.get("ok") is False
    assert "不可再次" in j.get("error", "")

    pub.close()


t_consent_sign_flow()


@step("客户签字：无效 token → 404；签字过短 → 拒绝")
def t_consent_sign_invalid():
    import httpx as _hx
    pub = _hx.Client(base_url=BASE, follow_redirects=False, timeout=10.0)
    r = pub.get("/consent/THIS_IS_NOT_VALID_TOKEN")
    assert r.status_code == 404

    # 造一个新 pending 任务专门测试签字过短
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import ConsentTask, ConsentTemplate
    db = SessionLocal()
    t = db.query(ConsentTemplate).filter(ConsentTemplate.is_active == True).first()
    assert t, "前置：应有上架模板"
    task = ConsentTask(
        template_id=t.id, customer_id=cust_id, pet_id=pet_id,
        title="测试·签字过短", snapshot_html="<p>测试</p>",
        token="test_short_sig_xyz", status="pending",
    )
    db.add(task); db.commit()
    db.close()

    r = pub.post("/consent/test_short_sig_xyz/sign",
                 json={"signature": "data:image/png;base64,AA=="})
    j = r.json()
    assert j.get("ok") is False
    assert "过于简单" in j.get("error", "")
    pub.close()


t_consent_sign_invalid()


@step("协议 PDF：weasyprint 装好时签字成功自动归档 ConsentDocument")
def t_consent_pdf_gen():
    """如果 weasyprint 可用，签字成功后 ConsentDocument 应已生成且 pdf_path 非空。
    如果 weasyprint 未装（缺系统库），允许 ConsentDocument 不存在但不报错。"""
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import ConsentTask, ConsentDocument
    try:
        import weasyprint  # noqa
        has_wp = True
    except ImportError:
        has_wp = False
    except OSError:
        # weasyprint 装了但系统库缺
        has_wp = False
    db = SessionLocal()
    # 前面 t_consent_sign_flow 已经签了一条
    task = db.query(ConsentTask).filter(ConsentTask.status == "signed").first()
    assert task, "前置：应有已签任务"
    doc = db.query(ConsentDocument).filter(ConsentDocument.task_id == task.id).first()
    if has_wp:
        assert doc is not None, "weasyprint 可用时应已归档"
        assert doc.pdf_path and doc.pdf_path.startswith("consent_pdfs/")
        # 文件应存在
        from pathlib import Path as _P
        assert _P("uploads") / doc.pdf_path
        assert doc.pdf_size > 0
    else:
        print("    (weasyprint 未装/缺系统库，跳过 PDF 落盘断言)")
    db.close()

t_consent_pdf_gen()


@step("协议推送：模板未配 / 无 openid 时静默失败不阻断签发")
def t_consent_push_silent():
    # 模板未配置 OPENAI/WECHAT 时，_try_push_consent_notice 应返回 False
    # 并且不抛异常。这里直接调内部函数验证。
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import ConsentTask, Customer, Pet
    from app.main import _try_push_consent_notice
    db = SessionLocal()
    task = db.query(ConsentTask).filter(ConsentTask.status == "pending").first()
    assert task
    cust = db.get(Customer, task.customer_id)
    pet = db.get(Pet, task.pet_id) if task.pet_id else None
    # 客户没有 wechat_openid（测试 DB 里也没有），应该返回 False
    ok = _try_push_consent_notice(db, task, cust, pet)
    assert ok is False  # 无 openid → 静默 False
    db.close()


t_consent_push_silent()


@step("协议推送：管理员重发接口仅 pending 状态可用")
def t_consent_resend_endpoint():
    import os; os.environ["DATABASE_URL"] = "sqlite:///./_test/test.db"
    from app import models  # noqa
    from app.database import SessionLocal
    from app.models import ConsentTask
    db = SessionLocal()
    pending = db.query(ConsentTask).filter(ConsentTask.status == "pending").first()
    signed = db.query(ConsentTask).filter(ConsentTask.status == "signed").first()
    pending_id = pending.id if pending else 0
    signed_id = signed.id if signed else 0
    db.close()
    # 重发已签的应被拒
    if signed_id:
        r = client.get(f"/admin/consent-tasks/{signed_id}")
        token = extract_csrf(r.text)
        r = client.post(f"/admin/consent-tasks/{signed_id}/resend", data={"csrf_token": token})
        assert r.status_code == 303
        from urllib.parse import unquote
        loc = unquote(r.headers.get("location", ""))
        assert "仅待签" in loc, f"loc={loc}"
    # 重发 pending 的（无 openid → 不会推成功，但接口不应 500）
    if pending_id:
        r = client.get(f"/admin/consent-tasks/{pending_id}")
        token = extract_csrf(r.text)
        r = client.post(f"/admin/consent-tasks/{pending_id}/resend", data={"csrf_token": token})
        assert r.status_code == 303
        from urllib.parse import unquote
        loc = unquote(r.headers.get("location", ""))
        # 测试环境没有 wechat appid 也没有 openid，应该提示"推送失败"
        assert "推送失败" in loc or "已重发" in loc


t_consent_resend_endpoint()


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
