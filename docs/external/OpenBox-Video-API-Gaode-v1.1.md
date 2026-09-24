# OpenBox 视频创作开放接口文档

| 项目 | 内容 |
|---|---|
| 文档版本 | v1.1 |
| 发布日期 | 2026-09-21 |
| 适用方 | 高德 |
| 接口版本 | `/v1` |
| 维护方 | OpenBox 技术团队 |

**修订记录**

| 版本 | 日期 | 说明 |
|---|---|---|
| v1.0 | 2026-09-18 | 首版，最小接口集与待确认问题 |
| v1.1 | 2026-09-21 | 回写高德确认结果：轮询为主、服务端调用、成片拉取签名 URL；模型参数改为质量 / 标准 / 灵活三档；固化视频规格默认值；删除已确认问题 |

---

## 目录

1. [概述](#1-概述)
2. [对接约定](#2-对接约定)
3. [接入准备](#3-接入准备)
4. [通用约定](#4-通用约定)
5. [接口列表](#5-接口列表)
6. [接口详情](#6-接口详情)
7. [消息与部件](#7-消息与部件)
8. [确认卡交互](#8-确认卡交互)
9. [完整调用时序](#9-完整调用时序)
10. [计费说明](#10-计费说明)
11. [遗留事项](#11-遗留事项)
12. [附录 A：错误码](#12-附录-a错误码)
13. [附录 B：事件流（可选）](#13-附录-b事件流可选)

---

## 1. 概述

### 1.1 能力范围

本接口开放 OpenBox 的对话式视频能力，接入方通过自然语言驱动 AI 完成：

- **视频创作**：给一个主题或讲稿，AI 完成写稿、分镜、逐镜生成、口播校验、合成，输出成片。
- **视频剪辑**：上传素材，用自然语言描述剪辑要求（拼接、转场、片头等），输出成片。

### 1.2 交互模型

接口是**会话式**的，一次视频任务对应一个会话，会话内可多轮修改。

```
上传素材（可选） → 创建会话 → 发送需求 → 轮询消息 → 回复确认卡 → 取得成片 → 结束
```

视频创作涉及付费生成，AI 在花费前会通过**确认卡**向用户确认（讲稿确认、分镜与报价确认）。接入方在自己的界面上展示确认卡并把用户选择回传，详见第 8 节。

### 1.3 名词

| 名词 | 说明 |
|---|---|
| 会话 Session | 一次视频任务的上下文容器，`ses_` 开头 |
| 消息 Message | 会话内的一条用户输入或 AI 回复，`msg_` 开头 |
| 部件 Part | 一条消息里的内容块：文本、确认卡、文件、进度 |
| 确认卡 Question | AI 向用户提出的选择题，`qst_` 开头 |
| 文件 File | 上传的素材或 AI 产出的视频，`fil_` 开头 |

---

## 2. 对接约定

以下为双方已确认的对接方式，接口设计以此为准。

| 项 | 约定 |
|---|---|
| 调用方 | 高德服务端调用，API Key 不下发到客户端 |
| 结果获取 | 轮询消息接口（6.5），建议间隔 2–3 秒；事件流为可选能力，见附录 B |
| 确认卡 | 高德按第 8 节结构自行渲染 |
| 成片交付 | 高德拉取签名下载地址自行存储 |
| 任务回调 | 不需要 Webhook |
| 用户范围 | 仅高德 B 端用户，按高德整体对账，不区分终端用户 |
| 视频规格 | 竖屏 9:16，1080p，成片时长 30 秒以内，不加字幕，不强制片头片尾 |
| 素材 | 由高德上传；无素材时允许 AI 生成画面 |
| 模型参数 | 质量 / 标准 / 灵活三档代号 `high` / `medium` / `low`，与网页端一致，见 6.3 |
| 调用量 | 日调用 100 以内，峰值每分钟 10 以内 |
| 确认卡超时 | 10 分钟 |
| 数据保留 | 素材与成片保留 30 天 |
| 内容审核 | 高德自行审核成片 |
| 安全 | API Key 鉴权，HTTPS |

上述视频规格已作为高德接入的**默认值**预置，每次请求无需重复传递。个别任务需要不同规格时，在需求文本中说明即可（如"这条做 16:9 横屏"）。

---

## 3. 接入准备

| 项 | 说明 |
|---|---|
| 接口地址 | 测试环境：`https://<待提供>/v1`　生产环境：`https://<待提供>/v1` |
| API Key | 由 OpenBox 签发，形如 `obx_sk_xxxxxxxx`；泄露请联系 OpenBox 重置 |
| 网络 | HTTPS，服务端到服务端 |
| 素材格式 | 图片 `jpg/png/webp`，视频 `mp4/mov`，音频 `mp3/wav/m4a`；单文件 ≤ 200 MB |

---

## 4. 通用约定

### 4.1 请求

- 所有请求带请求头 `Authorization: Bearer <API Key>`。
- 请求体与响应体为 `application/json; charset=utf-8`，文件上传为 `multipart/form-data`。
- 字段命名为 `snake_case`，时间为 ISO 8601 UTC 字符串，如 `2026-09-21T08:00:00Z`。

### 4.2 响应

- 成功返回 `2xx`，响应头 `X-Request-Id` 为本次请求唯一标识，排查问题时请提供。
- 失败返回非 `2xx`，响应体统一为：

```json
{
  "error": {
    "code": "SESSION_BUSY",
    "message": "session is busy, abort it first",
    "request_id": "req_01J8ZK..."
  }
}
```

错误码见附录 A。

### 4.3 限流

| 维度 | 额度 |
|---|---|
| 请求频率 | 60 次 / 分钟 |
| 同时处理中的会话 | 5 个 |

按高德预期量（峰值每分钟 10 次以内）留有余量。超限返回 `429`，响应头 `Retry-After` 为建议等待秒数。

### 4.4 幂等

发送需求接口支持 `client_message_id` 幂等键。网络超时重发时带相同的键，服务端返回原结果，不重复执行。

---

## 5. 接口列表

| 序号 | 方法 | 路径 | 说明 |
|---|---|---|---|
| 1 | POST | `/v1/files` | 上传素材 |
| 2 | POST | `/v1/sessions` | 创建会话 |
| 3 | GET | `/v1/sessions/{session_id}` | 查询会话状态 |
| 4 | POST | `/v1/sessions/{session_id}/messages` | 发送需求或修改意见 |
| 5 | GET | `/v1/sessions/{session_id}/messages` | 轮询消息 |
| 6 | POST | `/v1/sessions/{session_id}/questions/{question_id}` | 回复确认卡 |
| 7 | POST | `/v1/sessions/{session_id}/questions/{question_id}/reject` | 拒绝确认卡 |
| 8 | POST | `/v1/sessions/{session_id}/abort` | 中止当前处理 |
| 9 | GET | `/v1/files/{file_id}/content` | 重新获取文件下载地址 |

---

## 6. 接口详情

### 6.1 上传素材

`POST /v1/files`

**请求** `multipart/form-data`

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| file | 文件 | 是 | 素材文件，≤ 200 MB |

**响应** `201`

```json
{
  "id": "fil_01J8ZK3M...",
  "filename": "road.mp4",
  "mime_type": "video/mp4",
  "size": 10485760,
  "duration_s": 12.4,
  "created_at": "2026-09-21T08:00:00Z"
}
```

| 字段 | 说明 |
|---|---|
| id | 文件 ID，发送需求时放入 `attachments` |
| duration_s | 音视频时长，秒；图片为 `null` |

文件保留 30 天。

---

### 6.2 创建会话

`POST /v1/sessions`

**请求**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| title | string | 否 | 会话标题，便于后台查看 |
| quality | string | 否 | 生成档位 `high` / `medium` / `low`，缺省 `medium`，见 6.3 |
| metadata | object | 否 | 接入方自定义键值，用于对账，最多 16 个键，值为字符串 |

```json
{
  "title": "五一杭州自驾攻略口播",
  "quality": "high",
  "metadata": { "task_id": "t_456" }
}
```

**响应** `201`

```json
{
  "id": "ses_01J8ZK...",
  "title": "五一杭州自驾攻略口播",
  "status": "idle",
  "quality": "high",
  "metadata": { "task_id": "t_456" },
  "credits_used": "0",
  "created_at": "2026-09-21T08:00:00Z",
  "updated_at": "2026-09-21T08:00:00Z"
}
```

| status | 含义 |
|---|---|
| idle | 空闲，可发送新消息 |
| busy | 处理中 |
| error | 上一轮出错，可继续发消息 |

建议一个视频任务一个会话。同一会话内继续发消息即为修改（如"第二段语气轻快一点"）。

---

### 6.3 生成档位

`quality` 与 OpenBox 网页端的三档视频模型一致，接入方无需关心底层模型名称。分辨率按高德 1080p 要求预置，不需要传。

| 代号 | 名称 | 输出分辨率 | 说明 |
|---|---|---|---|
| `high` | 质量 | 1080p | 画质优先，适合成片 |
| `medium` | 标准 | 1080p | 日常短视频的均衡选择，**默认** |
| `low` | 灵活 | 768p | 快速出片、成本最低，适合先看效果；**该档模型不支持 1080p** |

成片时长 30 秒以内由多镜拼接完成，档位只影响单镜生成，不限制成片总长。
高德要求 1080p 成片，正式出片请使用 `high` 或 `medium`；`low` 仅适合预览效果。
档位可在会话创建时指定，也可在后续需求文本中切换（如"用质量档重做"）。各档单价不同，见第 10 节。

---

### 6.4 发送需求

`POST /v1/sessions/{session_id}/messages`

**请求**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| text | string | 是 | 自然语言需求或修改意见，≤ 20 KB |
| attachments | string[] | 否 | 素材文件 ID 列表，≤ 20 个 |
| client_message_id | string | 否 | 幂等键，建议用接入方任务 ID + 序号 |

```json
{
  "text": "帮我做一条 25 秒的五一杭州自驾攻略口播，用我上传的路况视频做素材",
  "attachments": ["fil_01J8ZK3M..."],
  "client_message_id": "t_456-1"
}
```

**响应** `202`

```json
{
  "session_id": "ses_01J8ZK...",
  "user_message_id": "msg_01J8ZK...",
  "assistant_message_id": "msg_01J8ZL..."
}
```

接口立即返回，之后通过 6.5 轮询 `assistant_message_id` 对应的消息获取进展与结果。
会话处于 `busy` 时返回 `409 SESSION_BUSY`，需等待空闲或先中止。

---

### 6.5 轮询消息

`GET /v1/sessions/{session_id}/messages`

**查询参数**

| 参数 | 必填 | 说明 |
|---|---|---|
| after | 否 | 消息 ID，只返回其后的消息（含该消息本身的最新状态） |
| limit | 否 | 默认 50 |

建议间隔 2–3 秒。响应体结构见第 7 节。

**轮询逻辑**

1. 发送需求后，以 `after=<user_message_id>` 轮询。
2. 每次取回 AI 消息（`role=assistant`），按 `parts` 更新界面：文本追加显示、出现 `status=pending` 的确认卡则渲染并等待用户、出现 `role=final` 的文件即为成片。
3. AI 消息 `finish` 不为 `null` 即本轮结束，停止轮询。

也可用 `GET /v1/sessions/{session_id}` 只看 `status`，`idle` 后再拉一次消息。

---

### 6.6 回复确认卡

`POST /v1/sessions/{session_id}/questions/{question_id}`

**请求**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| answers | string[][] | 是 | 与确认卡 `questions` 顺序一一对应，每项为选中的选项 `label` 数组 |

```json
{ "answers": [ ["可以"], ["带转场和动效字幕（约 120 积分）"] ] }
```

- 单选题数组长度为 1，多选题可多个。
- 确认卡 `custom=true` 时，可填写选项之外的自由文本，如 `["缩短到 20 秒左右"]`。

**响应** `200` `{ "ok": true }`

确认卡已回复或已超时返回 `409 INTERACTION_RESOLVED`。

---

### 6.7 拒绝确认卡

`POST /v1/sessions/{session_id}/questions/{question_id}/reject`

用户不想继续时调用。AI 停止当前流程并说明，之后可发新消息继续。

**响应** `200` `{ "ok": true }`

---

### 6.8 中止当前处理

`POST /v1/sessions/{session_id}/abort`

**响应** `200`

```json
{ "ok": true, "aborted": true }
```

`aborted=false` 表示当时没有处理中的任务。已提交的付费生成不会退回。

---

### 6.9 查询会话

`GET /v1/sessions/{session_id}`

响应结构同 6.2。`credits_used` 为本会话累计消耗积分。

---

### 6.10 重新获取文件下载地址

`GET /v1/files/{file_id}/content`

**响应** `302` 跳转至签名下载地址，有效期 24 小时。消息中的 `url` 过期后用此接口重新获取。

---

## 7. 消息与部件

### 7.1 消息结构

```json
{
  "data": [
    {
      "id": "msg_01J8ZL...",
      "role": "assistant",
      "finish": null,
      "error": null,
      "created_at": "2026-09-21T08:00:05Z",
      "updated_at": "2026-09-21T08:00:12Z",
      "parts": [ ... ]
    }
  ],
  "has_more": false
}
```

| 字段 | 说明 |
|---|---|
| role | `user` 用户消息，`assistant` AI 回复 |
| finish | `null` 处理中；`stop` 已完成；`error` 出错；`aborted` 被中止 |
| error | 出错时 `{ "code": "...", "message": "..." }`，见附录 A |
| parts | 内容部件数组，按产生顺序排列，处理中会不断追加与更新 |

### 7.2 部件类型

接入方需处理以下四种，其他 `type` 忽略即可。

**文本 `text`**

```json
{ "type": "text", "id": "prt_01...", "text": "好的，先给你写讲稿。\n\n## 讲稿\n..." }
```
Markdown 格式。同一部件的 `text` 在处理中会增长，按 `id` 覆盖更新。

**确认卡 `question`**

```json
{
  "type": "question",
  "id": "prt_02...",
  "question_id": "qst_01J8ZM...",
  "status": "pending",
  "expires_at": "2026-09-21T08:10:00Z",
  "questions": [ ... ]
}
```
`status`：`pending` 待回复；`answered` 已回复；`rejected` 已拒绝；`timeout` 超时。结构详见第 8 节。

**文件 `file`**

```json
{
  "type": "file",
  "id": "prt_03...",
  "file": {
    "id": "fil_01J8ZN...",
    "filename": "final_1080p.mp4",
    "mime_type": "video/mp4",
    "size": 24117248,
    "duration_s": 26.2,
    "width": 1080,
    "height": 1920,
    "url": "https://.../signed?...",
    "role": "final"
  }
}
```
`role`：`final` 成片；`intermediate` 单镜或中间产物；`input` 素材回显。`url` 有效期 24 小时。

**进度 `progress`**

```json
{ "type": "progress", "id": "prt_04...", "stage": "generating", "detail": "正在生成第 2/4 镜", "percent": 40 }
```
`stage`：`scripting` 写稿、`planning` 分镜、`generating` 生成镜头、`transcribing` 口播校验、`composing` 合成。可选展示。

---

## 8. 确认卡交互

### 8.1 何时出现

| 场景 | 确认卡 | 说明 |
|---|---|---|
| 视频创作 | 讲稿确认 | AI 写完讲稿后，确认内容与成片风格 |
| 视频创作 | 分镜与报价确认 | 展示每镜画面、时长与总价，用户确认后才开始付费生成 |
| 视频创作（条件） | 口播校验结果 | 生成后转写发现口误时，确认重拍或接受 |
| 视频创作（条件） | 合成确认 | 合成费用高于报价或用户中途改风格时 |
| 视频剪辑 | 剪辑方案确认 | 通常一张，简单任务可能没有 |

一次创作通常 2 张卡。**用户确认分镜与报价前不产生生成费用。**

### 8.2 数据结构

```json
{
  "question_id": "qst_01J8ZM...",
  "status": "pending",
  "expires_at": "2026-09-21T08:10:00Z",
  "questions": [
    {
      "header": "成稿",
      "question": "讲稿如上，可以开始分镜吗？",
      "options": [
        { "label": "可以", "description": "" },
        { "label": "需要修改", "description": "" }
      ],
      "multiple": false,
      "custom": true
    },
    {
      "header": "成片风格",
      "question": "选择成片方式",
      "options": [
        { "label": "无特效拼接（免费）", "description": "" },
        { "label": "带转场和动效（约 120 积分）", "description": "预估价，以实际合成为准" }
      ],
      "multiple": false,
      "custom": false
    }
  ]
}
```

| 字段 | 说明 |
|---|---|
| questions | 一张卡 1–4 个问题，推荐答案排在 `options` 第一位 |
| header | 问题短标题 |
| multiple | 是否多选 |
| custom | 是否允许用户输入选项之外的文本 |
| expires_at | 超时时间，10 分钟。超时按拒绝处理，AI 停下，用户可再发消息继续 |

确认卡正文（讲稿全文、分镜表、报价明细）在同一消息中位于卡片之前的 `text` 部件里，需一并展示。

### 8.3 渲染建议

- 每个问题渲染为单选或多选组，`custom=true` 时增加"自己输入"框。
- 展示 `expires_at` 倒计时，超时后禁用提交。
- 提交时按问题顺序组装 `answers`，调用 6.6。
- 提交后继续轮询，卡片 `status` 变为 `answered` 后收起。

---

## 9. 完整调用时序

```
高德服务端                                     OpenBox
  │  POST /v1/files (road.mp4)                  │
  │◄─ 201 fil_A                                 │
  │  POST /v1/sessions  {quality:"high"}        │
  │◄─ 201 ses_1                                 │
  │  POST /v1/sessions/ses_1/messages           │  text="25秒杭州自驾攻略口播", attachments=[fil_A]
  │◄─ 202  msg_u1 / msg_a1                      │
  │  GET  .../messages?after=msg_u1  (每 2–3 秒) │
  │◄─ msg_a1: text(讲稿)                        │
  │◄─ msg_a1: text(讲稿) + question(pending)    │
  │   ── 用户在高德界面选择 ──                    │
  │  POST .../questions/qst_1  {answers}        │
  │  GET  .../messages?after=msg_u1             │
  │◄─ msg_a1: … + text(分镜表+报价) + question   │
  │  POST .../questions/qst_2  {answers}        │
  │  GET  .../messages?after=msg_u1             │
  │◄─ msg_a1: … + progress(generating 2/4)      │
  │◄─ msg_a1: … + progress(composing)           │
  │◄─ msg_a1: … + file(role=final), finish=stop │  ← 停止轮询，拉取 url 存储
  │                                             │
  │  POST /v1/sessions/ses_1/messages           │  text="第三段语气轻快一点"   ← 修改轮，流程同上
```

参考耗时：写稿与分镜约 30–60 秒；每镜生成约 1–3 分钟，多镜并行；合成约 1–2 分钟。一条 30 秒以内成片端到端约 4–8 分钟。

---

## 10. 计费说明

| 项 | 说明 |
|---|---|
| 计费单位 | 积分，由合同预充到高德账户 |
| 费用点 | 镜头生成（主要，按档位与秒数）、云端合成（选"带转场"风格时，按分钟）、文本对话（极少） |
| 确认机制 | 分镜与报价确认卡明示总价，用户确认后才产生生成费用 |
| 余额不足 | 发送需求返回 `402`；处理中 AI 消息 `error.code` 为 `INSUFFICIENT_CREDITS` |
| 消耗查询 | `GET /v1/sessions/{id}` 的 `credits_used` |
| 对账 | 以 OpenBox 账单为准，按会话 `metadata.task_id` 关联 |

各档位单价见商务协议。

---

## 11. 遗留事项

| 序号 | 事项 | 责任方 | 说明 |
|---|---|---|---|
| 1 | 测试 / 生产环境地址与 API Key | OpenBox | 开发完成后提供 |
| 2 | 各档位单价 | 双方商务 | 写入协议后在本文档 10 节补充 |
| 3 | 开发周期与联调时间 | OpenBox | 由开发方（麦兔）评估后另行同步 |
| 4 | `low` 档最高 768p | 高德 | 与 1080p 要求不符，请确认是否需要开放该档位；不需要则接口只接受 `high` / `medium` |

---

## 12. 附录 A：错误码

| HTTP | code | 含义 | 处理建议 |
|---|---|---|---|
| 400 | `INVALID_REQUEST` | 参数错误，`message` 说明字段 | 修正参数 |
| 401 | `UNAUTHORIZED` | API Key 缺失或无效 | 检查 Key |
| 402 | `INSUFFICIENT_CREDITS` | 积分不足 | 联系充值 |
| 404 | `NOT_FOUND` | 会话或文件不存在 | 检查 ID |
| 409 | `SESSION_BUSY` | 会话处理中 | 等待 `idle` 或先中止 |
| 409 | `INTERACTION_RESOLVED` | 确认卡已回复或已超时 | 刷新状态 |
| 409 | `DUPLICATE_CLIENT_MESSAGE_ID` | 幂等键重复但内容不同 | 换新的幂等键 |
| 413 | `PAYLOAD_TOO_LARGE` | 文件或文本超限 | 压缩后重试 |
| 415 | `UNSUPPORTED_MEDIA_TYPE` | 素材格式不支持 | 转换格式 |
| 429 | `RATE_LIMITED` | 触发限流 | 按 `Retry-After` 重试 |
| 500 | `INTERNAL_ERROR` | 服务内部错误 | 携带 `request_id` 联系 OpenBox |
| 503 | `LLM_UNAVAILABLE` | 上游模型暂不可用 | 稍后重试 |

---

## 13. 附录 B：事件流（可选）

高德当前采用轮询，本节仅供后续需要更低延迟时参考，不在首期联调范围。

`GET /v1/sessions/{session_id}/events`，`Accept: text/event-stream`，查询参数 `after=<事件 id>` 断线续传。
推送与第 7 节部件对应的事件：`session.status`、`message.delta`、`message.updated`、`question.asked`、`question.resolved`、`progress`、`file.created`、`session.error`。每 20 秒一条 `: ping` 保活。

---

*本文档为对接稿，接口地址、单价与限额以正式协议为准。技术问题请联系 OpenBox 技术对接人。*
