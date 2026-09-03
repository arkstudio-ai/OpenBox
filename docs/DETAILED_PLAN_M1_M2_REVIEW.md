# 对 DETAILED_PLAN_M1_M2 的对抗审查

> 2026-09-03。依据：openbox `main`（b8b68e0）与 bossip（06270a65）两仓实查、new-api 源码、
> ECD SDK（alibabacloud_ecd20200930）字段、以及对一台现存 per_user 桌面（ecd-2oi2bfla5bl9erw2h）的只读探查。
> 结论分三级：**阻断**（不改就做错或做不下去）、**必须补**（计划没写、编码时一定会撞上）、**建议**。
> 每条给证据位置，方便直接回写进计划。
> **状态**：2026-09-03 晚用户拍板「桌面归 workspace，一订阅一台」，本审查已全部回写进 `DETAILED_PLAN_M1_M2.md`（v2，各节标「v2」）。本文保留作证据与理由。

---

## 0. 总判

计划的骨架（先通道、再包月、再池；账本自有、网关硬闸；workspace 垫底）是对的，
但有**一个模型级矛盾**和**三处「照搬 bossip」的事实错误**，会让 A1/A3/B2/B4 四个工作项在联调时互相打架：

1. **桌面到底归用户还是归 workspace，计划两头写。** A1 按 `user_id` 路由（`get_client(user_id)`、`cloud_desktops.user_id`），
   A3/B4 按 workspace 分配（`assign(workspace)`、`subscriptions.desktop_id`、`assigned_workspace_id`）。
   旗舰版 20 席一个 workspace 一台桌面？还是每席一台（¥1999 养不起 20 台 ¥250 的桌面）？这不定，A1 的接口签名、
   A3 的状态机、A5 的唯一键、B2 的 token 归属全都写不对。→ §1.1
2. **bossip 的「三铁律」只有两条是真的。** 「对余额行加锁」在 bossip 里没有实现（`usage-metering.service.ts:349-366`
   是裸 read-modify-write，没有 FOR UPDATE / 隔离级别），bossip 也没有任何余额硬闸（`hasAvailableCredits` 零调用）。
   计划把它当「沿用」，实际是从零写。→ §4.2
3. **bossip 现行舰队不用 frpc，用反向 SSH。** 计划说「frpc 方案作为备选，若团队更熟 frps 可换」，但 bossip 的 frps
   已在退役清单（`docs/2026-08-08-infra-retirement-plan.md:73`「frps 0 已停」），codex 舰队 README 明写「does not use FRP」，
   每台桌面一条 `ssh -R` 到 gw-1 独立回环端口。计划默认方案恰好就是 bossip 现行方案，备选项应删掉，别让人去翻 hermes 时代的死代码。→ §2.1
4. **金镜像里烘焙了秘密和旧隧道。** 实测 per_user 桌面：`openbox-tunnel.service` enabled、`-R 127.0.0.1:18000 root@47.110.66.89`、
   `/root/.ssh/openbox_tunnel` 私钥、action server 单元内联 `SESSION_API_KEY`——**所有用户桌面共用同一把 SSH 私钥和同一个 API 密钥**，
   且每 5 秒向杭州中继重试一次（当前被拒）。A1 第一步不是「写入 action.env」，是先把这些换掉。→ §2.2

其余是工作项内部的疏漏，按项列在后面。

---

## 1. 阻断级

### 1.1 桌面归属模型（影响 A1/A3/A5/B2/B4）

| 计划里的写法 | 按用户 | 按 workspace |
|---|---|---|
| A1 `get_client(user_id)`、`cloud_desktops.tunnel_port` | ✓ | |
| A3 `assign(workspace)`、`assigned_workspace_id`、「期望到期 = 订阅到期」 | | ✓ |
| B4 `subscriptions.desktop_id nullable`（一订阅一桌面） | | ✓ |
| A5 唯一键 `(workspace_id, desktop_id, platform)` | | ✓ |
| B2 `gateway_tokens(user_id pk)` 但余额在 `workspace_balances` | ✓ | ✓ 混用 |
| 现有代码 `cloud_desktops.user_id NOT NULL` + 一人一台部分唯一索引 | ✓ | |

