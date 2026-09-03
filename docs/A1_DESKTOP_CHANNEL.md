# A1 · 每桌面执行通道 —— 独立执行单

> 2026-09-03。从 `DETAILED_PLAN_M1_M2.md` v2 的 A1 抽出，给 Codex 目标模式单独执行。
> 本文自包含：目标、现状、必要资料、方案、验收条件、测试方式、交付证据清单。
> 证据与理由见 `DETAILED_PLAN_M1_M2_REVIEW.md` §1.2–1.3、§2.1。
>
> **执行者须知**：只做本文范围内的事；遇到「需确认」标记的动作先停下来报告；
> 任何会产生费用（建桌面、建办公网络、CEN）或改生产配置的动作，先报告再执行。

---

## 1. 目标

**一句话**：让 agent 的命令跑在「用户自己那台」无影桌面上，而不是全员共用的 `ecd-4zjxaq5g45dr5qr0i`；
云桌面 tab 看到的画面 = agent 干活的那台机器。

**完成定义**（全部满足才算完成）：
1. `WUYING_MODE=per_user` 时，两个不同用户各自开通桌面后，各发 `bash: hostname`，返回各自桌面的主机名。
2. 新开桌面从**无秘密金镜像**克隆，每台桌面有自己的 action server 密钥和自己的通道凭据；镜像里 0 个秘密。
3. 桌面重启后通道自动恢复；撤销一台桌面后它的通道立即失效。
4. 现有共享桌面路径（`WUYING_MODE=shared`、EC2 生产栈）行为不变。
5. 单测全绿、冒烟脚本新增层通过、文档更新。

**非目标**（不要做）：Workspace 表与迁移（B1）、包月参数（A2）、预热池（A3）、计费（B 线）、平台账号（A5）。
`cloud_desktops` 的归属键本项**仍用 `user_id`**；B1 落地后把归属换成 workspace（见 §5.3「归属接缝」），本项只留接缝，不改表主键语义。

---

## 2. 现状（2026-09-03 实查）

### 2.1 代码
| 事实 | 位置 |
|---|---|
| provider 协议有 `routes_per_user: bool = False` | `backend/sandbox/provider.py:63` |
| `WuyingProvider` 硬编码 `routes_per_user = False`，构造时读一个 `wuying_endpoint`，所有人一个 `ContainerInfo`（`CONTAINER_ID="wuying-desktop"`），`forward_to_container` 用同一把 `X-API-Key` | `backend/sandbox/wuying.py:44, 46-76, 149-161` |
| SandboxManager 用 `provider.get_user_container(user_id)` 取容器，再用 `provider.client_base_url`（进程级单值）建 `SandboxClient` | `backend/sandbox/manager.py:234-262` |
| 其它按用户取容器的调用点 | `api/browser.py:59-66`、`api/dev_browser.py:121`、`api/ws.py:318-363`、`cron/warmup.py:74-84` |
| `SandboxClient(host, port, api_key, base_url, user_scope)`，头 `X-API-Key`、`X-OpenBox-User-Scope`，`trust_env=False`；桌面租约 `desktop_lease()` 走 `/desktop/lease/acquire` | `backend/sandbox/client.py:233-260, 305-369` |
| `api/desktop.py::_per_user()` 要求 `wuying_mode=="per_user"` **且** `provider.routes_per_user`，否则回落共享桌面并 `log.error` | `backend/api/desktop.py:56-90` |
| `WuyingDesktopService`：`status/provision/resolve_ticket_target/release_ghost/start_patrol`，按标签收养，巡检只记日志 | `backend/sandbox/wuying_desktop_service.py` |
| `cloud_desktops` 列：`id, user_id(非空), desktop_id, end_user_id, region_id, status(creating/running/starting/stopped/failed), error, is_deleted, ...`；一人一台部分唯一索引 `ix_cloud_desktops_user_active` | `backend/db/models/cloud_desktop.py`；迁移头 `b2d4f6a8c0e2` |
| ECD 封装：`create_desktop` 强制 image/office_site/policy_group；标签 `openbox-user/openbox-eu-id/openbox-env`；标签读 `ListTagResources`（DescribeDesktops 的 Tags 可能为空） | `backend/sandbox/wuying_ecd.py:213-266, 420-427` |
| action server 鉴权：`SESSION_API_KEY` 环境变量；**为空 = 不鉴权**；`/alive /docs /openapi.json /terminal` 免鉴权 | `container/action_server.py:119, 266-273` |
| bootstrap：五步（runtime / action server / dev-browser / systemd 单元 / 中继授权），单元文件把密钥写成 `Environment=SESSION_API_KEY=...`，隧道单元 `-R 127.0.0.1:<port>:127.0.0.1:8000 <relay>`，密钥 `/root/.ssh/openbox_tunnel`；RunCommand 载荷上限 16KB，文件走 gzip+base64 分块 | `backend/scripts/wuying_bootstrap.py:206-300` |
| 冒烟：`scripts/wuying_provision_smoke.py` 三层 `check / enduser / full`（full 计费，`--yes`，`--disk ≥ 镜像 Size`）；`scripts/verify_wuying_desktop.py` 用 SandboxClient 验一台桌面 | `backend/scripts/` |
| 单测：`tests/unit/test_wuying_provisioning.py`（ECD 调用打桩）；单测库是 sqlite 内存、ORM `create_all`，不走 alembic | `backend/tests/` |
| 配置：`WUYING_ENDPOINT / API_KEY / DESKTOP_ID / REGION_ID / END_USER_ID / MODE / IMAGE_ID / OFFICE_SITE_ID / DESKTOP_TYPE / SYSTEM_DISK_SIZE / POLICY_GROUP_ID / CHARGE_TYPE / PASSWORD_SALT / ENV_TAG` | `backend/core/config.py:346-370, 602-615` |
| 桌面工具（obx-display 1080p 守护等）由后端首次用 computer 工具时 `ensure_desktop_tools` 刷新到桌面 | `backend/sandbox/desktop.py` |

