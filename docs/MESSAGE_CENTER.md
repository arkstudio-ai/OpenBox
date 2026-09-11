# 消息中心（M1 后端 + M3 App）

方案与拍板见 [MESSAGE_CENTER_PLAN.md](MESSAGE_CENTER_PLAN.md)。本文是后端接口与行为说明（M1）及 App 端实现说明（M3）；Web（M2）、后台界面（M4）按此接。

一句话：`notifications` 表是消息中心唯一真源；每条手机推送先是一条 inbox 记录，push payload 里的 `notificationId` 指回它；第一方公告由后台编辑，发布时扇出成每人一条记录；专题页是站内 Markdown。

## 数据

迁移 `a1c2e3b4d5f6`（续 `e4f6a8b0c2d4`）。桌面单机 SQLite 由 `db/base.py::_upgrade_desktop_message_center_columns` 补列（旧库 `workspace_id` 仍非空，桌面模式没有账号级公告）。

`notifications` 新列：`category`（`session` / `system` / `notice`）、`link`（JSON）、`source_key`（与 `push_messages.event_key` 同值，`(user_id, source_key)` 唯一）、`announcement_id`、`resolved_at`、`expires_at`；`workspace_id` 改可空。新表 `announcements`、`topics`，字段见 `backend/db/models/notification.py`。

保留策略（`InboxJanitor` 每 24h 跑一次 `inbox.sweep`）：`session` 类 90 天后删除；带 `expires_at` 的记录过期 30 天后删除；`system` 类不清理。

## 记录从哪里来

| 生产者 | category | kind | source_key | link |
| --- | --- | --- | --- | --- |
| `notifications/events.py::emit`（所有会推送的业务事件） | session / system | 与推送模板同名 | 与 `event_key` 同值 | 由 `inbox.link_for` 推导：有 `sessionId` → `session`；`cron_*` → `cron`；授权/发布类 → `auth_center` |
| `platforms/service.py::add_notification`（授权失效、云电脑重登、投稿完成） | system | 原 kind | 授权失效 `auth:{account}:expired:{date}`，投稿 `publish:{job}:terminal` | `auth_center` |
| `api/metadata.py`（技能待审核 → 各超管） | system | `skill_pending` | `skill:{id}:v{n}:pending` | `admin_skills` |
| `api/admin_skills.py`（审核结果 → 作者） | system | `skill_approved` / `skill_rejected` / `skill_delisted` | 无 | `skills` |
| `notifications/announcements.py::fan_out`（后台公告） | notice | `announcement` | `announcement:{id}` | 公告的 `link` |

`emit` 与 `add_notification` 用同一个 `source_key` 时只留一条（例如投稿完成：站内先写，推送入队时复用同一行）。授权失效有两条不同粒度的记录：工作空间广播（`user_id` 空，来自保活扫描）和发起者个人记录（来自推送路径），两者 `source_key` 不同，故意不合并。

问题被回答/取消/被新一轮替代时，`events.cancel_event` 同时给 inbox 行打 `resolved_at` 和 `read_at`，角标随之消失。

提交后总线发 `inbox.updated`，`data.userId` 为收件人，WebSocket 按用户分发；客户端收到后重新拉 `/api/inbox/unread`。事件不带正文。

## `link` 结构（客户端白名单解析）

```json
{"kind": "session",     "workspaceId": "…", "sessionId": "…", "panel": "desktop", "control": true}
{"kind": "cron",        "workspaceId": "…", "jobId": "…"}
{"kind": "auth_center", "workspaceId": "…", "jobId": "…"}
{"kind": "skills",      "workspaceId": "…"}
{"kind": "admin_skills"}
{"kind": "topic",       "slug": "release-2026-09"}
{"kind": "url",         "url": "https://…"}
{"kind": "inbox"}
```

- `panel` / `control` 只在 `session` 上出现，与 Web `paths.desktopTakeover`、App `Paths.workbench(control:)` 对齐。
- 未知 `kind` 一律回退为打开消息中心并高亮该条；客户端不得执行 `link` 之外的任何 URL。
- `url` 只有第一方公告/专题可用，服务端保存时校验：必须 `https`，主机在 `cors_origins` 的主机或环境变量 `ANNOUNCEMENT_LINK_HOSTS`（逗号分隔）内。会话来源的记录若带 `url` 直接被拒（`inbox.validate_link`）。
- 跨工作空间：点击前先确认用户仍是该空间成员、会话仍可访问，再切换作用域跳转；不可用时留在消息中心并提示。

## 用户接口

