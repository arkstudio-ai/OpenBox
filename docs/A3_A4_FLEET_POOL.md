# A3 · ECD 预热池 + A4 · 舰队观测与告警 v0 —— Codex 开工引子

> 2026-09-04。从 `DETAILED_PLAN_M1_M2.md` v3 的 A3 与 A4 抽出，合成一个任务分两阶段（先 A4 的快照与规则，再 A3 的池），
> 因为池的每一步都要靠快照对账兜底。从 `main`（≥ `ebe007c`）起分支 `codex/a3-fleet-pool`，用**自己的 worktree**，不要动主工作树。
>
> **执行者须知**：本项会**花真钱**（每台包月 4c8g/50G 约 ¥105.75/月，按合同价）。任何 `CreateDesktops`、`RenewDesktops`、`RebuildDesktops`、
> `ModifyDesktopChargeType`、`DeleteDesktops` 的真实调用，都要先把 `DescribePrice`/影响范围报给用户，得到明确确认再执行；自动补池开关默认关。
> 只改 §4 列出的目录；遇 §9 情形停下来报告。

---

## 1. 目标

**一句话**：一个 Python 服务 + DB 事实源的五态桌面池（预热 → 分配 → 释放 → 回收 → 重建），加一套每 10 分钟对账 ECD 与 DB 的快照规则和告警生命周期，让「开通即得桌面」和「舰队不盲飞」同时成立。

**完成定义**
1. **快照与对账（A4 v0）**：`fleet_snapshot` 任务每 10 分钟拉 ECD 全量（按 `openbox-env` 标签）与 DB 行，跑 §4.4 的 10 条规则，产生/自动关闭 `fleet_alerts`；后台能看、能 ack、能 mute；可选 webhook 推送。
2. **池状态机（A3）**：`cloud_desktops.pool_state ∈ reserve|prewarm|assigned|released|recycling|retired`（`reserve` = 已划归但未重建的储备机，无 DB 通道信息）；`assign` 在 2 分钟内把一台 prewarm 交给 workspace 并通过 A1 的 `verify`；`release` 保留数据撤访问；`recycle` 用 v2 金镜像 `RebuildDesktops` 后回 prewarm；`retire` 标记不再续期。
3. **采购四闸**：单价 ≤ `POOL_MAX_UNIT_PRICE_CNY`、账号余额 ≥ 2×单价、每次 ≤ `POOL_MAX_PURCHASES_PER_TICK`、每日 ≤ `POOL_MAX_PURCHASES_PER_DAY`；以 **ECD 侧按标签计数为准**，DB 只做校对；`POOL_AUTO_PURCHASE` 默认 `false`，关着时只报警不买。
4. **到期纪律**：prewarm 与 assigned 桌面 `expires_at` 距今 < `POOL_RENEW_BEFORE_DAYS`（默认 3）→ 续一期（`renew_desktop`，A2 已封装）并审计；`retired` 不续。`ModifyDesktopChargeType`（按量转包月）封装但本项不实调。
5. **开通接入**：`provision()` 在 per_desktop 模式下优先 `assign` 池机，池空时回落现有「即时创建」；DesktopTab 显示「分配中」。
6. **收养**：后台能把一台现存桌面收进池（v2 镜像直接收；非 v2 需 `rebuild + approve` 重建后收，数据清空）；只接受 `POOL_ADOPT_ALLOWLIST` 里的 id。首批收 A2 验收机 `ecd-0b7gj174mc6f23ctq`，再按用户清单逐台收养 bossip 舰队的包月机，直到水位 5。
7. **后台页** `/admin/fleet`（仅 `users.role=admin`）：桌面表（池态、归属、到期、通道、计费）、池水位卡、告警列表（ack/mute）、`ensure` 按钮（带 dry-run）。
8. 单测全绿；真机验收见 §5。

**非目标**（v3 镜像除外，它是本项第 0 步）：订阅到期状态机与 `RenewDesktops` 的「按订阅」触发（B4）；计费；平台账号（A5）；把共享桌面纳管；多地域（只做 `WUYING_REGION_ID`）；把 A1 的通道巡检迁进 internal_tasks（保留现状）。

---

## 2. 现状（2026-09-04，main `ebe007c`，gw2 已部署同代码）