### 2.2 基础设施
| 事实 | 值 |
|---|---|
| 生产（上海）gw2 | ECS `i-uf66pcsepxpc23v5qsts`，内网 `10.100.1.83`，公网 `106.15.105.236`，VPC `vpc-uf6pfjyysdwybkq8m0hzk`，vSwitch `vsw-uf6urpiz14jtl0ii58hpj`，安全组 `sg-uf6gg28fkz5rsv9vknhf`（80/443 全网，22 只对 EC2 `54.254.36.226`）。栈在 `/opt/openbox`（compose，backend 容器经 `172.17.0.1` 访问宿主机），镜像 tag `20260902-b8b68e0`，`WUYING_MODE=per_user`、`WUYING_ENV_TAG=gw2`、`REGION cn-shanghai` |
| 现共享执行面 | 共享桌面 `ecd-4zjxaq5g45dr5qr0i`（cn-shanghai）→ 反向 SSH 到杭州中继 `47.110.66.89`:18001 → gw2 上 `openbox-wuying-tunnel` 单元 `-L 172.17.0.1:18001` → backend `WUYING_ENDPOINT=http://172.17.0.1:18001`。gw-1（EC2 新加坡）共用同一路径 |
| ECD 上海参数 | 金镜像 `m-feb7j85i4d4qo09ux`（openbox-image-v1-shanghai，Size 90G → 系统盘必须 ≥ 90）、办公网络 `cn-shanghai+dir-2879607125`（**便捷型**，无 VPC）、策略组 `pg-0bbay5jmvosn8b2hc`（1080p，跨地域可用）、规格 `eds.enterprise_office.4c8g` |
| ECD 杭州参数 | 金镜像 `m-5j3k26iwy99nrxtv0`、办公网络 `cn-hangzhou+dir-6394706301`；EC2 生产栈的 prod 桌面 `ecd-2oi2bfla5bl9erw2h`、`ecd-cm7ot1jctchhy58kp` 在杭州 |
| **金镜像烘焙了秘密（实测 ecd-2oi2bfla5bl9erw2h）** | `openbox-tunnel.service` enabled，`ExecStart ... -i /root/.ssh/openbox_tunnel -R 127.0.0.1:18000:127.0.0.1:8000 root@47.110.66.89`；`/root/.ssh` 有 `authorized_keys known_hosts openbox_tunnel openbox_tunnel.pub`；`openbox-action-server.service` 内联 `SESSION_API_KEY`；journal 每 5 秒 `Permission denied (publickey,password)` |
| 无影桌面网络 | 桌面无入站路由，只能出站；便捷型办公网络没有可对等的 VPC。阿里文档有「ECS 与无影通过 CEN 互通」（标准办公网络才有 VPC） |
| ECD CLI | 必须同时给 `--region` 和 `--biz-region-id`；`--api-version 2020-09-30`；`run-command --type RunShellScript --content-encoding Base64 --command-content <b64> --desktop-id <id> --timeout N`，结果 `describe-invocations --invoke-id ... --include-output true`（Output 为 base64）；本机 TUN 代理会挡 `ecd.cn-shanghai`，上海操作在 gw2 容器里跑 |
| bossip 参照 | 现行舰队用**每桌面反向 SSH**（`apps/codex/v1/scripts/wuying/setup-reverse-ssh-server.sh`、`ssh-forward-supervisor.mjs`），frps 已退役；金镜像 SOP「零 secret + grep 全盘 0 命中」（`apps/codex/v1/deploy/wuying/GOLDEN_IMAGE_BUILD.md`）。**不要参考** `apps/center/src/wuying/wuying-cloud.service.ts` 的 `_ensureFrpc`（hermes 时代死代码） |

