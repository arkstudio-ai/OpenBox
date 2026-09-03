# 里程碑一、二详细计划（给编码执行用）

> 2026-09-03。本文是里程碑「打通」「可收费」的执行指南，供后续 vibe coding 直接照做。
> 决策来源：`PLAN_SHARE.md`（对外版）、`BILLING_PLAN.md`（计费设计）、`GAP_ANALYSIS.md`（差距与拍板）、
> `RELAY_CUTOVER.md`（网关切换现状）。冲突时以本文最新决策为准，并回写上述文档。
> **修订 v2（2026-09-03 晚）**：按 `DETAILED_PLAN_M1_M2_REVIEW.md` 的对抗审查回写。改动点在各节用「**v2**」标出；
> 审查里的证据位置不重复抄，需要时去看审查。
>
> **执行者须知**：每个工作项都写了「改哪里 / 数据 / 接口 / 验收」。不要引入本文之外的目标；
> 遇到本文未覆盖且影响超过单文件的新情况，停下来向用户报告。

---

## 进度（2026-09-03 晚，验收人更新）

| 工作项 | 状态 | 说明 |
|---|---|---|
| A1 每桌面通道 | ✅ 已验收、已上线 gw2 | 分支 `codex/a1-desktop-channel`，镜像 v2 `m-ccceuit7jn3xzwx45`，方案乙（gw2 自任中继） |
| B1 Workspace | ✅ 已验收，待合并 | 分支 `codex/b1-workspace-audit` |
| A1+B1 接缝 | ⬜ 下一步 | 执行单 `docs/A1B1_MERGE_SEAM.md` |
| A2 包月参数 | ⬜ 执行单 `docs/A2_PREPAID_DESKTOP.md` | 接缝合并后开；只交代码与询价，gw2 保持 PostPaid 到 A3 |
| B2 / B3 记账与对账 | ⬜ **移到里程碑二开头** | 顺序 B2 计量层 → B3 对账 → B2 定价与账本 → B4；见「里程碑二」首段 |
| A3 / B4 / A5 / C5 / E1 / E2 | ⬜ | C5 可随时开；A5 等 A3 合并后；B4 等 B2；E1/E2 等文案 |

---

## 0. 本轮新决策（2026-09-03，覆盖旧文档中的对应条目）

| 项 | 决策 |
|---|---|
| 积分面值 | **每 200 元对应 100 积分**（含体验套餐）。此前「1 积分 = 1 元」作废，费率表按新面值折算。 |
| 套餐三档 | **体验**：免费赠送，半个月（15 天）+ 100 积分，附带一台云电脑（从预热池分配）；**专业版** 499 元/月，259 积分；**旗舰版** 1999 元/月，1000 积分。 |
| 模型与视频对外定价 | 做成**可配置**：文件默认值 + 后台可改的覆盖表，先填占位值，渠道变动时只改配置。 |
| 预热池水位 | **5 台**。 |
| 平台账号首批 | **抖音创作者中心、抖音来客**。 |
| 支付渠道、官网文案、首页引导场景清单 | 晚些给；本计划为它们留接口和占位。 |
| 里程碑三、四 | 待定，本文不展开。 |
| **v2 桌面归属** | **桌面归 workspace，一个订阅一台桌面。** 旗舰版 20 席共用这一台，action server 的 `desktop_lease` 串行化多个使用者。`cloud_desktops` 以 `workspace_id` 为归属键，`user_id` 只记「谁触发的开通」。 |
| **v2 网关 token 粒度** | **一个 workspace 一个 new-api token**（token_name = workspace id）。用户归属由 `usage_events.user_id` 记，对账按 `request_id` 精确匹配，不靠 token_name 分用户。 |
| **v2 A1 拓扑** | 先花半天验证「标准办公网络 + VPC 直连」；不成立再做「gw2 自任中继 + 每桌面密钥 + `AuthorizedKeysCommand`」。**不做 frpc**（bossip 的 frps 已退役，现行舰队用的就是每桌面反向 SSH）。 |
| **v2 金镜像** | A1 第 0 步重做**无秘密金镜像**（40–50G 盘）。现镜像烘焙了隧道单元、SSH 私钥、action server 密钥，全体用户桌面同钥同密。 |

> 待确认：499 元按面值应为 249.5 积分，用户给的是 259。本文按 259 写，配置项可改。
> 待确认：体验套餐是免费的，谁能领？开放给所有新注册用户，等于恢复免费档；只由运营/邀请码发放，则与「免费试用推迟到推广前」一致。本文先按**运营或邀请码发放**写，开放注册领取留到里程碑四。

---

## 1. 通用约定（所有工作项遵守）

- **边界**：新表一律带 `workspace_id`；用户级数据通过用户的默认 workspace 归属。
  **v2** 计费、桌面、平台账号的归属**取会话行的 `workspace_id`**，不取请求头；请求头 `X-Workspace-Id` 只用于列表/创建类接口。
- **迁移**：Alembic 一个工作项一个迁移文件，可 `downgrade`；表名 snake_case。
  **v2** 单测的库是 ORM `create_all` 建的 sqlite 内存库，不走 alembic；涉及回填的迁移要照 `tests/unit/test_remove_skill_runtime_migration.py` 单独写测试。
  部分唯一索引同时给 `postgresql_where` 和 `sqlite_where`（`db/models/cloud_desktop.py` 有先例）。
  **v2** alembic 单头：B1 的 workspace 迁移先合 main，A1 从其后起分支，避免分叉。