| 事实 | 位置 |
|---|---|
| `cloud_desktops` 列：`id, workspace_id(非空, FK), user_id(可空), desktop_id, end_user_id, region_id, status, error, charge_type, expires_at, channel_kind, private_ip, tunnel_port(unique), tunnel_bind, tunnel_pubkey, tunnel_fingerprint(unique), action_api_key_hash/ciphertext, tunnel_state, last_seen_at, channel_error, is_deleted…`；一 workspace 一台部分唯一索引。**没有 pool_state**；`workspace_id` 非空意味着预热机（无主）放不进去 | `backend/db/models/cloud_desktop.py` |
| ECD 封装已有：`create_desktop(workspace_id)`（PrePaid 时带 period/auto_pay/auto_renew）、`describe_desktop`（含 `charge_type/expired_time`）、`describe_price`、`renew_desktop`、`wait_desktop_ready`、`start/stop/delete_desktop`、`desktop_tags`、`list_desktop_tags`、`list_desktops(user_id)`（按 `openbox-env` 过滤）、`verify_ownership(desktop_id, workspace_id)`、`ensure_end_user`、`remove_openbox_end_users`、`_retry_throttled` | `backend/sandbox/wuying_ecd.py:49-580` |
| **缺**的封装：`ModifyEntitlement`（SDK 字段 `desktop_id, end_user_id[], region_id`）、`RebuildDesktops`（`desktop_id[], image_id, after_status, operate_type`）、`TagResources/UntagResources`（`resource_type` 用 `ALIYUN::GWS::INSTANCE`，**不要**用 `ALIYUN::GWS::DESKTOP`，bossip 踩过两套命名空间互不可见）、`ModifyDesktopChargeType`、`DescribeDesktops` 按 `tag`/`desktop_status_list`/`charge_type` 过滤 | SDK `alibabacloud_ecd20200930/models.py` |
| 账号余额查询需要 `alibabacloud-bssopenapi20171214`，**未装**（pyproject 只有 ecd / eds-user / tea-openapi）；`QueryAccountBalance` 无地域 | `backend/pyproject.toml:42-49` |
| 通道：`sandbox/channel.py` 的 `WuyingChannel.install / verify / revoke`，端口分配 `cloud_desktop_repo.reserve_tunnel_port`；撤销时 `tunnel_port` 置空（全局唯一索引） | A1/A2 |
| `wuying_desktop_service`：`status/provision/resolve_ticket_target/release_ghost`，`_adopt_from_tags`（按 `openbox-user` 标签收养），`start_patrol` 5 分钟一轮只探通道 | `backend/sandbox/wuying_desktop_service.py` |
| 内置周期任务：`cron/internal_tasks.register(name, interval_sec, fn)`，单实例抢占 + 退避；`register_builtin_tasks()` 在 `main.py` 集中注册；后台 `GET /api/admin/internal-tasks` | `backend/cron/internal_tasks.py` |
| 后台：`/api/admin/*` 依赖 `require_admin` + `get_workspace`，现有 `users / workspaces/{id} / audit / internal-tasks` 四个只读接口；**前端没有任何 admin 页面** | `backend/api/admin.py`、`frontend-v2/src/app/router/router.tsx` |
| 审计：`audit.record(actor_user_id, workspace_id, action, target_type, target_id, detail, request)` | `backend/audit/__init__.py:12` |
| 现役上海桌面：共享桌面 `ecd-4zjxaq5g45dr5qr0i`（bossip 池 slot8，**不纳管**）；`ecd-8zp47qagrsc95h67t` openbox-dev-shanghai（6c12g，v1 镜像，10-01 到期，**现在是 `demo` 账号默认空间的在用桌面**，通道 up，端口 18103）；`ecd-0b7gj174mc6f23ctq` A2 验收机（4c8g/50G，**v2 镜像**，10-04 到期，通道已撤、行已软删、云侧保留）。两台包月都未开自动续费 | 验收人实查 |
| 金镜像 v2 `m-ccceuit7jn3xzwx45`（上海，50G）；策略组 `pg-0bbay5jmvosn8b2hc`；办公网络 `cn-shanghai+dir-2879607125`；规格 `eds.enterprise_office.4c8g`；上海 4c8g/50G 包月实付 ¥105.75、按量 ¥0.744/h | A1/A2 记录 |
| bossip 参照（只借规则不搬 bash）：`~/arkstudio/bossip/apps/codex/v1/deploy/wuying/FLEET_POOL_MANAGER.md`（五态、双闸、recycle 6.5 分钟、标签命名空间坑）、`apps/center/src/admin-v2/fleet-reconcile.ts`（纯函数对账、source-health 门控：某个数据源本轮拉取失败时不自动关闭它相关的告警）、`ECD_FLEET_PURCHASE_API.md` §0（余额预检 ≥ 单价×2） | bossip 仓库 |

---