---

## 3. 必要资料（开工前由用户提供或确认）

| # | 资料 | 用途 | 状态 |
|---|---|---|---|
| 1 | 本机 `aliyun` CLI 已配置，能调 `ecd`（cn-shanghai / cn-hangzhou）、`eds-user`、`ecs`（RunCommand 到 gw2） | 建桌面、RunCommand、验证 | 用户确认 |
| 2 | gw2 root 通道：`aliyun ecs RunCommand --RegionId cn-shanghai --InstanceId.1 i-uf66pcsepxpc23v5qsts`，或从 gw-1 用 `/root/.ssh/gw2ship` SSH | 改 sshd、装 AuthorizedKeysCommand 助手、看端口 | 用户确认 |
| 3 | gw2 `/opt/openbox/config/backend.env` 与 `openbox.json` 当前内容（脱敏后） | 对照现值改 `WUYING_*` | 用户提供 |
| 4 | 部署方式：gw-1 构建 `docker save` → scp → gw2 `docker load` → compose up；或直接在 gw2 构建 | 出镜像上线 | 用户确认走哪条 |
| 5 | 两个测试账号（或注册权限），用于双用户验收 | AC-1 | 用户提供 |
| 6 | 「需确认」项的审批人：新建办公网络 / CEN、建桌面、安全组改动、gw2 sshd 改动 | 花钱与生产变更 | 用户 |
| 7 | 阿里云账号余额足够开 2–3 台按量桌面（A1 验收用按量，不用包月） | AC-1/AC-3 | 用户确认 |
| 8 | 对 EC2 生产栈的处置：A1 上线后 EC2 仍指向共享桌面（本项用 `WUYING_ROUTING` 开关保证），是否同意 | 回归范围 | 用户确认 |

---

## 4. 方案

### 4.1 第 0 步：无秘密金镜像（先做，不等拓扑结论）

1. `wuying_bootstrap.py` 加 `--image-mode`：只做 runtime / action server / dev-browser 三步 + 写单元文件，**不生成密钥、不写密钥、不 enable 单元**：
   - `openbox-action-server.service`：`EnvironmentFile=/etc/openbox/action.env`，去掉 `Environment=SESSION_API_KEY=`；
   - `openbox-tunnel.service`：`EnvironmentFile=/etc/openbox/tunnel.env`，`ExecStart=/usr/bin/ssh ... -i /etc/openbox/tunnel_key -R ${TUNNEL_BIND}:${TUNNEL_PORT}:127.0.0.1:8000 -p ${RELAY_PORT} ${RELAY_USER}@${RELAY_HOST}`，`-o UserKnownHostsFile=/etc/openbox/known_hosts -o StrictHostKeyChecking=yes`；单元 **disabled**；
   - `/etc/openbox/` 存在但为空；`/root/.ssh` 不存在。