- **计费三铁律**（口径沿用 bossip，实现是新写）：套餐能力只做展示与调度，不进权限门；读改写包事务并对余额行加锁；所有金额变动都走账本，不直接改余额。
  **v2** 注意：bossip 只实现了第一、三条，第二条（余额行锁）和余额硬闸在 bossip 里不存在，不要去找「现成代码」。
- **v2 内置周期任务**：openbox 没有内置周期任务的注册点（现有 reaper/warmup/视频恢复都塞在 `cron/timer.py` 的 `finally` 里）。
  B1 先提供 `backend/cron/internal_tasks.py`：`register(name, interval_sec, fn)`，单实例锁（复用 `_claim_job` 的原子抢占思路），
  失败退避与最近一次结果可查。B3 对账、A3 巡检、A5 探活、B4 订阅巡检全部经它注册，**不再各自改 timer.py**。
- **开关**：`BILLING_MODE=off|shadow|enforce`（默认 `off`）；`users.role = developer` 豁免所有计费闸。
- **前端**：`frontend-v2` 改动必须过 `npm run check`（i18n 对齐 + lint + tsc + 单测）；locale 文件 zh-CN/en-US 同步；移动端 locale **逐字节复制**。
  **v2** 移动端没有 locale 对齐脚本，E2 之前先补 `mobile/scripts/check_locales.sh`（与 frontend-v2 逐字节比对）。
- **后端测试**：`uv run pytest tests/unit` 全绿（main 上既有的 3 个失败依赖本机 openbox.json，不算回归）。
- **提交**：中文提交信息，写清为什么；一个工作项一个或多个提交，不混。
- **不要动的**：`permission/` 权限门；视频工具的幂等键与恢复逻辑；共享桌面的旧路径在 A1 完成前保持可用。

---

## 里程碑一 · 打通

目标：一个 workspace 一台桌面真正可用；团队空间可用。包含 B1、A1、A1+B1 接缝、A2。
**v3（2026-09-03 晚拍板）**：B2、B3 移出里程碑一，放到里程碑二开头；里程碑一不再含记账。理由见 `DETAILED_PLAN_M1_M2_REVIEW.md` 之后的对话记录：记账在里程碑一里只是攒数据，不产生对外能力；影子期在里程碑二开发期间同步积累，不会白等。

### B1 Workspace + 审计 + 后台骨架（前置，第一周开工）

> **独立执行单**：`docs/B1_WORKSPACE_AUDIT.md`（与 A1 并行；不碰 `cloud_desktops`，`cloud_desktops.workspace_id` 留到 A1 合并后的接缝迁移；会话协作默认「成员只读」需确认）。

**改哪里**：`backend/db/models/`、`backend/db/migrations/`、`backend/auth/`、`backend/api/`、`backend/cron/internal_tasks.py`（新）、`frontend-v2/src/features/settings`。

**数据**
- `workspaces(id, name, owner_user_id, plan_id nullable, created_at, updated_at, is_deleted)`
- `workspace_members(workspace_id, user_id, role: owner|admin|member, status: active|invited|removed, created_at)`，主键 (workspace_id, user_id)
- `workspace_invitations(id, workspace_id, email_or_username, role, token_hash, expires_at, accepted_by, created_at)`
- 迁移回填：每个现有用户建一个同名默认 workspace，owner = 自己。
  **v2 加列规则**：只给「会话之外创建的行」加 `workspace_id`：`sessions`、`projects`、`cloud_desktops`、`file_assets`、`cron_jobs`、`user_skills`、`user_memories`、`video_productions`；
  `messages/parts/video_jobs/image_gen_cache/todos/prompt_history/skill_installs/permission_rules` 经 `session_id` 或 `user_id → 默认 workspace` 推导，不加列。
  加列都走「可空 → 回填 → 非空」。
- **v2** `audit_logs` 现有列 `user_id, action, resource_type, resource_id, details, ip_address, user_agent`，本项迁移加 `workspace_id nullable`；
  `audit.record(actor_user_id, workspace_id, action, target_type, target_id, detail)` 直接用现有 `PgAuditRepo`，并在注册、登录失败锁定、桌面开通/删除、技能发布/卸载、后台操作处调用。

**接口**
- 请求上下文：`get_current_user` 之外增加 `get_workspace(request)`：取头 `X-Workspace-Id`，**缺头 = 用户默认 workspace（不是 400，移动端里程碑三前不带头）**，校验成员关系，缓存到 request.state。
- `GET /api/workspaces`、`POST /api/workspaces`、`GET /api/workspaces/{id}/members`、`POST .../invitations`、`POST /api/invitations/{token}/accept`、`DELETE .../members/{user_id}`。
- 后台骨架：`/api/admin/*` 统一走 `require_admin`（`users.role in {admin}`），首批：`GET /api/admin/users?q=`、`GET /api/admin/workspaces/{id}`、`GET /api/admin/audit?workspace_id=`。角色先复用 `users.role`（`user|developer|admin`），不单独建 operator 表；细粒度运营角色留到里程碑三。
- **v2** `cron/internal_tasks.py` 注册原语（见通用约定），本项交付并用一个空任务验证单实例锁。

**前端**
- 设置页加「团队」页：成员列表、邀请、角色。侧栏顶部 workspace 切换器（个人默认可隐藏切换）。
- 所有 API 调用带 `X-Workspace-Id`（从 store 取）。

**验收**
- 新注册用户自动拥有默认 workspace；邀请 → 接受 → 成员可见对方 workspace 的会话列表。
- 审计表有记录；`/api/admin/users` 非 admin 返回 403。
- 单测：迁移回填（alembic 路径）、成员校验、邀请过期、内置任务单实例锁。

