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
- `AdminUser`：后台账号，`store` 存短名，`role` = superadmin/staff
- `TnrStoreConfig`：每家门店的 TNR 月度配额（`tnr_monthly_quota` 默认 30）和开关（`tnr_accepting`）
- `MediaFile`：申请的照片/视频，`kind` = application_image/application_video/surgery_before_image 等
- `FollowUp`：诊后回访任务，按 `Visit.visit_type` 自动衍生
  - 规则：surgery+3天 / postop+2天 / outpatient+7天 / beauty+14天 / vaccine 等不出
  - status：pending → due → sent → responded/closed/phone_pending
  - 调度：`app/services/followup_dispatch.py` 每小时通过 APScheduler 跑
  - 渠道：先小程序订阅消息（`wechat_tmpl_followup`）→ 短信网关 → 电话兜底
  - 客户反馈短链：`/follow-up/{token}`（无登录，token 即凭证）

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
