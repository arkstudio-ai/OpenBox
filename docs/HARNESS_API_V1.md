# OpenBox Harness API v1 接口文档

版本：v1 草案 · 日期：2026-09-18 · 状态：接口冻结稿，供开发改造与 SDK 生成对照
设计背景见 [HARNESS_API_PLAN.md](./HARNESS_API_PLAN.md)。

---

## 0. 快速开始

```bash
# 1. 建会话
curl -X POST https://api.openbox.example/v1/sessions \
  -H "Authorization: Bearer obx_sk_xxx" -H "Content-Type: application/json" \
  -d '{"title":"demo"}'

# 2. 发提示（同步等结果）
curl -X POST https://api.openbox.example/v1/sessions/ses_01J.../messages \
  -H "Authorization: Bearer obx_sk_xxx" -H "Content-Type: application/json" \
  -d '{"text":"把 README 翻译成英文","wait":true}'

# 3. 或者订阅事件流
curl -N https://api.openbox.example/v1/sessions/ses_01J.../events \
  -H "Authorization: Bearer obx_sk_xxx"
```

---

## 1. 通用约定

### 1.1 基础
| 项 | 约定 |
|---|---|
| Base URL | `https://<host>/v1` |
| 编码 | 请求 / 响应均为 `application/json; charset=utf-8`，SSE 为 `text/event-stream` |
| 字段命名 | 一律 `snake_case` |
| 时间 | ISO 8601 UTC 字符串，如 `2026-09-18T08:00:00.000Z` |
| ID | 带前缀的可排序字符串：`ses_` 会话、`msg_` 消息、`prt_` 部件、`qst_` 追问、`prm_` 权限、`fil_` 文件、`key_` API Key |
| 请求 ID | 每个响应带 `X-Request-Id`，错误体里也回显 |
| 版本 | 路径版本 `/v1`；字段只增不删，删字段或改语义升 `/v2` |

### 1.2 鉴权
```
Authorization: Bearer obx_sk_...
```
- API Key 绑定一个用户和一个 workspace，请求不需要再传 `X-Workspace-Id`。
- Key 带 scopes，缺少 scope 返回 `403 SCOPE_REQUIRED`。
- 同一端点也接受 Web 端的 JWT（便于前端调试），此时需带 `X-Workspace-Id`。

| scope | 覆盖范围 |
|---|---|
| `sessions:read` | 读会话、消息、diff、事件流 |
| `sessions:write` | 建 / 改 / 删会话，发提示，abort，fork，回复追问与权限 |
| `files:read` | 下载文件 |
| `files:write` | 上传文件 |

### 1.3 限流与配额
响应头：`X-RateLimit-Limit`、`X-RateLimit-Remaining`、`X-RateLimit-Reset`（Unix 秒）。
超限返回 `429 RATE_LIMITED` 并带 `Retry-After`。

| 维度 | 默认 | 错误码 |
|---|---|---|
| 请求速率（每 Key） | 60 / 分钟 | `RATE_LIMITED` |
| 并发轮次（每 Key） | 3 | `CONCURRENT_LIMIT_EXCEEDED` |
| 并发轮次（每用户，所有 Key + Web 共用） | 5 | `CONCURRENT_AGENT_QUOTA_EXCEEDED` |
| 会话总数（每用户） | 200 | `SESSION_QUOTA_EXCEEDED` |

### 1.4 分页
列表接口统一游标分页：请求 `?limit=50&cursor=<opaque>`，响应：
```json
{ "data": [ ... ], "next_cursor": "eyJ..." , "has_more": true }
```
`limit` 上限 200。

### 1.5 错误
所有非 2xx 响应体：
```json
{
  "error": {
    "code": "SESSION_BUSY",
    "message": "session ses_01J... is busy; abort it first",
    "request_id": "req_01J...",
    "details": {}
  }
}
```

