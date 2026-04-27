# 大风动物医院 管理系统 — 需求与路线图

> 最后审查更新：2026-04-27
> 技术栈：FastAPI + SQLAlchemy + SQLite + Jinja2 + 微信小程序
> 服务器：119.91.235.101 / /srv/tnr-app/releases/current
> GitHub：https://github.com/levanchic2-lang/dafopet-miniapp

---

## 诊疗工作流（核心业务逻辑）

```
客户到访
  ├── 买东西 ──────────────────────→ 【销售单】→ 收费
  └── 看诊
        └── 找到客户/宠物 → 新建就诊记录
              ├── 主诉（S）
              ├── 体格检查（O）
              ├── 【检查单】开化验/影像检查
              ├── 初步诊断（A）
              ├── 【处方单】开药（可多张）→ 库存出库
              ├── 【医嘱单】护理指引
              ├── 【销售单】保健品/耗材
              └── 【回访计划】
缴费 ← 汇总就诊+处方+销售 → 【收费单】
```

---

## 一、已完成功能（截至 2026-04-27 全面审查）

### 1.1 数据模型（28 张表）

| 模型 | 说明 | 状态 |
|------|------|------|
| applications | TNR 申请全流程（AI+人工审核/手术） | ✅ |
| appointments | 预约（TNR/门诊/手术/美容），三栏看板 | ✅ |
| media_files | 术前/术后照片与视频 | ✅ |
| customers | 客户档案 | ✅ |
| pets | 宠物档案 | ✅ |
| visits | 就诊病历（SOAP记录） | ✅ |
| prescriptions / prescription_items | 处方单 + 药品行 | ✅ |
| sales_orders / sales_order_items | 销售单 + 商品行 | ✅ |
| staff / contracts | 员工档案 + 合同 | ✅ |
| admin_users | 后台账号（角色权限） | ✅ |
| feedback | 用户反馈 | ✅ |
| notification_log / audit_log | 通知与审计日志 | ✅ |
| inventory_items | 库存品目（含管控/服务/低库存预警） | ✅ |
| inventory_transactions | 出入库流水 | ✅ |
| inventory_batches | 批次追踪（有效期/剩余量） | ✅ |
| stocktake_sessions / stocktake_items | 循环盘点（快照+提交） | ✅ |
| rabies_vaccine_records | 狂犬疫苗登记（双签名/证书号） | ✅ |
| adoption_pets | 待领养动物 | ✅ |

### 1.2 后台页面

| 路径 | 功能 | 状态 |
|------|------|------|
| `/admin` | TNR 审核看板 | ✅ |
| `/admin/appointments` | 预约管理（三栏看板） | ✅ |
| `/admin/customers` | 客户档案列表 + 详情 | ✅ |
| `/admin/visits` | 就诊病历列表 + SOAP表单 | ✅ |
| `/admin/prescriptions/create` | 新建处方单 | ✅ |
| `/admin/prescriptions/{id}` | 处方单详情/编辑 | ✅ |
| `/admin/sales-orders` | 销售单列表 | ✅ |
| `/admin/sales-orders/create` | 新建销售单 | ✅ |
| `/admin/sales-orders/{id}` | 销售单详情/收款 | ✅ |
| `/admin/inventory` | 库存品目列表（多筛选） | ✅ |
| `/admin/inventory/{id}` | 品目详情（批次/流水） | ✅ |
| `/admin/stocktake` | 循环盘点看板 | ✅ |
| `/admin/stocktake/{id}` | 盘点单（实盘/暂存/提交） | ✅ |
| `/admin/hr` | 人事管理（员工+账号） | ✅ |
| `/admin/staff/{id}` | 员工档案（含合同） | ✅ |
| `/admin/rabies` | 狂犬疫苗登记列表 | ✅ |
| `/admin/adoption` | 待领养动物管理 | ✅ |
| `/admin/feedback` | 用户反馈处理 | ✅ |

### 1.3 小程序页面

