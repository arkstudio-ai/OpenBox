# 部署文件

[部署指南](../docs/operations/DEPLOY.md)是环境和发布入口；
[gw2 Runbook](gw2/RUNBOOK.md)维护预检、激活、回滚、指标、备份和故障演练的具体步骤。

| 位置 | 用途 |
|---|---|
| `gw2/docker-compose.base.example.yml`、`docker-compose.override.example.yml` | 脱敏的服务器编排示例，用于对照与校验，不是实时配置备份 |
| `gw2/docker-compose.trajectory.yml` | 独立轨迹 worker 的 Compose overlay |
| `gw2/env.example`、`config/backend.env` | 示例变量和校验占位，不能替代目标环境密钥 |
| `gw2/scripts/` | 运维、备份、指标、生命周期与演练脚本 |
| `gw2/systemd/` | 指标、备份与镜像清理定时器 |
| `gw2/oss-lifecycle.xml` | 对象存储生命周期规则 |

仓库根目录 Compose 用于本地开发，`k8s/` 是遗留部署模板，两者不能直接覆盖服务器配置。
脚本的环境要求、执行范围和 dry-run 行为按 Runbook 核对；生产发布结果写入
[部署记录](../docs/reports/deployment/README.md)，不要把历史 tag 写成始终有效的当前版本。
