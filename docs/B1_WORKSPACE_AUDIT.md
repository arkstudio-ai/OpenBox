# B1 · Workspace + 审计 + 内置周期任务原语 —— 独立执行单

> 2026-09-03。从 `DETAILED_PLAN_M1_M2.md` v2 的 B1 抽出，给 Codex 目标模式单独执行，**与 A1（`A1_DESKTOP_CHANNEL.md`）并行**。
> 本文自包含：目标、现状、必要资料、方案、验收条件、测试方式、交付证据清单、与 A1 的冲突规则。
>
> **执行者须知**：只做本文范围内的事。§1.2 的「非目标」和 §9 的冲突规则是硬约束，碰到就停下来报告。
> 本项不花钱、不改生产配置；上线部署由用户做。

---

## 1. 目标

**一句话**：给 openbox 加上「工作空间」这一层——用户、成员、邀请三张表，所有按人归属的数据挂到 workspace 下；审计表真正落写；后台接口有骨架；并提供一个内置周期任务注册原语，供后续对账、巡检、探活使用。

**完成定义**：
1. 每个现有用户和新注册用户都有一个默认 workspace（owner = 自己）；邀请 → 接受 → 成员能看到该 workspace 的会话列表。
2. `sessions / projects / file_assets / cron_jobs / user_skills / user_memories / video_productions` 都带非空 `workspace_id`，回填完成，读写路径按 workspace 过滤。
3. `audit_logs` 有写入：注册、登录失败锁定、成员变更、后台操作。
4. `/api/admin/*` 三个只读接口可用，非 admin 403。
5. `cron/internal_tasks.py` 可注册周期任务，两个进程只有一个执行。
6. 前端有「团队」设置页与 workspace 切换器；所有请求带 `X-Workspace-Id`；缺头 = 默认 workspace。
7. 单测全绿，含 alembic 路径的回填测试；`npm run check` 通过。

### 1.2 非目标（不要做）
- **不碰 `cloud_desktops`**（表、模型、迁移、索引都不动）——这是 A1 的地盘；`cloud_desktops.workspace_id` 留到 A1 合并后的接缝小迁移。
- 不进 `backend/sandbox/*`、`api/desktop.py`、`scripts/wuying_*`、`container/*`、`agent/loop.py` 的桌面路由部分。
- 不做计费表（B2）、不做运营角色表（里程碑三）、不做移动端（缺头 = 默认即可）。
- 不把现有 reaper / warmup / 视频恢复三处 piggyback 迁到新原语（可以，但不在本项）。
- 会话协作只做「成员可见、只读打开」（见 §4.5，需确认），不做多人写同一会话。

---

## 2. 现状（2026-09-03 实查）

