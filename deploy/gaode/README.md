# 高德生产机（gaode.bossipai.com.cn）运维

ECS `i-uf6fm76cksm8z1cd7bqs`（cn-shanghai，`/opt/openbox`，无 trajectory overlay，`TRAJECTORY_WORKER_MODE=off`），云电脑 `ecd-d1pzbahxry54o9f9e`。两者均按月自动续费（ECS 到期 11-21，桌面 10-21 起每月续）。运维通道只有云助手 `aliyun ecs RunCommand`，同一时刻只能跑一条。

## 备份

- `systemd/openbox-pg-backup.timer`：每天 03:30（Asia/Shanghai）跑 `scripts/pg-backup-gaode.sh`，`pg_dump -Fc` 业务库 `openbox`（不含轨迹表数据）→ `pg_restore --list` 校验 → 经 backend 容器的 `python -m trajectory.ops.backup upload` 上传 `oss://bossip/backups/postgres-gaode/<YYYYMMDD>/openbox-<UTC>.dump`（presigned PUT，签名 HEAD 核 size/sha256），成功后删本地副本；失败的本地副本留在 `/var/backups/openbox/postgres`，3 天后清。
- 手动触发：`systemctl start openbox-pg-backup.service`；日志 `journalctl -u openbox-pg-backup.service`。
- 恢复演练：`docker compose exec -T backend python -m trajectory.ops.backup download --key backups/postgres-gaode/<day>/<file> --output /tmp/x.dump`，再 `pg_restore -l` / 恢复到临时库。gw2 的 `restore-check.sh` 依赖 worker 容器，这台不能直接用。
- 安装：`cp -r deploy/gaode /opt/openbox/deploy/`（随仓库同步），`install -m 0644 deploy/gaode/systemd/* /etc/systemd/system/`，`systemctl daemon-reload && systemctl enable --now openbox-pg-backup.timer openbox-prune-images.timer`。
- `openbox-prune-images.timer`：每周日 04:30 跑 gw2 的 `prune-images.sh --keep 3 --execute`（不依赖 overlay）。

## 告警（云监控，联系人组「openbox-gaode-alerts」，与 gw2 分开）

- 应用分组 `openbox-gaode`（GroupId 247562556，含该 ECS）。
- 主机可用性探测 `gaode-api-environment`（任务 2034951）：云监控插件在本机每分钟 GET `http://127.0.0.1:80/api/environment`，HTTP 状态 ≥ 400 连续 2 次或延迟 > 5 s 连续 3 次告警。公网站点监控（`CreateSiteMonitor`）在本账号返回 ExceedingQuota，需在控制台开通站点监控后再建；主机探测不覆盖 lighthouse 反代与证书，证书由 lighthouse 的续期 timer 负责。
- ECS 规则：`openbox-gaode-cpu`（CPU > 85% 持续 5 分钟）、`openbox-gaode-mem`（内存 > 90% 持续 5 分钟）、`openbox-gaode-disk`（磁盘 > 85% 持续 3 分钟）；内存、磁盘和可用性探测都依赖主机云监控插件（argusagent）。`InstallMonitoringAgent` API 在这台机报 YUNDUN.Execute，Argus 脚本 URL 已 404；插件最终用 OOS 公共模板装上：`aliyun oos StartExecution --RegionId cn-shanghai --TemplateName ACS-ECS-ConfigureCloudMonitorAgent --Parameters '{"action":"install","targets":{"Type":"ResourceIds","ResourceIds":["i-uf6fm76cksm8z1cd7bqs"],"RegionId":"cn-shanghai"}}'`（2026-10-14 执行 exec-4163ee9783914a0195d2，状态 running）。
- 五条告警（三条 ECS + 可用性探测的状态码/延迟）和应用分组都指向联系人组 `openbox-gaode-alerts`（2026-10-14 建，当前成员是账号默认联系人「浙江真的科技有限公司」，短信 190****0595）。要换人或加人：云监控控制台「报警联系人」加联系人后加入该组；gw2 以后的告警用单独的组，不要共用。

## Key 与账户

- 正式 Key：`key_01M4W5JBWHTV7YE71K3WYZHMGM`（前缀 `obx_sk_N8N-smVn`，到 2027-10-14，60/min，并发 10），密钥文件 `/opt/openbox/secrets/gaode-api-key-prod-20261014.json`（root 600，字段 `secret`）。测试 Key `key_01M31QPV8Q36CFJHZXJ7V2GT08` 11-20 到期，高德切换后用 `scripts/issue_api_key.py --revoke` 撤销。
- 用户 gaode / 工作区 `01M31Q8VPZSFYV95BDFM608D33`：正式与测试共用一个工作区（桌面按工作区分配，拆分需再买一台桌面）；对账以 `metadata.task_id` 与切换日期区分。