## 3. 必要资料与待拍板
| # | 项 | 状态 |
|---|---|---|
| 1 | **openbox-dev-shanghai 不入池**（2026-09-04 拍板），留给 `demo` 账号；快照规则把它当普通 assigned 桌面对待 | 已定 |
| 0 | **基准镜像 = openbox v2 `m-ccceuit7jn3xzwx45` → 本项第 0 步升到 v3（烘入 obx-display-guard，见 7h）**（openbox-image-v2-shanghai，50G，2026-09-05 在 A2 验收机上实查：Ubuntu 22.04.5、Google Chrome 138、ibus + ibus-libpinyin（引擎在 `/usr/libexec/ibus-engine-libpinyin`）、ffmpeg 4.4.2、Noto Sans CJK 35 款、`obx-display/obx-file/obx-shot/obx-x`、xdotool/scrot/wmctrl、node/npm、python3；零运行时秘密，隧道与 action server 单元 disabled）。所有池机——收养的 bossip 机与新购——一律重建/创建到它。`obx-display-guard` 不在镜像里，由后端首次用 computer 工具时 `ensure_desktop_tools` 装，预热校验不检查它。将来要改镜像走 A1 的 `wuying_bootstrap.py --image-mode` + `wuying_image_verify.py` 出 v3，本项不做 | 已定 |
| 2 | **预热水位 5**（已定）。水位主要靠**收养现有包月机**填：账号里有一批退不掉的包月机要重新入池（bossip 舰队 `bossip-sh-*`，6c12g、80G 盘、bossip 金镜像、PrePaid）。收养 = `RebuildDesktops` 到 openbox v2 镜像 + 换标签，**数据清空**，一台一台做、每台单独确认。**清单已定（2026-09-05 用户确认「全部可以」）**：上海 `bossip-sh-001…013` 中除共享桌面 `bossip-sh-007`（`ecd-4zjxaq5g45dr5qr0i`）外的 12 台，写进 `POOL_ADOPT_ALLOWLIST`：`ecd-ijea2hjljf9c4wd1b, ecd-5pvbuskezql1d4h5m, ecd-i4c4x8wpqg1lxktmi, ecd-c4qndqrko3db7kjfz, ecd-i4c4x8wpqg1lxktmj, ecd-gj5j513on7j0u97as, ecd-4y9s9igraz7hc58ea, ecd-c51eyfc786uzimn3o, ecd-4y9s9igraz7hc58eb, ecd-ctazuyee5p8enedta, ecd-b9oizzx4rfhbsm1uh, ecd-glxi1nk433hliivri`。先收 5 台到水位，其余 7 台先只打 `openbox-pool=reserve` 标签不重建（保留给后续扩池，重建前照样逐台确认） | 已定 |
| 3 | **规格已定：6c12g**（`WUYING_DESKTOP_TYPE=eds.enterprise_office.6c12g`，gw2 与 `.env.example` 同步改；收养的 bossip 机本来就是 6c12g，池内统一）。`POOL_MAX_UNIT_PRICE_CNY` 只是防失控兜底，按 6c12g/50G 包月询价设（bossip 实测 ¥241.5 原价，合同价待 `describe_price` 实查），建议 300 | 已定 |
| 4 | 阿里云余额是否足够新购；采购审批人 = 用户本人 | 用户确认 |
| 5 | **告警 webhook 本版不做**（2026-09-04 拍板），告警只进后台与日志；管理平台搭好后再梳理推送 | 已定 |
| 6 | gw2 部署与回滚照 `docs/DEPLOY.md`；alembic 自动迁移；**队友也在直接部署 gw2**，部署前先看 `.env` 当前 tag | 已知 |
| 7 | **环境实查补漏（2026-09-05）** | |
| 7a | bossip 12 台的策略组都是 `system-all-enabled-policy`（不是 1080p 的 `pg-0bbay5jmvosn8b2hc`）→ `adopt` 必须调 `ModifyDesktopsPolicyGroup` 切到 `WUYING_POLICY_GROUP_ID`，预热校验要断言策略组正确 | 写进 §4.5 |
| 7b | 12 台都绑着 `bossip-slot2…15` EndUser、全部 Disconnected（无人在用）；重建后必须解绑，否则 bossip 侧账号仍能用无影客户端登进池机 | 写进 §4.5 |
| 7c | 到期日：001/002 **10-04**，003–006 **10-06**，008–013 10-11/12。首批 5 台取最晚到期的 **009–013**（`ecd-c51eyfc786uzimn3o, ecd-4y9s9igraz7hc58eb, ecd-ctazuyee5p8enedta, ecd-b9oizzx4rfhbsm1uh, ecd-glxi1nk433hliivri`），008 作 reserve；001–006 六台**不续**（2026-09-05 拍板），任其在 10-04/10-06 到期释放；reserve 只剩 008。快照规则对它们在到期前不报 `expiring_soon`（allowlist 内且 `pool_state=reserve` 且未 adopt 的机器视为「放弃」，标签打 `openbox-pool=abandon`） | 已定 |
| 7d | gw2 `backend.env` 没有 `WUYING_DESKTOP_TYPE`（默认 4c8g）→ 部署本项时加 `WUYING_DESKTOP_TYPE=eds.enterprise_office.6c12g` 及全部 `POOL_*` 键 | 部署清单 |
| 7e | gw2 只有一个 admin 账号 `m1adm-0904`（M1 验收用的测试号），`demo` 是普通用户；后台页要用得把 `demo` 提成 admin。**Logto 登录不会带来 admin**：SSO 首登按普通用户建号，`users.role` 只在建号时默认 `user`，没有任何 claim→role 映射，也没有提权接口；只能 SQL：`update users set role='admin' where username='demo'`（角色写在 JWT 里，改完要重新登录）。claim 映射留给里程碑三的运营角色 | SQL 提权 |
| 7f | gw2 后端用的阿里云 AK 是**主账号 AK**（权限不成问题，BSS/重建/续费都能调），但主账号 AK 放在服务器上风险大，且验收人排查时曾把该 AK 打进过会话日志——建议本项上线前**换成 RAM 子账号 AK**（ECD/EDS 全权 + `AliyunBSSReadOnlyAccess`），替换 `/opt/openbox/secrets/aliyun-config.json` 并**吊销旧 AK** | 建议，用户定 |
| 7h | **1080p 守护此前没在 main 里（已于 09-05 合并 `66bb9de` 并部署 gw2）**：`obx-display-guard` 与「播放器连接后钉回 1080p」都在未合并分支 `claude/mystifying-leakey-449d79`（`4a87777`，基于 b8b68e0，改 `sandbox/desktop.py` +146 与 `DesktopTab.tsx` +31），上海共享桌面是手工装的。main 的 DesktopTab 没有 `setResolution`，`ensure_desktop_tools` 也不装守护。试合并只在 `DesktopTab.test.tsx` 冲突。**本项第 0 步：先把该分支合进 main，再用 `wuying_bootstrap.py --image-mode` 出 v3 镜像把守护烘进去（`WUYING_IMAGE_ID` 切 v3），然后才重建 bossip 机**——否则 5 台刚重建完又要再重建一次 | 写进 §8.1 第 0 步 |
| 7g | bossip-gw-1 的 `bossip-autoprovision` 处于 inactive/static，不会再动这批机器；`pool-manager.sh` 是手动脚本。重建后这些机器从 bossip 的 `purpose=codex` 视野里消失即可，不需要通知 bossip 侧代码 | 已确认 |

