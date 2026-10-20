# gw3 独立业务机（2026-10-19 建，给其他公司独立部署，产品名仍为 bossip）

## 机器

- ECS `openbox-gw3` `i-uf60y6abkxu135z3p7sp`，cn-shanghai-b，`ecs.u1-c1m8.xlarge` 4c32g（与 gw2 同规格），100G ESSD PL1，20Mbps 按流量。
  包年包月 ¥336/月，自动续费（首期到期 2026-11-19，订单 2006293012670294）。
- 公网 `47.116.171.3`（源站，80 只放 Windows nginx 114.132.87.58），内网 `10.100.1.91`（bossip-gw-vpc / vsw-uf6urpiz14jtl0ii58hpj）。
- 安全组 `openbox-gw3-sg` sg-uf63rekaltpmc45705wr：80 只放 114.132.87.58（2026-10-20 收紧，IP 直连已关），2222 全网（桌面反向隧道，仅公钥），**无 22**——运维只走云助手
  `aliyun ecs RunCommand --RegionId cn-shanghai --InstanceId.1 i-uf60y6abkxu135z3p7sp`（脚本 `#!/bin/bash` 开头，同一台不要并发两条）。
- bossip-newapi 安全组 sg-uf61yit0irjcov15390u 已放行 10.100.1.91 → 3000。

## 部署形态（克隆自 gw2）

`/opt/openbox` 照 gw2 复制：`docker-compose.yml` + trajectory + memory overlay + override，六容器（postgres / redis / qdrant-memory / backend / trajectory-worker / frontend），
镜像与 gw2 2026-10-19 当时一致：backend `20261016-video-material-cae33477-r2`、trajectory-worker `20261014-companion-63dcb411`、frontend `20261015-assistant-page-dd0ca3b4`；业务库 alembic head `pg40c1d2e3f4`，trace 库由 `deploy/gw2/scripts/create-trace-db.sh` 新建。
镜像来源：gw2 `docker save` → 内网 scp（临时 22 规则与密钥已撤）→ `docker load`。以后发布沿用 gw2 流水（本机 buildx → OSS `_deploy-tmp` 签名链接 → 机上 curl + `sha256sum -c` + `docker load`，改 override 的 image 行）。

与 gw2 的差异（`config/backend.env` / compose）：

- 全新 `JWT_SECRET`、`WUYING_PASSWORD_SALT`、`WUYING_CHANNEL_KEY`、`INTERNAL_API_TOKEN`（同步写 `/etc/openbox/authkeys.env`）、`QDRANT_API_KEY`（同步 `config/memory.env`）、两个库密码（`.env`）。业务库、trace 库、qdrant 都是空的，与 gw2 不共享任何用户数据。
- `WUYING_ENV_TAG=gw3`（桌面按标签隔离，不会与 gw2 互删）、`WUYING_RELAY_HOST=47.116.171.3`、`WUYING_RELAY_HOSTKEY` 为本机 ed25519。
- 桌面池：`POOL_AUTO_PURCHASE=false`、`POOL_TARGET_PREWARM=0`、`POOL_ADOPT_ALLOWLIST` 为空。**注意：超管开订阅会触发激活自动买桌面**（与 POOL_AUTO_PURCHASE 无关）。
- `PUBLIC_BASE_URL` / `PAYMENT_PUBLIC_BASE_URL` / `LOGTO_REDIRECT_URI` / `LOGTO_POST_LOGOUT_REDIRECT_URI`（含 docker-compose.yml 里写死的两项）都指向 `http://47.116.171.3`。
- 删掉了 APNs / JPush 变量与 override 里的 p8 挂载（App 只连 gw2）。
- 原样沿用 gw2 的：new-api 令牌（LLM/图片/视频走内网 10.100.1.76:3000，用量与 gw2 混在同一令牌）、TokenSpace/RunningHub/DashScope/Tavily key、支付宝商户、抖音开放平台 key、OSS 桶 `bossip`、ECD 镜像/办公网络/策略组。
- 没搬：杭州共享桌面隧道 `openbox-wuying-tunnel`（per_desktop 路由用不到）、trajectory 自定义指标 timer（gw2 上已因云监控时间线配额报错）。

