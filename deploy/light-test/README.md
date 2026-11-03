# light-test：Light 线独立测试机（2026-11-01 建）

Light 模式这条线（集成分支 `light-mode`，见 `docs/LIGHT_MODE_IMPL_PLAN.md` §10）**不合并 main、不发 gw2**，在这台机器上单独测试。配置从 gw2 克隆，密钥与数据全新。

## 机器

- ECS `openbox-light-test` `i-uf6gdzj4pv1hmdlexbf8`，cn-shanghai-b，`ecs.e-c1m2.xlarge` 4c8g 经济型，60G ESSD PL0，20Mbps 按流量，Ubuntu 24.04（`ubuntu_24_04_x64_20G_alibase_20261016.vhd`）。
- 包月 1 个月（订单 2007450784150294，¥123），**不自动续费**，到期 2026-12-01 16:00 UTC（北京 12-02 00:00）。到期前要么续，要么把测试结论搬走。
- 公网 `106.15.130.199`（源站，80 只放 lighthouse 106.52.167.53），内网 `10.100.1.92`（bossip-gw-vpc / vsw-uf6urpiz14jtl0ii58hpj）。
- 安全组 `openbox-light-test-sg` sg-uf67bgn4kwqmygsl98rg：80 只放 106.52.167.53/32，2222 全网（桌面反向隧道，仅公钥），**无 22**。运维只走云助手：
  `aliyun ecs RunCommand --RegionId cn-shanghai --InstanceId.1 i-uf6gdzj4pv1hmdlexbf8 --Type RunShellScript --ContentEncoding Base64 --CommandContent <b64> --read-timeout 90`，脚本以 `#!/bin/bash` 开头，同一台不要并发两条；结果 `DescribeInvocationResults --InvokeId`。
- bossip-newapi 安全组 sg-uf61yit0irjcov15390u 已放行 10.100.1.92 → 3000（LLM/图片/视频走内网 new-api，令牌与 gw2 共用）。
- 这台机 **拉不到 docker.io**（qdrant 超时），public.ecr.aws 也很慢：postgres / qdrant 基础镜像是从 gw2 `docker save` 后走内网 scp 搬的（临时在 gw2 安全组开 22 给 10.100.1.92、临时公钥，用完已撤）。以后要换基础镜像照此办。

## 部署形态（克隆自 gw2 2026-11-01 配置）

`/opt/openbox`：`docker-compose.yml` + `deploy/gw2/docker-compose.trajectory.yml` + `deploy/gw2/docker-compose.memory.yml` + `docker-compose.override.yml`（COMPOSE_FILE 与 gw2 相同），六容器 postgres / redis / qdrant-memory / backend / trajectory-worker / frontend。

- 应用镜像 `openbox-backend:20261101-light-050fa783`（backend 与 trajectory-worker）、`openbox-frontend-v2:20261101-light-050fa783`，源码 `feat/light-mode-backend@050fa783`（= PR #107 + #110，基于 main a32abfdc）。本机 `git archive` → `docker buildx --platform linux/amd64` 构建，经 `oss://bossip/_deploy-tmp/` 中转，机上 `sha256sum -c` 后装载，中转对象已删。
- 业务库 alembic head `pi95b6c7d8e9`（sessions.light_entry），trace 库 `t0007_turn_alerts`；trace 库与角色由 `deploy/gw2/scripts/create-trace-db.sh` 新建。
- override 只钉三行 image + aliyun-config / alipay / APNs p8 挂载 + `./config/billing-rates.json:/app/billing-rates.json`；**不再**像 gw2 那样 bind-mount `ph84a5b6c7d8` 迁移（已在镜像里）。
- `config/openbox.json` = gw2 10-31 版 + 档位 label 改名（L9 拍板）：`low` 灵活 → **省钱**、`high` 质量 → **高清**，`medium` 标准不变，ultra/fast 不动。

与 gw2 的差异（`config/backend.env` / `.env` / compose）：