---

## 4. 方案

### 4.1 数据（一个迁移，`down_revision=c3e5a7b9d1f4`，可降级）
- `cloud_desktops`：`workspace_id` 改**可空**（预热机无主）；一 workspace 一台的部分唯一索引加 `workspace_id IS NOT NULL` 条件；新增 `pool_state String(16) default 'assigned'`（存量行回填 `assigned`）、`pool String(16) nullable`（`trial|paid|internal`）、`assigned_at`、`released_at`、`spec String(48)`、`golden_image_id String(64)`、`retired bool default false`、`last_snapshot_at`。
- `fleet_snapshots(id, taken_at, source: ecd|db|account, ok bool, payload JSON, error text)`，保留 7 天（快照任务自清）。
- `fleet_alerts(id, rule String(48), severity: info|warn|critical, resource_type, resource_id, message, detail JSON, first_seen_at, last_seen_at, resolved_at nullable, acked_by nullable, acked_at, muted_until nullable)`；部分唯一索引 `(rule, resource_id) WHERE resolved_at IS NULL`。
- `pool_purchases(id, desktop_id nullable, unit_price, currency, quantity, request_id, status: ordered|created|failed, created_by: system|<user_id>, created_at, error)`——每次真实下单一行，日上限按它数。

### 4.2 配置（`core/config.py` + `.env.example`）
`POOL_ENABLED=false`（整个池逻辑开关，关着时 provision 走现有路径）、`POOL_AUTO_PURCHASE=false`、`POOL_TARGET_PREWARM=5`、`POOL_MAX_UNIT_PRICE_CNY=300`、`POOL_MAX_PURCHASES_PER_TICK=1`、`POOL_MAX_PURCHASES_PER_DAY=2`、`POOL_MIN_ACCOUNT_BALANCE_MULTIPLE=2`、`POOL_RENEW_BEFORE_DAYS=3`、`POOL_ASSIGN_ON_PROVISION=true`、`FLEET_SNAPSHOT_INTERVAL_SEC=600`、`FLEET_CHANNEL_DOWN_ALERT_SEC=600`。（本版不做 webhook。）

