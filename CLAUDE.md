# 大风动物医院 — TNR 申请系统

## 项目概述
流浪猫绝育（TNR）申请 + 预约管理系统。
- **后端**：FastAPI + SQLAlchemy 2.0 + SQLite，入口 `app/main.py`
- **模板**：Jinja2，位于 `templates/`
- **小程序**：微信小程序，位于 `miniapp/`
- **部署**：git push main → webhook 自动部署服务器；webhook 失效时用本机 SSH 一键脚本
  - 服务器：`ubuntu@119.91.235.101`（腾讯轻量云，hostname `VM-0-10-ubuntu`）
  - 本机已配 `~/.ssh/config` alias `tnr-server` + 部署 key `~/.ssh/tnr_deploy`
  - 一键命令：`ssh tnr-server "cd /srv/tnr-app/releases/current && sudo git fetch origin main && sudo git reset --hard origin/main && sudo systemctl restart tnr-app"`
  - sudo 时会弹腾讯云微信扫码 banner，正常 sudo 自动通过不需扫

## 门店
- 大风动物医院（东环店）— 短名 `东环店`
- 大风动物医院（横岗店）— 短名 `横岗店`
- `_STORE_SHORT_TO_FULL` / `_STORE_FULL_TO_SHORT` 做互转
- `Appointment.store` 存全名；`Staff.store` / `AdminUser.store` 存短名

## 申请状态流
```
draft → pending_ai → pending_manual → pre_approved → approved → scheduled → arrived_verified → surgery_completed
                                                              ↘ cancelled / rejected / no_show
```
- `draft`：客户提交表单后、媒体上传/AI 审核完成前的中间态，**不算「审核中」**
- 重复检测只拦截 `pending_ai` 及之后的状态

## 权限模型
- `superadmin`：全数据权限，门店筛选器可用
- `staff`：登录后只看到 `AdminUser.store` 对应门店的数据（TNR 申请、预约、员工）
- `_get_admin_store(request)` 返回当前用户的门店短名，superadmin 返回空字符串

## 关键模型（app/models.py）
- `Application`：TNR 申请，`clinic_store` 存全名
- `Appointment`：预约，`store` 存全名，`category` 区分 tnr/outpatient/surgery/beauty 等
- `Staff`：员工档案，`store` 存短名
- `AdminUser`：后台账号，`store` 存短名，`role` = superadmin/staff，`wecom_userid` 绑定企业微信免密登录
- `TnrStoreConfig`：每家门店的 TNR 月度配额（`tnr_monthly_quota` 默认 30）和开关（`tnr_accepting`）
- `MediaFile`：申请的照片/视频，`kind` = application_image/application_video/surgery_before_image 等
- `FollowUp`：诊后回访任务，按 `Visit.visit_type` 自动衍生
  - 规则：surgery+3天 / postop+2天 / outpatient+7天 / beauty+14天 / vaccine 等不出
  - status：pending → due → sent → responded/closed/phone_pending
  - 调度：`app/services/followup_dispatch.py` 每小时通过 APScheduler 跑
  - 渠道：先小程序订阅消息（`wechat_tmpl_followup`）→ 短信网关 → 电话兜底
  - 客户反馈短链：`/follow-up/{token}`（无登录，token 即凭证）
- 协议签署系统（commit 1-7 of 协议系列）：
  - 3 张表：`ConsentTemplate`（模板，Quill HTML + 占位符）/ `ConsentTask`（签署任务，含 token + snapshot_html）/ `ConsentDocument`（归档 PDF）
  - 后台 `/admin/consent-templates` 维护模板，富文本编辑器（Quill 2.0 CDN）+ 12 个变量占位符（`{{pet_name}}` 等）
  - 病例 / 客户档案"医疗文书"tab 点 "📝 发起协议签署" → 选模板 + 关联宠物/就诊 → 自动渲染变量 → 生成唯一 token
  - 客户无登录 H5 `/consent/{token}` → 看协议 + signature_pad canvas 手写 → 提交
  - 签字成功自动调 `app/services/consent_pdf.py` 用 weasyprint 渲染 PDF 归档到 ConsentDocument
  - 小程序订阅消息推送：`wechat_tmpl_consent` + push_consent_signature()，5 字段映射（thing5/6/1/time12/4）
  - 后台任务详情页：复制链接、重发通知、重新生成 PDF
  - 部署提醒：服务器需 `apt install libpango-1.0-0 libpangoft2-1.0-0 libcairo2`；env 配 WECHAT_TMPL_CONSENT + PUBLIC_BASE_URL