### A1 每桌面执行通道（前置，第一周开工）

> **独立执行单**：`docs/A1_DESKTOP_CHANNEL.md`（目标 / 现状 / 必要资料 / 方案 / 验收 AC-1…11 / 测试 / 证据清单），交给 Codex 目标模式执行时以该文为准；本节是摘要。
> 归属接缝：A1 阶段 `cloud_desktops` 仍以 `user_id` 为键，经 `sandbox/ownership.py::owner_for()` 抽象，B1 落地后切到 workspace。

**背景**：`WuyingProvider.routes_per_user = False`，所有命令都送到共享桌面 `ecd-4zjxaq5g45dr5qr0i`；`api/desktop.py::_per_user()` 因此把 per_user 闸住。
现办公网络 `cn-shanghai+dir-2879607125` 是**便捷型**，桌面没有 VPC、没有入站路由。

**v2 第 0 步：无秘密金镜像（先做，不等拓扑结论）**
现镜像（openbox-image-v1 / -shanghai）实测烘焙了：`openbox-tunnel.service` enabled 且指向 `-R 127.0.0.1:18000 root@47.110.66.89`、`/root/.ssh/openbox_tunnel` 私钥、
action server 单元内联 `SESSION_API_KEY`。要求：
1. 在 40–50G 系统盘的机器上重做（现 90G 是「制作镜像那台机的盘」，每台桌面每月多付 90G 盘钱）；
2. 镜像里：无 `/root/.ssh/*`、`openbox-tunnel.service` disabled 且 ExecStart 读 `/etc/openbox/tunnel.env`、
   `openbox-action-server.service` 改 `EnvironmentFile=/etc/openbox/action.env`；
3. `container/action_server.py` 的 `if SESSION_API_KEY and ...` 改为**密钥为空拒绝启动**；
4. 出镜像前跑 bossip 同款净化断言：`grep -rl` 私钥/密钥全盘 0 命中，写进 `scripts/wuying_image_verify.py`；
5. 上海、杭州各一份，`WUYING_IMAGE_ID` 切过去；存量 prod 桌面在 A1 联调时用 RunCommand 覆盖单元与密钥。

**v2 拓扑决策（半天验证，二选一）**
- **方案甲（优先验证）**：新建**标准办公网络**挂到 bossip-gw-vpc（或 CEN 同地域互通），后端直连桌面私网 IP（`DescribeDesktops.network_interface_ip`）。
  成立则**不需要隧道层**，`cloud_desktops` 记 `private_ip` 即可；新桌面建在新办公网络，存量桌面不迁。验证内容：建办公网络 → 建一台桌面 → gw2 `curl http://<ip>:8000/alive`。
- **方案乙（甲不成立时）**：**gw2 自任中继**（公网 106.15.105.236），桌面 `ssh -R 127.0.0.1:<port>:127.0.0.1:8000` 直接打到 gw2；后端容器经 host 网络访问 `127.0.0.1:<port>`。
  **每桌面一把 ed25519 密钥**（开通时在桌面生成，公钥回传），gw2 sshd 用 `AuthorizedKeysCommand` 调后端内部接口 `GET /internal/tunnel-keys?fingerprint=`，
  后端按指纹查 `cloud_desktops` 返回 `restrict,port-forwarding,permitlisten="<port>" <pubkey>`；撤销 = 行状态改 `revoked`。
  不改文件、不需要 ECS RunCommand、没有范围语法问题（`permitlisten` 与 `ssh -L` 都不支持范围）。
  安全组 22 只放行 ECD 出口段，或起第二个 sshd 在专用端口（key-only + `restrict`）。杭州中继 47.110.66.89 仅共享桌面旧路径继续用。

**改哪里**：`backend/sandbox/wuying.py`、`wuying_desktop_service.py`、`wuying_ecd.py`、`scripts/wuying_bootstrap.py`、`scripts/wuying_image_verify.py`（新）、`sandbox/client.py`、`api/desktop.py`、`api/internal.py`（方案乙，`AuthorizedKeysCommand` 接口）、`container/action_server.py`、`docs/WUYING_SANDBOX.md`。

**数据**：`cloud_desktops` 加 `workspace_id`（B1 已加）、`user_id` 改可空、`private_ip nullable`（甲）、`tunnel_port int unique nullable`、`tunnel_pubkey_fingerprint`（乙）、`action_api_key_hash`、`tunnel_state: pending|up|down|revoked`、`last_seen_at`。
端口池 `WUYING_TUNNEL_PORT_RANGE=18100-18999`，分配时事务内选最小空闲。一 workspace 一台的部分唯一索引替换现有一人一台索引。

**流程**
1. 开通/分配桌面时：生成该桌面的 action server API key，RunCommand 写 `/etc/openbox/action.env`；方案乙再在桌面生成密钥、写 `/etc/openbox/tunnel.env`（端口、gw2 地址）、写入 gw2 的 known_hosts 指纹（不要靠 `accept-new`，gw2 重装会卡死）、`systemctl enable --now openbox-tunnel`。
2. `WuyingProvider` 改为按 workspace 解析：`get_client(workspace_id)` → 查 `cloud_desktops` 拿地址与 key → `SandboxClient(base_url=...)`。
   客户端按桌面缓存，`action_api_key_hash`/端口变更时失效。`routes_per_user=True`（名字保留，语义是「按归属路由」）。共享桌面保留为 `WUYING_MODE=shared` 时的路径。
