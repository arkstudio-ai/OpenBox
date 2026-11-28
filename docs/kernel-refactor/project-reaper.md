# 项目目录回收的核实（第 0 步 0-9）

日期 2026-11-28。对应 `docs/KERNEL_DSH_COMPARISON_20261128.md` 第 8 节最后一条、第 11 节"项目目录回收"一行、25.2 的 0-9。核对的是分支 `kernel-refactor/round1`（`a9da02c8`）上的代码；没有连任何桌面、服务器或数据库，结论里涉及线上的部分都标了【推断】。证据标记沿用主文档：【已核对】读过代码或跑过测试，【推断】由代码推出、没有线上数据。

## 结论

- **是真缺陷，而且比原来写的多一处。**
  1. 跨租户误移【已核对，测试复现】：回收拿全库所有用户的"已过期"slug 去比对一台桌面上的目录。工作区 B 的用户 30 天前删了项目 `foo`，工作区 A 的桌面上有一个数据库里 A 并不认识的 `/workspace/foo`（Agent `git clone` 出来的、手建的），而全库没有一个活着的 `foo`，这个目录就被移进回收站。
  2. 进回收站等于立刻永久删除【已核对，测试复现；文件系统行为见下文】：清理回收站按 `find -mtime +14` 判断"在回收站里放了多久"，但 `mv` 不改目录自己的 mtime。一个顶层两周没增删过文件的项目目录，移进回收站后在**同一次**回收里就满足 `-mtime +14`，紧接着被 `rm -rf`。"保留 14 天"实际上不成立。这条也覆盖用户在界面上主动删项目时移进回收站的目录（`project/workspace.py:399-408`），它们在下一次回收（5 分钟内）就可能被清掉。
- **修复已经在本分支，只会减少移动和删除，不会增加。** 比对改成只看拥有这台桌面的工作区的项目；归属说不清时整轮跳过；回收站按名字里记下的移入时间计算保留期。没有发布，线上在下一次发布之前还是旧逻辑。

## 修复前：它扫哪里，和什么比

【已核对】

- **什么时候跑**：cron 计时器每次 tick 的 `finally` 里调 `sweep_if_due()`（`cron/timer.py:139-140`），进程内每 5 分钟最多一次（`cron/reaper.py:21,27-38`）。
- **跑在哪台桌面上**：`_sweep_workspace` 调 `sandbox_manager.get_only_client()`（`cron/reaper.py:154-176`）。它只在本进程的 `_clients` 里**恰好一个**客户端时返回那一个，否则返回 None（`sandbox/manager.py:499-510`）。`_clients` 以工作区 id 为键（`_map_key(owner)`，owner 来自 `Session.workspace_id`，`sandbox/ownership.py:31-47`）；客户端只在会话被删除或 cron 临时会话结束时释放（`session/session.py:637`、`cron/executor.py:295`），所以忙的进程里客户端只增不减。【推断】线上 gw2 主要是在后端重启后、第二个工作区连上沙箱之前，对第一个连上的工作区的桌面跑；单租户或开发环境则一直在跑。
- **扫什么**：那台桌面上 `ls -1 /workspace` 的顶层条目，跳过以 `.` 开头的（`project/reclaim.py:74-87`，修复前行号）。在 `WUYING_ROUTING=per_desktop`（gw2）下，桌面属于一个工作区，多个席位共用，`/workspace` 是这个工作区所有成员共用的目录（Action Server 不按用户范围改写路径）。在 `WUYING_ROUTING=shared`（EC2 旧栈）下，所有工作区共用一台桌面。
- **和什么比**：`_live_slugs()`、`_expired_slugs()` 都没带参数，查的是全库：活着的项目 slug，和删除超过 7 天的项目 slug（`project/reclaim.py:31-57`，修复前）。函数有一个 `user_id` 参数，但没有调用方传它；而且按用户收窄"活着"的集合，会让同一工作区里另一个成员的同名活项目失去保护，所以它本身也不能用。
- **slug 的唯一范围**：唯一索引是 `(workspace_id, user_id, slug)`，只对未删除的行（`db/models/project.py:26-33`）。同一工作区的两个成员可以各有一个 `foo`，它们落在同一个 `/workspace/foo`。
- **判定**：名字在"活着"里就跳过；在"已过期"里就移走；都不在就记为 unknown 留着。

## "移进回收站"是什么意思

【已核对】