需要 Bearer；Web 与 App 相同；`X-Workspace-Id` 缺省用默认工作空间。可见范围 = 用户自己的记录（所有仍为成员的工作空间 + 账号级）+ 当前工作空间的广播记录；过期记录不返回。

| 方法与路径 | 说明 |
| --- | --- |
| `GET /api/inbox?category=&unread=&cursor=&limit=` | 游标分页（`limit` 1–100，默认 30）。返回 `items`、`nextCursor`、`unread` |
| `GET /api/inbox/unread` | `{"total": n, "session": n, "system": n, "notice": n}` |
| `POST /api/inbox/{id}/read` | 幂等；不可见的 id 返回 404 |
| `POST /api/inbox/read-all?category=` | 返回 `{"updated": n}` |
| `GET /api/topics/{slug}` | **无需登录**；只返回已发布专题，草稿与非法 slug 一律 404 |

`items[]`：

```json
{"id": "ntf_…", "category": "session", "kind": "task_completed", "title": "任务完成", "body": "《制作视频》已完成，查看结果。",
 "link": {"kind": "session", "workspaceId": "…", "sessionId": "…"}, "workspaceId": "…", "announcementId": null,
 "readAt": null, "resolvedAt": null, "expiresAt": null, "createdAt": "2026-09-11T10:00:00+00:00"}
```

坏游标返回 400 `INBOX_BAD_CURSOR`。专题返回 `id, slug, title, coverUrl, contentMd, ctaLabel, ctaLink, publishedAt, updatedAt`。

旧接口 `GET /api/notifications` / `POST /api/notifications/{id}/read` 保留一个 App 发版周期（授权中心条幅在用），已额外返回 `category` 与 `link`，并过滤过期记录；它只看当前工作空间，不含账号级公告。

## 推送

`push_messages.payload` 新增 `notificationId`。App 点击通知：先按 `notificationId` 标已读，再按该记录的 `link` 路由；取不到记录时退回按 `type` 路由（现有逻辑）。

公告推送走同一个 outbox：`kind="notice"`，标题正文为公告字段，`guard={"kind":"announcement","id":…}`；worker 领取和发送前都校验公告仍为 `published` 且未过期，撤回即取消未发的。TTL 取公告 `expires_at` 与 24h 的较小值。前台抑制等规则不变。

## 超管接口（`/api/admin/messages`）

平台 `admin` 角色，服务端每次读库校验（与推送测试台同一个 `live_admin`）。所有写操作记审计。

公告：

| 方法与路径 | 说明 |
| --- | --- |
| `GET /announcements?status=&limit=` | 列表，按创建时间倒序 |
| `POST /announcements` | 建草稿；返回带 `recipientCount`（按受众实时统计） |
| `GET /announcements/{id}` | 详情 + `recipientCount` |
| `PUT /announcements/{id}` | 仅 `draft` / `scheduled` 可改；已发布返回 409 `MESSAGE_ALREADY_PUBLISHED` |
| `POST /announcements/{id}/publish` | `publishAt` 在未来 → `scheduled`，由 janitor 每分钟检查到点发布；否则立即 `published` 并后台扇出 |
| `POST /announcements/{id}/revoke` | `scheduled` / `published` → `revoked`；未读记录立即过期，返回 `hidden` 数 |
| `POST /announcements/{id}/preview` | 只发给调用的超管本人一条（标题加「预览 · 」前缀），公告勾选推送时同样入队 |

请求体：

```json
{"title": "版本更新", "body": "新版本已上线", "link": {"kind": "topic", "slug": "release-2026-09"},
 "audience": {"kind": "all"}, "push": false, "publishAt": null, "expiresAt": "2026-10-01T00:00:00Z"}
```

`audience`：`{"kind":"all"}` / `{"kind":"users","ids":[…]}`（1–5000）/ `{"kind":"workspace","id":…}`（成员；记录挂在该空间下）/ `{"kind":"role","role":"admin"|"user"}`。扇出按用户 id 顺序每批 500 人，`source_key` 唯一约束保证重跑幂等；`fanoutCount` 是库里实际记录数（不含预览）。发布后注册的用户不补发。

专题：

| 方法与路径 | 说明 |
| --- | --- |
| `GET /topics`、`GET /topics/{id}` | 含 `status`、`createdBy` |
| `POST /topics` | `slug` 2–64 位小写字母/数字/连字符，唯一（冲突 409 `TOPIC_SLUG_TAKEN`）；`coverUrl` 须 https；`ctaLabel` 与 `ctaLink` 必须成对；`contentMd` ≤ 200k |
| `PUT /topics/{id}` | 已发布的专题不能改 `slug`（409 `TOPIC_SLUG_LOCKED`） |
| `POST /topics/{id}/publish`、`POST /topics/{id}/unpublish` | 发布后 `/api/topics/{slug}` 才对外可见 |