3. 会话启动时从**会话行**取 workspace 再取桌面；没有请求头也能路由（cron、WS、视频恢复都走这里）。
4. 健康：provider 定期 `/alive` 探活，内存态记 `last_seen_at`，**只在状态变化时落库**；桌面未就绪时会话启动返回 `DESKTOP_NOT_READY`，前端引导去云桌面 tab；
   cron 会话遇到它：跳过本次运行并把原因写进 `cron_runs`，不重试。
5. DesktopTab：`_per_user()` 自然放行；开通按钮流程不变。

**验收**
- 两个 workspace 各自开通桌面，各发一条 `bash: hostname`，返回各自桌面主机名；云桌面 tab 看到的画面是各自的机器。
- 同一 workspace 两个成员并发发命令，落在同一台桌面且被 `desktop_lease` 串行化。
- 桌面重启后通道自动恢复，`last_seen_at` 更新；撤销一台桌面后其请求被拒（方案乙：`AuthorizedKeysCommand` 返回空）。
- 新镜像开出的桌面：`ls /root/.ssh` 为空、`systemctl is-enabled openbox-tunnel` 为 disabled、镜像净化断言通过。
- 上海冒烟脚本 full 层增加「通道通 + 一条 bash」断言。

### A2 包月开通

**改哪里**：`backend/sandbox/wuying_ecd.py::create_desktop`、`core/config.py`、`wuying_desktop_service.py`。

- 配置 `WUYING_CHARGE_TYPE=PrePaid`、`WUYING_PERIOD=1`、`WUYING_PERIOD_UNIT=Month`、**`WUYING_AUTO_PAY=true`（v2 漏项，不传只生成待支付订单，桌面不会创建）**、`WUYING_AUTO_RENEW=false`（续期由订阅巡检显式调 RenewDesktops）。仅 PrePaid 时下发 period 参数。
- **v2 鬼桌面**（DB 有、ECD 说没有）是我们的基础设施问题，不动订阅状态：对 PrePaid 桌面改为「行标记 `reclaimed` + 报警 + 由 A3 池重新 `assign` 一台」，不调 DeleteDesktops。
- 到期语义：见 B4；本项只保证创建参数与清理分支正确。
- **v2** 用一台真机核实两件事并写进 `docs/WUYING_SANDBOX.md`：Expired 后的保留天数（本文暂按 15 天）、Expired 期间 `RenewDesktops` 能否直接恢复 Running。

**验收**：**v2** 冒烟只验证请求参数与 `DescribePrice` 询价（不真买）；真买的第一台 PrePaid 桌面直接进 A3 的池当第一台 prewarm，不删。DescribeDesktops 显示 ChargeType=PrePaid、到期日为一个月后。

### B2 计费 M1 影子记账

**改哪里**：新建 `backend/billing/`（`rates.py`、`ledger.py`、`events.py`、`gateway.py`）、`backend/db/models/billing.py`、`agent/loop.py`、`agent/compaction.py`、`tool/bash.py`（LLM 判官）、`agent/llm.py`（标题/过滤调用处）、`tool/video_production.py`、`tool/image_gen.py`、`api/billing.py`、`frontend-v2/src/features/settings/components/UsagePage.tsx`。

**费率与套餐配置（「可配置的地方」）**
- `backend/billing/rates.yaml`：默认售价，单位积分。结构：
  ```yaml
  version: 2
  credit: { cny_per_credit: 2 }          # 每 200 元 100 积分
  llm:
    gpt-5.6-luna: { input_per_million: 1.0, cache_read_per_million: 0.5, output_per_million: 3.0 }
    # 其余模型先填占位，渠道稳定后按成本×毛利改
  video:
    wan3.0-video: { "480p": {per_second: 0.3}, "720p": {per_second: 0.5}, "1080p": {per_second: 0.8} }
  image: { default: { per_call: 0.3 } }
  stt:   { fun-asr: { per_minute: 0.05 } }
  ```
- `credit_rates` 表（后台覆盖，优先于文件）：`(kind, model, tier, unit, price, active, updated_by, updated_at)`；`GET/PUT /api/admin/rates`。
- `backend/billing/plans.yaml`：
  ```yaml
  plans:
    - id: trial   name: 体验    price_cny: 0     # 免费赠送，运营/邀请码发放
      period_days: 15  credits: 100  grant_only: true
    - id: pro     name: 专业版  price_cny: 499   period: month  credits: 259
    - id: max     name: 旗舰版  price_cny: 1999  period: month  credits: 1000
  ```
  **v2** `credits` 是唯一事实，`price_cny / cny_per_credit` 只用于展示换算；后台改价只改 `credits` 与 `price_cny`，不联动。

**数据**
- `usage_events(id, workspace_id, user_id, session_id, message_id, part_id, modality: llm|video|image|stt, provider, model, unit_type, quantity, input_tokens, output_tokens, cache_tokens, cost_credits numeric(12,4), ledger_id, status: estimated|confirmed|gateway_only|unbilled|shadow, reused bool, gateway_request_id, idempotency_key unique, raw jsonb, created_at)`
- `credit_ledger(id, workspace_id, kind: grant|topup|usage|adjust|refund, amount numeric(12,4) 带符号, balance_after, ref_type, ref_id, idempotency_key unique, note, actor_user_id, created_at)`
- `workspace_balances(workspace_id pk, balance, updated_at)` 作为加锁行：**v2** 写入用 `SELECT ... FOR UPDATE`（SQLAlchemy `with_for_update()`，sqlite 单测静默降级），同一事务内插 ledger + 更新余额。
- **v2** `gateway_tokens(workspace_id pk, newapi_token_id, token_name, key_ciphertext, synced_quota, synced_at)`——一 workspace 一个。
  加密密钥 `BILLING_TOKEN_KEY`（32 字节，env 注入），密文带版本前缀便于轮换。