## 账号

- `obx-ops`（admin），密码在机上 `/opt/openbox/secrets/accounts.txt`（root 600）。2026-10-19 用它跑通注册→登录→建会话→一轮聊天（10.5s 回「收到」）。
- `bbdwxh`（admin）已绑定 Logto 用户 `bbdwxh_admin`（sub ga5qt2bdsjet）；SSO 首登误建的 `bbdwxh_admin-retired` 已停用。

## 备份

- `openbox-pg-backup.timer` 每天 03:30 跑 `deploy/gw2/scripts/pg-backup.sh`，**机上副本已把前缀改成 `backups/postgres-gw3/`**（restore-check.sh 同改），dump `openbox` + `openbox_trace` 到 `oss://bossip/backups/postgres-gw3/<YYYYMMDD>/`。2026-10-19 首跑成功。
  从仓库重新同步 `deploy/gw2` 到这台时要重新做这一处替换，否则会写进 gw2 的 `backups/postgres/`。
- `openbox-prune-images.timer` 每周日 04:30。

## 告警（云监控，联系人组 `openbox-gw3-alerts`：wxy 短信 + 账号默认联系人）

- 应用分组 `openbox-gw3`（GroupId 247616683）。
- ECS 规则：`openbox-gw3-cpu`（>85% 持续 5 分钟）、`openbox-gw3-mem`（>90% 持续 5 分钟）、`openbox-gw3-disk`（>85% 持续 3 分钟）。
- 主机可用性探测：`gw3-api-environment`（任务 2035203，本机 GET `http://127.0.0.1:80/api/environment`，状态 ≥400 连续 2 次或延迟 >5s 连续 3 次）、
  `gw3-tunnel-sshd-2222`（任务 2035204，TELNET 127.0.0.1:2222，防重启后隧道 sshd 起不来）。
- 云监控插件 CmsGoAgent 用 OOS 模板 `ACS-ECS-ConfigureCloudMonitorAgent` 装（exec-64591ed5be904a94bccb），开机自启已验证。
- 公网站点监控在本账号 ExceedingQuota，未建；探测不覆盖公网链路。

## 域名（2026-10-20 上线）

- `https://ai.kuayetong.cn` 门户、`https://app.kuayetong.cn` 工作台、`https://home.kuayetong.cn` 备用占位。DNS 都指向对方公司的 Windows 服务器 114.132.87.58，上面的 nginx 做 TLS 并反代到 `http://47.116.171.3`（回源 Host 写 IP）。
- 证书是 TrustAsia DV，2027-01-17 到期，**不会自动续期**，到期前要在对方 Windows nginx 上换证书（`conf\cert\`）。配置模板：kuayetong.conf + kuayetong_proxy.conf（ai 上 /app|login|register|callback|invite 302 到 app；app 上 / → /app）。
- gw3 的 `PUBLIC_BASE_URL`、`PAYMENT_PUBLIC_BASE_URL`、`LOGTO_REDIRECT_URI`、`LOGTO_POST_LOGOUT_REDIRECT_URI`（含 docker-compose.yml 两项）= `https://app.kuayetong.cn`。
- Logto（bossip 共用，auth.bossipai.com.cn）bossip-web 应用回调白名单已加 `https://app.kuayetong.cn/callback`、`http://47.116.171.3/callback` 及对应退出跳转（直接改 RDS logto 库 applications.oidc_client_metadata，备份在 bossip-gw-1 /root/logto-backups/）。gw3 用户与 gw2 共用 Logto 账号池，但业务数据互不相通。
- **HTTP 下前端不可用的坑**：SSO 的 PKCE、已有会话发送的去重指纹（`pending-send.ts`）用 `crypto.subtle`，多处用 `crypto.randomUUID`，非安全上下文没有 → SSO 退回本地表单、已有会话追问静默发不出去。只能走 HTTPS 域名。
