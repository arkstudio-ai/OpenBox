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
2. **池状态机（A3）**：`cloud_desktops.pool_state ∈ prewarm|assigned|released|recycling|retired`；`assign` 在 2 分钟内把一台 prewarm 交给 workspace 并通过 A1 的 `verify`；`release` 保留数据撤访问；`recycle` 用 v2 金镜像 `RebuildDesktops` 后回 prewarm；`retire` 标记不再续期。
3. **采购四闸**：单价 ≤ `POOL_MAX_UNIT_PRICE_CNY`、账号余额 ≥ 2×单价、每次 ≤ `POOL_MAX_PURCHASES_PER_TICK`、每日 ≤ `POOL_MAX_PURCHASES_PER_DAY`；以 **ECD 侧按标签计数为准**，DB 只做校对；`POOL_AUTO_PURCHASE` 默认 `false`，关着时只报警不买。
4. **到期纪律**：prewarm 与 assigned 桌面 `expires_at` 距今 < `POOL_RENEW_BEFORE_DAYS`（默认 3）→ 续一期（`renew_desktop`，A2 已封装）并审计；`retired` 不续。`ModifyDesktopChargeType`（按量转包月）封装但本项不实调。
5. **开通接入**：`provision()` 在 per_desktop 模式下优先 `assign` 池机，池空时回落现有「即时创建」；DesktopTab 显示「分配中」。
6. **收养**：后台能把一台现存桌面收进池（写标签 + DB 行）；首批收 A2 验收机 `ecd-0b7gj174mc6f23ctq`。
7. **后台页** `/admin/fleet`（仅 `users.role=admin`）：桌面表（池态、归属、到期、通道、计费）、池水位卡、告警列表（ack/mute）、`ensure` 按钮（带 dry-run）。
8. 单测全绿；真机验收见 §5。

**非目标**：订阅到期状态机与 `RenewDesktops` 的「按订阅」触发（B4）；计费；平台账号（A5）；把共享桌面纳管；多地域（只做 `WUYING_REGION_ID`）；把 A1 的通道巡检迁进 internal_tasks（保留现状）。

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
| 1 | **openbox-dev-shanghai 的去留**：它现在是 `demo` 账号的在用桌面，不是闲置机。方案 A：留给 demo，池不收；方案 B：demo 释放后 `RebuildDesktops` 到 v2 入池（数据清空）。执行单默认 **A** | 需确认 |
| 2 | 预热水位：计划写 5，建议**验证期先 2**（1 台收养 + 1 台新购），跑通后再调 `POOL_TARGET_PREWARM` | 需确认 |
| 3 | 单价上限 `POOL_MAX_UNIT_PRICE_CNY`（建议 120，现价 105.75）、日采购上限（建议 2） | 需确认 |
| 4 | 阿里云余额是否足够（每台 ¥105.75）；采购审批人 = 用户本人 | 用户确认 |
| 5 | 告警推送 webhook（钉钉/飞书 机器人 URL），没有就只进后台 | 可选 |
| 6 | gw2 部署与回滚照 `docs/DEPLOY.md`；alembic 自动迁移 | 已知 |

---

## 4. 方案

### 4.1 数据（一个迁移，`down_revision=c3e5a7b9d1f4`，可降级）
- `cloud_desktops`：`workspace_id` 改**可空**（预热机无主）；一 workspace 一台的部分唯一索引加 `workspace_id IS NOT NULL` 条件；新增 `pool_state String(16) default 'assigned'`（存量行回填 `assigned`）、`pool String(16) nullable`（`trial|paid|internal`）、`assigned_at`、`released_at`、`spec String(48)`、`golden_image_id String(64)`、`retired bool default false`、`last_snapshot_at`。
- `fleet_snapshots(id, taken_at, source: ecd|db|account, ok bool, payload JSON, error text)`，保留 7 天（快照任务自清）。
- `fleet_alerts(id, rule String(48), severity: info|warn|critical, resource_type, resource_id, message, detail JSON, first_seen_at, last_seen_at, resolved_at nullable, acked_by nullable, acked_at, muted_until nullable)`；部分唯一索引 `(rule, resource_id) WHERE resolved_at IS NULL`。
- `pool_purchases(id, desktop_id nullable, unit_price, currency, quantity, request_id, status: ordered|created|failed, created_by: system|<user_id>, created_at, error)`——每次真实下单一行，日上限按它数。

