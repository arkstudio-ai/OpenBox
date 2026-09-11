# 消息中心方案（v1 草案，2026-09-11）

基线：`origin/main@19cfdeb`（含移动推送全部合并）。目标是在 Web 与 App 各加一个「消息中心」，把三类消息收进同一个列表：

| 来源 | 例子 | 点击去向 |
| --- | --- | --- |
| 会话产生的消息 | 任务完成/失败、等待回答/确认、定时任务结果、发布结果、授权失效 | 对应会话（或定时任务页 / 授权中心） |
| 第一方通知（后台发） | 版本更新、活动、运营公告 | 专题页 |
| 系统消息 | 技能审核结果、待审核提醒 | 技能中心 / 超管技能审核 |

手机推送不再是独立通道：**每条推送都先是一条消息中心记录**，推送只是这条记录的一种送达方式。点开通知直达目标，同时把这条记录标已读；App 里随时能在消息中心翻到历史。

## 一、现状（读代码得出）

两套互不相通的通知已经并存：

1. **站内 `notifications` 表**（`backend/db/models/notification.py`，A5 v2 引入）
   - 字段：`workspace_id`（非空）、`user_id`（空 = 工作空间全员）、`kind`、`title`、`body`、`read_at`。**没有跳转目标、没有幂等键。**
   - 生产者只有 3 处：`platforms/service.py::add_notification`（授权失效、发布完成）、`api/metadata.py`（技能待审核→所有超管）、`api/admin_skills.py`（技能审核结果→作者）。
   - 消费者只有授权中心的 `NotificationStrip`（Web）和 `notification_panel.dart`（App），只拉 `unread=true&limit=20`，点 × 即已读。用户在别处完全看不到。
2. **手机推送 `push_messages` / `push_deliveries`**（`backend/notifications/`）
   - 事务内入队，payload 带 `workspaceId / sessionId / actionId / guard`，worker 领取前二次校验业务条件；10 种固定模板，正文不含 LLM 原文。
   - 只有 App 一个消费端；App 前台时仅在"当前正打开的那个会话"弹一个 toast，其余静默丢弃，**没有任何落地列表**。
   - 点击路由在 `mobile/lib/app/notification_host.dart::_openPending`：白名单只认 `sessionId → 会话`、`cron_* → 定时任务`、`platform_auth_expired/publish_* → 授权中心`。
3. **其它可复用件**
   - Web WebSocket 按 `userId` 分发（`api/ws.py`），已有 `toast` 事件；加一个 `inbox.updated` 事件即可实时刷角标。
   - 超管控制台已有 4 栏（fleet / skills / billing / notifications），其中 notifications 是推送测试台；Web 可写、App 只读（`admin_console.dart`）。
   - 双端都有 Markdown 渲染（`Markdown.tsx`、`gpt_markdown`），专题页可以原生渲染而不用 WebView。
   - 仓库里**没有"专题页"概念**，也没有 announcement / campaign 相关代码；`landing` 是 SPA + 静态 SEO。

结论：不要再造第三张表。把 `notifications` 升级成消息中心的唯一真源，推送产生器在同一事务里顺手写一条记录并互相引用。

## 二、数据模型

### 2.1 `notifications` 表扩展（一次迁移，head 从 `e4f6a8b0c2d4` 续）

| 新增列 | 类型 | 说明 |
| --- | --- | --- |
| `category` | `String(16)` 非空 | `session` / `system` / `notice`，供列表分栏与角标分组 |
| `source_key` | `String(255)` 可空 | 幂等键，与 `push_messages.event_key` 同值；唯一约束 `(user_id, source_key)` |
| `link` | JSON 可空 | 跳转目标，见 2.3；客户端只按白名单解析 |
| `announcement_id` | `String(64)` 可空 FK | 第一方公告扇出来的记录回指公告 |
| `expires_at` | 可空 | 公告过期后不再展示 |
| `resolved_at` | 可空 | 等待回答/确认类在业务上已了结（回答、取消、被新一轮替代）时置位，同时置 `read_at`，避免陈旧红点 |
| `workspace_id` | 改为**可空** | 第一方公告是账号级，不属于任何工作空间 |

新增索引 `(user_id, created_at)`；现有 `(workspace_id, created_at)` 保留。

`push_messages.payload` 增加 `notificationId`（不加列，payload 已是 JSON）。

### 2.2 新表