| HTTP | code | 含义 |
|---|---|---|
| 400 | `INVALID_REQUEST` | 参数不合法，`details.field` 指出字段 |
| 400 | `TOOLS_NOT_SUPPORTED` | OpenAI 壳收到 `tools` 字段 |
| 401 | `UNAUTHORIZED` | Key 缺失、无效、已撤销、已过期 |
| 402 | `INSUFFICIENT_CREDITS` | 余额不足（enforce 计费模式） |
| 403 | `SCOPE_REQUIRED` | Key 缺少 scope，`details.scope` |
| 403 | `SESSION_READ_ONLY` | 会话属于同 workspace 其他人 |
| 404 | `NOT_FOUND` | 资源不存在或不在该 workspace |
| 409 | `SESSION_BUSY` | 会话正在跑一轮 |
| 409 | `INTERACTION_RESOLVED` | 追问 / 权限已被回复或超时 |
| 409 | `DUPLICATE_CLIENT_MESSAGE_ID` | 幂等键冲突且内容不同 |
| 413 | `PAYLOAD_TOO_LARGE` | 文本或文件超限 |
| 422 | `MODEL_UNAVAILABLE` | 模型不在可用列表或未定价 |
| 429 | `RATE_LIMITED` 等 | 见 1.3 |
| 500 | `INTERNAL_ERROR` | |
| 503 | `LLM_UNAVAILABLE` | 上游模型不可用 |

---

## 2. 数据结构

### 2.1 Session
```json
{
  "id": "ses_01J...",
  "title": "demo",
  "agent": "build",
  "model": "claude-sonnet-5",
  "variant": null,
  "status": "idle",
  "project_id": "default",
  "parent_id": null,
  "created_at": "...",
  "updated_at": "...",
  "token_usage": { "input": 0, "output": 0, "cache": 0, "total": 0, "cost": 0.0, "credits": "0", "context": 0 },
  "stats": { "additions": 0, "deletions": 0, "files_changed": 0 },
  "metadata": {}
}
```
- `status`：`idle | busy | retry | error | compacting`
- `agent`：`build | plan | ...`（见 `GET /v1/agents`）
- `variant`：模型的推理强度档，`null` 用模型默认
- `metadata`：调用方自定义，最多 16 个键，值为字符串，最长 512 字节
- 不对外：`user_id`、`workspace_id`、`sandbox_id`、`slug`、`kind`

### 2.2 Message
```json
{
  "id": "msg_01J...",
  "session_id": "ses_01J...",
  "role": "assistant",
  "parts": [ ...Part ],
  "created_at": "...",
  "client_message_id": null,
  "agent": "build",
  "model": "claude-sonnet-5",
  "variant": null,
  "parent_id": "msg_01J...",
  "finish": "stop",
  "tokens": { ...TokenUsage },
  "error": null,
  "format": null,
  "structured": null
}
```
- `role`：`user | assistant`
- `finish`：`stop | tool_calls | length | error | aborted | null`（轮次未结束为 `null`）
- `parent_id`：assistant 消息指向触发它的 user 消息
- `structured`：请求了 `format` 时的 JSON 结果
- `error`：`{ "code": "...", "message": "..." }`

### 2.3 Part
所有 Part 共有 `id`、`type`、`message_id`、`session_id`。按 `type`：

| type | 字段 | 说明 |
|---|---|---|
| `text` | `text`, `channel: commentary\|final\|null`, `synthetic` | 正文；`commentary` 是过程说明，`final` 是最终回答 |
| `reasoning` | `text` | 思考内容（模型支持且未隐藏时） |
| `tool` | `tool`, `status: pending\|running\|completed\|error`, `input`, `output`, `error`, `title`, `call_id`, `duration`, `metadata` | `output` 在事件流里截断到 2000 字符，完整值走 `GET messages` |
| `step-start` | `step` | 一次 LLM 调用开始 |
| `step-finish` | `step`, `input_tokens`, `output_tokens`, `cost`, `credits`, `duration` | 一次 LLM 调用结束 |
| `file` | `path`, `mime_type`, `size`, `file_id`, `url`, `relation` | `url` 为带签名的临时下载地址；`relation.role: input\|evidence\|intermediate\|result\|final` |
| `patch` | `files[{path, additions, deletions, status}]` | 本步改动摘要 |
| `plan` | `status: writing\|ready\|accepted\|rejected`, `content` | 计划模式产物 |
| `todo` | `items[TodoItem]` | 任务清单快照 |
| `subtask` | `agent`, `description`, `status`, `output` | 子 agent |
| `compaction` | `summary`, `auto` | 上下文压缩记录 |
| `retry` | `attempt`, `reason` | 上游重试 |
| `agent` | `agent` | agent 切换 |
| `skill_job` | `operation`, `status`, `summary`, `artifacts[]` | 技能作业（如视频生产） |

