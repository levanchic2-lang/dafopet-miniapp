from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "大风动物医院 · 流浪猫 TNR 申请"
    database_url: str = "sqlite:///./data/tnr.db"
    upload_dir: str = "uploads"
    # 后台一键备份 zip 输出目录（相对项目根目录）
    backup_dir: str = "backups"
    admin_password: str = "123456"

    # 多模态 API：密钥留空则跳过自动识别。可与 OpenAI 官方或「OpenAI 兼容」端点共用以下三项。
    # 阿里云百炼/通义：base_url=https://dashscope.aliyuncs.com/compatible-mode/v1 ，model=qwen-vl-plus 等
    openai_api_key: str = ""
    openai_base_url: str = ""
    openai_model: str = "gpt-4o-mini"

    # 自动通过：仅当 AI 判定为疑似流浪猫且置信度 >= 该值（0~1）
    stray_auto_approve_min_confidence: float = 0.78

    # 通知：若配置了 SMTP 则尝试发邮件；否则仅写入数据库与日志
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    notify_email_subject_prefix: str = "[大风动物医院·TNR] "

    # 通知：Webhook（推荐，对接企业微信/飞书/短信网关/自有系统）
    # 若提供 url，将在关键节点 POST JSON payload
    notify_webhook_url: str = ""
    # 可选：若填写，会在请求头加入 X-Signature=HMAC-SHA256(body, secret)
    notify_webhook_secret: str = ""

    # 通知：短信网关（推荐的“国内短信”接入方式）
    # 系统将向该网关 POST 标准 JSON，由网关对接具体短信供应商（阿里云/腾讯云/容联等）
    sms_gateway_url: str = ""
    sms_gateway_secret: str = ""

    # ── 腾讯云短信（直连，无需自建网关） ──
    # 配齐这 5 项后，协议签署等场景自动通过腾讯云发短信
    tencent_sms_secret_id:  str = ""    # 腾讯云控制台 → 访问密钥
    tencent_sms_secret_key: str = ""
    tencent_sms_sdk_app_id: str = ""    # 短信控制台 → 应用ID（10 位数字字符串）
    tencent_sms_sign_name:  str = ""    # 已审核通过的签名，如 "大风动物医院"
    tencent_sms_region:     str = "ap-guangzhou"
    # 协议签署「身份验证码」模板 ID（已审核通过），如 "2648448"
    # 模板内容：【深圳市大风动物医院】您的签字验证码 {1}，{2} 分钟内有效，请勿告诉他人
    # 模板参数顺序：1=6 位验证码, 2=有效时长（分钟）
    # 流程：客户打开协议 H5 → 系统发验证码到手机 → 客户输入 → 验证身份后签字
    tencent_sms_tmpl_consent: str = ""

    # 会话密钥：重启后登录仍有效；生产环境务必修改
    session_secret: str = "dev-change-session-secret-in-env"

    # 数据清理「二次口令」：删除病历及关联数据这类高危操作的额外口令。
    # 留空 = 工具禁用（任何人都进不去）；务必只写在服务器 .env，不要提交进仓库。
    data_purge_password: str = ""

    # 微信小程序订阅消息（推荐国内推送方式）
    wechat_appid: str = ""
    wechat_appsecret: str = ""
    # 审核结果订阅消息模板 ID（需要你在小程序后台配置）
    wechat_tmpl_application_result: str = ""
    # 手术完成订阅消息模板 ID（可选）
    wechat_tmpl_surgery_done: str = ""
    # 预约状态通知模板 ID（确认/取消预约）
    wechat_tmpl_appointment: str = ""
    # 待人工审核提醒模板 ID（字段：thing2=申请人,time3=申请时间,thing4=业务类型,character_string5=业务编号,phrase11=审核状态）
    wechat_tmpl_pending_manual: str = ""
    # 审核不通过通知模板 ID（字段：thing1=审核说明,phrase2=审核结果,thing3=审核对象,time4=审核时间）
    wechat_tmpl_rejection: str = ""
    # 手术前提醒模板 ID（字段：thing1=用户名称,thing2=预约项目,time3=预约时间,thing4=温馨提示）
    wechat_tmpl_surgery_reminder: str = ""
    # 疫苗到期提醒模板 ID（字段：thing1=宠物名,thing2=疫苗类型,time3=到期日,thing4=温馨提示）
    wechat_tmpl_vaccine_reminder: str = ""
    # 回访通知模板 ID（字段：thing1=宠物名,thing2=就诊类型,time3=就诊日,thing4=温馨提示）
    wechat_tmpl_followup: str = ""
    # 协议签署通知模板 ID（电子合同签约通知，字段：thing5=甲方,thing6=乙方,thing1=合同名称,time12=发起时间,thing4=备注）
    wechat_tmpl_consent: str = ""
    # 点击消息打开的小程序页面（可选）
    wechat_message_page: str = "pages/index/index"

    # 订阅消息模板关键词字段（重要：必须与所选模板的关键词 key 一致）
    # 示例（常见）：thing1,thing2,thing3,thing4,thing5
    # 你的模板若包含 time5/phrase2 等，请按模板详情填写
    wechat_fields_application_result: str = "thing1,thing2,thing3,thing4,thing5"
    wechat_fields_surgery_done: str = "thing1,thing2,thing3"
    # 预约通知模板字段：time1=预约时间,thing2=预约项目,phone_number3=联系电话,thing4=客户姓名,thing5=预约地址
    wechat_fields_appointment: str = "time1,thing2,phone_number3,thing4,thing5"
    # 待人工审核模板字段：thing2=申请人,time3=申请时间,thing4=业务类型,character_string5=业务编号,phrase11=审核状态
    wechat_fields_pending_manual: str = "thing2,time3,thing4,character_string5,phrase11"
    # 拒绝通知模板字段：thing1=审核说明,phrase2=审核结果,thing3=审核对象,time4=审核时间
    wechat_fields_rejection: str = "thing1,phrase2,thing3,time4"
    # 手术前提醒模板字段：thing1=用户名称,thing2=预约项目,time3=预约时间,thing4=温馨提示
    wechat_fields_surgery_reminder: str = "thing1,thing2,time3,thing4"
    # 疫苗到期提醒模板字段：thing5=温馨提示,thing8=服务对象(宠物名),thing11=服务项目(疫苗类型),time7=服务时间(到期日)
    wechat_fields_vaccine_reminder: str = "thing5,thing8,thing11,time7"
    # 回访通知模板字段：thing1=宠物名,thing2=就诊类型,time3=就诊日,thing4=温馨提示
    wechat_fields_followup: str = "thing1,thing2,time3,thing4"
    # 协议签署模板字段：thing5=甲方(客户),thing6=乙方(医院),thing1=合同名称(协议标题),time12=发起时间,thing4=备注
    wechat_fields_consent: str = "thing5,thing6,thing1,time12,thing4"

    # 公开访问 URL 前缀（用于生成回访反馈短链等）。例：https://api.dafopet.com
    public_base_url: str = ""

    # 地理编码（可选）：用于把经纬度反查为中文地址展示
    amap_web_key: str = ""  # 高德 Web 服务 Key（建议放在 .env，不要写死在前端）

    # ── 企业微信集成 ──
    # Phase 1：自建应用 OAuth 单点登录（员工免密进系统）
    # Phase 2：应用消息推送（13 类工作提醒推到员工聊天）
    # 配置位置：企业微信管理后台 → 应用管理 → 自建应用
    wecom_corp_id:   str = ""   # 企业 ID（我的企业页最下）
    wecom_agent_id:  str = ""   # 自建应用 AgentID
    wecom_secret:    str = ""   # 自建应用 Secret

    # Phase 4：语音/文字消息回调（接收员工发给应用的消息 → AI agent 执行）
    # 配置位置：自建应用 → 接收消息 → API 接收
    wecom_callback_token:    str = ""  # 自定义 Token（明文匹配签名）
    wecom_callback_aes_key:  str = ""  # 43 字符 EncodingAESKey

    # 企微 agent 专用模型（function calling 走纯文本路径，独立于 TNR 视觉审核）
    # 留空则回退到 OPENAI_MODEL
    wecom_agent_model: str = ""


settings = Settings()
