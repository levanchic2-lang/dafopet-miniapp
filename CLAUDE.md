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