| 页面 | 功能 | 状态 |
|------|------|------|
| 首页 | 功能入口 | ✅ |
| 预约 | 新建/查看我的预约 | ✅ |
| TNR状态 | 申请状态追踪 | ✅ |
| 公布展示 | 已绝育猫展示 | ✅ |
| 待领养 | 领养动物列表/详情 | ✅ |
| 狂犬登记 | 表单提交+签名 | ✅ |
| 反馈 | 提交反馈 | ✅ |

---

## 二、存在缺陷（已有功能待完善）

### 2.1 处方单缺少列表页 ⚠️
- **现状**：处方单只能从就诊记录进入，没有独立的 `/admin/prescriptions` 全局列表
- **影响**：无法全局查找某药品的使用记录，无法统计处方量
- **修复**：新增处方单列表页（按日期/医生/状态筛选）

### 2.2 处方单未与库存出库联动 ⚠️
- **现状**：开处方单不会自动减少库存；库存出库只能手动操作
- **影响**：库存数据与实际发药脱节
- **修复**：处方单状态改为"已发药"时，自动对每个药品行创建 `out` 流水

### 2.3 销售单未与库存出库联动 ⚠️
- **现状**：销售单标记收款后不会减少库存
- **影响**：同上
- **修复**：销售单付款时，对商品类行项自动创建库存 `out` 流水

### 2.4 就诊记录缺少体征时序追踪 ℹ️
- **现状**：体格检查是自由文本，无法追踪体重/体温/心率趋势
- **建议**：Visit 表补 `weight_kg`、`temperature`、`heart_rate` 字段，客户详情页画折线图

### 2.5 收费流程不完整 ⚠️
- **现状**：销售单有付款状态，但处方费用没有对应的收费记录，无法汇总一次就诊的总费用
- **影响**：无法出具完整收费单，无法统计日收入

---

## 三、待建功能优先级表

| 优先级 | 模块 | 核心价值 | 预估工作量 |
|--------|------|----------|------------|
| ★★★★★ | 收费单（Phase 4） | 日常收款闭环，经营数据 | 中 |
| ★★★★★ | 处方→库存出库联动 | 库存数据准确 | 小 |
| ★★★★★ | 销售单→库存出库联动 | 库存数据准确 | 小 |
| ★★★★☆ | 处方单全局列表页 | 药品使用可查 | 小 |
| ★★★★☆ | 疫苗档案（Phase 6） | 高频客户需求，复诊率 | 中 |
| ★★★☆☆ | 检查单（Phase 5） | 医疗规范性 | 中 |
| ★★★☆☆ | 医嘱单（Phase 5） | 医疗规范性 | 小 |
| ★★★☆☆ | 数据报表（Phase 8） | 管理决策 | 大 |
| ★★☆☆☆ | 就诊体征时序追踪 | 慢性病管理 | 小 |
| ★★☆☆☆ | 手术记录（麻醉/术式） | 专科完整性 | 中 |
| ★★☆☆☆ | 小程序客户端扩展 | 客户粘性 | 大 |
| ★☆☆☆☆ | 健康证/疫苗证打印 | 合规需求 | 小 |

---

## 四、各模块详细需求

### Phase 4 — 收费单（最高优先级）

```
invoices（收费单）
  id, customer_id(FK), visit_id(FK, nullable), pet_id(FK)
  invoice_no          -- 单号（YYYYMMDD-序号）
  invoice_date
  subtotal            -- 小计
  discount_amount     -- 折扣
  total_amount        -- 实收
  payment_status: unpaid / partial / paid
  payment_method: cash / wechat / alipay / credit（挂账）
  paid_at, notes, created_by, created_at

invoice_items（收费明细）
  id, invoice_id(FK)
  ref_type: prescription / sales_order / service / manual
  ref_id              -- 关联单据ID（可为空，手动条目）
  description         -- 描述（冗余）
  quantity, unit_price, subtotal
```