校验失败统一 422 `{"code":"MESSAGE_INVALID","message":…}`。

## 部署

- `uv run alembic upgrade head` 到 `a1c2e3b4d5f6`；backend Dockerfile 启动时自动跑。已在本机 PostgreSQL 16 从空库升到 head、降回 `e4f6a8b0c2d4`、再升，三步均通过。
- 可选环境变量 `ANNOUNCEMENT_LINK_HOSTS=bossip.example,www.bossip.example`；不设时只允许 `cors_origins` 里的主机。
- 新增后台任务 `InboxJanitor`（每 60s：发布到点的定时公告；每 24h：清理）。多实例同时跑是安全的：发布与扇出都幂等。

## 验证

- `tests/unit/test_inbox.py`：link 推导与白名单、跨空间可见性、`source_key` 幂等、了结即已读、游标分页、保留策略。
- `tests/integration/test_message_center.py`：业务事件落 inbox 且推送指回；回答问题后角标清零；提交后 `inbox.updated` 事件；公告扇出幂等/撤回/受众定向/外链白名单/推送与撤回守卫/定时发布；专题草稿不可见、发布后公开、slug 锁定；旧 `/api/notifications` 仍可用。
- 现有推送、授权中心、技能审核相关 124 项回归全过；PostgreSQL 版推送测试（`PUSH_TEST_DATABASE_URL`）通过。

## App 端（M3，已实现）

| 位置 | 文件 | 说明 |
| --- | --- | --- |
| 模型 | `shared/models/inbox.dart` | `InboxItem / InboxLink / InboxUnread / InboxPage / TopicPage` |
| 传输与状态 | `features/inbox/api/inbox_api.dart` | `InboxApi`；`inboxUnreadProvider`（WS `inbox.updated` 失效 + 2 分钟轮询）；`inboxFeedProvider(category)` 游标分页、本地乐观已读 |
| 链接解析 | `features/inbox/state/inbox_navigator.dart` | `InboxNavigator(read, router).open(link)`：白名单 kind；`session` / `cron` / `auth_center` / `skills` 先校验用户仍在该工作空间、会话仍可读（`GET /api/agent/session/{id}` 带 `X-Workspace-Id`），再切作用域跳转；`topic` 进站内专题页；`url` 仅 https，系统浏览器打开；未知 kind 回到消息中心；失败返回 `unavailable` 由调用方提示 |
| 页面 | `features/inbox/inbox_screen.dart`、`widgets/inbox_item_tile.dart` | 四个分栏带未读数，下拉刷新，滚动到底自动加载更多，全部已读，点击先标已读（响应里的 link 为准）再路由；跨空间记录显示空间 chip |
| 专题页 | `features/inbox/topic_screen.dart` | `/app/topics/:slug`，封面 + 标题 + `gpt_markdown` 正文 + CTA（同一解析器） |
| 入口 | `features/workspace/widgets/session_drawer.dart` | 抽屉「消息中心」行在授权中心之上，角标为跨空间未读总数（>99 显示 99+） |
| 路由 | `shared/router/paths.dart`、`app/router.dart` | `Paths.inbox = /app/inbox`（可带 `?category=`），`Paths.topic(slug)` |
| 推送点击 | `app/notification_host.dart::_openFromInbox` | payload 有 `notificationId` → `POST /api/inbox/{id}/read` 拿到 link → `InboxNavigator`；读不到（旧推送 / 记录已删）→ 退回原按 `type` 路由；目标不可用 → 打开消息中心并 toast。前台收到业务推送时立即刷新角标 |
| 文案 | `assets/locales/*/inbox.json`、`workspace.json` 的 `inbox / inboxHint` | 与 `frontend-v2/src/locales` 逐字一致（M2 直接使用） |

未改动：授权中心的通知条幅仍读旧 `/api/notifications`（兼容窗口内），超管控制台仍为四栏（公告编辑属 M4）。

验证：`flutter analyze` 无问题；locale 逐字节校验与 800 行门禁通过；新增测试 17 项（`test/features/inbox/*`、`test/app/notification_host_inbox_test.dart`、`test/features/workspace/session_drawer_inbox_test.dart`）；全量 Flutter 测试 338 过、2 失败与 origin/main 一致（`suggestion_composer_test` 大字号布局，与本次无关）。真机推送点击 → 标已读 → 进会话/专题，待上线前与 M5 一起验收。

