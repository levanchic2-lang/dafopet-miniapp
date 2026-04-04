## 大风动物医院 · 流浪猫 TNR 申请系统（本地演示）

### 一键启动

双击 `一键启动.bat`，浏览器会打开：

- 申请页：`http://127.0.0.1:8765/`
- 医院后台：`http://127.0.0.1:8765/admin`
- 公猫展示墙：`http://127.0.0.1:8765/showcase`

后台默认密码：`123456`

### 接入豆包（火山方舟）视觉模型做自动图片审核

本项目调用方式为 **OpenAI 兼容接口**（`chat.completions` + `image_url`），因此只需在项目根目录创建 `.env` 并配置 3 个变量：

```env
OPENAI_API_KEY=你的火山方舟 API Key
OPENAI_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
OPENAI_MODEL=doubao-vision-pro-32k-2410128
```

说明：

- `OPENAI_MODEL` 请以火山方舟控制台里你的可用模型 ID 为准（上面是示例）。
- 申请提交后，系统会对上传的图片（及视频抽帧）进行辅助判断：是否疑似流浪猫、置信度、理由与建议下一步。
- 只有当模型输出 `is_likely_stray=true` 且 `confidence >= STRAY_AUTO_APPROVE_MIN_CONFIDENCE` 时，才会自动预审通过；否则进入人工审核。

### 常见问题

- 登录提示密码错误：确认你访问的是新启动的服务窗口；如端口被占用，脚本会先尝试释放 `8765` 再启动。

### 通知推送（Webhook）

若需要把“通过/拒绝/手术完成”等结果推送到你的系统、企业微信/飞书机器人、短信网关等，推荐使用 Webhook。

在 `.env` 中配置：

```env
NOTIFY_WEBHOOK_URL=https://your-domain.example/tnr/webhook
NOTIFY_WEBHOOK_SECRET=可选_用于签名校验
```

系统会在发生申请审核结果通知时，向 `NOTIFY_WEBHOOK_URL` 发起 `POST`，请求体为 JSON（UTF-8）。若配置了 secret，会附带请求头：

- `X-Signature`: `HMAC-SHA256(body, secret)` 的 hex

### 通知推送（短信网关，推荐）

国内短信通常通过“短信网关服务”统一对接供应商（阿里云/腾讯云/容联等），本项目已内置对短信网关的推送。

在 `.env` 中配置：

```env
SMS_GATEWAY_URL=https://your-domain.example/sms/gateway
SMS_GATEWAY_SECRET=可选_用于签名校验
```

系统会在审核结果通知时，向 `SMS_GATEWAY_URL` 发起 `POST`，请求体 JSON 示例：

```json
{
  "event": "application_result",
  "application_id": 123,
  "phone": "13800000000",
  "approved": true,
  "subject": "[大风动物医院·TNR] 申请已通过",
  "text": "短信正文（可由网关再做模板渲染）",
  "extra": ""
}
```

若配置了 `SMS_GATEWAY_SECRET`，同样会附带 `X-Signature`（HMAC-SHA256）。

本地联调用于验收：运行 `tools/sms_gateway_mock.py`，并把 `SMS_GATEWAY_URL` 设为 `http://127.0.0.1:9878/sms`。

### 通知推送（微信小程序订阅消息，推荐）

如果你希望“用户在小程序里收到审核结果/手术完成提醒”，推荐使用 **小程序订阅消息**（比短信更低成本、更高触达）。

需要你在小程序后台配置订阅消息模板，并在 `.env` 填写：

```env
WECHAT_APPID=你的小程序AppID
WECHAT_APPSECRET=你的小程序AppSecret
WECHAT_TMPL_APPLICATION_RESULT=审核结果模板ID
WECHAT_TMPL_SURGERY_DONE=手术完成模板ID（可选）
WECHAT_MESSAGE_PAGE=pages/index/index
```

本项目提供接口 `POST /api/wechat/login`：小程序前端传 `{ "code": "<wx.login拿到的js_code>" }`，后端会返回 `{ "openid": "..." }`。

申请提交接口 `/api/apply` 支持额外字段 `wechat_openid`（可选）。若填写且模板 ID 配置正确，系统会在以下节点推送订阅消息：

- 预通过（待复核）
- 通过/拒绝（人工或自动）
- 手术完成

注意：订阅消息 `data` 字段的关键词（如 `thing1`、`phrase2` 等）必须与你所选模板的“关键词字段”一致；若不一致，系统会在后台通知日志里记录失败原因，方便调整。

### 小程序前端（已提供可运行工程）

本仓库已包含一个最小可跑通的微信小程序前端工程：`miniapp/`。

使用方式：

- 用 **微信开发者工具** 打开 `miniapp/` 目录（`project.config.json` 已包含 appid）。
- 本地开发（模拟器）可直接访问 `http://127.0.0.1:8765`；真机调试需要：
  - 用 `一键启动_手机联调.bat` 启动后端（监听 `0.0.0.0`）
  - 把 `miniapp/app.js` 里的 `apiBase` 改成你电脑的局域网 IP（例如 `http://192.168.1.10:8765`）
  - 微信开发者工具里打开「不校验合法域名、web-view、TLS版本」（否则真机可能拦截 HTTP 请求）
- 订阅消息授权：小程序页面点击“开启通知提醒”会弹出授权弹窗（系统 UI），用户点“允许”后才能收到推送。

提示：为了演示闭环，小程序端目前只上传 **第 1 张图片**（已足够触发自动审核与推送）。如果你需要支持多张图片/视频批量上传，我可以把后端改成“两段式上传”（先创建申请ID，再增量上传媒体）。