**页面：**
- 就诊详情页：「生成收费单」按钮，汇总本次就诊的处方单+销售单
- `/admin/invoices` — 收费单列表（含未结清筛选）
- `/admin/invoices/{id}` — 收费单详情 + 打印版
- 支持手动添加项目（补全不在销售单/处方单里的收费）

---

### Phase 5 — 检查单 + 医嘱单

```
exam_orders（检查单）
  id, visit_id(FK), ordered_by, ordered_at
  exam_type: blood_routine/biochem/urinalysis/fecal/xray/ultrasound/ct/other
  result_text         -- 文字结果
  result_files        -- JSON 文件路径列表
  status: pending / completed
  notes

care_instructions（医嘱单）
  id, visit_id(FK), pet_id(FK)
  content TEXT        -- 自由文本（用药方法/护理要点/复诊时间）
  follow_up_date      -- 建议复诊日期
  created_by, created_at
```

**页面：**
- 就诊详情页嵌入（类似现有处方单/销售单区块）
- 检查单支持上传结果图片/文件

---

### Phase 6 — 疫苗档案（通用）

```
vaccinations（疫苗接种记录）
  id, pet_id(FK), customer_id(FK)
  vaccine_type: rabies / combo_core / combo_full / bordetella / other
  vaccine_name        -- 品牌/商品名
  batch_no            -- 批次号（关联 inventory_batches）
  dose_number         -- 第几针（1/2/3/加强）
  vaccinated_date
  next_due_date       -- 下次接种日期（自动计算或手填）
  vet_name, notes, created_by, created_at
```

**功能：**
- 宠物详情页：疫苗接种时间轴
- 全局列表：按「下次接种日期」筛选，即将到期的显示提醒
- 微信推送：提前 7 天/1 天推送宠物主人

---

### Phase 8 — 数据报表

**日/月统计看板：**
- 门诊量趋势（按就诊类型）
- 日收入 / 月收入（来源：收费单）
- TNR 手术完成率
- 库存消耗排名（TOP 20 药品/耗材）
- 新客户增长
- 预约转化率（预约 → 到院）
- 员工接诊量

---

### 补充：手术记录（专科完整性）

当前手术信息只记录在 TNR 申请中，门诊手术（绝育手术、肿瘤切除等）无专用记录。

```
surgery_records（手术记录）
  id, visit_id(FK), pet_id(FK), customer_id(FK)
  surgery_type        -- 手术类型
  anesthesia_drug     -- 麻醉药物
  anesthesia_dose     -- 麻醉剂量
  surgeon_name, assistant_name
  start_time, end_time, duration_minutes
  intraop_notes       -- 术中记录
  complications       -- 并发症
  outcome: uneventful / minor_complication / major_complication
  created_by, created_at
```

---

### 补充：小程序客户端扩展

| 功能 | 说明 | 优先级 |
|------|------|--------|
| 就诊记录查询 | 客户查看宠物历次就诊/处方记录 | ★★★ |
| 疫苗提醒 | 推送下次接种提醒 | ★★★ |
| 电子处方 | 查看/下载当次处方内容 | ★★ |
| 电子收费单 | 查看账单明细 | ★★ |
| 宠物健康档案 | 体重/疫苗/过敏史一览 | ★★ |
| 在线问诊/图文咨询 | 复诊随访 | ★ |

---

## 五、技术约定

- **数据库迁移**：所有 `ALTER TABLE` 用 `try/except` 幂等写法（见 `database.py`）
- **模板**：Jinja2，继承 `base.html`，使用现有 CSS 变量
- **CSRF**：所有 POST 表单必须带 `csrf_token`
- **权限**：admin 路由统一调用 `require_admin()`，superadmin 专属操作检查 `admin_role`
- **文件上传**：复用 `/srv/tnr-app/shared/uploads/` 目录
- **部署**：`git push origin main` → GitHub Actions → HTTP webhook → `git pull + systemctl restart`
- **库存联动**：出库时使用 `InventoryTransaction(tx_type="out", ref_type="prescription"|"sales_order", ref_id=单据ID)`