- 进货单照片识别入库：
  - `app/services/purchase_ocr.py` 调多模态视觉大模型（复用 `settings.openai_*`）
  - 入口：`/admin/inventory/import-photo`（库存页右上角"📸 拍照入库"）
  - 流程：上传 → JS 异步调 `/recognize` → 模型返回 JSON → 表格可编辑 → 提交 `/commit` 批量入库
  - 每行自动匹配已有品目（SequenceMatcher fuzzy >= 0.7）→ "累加 / 新增 / 跳过" 三选一
  - 写 `InventoryItem` + `InventoryTransaction` (type=in)，有批号/有效期时再写 `InventoryBatch`
- 打印系统：
  - `templates/_print_base.html` 通用基础模板，4 种纸张：A4 / A5 纵 / A5 横 / 80mm 热敏
  - `@page` CSS + `body[data-size]` 屏幕预览同步切换，工具条 localStorage 记住偏好
  - 处方笺 `admin_prescription_print.html`：按国标格式（左诊断 / 中 Rp 表 / 右竖排"第一联"/ 底部医师栏），默认 A5 横
  - 收费单 `admin_invoice_print.html`：极简英伦风（Georgia serif + 双横线），10 种支付方式中英对照
  - 检查报告 `admin_exam_print.html`：按项目名自动选样式（B超/X光/显微镜/化验/通用），不同颜色 + 不同影像网格
  - 入口：处方/收费单/检查单详情页右上角 🖨 按钮，新标签打开
- **住院管理系统**（D1-D8 完整闭环）：
  - 入口：`/admin/inpatient`（医疗 ribbon）/ 病历详情页头部蓝色「🛏 转住院」按钮
  - 8 张表：
    - `Cage`：笼位（store/code/kind=general/iso/icu + daily_rate + sort_order，软删除，同店内 code 唯一）
    - `Hospitalization`：住院档案（pet/customer/visit/cage 关联 + reason + admitted_at/discharged_at + daily_rate_override + 双 token：staff_token + owner_token）
    - `MedicationAdminLog`：发药打勾日志（按 PrescriptionItem × scheduled_at 展开；status=pending/done/skipped/refused；reminder_sent_at 用于漏药推送去重）
    - `VitalSignsLog`：T/HR/RR/MM/CRT/Weight + notes（按 species 阈值判异常红标）
    - `IOLog`：direction=in/out + category（iv_fluid/oral/injection / urine/stool/vomit/drainage）+ amount_ml
    - `FeedingLog`：food_type + offered_g/eaten_g + appetite_score(0-4)
    - `HandoverNote`：shift=morning/afternoon/night + content（早 7-15 / 中 15-22 / 夜 22-7 自动识别）
  - `PrescriptionItem.schedule_times`：CSV 24h 时刻表（如 "8,14,20"），仅住院动物需填；处方 save 后自动生成发药任务（`_generate_med_logs_for_prescription`）
  - 天数算法 `_calc_hosp_days`：过夜算 1 天（discharge_date - admit_date），当天进当天出 = 0 天笼费
  - 出院自动结账：`_sync_visit_invoice` 加第 4 类「住院笼费」明细 + 绑 invoice_id
  - 看板视图（`/admin/inpatient`）：卡片 / 笼位图双视图切换（默认卡片）
    - 卡片：宠物 emoji 头像 + 笼号 pill + 住院天数 + 处方数 + 入院原因
    - 笼位图：所有笼位平铺，占用橙色 + 宠物名 + 已住天数，空位虚线 + 日费率
  - 详情页（`/admin/inpatient/{id}`）：
    - 顶部状态横幅 + 最新交班一句话黄色提醒
    - 用药任务面板（今日列表，按时间排序，巨大「✓ 打勾」绿色按钮，漏药红色警示）
    - 体征 / I/O（24h 净平衡）/ 进食（吃/提供 % + 食欲 0-4 着色）
    - 交班记录（早班蓝 / 中班橙 / 夜班紫 左边框）
    - 右侧栏：换笼、出院结账、误开取消、关联账单、打印笼牌
  - 笼牌打印 `/admin/inpatient/{id}/cage-tag`：A5 portrait 含员工 + 业主双二维码，PNG 由 `qrcode` 库生成
  - 扫码入口：
    - `/inpatient/staff/{token}` 员工 → 已登录直跳 admin 详情，未登录 ?next=
    - `/inpatient/owner/{token}` 业主 → 无登录只读 H5 时间轴（💊用药/🍽喂食/🌡体温 emoji 友好渲染，剂量/HR/RR/SOAP/费用一律不展示）
  - 漏药 + 接班推送：`app/services/inpatient_dispatch.py` 通过 APScheduler 跑
    - `scan_overdue_medications` 每 5 分钟扫，scheduled_at + 30 分钟仍 pending → 推该店所有 wecom_userid 已绑 admin（聚合按宠物）
    - `send_shift_handover_reminder` 6:50 / 14:50 / 21:50 北京时间分别推早/中/夜班接下来 9h 任务清单
    - `medication-log/{id}/uncheck` 时清 reminder_sent_at，允许重新提醒