TodoItem：`{ id, subject, description, status: pending|in_progress|completed|cancelled, priority: high|medium|low, source: model|user, started_at }`

### 2.4 TokenUsage
`{ input, output, cache, total, limit, cost, credits, context }`；`cost` 为美元浮点，`credits` 为积分精确字符串（账本为准）。

### 2.5 InteractionPolicy（无人值守策略）
```json
{
  "permission": "auto_allow",
  "question": "auto_reject",
  "timeout_s": 120,
  "allowed_tools": ["bash", "read", "write", "edit", "glob", "grep", "webfetch"]
}
```
| 字段 | 取值 | 说明 |
|---|---|---|
| `permission` | `auto_allow \| auto_deny \| ask` | `auto_allow` 仅对 `allowed_tools` 生效，其余工具按 `ask` |
| `question` | `auto_reject \| ask` | 追问是否等待客户端 |
| `timeout_s` | 5–600 | `ask` 模式下等待上限，超时按 `auto_*` 处理并继续 |
| `allowed_tools` | 工具名数组 | 发布类、支付类工具永远不可加入 |

优先级：请求体 `interaction` > Key 的默认策略 > 系统默认（`auto_allow / auto_reject / 120`）。

---

## 3. 会话 Sessions

### `POST /v1/sessions`　创建会话　`sessions:write`
请求：
```json
{
  "title": "demo",
  "agent": "build",
  "model": "claude-sonnet-5",
  "variant": null,
  "project_id": "default",
  "metadata": { "order_id": "123" }
}
```
全部可选。响应 `201 Session`。

### `GET /v1/sessions`　列表　`sessions:read`
查询：`limit`, `cursor`, `project_id`, `status`, `metadata[key]=value`（精确匹配，最多 3 个）。
响应：分页 `Session[]`。只返回该 Key 所属用户创建的会话。

### `GET /v1/sessions/{session_id}`　详情　`sessions:read`
响应 `Session`。

### `PATCH /v1/sessions/{session_id}`　修改　`sessions:write`
请求：`{ title?, agent?, model?, variant?, metadata? }`（`metadata` 整体替换）。响应 `Session`。

### `DELETE /v1/sessions/{session_id}`　删除　`sessions:write`
正在跑的轮次先被 abort。响应 `204`。

### `POST /v1/sessions/{session_id}/abort`　中止当前轮　`sessions:write`
响应 `{ "ok": true, "aborted": true|false }`。`aborted=false` 表示当时并无轮次在跑。

### `POST /v1/sessions/{session_id}/fork`　分叉　`sessions:write`
请求 `{ "message_id": "msg_..." }`（可选，缺省从最后一条分叉）。响应 `201 Session`（新会话，`parent_id` 指向源会话）。

### `GET /v1/sessions/{session_id}/diff`　文件改动　`sessions:read`
查询 `full=true` 返回 hunks。响应：
```json
{ "data": [ { "path": "README.md", "additions": 3, "deletions": 1, "status": "modified", "hunks": [] } ] }
```

---

## 4. 消息 Messages

### `GET /v1/sessions/{session_id}/messages`　历史　`sessions:read`
查询 `limit`, `cursor`, `order=asc|desc`（默认 `asc`）。响应分页 `Message[]`，`parts` 完整（tool `output` 不截断）。

### `GET /v1/sessions/{session_id}/messages/{message_id}`　单条　`sessions:read`
响应 `Message`。轮询同步结果时用它：`finish != null` 即完成。

