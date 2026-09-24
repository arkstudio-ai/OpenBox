# OpenBox 视频创作接口（高德接入版 · 最小集）

版本：v1-min · 日期：2026-09-18 · 状态：对接草案
适用能力：对话式视频创作（口播短视频生成）与视频剪辑。
完整规范见 [HARNESS_API_V1.md](./HARNESS_API_V1.md)，本文是其子集。

> **2026-09-21 更新**：高德已确认对接方式（轮询为主、服务端调用、三档 `quality`、视频规格预置），对外正式稿为
> [external/OpenBox-Video-API-Gaode-v1.1.md](./external/OpenBox-Video-API-Gaode-v1.1.md)，与本文冲突处以 v1.1 为准。

---

## 1. 一句话流程

```
上传素材(可选) → 建会话 → 发一句话需求 → 订阅事件 → 收到"确认卡"就回复 → 收到成片文件 → 结束
```

视频创作是**多轮确认**的：agent 会先给出讲稿让用户确认，再给出分镜与报价让用户确认，之后才花钱生成。
所以接入方必须实现"确认卡"的展示与回复（第 5 节），这是最小集里唯一不能省的交互。

---

## 2. 通用约定

| 项 | 约定 |
|---|---|
| Base URL | `https://<host>/v1` |
| 鉴权 | `Authorization: Bearer obx_sk_...`，由 OpenBox 侧签发给高德一把 Key，绑定高德专属 workspace |
| 格式 | JSON，`snake_case`，时间为 ISO 8601 UTC |
| 错误体 | `{"error": {"code": "...", "message": "...", "request_id": "..."}}` |
| 限流 | 60 请求 / 分钟，并发进行中的会话轮次 5；超限 `429` 带 `Retry-After` |

错误码（最小集用到的）：

| HTTP | code | 含义 |
|---|---|---|
| 400 | `INVALID_REQUEST` | 参数错误 |
| 401 | `UNAUTHORIZED` | Key 无效 |
| 402 | `INSUFFICIENT_CREDITS` | 账户积分不足 |
| 404 | `NOT_FOUND` | 会话 / 文件不存在 |
| 409 | `SESSION_BUSY` | 会话正在处理上一条，先 abort 或等 idle |
| 409 | `INTERACTION_RESOLVED` | 确认卡已回复或已超时 |
| 413 | `PAYLOAD_TOO_LARGE` | 文件超 200 MB |
| 429 | `RATE_LIMITED` | 限流 |
| 503 | `LLM_UNAVAILABLE` | 上游暂不可用，可重试 |

---