### 4.3 ECD 封装补齐（`sandbox/wuying_ecd.py`）
`modify_entitlement(desktop_id, end_user_ids)`、`rebuild_desktop(desktop_id, image_id, after_status="Running")`、`tag_desktop(desktop_id, tags)` / `untag_desktop(desktop_id, keys)`（`ALIYUN::GWS::INSTANCE`）、`list_fleet_desktops()`（按 `openbox-env` 标签分页拉全量，返回 `desktop_id/status/charge_type/expired_time/image_id/desktop_type/end_user_ids/tags`）、`modify_charge_type(...)`（封装 + 单测，不实调）、`query_account_balance()`（新增依赖 `alibabacloud-bssopenapi20171214`，返回可用余额 CNY）。全部套 `_retry_throttled`。

### 4.4 舰队快照与规则（`sandbox/fleet.py`，纯函数对账 + 薄 IO 层）
- `take_snapshot()`：三源——ECD（`list_fleet_desktops`）、DB（`cloud_desktops` 未软删 + 最近软删 24h）、账号余额；每源独立 try，失败记 `ok=false`。
- `reconcile(ecd, db, account, now) -> list[Finding]`：**纯函数，零 IO**，便于单测。规则 v0：

| 规则 | 触发 | 级别 |
|---|---|---|
| `ghost` | DB 行 `status=running` 但 ECD 无此桌面或 `Deleted` | critical |
| `orphan` | ECD 有 `openbox-env=本环境` 的桌面，DB 无活行 | warn |
| `tag_mismatch` | DB `workspace_id` ≠ 标签 `openbox-workspace`，或 `pool_state` ≠ 标签 `openbox-pool` | warn |
| `expiring_soon` | `expired_time` < now + `POOL_RENEW_BEFORE_DAYS` 且未 `retired` | warn |
| `expired` | ECD 状态 `Expired` | critical |
| `channel_down` | `pool_state=assigned` 且 `tunnel_state=down` 超过 `FLEET_CHANNEL_DOWN_ALERT_SEC` | warn |
| `postpaid_running` | ECD `PostPaid` 且 Running 超过 24h（成本泄漏） | warn |
| `prewarm_below_watermark` | prewarm 数（ECD 计数）< `POOL_TARGET_PREWARM` | info（`POOL_AUTO_PURCHASE=false` 时 warn） |
| `purchase_blocked` | 上一轮 `ensure_prewarm` 因任一闸拒绝 | warn |
| `account_balance_low` | 余额 < 单价 × `POOL_MIN_ACCOUNT_BALANCE_MULTIPLE` | critical |

- **source-health 门控**（借 bossip）：某源本轮 `ok=false` 时，只与该源相关的规则**不产生新告警也不自动关闭**旧告警；三源都 ok 才允许 auto-resolve。
- 告警生命周期：finding 存在 → upsert `fleet_alerts`（更新 `last_seen_at`）；finding 消失且未 mute → `resolved_at=now`；`ack` 只记人，不影响 resolve；`mute_until` 期间不推送。推送：本版只 `log.error`（critical）/ `log.warning`，同一告警只在 first_seen 和升级时打一次；webhook 留到管理平台搭好后。
- 注册：`internal_tasks.register("fleet_snapshot", FLEET_SNAPSHOT_INTERVAL_SEC, ...)`。

### 4.5 池状态机（`sandbox/pool.py`）
- `ensure_prewarm(dry_run=False)`（internal task，每 10 分钟，在快照之后）：
  1. `POOL_ENABLED` 为假直接返回。
  2. prewarm 计数 = ECD 上 `openbox-env=本环境 & openbox-pool=prewarm & 非 Expired/Deleted` 的数量；与 DB 差异 → 交给规则报 `tag_mismatch`，**不自行修**。
  3. 缺口 = 目标 − 计数；缺口 ≤ 0 返回。
  4. 四闸依次：`describe_price(PrePaid)` ≤ 上限；`query_account_balance()` ≥ 倍数×单价；本次 ≤ `PER_TICK`；今日 `pool_purchases` 计数 ≤ `PER_DAY`。任一不过 → 写 `purchase_blocked` finding 源数据 + 审计 `pool.purchase_blocked`，返回。
  5. `POOL_AUTO_PURCHASE=false` 或 `dry_run` → 只返回「将购买 N 台，单价 X」。
  6. 真买：`pool_purchases` 先落 `ordered` 行 → `create_desktop_for_pool()`（**无 EndUser**，`desktop_name=obx-pool-<8hex>`，标签 `openbox-env / openbox-pool=prewarm / openbox-spec / openbox-image`；若 ECD 拒绝无用户创建，改用池专用 EndUser `obx-pool`，验证后写进 §8）→ 行 `created` + DB 行（`pool_state=prewarm, workspace_id=NULL, charge_type=PrePaid, expires_at`）→ `wait_desktop_ready` → 预热校验（RunCommand `hostname` + `/usr/local/bin/obx-display` 存在）→ 审计 `pool.purchase`。
