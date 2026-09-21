# OpenBox Harness 对外开放接口计划

日期：2026-09-18　状态：草案，待拍板

## 1. 目标与结论

把 OpenBox 的 agent harness（会话 → 提示 → 工具循环 → 流式事件）开放给外部程序调用，
让第三方 / 自家其他产品能用一把 API Key 驱动 agent，而不必走 Web 前端的 JWT + WebSocket。

**结论：两层并存，先做原生层。**

| 层 | 风格 | 定位 | 优先级 |
|---|---|---|---|
| 原生 Harness API `/v1/...` | opencode 风格：session + message + SSE 事件 | 能力全集，SDK 主体 | M1 |
| 兼容适配层 `/v1/chat/completions` | OpenAI 风格 | 现有生态客户端零改动接入 | M2 |

理由：
- 现有内部模型（`Session` / `MessageWithParts` / `MessagePart` / 事件名）就是 opencode 派生的，原生层几乎是把 `/api/agent/*` 做一次"稳定化 + 换鉴权 + 换传输"。
- OpenAI 格式装不下 harness 的核心：工具在服务端自己跑、会追问、会要权限、会挂载沙箱 / 云电脑。它只适合做"把 OpenBox 当作一个会干活的模型"的薄壳。
- 不建议对外直接暴露现有 `/api/agent/*`：它绑定 15 分钟 JWT、WS 一次性 ticket、按用户全量广播事件，这三点都不适合机器客户端。

## 2. 现状（对设计有约束的部分）

| 项 | 现状 | 对开放 API 的影响 |
|---|---|---|
| 鉴权 | 只有 15 分钟 access JWT + refresh cookie；无 API Key / PAT | 必须新增 API Key |
| 传输 | 仅 WS `/ws/agent?ticket=`，无 SSE；事件按 userId 投递，不按 session | 需新增 SSE，并按 session 过滤 |
| 事件 | `session.status/error`, `message.created/updated/text_delta`, `part.created/updated/delta`, `tool.running/completed/error`, `permission.asked`, `question.asked`, `todo.updated` 等 | 可直接映射，需剔除内部字段（`canonical_tool_id` 等已 `exclude`）|
| 追问 / 权限 | 工具内阻塞等待回复，Redis TTL 300s；无人回答则整轮挂死 | 机器客户端必须有默认策略 + 超时 |
| 结构化输出 | `PromptBody.format = {type: json_schema, schema}`，结果在 `message.structured` | 可直接映射 OpenAI `response_format` |
| 计费 | `UsageMeter.start` 在每次 LLM 调用前拦截，`BillingError` 只以 `session.error` 事件冒出，不映射 HTTP 状态 | 需在提示入口预检并返回 402 |
| 限流 | 只有登录限流；`rate_limit_api="60/minute"` 配置项无消费者 | 需要按 Key 限流 |
| 同步发送 | `POST /session/{id}/message` 会抢占正在跑的轮次 | 对外要改为 409 而非静默抢占 |

## 3. 鉴权与租户

### 3.1 API Key
- 形态：`obx_sk_<32 bytes urlsafe>`，仅创建时明文返回一次；库里存 SHA-256 哈希 + 前缀（便于列表展示）。
- 归属：绑定 `user_id + workspace_id`，继承该用户在 workspace 的角色（对外只开放 member 级能力）。
- 字段：`id, user_id, workspace_id, name, key_prefix, key_hash, scopes[], policy(json), rate_limit, last_used_at, expires_at, revoked_at`。
- scopes（第一版够用即可）：`sessions:read` `sessions:write` `events:read` `files:read` `files:write`。
- 管理接口：`GET/POST /api/keys`、`DELETE /api/keys/{id}`（走现有 JWT，在设置页做 UI）。
- 认证接入：扩展 `auth/middleware.py:get_current_user`，Bearer 值以 `obx_sk_` 开头时走 Key 校验，否则走原 JWT 分支。结果字典多带 `auth_kind="api_key"`、`api_key_id`、`policy`，下游路由无需改动。