- 全新 `JWT_SECRET`、`WUYING_PASSWORD_SALT`、`WUYING_CHANNEL_KEY`、`INTERNAL_API_TOKEN`（同步写 `/etc/openbox/authkeys.env`）、`QDRANT_API_KEY`（同步 `config/memory.env`）、两个库密码。库、trace 库、qdrant 全空，与 gw2 不共享任何用户数据。
- `WUYING_ENV_TAG=light-test`、`WUYING_RELAY_HOST=106.15.130.199`、`WUYING_RELAY_HOSTKEY=[106.15.130.199]:2222 ssh-ed25519 …`（known_hosts 行格式，本机 ed25519）。隧道 sshd（`openbox-tunnel-sshd`，2222，`obx-authkeys` → 后端 `/api/internal/tunnel-keys`）已起。
- 桌面池：`POOL_AUTO_PURCHASE=false`、`POOL_TARGET_PREWARM=0`、`POOL_ADOPT_ALLOWLIST=`、`POOL_AUTO_RENEW=false`。**超管给工作区开订阅仍会触发激活自动买桌面**，测免费档以外的入口前先想好。
- `APP_ENV=staging`（顶栏角标「内测环境」）；`PUBLIC_BASE_URL` / `PAYMENT_PUBLIC_BASE_URL` / `LOGTO_REDIRECT_URI` / `LOGTO_POST_LOGOUT_REDIRECT_URI`（含 docker-compose.yml 写死的两项）= `https://light-test.bossipai.com.cn`。
- `OPENBOX_MANAGED_CACHE=false`：镜像来自 git，没有 gw2 线上那套未入库的 managed cache 代码。
- 原样沿用 gw2 的：new-api 令牌、TokenSpace / RunningHub / DashScope / Tavily / 抖音 / 支付宝 / OSS 桶 `bossip` / APNs + JPush（App 推送可用）/ ECD 镜像、办公网络、策略组、`BILLING_MODE=enforce`。
- 没搬：杭州共享桌面隧道、pg 备份 timer、prune timer、轨迹指标 timer、云监控告警——**测试机无备份无告警**。

## 域名与证书

- `https://light-test.bossipai.com.cn` → 腾讯 lighthouse 106.52.167.53（`nginx-gateway`，TLS 终结）→ `http://106.15.130.199`（回源 Host 写 IP）。配置 `/opt/nginx/conf.d/light-test.conf`。
- DNS 在 DNSPod（用户 2026-11-01 加 A 记录 → 106.52.167.53）。证书 Let's Encrypt ECDSA，2026-11-01 签发，到 2027-01-30，续期 `bossip-light-test-cert-renew.timer`（照 gaode-test 复制）。当时的自举定时器 `light-test-dns-watch.timer` 已自我禁用，脚本留在 `/usr/local/sbin/light-test-dns-watch.sh`（再建一台可照抄）。
- HTTP 直连 IP 不可用（安全组只放 lighthouse；而且前端 SSO 的 PKCE 用 `crypto.subtle`，非安全上下文起不来），一律走 HTTPS 域名。
- Logto（bossip 共用 auth.bossipai.com.cn）bossip-web 应用 `m9383f921ea87ocylm40p` 回调白名单已加 `https://light-test.bossipai.com.cn/callback` 与退出跳转（直接改 RDS logto 库 `applications.oidc_client_metadata`，备份 bossip-gw-1 `/root/logto-backups/bossip-web-oidc-20261101T112530.json`）。Logto 账号池与 gw2 共用，业务数据不相通。抖音开放平台的回调域名未加，测试机上抖音授权预计不可用。

## 账号与首轮验证（2026-11-01，机上 127.0.0.1:80）

- 探针用户 `light-probe-11011930`（本地注册），密码在机上 `/opt/openbox/secrets/accounts.txt`（root 600）。
- 注册即验证：`extra.first_video.status=offered`、`extra.ui_mode=light`、`credit_ledger` 一行 `signup_gift` 4 积分；`GET /api/light/entries` 7 个入口（video/menu/poster `ok`，douyin_publish/review/shopkeeper `needs_desktop`，footage_edit `coming_soon`）；`GET /api/first-video` `status=none, eta_seconds=110`；`/templates` 4 行业 × 3 模板；`/api/light/recent` 200；`/api/environment` `staging`；首页 `app-build` = 镜像 tag；匿名访问 Light 接口 401。
- 公网 HTTPS（2026-11-01 20:35）：`/` 200、`/api/environment` staging、首页 `app-build` = 镜像 tag、`/api/auth/logto/config` 的回调 = `https://light-test.bossipai.com.cn/callback`、匿名 Light 接口 401、HTTP 301 到 HTTPS；浏览器打开首页角标「内测环境」、登录页出 SSO 按钮。未做：真实 SSO 登录（需用户账号）、真机 App 联调。