**落账点（五处 + 中断）**
1. `agent/loop.py` step_finish：幂等键 = part id；同时把 LLM 响应头 `X-Oneapi-Request-Id` 带出来。
   **v2** openbox 有两条 LLM 路径：Responses API 自建 httpx（响应头直接可读）和 litellm 流式（需 `litellm.return_response_headers = True`，
   从 `_hidden_params["additional_headers"]` 取，键带 `llm_provider-` 前缀）。**先在 1.81.11 上验证流式对象带这个字段**，验证结果写进本条。
2. 标题生成（`loop.py` 直接调 litellm 处）、bash LLM 判官（`tool/bash.py`）、MCP 过滤模型、压缩分段摘要：统一改走一个 `billing.record_llm(...)` 包装，幂等键 = 调用点 + 消息/命令 id。
3. 压缩（`compaction.py`）：本身的 token 落账；**不再把会话累计清零**（改为只重置 context）。**v2** 现代码连 `cost` 一起清零、压缩调用本身没有 cost 字段，在途修复 789b31e 只改了每步覆盖，这两处本项修。
4. 视频（`video_production.py` 提交成功点，幂等键 = 已有 idempotency_key）：**v2** 预扣按「该模型最大时长 × 分辨率」估算，`-1` 智能时长尤其如此；终态按实际补正（见 B4 第 7 条）。
   图片（`image_gen.py` 成功点）：**v2** 跨用户缓存命中走 `reused=true, cost 0`。转写（成功点）。
5. 中断：流被 abort 时按已收到的输出估算并落一条 `status=estimated` 事件，等对账用网关数补正。
- **v2** `BILLING_MODE=shadow`：**只写 usage_events（status=shadow），不写 ledger、不动余额**；`enforce` 才写 ledger 扣减。切 enforce 时**不追扣** shadow 期间的事件。

**费用计算**：`billing/rates.py::price(modality, model, units)`，替换两处 `litellm.completion_cost`（1.81.11 没有 `prompt_tokens` 参数，现有调用恒抛异常归 0）。

**接口与前端**
- `GET /api/billing/summary`（余额、本期用量按 modality）、`GET /api/billing/events?from&to&modality`。
- UsagePage 改读 `usage_events` 聚合（后端分页），保留上下文进度条读会话字段。移动端暂不动（里程碑三）。

**每 workspace 网关 token（为对账归属，提前到 M1）**
- workspace 首次产生调用时，经 new-api 管理 API（`NEWAPI_BASE_URL/ACCESS_TOKEN/USER_ID`，见 RELAY_CUTOVER §2A）创建 token：name = workspace id，group `openbox`，unlimited；密文存 `gateway_tokens`。
- `agent/llm.py` 按会话行的 workspace 解出 key 覆盖 `api_key`（两条路径都改）；视频/图片同理。开发模式与 `BILLING_MODE=off` 仍用共享 token。

**验收**
- 一个 10 步会话后，`usage_events` 有 10 条 llm 事件 + 标题事件 + 压缩事件；`cost_credits` 非 0；会话累计与事件求和一致，压缩后累计不清零。
- 一条视频、一张图片各一条事件，幂等重放不重复；图片缓存命中记 reused。
- shadow 模式下 ledger 无新行、余额不变；切 enforce 后余额按事件扣减（单测，含两个并发扣减不丢更新）。

### B3 对账

**改哪里**：`backend/billing/reconcile.py`，**v2 经 `cron/internal_tasks.py` 注册**。

- 每 5 分钟拉最近 30 分钟、每晚 03:00 拉前一天：`GET /api/log/self` 按 token_name 与时间窗分页（已核实支持 `token_name/start_timestamp/end_timestamp/request_id`，`other` 含 `cache_tokens/end_reason/request_path`）。
- 匹配规则见 BILLING_PLAN §4.6：按 `gateway_request_id` 校正（差额写 adjust）、网关有我们无 → `gateway_only` 补记并扣、我们有网关无 30 分钟 → `unbilled` 并报警。
- 视频例外：费用以 `video_jobs` 终态 × 费率为准，网关只核对提交存在。
- 报警：写 `audit_logs` + 后台 `GET /api/admin/billing/alerts`；日漂移 > 5% 报警。

**验收**：人为制造三类差异（改事件 token 数、删一条事件、多插一条事件），对账后分别得到 adjust、gateway_only、unbilled。

### 里程碑一验收（v3）
- 新用户注册 → 其 workspace 拿到桌面 → 发任务在这台桌面上完成（A1 + 接缝）。
- 同 workspace 两个成员在同一台桌面上执行（接缝 AC-2）。
- Workspace 邀请可用，审计有记录（B1）。
- 包月参数与询价可用，未产生采购（A2）。
- ~~shadow 记账跑满一周~~ → 移到里程碑二。

---

## 里程碑二 · 可收费

目标：真实付款开通并按月续；出片质量与首屏表达跟上。包含 **B2、B3**（v3 移入）、B4、A3、A5、C5、E1、E2。