**建议拍板：桌面归 workspace，一个订阅一台。** 理由：套餐钱只够一台；action server 已有 `desktop_lease` 串行化多个使用者
（`sandbox/client.py:305-369`）和 `X-OpenBox-User-Scope`（`:33`）；平台账号本来就是「这台机器上登录了谁」。
落地改动：
- `cloud_desktops.user_id` 改可空并新增 `workspace_id`；一人一台的部分唯一索引改成「一 workspace 一台」；
- A1 provider 接口改 `get_client(workspace_id)`，会话启动时从会话行拿 workspace，不从请求头拿；
- B2 网关 token **一 workspace 一个**（token_name = workspace id）。否则 20 个成员 20 个 token，每个 token 的影子上限都是整份余额，
  同步前可超支 20 倍；对账按 `request_id` 精确匹配，不依赖 token_name 分用户，用户归属由 `usage_events.user_id` 记。

### 1.2 A1 隧道方案的三处硬伤

1. **`permitlisten` 没有范围语法，`ssh -L` 也没有。** OpenSSH 的 `permitlisten="[host:]port"` 只能逐个写；「中继端 permitlisten 改为范围放行」
   和「-L 一段端口范围」都不成立（要写 900 个 -L，且每加一台桌面要重载）。
2. **「密钥为部署级中继密钥」= 所有桌面共用一把私钥。** 任何一台桌面（或拿到镜像的人）都能 `-R` 绑别人的端口；B 桌面隧道一断，A 桌面就能接管 B 的端口，
   后端随后把 B 用户的命令送到 A。现在的 bootstrap 反而是对的：每桌面一把钥匙、authorized_keys 一行一个 `permitlisten` 钉死端口
   （`scripts/wuying_bootstrap.py:291-294`）。**保留每桌面密钥，撤销 = 删那一行。**
3. **中继在杭州（47.110.66.89），后端和桌面都在上海。** 多一跳、多一个单点、还要经 ECS RunCommand 改中继的 authorized_keys。

**建议方案**：中继就是 gw2 自己（公网 106.15.105.236），桌面 `-R` 直接打到 gw2 回环端口，后端容器经 host 网络访问 `127.0.0.1:<port>`。
authorized_keys 用 sshd 的 `AuthorizedKeysCommand` 指向后端一个内部接口（按公钥指纹查 `cloud_desktops` 返回带 `permitlisten` 的行），
分配/撤销即时生效、不改文件、不需要 ECS RunCommand 权限。安全组 22 只放行 ECD 出口段（或起第二个 sshd 在专用端口，
key-only + `restrict`）。

**更省的可能（值得花半天验证）**：现办公网络 `cn-shanghai+dir-2879607125` 是**便捷型**（bossip `ECD_FLEET_PURCHASE_API.md` §1），
所以才没有 VPC。若新建一个**标准办公网络**挂到 bossip-gw-vpc（或 CEN 同地域互通，阿里文档有「ECS 与无影通过 CEN 互通」），
后端可以直连桌面私网 IP（`DescribeDesktops` 返回 `network_interface_ip`），**整个隧道层都不用做**。代价：新桌面要建在新办公网络，
镜像是地域级可复用，存量桌面不能迁。A1 估 2–3 周，这个验证值得排在最前面。

### 1.3 金镜像必须重做（A1 前置，不是 A3 的事）

实测 ecd-2oi2bfla5bl9erw2h（prod 标签，2026-09-03）：

```
systemctl is-enabled openbox-tunnel openbox-action-server → enabled enabled
ExecStart=... -i /root/.ssh/openbox_tunnel -R 127.0.0.1:18000:127.0.0.1:8000 root@47.110.66.89
/root/.ssh: authorized_keys known_hosts openbox_tunnel openbox_tunnel.pub
openbox-action-server.service 含 SESSION_API_KEY → 1 处
journal: root@47.110.66.89: Permission denied (publickey,password)  （每 5 秒一次）
```