### `POST /v1/sessions/{session_id}/messages`　发提示　`sessions:write`
请求：
```json
{
  "text": "把 README 翻译成英文",
  "attachments": ["fil_01J..."],
  "model": "claude-sonnet-5",
  "agent": "build",
  "variant": null,
  "format": { "type": "json_schema", "schema": { "type": "object", "properties": { "summary": { "type": "string" } }, "required": ["summary"] } },
  "client_message_id": "my-idempotency-key",
  "interaction": { "permission": "ask", "timeout_s": 60 },
  "wait": false,
  "stream": false
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `text` | 是 | 最长 200 KB |
| `attachments` | 否 | `POST /v1/files` 返回的 `file_id` 列表，最多 20 个 |
| `model` / `agent` / `variant` | 否 | 本轮覆盖并记住到会话 |
| `format` | 否 | 结构化输出，结果在 assistant 消息的 `structured` |
| `client_message_id` | 否 | 幂等键，同键重发返回原消息、不重跑；不能以 `sjr:` 开头 |
| `interaction` | 否 | 见 2.5 |
| `wait` | 否 | `true` 则阻塞至轮次结束 |
| `stream` | 否 | `true` 则响应为本轮 SSE（见 5.3）；与 `wait` 互斥 |

三种响应：

**异步（默认）** `202`
```json
{ "session_id": "ses_...", "user_message_id": "msg_...", "assistant_message_id": "msg_..." }
```
`assistant_message_id` 是预分配的，可立即用于 `GET messages/{id}` 轮询。

**同步 `wait=true`** `200 Message`（assistant）。服务端等待上限 300 秒；超时返回 `202` 与异步相同的体并带头 `X-Wait-Timeout: true`，客户端转轮询或事件流。

**流式 `stream=true`** `200 text/event-stream`，事件格式见第 5 节，本轮结束（`session.status=idle|error`）后服务端关闭连接。

冲突规则：会话 `status` 为 `busy|retry|compacting` 时返回 `409 SESSION_BUSY`，不抢占。

### `DELETE /v1/sessions/{session_id}/messages/{message_id}`　撤回失败轮　`sessions:write`
仅允许删除 `error` 状态的 assistant 消息及其 user 消息。响应 `204`。

---

## 5. 事件流 Events（SSE）

### 5.1 端点
| 路径 | scope | 说明 |
|---|---|---|
| `GET /v1/sessions/{session_id}/events` | `sessions:read` | 单会话 |
| `GET /v1/events` | `sessions:read` | 该 Key 所属用户的全部会话 |

查询：`after=<event_id>` 断线续传（回放最近 500 条 / 1 小时内的事件）；`types=part.delta,tool.completed` 只订阅指定类型。

### 5.2 帧格式
```
id: 1042
event: part.delta
data: {"session_id":"ses_...","message_id":"msg_...","part_id":"prt_...","delta":"Hello"}