**v3 记账顺序（硬规则）**：B2 拆三层——计量层（`usage_events`、五处落账点、压缩不清零、请求 ID、每 workspace 网关 token）、定价层（`rates.yaml`、`credit_rates`、`price()`）、账本层（`credit_ledger`、`workspace_balances`、行锁、shadow 开关）。
执行顺序 **B2 计量层 → B3 对账 → B2 定价与账本 → B4**；对账只依赖计量层，先把「事件能对上网关日志」验掉。
**`BILLING_MODE=enforce` 只在真实流量上影子跑满一周、日对账漂移 < 5% 之后才打开**，写进 B4 验收，不随排期妥协。

### B4 套餐、支付、硬闸

**改哪里**：`backend/billing/subscriptions.py`、`payment/`（接口 + 首个实现 + 后台代付）、`api/billing.py`、`api/desktop.py`、`agent/loop.py`（回合前预检）、`frontend-v2` 新增 `features/billing`。

**数据**
- `subscriptions(id, workspace_id, plan_id, status: active|past_due|expired|canceled, period_start, period_end, source: payment|direct_grant|redeem, desktop_id nullable, payment_ref, created_at, updated_at)`；同一 workspace 至多一条 active（部分唯一索引）。**一订阅一桌面**，`desktop_id` 指向 `cloud_desktops`。
- **v2** `orders(id, workspace_id, plan_id, amount_cny, status: pending|paid|failed|refunded, provider, provider_order_id, idempotency_key unique, created_at, paid_at)` 记订单态；
  `payments(id, order_id, provider, provider_ref, amount_cny, status, raw, created_at)` 记渠道回执（重复回调、部分退款都落这里）。
- **v2** `plans.yaml` 的 trial 段加 `max_grants_per_day`（运营发放上限）；一个体验账号成本约 ¥325（100 积分 ≈ ¥200 用量 + 15 天桌面），要有闸。

**流程**
1. **开通**：`POST /api/billing/subscribe {plan_id}` → 建 order（provider 待定；测试期 `direct_grant` 由后台 `POST /api/admin/billing/grant {workspace_id, plan_id}`）→ 支付回调/代付成功 → order paid → `subscriptions.active` → 发放积分（`credit_ledger.grant`，幂等键 `grant:<sub_id>:<period_start>`）→ 确保网关 token → **触发桌面分配**（A3 的 `assign(workspace)`，无池时退回 A1 的即时开通）。
2. **续费**：到期前 3 天提醒；付款成功 → 追加周期、发放下期积分、调 RenewDesktops 一期。
3. **到期**：`period_end` 过 → `past_due`；桌面不再续期，阿里云到期后进入保留期（天数以 A2 核实为准）；宽限内续费 → RenewDesktops + 恢复；宽限后 → `expired`，桌面由池回收（A3 `release`），积分冻结不清零。
   **v2 past_due 语义**：包月桌面到期即停，past_due 期间**不能用桌面**；回合前预检对 past_due 直接拒（错误码 `SUBSCRIPTION_PAST_DUE`，文案「续费后恢复」），不做「放行不需要桌面的会话」这种半开状态。
4. **体验套餐**：免费，`period_days=15`，只能经后台代付或邀请码发放（`grant_only`），同一 workspace 只能领一次；开通时同样从预热池分配桌面；到期规则同上，到期未升级则桌面释放。
5. **回合前预检**（`loop.py` 入口，`BILLING_MODE=enforce` 时）：订阅 active 且余额 > 0，否则拒绝，错误码 `SUBSCRIPTION_REQUIRED` / `SUBSCRIPTION_PAST_DUE` / `CREDIT_EXHAUSTED`（沿用 `auth/quota.py` 的 429 结构）。developer 角色豁免。
   **v2** 余额阈值留 5% 给网关 pre-consume（new-api 按 max_tokens 预扣，余额贴零时网关会先拒）。
6. **影子上限**：每次 ledger 写入后异步推 `remain_quota := balance_credits × cny_per_credit × QuotaPerUnit`（QuotaPerUnit 从网关读，现为 500000/元）到 workspace token；失败重试，连续失败报警。
7. **视频扣费**：提交成功时按「最大时长 × 分辨率」预扣（estimated），终态后按实际时长/分辨率补正。
8. **v2 存量用户**：上线 enforce 前一次性给生产存量用户（含 ecd-demo）developer 角色或代付一期，写成迁移脚本 `scripts/billing_cutover.py`。
9. **v2 订阅巡检**（每小时，经 `internal_tasks`）：`period_end` 过期 → past_due；宽限过 → expired + `release`；到期前 3 天提醒。

**前端**
- `/billing`：三张套餐卡（体验/专业/旗舰）、余额、本期用量、订阅状态与到期日、续费按钮、兑换码入口（占位）。
- DesktopTab：无有效订阅时 CTA「开通套餐」跳 `/billing`；有订阅但桌面未就绪显示分配中。
- 回合前错误码文案与按钮（三条）。
- 移动端：里程碑三。

**验收**：后台代付开通 → 桌面分配 → 使用 → 人为把 period_end 调到过去 → past_due（回合被拒、文案正确）→ 代付续费 → active，全程无人工进库；真实支付渠道到位后再跑一遍付款路径，含一次重复回调不重复发放。

### A3 ECD 池（五态、预热 5 台）

**改哪里**：`backend/sandbox/pool.py`（新）、`wuying_desktop_service.py`、`wuying_ecd.py`、`api/admin_desktops.py`（新）、巡检任务（经 `internal_tasks`）。