| 事实 | 位置 |
|---|---|
| 没有任何 workspace / 团队 / 组织概念；租户单位是 `users.id`，唯一分组是 `projects` | 全库 grep；`db/models/memory.py:20` 只有一句注释 |
| `users` 列：`id, username, email, password_hash, avatar_url, role String(16) default "user", is_active, oauth_provider, oauth_id, failed_login_count, locked_until, monthly_cost_limit, is_deleted, deleted_at, created_at, updated_at`。`role` 只有 `user / admin` 两个值在用，无枚举无约束 | `backend/db/models/user.py:11-30` |
| `get_current_user` 返回 `{"user_id", "role"}`；单用户模式（无 `JWT_SECRET`）恒为 `{"default","admin"}`；`require_admin` 只在 `api/containers.py` 三处用 | `backend/auth/middleware.py:29-78` |
| `audit_logs` 表与 `PgAuditRepo.create/list_by_user` 存在，**生产代码零调用**；列 `id, user_id, action, resource_type, resource_id, details JSON, ip_address, user_agent, created_at`，没有 `workspace_id` | `backend/db/models/audit_log.py`、`backend/db/repository/audit_repo.py` |
| 会话仓库全部按 `user_id` 过滤：`get(session_id, user_id)`、`list_by_user`、`update`、`soft_delete`、`count_by_user` | `backend/db/repository/session_repo.py:11-74` |
| 会话 API 在 `/api/agent/session*`，路由清单见 `api/sessions.py:176-740`；`api/projects`、`api/files`、`api/cron`、`api/memories`、`api/video_productions`、`api/assets` 同样按 user 过滤 | `backend/main.py:230-284` 注册 |
| 内置周期任务没有注册点：reaper / warmup / 视频恢复三处 piggyback 在 `on_timer` 的 `finally` 里；cron 作业的单实例靠 `_claim_job` 的条件 UPDATE（`running_at` 为空或超过 `STUCK_RUN_MS`） | `backend/cron/timer.py:127-147, 304-330` |
| 迁移：alembic，`backend/db/migrations/versions/`，当前单头 `b2d4f6a8c0e2`（cloud_desktops）。单测库是 sqlite 内存 + ORM `create_all`，**不跑 alembic**；已有一个迁移单测的写法可照抄 | `backend/alembic.ini`、`tests/conftest.py:15-26`、`tests/unit/test_remove_skill_runtime_migration.py` |
| `JSONType` 在 PG 是 JSONB、其它库是 TEXT；部分唯一索引要同时给 `postgresql_where` 和 `sqlite_where` | `backend/db/base.py:27-46`、`db/models/cloud_desktop.py:29-35` |
| 前端：设置页 tabs `["account","usage","models","browser","appearance"]`；API 客户端 `doFetch` 统一加 `Authorization`，`request()` 一次 401 刷新重试；用户态在 zustand `auth-store.ts`；locale 12 个命名空间（zh-CN / en-US），`npm run check` = i18n 对齐 + lint + tsc + 单测 | `frontend-v2/src/features/settings/tabs.ts`、`src/shared/api/http.ts:47-70`、`src/shared/api/auth-store.ts`、`src/locales/*` |
| 移动端（Flutter，`mobile/`）同一套 API，无 `X-Workspace-Id`，且没有 locale 对齐脚本 | `mobile/assets/locales/*`、`mobile/scripts/` |
| bossip 参照（只看结构，不搬代码）：`Workspace / WorkspaceMember(OWNER|ADMIN|MEMBER, INVITED|ACTIVE|SUSPENDED|REMOVED) / WorkspaceInvitation(tokenHash, expiresAt)`；每次请求 `resolveForRequest` + `assertMember`；`lastActiveWorkspaceId` 只是 UX 指针；邀请流 bossip 也只有表没有前端 | `~/arkstudio/bossip/apps/center/prisma/schema.prisma:113-172`、`src/web/workspace.service.ts` |

---

## 3. 必要资料

| # | 资料 | 用途 | 状态 |
|---|---|---|---|
| 1 | 本机能起后端单测（`uv run pytest tests/unit`）与前端 `npm run check` | 验收 | 用户确认 |
| 2 | 本地全栈（postgres 5433 + redis + `JWT_SECRET`），起法：`uv run python scripts/backend_entrypoint.py --port 8080`，前端 `.claude/launch.json` 的 frontend-v2 | 端到端验收 AC-1/6 | 用户确认可用 |
| 3 | 生产库一份脱敏的 `users` 行数与 `sessions` 行数（回填耗时评估） | 迁移 | 用户提供 |
| 4 | 会话协作范围拍板：成员只读打开 vs 可写（§4.5） | AC-1 判据 | **需确认**，默认只读 |
| 5 | A1 分支名与其迁移 revision（若已存在），用于 §9 的合并顺序 | 冲突规则 | 用户提供 |

---

## 4. 方案

### 4.1 数据（两个迁移）