2. `container/action_server.py`：`SESSION_API_KEY` 为空时**启动即退出并打日志**（现在是「为空 = 放行所有请求」）。
3. 新增 `backend/scripts/wuying_image_verify.py`：对一台桌面 RunCommand 断言——`/root/.ssh` 不存在或为空；`systemctl is-enabled openbox-tunnel` = disabled；单元文件不含 `SESSION_API_KEY=`、不含 `-R 127.0.0.1:18000`、不含 `47.110.66.89`；`grep -rl -e 'OPENSSH PRIVATE' -e 'SESSION_API_KEY=' / --exclude-dir={proc,sys,dev,run}` 0 命中；`dpkg -l` 与现镜像桌面的差集为空（把现镜像桌面的 `dpkg -l | awk '{print $2}'` 存成基线文件比对）；`/usr/local/bin/obx-display` 存在且 target 1920x1080。
4. 在**上海**用裸 Ubuntu 22.04 + **50G** 系统盘建一台桌面（需确认），跑 `--image-mode` bootstrap → 跑 verify → `aliyun ecd create-image --desktop-id <id> --image-name openbox-image-v2-shanghai --api-version 2020-09-30 --region cn-shanghai --biz-region-id cn-shanghai` → 轮询 `describe-images` Status=Available → 删这台桌面。杭州暂不需要（EC2 栈不换）。
5. 产出：新镜像 id 写进本文 §2.2 与 `docs/WUYING_SANDBOX.md`；`WUYING_SYSTEM_DISK_SIZE` 从 90 改到 50。

### 4.2 拓扑决策（半天上限，二选一）

**方案甲（先验证）：标准办公网络 + VPC 直连。**
- 验证步骤：控制台或 CLI 查能否在 cn-shanghai 新建**标准型**办公网络并接入 `vpc-uf6pfjyysdwybkq8m0hzk`（或 CEN 与之互通）（**需确认**，可能产生 CEN 费用）→ 在该办公网络用新镜像建一台桌面 → 取 `DescribeDesktops.network_interface_ip` → gw2 上 `curl -m 3 http://<ip>:8000/alive`。
- 成立的判据：curl 200 且返回体带 `hostname`。成立则 §4.3 的「通道」= 记录 `private_ip`，不装隧道单元，`tunnel_state` 只表示「可达/不可达」。
- 不成立（不能接现有 VPC、CEN 不通、或半天到点）→ 方案乙。**验证过程与结论写进本文 §8**。

**方案乙：gw2 自任中继，每桌面一把密钥，`AuthorizedKeysCommand`。**
- gw2 新开第二个 sshd 实例（不动 22）：`/etc/ssh/sshd_tunnel_config`，端口 **2222**，只允许用户 `obxtunnel`（无 shell，`/usr/sbin/nologin`），`PasswordAuthentication no`、`PermitTTY no`、`X11Forwarding no`、`AllowTcpForwarding remote`、`PermitOpen none`、`GatewayPorts clientspecified`、`ClientAliveInterval 30`、`ClientAliveCountMax 3`、
  `AuthorizedKeysCommand /usr/local/bin/obx-authkeys %f`、`AuthorizedKeysCommandUser nobody`。systemd 单元 `openbox-tunnel-sshd.service`。安全组放行 2222/tcp 来源 0.0.0.0/0（**需确认**；桌面出口 IP 不固定；仅公钥、无 shell）。