### 4.2 配置（`core/config.py` + `.env.example`）
`POOL_ENABLED=false`（整个池逻辑开关，关着时 provision 走现有路径）、`POOL_AUTO_PURCHASE=false`、`POOL_TARGET_PREWARM=2`、`POOL_MAX_UNIT_PRICE_CNY=120`、`POOL_MAX_PURCHASES_PER_TICK=1`、`POOL_MAX_PURCHASES_PER_DAY=2`、`POOL_MIN_ACCOUNT_BALANCE_MULTIPLE=2`、`POOL_RENEW_BEFORE_DAYS=3`、`POOL_ASSIGN_ON_PROVISION=true`、`FLEET_SNAPSHOT_INTERVAL_SEC=600`、`FLEET_ALERT_WEBHOOK_URL=`（可选）、`FLEET_CHANNEL_DOWN_ALERT_SEC=600`。

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
- 告警生命周期：finding 存在 → upsert `fleet_alerts`（更新 `last_seen_at`）；finding 消失且未 mute → `resolved_at=now`；`ack` 只记人，不影响 resolve；`mute_until` 期间不推送。推送：`log.error` + webhook（若配置），同一告警只在 first_seen 和升级时推一次。
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
- `adopt(desktop_id, pool_state, actor)`：`describe_desktop` + 校验镜像 = v2（不是则拒绝，提示先 recycle）→ 写标签 → 建 DB 行（`prewarm` 时 workspace NULL）→ 审计。
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
| AC-4 | 收养 | `adopt ecd-0b7gj174mc6f23ctq prewarm` → 标签改写、DB 行 `prewarm/workspace NULL`、下一轮快照 prewarm=1 无 `tag_mismatch` |
| AC-5 | 采购四闸 | dry-run 报「将购买 1 台，单价 ¥105.75」；把 `POOL_MAX_UNIT_PRICE_CNY=100` → `purchase_blocked` 告警且未下单；`POOL_AUTO_PURCHASE=false` 下 ensure 不下单；单测覆盖日上限与余额闸 |
| AC-6 | 真买一台 | 用户确认后 `POOL_AUTO_PURCHASE=true` 跑一轮 ensure → `pool_purchases` 一行 `created`、桌面 Running、prewarm=2；ECD 标签正确；然后把开关关回 |
| AC-7 | 分配 | 新 workspace 点开通 → `assign` 从池取机，**≤ 2 分钟**通道 up，`bash: hostname` = 该桌面主机名；`expires_at` 距今 ≥ `POOL_RENEW_BEFORE_DAYS`；两个 workspace 并发开通不会拿到同一台（单测 + 实跑各一次） |
| AC-8 | 释放与回收 | `release` 后该 workspace 会话 `DESKTOP_NOT_READY`、端口释放、标签变 released、DB 行保留；`recycle --approve` → Running → prewarm，耗时记录（bossip 参考 6.5 分钟）；不带 approve 被拒 |
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

## 9. 停下来报告
- 任何真实花钱调用前未获确认。
- ECD 拒绝无 EndUser 创建且池专用 EndUser 方案也不通。
- `RebuildDesktops` 要求先停机且停机会改变通道/标签之外的东西。
- 需要改 `backend/tool/`、`billing/`、C5 技能目录或 B1 的 workspace 表。
- 快照发现共享桌面或 bossip 桌面被本环境标签命中（说明标签过滤有误，立刻停）。