**迁移 ①：新表 + 审计列 + 用户默认空间**
- `workspaces(id String(64) pk, name String(128), owner_user_id FK users.id, plan_id String(32) nullable, kind: personal|team, created_at, updated_at, is_deleted bool default false, deleted_at)`
- `workspace_members(workspace_id FK, user_id FK, role: owner|admin|member, status: active|invited|removed, invited_by nullable, created_at, updated_at)`，主键 `(workspace_id, user_id)`；索引 `(user_id, status)`
- `workspace_invitations(id pk, workspace_id FK, target String(255)  # email 或 username, role, token_hash String(64) unique, expires_at, accepted_by nullable, accepted_at nullable, created_by, created_at)`
- `users.default_workspace_id String(64) nullable`
- `audit_logs.workspace_id String(64) nullable` + 索引 `(workspace_id, created_at)`
- 回填：为每个 `users` 行建 `workspaces(id=新 id, name=username, owner=自己, kind=personal)` + `workspace_members(owner, active)`，写回 `users.default_workspace_id`。单用户模式的 `default` 用户同样处理（`db/base.py::_seed_single_user_scope` 要同步建默认空间）。
- `downgrade`：删三表与两列。

**迁移 ②：业务表加 `workspace_id`**
- 对 `sessions, projects, file_assets, cron_jobs, user_skills, user_memories, video_productions`：加 `workspace_id String(64) nullable` → 按 `user_id → users.default_workspace_id` 回填 → 改非空 → 加索引 `(workspace_id, is_deleted)`（有 is_deleted 的表）。
- **不包含 `cloud_desktops`**。
- `downgrade`：删列。
- 其它表（`messages/parts/video_jobs/image_gen_cache/todos/prompt_history/skill_installs/permission_rules`）经 `session_id` 或 `user_id` 推导，不加列。

### 4.2 请求上下文与守卫
- `auth/workspace.py`：
  - `get_workspace(request, user=Depends(get_current_user)) -> dict{"id","role"}`：读头 `X-Workspace-Id`；缺头 → `users.default_workspace_id`；查 `workspace_members(status=active)`，非成员 403 `{"code":"WORKSPACE_FORBIDDEN"}`；结果缓存到 `request.state.workspace`。
  - `require_workspace_role(*roles)` 依赖工厂。
  - 单用户模式：返回 `default` 用户的默认空间，role owner。
- `users.role` 接受 `developer`（本项只放行取值，不做豁免逻辑，那是 B2/B4）。

### 4.3 业务读写按 workspace
- 会话：`POST /api/agent/session` 创建时写 `workspace_id`（来自 `get_workspace`）；`GET /api/agent/session` 列表改为「本 workspace 内的会话」，返回体加 `user_id`、`workspace_id`；`GET /session/{id}`、`/message` 读取允许同 workspace 成员；**写操作**（发消息、删除、fork、revert、abort 等）仍限 `user_id == owner`（§4.5 需确认）。
- `projects / file_assets / cron_jobs / user_skills / user_memories / video_productions`：创建写 `workspace_id`；列表按 workspace；单条读写沿用「owner」规则，不放宽。
- 仓库层改法：`session_repo.list_by_user` 保留，新增 `list_by_workspace(workspace_id, ...)`；不要在这一轮把所有 repo 签名都改掉，只加需要的方法，避免和 A1 撞 `sandbox/manager.py` 之类的调用点。

### 4.4 审计
- `audit/__init__.py::record(actor_user_id, workspace_id, action, target_type, target_id, detail: dict | None, request: Request | None)`：走 `PgAuditRepo.create`，自动带 ip/ua；失败只打日志不抛。
- 调用点（本项只接这些）：`auth/routes.py` 注册成功、登录失败触发锁定；`api/workspaces.py` 邀请/接受/移除/角色变更；`api/admin.py` 每个接口。**桌面开通/删除、技能发布/卸载两处等 A1 合并后由 A1 或后续补一行**，本项不进那些文件。
- `action` 命名：`auth.register`、`auth.lockout`、`workspace.invite`、`workspace.accept`、`workspace.remove_member`、`workspace.change_role`、`admin.view_users`、`admin.view_workspace`、`admin.view_audit`。

### 4.5 会话协作范围（需确认，默认如下）
- 成员可见 workspace 内所有会话（列表 + 只读打开消息）。
- 发消息 / 删除 / fork / revert 等写操作只允许会话 owner；其它成员调用返回 403 `{"code":"SESSION_READ_ONLY"}`。
- 理由：多人写同一会话涉及沙箱租约、权限门、记忆归属，超出 B1；先把边界立住。