- 财务模块（commit 1-12 of 财务系列）：
  - `Wallet` + `WalletTransaction`：客户钱包 = 充值卡；4 种流水（recharge/consume/refund/adjust），balance_after 快照
  - `PackageProduct` + `CustomerPackage` + `PackageRedemption`：套餐目录（洗澡卡/造型卡等）+ 已购实例（含售卖时快照防价格漂移）+ 核销流水
  - `Deposit`：业务押金（手术/寄养/美容），关联 appointment/visit，status held → applied/partial_refund/refunded/cancelled
  - `Coupon`：自家发放/自家核销的优惠券，3 类（cash/discount/free_item），customer_id 空 = 通用券
  - `Payment`：收费单收款明细，**支持一单多笔混合支付**；method ∈ cash/wechat/alipay/shouqianba/meituan/third_party/wallet/package/deposit/coupon
  - 收费单 `/admin/invoices/{id}/add-payment` 增加一笔；`payment_status` 在 sum(Payments) >= total 时自动转 paid
  - 撤销 `/admin/invoices/{id}/payments/{pid}/void` 会回滚副作用（钱包返钱/套餐次数恢复/押金抵扣还原/券回 issued）
  - 收款统计：`/admin/reports/revenue` 用 Chart.js 出日趋势/支付方式/门店分布，按 Payment 表聚合，可导出 Excel
  - 启动迁移：`app/database.py` 用 `CREATE TABLE IF NOT EXISTS` 风格幂等建表 + 索引

## TNR 业务规则
- 每店每月最多 30 个已确认 TNR 预约（`TnrStoreConfig.tnr_monthly_quota`）
- 爽约（no_show）≥3 次/月 → 封禁 90 天
- 手术完成必须先上传术前 + 术后各至少 1 个文件

## 病历合规规则（病历结束系统）
- `Visit.status`：open / closed；closed 后病历及关联**处方/检查单**不可改、不可重开（行业惯例）
- 「✓ 结束病历」按钮在病历详情页头部，二次确认走 `/admin/visits/{id}/close`
- 关联模块锁定细节：
  - 处方 create/edit/delete → 视 visit closed 拒绝
  - 检查单 create/delete → 拒绝；report upload 仍允许（附证据非改单）
  - 处方/检查 void 仍允许（合规挽救动作）
  - **疫苗 / 驱虫 / 销售 / 美容**独立单据，不受 visit closed 影响
