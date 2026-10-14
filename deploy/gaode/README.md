# 高德生产机（gaode.bossipai.com.cn）运维

ECS `i-uf6fm76cksm8z1cd7bqs`（cn-shanghai，`/opt/openbox`，无 trajectory overlay，`TRAJECTORY_WORKER_MODE=off`），云电脑 `ecd-d1pzbahxry54o9f9e`。两者均按月自动续费（ECS 到期 11-21，桌面 10-21 起每月续）。运维通道只有云助手 `aliyun ecs RunCommand`，同一时刻只能跑一条。

## 备份

- `systemd/openbox-pg-backup.timer`：每天 03:30（Asia/Shanghai）跑 `scripts/pg-backup-gaode.sh`，`pg_dump -Fc` 业务库 `openbox`（不含轨迹表数据）→ `pg_restore --list` 校验 → 经 backend 容器的 `python -m trajectory.ops.backup upload` 上传 `oss://bossip/backups/postgres-gaode/<YYYYMMDD>/openbox-<UTC>.dump`（presigned PUT，签名 HEAD 核 size/sha256），成功后删本地副本；失败的本地副本留在 `/var/backups/openbox/postgres`，3 天后清。
- 手动触发：`systemctl start openbox-pg-backup.service`；日志 `journalctl -u openbox-pg-backup.service`。
- 恢复演练：`docker compose exec -T backend python -m trajectory.ops.backup download --key backups/postgres-gaode/<day>/<file> --output /tmp/x.dump`，再 `pg_restore -l` / 恢复到临时库。gw2 的 `restore-check.sh` 依赖 worker 容器，这台不能直接用。
- 安装：`cp -r deploy/gaode /opt/openbox/deploy/`（随仓库同步），`install -m 0644 deploy/gaode/systemd/* /etc/systemd/system/`，`systemctl daemon-reload && systemctl enable --now openbox-pg-backup.timer openbox-prune-images.timer`。
- `openbox-prune-images.timer`：每周日 04:30 跑 gw2 的 `prune-images.sh --keep 3 --execute`（不依赖 overlay）。

## 告警（云监控，联系人组「云账号报警联系人」）

- 应用分组 `openbox-gaode`（GroupId 247562556，含该 ECS）。
- 主机可用性探测 `gaode-api-environment`（任务 2034951）：云监控插件在本机每分钟 GET `http://127.0.0.1:80/api/environment`，HTTP 状态 ≥ 400 连续 2 次或延迟 > 5 s 连续 3 次告警。公网站点监控（`CreateSiteMonitor`）在本账号返回 ExceedingQuota，需在控制台开通站点监控后再建；主机探测不覆盖 lighthouse 反代与证书，证书由 lighthouse 的续期 timer 负责。
- ECS 规则：`openbox-gaode-cpu`（CPU > 85% 持续 5 分钟）、`openbox-gaode-mem`（内存 > 90% 持续 5 分钟）、`openbox-gaode-disk`（磁盘 > 85% 持续 3 分钟）；内存、磁盘和可用性探测都依赖主机云监控插件（argusagent）。`InstallMonitoringAgent` API 在这台机报 YUNDUN.Execute，插件改用云助手跑官方 `agent_install-ecs.sh` 安装。
- **联系人组「云账号报警联系人」目前没有任何联系人，告警无处可发**：在云监控控制台「报警联系人」加手机/邮箱并加入该组。

## Key 与账户

- 正式 Key：`key_01M4W5JBWHTV7YE71K3WYZHMGM`（前缀 `obx_sk_N8N-smVn`，到 2027-10-14，60/min，并发 10），密钥文件 `/opt/openbox/secrets/gaode-api-key-prod-20261014.json`（root 600，字段 `secret`）。测试 Key `key_01M31QPV8Q36CFJHZXJ7V2GT08` 11-20 到期，高德切换后用 `scripts/issue_api_key.py --revoke` 撤销。
- 用户 gaode / 工作区 `01M31Q8VPZSFYV95BDFM608D33`：正式与测试共用一个工作区（桌面按工作区分配，拆分需再买一台桌面）；对账以 `metadata.task_id` 与切换日期区分。
