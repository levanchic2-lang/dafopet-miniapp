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

    # 会话密钥：重启后登录仍有效；生产环境务必修改
    session_secret: str = "dev-change-session-secret-in-env"

    # 微信小程序订阅消息（推荐国内推送方式）
    wechat_appid: str = ""
    wechat_appsecret: str = ""
    # 审核结果订阅消息模板 ID（需要你在小程序后台配置）
    wechat_tmpl_application_result: str = ""
    # 手术完成订阅消息模板 ID（可选）
    wechat_tmpl_surgery_done: str = ""
    # 预约状态通知模板 ID（确认/取消预约）
    wechat_tmpl_appointment: str = ""
    # 点击消息打开的小程序页面（可选）
    wechat_message_page: str = "pages/index/index"

    # 订阅消息模板关键词字段（重要：必须与所选模板的关键词 key 一致）
    # 示例（常见）：thing1,thing2,thing3,thing4,thing5
    # 你的模板若包含 time5/phrase2 等，请按模板详情填写
    wechat_fields_application_result: str = "thing1,thing2,thing3,thing4,thing5"
    wechat_fields_surgery_done: str = "thing1,thing2,thing3"
    # 预约通知模板字段：time1=预约时间,thing2=预约项目,phone_number3=联系电话,thing4=客户姓名,thing5=预约地址
    wechat_fields_appointment: str = "time1,thing2,phone_number3,thing4,thing5"

    # 地理编码（可选）：用于把经纬度反查为中文地址展示
    amap_web_key: str = ""  # 高德 Web 服务 Key（建议放在 .env，不要写死在前端）


settings = Settings()