- **首条视频出片闭环（2026-11-02，公网 API 直驱）**：新注册用户 `light-e2e-1102a`（密码不入库，见会话记录），餐饮 × 门店氛围，店名「老王牛肉面」+ 主打「手工拉面 现熬牛骨汤」，无参考图。`POST /api/first-video` 07:54:15Z → H3 作业 07:54:16 建、07:55:49 完成（约 91 秒，含拷 OSS）→ IMS 花字 compose 07:55:49→07:56:10（21 秒）→ 07:56:18 轮询见 `ready`，端到端约 2 分钟，在「3 分钟」验收线内。成片 720×1280、15.0 秒、4.7 MB、带音轨，两行花字在画面下方；账本 `usage` −3.75（15 s × 0.25）+ −0.045（compose 按整分钟），余额 14 → 10.205；通知 `first_video_ready` 带会话链接；占位助理消息 `finish=stop`，挂 intermediate 原片（12.5 MB）与 final 成片两条 FilePart，固定文案 + 3 条建议。两点发现：新注册实际余额 14（`signup_gift` 4 + 首次读余额即发的每周 `allowance` 10），「余额不足补差」在当前免费档规则下不会触发；门店氛围模板会生成人物，H3 把人脸糊掉了，演示观感要看一眼。同日此前 `hjk`（其他 × 招牌推荐，无店名，75 秒）与 `jm`（餐饮 × 招牌推荐，带参考图，115 秒）各出过一条，均未走花字。

## 脚本（`deploy/light-test/scripts/`，2026-11-02）

- `ecsrun.sh <script> [timeout]`：云助手 RunCommand + 轮询，打印输出；客户端超时会按命令名找回已受理的调用，不会重发。脚本须以 `#!` 开头、base64 后 ≤ 16 KB。同一台机器不要并发两条。
- `ecssend.sh <file> <dir>`：云助手 SendFile（≤ 24 KB），给大于 RunCommand 上限的脚本用。
- `release.sh [ref]`：下面「发布」四步一条命令（构建、上传、签名链接、机上装载与换 override、健康检查、删 OSS 中转）；`--ship <tag>` 只重跑机上部分。签名链接只写进本机临时目录的 `remote.sh`，不进 git。一次约 4–5 分钟（构建有缓存时）。

## 发布（只从 `light-mode` 或其 PR 分支构建，不从 main）

1. 本机：`git archive <ref> backend container frontend-v2 | tar -x -C <dir>`；`docker buildx build --platform linux/amd64 --load -f backend/Dockerfile -t openbox-backend:<tag> .`（上下文是归档根，含 `container/`）；`docker buildx build --platform linux/amd64 --load --build-arg VITE_BUILD_ID=<tag> -t openbox-frontend-v2:<tag> frontend-v2`。tag 规范 `YYYYMMDD-light-<sha8>`。
2. `docker save | gzip -1` → `aliyun oss cp … oss://bossip/_deploy-tmp/<tag>/ --endpoint oss-cn-shanghai.aliyuncs.com --force`（本机经代理只有约 300KB/s，242MB 要 15 分钟；首次上传会卡死，kill 重传即可）→ `aliyun oss sign` 出链接。
3. 机上（云助手）：`curl` + `sha256sum -c` + `docker load` → 改 override 三行 image → `docker compose up -d`（backend 启动自跑 alembic）→ 看 `docker ps` healthy、`alembic current`、`/api/environment`。
4. 收尾删 `_deploy-tmp/<tag>/`。

发布会重启后端，正在对话的人会被打断。测试员（hjk、jm 等）在用时先 `release.sh --build-only` 构建暂存，等没人在对话、10 分钟内没有其他人发消息、没有在途视频作业时再 `release.sh --ship <tag>`。

## 发布记录

