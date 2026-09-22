# OpenBox

[English](README.md) | 中文

OpenBox 是一个 AI Agent 平台，支持资料研究、文件与代码处理、桌面自动化、媒体制作和团队
协作。Python 后端管理模型调用、权限和可恢复的执行流程；Web 工作台展示对话、工具调用、
云桌面与交付成果。

[文档索引](docs/README.md) · [能力架构](docs/architecture/AGENT_CAPABILITIES.md) ·
[工具目录](docs/reference/TOOLS.md) · [技能目录](docs/reference/SKILLS.md) ·
[开发指南](docs/contributing/ADDING_CAPABILITIES.md)

## 能力

- **Agent 运行内核：**模型与供应商适配、上下文压缩、持久化会话历史、用户中断、等待回答和恢复。
- **51 个内置工具，11 个职责领域：**文件执行、联网、桌面、技能与记忆、规划、Agent 与团队、
  用户交互、定时任务、媒体、营销和工具发现。
- **技能与指令：**7 个随仓库交付的技能，按项目、个人库和沙箱范围加载；支持附属脚本与参考资料、
  项目规则及快捷命令模板。
- **可复用 Agent 与团队：**模型/工具/技能配置、定义版本、委派、任务依赖、成员通信、成果验收与
  最终答复。团队花费使用用户账号积分账本。
- **外部接入：**可信平台插件、受范围约束的 MCP 工具/资源及 OAuth。安装、授权和当前可用状态
  分别管理。
- **Web 工作台：**流式对话、工具与思考轨迹、提问、计划、待办、文件变更、终端、浏览器和云桌面。

运行内核、工具、技能、业务服务和执行环境分别承担不同职责。技能提供工作方法，加载技能不会
授予工具权限；工具库存也不等于每个 Agent 都可以调用的工具列表。

## 架构与执行边界

```text
Web / 移动端
        |
FastAPI：认证、工作空间、会话与 Agent 定义
        |
Agent 内核：模型、上下文、工具选择、调度与恢复
        |
按职责分类的工具 ---- 技能、指令与有范围的资源
        |
业务服务 / MCP 与插件 / SandboxClient
        |
无影桌面与浏览器 / 外部供应商 / 对象存储
```

| 模块 | 执行边界 |
|---|---|
| Agent 循环、权限和业务服务 | 后端控制面 |
| 文件读写、搜索、命令执行 | 配置的沙箱 / Action Server，生产通常使用无影 |
| 桌面与浏览器 | 根据操作和模式使用云桌面或用户已连接的浏览器 |
| 技能内容 | 从对应作用域的 Provider 加载；脚本和实际动作通过执行工具运行 |
| MCP | 通过配置的连接和适配器执行，不能统一认定为在后端宿主机运行 |
| 媒体与文件交付 | 供应商、资产和对象存储服务；部分操作还需要沙箱 |

无影支持共享开发配置和按用户分配桌面。会话使用项目工作目录，**并非统一为每个会话创建独占
桌面**。Docker 与 Kubernetes Provider 仍保留在代码中。桌面不可用时，相关操作应返回依赖错误，
普通对话可以继续。具体边界见[能力架构](docs/architecture/AGENT_CAPABILITIES.md)和
[无影指南](docs/operations/WUYING_SANDBOX.md)。

## 项目结构

```text
backend/
  agent/ · session/             Agent 内核、模型适配和历史
  tool/                         按职责分类的工具入口与公共协议
    workspace/ · web/ · desktop/ · knowledge/ · planning/
    collaboration/ · interaction/ · automation/ · media/ · marketing/ · discovery/
    integrations/               动态 MCP 与平台插件适配
    catalog.py · registry.py · tool.py · truncation.py
  skill/ · .openbox/skills/      技能服务与随仓库交付的技能包
  agent_catalog/ · team/        可复用定义与持久化团队协作
  command/ · memory/            命令模板与用户记忆
  sandbox/ · mcp/               执行环境与外部连接
  video/ · publish/ · trends/ · autopilot/ · platforms/
  cron/ · question/ · notifications/
  api/ · auth/ · permission/ · billing/ · db/ · trajectory/
frontend-v2/                    当前主推的 Web UI
frontend/                       旧版 Web，保留作迁移参考
mobile/                         移动客户端
container/ · extension/         Action Server、浏览器运行环境和浏览器扩展
k8s/                            遗留部署清单
docs/                           架构、参考、运维与历史证据索引
```

主要技术为 Python 3.12、FastAPI、Pydantic AI、LiteLLM、PostgreSQL、Redis；Web 使用 React、
TypeScript、Vite、Tailwind CSS、Zustand 和 TanStack Query。媒体传输使用配置的对象存储服务，
包括 OSS。各环境部署方式见[部署文档](docs/operations/DEPLOY.md)。

## 本地开发

在仓库根目录为尚未配置的环境创建配置文件：

```bash
cp -n backend/openbox.jsonc.example backend/openbox.json
cp -n backend/.env.example backend/.env
make deps
```

填写模型路由、数据库和执行环境配置。后端入口默认加载 `backend/.env`，不会仅因为另一个环境
文件存在就自动选择它。环境选择和隧道准备见[运行命令](docs/contributing/DEV_COMMANDS.md)与
[无影指南](docs/operations/WUYING_SANDBOX.md)。

在一个终端启动后端：

```bash
cd backend
uv sync --extra test
cd ..
make backend
```

在另一个终端启动 Web：

```bash
cd frontend-v2
npm ci
npm run dev -- --port 3000
```

后端端口为 `8080`，Web 端口为 `3000`，Web 将 `/api` 和 `/ws` 代理到后端。
`make backend` 启动前会应用数据库迁移。本地依赖见 `docker-compose.dev.yml`；各端口和地址
可以由实际环境配置覆盖。

## 验证

```bash
cd backend
uv run python -m tool.catalog
uv run pytest tests/unit -q
```

修改 Web 时：

```bash
cd frontend-v2
npm run check
```

浏览器 E2E 需要配置好的后端和测试账号，见 [Web 开发说明](frontend-v2/README.md)。连接真实
外部服务的集成测试需要独立测试配置；纯工具分类不需要调用付费模型或媒体服务。不要提交
凭据或环境文件。

## 文档

子项目入口：[后端](backend/README.md)、[Web](frontend-v2/README.md)、[移动端](mobile/README.md)、[执行环境](container/README.md)、[浏览器扩展](extension/README.md)、[部署](deploy/README.md)。开发与文档维护统一见[贡献指南](CONTRIBUTING.md)。

从[文档索引](docs/README.md)进入。索引区分当前参考、运维说明、设计提案、实施记录和历史证据。

- [Agent 能力边界](docs/architecture/AGENT_CAPABILITIES.md)
- [工具与依赖](docs/reference/TOOLS.md)
- [技能、指令与资源](docs/reference/SKILLS.md)
- [新增能力指南](docs/contributing/ADDING_CAPABILITIES.md)
- [Agent 运行内核](docs/architecture/AGENT_KERNEL_ARCHITECTURE.md)
- [团队接口](docs/reference/AGENT_TEAM_API_HANDOFF.md)与[团队运维](docs/operations/AGENT_TEAM_OPERATIONS.md)
- [本地命令](docs/contributing/DEV_COMMANDS.md)、[部署](docs/operations/DEPLOY.md)与[SSO](docs/operations/LOGTO_PROD.md)