### 4.6 后台骨架
- `api/admin.py`，前缀 `/api/admin`，全部 `Depends(require_admin)`：
  - `GET /users?q=&offset=&limit=`（id、username、email、role、created_at、default_workspace_id）
  - `GET /workspaces/{id}`（基本信息 + 成员列表）
  - `GET /audit?workspace_id=&action=&offset=&limit=`
- 每个接口写一条审计。

### 4.7 内置周期任务原语
- `cron/internal_tasks.py`：
  - `register(name: str, interval_sec: int, fn: Callable[[], Awaitable[None]])`，进程启动时注册（`main.py` lifespan 里一处集中注册）。
  - 表 `internal_task_state(name pk, running_at nullable, last_run_at nullable, last_status: ok|error, last_error text, backoff_until nullable, updated_at)`（放进迁移 ①）。
  - `tick()`：对每个注册项，若 `now - last_run_at ≥ interval` 且不在退避，用一条条件 UPDATE 抢 `running_at`（照 `_claim_job` 的写法，超时 `STUCK_MS` 可重抢）；抢到才执行；异常记 `last_error` 并按 `min(interval*2^n, 1h)` 退避。
  - 接入：在 `cron/timer.py` 的 `finally` 里**只加一行** `await internal_tasks.tick()`（放在现有三处之后），不改现有三处。
  - `GET /api/admin/internal-tasks` 列出状态（用 §4.6 的 admin 路由）。
- 用一个 `noop` 任务验证：两个后端进程同时跑，`internal_task_state.last_run_at` 每个周期只前进一次。

### 4.8 前端
- `shared/api/workspace-store.ts`：当前 workspace id（持久到 localStorage），登录后从 `GET /api/workspaces` 取列表并选默认。
- `http.ts::doFetch` 加头 `X-Workspace-Id`（有值才加）。
- 设置页新 tab `team`：成员表（用户名、角色、状态、加入时间）、邀请表单（用户名或邮箱 + 角色）、待接受邀请列表、移除/改角色（owner/admin 才显示）；接受邀请页 `/invite/{token}`。
- 侧栏顶部 workspace 切换器：只有一个 workspace 时隐藏。
- 会话列表项显示发起人（非本人时）；只读会话隐藏输入框并提示。
- locale：`settings.json`、`common.json`、`errors.json` 加 key，zh-CN / en-US 同步；移动端 locale 本项不改（新 key 只在 web 用）。

---

## 5. 验收条件

| 编号 | 条件 | 判据 |
|---|---|---|
| AC-1 | 默认空间 | 迁移后每个 `users` 行都有 `default_workspace_id`，对应 `workspaces.owner_user_id` = 自己，`workspace_members` 有 owner/active 行；新注册用户同样自动拥有 |
| AC-2 | 回填 | 七张业务表 `workspace_id` 非空且等于各自 `user_id` 的默认空间；`downgrade` 后再 `upgrade` 幂等 |
| AC-3 | 邀请链路 | U1 在团队页邀请 U2（用户名）→ U2 打开 `/invite/{token}` 接受 → U2 切到 U1 的 workspace 能看到 U1 的会话列表，打开为只读；U2 发消息得到 403 `SESSION_READ_ONLY`；过期 token 接受 → 410 |
| AC-4 | 隔离 | U2 带 `X-Workspace-Id=<U1 空间>` 但未被邀请 → 403 `WORKSPACE_FORBIDDEN`；缺头 → 落到 U2 自己的默认空间 |
| AC-5 | 审计 | 注册、锁定、邀请、接受、移除、后台查询各产生一条 `audit_logs`，带 `workspace_id`、ip、ua |
| AC-6 | 后台 | admin 用户三个接口返回正确；普通用户 403；单用户模式（无 JWT_SECRET）admin 接口可用 |
| AC-7 | 周期任务 | 两个后端进程同时运行 60 秒，`noop` 任务的 `last_run_at` 推进次数 = 周期数（不是 2 倍）；把 fn 改成抛异常后 `last_status=error`、`backoff_until` 递增 |
| AC-8 | 单测 | `uv run pytest tests/unit` 全绿（既有 3 个依赖本机 openbox.json 的失败除外）；新增：迁移 ①② 的回填（alembic 路径，照 `test_remove_skill_runtime_migration.py`）、成员校验、邀请过期、只读会话、`internal_tasks` 抢占与退避、单用户模式默认空间 |
| AC-9 | 前端 | `npm run check` 通过；团队页与切换器可用；只读会话无输入框 |
| AC-10 | 不越界 | `git diff --stat main` 不含 `backend/sandbox/*`、`api/desktop.py`、`db/models/cloud_desktop.py`、`scripts/wuying_*`、`container/*`；alembic `heads` 只有一个 |

