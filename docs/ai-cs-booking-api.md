# AI 客服预约接口

这些接口给医院自己的 AI 客服调用，用来查可约时段、代客预约、改期和取消。小程序顾客预约、TNR 预约不走这里。

调用时必须带服务器内部密钥，请求头：

```
X-AI-CS-Key: <密钥>
```

密钥来自环境变量 `AI_CS_API_KEY`（也可写在服务器 `.env`，不要写进小程序，不要复用管理员密码）。没配置时返回 503；密钥不对返回 401。

时间按北京时间。成功时 `ok` 为 `true`。失败时 HTTP 状态不是 200，正文是：

```json
{"ok": false, "error_code": "conflict", "message": "时间冲突：……"}
```

## 可约时段

`GET /api/ai-cs/slots/{kind}`

`kind`：

| kind | 含义 | 时长 | 容量 | 当天 | 时间 |
| --- | --- | --- | --- | --- | --- |
| `outpatient` | 门诊 | 60 分钟；项目名含「疫苗」或「驱虫」时为 30 分钟 | 3；疫苗/驱虫为 1 | 可以 | 10:00–21:00，开始时间须早于 21:00 |
| `vaccine` | 疫苗驱虫 | 30 分钟 | 1 | 可以 | 同上，10:00–21:00 |
| `surgery` | 绝育 / 手术 | 按项目，绝育 60 分钟 | 4 | 不可以，至少明天 | 11:00–18:00，18:00 不能作为开始时间 |
| `beauty` | 美容 | 按物种、体型、毛长，再加上附加项 | 不占医疗容量，一店一位美容师 | 可以 | 13:00–22:00，且必须在 22:00 前结束 |

公共参数：

| 参数 | 说明 |
| --- | --- |
| `store` | `横岗店`、`东环店`，或两家店的全称 |
| `date` | `YYYY-MM-DD`。今天到往后 15 天，含第 15 天 |
| `service_name` | 门诊、疫苗、手术的项目名。疫苗不填时按「疫苗/驱虫」 |
| `pet_size` `coat_length` `addons` `weight_kg` | 美容单只用。没传 `pet_size` 时，用 `weight_kg` 或 `pet_id` 档案里的最近体重分档。两者都没有则 400，`error_code=pet_weight_required`，不要按 60 分钟估算。已传入的体型档原样使用，包括历史文案。`weight_kg` 有值时，去浮毛按体重加时 |
| `pets` | 多只宠物一起美容时传 JSON 数组，见下文 |
| `pet_id` `birthday` `species` | 可选。用来在查时段时先做绝育/疫苗年龄和物种提示 |

返回字段：

| 字段 | 说明 |
| --- | --- |
| `slots` | 可开始时间，`HH:MM`，30 分钟一档。已经过去的当天时段不会出现 |
| `slot_details` | 与 `slots` 一一对应。每条有 `start` 和 `end`。结束时间以 `end` 为准 |
| `pet_size` | 美容实际用来计时的体型档。由传入值或体重推导 |
| `groups` | 只在多宠美容时出现。每一组是同一美容师上首尾相接的时间，含 `end` |
| `duration_minutes` `capacity_units` | 这一档会占用的时长和容量 |
| `rules` | 提前量、时间窗、15 天上限 |
| `arrival_note` | 手术/绝育时说明：预约时间只是预计到院时间，实际手术时间看当天安排 |
| `warning` / `warnings` | 年龄或物种未知时的提示。没有则为空字符串和空数组 |
| `late_penalty` | 恒为 `false`。迟到不另外收费或处罚 |

多宠美容的 `pets` 示例：

```json
[
  {"service_name": "猫洗护", "pet_size": "小型猫（不足 3kg）", "coat_length": "短毛", "addons": ""},
  {"service_name": "犬洗护", "weight_kg": 8, "coat_length": "短毛", "addons": "去浮毛"}
]
```

`groups[].pets[]` 里有每只的 `time` 和 `duration_minutes`。后一只从上一只结束时开始，可以不是 30 分钟整点（例如去底绒加了 45 分钟）。

已有的 `GET /api/appointments/beauty-slots` 仍然给后台和小程序估算美容时段，不需要这个密钥。有 `pet_size` 就用体型；否则用 `weight_kg`，或 `pet_id` / `appointment_id` 对应宠物档案里的最近体重。改期可带 `appointment_id`：请求没有另给体型或体重、且预约上已有时长时，原样沿用，不重算。都没有则 400，正文是「请先填写体型或体重」。已发布的顾客小程序查时段时总会带上 `pet_size`（没选体型就不会发这个请求），因此不受这次拒绝的影响。

## 预约

`POST /api/ai-cs/appointments`

```json
{
  "openid": "微信 openid",
  "store": "横岗店",
  "date": "2026-11-12",
  "time": "14:00",
  "kind": "vaccine",
  "service_name": "狂犬疫苗",
  "pet_id": 12,
  "notes": ""
}
```

`openid` 必须对应已经绑定的客户档案。只对应一个客户时可以不传 `customer_id`。同一个 openid 绑了多个客户时，必须再传 `customer_id`，而且它必须是其中一份档案。没传，或传的编号不属于这个微信，返回 `customer_ambiguous`。`message` 和 `choices` 只列出档案 id 和宠物名，不含手机号、地址或客户姓名。

```json
{"openid": "微信 openid", "customer_id": 12}
```

`choices` 示例：`[{"id": 12, "pet_names": ["大黄", "小白"]}]`。没有宠物时 `pet_names` 为空数组，文案里写「暂无宠物」。

美容多宠时不要只传一个 `pet_id`，改传 `pets` 数组，并给出第一只的 `time`。系统按算出的时长依次排开，全部成功才保存。每条预约的 `source` 为 `ai_cs`，状态为已确认。