- `/usr/local/bin/obx-authkeys`（宿主机脚本）：`curl -s -H "X-Internal-Token: $TOKEN" "http://127.0.0.1:8080/api/internal/tunnel-keys?fingerprint=$1"`，token 从 `/etc/openbox/authkeys.env`（600）读；后端按指纹查 `cloud_desktops`，`tunnel_state != revoked` 且行未删则返回一行
  `restrict,port-forwarding,permitlisten="172.17.0.1:<port>" <pubkey>`，否则空。`X-Internal-Token` 与 backend.env 的 `INTERNAL_API_TOKEN` 相同；该接口只绑 127.0.0.1 或校验来源。
- 桌面侧：开通时 RunCommand 在桌面生成 `/etc/openbox/tunnel_key`（ed25519，600），把公钥与指纹回传；写 `tunnel.env`（`RELAY_HOST=106.15.105.236 RELAY_PORT=2222 RELAY_USER=obxtunnel TUNNEL_BIND=172.17.0.1 TUNNEL_PORT=<port>`）、`known_hosts`（gw2 的 host key，后端配置项 `WUYING_RELAY_HOSTKEY`）、`action.env`（`SESSION_API_KEY=<该桌面的 key>`）；`systemctl enable --now openbox-action-server openbox-tunnel`。
- 绑定地址用 `172.17.0.1`（docker0 网关，宿主机内部），backend 容器按现有方式访问 `http://172.17.0.1:<port>`；不暴露到公网。
- 撤销：行 `tunnel_state=revoked` → 下次 sshd 认证即失败；已建立的连接由后端调 gw2 `ss -K`/`pkill -f "permitlisten=\"172.17.0.1:<port>\""`（可选，或等 ClientAlive 断）。
- 杭州中继 `47.110.66.89` 与 `openbox-wuying-tunnel` 单元只服务共享桌面旧路径，本项不动。

### 4.3 数据

`cloud_desktops` 新增（一个迁移，可 downgrade）：
`channel_kind: direct|ssh`、`private_ip nullable`、`tunnel_port int unique nullable`、`tunnel_bind nullable`、`tunnel_pubkey text nullable`、`tunnel_fingerprint String(64) unique nullable`、
`action_api_key_hash String(64)`、`action_api_key_ciphertext text`（用 `WUYING_CHANNEL_KEY` 32 字节对称加密，密文带 `v1:` 前缀）、`tunnel_state: pending|up|down|revoked`、`last_seen_at nullable`、`channel_error text nullable`。
端口分配：`WUYING_TUNNEL_PORT_RANGE=18100-18999`，事务内 `SELECT` 已用端口取最小空闲，靠 unique 约束兜底重试。

新增配置（`core/config.py` + `.env.example`）：
`WUYING_ROUTING=shared|per_desktop`（**默认 shared**，gw2 设 per_desktop；这是让 EC2 栈部署新镜像后行为不变的开关）、
`WUYING_CHANNEL=direct|ssh`、`WUYING_RELAY_HOST/PORT/USER/HOSTKEY`、`WUYING_TUNNEL_BIND=172.17.0.1`、`WUYING_TUNNEL_PORT_RANGE`、`WUYING_CHANNEL_KEY`、`INTERNAL_API_TOKEN`、`WUYING_HEALTH_INTERVAL_SEC=30`。

### 4.4 代码改动清单