`announcements`（后台可编辑的第一方通知）

| 列 | 说明 |
| --- | --- |
| `id`, `created_by`, `created_at`, `updated_at` | |
| `status` | `draft` / `scheduled` / `published` / `revoked` |
| `title`, `body` | 消息中心里显示的标题与一句摘要（沿用 120 / 500 限制） |
| `link` | 同 2.3，通常是 `topic` |
| `audience` | JSON：`{"kind":"all"}` / `{"kind":"users","ids":[…]}` / `{"kind":"workspace","id":…}` / `{"kind":"role","role":"admin"}` |
| `push` | bool，是否同时走手机推送（默认 false） |
| `publish_at`, `expires_at` | 定时发布 / 过期 |
| `fanout_at`, `fanout_count` | 扇出完成时间与人数，供后台展示 |

`topics`（专题页）

| 列 | 说明 |
| --- | --- |
| `id`, `slug`（唯一，URL 用）, `title`, `cover_url`, `content_md` | Markdown 正文，双端原生渲染 |
| `cta_label`, `cta_link` | 可选按钮；`cta_link` 只允许站内 `link` 或我们自己域名的 https |
| `status` | `draft` / `published` |
| `created_by`, `created_at`, `updated_at`, `published_at` | |

### 2.3 `link` 结构与解析规则

```json
{"kind": "session", "workspaceId": "…", "sessionId": "…", "panel": "desktop", "control": true}
{"kind": "cron",        "workspaceId": "…", "jobId": "…"}
{"kind": "auth_center", "workspaceId": "…", "jobId": "…"}
{"kind": "skills",      "workspaceId": "…"}
{"kind": "admin_skills"}
{"kind": "topic",       "slug": "…"}
{"kind": "url",         "url": "https://…"}
```

- `session` 类由服务端从 `kind + sessionId + actionId` 推导，接管卡片类可带 `panel/control`，与 `paths.desktopTakeover` 对齐。
- 客户端解析器是**白名单**：未知 `kind` 一律回退到"打开消息中心并高亮该条"，不执行任意 URL。`url` 只允许第一方公告使用，且服务端在保存时校验域名白名单。
- 跨工作空间：点击时先校验用户仍是该空间成员、会话仍可访问（App 现有 `_openPending` 的做法），再切换作用域跳转；失败则提示"目标已不可用"并留在消息中心。

## 三、后端

### 3.1 生产者统一入口

新增 `notifications/inbox.py`：

```python
async def add_inbox(db, *, user_id, category, kind, title, body, link=None,
                    workspace_id=None, source_key=None, announcement_id=None,
                    expires_at=None) -> Notification
async def resolve_inbox(db, user_id, source_key)   # 置 resolved_at + read_at
```

- `notifications/events.py::emit` 在 `enqueue_notification` 之前先 `add_inbox`（同一事务，同一 `event_key` 作 `source_key`），把 `notification.id` 放进 push payload 的 `notificationId`。
- `cancel_event`（问题被回答/取消/替换）同时 `resolve_inbox`。
- 现有 3 处 `Notification(...)` 直写改为 `add_inbox`，补 `link` 与 `source_key`。授权失效目前站内和推送各写一次，统一后靠 `source_key` 去重为一条。
- 提交后通过总线发 `inbox.updated`（`userId`、`unread` 计数），Web/App 用它刷角标；不推送正文，正文走 API。

### 3.2 API

用户侧（Bearer，Web 与 App 同用）：

| 方法与路径 | 说明 |
| --- | --- |
| `GET /api/inbox?category=&cursor=&limit=` | 游标分页；范围 = 我的全部工作空间里 `user_id == 我` 的记录 + 当前工作空间 `user_id IS NULL` 的记录 + 账号级记录；过滤 `expires_at` |
| `GET /api/inbox/unread` | `{"total": n, "session": n, "system": n, "notice": n}` |
| `POST /api/inbox/{id}/read`、`POST /api/inbox/read-all?category=` | |
| `GET /api/topics/{slug}` | **公开**，仅返回 `published` 的专题；Web 可做分享落地页 |

`/api/notifications` 两个旧接口保留一个 App 发版周期作兼容（内部改为查 `category in (system, notice) and kind in (授权中心相关)`），双端授权中心改用 `/api/inbox` 后再删。

超管侧（`User.role == "admin"`，与推送测试台同一鉴权）：