后果：(a) 全体用户桌面同一个 action server 密钥，等于没有密钥；(b) 同一把 SSH 私钥；(c) 这把钥匙一旦被授权 18000 端口，
所有克隆都能抢共享桌面的端口。bossip 金镜像 SOP 的标准是「零 secret + `grep -rl` 全盘 0 命中」（`GOLDEN_IMAGE_BUILD.md:31,136-146`）。
计划 A1 步骤 1 要改成：**先做无秘密金镜像**（删私钥、隧道单元 disabled 且不带目标、action server 单元改 `EnvironmentFile=/etc/openbox/action.env`），
开通时再由 RunCommand 写入本桌面的密钥与端口。同时修 `container/action_server.py:271` 的 `if SESSION_API_KEY and ...`：
密钥为空应拒绝启动，而不是放开所有请求（action.env 没写进去时，隧道一通就是裸奔）。

镜像顺带瘦身：现在 90 GiB 系统盘是「制作镜像那台机的盘」而非内容体积（bossip 同一坑，`ECD_FLEET_PURCHASE_API.md` §1），
每台桌面每月多付 90G 的盘钱。重做时在 40–50G 盘的机器上做。

### 1.4 A3 预热池买的是包月，但没对齐订阅到期

预热桌面在补池时购买，到期日 = 购买日 + 1 月；用户订阅时它可能已经用掉 25 天。计划写「期望到期 = 订阅到期」只是写进 DB，
**ECD 侧不会跟着变**，桌面会在订阅中途 Expired。且 `WUYING_AUTO_RENEW=false` 下，池里闲置超过一个月的预热机会被阿里云到期释放。
要补：
- `assign` 时若 `expired_time < period_end` 先 `RenewDesktops`（SDK 有，字段 `period/period_unit/auto_pay/auto_renew`）；
- 巡检对 `prewarm` 也要做「到期前 N 天续期或主动退役」。
- 可选的省钱路线：预热机按量买，`assign` 时 `ModifyDesktopChargeType` 转包月（SDK 有此接口，字段 `charge_type/period/period_unit/auto_pay`），
  5 台闲置池不再按月付整月。

---

## 2. A 线（桌面）

### 2.1 A1 其它疏漏
- 「frpc 备选」删除（见 §0.3）。bossip 可参考的是 `apps/codex/v1/scripts/wuying/setup-reverse-ssh-server.sh` + `ssh-forward-supervisor.mjs`，不是 `_ensureFrpc`。
- `WuyingProvider` 现在没有 `get_client`，传输走 `forward_to_container`（`sandbox/wuying.py:149-161`），客户端是进程级单例。
  改按 workspace 解析后要处理：客户端缓存与失效（端口/密钥轮换后）、`X-OpenBox-User-Scope`、`desktop_lease` 的 token 是按桌面的。
- 健康探活写 `last_seen_at`：每台桌面每次探活一次 UPDATE，100 台桌面 × 30 秒一次就是持续写压力；用内存态 + 变化时才落库。
- `DESKTOP_NOT_READY` 在会话启动返回：cron 会话（`sessions.kind=cron`）没有前端引导，要定义 cron 遇到它的行为（跳过并记 run 失败原因）。
- 验收「桌面重启后隧道自动恢复」：systemd `Restart=always` 已有；但 `StrictHostKeyChecking=accept-new` 在 gw2 重装/换 IP 后会卡死，
  known_hosts 应随开通命令写入。

### 2.2 A2
- `CreateDesktops` 包月除了 `period/period_unit/auto_renew` 还**必须传 `auto_pay=true`**（SDK 字段有；bossip 命令行 `--auto-pay true`），
  否则只生成待支付订单，桌面不会创建。计划漏了。
- 「鬼桌面清理：对 PrePaid 改为标记 + 关联订阅置 past_due」语义不对：鬼桌面是「DB 有、ECD 说没有」，是我们的基础设施问题，
  用户的钱没停，不该把订阅打成 past_due 惩罚用户。应是：标记桌面 `reclaimed`、报警、从池重新 `assign` 一台。