**数据**：`cloud_desktops` 扩列：`pool_state: prewarm|assigned|released|reclaimed|recycling`（**v2 与现有 `status` 分开，`status` 继续记 ECD 运行态**）、`pool: trial|paid|internal`、`assigned_workspace_id`（即 `workspace_id`，分配时写）、`assigned_at`、`expires_at`（ECD 侧 `expired_time` 的镜像）、`spec`、`charge_type`、`golden_image_id`；
标签同步写到 ECD 实例（`openbox-pool`、`openbox-workspace`、`openbox-expires`），DB 为事实源，标签用于收养与对账。
**v2** 标签读写只用 `ALIYUN::GWS::INSTANCE` 一套命名空间（现有 `ListTagResources` 就是），不要写 `ALIYUN::GWS::DESKTOP`（bossip 踩过：两套互不可见，会把在用机当预热机派出去）。

**v2 首台预热机**：上海现存包月桌面 `ecd-8zp47qagrsc95h67t`（openbox-dev-shanghai，旧镜像 v1，2026-09-01 购）到 A3 时 `RebuildDesktops` 到 v2 镜像后作为第一台 `prewarm` 入池，不再另买；在此之前保持不动。

**状态机**
- `ensure_prewarm`：巡检每 10 分钟，**以 ECD 侧按标签计数为准**（DB 只做校对），低于 **5** 时补购（PrePaid 一个月 + `auto_pay`，金镜像，策略组 1080p）。
  **v2 采购四闸**：`DescribePrice` 与 `POOL_MAX_UNIT_PRICE_CNY` 比对；账号余额预检 `bssopenapi QueryAccountBalance ≥ 单价 × 2`；一次最多补 2 台；`POOL_MAX_PURCHASES_PER_DAY`（默认 4）。任一闸不过拒绝并报警。
  **v2 预热机到期**：`WUYING_AUTO_RENEW=false` 下闲置超一个月会被阿里云释放；巡检对 `prewarm` 在到期前 3 天 `RenewDesktops` 一期（或退役，按 `POOL_PREWARM_MAX_AGE_DAYS`）。
  可选省钱路线（配置项 `POOL_PREWARM_CHARGE_TYPE=PostPaid`）：预热机按量买，`assign` 时 `ModifyDesktopChargeType` 转包月。
- `assign(workspace)`：事务内取一台 `prewarm` → `ensure_end_user`（EndUser 同步约 60 秒；**注册时预建 EndUser 以缩短**）→ **`ModifyEntitlement`**（不存在「ModifyDesktop 归属」）→ 重写 `openbox-user/openbox-eu-id/openbox-workspace` 标签（出票 API 校验的是标签）→ **若 `expired_time < 订阅 period_end` 先 `RenewDesktops` 对齐** → 安装该桌面的通道与 key（A1 步骤 1）→ `verify`（通道通 + 一条 bash + 分辨率 1920x1080）通过才置 `assigned`；失败回滚为 `prewarm` 并报警。
- `release`：订阅 expired → 关闭访问（撤销 action key、通道行置 `revoked`）→ `released`（数据与登录态保留）。
- `reclaim`：released 超过保留期（与阿里云到期一致）→ 桌面到期自然释放或人工 `recycle`。
- `recycle --approve`：后台一键（需二次确认）：先撤通道行 → `RebuildDesktops` 金镜像 → 重跑 `verify` → 回 `prewarm`。

**接口**：`GET /api/admin/desktops?pool_state=`、`POST /api/admin/desktops/{id}/{release|recycle}`、`POST /api/admin/pool/ensure`。

**验收**：池初始化后 5 台 prewarm；开通套餐**不等 CreateDesktops，2 分钟内 assigned**（v2，原「3 秒」不成立）；释放后 prewarm 自动补到 5；补购价格超阈值 / 余额不足 / 日上限触顶时拒绝并有报警记录；分配后 `expired_time ≥ period_end`。

### A5 授权中心 v0（登记 + 在线检测）

**首批平台**：抖音创作者中心（creator.douyin.com）、抖音来客（life.douyin.com / 来客商家后台）。

**改哪里**：`backend/db/models/platform_account.py`、`backend/db/models/notification.py`（**v2 新，两边都没有通知模型**）、`backend/platform_accounts/`（新：`service.py`、`probe.py`、`platforms.py`）、`api/platform_accounts.py`、`api/notifications.py`（新）、`tool/platform_login.py`（新工具）、`internal_tasks` 注册探活、`frontend-v2/src/features/settings` 新页「平台账号」、技能元数据。

**数据**
- `platform_accounts(id, workspace_id, desktop_id, bound_by_user_id, platform: douyin_creator|douyin_laike, status: unbound|bound|expired|revoked, nickname, external_id nullable, bound_at, last_probe_at, last_ok_at, last_error, created_at, updated_at)`，唯一 (workspace_id, desktop_id, platform)。桌面归 workspace，账号自然也归 workspace。
- **v2** `notifications(id, workspace_id, user_id nullable, kind, title, body, read_at, created_at)` + `GET /api/notifications`、`POST .../{id}/read`；设置页红点读未读数。

**平台定义**（`platforms.py`）：每个平台一条记录：登录页 URL、已登录判定、昵称来源、探活方式、退出方式。
**v2** 探活不整页导航：用 CDP `Network.getCookies` 判该域会话 cookie 存在 + 调一个轻量 JSON 接口（如创作者中心的用户信息接口）取昵称与状态码；
退出用 `Network.deleteCookies(name, domain)` 逐条或 `Storage.clearDataForOrigin(origin, "cookies")`（`Network.clearBrowserCookies` 是清全部，不能用）。