## 测试环境 gaode-test.bossipai.com.cn（2026-10-14 建）

- ECS `openbox-gaode-test` `i-uf6bbmnu8of60i0su4co`，cn-shanghai-b，`ecs.e-c1m2.large` 2c4g，40G ESSD PL0，5Mbps 按流量，包月 ¥64 自动续费；公网 `106.14.89.214`，内网 `10.100.1.90`（同 VPC/交换机）；安全组 `openbox-gaode-test-sg` sg-uf6gai8rnxeoqdbx48up（80 只放 lighthouse 106.52.167.53，2222 全网，无 22，只用云助手）。bossip-newapi 安全组 sg-uf61yit0irjcov15390u 已放行 10.100.1.90 → 3000。
- 部署：`/opt/openbox` 从高德机克隆（compose / override / openbox.json / backend.env / gaode-console.html / aliyun-config），镜像 `20261013-trajlink-1170e8e4`（= 当时 main 头，领先高德线上一版），业务库全新迁到 head。backend.env 改动：全新 JWT/盐/通道密钥/INTERNAL_API_TOKEN/库密码，`WUYING_ENV_TAG=gaode-test`、`WUYING_RELAY_HOST=106.14.89.214`、`WUYING_RELAY_HOSTKEY` 为本机 ed25519、`WUYING_DESKTOP_TYPE=eds.enterprise_office.4c8g`、`APP_ENV=staging`、`MAX_CONCURRENT_AGENTS=4`、`POOL_ADOPT_ALLOWLIST` 空、域名全部换 gaode-test。桌面反向隧道 sshd（2222、obxtunnel、`/usr/local/bin/obx-authkeys` 读 `/etc/openbox/authkeys.env`）已起。**坑**：`useradd` 建的 obxtunnel 密码字段是 `!`（锁定），sshd 会以 "account is locked" 拒绝公钥登录，必须 `usermod -p '*' obxtunnel`；修好后桌面 10 秒内重连，激活 ready，隧道端口 18100。
- 账号（`/opt/openbox/secrets/accounts.txt`，root 600）：`obx-ops`（admin）、`gaode`（工作区 `01M4WGPK388FG3DRT08Z602ZFT`）。gaode 工作区由超管接口开通 max 月付订阅（含 1,680 套餐积分）+ 300 积分；订阅激活自动购买了 4c8g 桌面 `ecd-epnsbj1fkffqa83st`（约 ¥109.5/月，`POOL_AUTO_RENEW=true` 由应用续费）。
- 测试 Key：`key_01M4WGPR29E9WR95C9P3B4DNJD`（前缀见 `secrets/gaode-test-api-key.json`，180 天到 2027-04-12，60/min，并发 4），`/v1/me` 经本机 nginx 验证 200。；2026-10-14 经公网 `https://gaode-test.bossipai.com.cn/v1` 跑通 preflight 4/4 + 文本一轮（回答「收到。」，扣 0.215 积分）。给高德的测试接入信息 = 这个 base URL + 该 Key。
- 域名：DNS A 记录 `gaode-test.bossipai.com.cn → 106.52.167.53`（lighthouse）已加；lighthouse `/opt/nginx/conf.d/gaode-test.conf` 含 80（ACME + 301）和 443（照 gaode.conf，上游 `http://106.14.89.214`，证书 `/etc/nginx/ssl/gaode-test/`）。Let's Encrypt 证书 2026-10-14 签发，到期 2027-01-12；续期脚本 `/usr/local/sbin/renew-bossip-gaode-test-cert.sh` + `bossip-gaode-test-cert-renew.timer`（每日 05:57，`enable` 后还要 `start`，否则 list-timers 显示 n/a）。**坑**：本机 DNS 被代理劫持成 198.18.x 假 IP，解析/传播检查必须在 lighthouse 上用 `dig @8.8.8.8` 做；DNS 刚加 1 分钟内 certbot 会在 secondary validation 报 NXDOMAIN，等公共解析一致后再签。
- 未配：备份 timer、云监控告警（测试环境不配）；发布顺序应为 测试机 → 高德线上，镜像同 tag。
- 测试机不做备份、不接云监控告警（库可随时重迁，坏了按上面步骤重克隆）；镜像升级与高德机同一套 `/opt/openbox` 流程，先在这台验证再升生产。
