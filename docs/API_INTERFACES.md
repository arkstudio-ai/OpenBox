# OpenBox Backend API Interfaces

前端所需的全部后端接口定义。

**Base URL**: 由 `VITE_API_URL` 环境变量配置，默认为当前域名。

**通用约定**:
- Content-Type: `application/json`
- 错误响应: `{ "detail": "error message" }`
- 时间格式: ISO 8601 (`2024-01-01T00:00:00Z`)

---

## 目录

1. [Container 容器管理](#1-container-容器管理) (9 个)
2. [Session 会话管理](#2-session-会话管理) (5 个)
3. [Message 消息](#3-message-消息) (4 个)
4. [Compaction 上下文压缩](#4-compaction-上下文压缩) (1 个)
5. [Revert 回滚](#5-revert-回滚) (2 个)
6. [Command 命令执行](#6-command-命令执行) (1 个)
7. [Todo 任务列表](#7-todo-任务列表) (1 个)
8. [Diff 代码变更](#8-diff-代码变更) (1 个)
9. [Permission 权限确认](#9-permission-权限确认) (1 个)
10. [Question 交互提问](#10-question-交互提问) (1 个)
11. [Config 配置与元数据](#11-config-配置与元数据) (10 个)
12. [SSE 实时事件流](#12-sse-实时事件流) (1 个 + 17 种事件)
13. [WebSocket 终端](#13-websocket-终端) (1 个)
14. [Type 类型定义汇总](#14-type-类型定义汇总)
15. [PlatformAccounts 授权中心](#15-platformaccounts-授权中心) (11 个 + 1 个 Webhook)
16. [Admin 技能管理](#16-admin-技能管理) (10 个)
17. [Admin 订阅管理](#17-admin-订阅管理) (3 个)

**接口总计**: 1–11 节 36 个 + 15 节 11 个 + 16 节 10 个 + 17 节 3 个 = **60 个 HTTP**，另有 1 个 SSE、1 个 WebSocket、1 个 Webhook。

---

## 1. Container 容器管理

### 1.1 创建容器

```
POST /api/containers
```

**Request Body**:
```typescript
{
  name: string           // 容器名称
  image?: string         // Docker 镜像，可选
}
```

**Response** `200`:
```typescript
ContainerInfo
```

---

### 1.2 容器列表

```
GET /api/containers
```

**Response** `200`:
```typescript
{
  containers: ContainerInfo[]
  total: number
}
```

---

### 1.3 获取容器详情

```
GET /api/containers/:id
```

**Response** `200`:
```typescript
ContainerInfo
```

---

### 1.4 删除容器

```
DELETE /api/containers/:id
```

**Response** `200`: `void`

---

### 1.5 停止容器

```
POST /api/containers/:id/stop
```

**Response** `200`: `void`

---

### 1.6 启动容器

```
POST /api/containers/:id/start
```

**Response** `200`: `void`

---

### 1.7 获取系统信息

```
GET /api/containers/:containerId/files/system_info
```

**Response** `200`:
```typescript
{
  cpu: { percent: number; count: number }
  memory: { total: number; used: number; percent: number }
  disk: { total: number; used: number; free: number; percent: number }
}
```

---

### 1.8 列出文件

```
POST /api/containers/:containerId/files/list
```

**Request Body**:
```typescript
{
  path: string           // 目录路径，如 "/workspace"
}
```

**Response** `200`:
```typescript
{
  files: Array<{
    name: string
    is_dir: boolean
    size: number | null
    modified: string | null   // ISO 8601
  }>
}
```

---

### 1.9 终端 WebSocket URL

```
WS /ws/terminal/:containerId
```

> 由前端拼接生成，不是 HTTP 接口。详见 [WebSocket 终端](#13-websocket-终端)。

---

## 2. Session 会话管理

### 2.1 创建会话

```
POST /api/agent/session
```

**Request Body**: 无

**Response** `200`:
```typescript
Session
```

---

### 2.2 会话列表

```
GET /api/agent/session
```

**Response** `200`:
```typescript
Session[]
```

---

### 2.3 获取会话详情

```
GET /api/agent/session/:id
```

**Response** `200`:
```typescript
Session
```

---

### 2.4 删除会话

```
DELETE /api/agent/session/:id
```

**Response** `200`: `void`

---

### 2.5 更新会话

```
PATCH /api/agent/session/:id
```

**Request Body**:
```typescript
Partial<Session>         // 可更新 title, agent, model 等字段
```

**Response** `200`:
```typescript
Session
```

---

## 3. Message 消息

### 3.1 获取消息列表

```
GET /api/agent/session/:sessionId/message
```

**Response** `200`:
```typescript
MessageWithParts[]
```

---

### 3.2 发送消息（同步）

```
POST /api/agent/session/:sessionId/message
```

**Request Body**:
```typescript
{
  text: string
}
```

**Response** `200`:
```typescript
MessageWithParts
```

---

### 3.3 发送消息（异步 + SSE 流式响应）

```
POST /api/agent/session/:sessionId/prompt_async
```

> 发送后立即返回，后续通过 SSE 推送流式响应。

**Request Body**:
```typescript
{
  text: string
  agent?: string         // 指定 agent，如 "build", "explore"
  model?: string         // 指定模型，如 "anthropic/claude-sonnet-4"
  variant?: string       // 模型变体
}
```

**Response** `200`:
```typescript
{ ok: boolean }
```

---

### 3.4 中断会话

```
POST /api/agent/session/:sessionId/abort
```

**Response** `200`: `void`

---

## 4. Compaction 上下文压缩

### 4.1 触发压缩

```
POST /api/agent/session/:sessionId/summarize
```

> 触发上下文压缩。后端通过 SSE 推送 `session.compaction.start` 和 `session.compaction.complete` 事件。

**Response** `200`: `void`

---

## 5. Revert 回滚

### 5.1 回滚到指定消息

```
POST /api/agent/session/:sessionId/revert/:messageId
```

> 回滚会话状态到指定消息（含文件变更回退）。

**Response** `200`: `void`

---

### 5.2 撤销回滚

```
POST /api/agent/session/:sessionId/unrevert
```

**Response** `200`: `void`

---

## 6. Command 命令执行

### 6.1 执行斜杠命令

```
POST /api/agent/session/:sessionId/command
```

**Request Body**:
```typescript
{
  command: string        // 命令名（不含 /），如 "commit", "review"
  arguments?: string     // 命令参数
}
```

**Response** `200`: `void`

---

## 7. Todo 任务列表

### 7.1 获取 Todo

```
GET /api/agent/session/:sessionId/todo
```

**Response** `200`:
```typescript
{
  items: TodoItem[]
}
```

---

## 8. Diff 代码变更

### 8.1 获取会话 Diff

```
GET /api/agent/session/:sessionId/diff
```

**Response** `200`:
```typescript
DiffEntry[]
```

---

## 9. Permission 权限确认

### 9.1 回复权限请求

```
POST /api/agent/permission/:id
```

> 当 Agent 请求执行危险操作时，前端通过 SSE 收到 `permission.asked` 事件后，调用此接口回复。

**Request Body**:
```typescript
{
  action: "once" | "always" | "reject"
}
```

**Response** `200`: `void`

---

## 10. Question 交互提问

### 10.1 回复提问

```
POST /api/agent/question/:id
```

> 当 Agent 向用户提问时，前端通过 SSE 收到 `question.asked` 事件后，调用此接口回复。

**Request Body**:
```typescript
{
  answers: Record<string, string>   // key: 问题 ID, value: 选择的答案
}
```

**Response** `200`: `void`

---

## 11. Config 配置与元数据

### 11.1 获取应用配置

```
GET /api/agent/config
```

**Response** `200`:
```typescript
{
  models: ModelInfo[]
  default_model: string       // 如 "anthropic/claude-sonnet-4"
  default_agent: string       // 如 "build"
  video_models: ModelInfo[]
  default_video_model: string
  default_video_resolution: string
  skill_store_review: boolean // 2026-09-08 新增，见 §16
}
```

`skill_store_review` 来自后端配置 `SKILL_STORE_REVIEW`（默认 `true`）。前端只用它选发布对话框的文案：`true` 时是「提交后由管理员审核」，`false` 时是「上传后所有用户可见」。**它不是权限**——上架规则由服务端在发布时决定，前端改这个值改不了结果。

---

### 11.2 Agent 列表

```
GET /api/agent/agent
```

**Response** `200`:
```typescript
AgentConfig[]
```

---

### 11.3 Skill 列表

```
GET /api/agent/skill
```

沙箱与主机的技能合并列表，再叠加数据库里的归属信息（`skill.user_library.annotate_installed_skills`）。

**Response** `200`:
```typescript
(SkillInfo & {
  category: "personal" | "store" | "installed" | "builtin" | "host"
  library_id: string | null            // 仅 personal
  catalog_id: string | null            // personal 发布过为 "community:<id>"；store 为安装时记下的键
  publication_status: "unpublished" | "published" | "withdrawn" | null
  published_at: string | null
  // 2026-09-08 新增：审核状态随作者自己的副本一起返回，"我的"才能显示「已驳回，因为…」
  listing: "pending" | "listed" | "rejected" | "delisted" | null
  listing_note: string | null          // 驳回/下架原因，只有作者与超管看得到
  is_official: boolean
})[]
```

`listing` 只在 `category === "personal"` 且**发布过**时非空；从未提交过的草稿是 `null`（列的默认值 `listed` 不能当成「已上架」）。`publication_status === "withdrawn"` 是作者自己撤回；`listing` 保留撤回前的审核状态。

---

### 11.4 Command 列表

```
GET /api/agent/command
```

**Response** `200`:
```typescript
CommandInfo[]
```

---

### 11.5 MCP 服务器状态

```
GET /api/agent/mcp
```

**Response** `200`:
```typescript
McpServer[]
```

---

### 11.6 连接 MCP 服务器

```
POST /api/agent/mcp/:name/connect
```

**Response** `200`: `void`

---

### 11.7 断开 MCP 服务器

```
POST /api/agent/mcp/:name/disconnect
```

**Response** `200`: `void`

---

### 11.8 技能商店目录

```
GET /api/agent/catalog
```

用户侧商店。三个来源合成一份：代码里的 `SKILL_CATALOG` / `MCP_CATALOG`（叠加 `catalog_overrides` 的上下架覆盖）、`OPENBOX_CATALOG_URL` 远程覆盖、以及数据库里已上架的用户投稿。

**Response** `200`:
```typescript
{
  skills: CatalogEntry[]
  mcp: CatalogEntry[]
}

interface CatalogEntry {
  id: string                 // 目录条目为 "web-research"；投稿为 "community:<user_skills.id>"
  catalog_id: string         // 全店统一键："skill:web-research" / "mcp:playwright" / "community:<id>"
  kind: "skill" | "mcp"
  name: string
  title: string
  icon: string
  description: string
  publisher: string          // 目录条目是发布方；投稿是作者用户名
  homepage: string
  tags: string[]
  requires_mcp: string[]
  missing_mcp: string[]      // requires_mcp 里当前沙箱还没装的
  installed: boolean         // 沙箱不可达时全为 false
  install: object            // 目录条目的安装说明；投稿为 {}
  // 2026-09-08 新增
  origin: "official" | "community" | "third_party"
  official: boolean          // origin === "official"
  featured: boolean          // 置顶
  installs_count: number     // 按 skill_installs 统计；单机模式无库时为 0
  version: number | null     // 仅投稿
  published_at: string | null// 仅投稿
  listing?: "listed"         // 目录条目带；这里只会出现已上架的
  community?: true           // 仅投稿
}
```

**2026-09-08 起的行为变化**：
- 只返回 `listing === "listed"` 的条目。投稿还要 `status === "published"` 且作者账号有效（`skill.user_library.store_visible()`）。默认口径下 `anthropic-skills` 不在这里（代码默认 `delisted`），7 个 MCP 在。
- 排序：置顶 → 安装数倒序 → 发布时间倒序 → 标题。代码目录条目没有发布时间，排在有日期的之后。
- 下架**不影响依赖解析**：`with_mcp` 装依赖读的是未过滤的 `catalog_index()`，官方内容技能声明的 MCP 永远装得上。这是有意的，见 `skill/catalog.py` 注释。
- `POST /api/agent/catalog/install` 也按上架状态判：条目被下架后再调用返回 `409`（不是 404），文案「…is no longer available in the store.」；安装成功后写一行 `skill_installs`（`catalog_id = "<kind>:<id>"`、`user_skill_id = null`），写不进去就回滚安装并返回 500。

---

### 11.9 发布个人技能到商店

```
POST /api/agent/skill/:name/publish
```

把作者当前的草稿快照复制成公开版本。`name` 可以是技能名或安装目录名，且只能是自己的 `category === "personal"` 技能。

**Response** `200`: 与 11.3 中 personal 条目同构的快照，关键字段：
```typescript
{
  id: string
  catalog_id: string           // "community:<id>"
  name: string
  publication_status: "published"
  status: "published"
  listing: "pending" | "listed" | "delisted"
  listing_note: string | null  // 重新提交时清空；保持下架时保留原因
  is_official: boolean
  published_version: number
  published_at: string
  has_unpublished_changes: boolean
}
```

`listing` 由服务端决定（`skill.user_library._listing_after_publish`），前端给不了也改不了：

| 情况 | 结果 |
|---|---|
| 发布者 `role === "admin"` | `listed` + `is_official = true`，不看审核开关 |
| `SKILL_STORE_REVIEW=true` | `pending`（首次、被驳回后重提、被下架后重提，一律回队列）；同时给每个 admin 写一条 `skill_pending` 通知 |
| `SKILL_STORE_REVIEW=false` 且当前不是 `delisted` | `listed` |
| `SKILL_STORE_REVIEW=false` 且当前是 `delisted` | 仍是 `delisted`——「更新版本」不能绕过下架 |

内容与上次发布一致时不递增 `published_version`、不刷新 `published_at`，只更新 `listing`。审计 `skill.publish`。

**错误**：`404` 找不到个人技能；`409` 沙箱里那个目录不是这个人的个人技能；`503` 没有可用沙箱；`400` 校验失败。

---

### 11.10 撤回已发布的技能

```
POST /api/agent/skill/:name/withdraw
```

作者自己把发布版本从商店撤下（§3-Q4）。只对自己的 `personal` 技能、且必须发布过。

**Response** `200`: 同 11.9 的快照，其中 `status`/`publication_status` 变为 `"withdrawn"`。

- `listing` 与 `published_*` 快照都**保留**：作者随时能重新提交（重提按 11.9 的规则再走一遍审核），超管也仍能查到当时审了什么。
- 已经安装的用户不受影响，沙箱里的副本不动。
- 审计 `skill.withdraw`。
- **错误**：`404` 不是自己的个人技能；`400` 从未发布过（`This skill has never been published`）。

---

## 12. SSE 实时事件流

### 连接端点

```
GET /api/agent/event
```

> 使用浏览器原生 `EventSource` API 连接。每条消息格式为 `data: JSON\n\n`。

### 消息格式

```typescript
{
  type: string           // 事件类型
  data: object           // 事件数据
}
```

### 12.1 事件类型清单

#### Session 事件

| 事件类型 | data 结构 | 说明 |
|---------|----------|------|
| `session.status` | `{ sessionId: string; status: SessionStatus }` | 会话状态变更 |
| `session.title` | `{ sessionId: string; title: string }` | 会话标题更新（Agent 自动生成） |
| `session.error` | `{ sessionId: string; error: { message: string } }` | 会话错误 |
| `session.diff` | `{ sessionId: string }` | 代码变更通知（触发前端 refetch diff） |
| `session.compaction.start` | `{ sessionId: string }` | 开始上下文压缩 |
| `session.compaction.complete` | `{ sessionId: string }` | 压缩完成 |

#### Message 事件

| 事件类型 | data 结构 | 说明 |
|---------|----------|------|
| `message.created` | `{ sessionId: string; message: MessageWithParts }` | 新消息（user 或 assistant） |
| `message.updated` | `{ sessionId: string; message: MessageWithParts }` | 消息更新（完整替换） |
| `message.text_delta` | `{ sessionId: string; messageId: string; partId: string; text: string }` | 文本增量（流式打字效果） |

#### Part 事件

| 事件类型 | data 结构 | 说明 |
|---------|----------|------|
| `part.created` | `{ sessionId: string; messageId: string; part: MessagePart }` | 新 Part 添加到消息 |
| `part.updated` | `{ sessionId: string; messageId: string; part: MessagePart }` | Part 内容更新 |
| `part.delta` | `{ sessionId: string; messageId: string; partId: string; delta: string }` | Part 文本增量（reasoning 打字效果） |

#### Tool 事件

| 事件类型 | data 结构 | 说明 |
|---------|----------|------|
| `tool.running` | `{ sessionId: string; partId: string; tool: string; input: Record<string, unknown> }` | 工具开始执行 |
| `tool.completed` | `{ sessionId: string; partId: string; output: string; title?: string }` | 工具执行完成 |
| `tool.error` | `{ sessionId: string; partId: string; error: string }` | 工具执行出错 |

#### 交互事件

| 事件类型 | data 结构 | 说明 |
|---------|----------|------|
| `permission.asked` | `PermissionRequest` | Agent 请求权限确认 |
| `question.asked` | `QuestionRequest` | Agent 向用户提问 |

#### 其他事件

| 事件类型 | data 结构 | 说明 |
|---------|----------|------|
| `todo.updated` | `{ sessionId: string }` | Todo 列表变更（触发 refetch） |

---

## 13. WebSocket 终端

### 连接端点

```
WS /ws/terminal/:containerId
```

> HTTP 协议部分自动替换为 `ws://` 或 `wss://`。

### 二进制帧协议

| 前缀字节 | 含义 | 数据 |
|---------|------|------|
| `0x00` | 数据帧 | 终端输入/输出数据 |
| `0x01` | 窗口调整 | 行列大小（JSON 或二进制编码） |

### JSON 消息格式

```typescript
{
  type: "input" | "output" | "error" | "heartbeat"
  data?: string
  exit_code?: number
}
```

---

## 14. Type 类型定义汇总

### ContainerInfo

```typescript
interface ContainerInfo {
  id: string
  name: string
  status: "creating" | "running" | "stopped" | "error"
  image: string
  created_at: string
  port: number | null
  api_key: string | null
}
```

### Session

```typescript
interface Session {
  id: string
  title: string
  agent: string                   // 使用的 agent 名称
  model: string                   // 使用的模型 ID
  status: "idle" | "busy" | "retry" | "error" | "compacting"
  created_at: string
  updated_at: string
  additions?: number              // 新增行数
  deletions?: number              // 删除行数
  files_changed?: number          // 变更文件数
  token_usage?: TokenUsage
}
```

### TokenUsage

```typescript
interface TokenUsage {
  input: number
  output: number
  cache: number
  total: number
  limit: number
  cost: number                    // 单位: USD
}
```

### MessageWithParts

```typescript
interface MessageWithParts {
  id: string
  session_id: string
  role: "user" | "assistant" | "system"
  parts: MessagePart[]
  created_at: string
  agent?: string
  model?: string
}
```

### MessagePart（联合类型）

```typescript
type MessagePart =
  | TextPart           // { type: "text", id, text }
  | ReasoningPart      // { type: "reasoning", id, text }
  | ToolPartData       // { type: "tool", id, tool, status, input?, output?, error?, title?, duration? }
  | StepStartPart      // { type: "step-start", id, step }
  | StepFinishPart     // { type: "step-finish", id, step, input_tokens, output_tokens, cost, duration }
  | CompactionPart     // { type: "compaction", id, summary? }
  | SubtaskPart        // { type: "subtask", id, agent, description, status, output? }
  | PatchPart          // { type: "patch", id, files: PatchFile[] }
  | FilePart           // { type: "file", id, path, mime_type?, url? }
  | AgentSwitchPart    // { type: "agent", id, agent }
  | RetryPart          // { type: "retry", id, attempt, reason? }
```

### PatchFile

```typescript
interface PatchFile {
  path: string
  additions: number
  deletions: number
  status: "added" | "modified" | "deleted"
}
```

### DiffEntry

```typescript
interface DiffEntry {
  path: string
  additions: number
  deletions: number
  status: "added" | "modified" | "deleted"
  hunks?: DiffHunk[]
}

interface DiffHunk {
  old_start: number
  old_count: number
  new_start: number
  new_count: number
  lines: DiffLine[]
}

interface DiffLine {
  type: "add" | "del" | "context"
  content: string
  old_line?: number
  new_line?: number
}
```

### TodoItem / TodoList

```typescript
interface TodoItem {
  id: string
  subject: string
  description?: string
  status: "pending" | "in_progress" | "completed"
  active_form?: string
}

interface TodoList {
  items: TodoItem[]
}
```

### AgentConfig

```typescript
interface AgentConfig {
  name: string
  description: string
  model: string
  temperature: number
  tools: string[]
  system_prompt?: string
}
```

### ModelInfo

```typescript
interface ModelInfo {
  id: string
  name: string
  provider: string
  max_tokens: number
  variants?: string[]
}
```

### SkillInfo

```typescript
interface SkillInfo {
  name: string
  description: string
  source: "global" | "project" | "remote"
  content?: string
}
```

### McpServer / McpTool

```typescript
interface McpServer {
  name: string
  type: "stdio" | "remote"
  status: "connected" | "disconnected" | "error"
  tools: McpTool[]
  error?: string
}

interface McpTool {
  name: string
  description: string
}
```

### CommandInfo

```typescript
interface CommandInfo {
  name: string
  description: string
  arguments?: string
}
```

### AppConfig

```typescript
interface AppConfig {
  models: ModelInfo[]
  default_model: string
  default_agent: string
}
```

### PermissionRequest / PermissionReply

```typescript
interface PermissionRequest {
  id: string
  session_id: string
  tool: string
  input: Record<string, unknown>
  patterns?: string[]
  metadata?: Record<string, unknown>
  is_doom_loop?: boolean
  created_at: string
}

type PermissionAction = "once" | "always" | "reject"

interface PermissionReply {
  id: string
  action: PermissionAction
}
```

### QuestionRequest / QuestionReply

```typescript
interface QuestionRequest {
  id: string
  session_id: string
  questions: Question[]
  created_at: string
}

interface Question {
  question: string
  header?: string
  options: QuestionOption[]
  multi_select: boolean
}

interface QuestionOption {
  label: string
  description?: string
}

interface QuestionReply {
  id: string
  answers: Record<string, string>
}
```

---

## 15. PlatformAccounts 授权中心

> 2026-09-07 新增，见 `docs/A5_AUTHORIZATION_CENTER.md`。除回调与 Webhook 外都要带 `Authorization` 与 `X-Workspace-Id`。
> 错误响应形如 `{ "detail": { "code": "PLATFORM_…", "message": "…" } }`，同时带 `X-Error-Code` 头；前端按 `code` 查 `auth-center.json` 的 `errors.*` 文案。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/api/platforms` | 成员 | 平台目录：`{key, display, capabilities: ("login"\|"publish")[], configured, maxGrantDays}[]` |
| GET | `/api/platform-accounts` | 成员 | 当前工作空间已绑定账号（不含任何 token 字段） |
| POST | `/api/platform-accounts/{platform}/authorize` | owner/admin | → `{authorizeUrl, state}`，前端整页跳转 |
| GET | `/api/platform-accounts/{platform}/callback?code&state&scopes` | 无鉴权 | 平台回跳；服务端换 token 后 302 到 `/app/auth-center?platform=…&bound=<id>` 或 `&error=<code>` |
| POST | `/api/platform-accounts/{id}/probe` | 成员 | 用 access_token 读资料验证授权；失效则尝试刷新，再失败置 `expired` |
| POST | `/api/platform-accounts/{id}/refresh` | owner/admin | 手动刷新/续期 |
| DELETE | `/api/platform-accounts/{id}` | owner/admin | 解绑（软删 + 清 token） |
| POST | `/api/platform-accounts/douyin/publish` | 成员 | `{file_asset_id, title?, hashtags?[], private_status? 0\|1\|2, download_type? 1\|2}` → `{job, schema}`；`schema` 是 `snssdk1128://openplatform/share?…`，前端渲染成二维码 |
| GET | `/api/publish-jobs` | 成员 | 最近 50 条投稿记录 |
| GET | `/api/publish-jobs/{id}` | 成员 | 轮询投稿状态：`pending\|published\|failed\|expired` |
| GET / POST | `/api/notifications?unread=`、`/api/notifications/{id}/read` | 成员 | 站内通知（授权失效、投稿完成） |
| GET / POST | `/api/inbox?category=&unread=&cursor=&limit=`、`/api/inbox/unread`、`/api/inbox/{id}/read`、`/api/inbox/read-all` | 成员 | 消息中心：跨工作空间收件箱、分类未读数、已读（见 [MESSAGE_CENTER.md](MESSAGE_CENTER.md)） |
| GET | `/api/topics/{slug}` | 公开 | 已发布的专题页（Markdown） |
| GET / POST / PUT | `/api/admin/messages/announcements[/{id}[/publish\|revoke\|preview]]`、`/api/admin/messages/topics[/{id}[/publish\|unpublish]]` | 平台超管 | 第一方公告与专题页管理 |
| POST | `/api/webhooks/douyin` | 抖音签名 | `verify_webhook` 回 `{challenge}`；`create_video` 按 `share_id` 回写投稿结果；`X-Douyin-Signature = sha1(client_secret + body)`，`Msg-Id` 去重 |

### PlatformAccount

```typescript
interface PlatformAccount {
  id: string
  platform: string                  // "douyin"
  authKind: "oauth" | "desktop_cookie"
  externalId: string                // open_id
  unionId: string | null
  nickname: string | null
  avatarUrl: string | null
  scopes: string[]
  status: "bound" | "expired" | "revoked"
  accessExpiresAt: string | null
  refreshExpiresAt: string | null
  renewCount: number
  renewalsLeft: number
  lastRefreshAt: string | null
  lastProbeAt: string | null
  lastOkAt: string | null
  lastError: string | null
  boundAt: string | null
  boundByUserId: string
}

interface PublishJob {
  id: string
  platform: string
  platformAccountId: string | null
  fileAssetId: string
  title: string
  hashtags: string[]
  shareId: string | null
  status: "pending" | "published" | "failed" | "expired"
  itemId: string | null
  videoId: string | null
  fromOpenId: string | null
  error: string | null
  expiresAt: string | null
  publishedAt: string | null
  createdAt: string | null
}
```


---

## 16. Admin 技能管理

> 2026-09-08 新增，见 `docs/ADMIN_CONSOLE_SKILL_STORE_PLAN.md` §4.5.1。实现在 `backend/api/admin_skills.py`，决策逻辑在 `backend/skill/user_library.py`。
> **鉴权**：`Authorization` + `users.role === "admin"`，否则 `403 {"detail": "Admin access required"}`。角色在 access JWT 里，改库后要重新登录。
> **不需要 `X-Workspace-Id`**：超管视图跨租户，router 没有挂 `get_workspace`。审计里的 `workspace_id` 填被操作对象所属空间（投稿作者的空间），目录条目为 `null`。
> **`catalog_id`** 是全店统一键：投稿 `community:<user_skills.id>`，代码目录条目 `skill:<id>` / `mcp:<id>`。放进路径时必须 `encodeURIComponent`（它含冒号）。格式不合法返回 `400`。
> **幂等**：写接口重复同一决定返回 `200` 且 `changed: false`，不再写审计、不再发通知。
> **审计**：所有 GET 记 `admin.view_skills`；下载 ZIP 记 `admin.skill.download`；写操作分别记 `admin.skill.listing` / `admin.skill.featured` / `admin.skill.official` / `admin.skill.approve` / `admin.skill.reject`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/skills/store` | 商店全量（目录条目 + 投稿），可筛选、分页 |
| POST | `/api/admin/skills/store/{catalog_id}/listing` | 上架 / 下架 / 直接改审核状态；下架与驳回必填原因 |
| POST | `/api/admin/skills/store/{catalog_id}/featured` | 置顶开关 |
| POST | `/api/admin/skills/store/{catalog_id}/official` | 标记官方，**只对投稿** |
| GET | `/api/admin/skills/review` | 审核队列（待审核 / 已驳回），等得最久的排前面 |
| GET | `/api/admin/skills/review/{catalog_id}` | 审核详情：元数据 + SKILL.md 全文 + 文件清单 |
| GET | `/api/admin/skills/review/{catalog_id}/archive` | 下载投稿 ZIP 原文件 |
| POST | `/api/admin/skills/review/{catalog_id}/approve` | 通过：`listing → listed` |
| POST | `/api/admin/skills/review/{catalog_id}/reject` | 驳回：`listing → rejected`，`note` 必填 |
| GET | `/api/admin/skills/installs` | 谁装了什么（两类来源都记） |

### 16.1 商店列表

```
GET /api/admin/skills/store?origin=&kind=&listing=&q=&sort=&offset=&limit=
```

| 参数 | 取值 | 说明 |
|---|---|---|
| `origin` | `official` \| `community` \| `third_party` | `community` 只看投稿；`third_party` 只看代码目录里非 OpenBox 发布方的条目 |
| `kind` | `skill` \| `mcp` | `mcp` 只有代码目录条目（投稿全是 skill） |
| `listing` | `pending` \| `listed` \| `rejected` \| `delisted` | 目录条目只可能是 `listed` / `delisted` |
| `q` | string | 目录条目匹配 `catalog_id/name/title/description/publisher`；投稿匹配技能名、作者用户名与邮箱 |
| `sort` | `recent`(默认) \| `oldest` \| `installs` \| `name` | 日期排序时，没有发布时间的代码目录条目排在有日期的之后 |
| `offset` | 0 – 5000 | 合并两个来源要读到 `offset + limit`，所以深度有上限；再深请改用筛选 |
| `limit` | 1 – 200，默认 50 | |

**Response** `200`:
```typescript
{
  items: StoreEntry[]
  total: number      // 命中的目录条目数 + 命中的投稿数
  offset: number
  limit: number
}

interface StoreEntry {
  catalog_id: string
  id: string                       // 与 catalog_id 相同，方便前端复用列表原语
  source: "catalog" | "community"
  kind: "skill" | "mcp"
  origin: "official" | "community" | "third_party"
  name: string
  title: string
  icon: string
  description: string
  publisher: string                // 目录条目是发布方；投稿是作者用户名
  homepage: string
  author: { user_id: string; username: string | null; email: string | null } | null  // 目录条目为 null
  workspace_id: string | null
  library_id: string | null        // 投稿的 user_skills.id
  install_dir: string | null
  version: number | null
  published_at: string | null
  created_at: string | null
  updated_at: string | null
  installs_count: number
  status: "published" | "withdrawn" | null                       // 目录条目为 null
  listing: "pending" | "listed" | "rejected" | "delisted"
  listing_note: string | null
  listing_changed_by: string | null
  listing_changed_at: string | null
  featured: boolean
  is_official: boolean
  requires_mcp: string[]
  size: number | null              // 投稿 ZIP 字节数
  sha256: string | null
}
```

列表查询用 `load_only` 排除了两列 `LargeBinary`，一页不会拖 ZIP 字节。目录条目也不返回 `install` / `config`（SKILL.md 正文与 MCP 命令行、env）。

### 16.2 上架 / 下架 / 改审核状态

```
POST /api/admin/skills/store/{catalog_id}/listing
```

**Request Body**:
```typescript
{
  listing: "pending" | "listed" | "rejected" | "delisted"
  note?: string        // listing 为 delisted 或 rejected 时必填
}
```

**Response** `200`（三个写接口共用的结果结构）:
```typescript
interface ModerationResult {
  catalog_id: string
  source: "catalog" | "community"
  field: "listing" | "featured" | "official"
  previous: string | boolean
  current: string | boolean
  changed: boolean          // false = 这次调用没改动任何东西，也没写审计、没发通知
  listing: string
  featured: boolean
  is_official: boolean
  note: string | null
  owner_id: string | null   // 目录条目为 null
  workspace_id: string | null
  name: string | null
  version: number | null
}
```

**错误**：`422` 该决定要求原因但没给；`400` `catalog_id` 格式不合法，或给目录条目下了 `pending` / `rejected`（目录条目只接受 `listed` / `delisted`）；`404` `community:<id>` 找不到对应行。代码目录里没有的 `skill:` / `mcp:` id **不会 404**——按「上架、不置顶」当默认，照样写一条 `catalog_overrides`，这样对远程 overlay 里的条目也能做决定。

**副作用**：投稿的 `listing` 真的变了才通知作者——`listed → skill_approved`、`rejected → skill_rejected`、`delisted → skill_delisted`（正文含技能名、版本与原因）。回到 `pending` 是作者自己重新提交，不再通知。通知写失败只记日志，不回滚已经生效的决定。

### 16.3 置顶 / 标记官方

```
POST /api/admin/skills/store/{catalog_id}/featured   { "featured": true }
POST /api/admin/skills/store/{catalog_id}/official   { "is_official": true }
```

**Response** `200`: `ModerationResult`。

`official` 只对投稿：代码目录条目的 `origin` 跟着代码里的 `publisher` 走，对它调用返回 `400`。置顶不会改 `listing_changed_at`（那个日期是给作者看的审核时间），谁在什么时候置顶去审计里查。

### 16.4 审核队列

```
GET /api/admin/skills/review?state=pending|rejected&offset=&limit=
```

`state` 默认 `pending`，`limit` 1–200 默认 50。只列 `status === "published"` 的行——作者撤回后就退出队列。排序固定为提交时间升序（等最久的排最前）。

**Response** `200`: `{ items: StoreEntry[], total, offset, limit }`，`items` 是 16.1 的投稿形态。

### 16.5 审核详情

```
GET /api/admin/skills/review/{catalog_id}
```

**Response** `200`: 16.1 的投稿字段，外加：
```typescript
{
  source: "community"
  installs_count: number
  files: { path: string; size: number }[]   // 最多 500 条，不含目录项
  files_total: number
  files_truncated: boolean
  skill_md: string | null                   // ZIP 里层级最浅的那个 SKILL.md，最多 64 KB
  skill_md_truncated: boolean
  skill_md_error: string | null             // 例如 "No SKILL.md in this archive"
  archive_error: string | null              // 包太大 / 条目太多 / 读不开
}
```

**安全口径（§4.9）**：ZIP 只在内存里打开，只读 `infolist()` 与被截断的 SKILL.md；不解压到磁盘、不执行任何内容。`> 64 MB` 或声明 `> 20000` 个成员时直接拒读并在 `archive_error` 说明。`size` 是作者声明的成员大小，原样呈现——不合理的数字正是审核人应该看到的。前端按**纯文本**渲染，不做 Markdown。

**错误**：`404` `catalog_id` 不是 `community:` 前缀（目录条目没有审核记录），或该投稿不存在。作者账号被停用不影响这里读——那恰恰是审核人最需要看的一条。

### 16.6 下载投稿 ZIP

```
GET /api/admin/skills/review/{catalog_id}/archive
```

**Response** `200`: `application/zip` 字节流，带 `Content-Disposition: attachment; filename="<name>.zip"; filename*=UTF-8''…`、`Content-Length`、`X-Content-Type-Options: nosniff`。`404` 该投稿没有归档字节。

### 16.7 通过 / 驳回

```
POST /api/admin/skills/review/{catalog_id}/approve        // 无 body
POST /api/admin/skills/review/{catalog_id}/reject         // { "note": "必填原因" }
```

**Response** `200`: `ModerationResult`。两个接口都只接受 `community:` 前缀，否则 `404`；`reject` 的 `note` 为空返回 `422`。通过等价于 16.2 的 `listing: "listed"`，只是审计动作不同（`admin.skill.approve` / `admin.skill.reject`）。

### 16.8 用户安装记录

```
GET /api/admin/skills/installs?catalog_id=&user_id=&q=&offset=&limit=
```

`q` 匹配用户名、邮箱、安装时记下的技能名与投稿的发布名。排序按安装时间倒序。

**Response** `200`:
```typescript
{
  items: {
    id: string
    user: { id: string; username: string; email: string }
    catalog_id: string
    kind: "skill" | "mcp"
    origin: "official" | "community" | "third_party"
    name: string
    title: string
    icon: string
    install_dir: string
    installed_at: string
  }[]
  total: number
  offset: number
  limit: number
}
```

按安装记录显示。用户在沙箱里手动删掉的技能不会实时反映；从内置镜像自带的技能没有安装记录。

---

## 17. Admin 订阅管理

> 2026-09-08 新增，见 `docs/ADMIN_CONSOLE_SKILL_STORE_PLAN.md` §4.5.2。实现在 `backend/api/admin_billing.py`。
> **本期只读**（§3-Q5）：这一节只有 GET，没有调账、代付、改到期。
> 鉴权同 §16（`role === "admin"`，不需要 `X-Workspace-Id`）。全部记审计 `admin.view_billing`。
> **金额口径**：`amount_fen` 是整数分，前端自己除以 100；`credits` / `balance` / `amount` 是 `Numeric(28, 12)` 序列化出来的**字符串**，不要转 float。
> **不返回**：`checkout_url`、支付渠道密钥或任何 token。`provider_order_id` 会返回——对账要用。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/admin/billing/subscriptions` | 每个空间一行：当前套餐、状态、余额、排队续期数、最近支付 |
| GET | `/api/admin/billing/orders` | `payment_orders` 全量，可按渠道 / 状态 / 类型 / 日期 / 关键词筛 |
| GET | `/api/admin/billing/workspaces/{workspace_id}` | 空间详情：订阅历史、订单、账本尾、近 30 天用量 |

### 17.1 订阅列表

```
GET /api/admin/billing/subscriptions?plan=&state=&q=&offset=&limit=
```

| 参数 | 取值 | 说明 |
|---|---|---|
| `plan` | `plans.json` 里的套餐 id（`free` / `pro` / `max`…） | 未知值返回 `422 Unknown plan <x>`。`free` 指「当前没有有效付费期」，不是 `plan_id='free'` 的行 |
| `state` | `active` \| `expired` \| `free` | `active` = `starts_at <= now < ends_at`；`expired` = 有过订阅但当前没有；`free` = 从来没买过 |
| `q` | string | 空间名 / owner 用户名 / 邮箱 |
| `offset` / `limit` | `limit` 1–200，默认 50 | |

筛选在 SQL 里完成（相关子查询），所以 `total` 与分页是一致的。已软删的空间不在列表里；排序按空间创建时间倒序。

**Response** `200`:
```typescript
{
  items: {
    workspace: { id: string; name: string; kind: string }
    owner: { id: string; username: string; email: string }
    plan_id: string                 // 没有有效订阅时是 free 套餐的 id
    cycle: string | null
    starts_at: string | null
    ends_at: string | null
    state: "active" | "expired" | "free"
    queued_count: number            // 已付款但还没开始的续期期数
    balance: string                 // 只读余额，不会顺手发放周期赠额
    last_paid_at: string | null
  }[]
  total: number
  offset: number
  limit: number
}
```

有效期判定复用 `billing.subscriptions.active_subscription` 的同一条谓词，与用户侧 `/api/billing/subscription` 不会打架（AC-11）。owner 是 inner join：owner 账号已从 `users` 消失的空间不会出现在这个列表里（订单列表反过来用 outer join，历史订单不会丢行）。

### 17.2 订单列表

```
GET /api/admin/billing/orders?provider=&status=&kind=&from=&to=&q=&offset=&limit=
```

| 参数 | 取值 | 说明 |
|---|---|---|
| `provider` | string | 支付渠道，精确匹配 |
| `status` | `pending` \| `paid` \| `cancelled` | |
| `kind` | `topup` \| `subscription` | |
| `from` / `to` | `YYYY-MM-DD` | 都按整 UTC 日算，`to` 当天**包含**在内；`from > to` 返回 `422` |
| `q` | string | 订单号 / 渠道单号 / 空间名 / 下单人用户名或邮箱 |

空间与下单账号都用 outer join：订单比空间和账号活得久，运营列表不能悄悄丢行。

**Response** `200`:
```typescript
{
  items: {
    id: string
    workspace_id: string
    workspace_name: string | null
    user_id: string
    user: { id: string; username: string; email: string } | null
    provider: string
    kind: "topup" | "subscription"
    amount_fen: number
    currency: string
    credits: string | null
    status: "pending" | "paid" | "cancelled"
    plan_id: string | null        // 订阅单才有
    cycle: string | null
    provider_order_id: string | null
    created_at: string
    paid_at: string | null
    cancelled_at: string | null
    cancellation_reason: string | null
  }[]
  total: number
  offset: number
  limit: number
}
```

### 17.3 空间详情

```
GET /api/admin/billing/workspaces/{workspace_id}
```

已软删的空间**仍然可读**：它的支付历史还在，对账要够得着。找不到返回 `404`。

**Response** `200`:
```typescript
{
  workspace: {
    id: string; name: string; kind: string
    owner_user_id: string; plan_id: string
    created_at: string; is_deleted: boolean; deleted_at: string | null
  }
  owner: { id: string; username: string; email: string } | null
  member_count: number                    // status = active 的成员
  balance: string
  plan_id: string                         // 当前有效套餐，没有则 free
  subscription: Term | null               // 当前有效期
  queued: Term[]                          // 已付款、尚未开始
  history: Term[]                         // 全部订阅期，开始时间倒序
  orders: Order[]                         // 最近 100 单，含下单人
  ledger: {                               // 最近 50 条积分流水
    id: string; kind: string; amount: string
    balance_after: string; reference_id: string | null; created_at: string
  }[]
  usage: {
    since: string; days: 30
    items: { status: string; events: number; total_tokens: number; credits: string }[]
  }
}

interface Term {
  order_id: string; plan_id: string; cycle: string
  starts_at: string; ends_at: string
}
```