- 验收「按 PrePaid 开一台…用 --keep 记录待到期」：每跑一次全层冒烟就是一个月桌面钱。改成：冒烟只验证请求参数与 DescribePrice 询价，
  真买的那台直接进池当第一台 prewarm。
- 「阿里云到期后保留约 15 天」在杭州账号上能观察到 Expired 桌面仍被列出，但具体保留天数与「Expired 期间能否 RenewDesktops 恢复」要用一台真机验证，
  不要写死 15。

### 2.3 A3
- 自动花钱的护栏不够：现在只有「单价阈值 + 每次最多 2 台」。补：**账号余额预检**（bossip 的硬门：`bssopenapi QueryAccountBalance` ≥ 单价×2，
  `ECD_FLEET_PURCHASE_API.md` §0）、**每日采购上限**、以及**以 ECD 侧计数为准**（按标签 DescribeDesktops）而不只看 DB——
  DB 与 ECD 不一致时（正是鬼桌面场景）每 10 分钟买 2 台，一天 288 台。
- 「绑定 EndUser 并 ModifyDesktop 归属」：没有这个 API，是 `ModifyEntitlement`；预热机创建时没有用户，`assign` 时要
  先 `ensure_end_user`（bossip/openbox 都要等约 60 秒同步）→ `ModifyEntitlement` → 重写 `openbox-user/openbox-eu-id` 标签
  （出票 API 校验的是标签）→ 装隧道 → verify。**「3 秒内拿到桌面」做不到**，验收改「不等 CreateDesktops，2 分钟内 assigned」，
  或注册时就预建 EndUser。
- `cloud_desktops` 扩列前提：`user_id` 改可空、`status`（creating/running…）与 `pool_state` 语义分开，别复用一列。
- bossip 的标签双命名空间坑（`FLEET_POOL_MANAGER.md`「🔴 标签的两个命名空间」）：只用 `ALIYUN::GWS::INSTANCE` 一套读写，
  openbox 现有 `ListTagResources` 已是这一套，写标签时别用 `ALIYUN::GWS::DESKTOP`。
- `recycle` 用 `RebuildDesktops`（SDK 有），bossip 实测 6.5 分钟含验收；要先把桌面从 `assigned` 上撤掉隧道行，否则重建期间旧端口还在。

### 2.4 A5
- CDP 的 `Network.clearBrowserCookies` 是**清全部** cookie，不按域；按域要 `Network.deleteCookies(name, domain)` 逐条或
  `Storage.clearDataForOrigin(origin, "cookies")`。
- 每天 06:00 在用户桌面「打开探活 URL」：会真的在用户桌面上弹页面，且抖音对自动化导航敏感，探活本身可能把登录态探没。
  改用 `Network.getCookies` 判 `sessionid` 类 cookie 存在 + 一个轻量 JSON 接口（如创作者中心的用户信息接口）取昵称，不整页导航。
- 「需要平台登录的技能/工具在执行前查状态」：openbox SKILL.md 只有 `allowed-tools`，没有 bossip 那种 `requires.config: [douyin_account]`
  声明。要给技能元数据加一个「需要平台」字段，阻断点才有依据；否则只能全局拦浏览器工具。
- 人扫码用的 Chrome profile 必须与 agent 的 dev-browser 是同一个 `--user-data-dir`，否则登录落在另一个 profile，探活永远 unbound。写进验收。
- 探活失败写「应用内消息 + 设置页红点」：openbox 没有通知模型；bossip 也没有（`User.notificationEmail` 只存不发）。这是新表新接口，
  计划按「已有」写了。

---

## 3. B1 Workspace

- **谁来解析 workspace 不能只靠请求头。** agent loop、cron 执行、WebSocket、视频恢复都不经过 `get_workspace(request)`；
  计费归属必须取**会话行**的 `workspace_id`。请求头只用于列表/创建类接口。
- 回填清单少了几张：`video_jobs`、`video_productions`、`user_memories`、`image_gen_cache`、`permission_rules`、`todos`、
  `prompt_history`、`skill_installs`。定一条规则：能经 `session_id` 推导的不加列，只有会话之外创建的行才加列，避免 20 张表都改。