| 时间 | 镜像 tag | 源码 | 内容 |
|---|---|---|---|
| 2026-11-01 | `20261101-light-050fa783` | feat/light-mode-backend@050fa783 | 首版：#107 + #110，迁移 `pi95b6c7d8e9` |
| 2026-11-02 16:34 | `20261102-light-75538957` | light-full | menu-maker / poster-maker、样片签名（`light/media.py`） |
| 2026-11-02 16:48 | `20261102-light-8883666d` | light-full | `light-video` 技能（免费档做视频不再卡在云桌面）、Light 提示词带店名 |
| 2026-11-02 17:09 | `20261102-light-eb7e7262` | light-full | F5：合成完成后唤醒对话；compose 计入在途 |
| 2026-11-02 17:20 | `20261102-light-520b4184` | light-full | 单子类样式名、菜单/海报不以问题卡收尾 |
| 2026-11-02 17:34 | `20261102-light-6e10c68b` | light-full-autopilot | M2.5 省心托管（`/api/light/autopilot`、只出片无桌面执行）|
| 2026-11-02 17:45 | `20261102-light-20e41c94` | light-full-autopilot | 托管小修 + 文档；对应 Android 1.1.0 (102) |
| 2026-11-02 18:01 | `20261102-light-6c2d97f0` | light-full-footage | M3 你拍我剪（迁移 `pj06c7d8e9f0` file_assets.meta）；验收见 `docs/LIGHT_FOOTAGE_EDIT.md` §3 |
| 2026-11-02 18:15 | `20261102-light-57b6928a` | light-full-footage | 字幕 ≤12 字 + 文档；对应 Android 1.1.0 (103) |
| 2026-11-02 19:17 | `20261102-light-38db4a6f` | light-full-footage | 你拍我剪提速一：上传即读素材、合成进程内盯完成、视频意图包按「剪/素材」打开、减帧 |
| 2026-11-02 晚 | `20261102-light-b9a10d4a` | light-full-footage | 提速二：你拍我剪成片直接贴进对话（无唤醒轮）、视觉回复容错 + 一次重试 |
| 2026-11-03 15:14 | `20261103-light-edcac2ed` | light-full-footage | 提速三：视觉调用 20 秒没回再问一次、单次 45 秒放弃。**等测试员空闲后才上**（`release.sh --build-only` → `--ship`）；复验 5 段 → 方案卡 40 秒、系统耗时约 1 分 54 秒（`docs/LIGHT_FOOTAGE_EDIT.md` §3.1） |
| 2026-11-03 15:27 | `20261103-light-7c1a0ab4` | light-full-footage | 时间线嵌套对象（花字 `style` 等）以 JSON 文本传来也能读（复验里白花 33 秒的那处）；对应 Android 1.1.0 (104) |
| 2026-11-03 15:40 | `20261103-light-8c35a62a` | light-full-bgm | 你拍我剪配乐：IMS 公共曲库按 MediaId 引用、循环/截断、说话处压低、首尾淡入淡出；方案卡「配乐」+「不要配乐」 |
| 2026-11-03 15:52 | `20261103-light-2e372886` | light-full-bgm | 超宽字幕编译时拆行（IMS 不给中文自动换行）、曲子剩不到 4 秒不再循环、技能直接 submit（App 仍是 1.1.0 (104)，无需新包） |
| 2026-11-03 16:41 | `20261103-light-6c69a3e1` | fix/light-test-sandbox-journal-pin | 云桌面步骤修复首版：Driver 首次准备桌面时自动钉住**并 bind** v2 journal；`RuntimePreparationUncertain` 改为中文提示 + 错误码 `RUNTIME_PREPARATION_UNCERTAIN`。**bind 是错的**：门一旦有 control 行就拒绝所有不带围栏的请求（423），后端的通道校验（`/desktop/lease/acquire`）等仍不带围栏，三台桌面通道 16:43–16:54 掉线（`tunnel_state=down`），手工删掉三台 `control` 行后恢复 |
| 2026-11-03 17:04 | `20261103-light-131fadd6` | fix/light-test-sandbox-journal-pin | 修正版：只钉 journal **不 bind**，`validate_locked` 接受已钉住但未 bind 的租约；发后三条租约快照已改为 control=null，三台桌面围栏+journal 与不带围栏的 `/execute` 均 200、通道 up（App 仍是 1.1.0 (104)） |
| 2026-11-04 02:00 | `20261104-light-ed64b4f9` | light-mode（已吸收 main 3ed4e708 + #130） | light-mode 单向吸收 main（#113–#135：任务房间、call-sheet、语音线），Alembic 双头以 merge revision `pk17d8e9f0a1` 收单头（机上 `alembic current` = pk17d8e9f0a1 mergepoint）；发后 API 直驱复验：首条视频 `light-e2e-1104a` 83 秒出片（15.0 s、带音轨、花字在位，账本 −3.75/−0.045，通知 `first_video_ready`，占位消息挂原片+成片）；你拍我剪 `light-e2e-1104b` 同 5 段素材：上传完成→方案卡 17 秒、确认→成片贴进对话 35 秒（43.9 s、带原声、店名花字；账本 素材分析 2 + 成片 5，通知 `footage_edit_ready`）。**发现**：agent 轮结束（「开始合成了」）时 `task_finished` 就发 `task_completed`「任务完成」通知，比成片早 21 秒（消息中心自 9 月就有的行为，不是这次合并带来的，之前几轮没注意）→ 02:35 版已压掉（App 仍是 1.1.0 (104)） |
| 2026-11-04 02:35 | `20261104-light-f1a14cf5` | light-mode | PR #136：Light 会话有在途视频作业时不发「任务完成」（`notifications/events.py` `light_delivery_pending`）。复验 `light-e2e-1104c`：通知只剩「等待回答」×2 + `footage_edit_ready`，上传完成→方案卡 67 秒、确认→成片 32 秒，账本 2 + 5（App 仍是 1.1.0 (104)） |
| 2026-11-04 05:03 | `20261104-light-765a71c1` | light-mode | #138 第二个 tab 只留助理；#139 助理页问答遮罩式弹层 + 输入条上方渐隐；#140 首页输入条改提示行；#141 助理感知界面模式与首页目录、tasks.submit 四种 Light 工作类型直派云端技能、只读工具 light.entries；#142 参考样例库（**迁移 `pl28e9f0a1b2` 建 `light_examples`**，超管接口与「样例库」页面、轨迹页「收进样例库」）。切换后三容器 healthy、`alembic current` = pl28e9f0a1b2、后端无错误、`/api/admin/examples/taxonomy` 匿名 401；**当前线上**。配套 App 1.1.0 (105) 双端（见 docs/MOBILE_RELEASE_1_1_0_20261104.md） |