| 方法与路径 | 说明 |
| --- | --- |
| `GET/POST/PUT /api/admin/announcements`，`POST …/{id}/publish`，`POST …/{id}/revoke` | 发布 = 置 `published` + 启动扇出任务；撤回 = 未读记录标 `expires_at=now` |
| `GET/POST/PUT /api/admin/topics`，`POST …/{id}/publish` | 专题 CRUD；`slug` 一经发布不可改 |
| `POST /api/admin/announcements/{id}/preview` | 用当前超管账号扇出一条 + 可选推送，复用测试台的 10 秒延迟与前台抑制 |

### 3.3 公告扇出与推送

- 发布后由后台任务按 `audience` 解析用户，每批 500 条插入 `notifications`（`source_key = announcement:{id}`，唯一约束天然幂等，可重跑）。当前用户规模下扇出即可，不做"懒读取 + 阅读表"的复杂方案。
- `push=true` 时对每个目标用户调用 `enqueue_notification(kind="notice", …)`：`store.py` 允许列表加 `notice`；标题正文取公告字段（第一方内容，不受"固定文案"约束）；`guard={"kind":"announcement","id":…}`，`guard_valid` 校验公告仍为 `published` 且未过期；TTL 取 `expires_at` 与 24h 的较小值。推送仍受 presence 规则约束，前台用户不弹系统通知，只更新角标。
- 发布后注册的新用户不补发历史公告。

## 四、Web（frontend-v2）

- 路由：`paths.inbox = "/app/inbox"`、`paths.topic(slug) = "/topics/:slug"`（公开，不在 `/app` 下）。
- 入口：`Sidebar.tsx` 顶部（工作空间名旁）加铃铛 + 未读数；**`fe-nav-profile`（PR #20）还没合，它也改 Sidebar，铃铛位置要和那条线对一下，避免两次冲突**。
- 页面 `features/inbox/`：三个分栏（全部 / 会话 / 官方），每条显示分类图标、标题、摘要、时间、所属工作空间 chip（不同于当前空间时显示）；点击 = 标已读 + `resolveLink()`；顶部"全部已读"。
- `resolveLink()` 放 `shared/router/inboxLink.ts`，与 `paths.ts` 同源，白名单解析。
- 实时：`shared/events/bus.ts` 加 `inbox.updated`，收到即让 React Query 失效 `["inbox","unread"]`。
- 专题页 `routes/topics/TopicRoute.tsx`：封面 + 标题 + Markdown + CTA；未登录可看；CTA 为站内 link 时先登录再跳。
- 授权中心 `NotificationStrip` 改拉 `/api/inbox?category=system`。
- 超管：`ADMIN_SECTIONS` 加 `messages`（`/app/admin/messages`），两个 tab：公告（列表 / 编辑 / 受众 / 是否推送 / 定时 / 预览发我 / 发布 / 撤回）、专题（列表 / Markdown 编辑 / 发布 / 复制链接）。现有推送测试台保持原位。

## 五、App（mobile）

- `Paths.inbox = '/app/inbox'`、`Paths.topic(slug) = '/app/topics/$slug'`。
- 入口：`session_drawer.dart` 导航区加"消息中心"行 + 未读角标（顺序与 Web 侧栏一致，放在授权中心之上）；AppBar 铃铛可选。
- 页面 `features/inbox/`：与 Web 同分栏；下拉刷新 + 游标加载；`inbox_link_resolver.dart` 复用 `notification_host.dart` 里"校验成员 → 切空间 → go()"那段逻辑，两处收敛成一个。
- `notification_host.dart`：
  - 点击推送：优先按 payload 的 `notificationId` 取记录并标已读，再按记录的 `link` 路由；取不到记录时退回现有按 `type` 的路由；目标不可用时打开消息中心而不是只弹 toast。
  - 前台收到推送：除现有"当前会话 toast"外，触发 `inbox.updated` 刷角标；不弹系统通知（规则不变）。
- WebSocket：`ws_client.dart` 订阅 `inbox.updated`。
- 专题页原生 `gpt_markdown` 渲染；`url` 类 CTA 用系统浏览器打开。
- 超管控制台：`notifications` 栏加"公告"tab，可新建 / 编辑 / 预览发我 / 发布 / 撤回（见第七节第 6 项）；专题只查看与发布。