- 移动：`mkdir -p /workspace/.openbox/trash && mv /workspace/<name> /workspace/.openbox/trash/<name>-<UTC 时间戳> || true`。
- 能不能找回：只能有桌面权限的人手工 `mv` 回去。仓库里没有恢复接口，也没有界面。
- 保留期：同一次回收的最后一步是清理回收站：`find /workspace/.openbox/trash -maxdepth 1 -mindepth 1 -type d -mtime +14`，对结果逐个 `rm -rf`（`project/reclaim.py:108-128`，修复前）。`find -mtime` 看的是目录自己的 mtime；目录的 mtime 只在它的直接子项增删改名时变，改深层文件、改文件内容都不变。`rename(2)` 移动目录只更新它的 ctime，不改 mtime：本机 APFS 上实测如此（移动一个 mtime 为 1 月 1 日的目录后，它立刻出现在 `find -mtime +14` 的结果里）；Linux ext4 的 rename 实现也只更新 ctime【推断，按内核实现，没有在 Linux 上实测】。所以"保留 14 天"在实际上是"保留到目录顶层的最后一次增删满 14 天"，对一个被删了一周以上的旧项目，通常就是立刻删。
- 修复前 `_purge_trash` 返回的是 `find` 找到的全部路径，连被路径保护拒绝的也算进"purged"，日志里的数会偏大。

## 修复前会出事的情形

| 情形 | 修复前 | 证据 |
|---|---|---|
| 工作区 A 的桌面有 `foo`，A 不认识；B 很久前删过 `foo`；全库没有活的 `foo` | 移进回收站；若 `foo` 顶层两周没变，同一轮永久删除 | 【已核对】测试复现 |
| 同一工作区先删 `foo`（30 天前），重建后又删（昨天，还在 7 天宽限期里，目录没被移走，比如由助理删除，`assistant/delete_tools.py:15-18`；修复前测试里这一条没有复现，是读代码得出的） | 按 30 天前那次删除就移走，第二个项目的宽限期被跳过 | 【已核对】代码 |
| 同一工作区删了 `foo` 之后，Agent 又建了一个 `/workspace/foo` 在用 | 移走 | 【已核对】代码 |
| 用户在界面上删项目（桌面在线，立即移进回收站） | 下一次回收就可能 `rm -rf`，不等 14 天 | 【已核对】代码 + 测试 |
| `WUYING_ROUTING=shared`，所有工作区共用一台桌面 | 进程里恰好一个工作区的客户端时照跑 | 【已核对】代码 |

线上有没有真的移走或删掉过别人的目录，没有核实【推断：有可能，取决于有没有撞名的 slug 和重启后的那段窗口】。下文给了只读的查法。

## 修复

改动在 `backend/project/reclaim.py`、`backend/cron/reaper.py`、`backend/sandbox/manager.py`。原则是 `backend/AGENTS.md` 的删除边界：定时任务的删除范围只能收窄，不能放宽。下面每一条都是在原来的条件上再加限制。

1. **只比对拥有这台桌面的工作区。** 新的 `SandboxManager.get_only_runtime()` 返回 `(工作区 id, 客户端)`，工作区 id 就是取得这个客户端时的映射键，不借任何用户的身份（`sandbox/manager.py`，`get_only_client` 之后）。`reclaim(sandbox, workspace_id=...)` 的"已过期"集合只取这个工作区的项目。
2. **归属说不清就整轮跳过，不移也不清回收站。** `get_only_runtime()` 在这些情况下返回 None：本进程不是恰好一个客户端；映射里的 `SandboxInfo.user_id` 与键不一致；客户端自带的 `workspace_id` 与键不一致；提供方是所有工作区共用一台桌面（`owns_containers` 为假且 `routes_per_user` 不为真，即 `WUYING_ROUTING=shared`）。`reclaim()` 没拿到 `workspace_id` 时直接返回 `skipped: "no workspace"`，不向桌面发任何命令。
3. **保护集合不收窄。** 全库任何工作区里活着的 slug 仍然受保护，和原来一样；只收窄到本工作区的话，反而会多移走一些目录。另外加上本工作区里删除不满 7 天、或者没有删除时间的同名项目。
4. **目录在项目删除之后变过，就不算遗留。** 对候选目录读一次顶层 mtime（`stat -c %Y`，只读）。mtime 不早于这个 slug 在本工作区最近一次删除的时间，或者读不出来，就记为 unknown 留着。
5. **只移动合法 slug 形状的名字**（`SLUG_RE`），避免把奇怪的目录名拼进 shell 命令。
6. **回收站按移入时间算保留期。** 两处写回收站的代码都把条目命名为 `<slug>-<UTC 移入时间>`。清理时仍先用原来的 `find -mtime +14` 筛一遍，再要求名字里的移入时间早于 14 天前；名字里没有时间戳、或不是 slug 形状的条目不删。`rm -rf` 的路径加了 `shlex.quote`。返回值只列真正删了的。