- agent 的 `_resolve_or_list_visit` 仅查 open 病历，已结束病历带 🔒 标记

## 住院业务规则
- 笼位自由增删改，同店内 code 唯一；占用中不允许删
- 住院天数：过夜算 1 天（按日期差，不按小时数），当天进当天出 = 0 天笼费
- 日费率优先 `Hospitalization.daily_rate_override` > `Cage.daily_rate`
- 出院 → 自动笼费写入 visit 收费单 + 绑 `invoice_id`
- 业主只读 H5 不展示：剂量 / SOAP / 诊断 / 收费 / I/O 输液量 / HR/RR/MM/CRT / 交班记录

## 手机 PWA（M0-M6 完整重构）
桌面后台是医生/前台用的"完整工作台"；手机端是**现场快速动作**工具，按角色分三套：

### 路由骨架
- `/m` 入口：UA 检测 + session.mobile_role 派发到 `/m/doctor` / `/m/nurse` / `/m/groomer`
- 登录后 `_post_login_redirect`：优先 `?next=` → referer 里的 `?next=` → UA 兜底（手机跳 `/m`，桌面跳 `/admin`）
- 强制切换：URL 加 `?desktop=1` 临时桌面 / `?mobile=1` 临时手机 / `/m/desktop` 写 cookie 长期桌面
- `AdminUser.mobile_role` 字段：auto / doctor / nurse / groomer（auto = superadmin → doctor，staff → nurse）

### 三套 tab bar（按 `ctx.mobile_role` 渲染）
| 角色 | tab |
|---|---|
| 医生 | 今日 / 客户 / 住院 / 回访 / 我（5）|
| 助理 | 今日 / 住院 / 配药 / 回访 / 我（5）|
| 美容师 | 今日 / 美容单 / 新建 / 我（4）|

**注意 (M-audit 教训)**：tabbar 渲染按 `ctx.mobile_role`，不是 `session.mobile_role`。角色专属路由（`/m/doctor`、`/m/nurse`、`/m/groomer`、`/m/grooming*`）必须在 ctx 强制覆盖 `mobile_role`，否则用户 session 是 doctor 但访问 `/m/groomer` 会显示错误的 doctor tab。共享路由（`/m/inpatient`、`/m/follow-ups`、`/m/customers`）保持按 session role 渲染。

### 6 阶段 + 1 自查（commit 历史里全在）
- **M0**：补 GroomingOrder 前后照片 UI（数据库 `before_photos`/`after_photos` CSV 字段早有，UI 漏了）
- **M1**：路由骨架 + UA/role 跳转 + tab bar + 我的页 + `mobile_role` 字段迁移
- **M2**：助理版 — 住院打勾/体征/喂食/IO/交班 + 回访 + 待配药（`Prescription.dispensed_at` 新字段）
- **M2.5**：美容师 — 今日预约 + 新建美容单（chip 多选 + 客户搜索 JSON API）+ 现场拍美容前后照
- **M3**：医生只读层 — 今日 + 客户档案 + 病历详情（SOAP/处方/检查/疫苗/驱虫/住院）
- **M4**：医生可写层 — 病历编辑 + 新建 + 疫苗/驱虫快速新建
- **M5 圣杯**：开处方专屏 — 逐行卡片，药品搜索 180ms 节流 + 库存红黄绿 + 给药途径/频次 chip + 自动算取药量 + 模板套用 + 住院发药时刻表 + 底部固定栏合计
- **M6**：开检查单（chip 多选 lab/imaging/microscopy）+ 报告拍照（capture=environment）+ TNR 审核

### 后端复用模式
**不重写业务逻辑**，全部走现有 `/admin/...` POST 路由，加 `next_url` 参数 + `_safe_next(next_url, fallback)` 防开放重定向。涉及：
- `medication-log/{id}/check|skip|uncheck`
- `inpatient/{id}/vitals|io|feeding|handover`
- `follow-ups/{id}/handle`
- `prescriptions/create` + `visits/create|edit` + `exam-orders/create|upload`
- `vaccinations/create` + `dewormings/create`
- `grooming-orders/{id}/upload-photos|delete-photo`
- `app/{id}/manual-approve|reject`（TNR）