- `assign(workspace_id, triggered_by_user_id) -> record`：事务内 `SELECT … WHERE pool_state='prewarm' AND is_deleted=false ORDER BY expires_at DESC LIMIT 1 FOR UPDATE SKIP LOCKED` 置 `pool_state='assigning'`（中间态，防双分配）→ `ensure_end_user(workspace_id)` → `modify_entitlement` → `tag_desktop({openbox-workspace, openbox-user, openbox-eu-id, openbox-pool=assigned})` → 若 `expires_at − now < POOL_RENEW_BEFORE_DAYS` 则 `renew_desktop` 一期（审计 `pool.renew`）→ `WuyingChannel.install` → `verify`（通道通 + `bash hostname` + 1920×1080）→ `pool_state='assigned', workspace_id, user_id, assigned_at`。任一步失败：撤已装通道、去标签、`pool_state='prewarm'`，写 `fleet_alerts(rule=assign_failed)` 并抛错。
- `provision()` 接入：`POOL_ENABLED and POOL_ASSIGN_ON_PROVISION` 且有 prewarm → `assign`；否则现有创建路径（创建出的桌面 `pool_state='assigned'`）。
- `release(desktop_id, actor)`：仅 `assigned` → `revoke` 通道（端口置空）→ `modify_entitlement(desktop, [])`（若 API 不接受空列表则保留 EndUser 只撤通道，写进 §8）→ 标签 `openbox-pool=released`，去 `openbox-workspace/user`（保留 `openbox-eu-id` 便于追溯）→ `pool_state='released', released_at`；DB 行**不软删**（数据与登录态仍在盘上）。审计。
- `recycle(desktop_id, actor, approve: bool)`：仅 `released|retired→否`；`approve` 必真；`rebuild_desktop(image=WUYING_IMAGE_ID)` → `pool_state='recycling'` → 等 Running → 预热校验 → `pool_state='prewarm'`，清 `workspace_id/user_id/end_user_id`；标签 `openbox-pool=prewarm`。审计 `pool.recycle`。
- `retire(desktop_id, actor)`：`retired=true`，标签 `openbox-pool=retired`；`renew_expiring` 跳过它；到期后由 ECD 自然释放，规则 `expired` 提醒，人工确认后 `DeleteDesktops`（本项不自动删）。
- `adopt(desktop_id, pool_state, actor, rebuild: bool=False, approve: bool=False)`：`describe_desktop` → 若镜像 ≠ v2：`rebuild=True and approve=True` 才走 `rebuild_desktop(WUYING_IMAGE_ID)`（**数据清空**，先记录原标签与 EndUser 到审计 detail），否则拒绝并提示 → 等 Running → `ModifyDesktopsPolicyGroup(WUYING_POLICY_GROUP_ID)`（bossip 机是 `system-all-enabled-policy`）→ 预热校验（含策略组断言）→ **换标签**：删 bossip 侧 `purpose/pool/codex-user/spec/environment/managed-by` 等非 openbox 键（`untag_desktop`），写 `openbox-env / openbox-pool / openbox-spec / openbox-image` → `modify_entitlement(desktop, [])` 解绑原 EndUser（若 API 不接受空列表，改为绑池专用 EndUser，写进 §8）→ 建 DB 行（`prewarm` 时 workspace NULL，`spec` 记实际规格如 `6c12g`，`charge_type/expires_at` 取自 ECD）→ 审计 `pool.adopt`。**只接受用户清单里的 desktop id**（配置 `POOL_ADOPT_ALLOWLIST`，逗号分隔），不在清单内的一律拒绝——同账号上还有 bossip 真实用户机，误收会打到线上客户。
- `renew_expiring()`（internal task，每天 1 次）：`pool_state in (prewarm, assigned)` 且未 `retired` 且 `expires_at − now < POOL_RENEW_BEFORE_DAYS` → `renew_desktop` 一期 → 刷新 `expires_at` → 审计。**首次真实续费前向用户报数**（每台 ¥105.75）。

