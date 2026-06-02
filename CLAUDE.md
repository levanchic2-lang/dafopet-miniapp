# 大风动物医院 — TNR 申请系统

## 项目概述
流浪猫绝育（TNR）申请 + 预约管理系统。
- **后端**：FastAPI + SQLAlchemy 2.0 + SQLite，入口 `app/main.py`
- **模板**：Jinja2，位于 `templates/`
- **小程序**：微信小程序，位于 `miniapp/`
- **部署**：git push main → webhook 自动部署服务器，无需手动登录

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