create 类路由 `next_url` 支持 `{id}` 占位符，服务端 replace 为新建出的记录 id（visits / exam-orders / prescriptions 都用）。

### `_m_ctx` + `_m_badges`
- `_m_ctx` 统一注入：csrf_token, mobile_role, admin_username, admin_role, admin_store（过滤用，超管=""）, admin_store_label（**显示用**，看 session.admin_store 实际值，超管挂横岗店就显示"横岗店"）
- `_base.html` 顶部条 topbar_sub 默认用 `admin_store_label`，**不用 admin_store**（M-audit 教训：两个变量混用会导致同一用户在不同页面看到不同门店标签）
- `_m_badges`：4 个首页待办数 — overdue_meds / pending_dispense / due_followups / pending_consents，全部按 `Pet.store` 关联过滤（早期错用 `Visit.clinic_store` 不存在的字段，M3 修了）

### Jinja2 坑（M-audit 找出来的 500 bug）
**当模板上下文是 Python dict 时，不要用 `dict.attr` 访问，要用 `dict['attr']`**。原因：Jinja 先试下标，失败回退 `getattr`，dict 内置方法 `items / keys / values / get / pop / update` 会被命中返回 bound method，再切片或调用就 500。例：`exam_rows = [{"eo":..., "items":[...], "reports":[...]}]` 在模板里必须写 `r['items']`，写 `r.items` 会 500。SQLAlchemy model 没这问题（`p.items` 走属性返回 relationship）。

### CSS（`static/m.css`）
纯手写、不依赖框架，固定类名：`m-card` / `m-card-title` / `m-btn`（+ secondary/outline/danger）/ `m-empty` / `m-tabbar` / `m-tab` / `m-todo-row` / `m-todo-icon` / `m-todo-badge` / `m-quick-tile` / `m-role-chip`（active 高亮）/ `m-section-label` / `m-msg`（ok / err）。body padding-bottom: calc(64px + safe-area-inset-bottom)，tab bar 高 54px。

### 手机不做的（按设计保留）
- 收费单结算（多笔混合支付）/ 库存盘点 / 财务报表 / 员工与权限 / 配额管理 / TNR复核 / 预约创建（涉及 TNR 配额 / 容量 / 重复申请校验，桌面端做）
- 受管控药品强制扫码 + 双人复核（未来迭代）

### 自动化实测脚本
`_test/shoot.py` 用 Playwright 跑 dafopet.com，模拟 iPhone 13 视口，登录后逐页截图保存 `_test/shots/`。CSS 改动后跑一遍可视化验证：
```
python _test/shoot.py all
python _test/shoot.py viewportcheck visit  # 单跑
```
注意：`full_page=True` 截图会把 `position:fixed` 的 tab bar 画在初始 viewport 位置，看似遮挡内容，实际不是 bug——用 `full=False` 滚到底拍非全页确认。

## 小程序关键页面
- `miniapp/pages/index/`：TNR 申请表单，多城市地址选择（深圳/东莞/惠州）
- `miniapp/pages/appointment/`：预约页，提前检查门店配额和爽约封禁
- `miniapp/pages/status/`：客户查看申请进度
- `miniapp/utils/shenzhen_regions.json`：多城市格式 `{"深圳市": {...}, "东莞市": {...}, "惠州市": {...}}`

## 开发注意事项
- 数据库迁移写在 `app/database.py` 的 `_run_migrations()` 函数，用 `ALTER TABLE IF NOT EXISTS` 风格
- CSRF token 所有 POST 表单都需要带
- 模板里用 `request.session.get('admin_role')` 判断权限，不要在 Python 里传多余变量
- 不要用 `git add -A`，按文件名 add
- 提交信息用中文 feat/fix/refactor 风格