**流程**
1. **绑定**：用户在设置页点「绑定」→ 后端调工具 `platform_login.open(platform)`：在该 workspace 桌面的 Chrome（现有 dev-browser CDP）打开登录页并置顶 → 前端把云桌面 tab 切到前台提示扫码 → 用户扫码/验证 → 前端轮询 `POST /api/platform-accounts/{platform}/probe` → 探活通过写 `bound`、抓昵称。agent 永远不代填验证码。
   **v2** 人扫码用的 Chrome 与 dev-browser 必须是**同一个 `--user-data-dir`**，否则登录落在另一个 profile；写进验收。
2. **探活**：`internal_tasks` 每天 06:00 对每台 `assigned` 桌面上的 `bound` 账号跑一次 `probe`（取 `desktop_lease`，不截图存储）；失败 → `expired`，写 `notifications`。
3. **阻断**：**v2** SKILL.md frontmatter 加 `requires-platforms: [douyin_creator]`（openbox 现只有 `allowed-tools`）；技能加载时把它带进工具调用上下文，
   浏览器类工具在执行前查状态，非 `bound` 时返回结构化错误 `PLATFORM_AUTH_REQUIRED {platform}`，agent 转述给用户并引导去绑定，不自行重试登录。
4. **撤销**：按上面的退出方式清该域 cookie → `revoked`。

**接口**：`GET /api/platform-accounts`、`POST /api/platform-accounts/{platform}/bind`、`POST .../probe`、`DELETE .../{id}`。

**前端**：设置页「平台账号」：两张卡（抖音创作者中心、抖音来客），状态、昵称、上次检测时间、绑定/重新登录/解绑按钮；绑定时联动打开云桌面 tab。

**验收**：用真实账号在上海桌面完成一次绑定，状态 bound 并显示昵称；手动清 cookie 后次日探活变 expired 且通知出现、红点亮；解绑后状态 revoked；探活前后用户桌面上没有新开的页面。

### C5 视频生成技能规则细化

**改哪里**：`backend/.openbox/skills/video-production/`（SKILL.md、references/、scripts/），对照 bossip `apps/center/skills/bossip-video-production/`。

**做法**
1. 先做一份对照表。**v2** bossip 的真实阶段机是 `scripts/gate.py` 的
   `init → script_ok → segments_ok → spend_ok → generated → stt_ok → composed → delivered`，五个必停点（讲稿、分镜、**费用**、STT 自检、成片形式），
   核心机制是第 6 步的花钱闸与 `contentHash` 变更即 STALE；字幕是 §9 的可选项，不是独立关。对照表按这个起草，逐条标注 openbox 现状「已有 / 缺 / 不同」。
2. 按关卡逐关补写 references 与脚本检查（lint），每补一关实拍一条 15 秒口播验证。
3. 规则只放知识层，不改工具层；涉及计费、凭据、幂等的规则一律不进技能。
   **v2** bossip 技能里的 `durationSec: -1` 与其计费漏账绑定（提交时按 1 秒计），对照时标「有意不同」，openbox 的 `-1` 由 B2 按最大时长预扣。

**验收**：对照表全部条目为「已有」或「有意不同（写明理由）」；三条不同题材的口播成片由产品同事评审通过。

### E1 官网文案重构、E2 空白对话引导重构

**改哪里**：`frontend-v2/src/locales/{zh-CN,en-US}/landing.json`、`workspace.json`；`mobile/assets/locales/*` 逐字节同步；`mobile/scripts/check_locales.sh`（**v2 新**）；如需结构调整再动 `features/workspace` 的空态组件。

- E1：等文案定稿后替换；不改布局；英文由文案组给或先机翻标注待校。
- E2：引导改为业务场景 chips，每条 = 标题 + 一句副文案 + 预填 prompt；来源是市场给的 6 到 8 条清单，先用占位（制片、图文、账号运营三类各两条）。chips 点击可联动模式（执行/对话）与技能（C5 就绪后联动 video-production）。

**验收**：`npm run check` 过 i18n 对齐；`mobile/scripts/check_locales.sh` 通过（先补脚本，再谈验收）；产品同事在 ai.bossipai.com.cn 上确认。

### 里程碑二验收
- 真实用户：付款（或代付）→ 开通 → 使用 → 到期 → 续费，全程无人工进库。
- 预热池 5 台，开通即得。
- 抖音创作者中心与来客各完成一次绑定、一次探活失效提醒。
- 口播成片规则对照表清零；官网与首页引导为业务口径。

---

## 附：建议开工顺序（三人并行）

| 周 | 人 1（桌面线） | 人 2（计费线） | 人 3（技能与表达） |
|---|---|---|---|
| 1 | A1 第 0 步无秘密金镜像 + 拓扑半天验证（甲/乙定案） | B1 Workspace/审计/`internal_tasks`（迁移先合） | C5 对照表 + 前两关 |
| 2 | A1 通道（按定案方案） | B1 收尾 + B2 数据模型与费率 | C5 |
| 3 | A2 包月 + A1 收尾 | B2 影子记账 | C5 后三关 |
| 4–5 | A3 池 | B2 收尾 + B3 对账 | E2 引导（占位文案）+ 移动端 locale 脚本 |
| 6–7 | A3 收尾 + A5 授权中心 | B4 套餐与硬闸（代付先行） | E1 文案（待定稿） |
| 8 | A5 收尾、联调 | B4 支付渠道接入（待资质）+ 存量用户切换 | 联调、验收 |