| 文件 | 改动 |
|---|---|
| `sandbox/provider.py` | `routes_per_user` 语义改为「按归属路由」（名字保留） |
| `sandbox/wuying.py` | 双模式：`WUYING_ROUTING=shared` 完全保留现行为；`per_desktop` 时 `get_user_container(owner)` 查 `cloud_desktops` 返回该桌面的 `ContainerInfo(host, port, api_key)`，`client_base_url` 改为 `None`（让 manager 用 host/port），`forward_to_container(container_id=...)` 按容器 id 取各自 key；客户端按桌面缓存，密钥/端口变更即失效；`reconcile` 对所有 `assigned` 桌面探活 |
| `sandbox/manager.py:234-262`、`api/browser.py`、`api/dev_browser.py`、`api/ws.py`、`cron/warmup.py` | 经 `sandbox/ownership.py::owner_for(user_id)` 取归属键再调 provider（见 §5.3 接缝）；`base_url=getattr(provider,"client_base_url")` 在 per_desktop 下为 None 即走 host/port |
| `sandbox/channel.py`（新） | 分配端口、生成 key、加解密、RunCommand 写文件、回读指纹、`verify(desktop)`（`/alive` + `bash hostname` + `xrandr` 1920x1080）、`revoke` |
| `sandbox/wuying_desktop_service.py` | `provision` 在 Running 后调 `channel.install` → `verify` 通过才置 `running`；`_patrol_loop` 改为探活（内存态 `last_seen_at`，只在 up/down 变化时落库并记日志）；`release_ghost` 同时 `revoke` |
| `api/internal.py`（新） | `GET /api/internal/tunnel-keys?fingerprint=`（方案乙），`X-Internal-Token` 鉴权 |
| `api/desktop.py` | `_per_user()` 逻辑不变（provider 置 True 后自然放行）；`/status` 增加 `channel: {state, last_seen_at}` |
| `agent/loop.py` 会话启动处 | 归属桌面 `tunnel_state != up` → 抛 `DesktopNotReady` → API 层 503 `{"code":"DESKTOP_NOT_READY"}`；cron 会话遇到它跳过本次并写 `cron_runs` 原因，不重试 |
| `container/action_server.py` | 空密钥拒绝启动 |
| `scripts/wuying_bootstrap.py` | `--image-mode`；单元模板改 EnvironmentFile；保留旧模式给共享桌面 |
| `scripts/wuying_image_verify.py`（新） | §4.1 第 3 条 |
| `scripts/wuying_provision_smoke.py` | 新增 `channel` 层：建桌面 → 装通道 → verify → 删除；`--keep` 可留 |
| `frontend-v2` | 云桌面 tab 显示通道状态；会话收到 `DESKTOP_NOT_READY` 时提示并跳云桌面 tab（zh-CN/en-US 两份 locale，过 `npm run check`） |
| `docs/WUYING_SANDBOX.md` | per-user 章节改为「已接通」，写清两种通道、镜像 v2、`WUYING_ROUTING` |

### 4.5 归属接缝（给 B1 留的口）

`sandbox/ownership.py::owner_for(user_id) -> str`：本项返回 `user_id`；B1 落地后改为返回该用户默认 workspace id，并把 `cloud_desktops` 的归属列换成 `workspace_id`。
provider、channel、manager 全部只认 `owner` 字符串，不直接读 `users`。这样 B1 不需要重写 A1。

---

## 5. 验收条件（编号，逐条可核）

| 编号 | 条件 | 判据 |
|---|---|---|
| AC-1 | 双用户各自桌面 | gw2 上用户 U1、U2 各开通桌面后，各在聊天里执行 `bash: hostname`，返回值分别等于各自桌面的 `DescribeDesktops.host_name`，且 `≠ 0zd5sxxe1uw10r6`（共享桌面）；云桌面 tab 截图能对上（各自桌面上开一个终端窗口显示 hostname） |
| AC-2 | 镜像无秘密 | `wuying_image_verify.py` 对新镜像开出的桌面全部断言通过，输出附上；`dpkg` 差集为空 |
| AC-3 | 每桌面独立凭据 | 两台桌面的 `/etc/openbox/action.env` 中 key 不同、`tunnel_fingerprint` 不同；用 U1 桌面的 key 请求 U2 桌面 → 403 |
| AC-4 | 自恢复 | `aliyun ecd reboot-desktops` 重启 U1 桌面后 ≤ 3 分钟 `tunnel_state` 回到 `up`，`/api/desktop/status` 的 `last_seen_at` 更新，再发一条 `bash: hostname` 成功 |
| AC-5 | 撤销 | 后台把 U2 行置 `revoked` 后：方案乙 `obx-authkeys <指纹>` 返回空、桌面侧 journal 出现认证失败；方案甲 provider 拒绝路由；U2 会话得到 `DESKTOP_NOT_READY` |
| AC-6 | 未就绪提示 | 新注册用户未开通桌面直接发消息 → 503 `DESKTOP_NOT_READY`，前端跳云桌面 tab；cron 会话遇到时 `cron_runs` 有原因，不重试 |
| AC-7 | 旧路径不变 | `WUYING_ROUTING=shared`（或 EC2 栈）下：共享桌面聊天回合正常、`/api/desktop/status` 仍是 `mode: shared`、启动日志仍是「agent runs on … view streams …」；单测 `test_wuying_provisioning.py` 旧用例不改就过 |
| AC-8 | 单测 | `uv run pytest tests/unit` 全绿（既有 3 个依赖本机 openbox.json 的失败除外）；新增用例覆盖：端口分配与冲突重试、密钥加解密与轮换、`owner_for`、`tunnel-keys` 接口鉴权与 revoked、`DESKTOP_NOT_READY`、provider 双模式 |
| AC-9 | 冒烟 | `wuying_provision_smoke.py channel --yes` 在 gw2 容器内通过（建 → 通道 → verify → 删），输出附上 |
| AC-10 | 端口与暴露 | gw2 `ss -ltnp` 显示各桌面端口只绑在 `172.17.0.1`；从公网 `nc -z 106.15.105.236 <port>` 不通；2222 只接受 `obxtunnel` 公钥 |
| AC-11 | 文档与提交 | `docs/WUYING_SANDBOX.md` 更新；本文 §8 填写；提交信息中文、按工作项分 commit；不引入本文之外的改动 |