## 六、分期与验收

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| M1 后端 | 迁移、`inbox.py`、生产者接线、`/api/inbox`、`/api/topics`、总线事件 | 单测：每个推送 kind 都落一条 inbox 且 `source_key` 去重；问题回答后 `resolved_at` 置位；旧 `/api/notifications` 行为不变 |
| M2 Web | 消息中心页、铃铛、link 解析、专题页、授权中心切新接口 | Vitest 覆盖解析器白名单与跨空间跳转；未登录可打开专题页 |
| M3 App | 抽屉入口、列表页、专题页、`notification_host` 改造、WS 事件 | 现有 `notification_host_test.dart` 全过；新增"推送点击→标已读→路由"用例；800 行门禁 |
| M4 后台 | 公告/专题 CRUD、受众、扇出任务、可选推送、预览发我 | 集成测试：扇出幂等；撤回后不再出现在列表；`guard_valid` 拦截已撤回公告的推送 |
| M5 上线 | 迁移 + gw2/AWS 两边发布（按 `gw2 release procedure`），App 发版，兼容窗口结束后删旧接口 | 两部真机：推送点击进会话/进专题、冷启动去重、跨空间跳转 |

M1 与 M4 后端部分可以并行；M2、M3 依赖 M1 的接口冻结。

## 七、已拍板（2026-09-11）

| # | 事项 | 决定 |
| --- | --- | --- |
| 1 | 消息中心范围 | 跨用户的全部工作空间；不同于当前空间的记录显示空间 chip，点击时校验成员后切换作用域 |
| 2 | 专题页形态 | 站内 Markdown 页，双端原生渲染；Web `/topics/:slug` 公开可分享，CTA 可为站内 link 或白名单 https |
| 3 | 公告推送 | 默认不推，每条公告单独勾选 `push`；推送仍受 presence 规则约束 |
| 4 | 历史保留 | `session` 类 90 天后清理；`notice` 类按 `expires_at`；`system` 类暂不清理 |
| 5 | 与 PR #20 合流 | 先合并 `fe-nav-profile`（PR #20），再基于合并后的 main 做 M2 Web 铃铛与消息中心页 |
| 6 | 手机端超管 | 手机也能发公告：App 超管控制台 `notifications` 栏加公告 tab，支持新建、编辑、预览发我、发布、撤回；专题正文编辑仍以 Web 为主，手机可查看与发布 |

对第 6 项，第五节「超管控制台仍只读」作废：App 端公告编辑复用 Web 的 `/api/admin/announcements`，表单字段与 Web 一致；Markdown 正文在手机只做纯文本编辑框 + 预览。

## 八、进度

- **M1 后端：已完成（2026-09-11）**，实现细节与接口见 [MESSAGE_CENTER.md](MESSAGE_CENTER.md)。与本方案的差异：迁移 id 为 `a1c2e3b4d5f6`（`f1a2b3c4d5e6` 已被占用）；授权失效保留"工作空间广播 + 发起者个人"两条不同粒度记录，不合并；超管预览发我复用扇出逻辑而非测试台延迟。
- **M3 App：已完成（2026-09-11）**，见 [MESSAGE_CENTER.md](MESSAGE_CENTER.md)「App 端」。与本方案的差异：链接解析器放在 `features/inbox/state/inbox_navigator.dart`（feature 层不反向依赖 app 层，router 由调用方传入）；点击推送以 `POST /api/inbox/{id}/read` 的响应作为 link 来源，不再单独拉记录；未加 AppBar 铃铛，入口只在抽屉。
- **M4 后台界面：已完成（2026-09-11）**，见 [MESSAGE_CENTER.md](MESSAGE_CENTER.md)「后台界面」。与本方案的差异：App 端没有把公告塞进「通知测试」栏，而是加了第五个底部入口「消息通知」，避免与现有测试台的滚动结构互相干扰；Web 的确认框在 `admin-messages` 内自带一份（feature 之间不互相引用）。
- M2 Web 用户侧 / M5 上线：未开始。M2 等 PR #20 合并。

## 九、工作树说明

主工作树 `/Users/wxy/https-github-com-arkstudio-ai-openbox` 落后 `origin/main` 164 个提交，且 63 个未提交改动全部与上游重叠（Codex 正在其中工作），未强行 pull。本方案基于新建的干净工作树 `/Users/wxy/openbox-msgcenter`（分支 `feat/message-center`，起点 `origin/main@19cfdeb`），后续实现也在这里做。