## 根因记录

- **2026-11-03 云桌面步骤 46/46 进 `manual_review`（做菜单/做海报「这一轮出错了 · Runtime preparation may have partially completed」）**：本机三台 per-user 桌面（hjk / jm / e，2026-11-02 随订阅自动购买）的 Action Server 是 `2026.10.29-managed-cache-v1`，开着 `resource_admission_v2` 门；带资源围栏头（`X-OpenBox-Resource-*`）但没有 `X-OpenBox-Resource-Journal` 的请求一律 `409 RESOURCE_JOURNAL_CHANGED`。后端只在 `resource_control_leases.remote_journal_id` 已钉住时才带这个头，而钉住此前只能靠 assistant API 手工 `bind`（`POST /api/assistant/resources/{id}/control`），这台机从未做过，三条租约全是 NULL。于是每个 `project_directory` / `catalogue_read` / `tool_execute` 效果都在越过发送边界后约 100 ms 被 409、`attempt_count=1` 无法撤回而进 `outcome_unknown` → 恢复器无 reconciler → `manual_review`。连通性、密钥、桌面上的 `/workspace`、`/opt/openbox/skills` 都没问题（同一请求补上 journal 头即 200）。
  修复：`backend/assistant/resource_commands.py::ensure_remote_journal`，Driver 首次为某桌面做运行时准备前自动读 `/resource-control/status`、把 journal 钉进租约并记录远端快照（`control` 为 null）；`resource_control.validate_locked` 接受"已钉住但未 bind"的租约。**不 bind**：门有了 control 行就会用 423 拒绝一切不带围栏的请求，而后端的通道校验、Driver 之外的技能/MCP 列表、终端、诊断都还不带围栏——首版自动 bind 让三台桌面通道掉线 11 分钟（见发布记录），bind 仍只留给显式命令。gw2 影响面：已钉住的租约只多一次 DB 读，无 journal 端点的旧桌面只多一次 404 探测。
  三台桌面的 `control_commands` 里留着首版写入的 `enrol:<resource>:1` bind 回执（不可删的历史）；将来若显式 bind 复用同一命令 id，门会回放旧回执而不重建 control 行——那时换一个命令 id。

iOS：**1.1.0 (104)** 2026-11-04 03:26 已上 TestFlight（只挂内部组「Light 测试」，运营测试组已重建为不自动分发），见 [docs/MOBILE_RELEASE_1_1_0_20261104.md](../../docs/MOBILE_RELEASE_1_1_0_20261104.md)。

账号（密码在机上 `/opt/openbox/secrets/accounts.txt` 或会话记录，不入库）：`light-full-samples`（样片，admin_grant 80 积分用于出片）；App 模拟器联调号 `light-full-1102a`（首条视频 / Light / 托管）、`light-full-1102b`（你拍我剪免费档验收）。

到期提醒：包月到 **2026-12-01 16:00 UTC（北京 12-02 00:00）**，不自动续费；续费或在此前把测试结论搬走。