## 企业微信集成
- **域名归属校验**：`GET /WW_verify_f5g3FhGYiTN0VHR8.txt` 返回校验内容（`app/main.py`）
- **Phase 1 单点登录（已完成）**：
  - `app/services/wecom_client.py`：access_token 缓存 + OAuth (`code_to_userid`) + `send_app_message`（Phase 2 用）
  - `/admin/wecom-login` → 跳企微 OAuth → `/admin/wecom-callback` → 找 `AdminUser.wecom_userid` → 写 session
  - `AdminUser.wecom_userid` 字段（含索引，唯一约束在路由里做）
  - 后台 `/admin/hr` 加「企微 userid」列，超管录入
  - 登录页 `/admin/login` 加「用企业微信登录」按钮
  - 服务器 `.env` 需配 `WECOM_CORP_ID` / `WECOM_AGENT_ID` / `WECOM_SECRET` + `PUBLIC_BASE_URL=https://dafopet.com`
  - 企微后台「网页授权及JS-SDK」可信域名：`dafopet.com`
- **Phase 4 语音/文字 agent**（已完成）：
  - 回调入口：`POST /wecom/callback`（AES-256-CBC + sha1 加解密，`app/services/wecom_callback_crypto.py`）
  - agent 主体：`app/services/wecom_agent.py`（11 个 function tool · LLM 5 跳上限）
  - 工具清单（写动作全部走复诵确认 + 草稿态）：
    - 查：find_customer / get_customer_profile / get_recent_visits / get_wallet / get_today_appointments
    - 写：create_visit / update_visit_field / create_appointment / create_exam_order / create_vaccination / create_deworming / create_grooming_order / create_prescription_draft
    - 处方草稿态：不传 item_id 不扣库存，status=draft，sync_invoice 跳过；受管控药品（is_controlled=True）拒绝
  - 上下文：`app/services/wecom_session.py` 内存 dict，30min TTL；current_customer_id / current_pet_id / current_visit_id + pending_action
  - 复诵确认：写工具返回 `PENDING:` 前缀，session 存 pending；用户回「确认」/「是」/「对」等触发执行；「取消」/「不」清空
  - 入口校验：`AdminUser.wecom_userid` 绑定；未绑用户拒绝
  - 模型拆分：`OPENAI_MODEL`（TNR 视觉审核用 doubao-1-5-vision-pro）/ `WECOM_AGENT_MODEL`（agent 用 doubao-1-5-pro 或 lite，便宜 3-4 倍；空则回退 OPENAI_MODEL）
  - 复用今日病历：`_resolve_or_list_visit` 自动找当天 open visit；无则列最近 3 次未结束病历让用户选「用病历 #N」/「新建病历」
  - 默认门店：`AdminUser.store` 直读（含超管，让超管也能有"常驻门店"）

## UK 风格规范（B 系列重写）
桌面后台正在按英伦极简（UK Minimal）风格分页重写。**所有新页 / 新模板必须严格遵守 7 条核心 + 9 条禁忌**，否则验收必退。

### 7 条核心
1. **字体全衬线 + 数字等宽**
   - 中文 fallback：`Georgia, "Times New Roman", "Source Han Serif SC", serif`
   - 数字：`tabular-nums lining-nums`，比正文略小 0.5pt
   - italic 只用于标签 / 小灰字 / 占位，**正经导航必须正体**
2. **颜色：纯黑 + 3 层灰 + 3 暗警示**
   - 文字：`#1a1a1a` / `#4a4a4a` / `#8a8a8a`
   - 强调：纯黑 `#1a1a1a`，**不是蓝**
   - 警示：暗红 `#7a2828` / 暗琥珀 `#6b4423` / 暗绿 `#1d4d3a`
3. **分隔：hairline 优先**
   - `0.5px solid var(--hair)` 做一切分组
   - section 之间靠**留白 40px**，不用 card 阴影
   - 真强调用 `双横线`；圆角 = 0，阴影 = none
4. **容器宽度 1280px**（不是 1480；1280 = 个人/书页感，1480 = 工业感）
5. **节奏：留白 > 内容**
   - 标题下 32px 才开始内容；区块间 40px；行高 1.7-1.8
   - "宁愿翻一下，不要挤"