---

## 6. 测试方式

### 6.1 单元（本机）
```bash
cd backend && uv run pytest tests/unit -q
```
新增测试文件建议：`tests/unit/test_wuying_channel.py`、`tests/unit/test_internal_tunnel_keys.py`、`tests/unit/test_sandbox_ownership.py`。ECD/RunCommand 全部打桩，不联网。

### 6.2 前端
```bash
cd frontend-v2 && npm run check
```

### 6.3 镜像（上海，需确认后执行）
1. 建 50G 裸 Ubuntu 桌面 → `wuying_bootstrap.py --image-mode --desktop-id <id> --region cn-shanghai`
2. `wuying_image_verify.py --desktop-id <id> --baseline docs/image-baseline-dpkg.txt`
3. `create-image` → Available → 记 id → 删桌面

### 6.4 通道冒烟（gw2 容器内，避开本机 TUN 代理）
```bash
docker exec -it openbox-backend uv run python scripts/wuying_provision_smoke.py channel --yes --disk 50
```

### 6.5 端到端（gw2，真实账号）
1. 部署新镜像与 env（`WUYING_ROUTING=per_desktop`、`WUYING_IMAGE_ID=<v2>`、`WUYING_SYSTEM_DISK_SIZE=50`、通道相关项）。
2. U1、U2 注册 → 云桌面 tab 开通 → 等 `running` 且 `channel.state=up`。
3. 各发 `bash: hostname`；在桌面上开终端 `hostname` 截图。
4. AC-3：`curl -H "X-API-Key: <U1 key>" http://172.17.0.1:<U2 port>/health`（gw2 宿主机）→ 403。
5. AC-4：重启 U1 桌面，计时。
6. AC-5：改 U2 行 → 验证。
7. AC-7：把 `WUYING_ROUTING` 改回 `shared` 重启一次，跑一条共享桌面回合；改回。
8. 收尾：测试桌面按量，验收后删除（或 `--keep` 留给 A3 做池，需确认）。

---

## 7. 交付证据清单（做完收集给验收人）