被移动的集合，修复前是"名字在全库已过期 ∩ 不在全库活着"，修复后是它的子集："名字在本工作区已过期 ∩ 不在全库活着 ∩ 不在本工作区近期删除 ∩ 合法 slug ∩ 顶层 mtime 早于删除时间"。被删除的回收站条目同理是原集合的子集。

**代价**：`WUYING_ROUTING=shared` 的环境不再回收，也不再清回收站，旧目录会留在盘上。工作区 A 已过期的 `foo` 如果别的工作区恰好有活的 `foo`，仍然留着（修复前也是这样）。

**剩下的风险**：归属靠的是取得客户端时按数据库解析出的路由（per_desktop 下是 `cloud_desktop_repo.get_for_workspace`）。桌面在两次取得之间被重新分配给别的工作区，这里不会知道，要等下一次取得时路由比对才会换掉客户端。mtime 比对受桌面时钟影响：桌面时钟慢的话，删除前后几分钟内建的目录可能被当成遗留。

## 测试

【已核对】在本机跑过，没有用真实桌面。

- 新增 `backend/tests/unit/test_project_reclaim_scope.py`：用内存 SQLite 写真实的项目行，用一个假桌面应答回收发出的每一条命令（`ls`、`stat`、`mv`、`find`、`rm -rf`，其他命令直接让测试失败）；假桌面的 `mv` 保留 mtime，和真盘一致。覆盖：跨工作区不移；本工作区的旧项目会移且同一轮不删；同工作区另一成员的活项目保护目录；别的工作区的活项目仍然保护；后一次删除保住宽限期；没有删除时间不移；删除后变过的目录不移；读不出 mtime 不移；没有工作区什么都不做；回收站按名字里的时间清理；`get_only_runtime` 的六种情况；`_sweep_workspace` 把工作区交给回收、拿不到就跳过。
- 修复前用同一个文件里的前两条跑旧代码：第一条失败于 `assert ['slug-…'] == []`（目录被移走），第二条失败于 `rm -rf /workspace/.openbox/trash/slug-…-20261128-112805`（同一轮被删）。
- 改了 `backend/tests/unit/test_project_reclaim.py`：调用补上 `workspace_id`，替身函数换成新名字，假沙箱应答 `stat`。原有断言一条没删。
- 结果：`test_project_reclaim.py`、`test_project_reclaim_scope.py`、`test_cron_project.py`、`test_user_id_discipline.py`、`test_project_workspace.py`、`test_sandbox_manager.py` 共 83 条，81 过，2 条失败。失败的是 `test_sandbox_manager.py` 里的 `test_acquire_reuses_alive_container` 和 `test_acquire_rebuilds_client_with_the_retained_physical_desktop_id`，原因是本机没有 Docker 守护进程；把 `manager.py` 换回原版也是这两条失败，和本次改动无关。

## 和 25.2 的 0-9 的关系，还要做的事

清单原文是"只读代码和库，不动数据……会的话先停这个定时任务，修复另行安排"。这次在分支上直接修了，因为委托写明要修；修复只收窄，没有动任何数据。**线上在发布之前仍跑旧逻辑。** 这个定时任务没有单独的开关：要在发布前止损，只能发布这个修复，或者另做一个关掉 `_sweep_workspace` 的改动。

要有权限的人只读核实的：

```sh
# gw2，部署根目录
cd /opt/openbox

# 1. 旧逻辑有没有移过、删过东西（日志保留多久就能查多久）
docker compose logs --no-color --since 720h backend 2>&1 \
  | grep -E 'Reclaim: binned|Workspace sweep: binned|unrecognised directories alone'

# 2. 旧规则会在任何桌面上移走的名字：全库删除超过 7 天、全库没有活的同名项目；
#    deleted_in_workspaces > 1 或桌面属于别的工作区时就是跨租户的风险面
docker compose exec -T postgres psql -U openbox -d openbox -c "
WITH expired AS (
  SELECT DISTINCT slug, workspace_id FROM projects
  WHERE is_deleted AND deleted_at < now() - interval '7 days' AND slug IS NOT NULL),
live AS (SELECT DISTINCT slug FROM projects WHERE NOT is_deleted AND slug IS NOT NULL)
SELECT e.slug, count(DISTINCT e.workspace_id) AS deleted_in_workspaces
FROM expired e LEFT JOIN live l USING (slug)
WHERE l.slug IS NULL
GROUP BY e.slug ORDER BY 2 DESC, 1 LIMIT 100;"
```

第 1 条如果有记录，被移走的目录可能已经被同一轮删掉了；还在的话在对应桌面的 `/workspace/.openbox/trash/` 下，名字带移入时间。看桌面上的目录要有那台桌面的权限，而且是用户的数据，只列目录（`ls -la --time-style=full-iso /workspace/.openbox/trash`），不要动。