: ping
```
- `id` 单调递增（每会话独立计数），用于 `after`
- 每 20 秒一条 `: ping` 注释帧保活
- `data` 内一律带 `session_id`；全局流还多带 `event_ts`

### 5.3 事件表

| event | data 字段 | 触发时机 |
|---|---|---|
| `session.status` | `status`, `attempt?`, `max_attempts?` | 会话状态变化；`retry` 时带尝试次数 |
| `session.updated` | `session`（Session 部分字段） | 标题、token_usage、agent 变化 |
| `session.error` | `error{code,message}` | 轮次失败。`code` 含 `INSUFFICIENT_CREDITS`、`LLM_UNAVAILABLE`、`ABORTED`、`INTERNAL_ERROR` |
| `session.compaction` | `phase: start\|complete` | 上下文压缩 |
| `message.created` | `message`（含初始 parts） | user 消息入库、assistant 消息预创建 |
| `message.updated` | `message{id,role,finish,tokens,error,structured}` | 轮次结束或出错 |
| `message.delta` | `message_id`, `part_id`, `text` | 正文文本增量 |
| `part.created` | `message_id`, `part` | 新部件 |
| `part.updated` | `message_id`, `part` | 部件更新（tool 部件 `output` 截断至 2000 字符） |
| `part.delta` | `message_id`, `part_id`, `delta` | reasoning 文本增量或 tool 参数增量 |
| `tool.started` | `part_id`, `tool`, `input` | 工具开始执行 |
| `tool.completed` | `part_id`, `tool`, `title`, `output`（截断）, `duration` | 工具成功 |
| `tool.error` | `part_id`, `tool`, `error` | 工具失败 |
| `question.asked` | `id`, `questions[]`, `expires_at` | agent 追问（见 6.1） |
| `question.resolved` | `id`, `resolution: answered\|rejected\|timeout` | 追问结束 |
| `permission.asked` | `id`, `tool`, `input`, `patterns[]`, `expires_at` | 权限请求（见 6.2） |
| `permission.resolved` | `id`, `resolution: once\|always\|reject\|timeout\|auto_allow\|auto_deny` | 权限结束 |
| `todo.updated` | `items[]` | 任务清单变化 |

一轮的典型顺序：
```
message.created(user) → message.created(assistant) → session.status(busy)
→ part.created(step-start) → part.created(text) → message.delta × N
→ part.created(tool) → tool.started → part.updated × N → tool.completed
→ part.created(step-finish) → ... → message.updated(finish=stop) → session.status(idle)
```

---

## 6. 交互 Interactions

`interaction.question=ask` 或 `permission=ask` 时，agent 会发事件并等待，超时后按策略自动处理。

### 6.1 追问 Question
事件 `question.asked`：
```json
{
  "id": "qst_01J...",
  "session_id": "ses_...",
  "questions": [
    {
      "question": "发布到哪个账号？",
      "header": "账号",
      "options": [ { "label": "主号", "description": "" }, { "label": "小号", "description": "" } ],
      "multiple": false,
      "custom": true
    }
  ],
  "expires_at": "..."
}
```

`GET /v1/sessions/{session_id}/questions`　待回复列表　`sessions:read`

`POST /v1/sessions/{session_id}/questions/{question_id}`　回复　`sessions:write`
```json
{ "answers": [ ["主号"] ] }
```
`answers` 与 `questions` 一一对应，每项为选中的 `label` 数组；`custom=true` 时可填自由文本。响应 `{ "ok": true }`。

`POST /v1/sessions/{session_id}/questions/{question_id}/reject`　拒绝　`sessions:write`
agent 收到"用户拒绝回答"并自行决定。响应 `{ "ok": true }`。

已处理或超时的追问返回 `409 INTERACTION_RESOLVED`。

### 6.2 权限 Permission
事件 `permission.asked`：
```json
{
  "id": "prm_01J...",
  "session_id": "ses_...",
  "tool": "bash",
  "input": { "command": "rm -rf build" },
  "patterns": ["bash:rm *"],
  "expires_at": "..."
}
```

`GET /v1/sessions/{session_id}/permissions`　待回复列表

`POST /v1/sessions/{session_id}/permissions/{permission_id}`　回复
```json
{ "action": "once", "message": "" }
```
`action`：`once` 本次放行、`always` 本 Key 后续同 pattern 自动放行、`reject` 拒绝（`message` 会作为拒绝原因回给 agent）。响应 `{ "ok": true }`。

---

## 7. 文件 Files

### `POST /v1/files`　上传　`files:write`
`multipart/form-data`，字段 `file`（≤ 50 MB）、可选 `purpose=attachment`。响应：
```json
{ "id": "fil_01J...", "filename": "brief.pdf", "mime_type": "application/pdf", "size": 10240, "created_at": "..." }
```

### `GET /v1/files/{file_id}`　元数据　`files:read`

### `GET /v1/files/{file_id}/content`　下载　`files:read`
`302` 到带签名的临时 URL（有效 10 分钟）；也可用消息里 `file` 部件的 `url` 直接下载。

---

## 8. 元数据 Meta

### `GET /v1/models`
```json
{ "data": [ { "id": "claude-sonnet-5", "name": "Claude Sonnet 5", "variants": ["low","medium","high"], "default_variant": null, "pricing_tier": "standard", "context_window": 200000 } ] }
```

### `GET /v1/agents`
```json
{ "data": [ { "id": "build", "name": "Build", "description": "..." }, { "id": "plan", "name": "Plan", "description": "..." } ] }
```

### `GET /v1/me`
```json
{
  "key": { "id": "key_...", "name": "ci-bot", "scopes": ["sessions:read","sessions:write"], "created_at": "..." },
  "workspace": { "id": "ws_...", "name": "..." },
  "user": { "id": "usr_...", "username": "..." },
  "billing": { "mode": "enforce", "balance_credits": "1234.50", "monthly_limit_usd": 50.0 },
  "limits": { "rate_per_minute": 60, "concurrent_turns": 3 },
  "default_interaction": { ...InteractionPolicy }
}
```

---

## 9. API Key 管理（Web 登录态，非 `/v1`）

供设置页使用，走现有 JWT + `X-Workspace-Id`。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/api/keys` | 列表：`{ id, name, key_prefix, scopes, default_interaction, rate_limit, last_used_at, expires_at, created_at }` |
| `POST` | `/api/keys` | `{ name, scopes[], default_interaction?, expires_in_days? }` → 同上并**仅此一次**返回 `key` 明文 |
| `PATCH` | `/api/keys/{id}` | 改 `name`、`scopes`、`default_interaction` |
| `DELETE` | `/api/keys/{id}` | 撤销，立即生效 |
| `GET` | `/api/keys/{id}/usage?from&to` | 按天的 `{ date, turns, tokens, credits }` |