- `audit_logs` 现有列是 `user_id, action, resource_type, resource_id, details, ip, ua`（`db/models/audit_log.py:13-21`），没有 `workspace_id`，
  `audit.record(..., workspace_id, ...)` 需要一次迁移；`PgAuditRepo` 已存在可直接用（`db/repository/audit_repo.py`）。
- 单测跑的是 ORM `create_all` 的 sqlite 内存库，**不走 alembic**（`tests/conftest.py:15-26`）。「单测：迁移回填」要照
  `test_remove_skill_runtime_migration.py` 的方式单独写；部分唯一索引要同时给 `postgresql_where` 和 `sqlite_where`（`cloud_desktop.py:29-35` 有先例）。
- 移动端不带 `X-Workspace-Id` 就只能看默认 workspace；里程碑三之前要在接口层保证「缺头 = 默认」而不是 400。
- 存量生产用户（含 ecd-demo）上线 M2 前要一次性给 developer 或代付，BILLING_PLAN §8 有、本计划没有。

---

## 4. B2 / B3 计费

### 4.1 落账点
- `X-Oneapi-Request-Id`：openbox 有两条 LLM 路径——Responses API 自建 httpx（`agent/llm.py:968-972`，响应头直接可读）和 litellm 流式
  （`:1775` 附近，只从 chunk / `_hidden_params["usage"]` 取用量）。litellm 路径需要 `litellm.return_response_headers = True` 并从
  `_hidden_params["additional_headers"]`（键会被加 `llm_provider-` 前缀）取；先在 1.81.11 上验证流式对象也带这个字段，再写计划。
- `litellm.completion_cost` 在 1.81.11 的参数是 `completion_response/model/prompt/messages/completion/...`，没有 `prompt_tokens`，
  两处调用（`llm.py:1350`、`:1775`）确实恒抛异常归 0——计划这条对。
- 压缩「不再把会话累计清零」：`agent/compaction.py:455-470` 现在连 `cost` 一起清零，且压缩自身的调用**没有 cost 字段**（`:446-452`）；
  在途修复 789b31e 只改了每步覆盖，压缩清零仍在。
- 影子模式「ledger.kind=usage 的 amount 记为 0」会往账本塞成千上万条 0 元行。bossip 的做法是 shadow 只写 usage_events 不写 ledger
  （`usage-metering.service.ts:44-47`）。同时明写：切 enforce 时**不追扣** shadow 期间的事件。
- 视频 `-1` 智能时长：bossip 按提交时 `durationSec` 计费、`-1` 折成 1 秒，是一个已知漏账（`media-gen.service.ts:403-451`）。
  openbox 也有 `-1`（c194657）。预扣按模型最大时长，终态按实际补正——计划 B4 第 7 条写了，但 B2 的「视频提交成功点落账」要注明用最大时长预估。
- 图片工具的跨用户缓存命中（`tool/image_gen.py:577-621`）是零成本成功，落账要区分 `reused`（bossip 记 quantity 0）。

### 4.2 余额与并发
- 「读改写包事务并对余额行加锁」bossip 没做（见 §0.2）。openbox 写法：`SELECT ... FROM workspace_balances WHERE workspace_id=? FOR UPDATE`
  在同一事务里插 ledger + 更新余额；sqlite 单测没有 FOR UPDATE，用 `with_for_update()` 让 SQLAlchemy 在 sqlite 上静默降级即可。
- new-api 侧 pre-consume（`service/pre_consume_quota.go`）会按 max_tokens 预扣 token 额度，余额低时合法请求会被网关先拒；
  影子上限要留余量，回合前预检也要给网关留出这笔预扣。

### 4.3 网关 token 与对账（已核实可行）
- `GET /api/log/self` 支持 `token_name / model_name / start_timestamp / end_timestamp / request_id / upstream_request_id` 与分页
  （`controller/log.go:36-57`）；`logs.other` 含 `cache_tokens`、`end_reason`、`request_path`（`service/log_info_generate.go:23-103`）；
  响应头常量 `X-Oneapi-Request-Id`（`common/constants.go:182`）。计划的对账口径成立。
