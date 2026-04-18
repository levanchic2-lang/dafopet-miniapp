# 大风动物医院 TNR 系统 — 医疗管理系统需求文档

> 生成日期：2026-04-18（最后更新：2026-04-18）
> 当前技术栈：FastAPI + SQLAlchemy + SQLite + Jinja2 + 微信小程序
> 服务器：119.91.235.101 / /srv/tnr-app/releases/current
> GitHub：https://github.com/levanchic2-lang/dafopet-miniapp

---

## 诊疗工作流（核心业务逻辑）

```
客户到访
  ├── 买东西 ──────────────────────→ 【销售单】→ 缴费
  └── 看诊
        └── 找到宠物 → 新建就诊
              ├── 主诉（S）
              ├── 客观检查（O）
              ├── 【检查单】开化验/影像检查
              ├── 初步诊断（A）
              ├── 【处方单】开药（可多张）
              ├── 【医嘱单】护理指引
              ├── 【销售单】保健品/耗材
              └── 【回访计划】
缴费 ← 汇总就诊+处方+销售 → 【收费单】
```

**数据层级：**
- 客户档案 → 宠物档案 → 就诊记录（核心枢纽）
- 就诊记录 → 处方单、销售单、检查单、医嘱单
- 客户档案 → 收费记录（汇总所有消费）

---

## 一、已完成功能清单

### 数据模型
| 表名 | 说明 | 状态 |
|------|------|------|
| applications | TNR 申请全流程（AI审核/人工审核/手术） | ✅ |
| appointments | 预约（TNR/门诊/手术/美容），三栏看板 | ✅ |
| media_files | 术前/术后照片与视频 | ✅ |
| customers | 客户档案 | ✅ |
| pets | 宠物档案 | ✅ |
| visits | 就诊病历（SOAP记录） | ✅ |
| staff | 员工档案（含合同） | ✅ |
| admin_users | 后台账号（角色权限） | ✅ |
| feedback | 用户反馈 | ✅ |
| notification_log / audit_log | 通知与审计 | ✅ |

### 后台页面
- `/admin` — TNR 审核与手术登记
- `/admin/appointments` — 预约管理（三栏看板）
- `/admin/customers` — 客户档案列表
- `/admin/customers/{id}` — 客户详情（含宠物、就诊历史）
- `/admin/visits` — 就诊病历列表
- `/admin/visits/{id}` — 就诊记录（SOAP表单，左右布局）
- `/admin/staff` — 员工管理
- `/admin/users` — 账号管理
- `/admin/feedback` — 反馈管理
- `/admin/changelog` — 开发日志

---

## 二、Phase 2 ✅ — 就诊病历（SOAP记录）完成

**已实现：**
- visits 表：主诉/体格检查/诊断/处理方案/备注/医生/就诊类型
- 新建/编辑/删除就诊记录
- 客户搜索步骤（从就诊列表新建时）
- 客户详情页按宠物展示就诊历史
- 导航栏"就诊病历"入口

---

## 三、Phase 3 — 处方单 + 销售单（当前进行中）

### 3.1 处方单 (prescriptions / prescription_items)

**数据表：**
```
prescriptions
  id, visit_id(FK), customer_id(FK), pet_id(FK)
  prescribed_date, vet_name
  status: draft（草稿）/ issued（已开具）/ dispensed（已发药）
  notes
  created_by, created_at, updated_at

prescription_items
  id, prescription_id(FK)
  drug_name           -- 药品名称
  drug_type           -- oral/topical/injection/eye_drop/other
  dosage              -- 剂量（如 5mg）
  frequency           -- 频次（如 每日两次）
  duration_days       -- 疗程天数
  quantity            -- 总量
  instructions        -- 特殊说明
```

**页面：**
- 就诊详情页底部：处方单列表 + "开处方"按钮
- `/admin/prescriptions/create?visit_id=X` — 新建处方（动态药品行）
- `/admin/prescriptions/{id}` — 查看/编辑，支持打印

