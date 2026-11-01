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
- 公网 HTTPS（2026-11-01 20:35）：`/` 200、`/api/environment` staging、首页 `app-build` = 镜像 tag、`/api/auth/logto/config` 的回调 = `https://light-test.bossipai.com.cn/callback`、匿名 Light 接口 401、HTTP 301 到 HTTPS；浏览器打开首页角标「内测环境」、登录页出 SSO 按钮。未做：真实 SSO 登录（需用户账号）、真机 App 联调、首条视频真实出片（会消耗 H3 积分）。

## 发布（只从 `light-mode` 或其 PR 分支构建，不从 main）

1. 本机：`git archive <ref> backend container frontend-v2 | tar -x -C <dir>`；`docker buildx build --platform linux/amd64 --load -f backend/Dockerfile -t openbox-backend:<tag> .`（上下文是归档根，含 `container/`）；`docker buildx build --platform linux/amd64 --load --build-arg VITE_BUILD_ID=<tag> -t openbox-frontend-v2:<tag> frontend-v2`。tag 规范 `YYYYMMDD-light-<sha8>`。
2. `docker save | gzip -1` → `aliyun oss cp … oss://bossip/_deploy-tmp/<tag>/ --endpoint oss-cn-shanghai.aliyuncs.com --force`（本机经代理只有约 300KB/s，242MB 要 15 分钟；首次上传会卡死，kill 重传即可）→ `aliyun oss sign` 出链接。
3. 机上（云助手）：`curl` + `sha256sum -c` + `docker load` → 改 override 三行 image → `docker compose up -d`（backend 启动自跑 alembic）→ 看 `docker ps` healthy、`alembic current`、`/api/environment`。
4. 收尾删 `_deploy-tmp/<tag>/`。