6. **读写分离**（用户最强直觉，违反必退）
   - 主区 tab = **看**（纯展示）
   - 侧栏 = **做**（新建 / 操作动作）
   - 严禁在主 tab 内放 CREATE 按钮
7. **tab/nav 节制**
   - 一行最多 8 个，超过分流到侧栏列表
   - **不带 `(N)` 计数 badge**（噪声）
   - active = 加粗 + 1px 下划线，**禁 italic 长 nav**

### 9 条禁忌（错过会退）
- ❌ 任何 emoji
- ❌ 任何英文文案（含"est. 2024"、nav 英文、payment_method 直显 `wechat`/`cash`、状态字面 raw 落库）
- ❌ 圆角 + 阴影
- ❌ 饱和蓝色
- ❌ tab 后挂 `(N)` 计数
- ❌ 主区放 CREATE 按钮
- ❌ italic + `·` 分隔做长 nav（之前换行破碎）
- ❌ 顶部塞 chips 概要（信息密度过高，丢去侧栏 stat）
- ❌ 容器宽 1480

### 每个 tab/页面强制 4 段式
1. **顶上工具栏**：`+ 新建` 按钮 + 筛选（如果有）
2. **数据**：表或卡片时间线
   - 列表型 → `<table class="uk-tbl-tab">`
   - 详情型 / 时间线 → 卡片
3. **空态**：italic 灰字 `"暂无 XX · 可在右侧 +新建 XX"`
4. **分页**：底部 `共 N 条` + hairline

### 关键文件
- `static/uk_minimal.css`（~600 行）：设计系统
  - 变量 `--ink/--paper/--bg/--hair/--accent-red/...`
  - 组件 `.uk-card/.uk-btn/.uk-tbl/.uk-pill/.uk-kpi/.uk-tabs/.uk-filter/.uk-pager/.uk-msg`
- `static/uk_global.css`（411 行）：overlay 层
  - 映射老类名 `.btn/.card/.page-head/.data-table/.msg/.pill/.badge/.filter-card`
- `templates/uk/_base.html`：UK 桌面基础页（不 extends base.html）
- `templates/uk/workbench.html`：今日工作台（B1）
- `templates/uk/customer.html`：客户档案 Hub（B3 完整三段 B3.1/3.2/3.3）

### 完成进度
- ✅ B1 今日工作台 + 全局顶部条
- ✅ B3.1 客户档案 Hub（主区 + 侧栏 + 8 个医疗 tab 中 6 个）
- ✅ B3.2 8 个低频 tab 内容（销售/收费/体重/文书/钱包/套餐/押金/优惠券）
- ✅ B3.3 6 个新建 modal（协议签署/钱包充值/售套餐/收押金/发优惠券/发短信）+ 宠物编辑入口
- ⏳ B4 库存列表
- ⏳ B5 病历详情/表单（SOAP 必须罗马数字 I./II./III.）
- ⏳ B6 住院看板
- ⏳ B7 住院详情
- ⏳ B8 收费单详情
- ⏳ B9 预约管理
- ⏳ B10 HR 人事

### 不做
- 打印页（处方/收费/检查）已是行业标准格式，**不动**
- 手机 PWA（M0-M6）独立体系，**不动**

### 常见 bug 教训
- **Jinja2 dict.attr 陷阱**：context dict 必须用 `dict['key']` 访问，不要 `dict.key`。Jinja 优先下标，失败回退 `getattr`，命中 dict 内置 `items/keys/values/get/pop/update` 返回 bound method，500。
- **CSS 优先级**：内联 `style="..."` > 外部 `!important`。改色生效不了先查内联。
- **wallet 对象 vs wallet_balance**：context 传 `wallet_balance` 标量，不是 `wallet` 对象；模板写 `wallet.balance` 会 500（因为 wallet 变量不存在 → undefined.balance）。
- **支付方式英文落库**：模板顶部需要 `pay_method_zh` 映射表覆盖 11 种（cash/wechat/alipay/shouqianba/meituan/third_party/wallet/package/deposit/coupon/mixed）