### 4.6 后台接口（`api/admin_fleet.py`，`require_admin`）
`GET /api/admin/fleet/desktops?pool_state=&q=`、`POST /api/admin/fleet/desktops/{id}/{release|recycle|retire|renew|adopt}`（recycle 带 `{"approve":true}`，adopt 带 `{"pool_state":"prewarm"}`）、`POST /api/admin/fleet/pool/ensure?dry_run=true|false`、`GET /api/admin/fleet/pool`（水位、今日采购、闸状态）、`GET /api/admin/fleet/alerts?state=open|resolved`、`POST /api/admin/fleet/alerts/{id}/{ack|mute}`（mute 带 `{"until": iso}`）、`GET /api/admin/fleet/snapshots/latest`。每个写接口一条审计。

### 4.7 前端（`frontend-v2/src/features/admin/`，新）
路由 `/admin/fleet`，`users.role !== "admin"` 跳回首页；三块：桌面表（id、名称、池态、归属 workspace、通道态、计费与到期、镜像是否 v2、操作按钮）、池水位卡（prewarm/目标、今日采购、四闸状态、`ensure` dry-run 与真买按钮，真买二次确认弹窗显示单价与台数）、告警列表（级别、规则、资源、首次/最近、ack/mute）。locale zh-CN/en-US 同步；`npm run check` 过。DesktopTab：`provision` 走池时显示「分配中」。

### 4.8 桌面同步与部署
无桌面侧脚本改动。部署照 `docs/DEPLOY.md`；上线时 gw2 `backend.env` 先 `POOL_ENABLED=true, POOL_AUTO_PURCHASE=false`，自动采购由用户手动打开。

---

## 5. 验收条件

| 编号 | 条件 | 判据 |
|---|---|---|
| AC-1 | 对账规则单测 | `reconcile()` 对 10 条规则各至少一条正例一条反例；source-health 门控：ECD 源失败时不新增也不关闭 `ghost/orphan/expired` |
| AC-2 | 快照实跑 | gw2 上 `fleet_snapshot` 每 10 分钟一行 `fleet_snapshots`（三源 ok），`GET /api/admin/fleet/snapshots/latest` 能看 |
| AC-3 | 告警生命周期 | 人为制造 ghost（DB 行指向不存在的桌面 id）→ 下一轮出现 critical 告警 → ack → 修正后下一轮 `resolved_at` 非空；mute 期间 webhook 不推 |
| AC-4 | 收养 v2 机 | `adopt ecd-0b7gj174mc6f23ctq prewarm` → 标签改写、DB 行 `prewarm/workspace NULL`、下一轮快照 prewarm=1 无 `tag_mismatch` |
| AC-4b | 收养并重建 | 对用户清单里一台 `bossip-sh-*`：不带 `approve` 被拒；带 `rebuild+approve` → 重建到 v2 → Running → 标签只剩 openbox-*、原 EndUser 解绑、DB 行 `prewarm/spec=6c12g` → 快照无 `orphan/tag_mismatch`；不在 allowlist 的 id 被拒。记录耗时 |
| AC-5 | 采购四闸 | dry-run 报「将购买 1 台，单价 ¥105.75」；把 `POOL_MAX_UNIT_PRICE_CNY=100` → `purchase_blocked` 告警且未下单；`POOL_AUTO_PURCHASE=false` 下 ensure 不下单；单测覆盖日上限与余额闸 |
| AC-6 | 真买一台 | **仅在收养后仍不足 5 台时做**（规格 6c12g 已定）：用户确认后 `POOL_AUTO_PURCHASE=true` 跑一轮 ensure → `pool_purchases` 一行 `created`、桌面 Running、规格 = `WUYING_DESKTOP_TYPE`；ECD 标签正确；然后把开关关回。规格未确认则本条记「跳过，原因」 |
| AC-7 | 分配 | 新 workspace 点开通 → `assign` 从池取机，**≤ 2 分钟**通道 up，`bash: hostname` = 该桌面主机名；`expires_at` 距今 ≥ `POOL_RENEW_BEFORE_DAYS`；两个 workspace 并发开通不会拿到同一台（单测 + 实跑各一次） |
| AC-8 | 释放与回收 | `release` 后该 workspace 会话 `DESKTOP_NOT_READY`、端口释放、标签变 released、DB 行保留；`recycle --approve` → Running → prewarm，耗时记录（bossip 参考 6.5 分钟）；不带 approve 被拒。80G 盘的收养机重建到 50G 镜像要验证一次成功 |
| AC-9 | 续期 | 单测：`expires_at` 距今 2 天 → `renew_expiring` 调 `renew_desktop`；`retired` 不调。真机续费本项**不做**（两台池机到期都在验收之后，见 §3） |
| AC-10 | 后台页 | admin 账号能看三块并完成 ack、dry-run、release；普通账号 403/跳转 |
| AC-11 | 不越界与测试 | `git diff --stat main` 只含 `backend/sandbox/{fleet,pool}.py`、`wuying_ecd.py`、`wuying_desktop_service.py`、`api/admin_fleet.py`、`core/config.py`、`.env.example`、`pyproject.toml`（bss SDK）、模型与一个迁移、`cron/internal_tasks.py` 注册处、`main.py` 注册、测试、`frontend-v2/src/features/admin/**` 与路由/locale、`docs/WUYING_SANDBOX.md`；后端全量除既有 4 个视频配置用例外全绿；`npm run check` 过 |
| AC-12 | 账本 | 全程真实采购/续费/重建次数与用户确认记录一致，写进 §8 |

