# OpenBox

[English](README.md) | 中文

**一个 AI Agent 执行平台** —— 给大模型一个安全隔离的沙箱,让它读写文件、运行代码、操作浏览器,并配套生产级的编排、上下文管理与多租户隔离。

> 一个通用 Agent 运行时（灵感来自 OpenCode / Claude Code），用 Python 实现了**自研持久 Agent 内核**，并接入 LiteLLM 与 OpenAI Responses 适配器。FastAPI 控制面负责 Agent 状态与策略；文件、命令、桌面和沙箱 MCP 执行统一进入阿里云 **无影云电脑**。Docker Compose 只用于 PostgreSQL、Redis 和可选 Azurite 等本地基础设施。

> **前端方向：**[`frontend-v2/`](frontend-v2/) 是 OpenBox 当前主推且持续开发的 Web UI；原 [`frontend/`](frontend/) 仅作为旧版迁移参考保留。

---

## 速览

| | |
|---|---|
| **是什么** | 一个全栈平台：AI Agent 通过无影执行改代码、跑命令和操作桌面，v2 Web UI 与移动端展示每个持久 Turn 和工具调用。 |
| **核心技术** | FastAPI · 自研 Agent 内核 · LiteLLM/Responses · WUYING Action Server · PostgreSQL · Redis · React 19 · Flutter |
| **Agent 循环** | 持久 single-flight Driver、代际 fencing 与恢复、portable 工具 Schema、并发 body/有序提交、权限与上下文压缩 |
| **隔离** | 后端决定租户/项目策略；无影工作区、Skill/MCP 目录、资产与快照使用稳定用户/项目命名空间 |
| **规模** | 多 Worker SaaS 控制面；当前共享无影仅用于可信单用户验收，生产隔离边界是一用户一无影 |

---

## OpenBox 有何不同

大多数「让大模型跑代码」的 demo 一上生产就崩。OpenBox 把 Agent 当成一个**系统**来做,而不只是一段 prompt:

| 关注点 | 普通 Agent demo | OpenBox |
|---|---|---|
| **安全** | 大模型直接在宿主机执行命令 | 文件/命令/桌面动作跨越带认证、租户 scope 和 generation fence 的无影边界 |
| **上下文溢出** | 对话一直增长直到撑爆窗口 | 完整内部历史 + 压缩、portable 工具 Schema 预算、按需能力发现和输出裁剪 |
| **可靠性** | 一次失败的工具调用就崩 | 持久 Run 所有权、冷恢复 tail repair、工具有序提交与保守 unknown-outcome 语义 |
| **多用户** | 单一共享进程 | PostgreSQL/Redis 控制面、用户/项目命名空间、Skill/MCP scope 与 JWT/Logto |
| **可审计** | 不透明的对话历史 | SSE/WebSocket、工具 trace，以及 regenerate/dismiss 的 append-before-delete Surface 事件 |

---

## 架构

```
Web / Mobile
      │ REST + SSE/WebSocket
      ▼
FastAPI 控制面
  ├── Agent Driver、模型/上下文策略、有序工具调度
  ├── PostgreSQL + Redis：持久所有权、Session、Cron、事件
  └── 权限、租户/项目身份、Skill/MCP catalogue 策略
      │ 带认证与 scope/fencing header 的 SandboxClient
      ▼
无影执行面
  ├── root 所有的 Action Server
  ├── 非 root sandbox runner
  └── 租户/项目命名空间工作区和 Skill/MCP 状态
```

### 执行边界(宿主机 vs 沙箱)

| 工具 / 模块 | 执行位置 | 原因 |
|---|---|---|
| `bash`、`read`、`write`、`edit`、`apply_patch`、`glob`、`grep` | **沙箱** | 文件 / 命令操作必须隔离 |
| 沙箱 MCP 进程 | **无影** | stdio server 与文件状态留在执行面 |
| MCP catalogue/策略和 host MCP | 后端 | scope、权限、canonical identity 与生命周期属于控制面 |
| Skill 加载 | 后端或**无影** | 项目/宿主指令保持权威，用户安装包从其 scope 沙箱读取 |
| Skill 执行(LLM 按指令行动) | **沙箱** | 实际操作走 `bash`/`write` |
| 可信平台插件 | Backend 运行期 | 依赖有序 generation、异步 setup/dispose、原子热替换、LKG 回滚与在途调用排空；仅限管理员控制的代码 |
| `web_fetch`、`web_search` | 宿主机 | 网络请求 |
| Agent hooks 与平台注册集成 | 后端 | 认证、参数策略、trace 与提交排序属于控制面 |
| Agent 编排 / 权限 / 事件总线 | 宿主机 | 控制面 |

---

## 核心能力