## 3. 端点总览（8 个）

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/v1/files` | 上传素材（图片 / 视频 / 音频） |
| GET | `/v1/video/models` | 可选视频模型与分辨率 |
| POST | `/v1/sessions` | 建会话 |
| POST | `/v1/sessions/{id}/messages` | 发需求 / 发修改意见 |
| GET | `/v1/sessions/{id}/events` | 事件流（SSE） |
| GET | `/v1/sessions/{id}/messages` | 拉历史（无 SSE 时的轮询兜底） |
| POST | `/v1/sessions/{id}/questions/{qid}` | 回复确认卡 |
| POST | `/v1/sessions/{id}/abort` | 中止 |

不在最小集但已预留、随时可开：`GET /v1/sessions`、`DELETE /v1/sessions/{id}`、`GET /v1/files/{id}/content`、权限确认、OpenAI 兼容接口。

---

## 4. 接口详情

### 4.1 上传素材　`POST /v1/files`
`multipart/form-data`，字段 `file`。支持 `image/*`、`video/mp4`、`video/quicktime`、`audio/*`，单文件 ≤ 200 MB。
```json
{ "id": "fil_01J...", "filename": "clip.mp4", "mime_type": "video/mp4", "size": 10485760, "duration_s": 12.4, "created_at": "..." }
```
文件保留 30 天。

### 4.2 视频模型列表　`GET /v1/video/models`
```json
{
  "data": [
    { "id": "seedance-1.5-pro", "name": "Seedance 1.5 Pro", "resolutions": ["720p", "1080p"], "max_duration_seconds": 15, "tier": "pro" },
    { "id": "wan-3.0", "name": "Wan 3.0", "resolutions": ["720p", "1080p"], "max_duration_seconds": 30, "tier": "standard" }
  ],
  "default": { "model": "seedance-1.5-pro", "resolution": "1080p" }
}
```
建会话时不指定则用 `default`。

### 4.3 建会话　`POST /v1/sessions`
```json
{
  "title": "五一出行攻略口播",
  "video_model": "seedance-1.5-pro",
  "video_resolution": "1080p",
  "metadata": { "gaode_uid": "u_123", "task_id": "t_456" }
}
```
全部可选。响应 `201`：
```json
{
  "id": "ses_01J...",
  "title": "五一出行攻略口播",
  "status": "idle",
  "video_model": "seedance-1.5-pro",
  "video_resolution": "1080p",
  "metadata": { "gaode_uid": "u_123", "task_id": "t_456" },
  "created_at": "..."
}
```
`status`：`idle` 空闲 / `busy` 处理中 / `error` 上轮出错。
建议一次视频任务一个会话，同会话内可多轮修改（"把第二段换个说法"、"字幕改成黄色"）。

### 4.4 发需求　`POST /v1/sessions/{id}/messages`
```json
{
  "text": "帮我做一条 45 秒的五一杭州自驾攻略口播，用我上传的这段路况视频做素材",
  "attachments": ["fil_01J..."],
  "client_message_id": "gaode-t_456-1"
}
```
| 字段 | 必填 | 说明 |
|---|---|---|
| `text` | 是 | 自然语言需求或修改意见，≤ 20 KB |
| `attachments` | 否 | 素材 `file_id` 列表，≤ 20 个 |
| `client_message_id` | 否 | 幂等键，重发不重跑 |

响应 `202`：
```json
{ "session_id": "ses_01J...", "user_message_id": "msg_01J...", "assistant_message_id": "msg_01J..." }
```
会话 `busy` 时返回 `409 SESSION_BUSY`。

### 4.5 事件流　`GET /v1/sessions/{id}/events`
`Accept: text/event-stream`。查询参数 `after=<id>` 断线续传（保留 1 小时内事件）。
```
id: 17
event: message.delta
data: {"session_id":"ses_...","message_id":"msg_...","text":"好的，先给你写讲稿"}

: ping
```
每 20 秒一条 `: ping`。事件表见第 5 节。

### 4.6 拉历史　`GET /v1/sessions/{id}/messages?after=<message_id>`
无法用 SSE 时的兜底：每 2–3 秒轮询一次，返回 `after` 之后的消息（含 `parts`）。轮次结束的判定：assistant 消息 `finish != null`。
待回复的确认卡在 `parts` 里以 `question` 部件出现，`status: pending` 表示还没回。

### 4.7 回复确认卡　`POST /v1/sessions/{id}/questions/{qid}`
```json
{ "answers": [ ["可以"], ["带转场和动效字幕（约 120 积分）"] ] }
```
`answers` 与卡片的 `questions` 一一对应，每项是选中的 `label` 数组；允许自由文本时可以填选项之外的话（如 `["短到约 30 秒"]`）。响应 `{"ok": true}`。
不想继续：`POST /v1/sessions/{id}/questions/{qid}/reject`，agent 会停下并说明。

### 4.8 中止　`POST /v1/sessions/{id}/abort`
响应 `{"ok": true, "aborted": true}`。已提交的付费生成不会退款。

---

## 5. 事件与确认卡

### 5.1 事件表（最小集只需处理这 8 种）

| event | data | 接入方要做的事 |
|---|---|---|
| `session.status` | `status` | `busy` 显示处理中；`idle` 本轮结束；`error` 提示失败 |
| `message.delta` | `message_id`, `text` | 追加显示 agent 的话（流式） |
| `message.updated` | `message{id, finish, error}` | `finish="stop"` 本轮说完；`error` 非空显示错误 |
| `question.asked` | `id`, `questions[]`, `expires_at` | **渲染确认卡并等用户回答** |
| `question.resolved` | `id`, `resolution: answered\|rejected\|timeout` | 收起卡片 |
| `file.created` | `file{id, filename, mime_type, size, url, role}` | `role=final` 就是成片，展示 / 下载 `url` |
| `progress` | `stage`, `detail`, `percent?` | 可选：显示"生成第 2/5 镜"之类进度 |
| `session.error` | `error{code, message}` | 显示错误；`INSUFFICIENT_CREDITS` 需引导充值 |

其他事件（`part.*`、`tool.*`、`todo.updated`）也会发，接入方可忽略。

### 5.2 确认卡格式
```json
{
  "id": "qst_01J...",
  "session_id": "ses_...",
  "questions": [
    {
      "header": "成稿",
      "question": "讲稿如上，可以开始分镜吗？",
      "options": [ { "label": "可以", "description": "" }, { "label": "需要修改", "description": "" } ],
      "multiple": false,
      "custom": true
    },
    {
      "header": "成片风格",
      "question": "选择成片方式",
      "options": [
        { "label": "无特效拼接（免费）", "description": "" },
        { "label": "带转场和动效字幕（约 120 积分）", "description": "预估" }
      ],
      "multiple": false,
      "custom": false
    }
  ],
  "expires_at": "2026-09-18T08:10:00Z"
}
```
- 一张卡最多 4 个问题，推荐答案排第一。
- `custom=true` 时前端要给"自己输入"入口。
- 卡片正文（讲稿、分镜表、报价明细）在卡片之前的 `message.delta` 文本里，是 Markdown。
- 超时（默认 10 分钟）未答视为拒绝，agent 停下；用户可以再发一条消息继续。

一次完整创作通常 2 张卡：**讲稿确认** 和 **分镜 + 报价确认**。转写发现口误或价格变化时会多 1–2 张。剪辑类任务通常 1 张（剪辑方案确认）或 0 张。

### 5.3 成片文件
```json
{
  "file": {
    "id": "fil_01J...",
    "filename": "final_1080p.mp4",
    "mime_type": "video/mp4",
    "size": 24117248,
    "duration_s": 46.2,
    "url": "https://.../signed?...",
    "role": "final"
  }
}
```
`url` 是签名下载地址，有效期 24 小时；过期后用 `GET /v1/files/{id}/content` 重新获取。
`role` 为 `final` 才是成片，`intermediate` 是单镜或中间产物，`input` 是回显的素材。

---

## 6. 完整时序示例

```
高德                                   OpenBox
 │ POST /v1/files (路况.mp4)            │
 │◄── fil_A                            │
 │ POST /v1/sessions                    │
 │◄── ses_1                            │
 │ GET /v1/sessions/ses_1/events (SSE) │
 │ POST /v1/sessions/ses_1/messages     │  text="45秒杭州自驾攻略口播" attachments=[fil_A]
 │◄── 202                              │
 │◄── session.status busy              │
 │◄── message.delta ×N   (讲稿正文)     │
 │◄── question.asked qst_1 (成稿/风格)  │
 │    …用户在高德端选择…                 │
 │ POST .../questions/qst_1 {answers}   │
 │◄── question.resolved                │
 │◄── message.delta ×N   (分镜表+报价)  │
 │◄── question.asked qst_2 (确认生成)   │
 │ POST .../questions/qst_2 {answers}   │
 │◄── progress ×N       (生成/转写/合成)│
 │◄── file.created role=final          │
 │◄── message.updated finish=stop      │
 │◄── session.status idle              │
```
再发一条"第三段语气轻快点"即进入修改轮，流程相同。

---

## 7. 计费与账号
- 高德为一个 workspace，一把 Key，积分由合同预充。余额不足时发消息返回 `402`，进行中的轮次发 `session.error INSUFFICIENT_CREDITS`。
- 每个会话的消耗在 `GET /v1/sessions/{id}` 的 `token_usage.credits` 与成片事件同时给出；对账口径以 OpenBox 账本为准。
- 费用点：文本对话（很少）、每镜视频生成（主要）、云端合成（选"带转场"时）。报价在第二张卡上明示，用户确认前不产生生成费用。

---

## 8. 对接前需高德确认
1. 是否能消费 SSE。不能就用 4.6 轮询，功能等价，延迟 2–3 秒。
2. 确认卡由高德端渲染（推荐）还是要 OpenBox 提供 H5 嵌入页。
3. 成片是拉取 `url` 自行存储，还是需要 OpenBox 直传高德 OSS（需要额外接口）。
4. `metadata` 里放什么用于对账（建议 `gaode_uid` + `task_id`）。