写入前会再查一次容量、日历封锁和美容师冲突，避免两人同时约到同一档。

成功时返回 `appointments`（含 `id`、`source`、`store`、`appointment_date`、`appointment_time`、`duration_minutes`、`pet_id`）。手术另有 `arrival_note`。

## 改期

`POST /api/ai-cs/appointments/{id}/reschedule`

```json
{"openid": "……", "customer_id": 12, "store": "横岗店", "date": "2026-11-13", "time": "11:00"}
```

多个档案时 `customer_id` 必填，规则和新建预约相同。只绑了一份档案时可以不传。

`store` 可以不传，表示还在原门店。规则与新预约相同，并排除这张预约自己。已到店、已完成、已取消的不能改。TNR 不能改。

美容改期沿用这张预约已经保存的时长来查空档和做容量、冲突检查，不按新的体型表重算，也不改写这条时长。没有保存时长时，用预约上的体型，或这只宠物档案里的最近体重。都没有则 400，`error_code=pet_weight_required`，提示「请先填写体型或体重」，不按 60 分钟估算。

## 取消

`POST /api/ai-cs/appointments/{id}/cancel`

```json
{"openid": "……", "customer_id": 12, "reason": "临时有事"}
```

多个档案时 `customer_id` 必填。只绑了一份档案时可以不传。

当天可以取消。返回 `late_penalty: false` 和 `same_day_cancel_allowed: true`。没有迟到处罚。

## 疫苗的年龄和门店

只有从 `service_name`（或同时看的类别文字）里能唯一认出下面三种时才校验。认出来之后：

| 苗 | 动物 | 年龄 | 门店 |
| --- | --- | --- | --- |
| 狂犬（名称含「狂犬」或 rabies） | 犬和猫 | 满 3 个月 | 政府狂犬目前只在横岗店。约到东环会直接拒绝，并提示改约横岗 |
| 犬八联（「八联」「8联」） | 只限犬 | 满 6 周 | 两家店都可以 |
| 猫三联（「猫三联」「三联」「3联」） | 只限猫 | 满 8 周 | 两家店都可以 |

年龄已知但不够，返回 `vaccine_too_young`。物种对不上，返回 `vaccine_species`。狂犬约到东环，返回 `rabies_store`。

年龄或生日未知：仍然接受，`warning` 里说明需要到店由医生确认。物种没填：同样接受并警告。档案里已有生日时，以档案为准。

**为什么「疫苗/驱虫」不会触发这些校验：** 预约目录里的疫苗项目就叫「疫苗/驱虫」，它不区分狂犬、犬八联和猫三联，驱虫也走同一个时段规则。如果对这个笼统名字做年龄或门店限制，会把驱虫和未指明的疫苗误拒。客服要校验时，请把 `service_name` 写成具体苗名，例如「狂犬疫苗」「犬八联」「猫三联」。名称同时像两种苗时无法判断，也不做硬性校验，只在 `warning` 里说明。

疫苗时段仍然是 10:00–21:00，没有另外收窄。

绝育（项目名含「绝育」）另有一条：未满 6 个月拒绝；生日未知则接受并返回 `warning`。

## 美容加时

附加项里的「去浮毛」就是价目表的「去底绒」。有体重档案或传入 `weight_kg` 时按价目加时；没有体重时按体型取覆盖范围内更长的一档。其余附加项仍是 0 分钟。配置只在服务端的 `_BEAUTY_ADDON_EXTRA_MINUTES`。

## 错误码

| error_code | HTTP | 含义 |
| --- | --- | --- |
| `auth_not_configured` | 503 | 服务器没配 `AI_CS_API_KEY` |
| `auth_invalid` | 401 | 密钥不对 |
| `customer_not_found` | 400 | openid 没有绑定客户 |
| `customer_ambiguous` | 400 | 同一个 openid 绑了多个客户，却没有给出其中一份的 `customer_id`。响应里的 `choices` 只有 id 和宠物名 |
| `customer_phone_invalid` | 400 | 档案手机号不是 11 位大陆号码 |
| `pet_not_found` / `pet_not_owned` | 400 | 宠物不存在或不属于该客户 |
| `store_invalid` | 400 | 不是横岗店或东环店 |
| `date_invalid` / `time_invalid` | 400 | 日期或时间格式不对 |
| `date_in_past` | 400 | 早于今天 |
| `lead_time` | 400 | 绝育/手术不能约当天 |
| `beyond_horizon` | 400 | 超过 15 天 |
| `outside_hours` | 400 | 不在可约时间窗 |
| `slot_invalid` | 400 | 不是 30 分钟整点 |
| `slot_passed` | 400 | 当天这个点已经过了 |
| `kind_invalid` / `service_required` / `service_invalid` | 400 | 类型或项目不对 |
| `blocked` | 409 | 日历封锁，含美容师部分时段请假 |
| `capacity_full` | 409 | 该时段容量不够 |
| `conflict` | 409 | 和已有预约冲突（美容师同一时间只能一单，烘干机规则仍有效） |
| `age_too_young` | 400 | 绝育未满 6 个月 |
| `vaccine_too_young` | 400 | 疫苗年龄不够 |
| `vaccine_species` | 400 | 疫苗和物种不符 |
| `rabies_store` | 400 | 政府狂犬不在东环 |
| `status_locked` | 400 | 当前状态不能改期或取消 |
| `not_owner` | 403 | 预约不属于这个客户 |
| `appointment_not_found` | 404 | 没有这张预约 |
| `tnr_not_supported` | 400 | TNR 不走这个接口 |

后台改期如果勾了「仍要改约」，可以跳过容量、封锁和冲突，并在审计日志里记下 `override` 和被跳过的原因。这个开关只在后台，不给 AI 接口。
