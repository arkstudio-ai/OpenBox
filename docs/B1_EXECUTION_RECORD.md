# B1 Workspace 审计执行记录

日期：2026-09-03

分支：`codex/b1-workspace-audit`

基线：`main@b8b68e0`

## 结论

B1 已按 `B1_WORKSPACE_AUDIT.md` 的默认协作口径实现：workspace 是业务租户边界；成员可查看同 workspace 会话，非会话发起人只能只读打开；其它单条业务资源仍保持 owner 写权限。A1 的云桌面与沙箱边界未纳入本次修改。

## 交付范围

- 新增 workspace、成员、邀请、内部任务状态模型与两段 Alembic 迁移。
- 为用户建立默认 workspace，并为 7 张业务表回填、约束和索引 `workspace_id`。
- 新增统一 workspace 请求守卫；缺少请求头时使用用户默认空间，无权限或无效空间返回 403。
- 会话列表/读取按 workspace 开放，全部会话写操作保持 owner-only，并返回稳定错误码 `SESSION_READ_ONLY`。
- 项目、资源、定时任务、技能、记忆和视频生产读写路径加入 workspace 作用域。
- 新增邀请、成员管理、三个只读 admin 接口和审计事件。
- 新增数据库条件抢占的内部任务框架，包含成功、失败退避和卡死恢复。
- 前端新增工作区选择器、团队设置、邀请接受页和只读会话状态；单 workspace 时隐藏选择器。

## 验收结果

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| AC-1 默认空间 | 通过 | PostgreSQL 注册链路验证 owner、member、默认项目在同一事务正确落库 |
| AC-2 租户隔离 | 通过 | 无成员关系和无效 `X-Workspace-ID` 均为 403；缺少请求头使用默认空间返回 200 |
| AC-3 邀请与只读协作 | 通过 | 邀请 201、接受 200、成员读取 200、成员写入 403/`SESSION_READ_ONLY`、过期邀请 410 |
| AC-4 审计 | 通过 | 本地 HTTP 流程产生 8 条可查询审计事件 |
| AC-5 admin | 通过 | `/api/admin/users`、`/api/admin/internal-tasks`、`/api/admin/audit` 均返回 200；非 admin 覆盖在单测中 |
| AC-6 内部任务 | 通过 | 两个后端进程共享 PostgreSQL/Redis 连续运行超过 60 秒；`noop` 最终 `last_status=ok`、`running_at=NULL`、失败数 0 |
| AC-7 迁移 | 通过 | 空库升级；带业务数据 downgrade/upgrade/re-upgrade；单一 Alembic head `d4f6a8b0c2e5` |
| AC-8 后端测试 | 通过 | 全量单测除 4 个依赖本机 `openbox.json` 视频模型配置的既有用例外全绿；B1 新增定向用例全绿 |
| AC-9 前端 | 通过 | `npm run check` 全绿；Vitest 27 文件、189 用例通过 |

## PostgreSQL 迁移验证

在临时 PostgreSQL 16 上完成以下验证：

1. 从空数据库执行 `alembic upgrade head`。
2. 降级至 `b2d4f6a8c0e2`，在 7 张业务表分别插入带 owner 的数据，再升级至 head。
3. 再次 downgrade B1 两段迁移并 re-upgrade；用户默认空间空值数和 7 张业务表 `workspace_id` 空值总数均为 0。
4. `alembic heads` 只有 `d4f6a8b0c2e5`。

迁移压测：在 `c3e5f7a9b1d4` 状态构造 10,000 用户、10,000 workspace/项目和 100,000 会话，执行第二段迁移耗时 `real 1.95s`（本机 Docker PostgreSQL 16）；迁移后 100,000 会话的 `workspace_id` 空值数为 0。该结果仅用于估算，生产耗时仍取决于硬件、锁竞争与真实表宽度。

## 真实 HTTP / UI 记录

HTTP 状态汇总：

```text
pre_accept_denied=403
outsider_denied=403
accept=200
member_read=200
member_write_denied=403
write_error_code=SESSION_READ_ONLY
missing_header_defaults=200
invalid_header_denied=403
expired_invitation_denied=410
admin_users=200
admin_internal_tasks=200
admin_audit=200
non_manager_role_change_denied=403
owner_promote_member=200
owner_demote_member=200
owner_remove_member=200
removed_member_access_denied=403
```

浏览器验证覆盖：所有者团队页、真实邀请链接与接受结果、成员的双 workspace 选择器、成员查看他人会话时隐藏输入框并显示只读提示。验收过程中额外发现并修复了两个仅在真实 PostgreSQL/UI 流程暴露的问题：注册事务的外键 flush 顺序，以及新成员进入共享空间时重复创建默认项目。

截图：

- [团队成员与角色](evidence/b1/team-settings.jpg)
- [邀请接受页](evidence/b1/invitation-accept.jpg)
- [工作区选择器](evidence/b1/workspace-switcher.jpg)
- [跨成员只读会话](evidence/b1/member-readonly-session.jpg)

## 已知基线例外

下列 4 个视频模型用例读取本机项目外的 `openbox.json`/relay 配置，在当前 `main` 基线同样不稳定；B1 未修改对应视频模型配置逻辑，最终全量命令通过 `--deselect` 明确排除：

- `test_video_open_generation.py::test_an_undeclared_model_still_gets_a_channel_wide_duration_guard`
- `test_video_open_generation.py::test_minimax_keeps_its_own_resolution_vocabulary`
- `test_video_production.py::test_a_declared_relay_model_keeps_its_id_and_takes_the_metadata_shape`
- `test_video_production.py::test_bossip_relay_submit_and_status_use_v1_videos`

## 边界检查

本次没有修改以下 A1/高冲突路径：

- `backend/sandbox/*`
- `backend/api/desktop.py`
- `backend/db/models/cloud_desktop.py`
- `backend/scripts/wuying_*`
- `backend/container/*`