### 3.2 限流与配额
- 按 Key 的 Redis 滑动窗口（复用 `rate_limit_api`），超限返回 429 + `Retry-After`。
- 并发轮次沿用 `auth/quota.check_concurrent_agents`（按用户），另加按 Key 的上限，避免一把泄露的 Key 吃满用户额度。
- 每个响应带 `X-RateLimit-Limit / Remaining / Reset`。

### 3.3 计费
- 提示入口预检：`billing_mode()==enforce` 且余额 `<= 0` 时直接返回 `402 {"error":{"code":"INSUFFICIENT_CREDITS"}}`，不再创建消息。
- 轮次中途的 `BillingError` 仍以事件冒出，但事件里带 `code`，并让 session 状态进入 `error`。
- Key 级用量统计：ledger 记录里追加 `api_key_id`，方便按 Key 出账单和做用量页。

## 4. 原生 Harness API（opencode 风格）

前缀 `/v1`，独立 router 文件放在 `backend/api/v1/`，只挂 API Key 认证（JWT 也可用，方便前端调试）。
错误体统一：`{"error": {"code": "...", "message": "...", "request_id": "..."}}`。

### 4.1 会话
| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/v1/sessions` | `{model?, agent?, variant?, title?, project_id?, metadata?}` → Session |
| `GET` | `/v1/sessions?limit&cursor&project_id` | 分页列表 |
| `GET` | `/v1/sessions/{id}` | |
| `PATCH` | `/v1/sessions/{id}` | title / model / agent |
| `DELETE` | `/v1/sessions/{id}` | |
| `GET` | `/v1/sessions/{id}/messages?limit&cursor` | `MessageWithParts[]` |
| `POST` | `/v1/sessions/{id}/messages` | 发提示，见 4.2 |
| `POST` | `/v1/sessions/{id}/abort` | |
| `POST` | `/v1/sessions/{id}/fork` | |
| `GET` | `/v1/sessions/{id}/diff` | |

### 4.2 发提示（一个入口，三种模式）
请求体 = 现有 `PromptBody` 精简：`{text, attachments?, model?, agent?, format?, client_message_id?, stream?, wait?}`。

| 模式 | 参数 | 行为 |
|---|---|---|
| 异步 | 默认 | `202 {message_id, session_id}`，事件走 4.3 |
| 同步 | `wait=true` | 阻塞到轮次结束，返回最终 assistant `MessageWithParts`（含 `structured`）；服务端上限 `harness_sync_timeout`（默认 300s），超时返回 202 让客户端转轮询 |
| 流式 | `stream=true` | 响应体就是本轮的 SSE，只含该 session、该轮的事件，`session.idle` 后关闭 |

规则：
- 会话正忙时返回 `409 SESSION_BUSY`，不抢占；调用方想抢先 `abort`。
- `attachments` 为 `file_asset` id，上传走 `POST /v1/files`（复用现有 assets 路由的多部分上传）。
- 幂等：`client_message_id` 重复则返回已有消息，不重跑。

### 4.3 事件流（SSE）
| 路径 | 说明 |
|---|---|
| `GET /v1/sessions/{id}/events?after=<seq>` | 单会话事件流，支持断线续传 |
| `GET /v1/events` | 该 Key 名下所有会话（对应 opencode `GET /event`） |

实现：`bus.subscribe_all` + 按 `userId`、`sessionId` 过滤 → `text/event-stream`。
每条事件 `id: <seq>`、`event: <type>`、`data: <json>`；服务端每 20s 发 `: ping`。
续传：每个 session 在 Redis 保留最近 N 条（默认 500，TTL 1h）的环形缓冲，`after` 命中就先回放再接实时。

对外事件名（v1 冻结，内部名 → 外部名）：

| 外部事件 | 内部来源 | 关键字段 |
|---|---|---|
| `session.status` | 同名 | `status: idle\|busy\|retry\|error\|compacting` |
| `session.error` | 同名 | `error.code, error.message` |
| `message.created` / `message.updated` | 同名 | `message` |
| `message.delta` | `message.text_delta` | `message_id, part_id, text` |
| `part.created` / `part.updated` / `part.delta` | 同名 | `message_id, part` |
| `tool.started` / `tool.completed` / `tool.error` | `tool.running` 等 | `part_id, tool, input / output(截断) / error` |
| `question.asked` / `question.resolved` | `question.asked` / `replied` / `rejected` | `id, questions[]` |
| `permission.asked` / `permission.resolved` | `permission.asked` / `replied` | `id, tool, input, patterns` |
| `todo.updated` | 同名 | `items` |

不对外：`toast`、`build.*`、`cron.*`、`mcp.tools_changed`、`server.*`、`devbrowser.*`。
序列化统一走一个 `to_public_event()`，剔除 `userId`，字段名统一 `snake_case`。

### 4.4 追问与权限（无人值守是关键）
Key 的 `policy` 决定默认行为，请求体可按轮覆盖：

```json
{
  "permission": "auto_allow | auto_deny | ask",
  "question":   "auto_reject | ask",
  "interaction_timeout_s": 120
}
```

- `ask`：照常发 `question.asked` / `permission.asked` 事件，客户端用
  `POST /v1/sessions/{id}/questions/{qid}` `{answers}` 与
  `POST /v1/sessions/{id}/permissions/{pid}` `{action: once|always|reject}` 回复。
- 超时：把现有的 300s 硬等待改成读 `interaction_timeout_s`，超时按 `auto_*` 处理并发 `*.resolved {reason:"timeout"}`，
  轮次继续而不是挂死。这一改动同时惠及 Web 端。
- `auto_allow` 只对 `policy.allowed_tools` 白名单生效（默认沙箱内工具），涉及云电脑发布、支付类工具永远走 `ask` 或拒绝。

### 4.5 结构化输出
沿用 `format: {type: "json_schema", schema}`，最终消息 `structured` 字段即结果；流式时额外发 `message.structured` 事件。

### 4.6 元数据
`GET /v1/models`（可用模型 + 定价档位）、`GET /v1/agents`、`GET /v1/me`（Key 归属、workspace、余额、限额）。

## 5. OpenAI 兼容层

目的是让 LangChain / Dify / Cursor 之类"只会说 OpenAI"的客户端能直接接。只做**薄壳**，映射到第 4 节。

### 5.1 `POST /v1/chat/completions`
| OpenAI 字段 | 映射 |
|---|---|
| `model` | `agent[/model]`，如 `build`、`build/claude-sonnet-5`；`GET /v1/models` 列出组合 |
| `messages` | 最后一条 `user` 作为本轮 `text`；`system` 前置为附加指令；历史 `assistant/user` 仅在新建会话时回灌为上下文（不重跑） |
| 会话延续 | 请求头 `X-Session-Id` 或 `metadata.session_id`；缺省则每次新建临时会话（TTL 回收）；响应头回 `X-Session-Id` |
| `stream` | `chat.completion.chunk`，`delta.content` 来自 `message.delta`；工具执行以 `delta.content` 之外的扩展字段 `openbox.event` 附带（可关） |
| `response_format: json_schema` | `format` |
| `tools` / `tool_choice` | 不支持，返回 400 `TOOLS_NOT_SUPPORTED`（工具由 harness 自己跑） |
| `usage` | 来自 `TokenUsage`，`stream_options.include_usage` 支持 |
| 追问 / 权限 | 强制按 Key policy 自动处理；`ask` 模式下超时即拒绝，`finish_reason="stop"` 并在 `openbox.pending` 里带未决项 |

### 5.2 `POST /v1/responses`（可选，M2 后半）
Responses API 是有状态的，`previous_response_id` 天然对应 session，`response.output_text.delta` 等语义事件比 chat.completion.chunk 更贴 harness。
若生态客户端需求出现再做，内部复用同一映射层。

## 6. SDK 与文档
- `/v1` 单独一份 OpenAPI（FastAPI `include_in_schema` 分组 + tag），发布到 `docs.openbox.*/api`。
- 用 openapi 生成 TS（`@hey-api/openapi-ts`，与 opencode 同路线）和 Python（`openapi-python-client`）SDK；先只发 TS。
- SDK 里手写三个便利方法：`session.prompt()`（同步）、`session.stream()`（async iterator）、`session.answer()`（回复追问 / 权限）。
- 示例：Node 20 行、Python 20 行、`curl` 三条（建会话 / 发提示 / 拉事件）。

## 7. 里程碑

| 里程碑 | 内容 | 关键文件 | 预估 |
|---|---|---|---|
| M0 冻结 | 本文档定稿、事件 v1 字段表、错误码表 | `docs/HARNESS_API_PLAN.md` | 1 天 |
| M1 原生 API | API Key 模型 + 迁移 + 管理路由；中间件接 Key；`/v1/sessions*`；SSE 事件 + 续传缓冲；追问 / 权限策略与超时；402 预检；限流 | `auth/api_key.py`(新) `auth/middleware.py` `db/models/api_key.py`(新) `alembic/` `api/v1/{sessions,events,keys}.py`(新) `question/question.py` `permission/permission.py` `billing/service.py` `core/config.py` `main.py` | 5–7 天 |
| M1.5 前端 | 设置页"API Keys"（创建 / 撤销 / 用量）| `frontend-v2/src/features/settings/` | 2 天 |
| M2 OpenAI 壳 | `/v1/chat/completions` `/v1/models`；临时会话回收；集成测试用官方 `openai` SDK 跑通 | `api/v1/openai.py`(新) `api/v1/mapping.py`(新) | 3–4 天 |
| M3 发布 | OpenAPI 分组、TS SDK、文档站、示例；`/v1/responses` 视需求 | `docs/`、`sdk/`(新) | 3 天 |

M1 完成即可给内部产品和首批合作方试用；M2 之后才对外宣传"OpenAI 兼容"。

## 8. 需要拍板的点
1. Key 归属粒度：按 user+workspace（本文建议）还是按 workspace 共享 Key。
2. 无人值守默认策略：建议 `permission=auto_allow(白名单) / question=auto_reject / 120s`；发布类工具永远 `ask`。
3. 临时会话（OpenAI 壳无 `X-Session-Id` 时）保留多久：建议 24h 后由 cron 清理。
4. 是否公开 `tool.*` 事件里的 `input/output`（含沙箱路径），还是只给 `title` 与状态。
5. 定价：API 调用是否与 Web 同价，是否给 Key 单独设月度上限（`monthly_cost_limit` 已有按用户的）。

## 9. 明确不做（v1）
- 不做 MCP Server 形态（OpenBox 目前只是 MCP 客户端，后续可再评估）。
- 不开放 admin / fleet / desktop 管理接口。
- 不支持外部客户端注入自定义 tools（OpenAI `tools` 字段）。
- 不做 WebSocket 对外版本，SSE + HTTP 回复足够。

---

## 10. 高德首期开发计划（2026-09-21 定稿）

范围：仅 [external/OpenBox-Video-API-Gaode-v1.1.md](./external/OpenBox-Video-API-Gaode-v1.1.md) 的 9 个端点 + 轮询 + 三档 `quality` + 确认卡。
SSE、OpenAI 壳、设置页 Key 管理、全局事件流不进本期。

### 10.1 任务与分工

| 编号 | 任务 | 负责 | 估时 | 关键改动点 |
|---|---|---|---|---|
| A | API Key 鉴权 | Claude | 1 天 | `db/models/api_key.py`(新) + alembic 迁移；`auth/api_key.py`(新) 哈希校验；`auth/middleware.py:get_current_user` 对 `obx_sk_` 前缀分支，注入 `user_id/workspace_id/auth_kind/api_key_id/policy`；签发脚本 `scripts/issue_api_key.py`（不做 UI） |
| B | `/v1` 路由层 | Claude | 2 天 | `api/v1/{sessions,messages,questions,files}.py`(新)，`api/v1/public.py` 公开序列化（剔除内部字段、snake_case、part 只出 text/question/file）；发消息 202 预分配 assistant id、忙时 409、`client_message_id` 幂等；`GET messages?after=`；`main.py` 挂载 `/v1` |
| E | 计费与限流 | Claude | 0.5 天 | 发消息入口 `billing_mode()==enforce && balance<=0` → 402；按 Key 的 Redis 滑动窗口限流（复用 `rate_limit_api`）与并发上限 |
| C | 交互策略与确认卡部件 | 麦兔 | 1 天 | `question/question.py`：`ask` 时向当前 assistant 消息 `add_part` 一个 `question` 部件（`question_id/status/expires_at/questions`），回复/拒绝/超时时 `update_part` 改 status；等待超时读 Key policy（高德 600s）而非固定 300s；超时按 `auto_reject` 处理并让轮次继续。`permission/permission.py`：Key policy `auto_allow` 对 `allowed_tools` 白名单直接放行 |
| D | 高德预置 | 麦兔 | 1 天 | 高德专属 workspace + Key；`quality` → `model_tiers.video` 映射（high/medium 固定 1080p，low 取档默认）；会话级默认 9:16 / 1080p / 无字幕 / ≤30s 注入（建议做成 workspace 级 `integration_profile` 配置，追加到 system prompt） |
| F | 测试与联调准备 | 双方 | 1 天 | 单测；模拟高德全流程的集成脚本（上传→建会话→发需求→轮询→答卡→取成片）；gw2 测试环境部署；签发测试 Key；文档补环境地址 |
| G | 联调修复 | 双方 | 3 天 | |
| H | 验收上线 | 双方 | 1 天 | 生产签发 Key、灰度 |

### 10.2 首期明确不做（文档需标注"后续补充"）
- `progress` 部件不发（文档已标可选）。
- 文件 `duration_s / width / height` 首期返回 `null`。
- 账本不带 `api_key_id`，按 workspace 对账。

### 10.3 日程

| 日期 | 事项 | 交付 |
|---|---|---|
| 09-21 至 09-23 | Claude 做 A+B+E，麦兔做 C+D，各自独立分支 | 两个 PR |
| 09-24 | 合并、部署 gw2 测试环境、跑集成脚本、签发测试 Key | **高德拿到测试 Key** |
| 09-25 | 中秋，不排工 | |
| 09-28 至 09-30 | 与高德第一轮联调 | 节前清掉主要问题 |
| 10-01 至 10-07 | 国庆，不排工 | |
| 10-08 至 10-09 | 修复、验收、生产签发 Key | **上线** |

### 10.4 约束
- 主工作树被其他会话共用，所有开发在独立 worktree + 分支进行，经 PR 合并，不直接在主工作树提交。
- 对外字段"只增不删"，与 v1.1 文档冲突的实现以文档为准；确需改文档先改文档再改代码。
- 待高德答复：`low` 档 768p 是否开放；未答复前 D 的校验按只接受 `high/medium` 实现，`low` 返回 400。

### 10.5 A / B / E 实施记录（2026-09-21，分支 `feat/harness-api-v1`）

代码位置与约定，供 C / D / F 对接：

| 项 | 实现 |
|---|---|
| 挂载 | `main.py` 以子应用挂 `/v1`（`api/v1/app.py:create_v1_app`），自带错误体 `{"error":{code,message,request_id}}`、`X-Request-Id`、`X-RateLimit-*`；`/v1/docs` 有独立 OpenAPI |
| 鉴权 | `auth/api_key.py`；`auth/middleware.py` 对 `obx_sk_` 前缀走 Key 校验，identity 多带 `auth_kind="api_key"`、`api_key_id`、`workspace_id`、`scopes`、`policy`、`rate_limit`；`auth/workspace.py` 对 Key 忽略 `X-Workspace-Id`。JWT 仍可调 `/v1`（带 `X-Workspace-Id`），便于调试 |
| 表与迁移 | `db/models/api_key.py`（`api_keys`）；`sessions` 新增 `quality` / `metadata` / `api_key_id`；alembic `e7c9a1b3d5f0`（**部署前先跑迁移**，`/health` 就绪检查已包含新表列） |
| 签发 | `uv run python scripts/issue_api_key.py --user <用户名或id> --name "高德测试" [--workspace ws_x] [--expires-days 90] [--policy '{...}'] [--rate-limit 60/minute]`；`--list` / `--revoke key_x`。明文只打印一次 |
| policy 默认 | `permission=auto_allow`、`question=auto_reject`、`interaction_timeout_s=600`、`allowed_tools=[]`、`max_concurrent_sessions=5`（`auth/api_key.py:DEFAULT_POLICY`，C 读前三项） |
| 公开 id | `session_`→`ses_`、`message_`→`msg_`、`part_`→`prt_`、`asset_`→`fil_`、追问裸 ULID→`qst_`（`api/v1/ids.py`）。**assistant 消息 id = 触发它的 user 消息 ULID + 1**：内部每个 LLM step 是一条 assistant 消息，对外按轮折叠成一条，id 因此可在 202 时就给出 |
| 折叠规则 | `api/v1/public.py:public_messages`：真实 user 消息开启一轮，之后所有 assistant step 并入；纯 synthetic 的 user 消息（plan 进入等）不显示；`finish` 取最新 step（`length`→`stop`），无终态 step 时会话仍活跃→`null`、会话 `error`→`error`、否则 `stop`；part 只出 `text`（非 synthetic）/`question`/`file`（有 `asset_id` 且非 transient；`url` 24h 签名） |
| 确认卡桥接 | C 未落地前，`question` 部件由 `tool` 部件的 `metadata.question_id` + `question_checkpoints` 行合成（`status`：pending/answered/rejected/expired→timeout，pending 过期也按 timeout）；C 落地原生 `type=question` 部件后自动优先原生、不重复 |
| 会话状态 | `busy/compacting/retry/queued/waiting_input`→`busy`，`error`→`error`，其余 `idle`；发消息时会话活跃（含等卡）→ `409 SESSION_BUSY`，不抢占 |
| 发消息 | 走 durable inbox（`accept_inbox_item` + `wake_inbox_session`），`client_message_id` 幂等（内容不同→`409 DUPLICATE_CLIENT_MESSAGE_ID`）；文本 > 20 KB→413；附件 ≤ 20 |
| 402 预检 | `billing/service.py:precheck_balance`：仅 `BILLING_MODE=enforce` 生效，与 `UsageMeter.start` 同口径（先记本期额度再判 `<= 0`） |
| 限流 | 每 Key 固定窗口（`config.rate_limit_api` 或 Key 自带 `rate_limit`），超限 `429 RATE_LIMITED` + `Retry-After`；每 Key 同时处理中的会话 ≤ `policy.max_concurrent_sessions`（`429 CONCURRENT_LIMIT_EXCEEDED`），按 `sessions.api_key_id` 计数；用户级并发仍由 driver 配额兜底 |
| quality | `api/v1/quality.py`：`high/medium` 固定 1080p，`low` 暂 400（§10.4）；`resolve_quality` 读 `model_tiers.video`，D 只改这一个文件即可；会话级 9:16/无字幕/≤30s 注入仍归 D |
| 文件 | `POST /v1/files` 服务端收字节→OSS（≤ 200 MB；jpg/png/webp/mp4/mov/mp3/wav/m4a，其余 415）；`duration_s/width/height` 为 `null`（§10.2）；`GET /v1/files/{id}/content` 302 到 24h 签名地址 |
| 测试 | `tests/unit/test_api_key_auth.py`、`test_v1_public.py`、`test_v1_routes.py`、`test_api_key_migration.py`、`test_gaode_flow_script.py` |
| 联调脚本（F） | `uv run python scripts/gaode_flow_e2e.py --base-url https://<host>/v1 --key obx_sk_… --material road.mp4 --text "…"`：上传→建会话→发需求→每 2.5s 轮询→答卡（`--answers first|interactive|<JSON>`）→取成片→重放同一 `client_message_id`→读 `credits_used`→刷新下载链接，每步做契约断言，stdout 出 JSON 摘要，全过才退出 0；`--preflight-only` 只验 Key 与错误体；`--download-dir` 落盘成片。只走 HTTP，不依赖后端代码 |

未做（按 §10.2 / 范围）：`progress` 部件、SSE、账本 `api_key_id`、Key 管理 UI、`GET /v1/sessions` 列表。