- 「每用户网关 token」bossip 没有（`model-gateway-admin.service.ts` 只是运营台代理，`unified-usage-billing-v1.md:23-33` 明写不按网关日志出账），
  这块是新写，且按 §1.1 应为每 workspace 一个。
- `gateway_tokens.key_ciphertext` 的加密密钥从哪来（`BILLING_TOKEN_KEY`？轮换？）计划没写。
- B3「挂到现有 cron 基础设施」：openbox 没有内置周期任务注册点，现有的 reaper/warmup/视频恢复都是塞在 `cron/timer.py:127-147` 的
  `finally` 里。B3、A3 巡检、A5 探活三个人都要「每 N 分钟」，先在 B1 里给一个 `internal_tasks` 注册原语（带单实例锁），别三个人各改 timer.py。

### 4.4 B4
- 回合前预检允许 `past_due`，但包月桌面到期即停，past_due 用户的会话会卡在 `DESKTOP_NOT_READY`。要么 past_due 只放行不需要桌面的会话，
  要么明确 past_due 期间就是不能用，预检直接拒。
- `payments` 与「订单」混一张表：退款、重复回调、部分支付都需要订单态；bossip 的 `docs/billing.md:205` 就是因为没 Order 表在补。
  建议 `orders(status: pending|paid|failed|refunded)` + `payments` 记渠道回执。
- 体验套餐成本：100 积分 ≈ ¥200 用量 + 15 天桌面 ≈ ¥125，一个体验账号成本约 ¥325，「运营/邀请码发放」的额度要有上限配置。
- 499/259 与面值不符这条，配置能改，但 `plans.yaml` 里 `credits` 与 `price_cny/cny_per_credit` 两个来源要定谁优先，否则后台改一处对不上另一处。

---

## 5. C5 / E1 / E2

- C5 对照表的关卡列表写错了：bossip 的真实阶段机是 `gate.py:39` 的
  `init → script_ok → segments_ok → spend_ok → generated → stt_ok → composed → delivered`，五个必停点，
  **核心是第 6 步的花钱闸 + contentHash 变更即 STALE**；字幕只是 §9 的可选项，不是独立关。对照表按这个起草。
- bossip 技能里 `durationSec: -1` 与其计费漏洞绑定，对照时标「有意不同」。
- E2「移动端 locale 校验通过」：`mobile/scripts/` 只有 `check_file_size.sh`，**没有 locale 对齐检查**，验收无法执行；先补一个逐字节比对脚本。
- 三人并行表里 B1 与 A1 同周开工，两边都动 `cloud_desktops` 和迁移，alembic 单头（当前 `b2d4f6a8c0e2`）会分叉；
  约定 B1 的 workspace 迁移先合，A1 从其后起分支。

---

## 6. 建议回写进计划的决策清单

| # | 决策 | 建议 |
|---|---|---|
| 1 | 桌面归属 | workspace，一订阅一台；token 一 workspace 一个 |
| 2 | A1 拓扑 | 先半天验证标准办公网络 + VPC 直连；不行则 gw2 自任中继 + 每桌面密钥 + `AuthorizedKeysCommand` |
| 3 | 金镜像 | A1 第 0 步重做无秘密镜像（40–50G 盘），action server 空密钥改为拒绝启动 |
| 4 | 包月参数 | 加 `auto_pay=true`；assign 时对齐到期（RenewDesktops）或按量转包月 |
| 5 | 池护栏 | 余额预检 + 日上限 + 以 ECD 计数为准 |
| 6 | 影子记账 | 只写 usage_events；切 enforce 不追扣 |
| 7 | 周期任务 | B1 先给内置任务注册原语 |
| 8 | 鬼桌面 | 标记 + 重分配 + 报警，不动订阅状态 |
| 9 | 探活 | cookie 存在性 + 轻量接口，不整页导航；同一 Chrome profile 写进验收 |
| 10 | 移动端 | 先补 locale 比对脚本，E2 验收才成立 |
