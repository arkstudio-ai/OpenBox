# OpenBox Backend

FastAPI 控制面负责认证、会话、Agent 执行、能力管理和业务状态。沙箱文件与桌面动作通过执行环境完成，
不会因为 Python 服务能访问宿主机就默认在宿主机执行用户任务。

## 运行

需要 Python 3.12+、uv、PostgreSQL、Redis；配置按[本地命令](../docs/contributing/DEV_COMMANDS.md)准备。
在仓库根目录运行：

```bash
cp -n backend/.env.example backend/.env
cp -n backend/openbox.jsonc.example backend/openbox.json
make deps
cd backend
uv sync --extra test
cd ..
make backend
```

`make backend` 使用统一入口，先应用业务数据库迁移，再监听 8080。
环境文件存在不等于会自动加载；模型、无影与隧道选择见[无影指南](../docs/operations/WUYING_SANDBOX.md)。
轨迹 worker 的独立启动与迁移见[部署 Runbook](../deploy/gw2/RUNBOOK.md)。

## 代码职责

| 范围 | 模块 |
|---|---|
| Agent 执行与恢复 | `agent/`、`session/`、`models/`、`permission/` |
| 工具定义与适配 | `tool/`，按 11 个职责领域分组；MCP / 插件适配放 `tool/integrations/` |
| 技能、指令、记忆 | `skill/`、`.openbox/skills/`、`command/`、`memory/` |
| 可复用 Agent 与团队 | `agent_catalog/`、`team/` |
| 业务服务 | `video/`、`publish/`、`platforms/`、`autopilot/`、`trends/`、`billing/` |
| 交互与定时 | `question/`、`cron/`、`notifications/` |
| 数据与连接 | `api/`、`auth/`、`db/`、`cache/`、`blob/`、`sandbox/`、`mcp/`、`trajectory/` |

[能力架构](../docs/architecture/AGENT_CAPABILITIES.md)说明工具、技能、业务服务与执行环境的边界；
[工具目录](../docs/reference/TOOLS.md)和[新增能力指南](../docs/contributing/ADDING_CAPABILITIES.md)说明具体落点。

## 验证

在 `backend/` 下运行：

```bash
uv run python -m tool.catalog
uv run pytest tests/unit -q
uv run python scripts/check_main_contract.py --base HEAD
```

涉及数据库、沙箱或真实供应商时，使用独立测试环境运行对应集成测试。
接口说明见 [API 参考](../docs/reference/API_INTERFACES.md)；操作云桌面前遵守 [AGENTS.md](AGENTS.md) 的删除边界。
不要把外部服务密钥、私有环境文件或真实用户数据放入测试样例。