- **Agent 内核**（`backend/agent/`）：持久 Driver lease/generation、周期恢复、逐步模型选择、portable 工具暴露、有序工具调度和有界 Task handoff。
- **无影沙箱**（`backend/sandbox/`、`container/action_server.py`）：唯一支持的执行 Provider、scope catalogue、非 root 命令执行、桌面 lease 和 run fencing。
- **22+ 内置工具**:bash、read、write、edit、glob、grep、mcp、skill、web_fetch、web_search、question、todo、plan、batch……
- **细粒度权限**(`backend/permission/`):逐工具审批流 + 用户交互式确认。
- **上下文 / 记忆**：完整内部 transcript、公共最新窗口分页、压缩、沙箱 instruction 发现和破坏性投影的 append-only 快照。
- **Cron 代理**（`backend/cron/`）：带数据库 lease、fencing、heartbeat、takeover 和项目所有权的定时 Agent。
- **Session 分支 / 恢复**：仅从完整闭合 Turn 的事件前缀分支，保存 lineage，并配套快照、regenerate 与 fenced recovery。
- **v2 前端工作台**(`frontend-v2/`):流式对话、工具 / 思考轨迹、权限 / 问题 / 计划 / Todo 卡片,以及 Diff 审阅、PTY 终端、浏览器、桌面和文件面板。
- **产品级 UI 基础**:中英文国际化、8 套主题、深浅色模式、4 档字号、无障碍交互与响应式布局。

---

## 技术栈

**后端**(Python 3.12)
- FastAPI + Uvicorn · 自研 **Agent Driver / Processor / Inbox** · **LiteLLM + OpenAI Responses** Provider 适配
- PostgreSQL(SQLAlchemy async + Alembic)· Redis(session / ticket / 上下文缓存)
- WUYING Action Server client · 阿里云 OSS / 可选 Azure Blob 集成
- JWT + Logto OIDC(企业 SSO)

**前端 v2**(React 19)
- Vite 8 + TypeScript 6 · Tailwind CSS 4 语义化 Token
- Zustand 5 + TanStack Query 5 · React Router 8 · i18next
- xterm.js 6(PTY)· Vitest + Testing Library · Playwright

**基础设施**
- 阿里云无影执行面 · Docker Compose 仅提供本地 PostgreSQL/Redis/Azurite · Makefile 工作流 · Python/Node/Flutter monorepo

---

## 目录结构

```
OpenBox/
├── backend/
│   ├── agent/        # Agent 循环、压缩、缓存、重试、钩子
│   ├── sandbox/      # 无影 Provider、scope client 与 manager
│   ├── tool/         # 内置工具
│   ├── permission/   # 逐工具审批
│   ├── mcp/          # MCP 集成
│   ├── skill/        # Skill 加载 / 执行
│   ├── session/      # Session 生命周期、分支 / 回滚
│   ├── cron/         # 定时代理
│   ├── api/ · auth/ · db/ · bus/ · cache/ · blob/
│   └── main.py
├── frontend-v2/      # 主推的 React 19 UI（持续开发）
├── frontend/         # 旧版 v1 UI（仅作迁移参考）
├── container/        # 无影 Action Server
├── docs/             # 架构与设计文档
└── docker-compose.yml
```

---

## 快速开始(本地)

```bash
# 本地依赖(PostgreSQL + Redis + Azurite)
make deps

# 配置无影执行面和模型供应商
cp backend/openbox.jsonc.example backend/openbox.json   # 配置模型 / 供应商
cp backend/.env.example backend/.env.wuying-dev         # 填密钥（切勿提交）
cd backend && ./scripts/wuying_tunnel.sh                 # 建立 Action Server 本地转发

# 启动后端（FastAPI，http://localhost:8080）
cd backend && uv sync && cd ..
make backend
```

在新终端启动主推前端:

```bash
cd frontend-v2
npm ci
npm run dev          # /api 和 /ws 自动代理到 localhost:8080
```

提交 PR 前运行 v2 质量门禁:

```bash
cd frontend-v2
npm run check          # i18n 对齐 + ESLint + TypeScript + Vitest
npx playwright test    # E2E；需要后端和 devtest 账号
```

后端会在配置和运行时拒绝 Docker、Kubernetes 与未知沙箱 Provider。部署 Action Server 前请阅读 [`docs/WUYING_SANDBOX.md`](docs/WUYING_SANDBOX.md)。

---

## 文档

当前实现文档：[`AGENT_KERNEL_ARCHITECTURE.md`](docs/AGENT_KERNEL_ARCHITECTURE.md)、[`WORKSPACE_NAMESPACING.md`](docs/WORKSPACE_NAMESPACING.md)、[`WUYING_SANDBOX.md`](docs/WUYING_SANDBOX.md)、[`PREVIEW_ORIGIN_ISOLATION.md`](docs/PREVIEW_ORIGIN_ISOLATION.md) 与 [`DeepSeek-Harness-vs-OpenBox-source-analysis.md`](docs/DeepSeek-Harness-vs-OpenBox-source-analysis.md)。

---

## 说明

密钥与环境文件不进入版本库。请通过忽略的环境文件和 `openbox.json` 配置模型与无影凭据；浏览器公开 API 不得返回 Action Server 内部凭据。
