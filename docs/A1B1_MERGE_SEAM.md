# A1+B1 合并与归属接缝 —— 独立执行单

> 2026-09-03。A1（`codex/a1-desktop-channel`，已上线 gw2）与 B1（`codex/b1-workspace-audit`）都已验收通过。
> 本项把两条分支合进 `main`，并完成「桌面归 workspace，一订阅一台」的归属接缝。规模小，一个会话做完。
> 执行者须知：只做本文范围；部署 gw2 前先报告；不要顺手做 A2/B2 的任何内容。

---

## 1. 目标

**完成定义**：
1. `main` 包含 A1 与 B1 全部提交，alembic 单头，测试全绿。
2. `cloud_desktops` 以 `workspace_id` 为归属键；一个 workspace 一台桌面；同一 workspace 的两个成员在同一台桌面上执行命令。
3. 桌面路由、发消息预检、云桌面 tab、开通接口全部按 workspace 解析；`owner_for()` 返回用户的默认 workspace。
4. 桌面开通 / 删除 / 撤销写审计。
5. gw2 部署并验收。

**非目标**：包月参数（A2）、池（A3）、计费（B2）、平台账号（A5）。不改 `X-Workspace-Id` 语义，不做多桌面。

---

## 2. 现状（验收人实查，2026-09-03）

- 试合并结果：A1 先合无冲突；再合 B1 有 **2 个冲突** —— `backend/api/sessions.py` 的 `send_message` 与 `send_message_async`（A1 的 `_desktop_route_preflight` 与 B1 的 `_require_session_owned` 同一位置）；`frontend-v2/src/features/chat/lib/content-view.ts` 两处纯注释。
- alembic 两个头：A1 `c1d3e5f7a9b2`（`down_revision=b2d4f6a8c0e2`），B1 `c3e5f7a9b1d4 → d4f6a8b0c2e5`（`c3e5f7a9b1d4.down_revision=b2d4f6a8c0e2`）。
- 解决后（B1 归属检查在前、A1 预检在后 + 单头）：后端 1282 通过、4 个既有视频配置失败；前端 189 通过。
- A1 的归属键仍是 `cloud_desktops.user_id`（非空，一人一台部分唯一索引 `ix_cloud_desktops_user_active`）；`sandbox/ownership.py::owner_for(user_id)` 原样返回 `user_id`；`WuyingProvider.get_user_container(owner)`、`api/sessions._desktop_route_preflight(user_id)`、`cron/executor` 的跳过、`wuying_desktop_service.status/provision(user_id)`、`api/desktop.py` 三个接口都以这个 owner 串起来。
- B1 给 `users.default_workspace_id`、`workspace_members`、`auth/workspace.py::get_workspace`（把 `workspace_id/workspace_role` 写进 `current_user`）；`audit/__init__.py::record(actor_user_id, workspace_id, action, target_type, target_id, detail, request)`。
- B1 留了两处审计未接：桌面开通/删除、技能发布/卸载（后者不在本项）。
- gw2：`WUYING_ROUTING=per_desktop`，库 revision `c1d3e5f7a9b2`，无真实用户桌面（A1 验收桌面已全删）。EC2 生产栈是旧镜像，`WUYING_ROUTING` 默认 shared，不受影响。

---

## 3. 必要资料
- 两条分支最新提交：A1 `6965784`，B1 `6622c61`（在 `/Users/wxy/https-github-com-arkstudio-ai-openbox-b1` 工作树）。
- gw2 部署方式与上次 A1 相同（用户确认）。
- 本地全栈（postgres + redis + `JWT_SECRET`）用于双成员验收。

---

## 4. 方案

### 4.1 合并
1. `main` ← A1（fast-forward 或普通 merge，中文提交信息）。
2. B1 rebase 到新 `main`：
   - `c3e5f7a9b1d4.down_revision` 改为 `c1d3e5f7a9b2`，**不要**用 `alembic merge` 造第三个空修订；
   - `sessions.py` 两处：先 `session = await _require_session_owned(session_id, current_user)`，再 `not_ready = await _desktop_route_preflight(...)`；
   - `content-view.ts` 任选一侧注释。
3. 合并后跑 `uv run alembic heads`（单头）、`uv run pytest tests/unit`、`npm run check`。

### 4.2 接缝迁移（一个新修订，`down_revision` = B1 的 `d4f6a8b0c2e5`）
- `cloud_desktops` 加 `workspace_id String(64) nullable` → 回填 `users.default_workspace_id`（按 `user_id`）→ 非空 → 加 FK。
- `user_id` 改可空（语义改为「谁触发的开通」，注释写明）。
- 删 `ix_cloud_desktops_user_active`，建 `ix_cloud_desktops_workspace_active`：`workspace_id` unique，`postgresql_where` 与 `sqlite_where` 都是 `is_deleted = false`。
- `downgrade` 反向可跑。
- 迁移单测照 `tests/unit/test_workspace_migrations.py` 的方式加一条。