1. `git log --oneline main..<branch>` 与 `git diff --stat main`。
2. `uv run pytest tests/unit -q` 完整输出；`npm run check` 输出。
3. `wuying_image_verify.py` 输出；新镜像 `describe-images` 结果（id、Size、Status）。
4. 拓扑验证记录：方案甲的每一步命令与结果（成立或不成立的证据），或方案乙的 `sshd -T -f /etc/ssh/sshd_tunnel_config | grep -i -e authorizedkeyscommand -e gatewayports -e allowtcp -e permitopen -e port`、`obx-authkeys <指纹>` 的输出（成功一次、revoked 一次）。
5. 两台桌面的 RunCommand 输出：`systemctl is-enabled openbox-tunnel openbox-action-server; systemctl is-active openbox-tunnel; ls -la /etc/openbox; ls /root/.ssh 2>&1; grep -c SESSION_API_KEY /etc/systemd/system/openbox-action-server.service; hostname`。
6. gw2：`ss -ltnp | grep -E '172\.17\.0\.1:18[1-9]'`；公网 `nc -zv 106.15.105.236 <port>` 结果。
7. U1、U2 的 `/api/desktop/status` JSON（各一份，含 `channel`）；两段聊天记录（含 `hostname` 输出）；两张云桌面 tab 截图。
8. AC-4 重启前后的 `status` JSON 与时间戳；AC-5 撤销后的会话错误体；AC-6 新用户的 503 响应体与 `cron_runs` 行。
9. AC-7 共享模式回归的启动日志两行与一条回合记录。
10. backend 启动日志中的 `Cloud desktop — agent runs on ...` 行（per_desktop 模式应改为按桌面打印或注明 per-desktop）。
11. 冒烟 `channel` 层完整输出。
12. 本文 §8 填好。

---

## 8. 执行记录（执行者填写）

- 拓扑决策：☐ 甲成立 ☒ 甲不成立→乙。2026-09-03 只读复核上海现有办公网络
  `cn-shanghai+dir-2879607125` 为 `SIMPLE`，没有可与 gw2 对等的 VPC；gw2 对现共享桌面
  `10.1.32.206:8000/alive` 的 3 秒直连探测超时。未为验证甲新建计费的办公网络或 CEN，
  选择方案乙。gw2 已启用独立 `openbox-tunnel-sshd.service`（2222），动态授权自测记录：
  指纹授权后仅监听 `172.17.0.1:18888` 且内部 HTTP 200；把测试行置为 `revoked` 后
  `obx-authkeys` 返回 0 字节，同一密钥重连为 `Permission denied (publickey)`。从杭州 ECS
  `i-bp1fbwlih14on7rclpqk` 探测公网 `106.15.105.236:18888` 3 秒超时。本机 `nc` 会被 TUN
  代理假阳性接通，故不作为公网暴露证据。
- 新镜像 id：待首次计费动作获批后创建。
- 分支 / 提交：`codex/a1-desktop-channel`；当前提交依次为 `e5a5f18`、`468841a`、
  `df805a6`、`b69f66c`（最终文档提交待补）。gw2 部署 tag
  `20260903-a1-e9ec7ff`，数据库 revision `c1d3e5f7a9b2`。
- 未完成或偏离本文的地方（必须写）：真实金镜像、双用户、重启和真实桌面撤销验收等待
  一次费用授权。`AuthorizedKeysCommandUser nobody` 必须读取 token，因此
  `/etc/openbox/authkeys.env` 使用 `root:nogroup 0640`，而不是不可同时满足的 `root 0600`；
  helper 与父目录仍不可由 `nobody` 写。gw2 无法稳定拉取 Docker Hub/GHCR，部署镜像用
  已运行且依赖锁相同的镜像作基础层覆盖源码，前端则上传本机已通过 `npm run check` 的
  `dist` 覆盖 nginx 层。
- 花费：截至本记录未创建桌面、办公网络或 CEN，新增计费资源为 0。实时
  `DescribePrice` 返回上海 `eds.enterprise_office.4c8g` + Linux 50G 高效云盘按量价
  `CNY 0.744086/台/小时`；等待授权后预计创建 1 台镜像源和 2 台验收桌面并在用完后删除。

---

## 9. 停下来报告的情形

- 方案甲需要新建办公网络或 CEN（有费用）。
- 需要改 gw2 安全组、sshd、或任何 `/opt/openbox` 生产配置。
- 需要建桌面（每台按量约 ¥0.3/小时起，别忘删）。
- 发现现有共享桌面路径必须改才能过 AC-7。
- B1 已并入 main 且改了 `cloud_desktops`（此时按 §4.5 接缝改归属列，不要自创第二套）。
- 任何本文没写、改动超过单文件的新情况。