---

## 6. 测试方式

```bash
cd backend && uv run pytest tests/unit -q
cd backend && uv run alembic heads          # 必须只有一个
cd frontend-v2 && npm run check
```

端到端（本地全栈）：注册 U1、U2 → U1 建两个会话 → 邀请 U2 → 接受 → U2 切换空间看列表、只读打开、尝试发消息 → admin 用户查三个后台接口 → 两个后端进程（不同端口）跑 60 秒看 `internal_task_state`。

回填压测：在本地 PG 造 1 万 `users` + 10 万 `sessions`，跑迁移 ②，记录耗时（生产回填是否需要分批的依据）。

---

## 7. 交付证据清单

1. `git log --oneline main..<branch>`、`git diff --stat main`、`uv run alembic heads` 输出。
2. `pytest` 与 `npm run check` 完整输出。
3. 迁移前后 SQL 核对：`SELECT count(*) FROM users WHERE default_workspace_id IS NULL`（=0）；七张表 `count(*) WHERE workspace_id IS NULL`（全 0）。
4. AC-3 的 HTTP 记录：邀请、接受、U2 列表、U2 只读打开、U2 发消息 403、过期 token 410。
5. AC-4 两个响应体。
6. `SELECT action, workspace_id, ip_address FROM audit_logs ORDER BY created_at` 截取。
7. AC-6 三个接口的响应（admin 与非 admin 各一次）。
8. AC-7 的 `internal_task_state` 行两次快照（间隔 60 秒）与两个进程的日志。
9. 团队页、切换器、只读会话三张截图。
10. 回填压测耗时。
11. §8 填好。

---

## 8. 执行记录（执行者填写）

- 分支 / 提交：
- 迁移 revision ①、②：
- 会话协作范围是否按 §4.5 默认执行：
- 偏离本文之处（必须写）：

---

## 9. 与 A1 并行的冲突规则（硬约束）

1. **不碰 `cloud_desktops`**：不加列、不改索引、不建关系。A1 合并后另开「接缝迁移」加 `workspace_id` 并把 `sandbox/ownership.py::owner_for()` 改为返回默认空间——那是第三个小任务，不在本项。
2. **文件边界**：本项不进 `backend/sandbox/*`、`backend/api/desktop.py`、`backend/scripts/wuying_*`、`backend/container/*`、`backend/agent/loop.py` 的桌面路由段；A1 不进 `db/models/`（除 `cloud_desktop.py`）、`auth/*`、`api/workspaces.py`、`api/admin.py`、`cron/internal_tasks.py`。
3. **alembic 单头**：本项迁移 ① 的 `down_revision = b2d4f6a8c0e2`；若 A1 先合并且带迁移，本项 rebase 后把 `down_revision` 改成 A1 的 revision；合并前 `alembic heads` 必须只有一个。
4. **`cron/timer.py` 只加一行**（`internal_tasks.tick()`），A1 不改这个文件。
5. 审计的桌面两处调用点不在本项接。
6. 发现必须改上述任一边界文件才能过验收 → 停下来报告，不要自行跨界。