---

## 10. OpenAI 兼容层

目标：让只会说 OpenAI 协议的客户端直接接入。是第 3–6 节的薄壳，能力子集。

### `GET /v1/models`（同 8，额外兼容 OpenAI 形状）
带 `object: "list"`，每项 `{ id, object: "model", owned_by: "openbox" }`。`id` 形如 `build`、`build/claude-sonnet-5`、`plan/claude-opus-5`。

### `POST /v1/chat/completions`
```json
{
  "model": "build/claude-sonnet-5",
  "messages": [
    { "role": "system", "content": "只用中文回答" },
    { "role": "user", "content": "把 README 翻译成英文" }
  ],
  "stream": true,
  "stream_options": { "include_usage": true },
  "response_format": { "type": "json_schema", "json_schema": { "name": "out", "schema": { ... } } },
  "metadata": { "session_id": "ses_..." , "interaction": { "permission": "auto_allow" } },
  "user": "end-user-42"
}
```

映射规则：

| OpenAI 字段 | 行为 |
|---|---|
| `model` | `<agent>[/<model>]`；只给 agent 则用会话 / 默认模型 |
| `messages` | 最后一条 `user` 作为本轮 `text`（`content` 为数组时取 `text` 项拼接，`image_url` 转为附件）；`system` 作为本轮附加指令；更早的 `user/assistant` 只在**新建会话**时作为历史上下文写入，不会重跑 |
| 会话延续 | `metadata.session_id` 或请求头 `X-Session-Id`；缺省新建临时会话（24 小时后回收）。响应头总是回 `X-Session-Id` |
| `stream` | `false` 返回 `chat.completion`；`true` 返回 `chat.completion.chunk` 流，末尾 `data: [DONE]` |
| `response_format` | `json_object` → 无 schema 的结构化输出；`json_schema` → `format`。结果放在 `message.content`（JSON 字符串） |
| `tools` / `functions` / `tool_choice` | `400 TOOLS_NOT_SUPPORTED` |
| `temperature` / `top_p` / `max_tokens` 等 | 忽略，响应头 `X-OpenBox-Ignored: temperature,top_p` |
| `n` | 只支持 1 |
| `user` | 写入会话 `metadata.end_user` |
| 交互 | 按 `metadata.interaction` > Key 默认；`ask` 模式下超时即按 `auto_*` 处理 |

非流式响应：
```json
{
  "id": "msg_01J...",
  "object": "chat.completion",
  "created": 1758182400,
  "model": "build/claude-sonnet-5",
  "choices": [ { "index": 0, "message": { "role": "assistant", "content": "..." }, "finish_reason": "stop" } ],
  "usage": { "prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500 },
  "openbox": {
    "session_id": "ses_...",
    "message_id": "msg_...",
    "credits": "12.5",
    "files": [ { "id": "fil_...", "path": "README.en.md", "url": "..." } ],
    "pending": []
  }
}
```
- `content` 只包含 `channel=final` 的 text 部件；过程说明与工具输出不进 `content`
- `finish_reason`：`stop | length | content_filter`；轮次出错时为 `stop` 且 `openbox.error` 带 `{code,message}`
- `openbox.pending`：`ask` 模式超时仍未决的追问 / 权限 id，客户端可用第 6 节接口补答后再发下一轮