### 3.2 销售单 (sales_orders / sales_order_items)

**数据表：**
```
sales_orders
  id, customer_id(FK), visit_id(FK, nullable), pet_id(FK, nullable)
  order_date, status: pending / paid / cancelled
  total_amount, payment_method（现金/微信/支付宝/挂账）
  notes, created_by, created_at, updated_at

sales_order_items
  id, order_id(FK)
  item_name, item_type（product/service/medication/vaccine）
  unit_price, quantity, subtotal, notes
```

**页面：**
- 就诊详情页底部：销售单列表 + "开销售单"按钮
- 客户详情页：消费记录 Tab
- `/admin/sales-orders/create?customer_id=X&visit_id=Y` — 新建
- `/admin/sales-orders/{id}` — 查看/编辑/标记收款

---

## 四、Phase 4 — 收费单（汇总缴费）

汇总一次就诊产生的处方费用 + 销售单费用，生成一张收费单，记录支付方式和状态。

```
invoices
  id, customer_id(FK), visit_id(FK, nullable)
  invoice_date, total_amount
  payment_status: unpaid / paid / partial
  payment_method, notes, created_by, created_at

invoice_items
  id, invoice_id(FK)
  ref_type（prescription/sales_order/service）
  ref_id, description, amount
```

---

## 五、Phase 5 — 检查单 + 医嘱单

### 检查单 (exam_orders)
```
id, visit_id(FK), exam_type（血常规/生化/影像/粪检/尿检/其他）
ordered_by, ordered_at, result_text, result_files（JSON路径列表）
status: pending / completed
notes
```

### 医嘱单 (care_instructions)
```
id, visit_id(FK), pet_id(FK)
instruction_text   -- 自由文本或结构化
created_by, created_at
```

---

## 六、Phase 6 — 疫苗档案 + 到期提醒

```
vaccinations
  id, pet_id(FK), customer_id(FK)
  vaccine_name, vaccine_brand, batch_number, dose_number
  vaccinated_date, next_due_date
  vet_name, notes, created_by, created_at
```
- 每天定时任务提前7天/1天推微信消息

---

## 七、Phase 7 — 库存管理

```
inventory_items   -- 品目（药品/耗材/疫苗，含低库存预警）
inventory_transactions  -- 入库/出库/盘点变动记录
```

---

## 八、Phase 8 — 数据报表

- 月度手术量/门诊量趋势
- 收入按服务类型分布
- 新客户增长/疫苗接种统计
- 按门店对比

---

## 九、优先级总表

| 优先级 | Phase | 核心价值 | 状态 |
|--------|-------|----------|------|
| ★★★★★ | Phase 2 就诊病历 | 医院核心记录 | ✅ 完成 |
| ★★★★★ | Phase 3 处方单+销售单 | 完整诊疗闭环 | 🔨 进行中 |
| ★★★★☆ | Phase 4 收费单 | 经营数据可视化 | 待建 |
| ★★★★☆ | Phase 5 检查单+医嘱单 | 医疗规范性 | 待建 |
| ★★★☆☆ | Phase 6 疫苗档案 | 高频客户需求 | 待建 |
| ★★☆☆☆ | Phase 7 库存管理 | 运营效率 | 待建 |
| ★★☆☆☆ | Phase 8 报表看板 | 管理决策 | 待建 |

---

## 十、技术约定

- 数据库迁移：所有 ALTER TABLE 均用 `try/except` 幂等写法（见 database.py）
- 模板：Jinja2，继承 `base.html`，使用现有 CSS 变量
- 路由：FastAPI，admin 路由统一加 `check_admin()` 验证
- 文件上传：复用 `/srv/tnr-app/shared/uploads/` 目录
- 部署：`git push origin main` → 服务器 `git pull origin main && sudo systemctl restart tnr-app`
