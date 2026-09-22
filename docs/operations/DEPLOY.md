# 部署与发布

本文说明部署入口、配置归属和发布约束。线上镜像、数据库版本和套餐价格必须从目标环境核对；
文档整理不会改变部署。2026 年 9 月的具体发布批次与旧操作说明已移至[部署历史](../reports/deployment/DEPLOYMENT_HISTORY_202609.md)。

## 环境与拓扑

| 环境 | 仓库记录的用途 | 配置要点 |
|---|---|---|
| AWS | 开发验证 | 历史入口 `ai.ueejavelin.org`；使用无影时按开发环境配置；跨云 OSS 不能直接使用阿里云内网端点 |
| 阿里云 gw2 | 生产 | 历史入口 `ai.bossipai.com.cn`；发布、备份、回滚与观测见 [gw2 Runbook](../../deploy/gw2/RUNBOOK.md) |
| 本地 | 开发与回归 | 见[本地命令](../contributing/DEV_COMMANDS.md)，不能用根目录开发 Compose 覆盖服务器编排 |

```text
入口代理 → frontend (nginx)
             ├─ 业务 API / WS → backend
             └─ 轨迹管理 API / WS → trajectory-worker
backend → trajectory-spool → trajectory-worker → trace 数据库 / OSS
backend → 业务数据库 / Redis / 配置的执行环境和供应商
```

轨迹独立 worker 已有 2026-09-16 的发布记录；实际是否启用以目标环境配置为准。
worker 使用后端镜像，但有独立启动命令、数据库角色与迁移入口。细节见
[轨迹规范](../trajectory-rearch/SPEC.md)和 [Compose overlay](../../deploy/gw2/docker-compose.trajectory.yml)。

## 配置与源码归属

| 项目 | 仓库入口 | 服务器职责 |
|---|---|---|
| 后端环境变量 | [`.env.example`](../../backend/.env.example) | `config/backend.env`，包括 DB、Redis、无影、对象存储、SSO 等配置 |
| 模型与供应商 | [`openbox.jsonc.example`](../../backend/openbox.jsonc.example) | `config/openbox.json` |
| Compose 基础与覆盖项 | [部署文件说明](../../deploy/README.md) | 比较服务器实际文件后合并，镜像钉版可能覆盖 `.env` 的 tag |
| 轨迹部署 | [gw2 Runbook](../../deploy/gw2/RUNBOOK.md) | 独立 worker、spool、trace DB、备份与指标定时器 |
| 密钥 | 外部凭据管理 | `secrets/` 等受控位置；不写入 README、示例或发布报告 |
| 计费 | [积分与支付](CREDIT_BILLING.md) | 读取当前套餐目录和 `BILLING_MODE`；`APP_ENV=prod` 不会自动改变价格 |

Compose 的 `environment` 会覆盖 `env_file` 的同名变量。发布前核对最终解析值，但不要将包含密钥的
完整 `docker compose config` 输出粘贴进公共文档。SSO 回调见 [Logto 配置](LOGTO_PROD.md)。

## 发布流程与可用性要求

1. 确定目标环境与源码提交，为镜像使用可追溯 tag；检查配置差异、两套数据库迁移和回滚兼容性。
2. 按 [Runbook 的 preflight 与 release procedure](../../deploy/gw2/RUNBOOK.md)保存配置、镜像信息、业务库与 trace 库备份，并校验备份可读。
3. 切换执行服务前检查活动运行租约和未完成的视频等异步任务。等待自然结束，避免中断用户工作。
4. 只替换实际变化的服务。使用 `--no-deps` 逐个切换并等待 healthy；涉及轨迹时按 Runbook 处理 worker 与迁移顺序，不执行无范围的整体重建。
5. 验证首页、静态资源、认证 API、业务 API、轨迹读写和日志；记录实际影响与遗留问题。单实例替换可能有短暂中断，不应宣称零停机。
6. 失败时按事先验证的兼容性回滚；数据库已迁移时，不能只恢复旧镜像而忽略 schema 变化。

后端镜像从仓库根目录构建，Dockerfile 在 `backend/Dockerfile`；Web 镜像以 `frontend-v2/` 为构建上下文。
目标服务器为 amd64 时，在 ARM 开发机上需指定 `--platform linux/amd64`。
具体传输、激活、健康检查和回滚命令统一维护在 Runbook，避免多份步骤互相漂移。

## 运行与排障

- [无影沙箱](WUYING_SANDBOX.md)：共享开发与按用户桌面配置、连接通道及诊断。
- [团队运行与恢复](AGENT_TEAM_OPERATIONS.md)：团队、任务、成员、积分与异常恢复。
- [长输入与轨迹恢复](TRACE-LARGE-INPUT-REPLAY.md)、[视频转存恢复](VIDEO_TRANSFER_RECOVERY.md)。
- [移动通知接口](../reference/MOBILE_NOTIFICATIONS.md)、[Android 厂商推送](ANDROID_PUSH_VENDORS.md)。
- [历史发布记录](../reports/deployment/README.md)：说明某次发布验证了什么，不能替代当前环境检查。