### 4.3 代码
| 文件 | 改动 |
|---|---|
| `sandbox/ownership.py` | `owner_for(user_id)` → 查 `users.default_workspace_id`；没有则抛 `DesktopNotReady({"state":"no_workspace"})`。新增 `owner_for_request(current_user)`：优先 `current_user["workspace_id"]`（B1 守卫写入的），缺省再走默认空间 |
| `db/repository/cloud_desktop_repo.py`、`sandbox/wuying_desktop_service.py` | 所有按 `user_id` 查/建桌面的方法改按 `workspace_id`；`provision(workspace_id, triggered_by_user_id)` 写 `user_id` 为触发者；标签 `openbox-user` 改写为 workspace id 并新增 `openbox-workspace`（两者同值，兼容收养）；EndUser 派生改用 workspace id（`obx-<sha256(workspace_id)[:16]>`），`_adopt_from_tags` 同步 |
| `api/desktop.py` | `status/provision/ticket` 用 `owner_for_request(current_user)`；成员均可看画面与出票（出票校验标签 = workspace id） |
| `api/sessions.py`、`cron/executor.py`、`api/ws.py`、`api/browser.py`、`api/dev_browser.py`、`cron/warmup.py` | 取 owner 的地方统一换成 workspace（HTTP 走 `owner_for_request`，无请求上下文走 `owner_for`；cron 用作业行的 `workspace_id`） |
| `sandbox/wuying.py` | 不改路由逻辑，只确认 `get_user_container(owner)` 收到的是 workspace id |
| `audit` 调用 | `desktop.provision`、`desktop.release_ghost`、`desktop.revoke` 三处 `record(...)`，带 `workspace_id` |
| `frontend-v2` DesktopTab | 文案从「你的云电脑」改为「本空间的云电脑」（zh-CN / en-US），非 owner 成员也显示开通状态（开通按钮只给 owner/admin，`require_workspace_role`） |
| `docs/WUYING_SANDBOX.md` | per-user 章节改「per-workspace」，标签与 EndUser 派生规则更新 |

### 4.4 部署
- gw2：出镜像 → `alembic upgrade head` → 重启；因 gw2 没有真实桌面行，回填为空操作。EC2 不动。

---

## 5. 验收条件

| 编号 | 条件 | 判据 |
|---|---|---|
| AC-1 | 单头与测试 | `alembic heads` 一个；后端除既有 4 个视频配置用例外全绿；`npm run check` 通过 |
| AC-2 | 一空间一台 | U1 开通桌面后邀请 U2 入空间；U2 切到该空间发 `bash: hostname` 得到与 U1 相同主机名；云桌面 tab 出票 200 |
| AC-3 | 隔离 | U2 在自己的默认空间没有桌面 → `DESKTOP_NOT_READY`；U2 对 U1 空间的 provision 若非 owner/admin → 403 `WORKSPACE_ROLE_REQUIRED` |
| AC-4 | 并发 | U1、U2 同时发命令，两条都成功，action server 日志能看到租约串行 |
| AC-5 | 迁移 | 带一行 `cloud_desktops(user_id=U1)` 的库升级后 `workspace_id` = U1 默认空间；降级再升级幂等；旧索引不存在、新索引存在 |
| AC-6 | 审计 | 开通、撤销各一条 `audit_logs`，带 `workspace_id` |
| AC-7 | 收养 | 删掉 DB 行后 `status` 能按 `openbox-workspace` 标签收养回来 |
| AC-8 | gw2 | 部署后 revision = 新修订，`/health` ok，`WUYING_ROUTING=per_desktop`，用一个测试空间跑 AC-2 后删桌面 |
| AC-9 | 提交 | `main` 上：A1 提交、B1 提交（rebase 后）、接缝提交；提交信息中文 |

---

## 6. 测试方式
```bash
cd backend && uv run alembic heads && uv run pytest tests/unit -q
cd frontend-v2 && npm run check
```
端到端在本地全栈跑 AC-2/3/4/6/7（需要一台真桌面时在 gw2 跑，按量，测完删）。

## 7. 交付证据
1. `git log --oneline` 显示三段提交；`alembic heads` 输出。
2. pytest、npm check 输出。
3. AC-2 两个用户的聊天记录（hostname 相同）与 `/api/desktop/status` JSON。
4. AC-3 两个响应体。
5. AC-5 迁移前后 SQL 核对。
6. 审计行截取。
7. gw2 部署后 `/health`、revision、`docker ps`。
8. 测试桌面删除证明（`describe-desktops` 查不到）。

## 8. 执行记录（执行者填写）
- 合并提交 / 接缝修订：
- 偏离：
- 建了几台桌面、是否删除：

## 9. 停下来报告
- 冲突超出 §2 列的两处。
- 需要改 `WuyingProvider` 路由逻辑才能过验收。
- gw2 上出现真实用户桌面行（回填非空）。