---

## 6. 测试方式
```bash
cd backend && uv run pytest tests/unit -q -k "fleet or pool or wuying or internal_task"
cd backend && uv run pytest tests/unit -q && uv run alembic heads
cd frontend-v2 && npm run check
```
真机：gw2 部署后按 AC-2/3/4/5/6/7/8/10 顺序做；AC-6 与 AC-7 之间用户确认；AC-8 的 recycle 在验收机上做（它本来就是池机）。

## 7. 交付证据
1. `git log/diff --stat`、pytest、`npm run check`、`alembic heads`。
2. AC-3 告警三态截图或 API 输出；AC-2 快照行。
3. AC-4/6/7/8 的 ECD `describe-desktops` 与 DB 行前后对照、耗时。
4. AC-5 dry-run 与 blocked 输出。
5. 后台页三张截图。
6. §8 填好：真实采购台数与金额、`CreateDesktops` 无 EndUser 是否被接受、`ModifyEntitlement` 空列表是否被接受、recycle 耗时。

## 8. 执行记录（执行者填写）
- 分支 / 提交 / 迁移修订：
- 采购与续费明细（台数、金额、RequestId、用户确认时间）：
- ECD 行为实测（无用户创建 / 空列表解绑 / rebuild 是否需先 Stop）：
- 偏离：

## 8.1 首批入池顺序（建议）
0. **先出 v3 镜像**：守护分支**已于 2026-09-05 合入 main（`66bb9de`）并部署 gw2**，本步只剩镜像——在 `wuying_bootstrap.py --image-mode` 里加装 `obx-display-guard`（systemd，root，每 3 秒经 `obx-x` 找 DISPLAY/XAUTHORITY 以 xauth 属主 `runuser` 跑 `obx-display`，钉不住退避 60 秒——实现取自该分支的 `ensure_desktop_tools`）→ 50G 按量机跑 image-mode + `wuying_image_verify.py`（新增断言：`systemctl is-enabled obx-display-guard` = enabled）→ `create-image openbox-image-v3-shanghai` → gw2 `WUYING_IMAGE_ID` 切 v3 → 删临时机。费用约 ¥1。
1. `adopt ecd-0b7gj174mc6f23ctq prewarm`（v2 机；`recycle --approve` 一次刷到 v3，顺带验证 recycle 路径）。
2. 逐台 `adopt --rebuild --approve` bossip 包月机，顺序按到期日从晚到早：013 → 012 → 011 → 010 → 009，每台先报「将清空该机数据、原 EndUser 解绑、策略组切 1080p」再执行；到 5 台为止（含 A2 验收机则 4 台即够，多出的一台也重建，水位按 5 台 prewarm 算）。
3. 仍不足 5 台 → 按 6c12g 新购补齐（先 dry-run 报价）。
4. 其余 7 台 bossip 机打 `openbox-pool=reserve` 标签，不重建，留作扩池储备；快照规则把 `reserve` 当合法状态不报 orphan。

## 9. 停下来报告
- 任何真实花钱调用前未获确认。
- ECD 拒绝无 EndUser 创建且池专用 EndUser 方案也不通。
- `RebuildDesktops` 要求先停机且停机会改变通道/标签之外的东西。
- 需要改 `backend/tool/`、`billing/`、C5 技能目录或 B1 的 workspace 表。
- 快照发现共享桌面 `ecd-4zjxaq5g45dr5qr0i` 或不在 allowlist 的 bossip 桌面被本环境标签命中（说明标签过滤有误，立刻停）。
- 收养重建前发现该机仍有 bossip 用户在用（`codex-user` 标签非空且 gateway 仍注册）。