流式响应：每个 `message.delta`（final 通道）转为一个 chunk：
```
data: {"id":"msg_...","object":"chat.completion.chunk","created":1758182400,"model":"build/claude-sonnet-5","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}

data: {"id":"msg_...","object":"chat.completion.chunk","created":1758182400,"model":"build/claude-sonnet-5","choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}

data: {"id":"msg_...","object":"chat.completion.chunk","created":1758182400,"model":"build/claude-sonnet-5","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{...}}

data: [DONE]
```
可选：请求头 `X-OpenBox-Events: true` 时，工具事件以扩展 chunk 附带：`{"choices":[],"openbox":{"event":"tool.completed","data":{...}}}`，标准客户端会忽略。

错误：与 OpenAI 一致的 `{ "error": { "message", "type", "code" } }`，`type` 取 `invalid_request_error | authentication_error | permission_error | rate_limit_error | insufficient_quota | server_error`，`code` 用 1.5 的错误码。

### `POST /v1/responses`（预留）
v1 不实现。实现时 `previous_response_id` ↔ `session_id`，流式事件用 `response.output_text.delta` 等标准名。

---

## 11. 端点总览

| 方法 | 路径 | scope |
|---|---|---|
| POST | `/v1/sessions` | sessions:write |
| GET | `/v1/sessions` | sessions:read |
| GET | `/v1/sessions/{id}` | sessions:read |
| PATCH | `/v1/sessions/{id}` | sessions:write |
| DELETE | `/v1/sessions/{id}` | sessions:write |
| POST | `/v1/sessions/{id}/abort` | sessions:write |
| POST | `/v1/sessions/{id}/fork` | sessions:write |
| GET | `/v1/sessions/{id}/diff` | sessions:read |
| GET | `/v1/sessions/{id}/messages` | sessions:read |
| GET | `/v1/sessions/{id}/messages/{mid}` | sessions:read |
| POST | `/v1/sessions/{id}/messages` | sessions:write |
| DELETE | `/v1/sessions/{id}/messages/{mid}` | sessions:write |
| GET | `/v1/sessions/{id}/events` | sessions:read |
| GET | `/v1/events` | sessions:read |
| GET | `/v1/sessions/{id}/questions` | sessions:read |
| POST | `/v1/sessions/{id}/questions/{qid}` | sessions:write |
| POST | `/v1/sessions/{id}/questions/{qid}/reject` | sessions:write |
| GET | `/v1/sessions/{id}/permissions` | sessions:read |
| POST | `/v1/sessions/{id}/permissions/{pid}` | sessions:write |
| POST | `/v1/files` | files:write |
| GET | `/v1/files/{fid}` | files:read |
| GET | `/v1/files/{fid}/content` | files:read |
| GET | `/v1/models` | 任意 |
| GET | `/v1/agents` | 任意 |
| GET | `/v1/me` | 任意 |
| POST | `/v1/chat/completions` | sessions:write |

---

## 12. 与内部接口的对应（开发参考）

| 对外 | 内部现状 | 改造点 |
|---|---|---|
| `/v1/sessions*` | `/api/agent/session*` | 换鉴权、剔除内部字段、游标分页、`metadata` 新增列 |
| `POST messages` | `POST /session/{id}/message`（同步）+ `prompt_async` | 合并为一个入口；忙时 409 而非抢占；预分配 assistant id；402 预检 |
| `stream=true` / `/events` | 无（仅 WS 按用户广播） | `bus.subscribe_all` + session 过滤 + Redis 环形缓冲 |
| 事件名 | `message.text_delta`、`tool.running` 等 | `to_public_event()` 统一改名、去 `userId`、snake_case |
| 交互超时 | 固定 300s 硬等 | 读 `interaction.timeout_s`，超时按策略自动处理 |
| `/v1/files` | `/api/assets` | 换鉴权、Key 级配额 |
| API Key | 无 | 新表 + 中间件分支 + 管理路由 |
| OpenAI 壳 | 无 | 新 router，纯映射层 |
