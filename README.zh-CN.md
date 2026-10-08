# OpenBox

[English](README.md) | 中文

**一个 AI Agent 执行平台** —— 给大模型一个安全隔离的沙箱,让它读写文件、运行代码、操作浏览器,并配套生产级的编排、上下文管理与多租户隔离。

> 一个通用 Agent 运行时(灵感来自 OpenCode / Claude Code),用 Python 围绕 **Pydantic AI + LiteLLM** 重写,所有文件 / 命令操作都限制在每个 Session 独占的 **无影云桌面沙箱**内执行(Docker / Kubernetes 引擎仍保留，但已不是生产路径)。部署在 AWS(开发)与阿里云(生产)，见 [docs/DEPLOY.md](docs/DEPLOY.md)。

> **前端方向：**[`frontend-v2/`](frontend-v2/) 是 OpenBox 当前主推且持续开发的 Web UI；原 [`frontend/`](frontend/) 仅作为旧版迁移参考保留。

---

## 最新更新：长期记忆、个人助理与语音通话（2026-10）

- **长期记忆**（`backend/memory/`）：从对话里学习用户说过的事，只保存用户原话能证明的，并且每条先核验再保存。记忆分个人和项目两种范围。需要时自动召回（JEV 判断要不要查，向量检索加精排）；用户删掉的不会再学回来。
- **知识库**：记忆、自动整理的主题页和上传的文档放在同一个页面，网页和手机都有。
- **个人助理**（`backend/assistant/`）：一段长期对话里的私人秘书。
  - 把事情派到用户的项目对话里执行（用云桌面），跟进进度，用白话汇报。
  - 把等用户回复的问题集中起来，还能跑定时任务和每日简报。
  - 名字、称呼、语气、回答长短都可以设置；初次见面会问几个问题，每个都能跳过。
- **实时语音通话**（`backend/voice/`）：个人助理的电话前台。网页是悬浮通话窗，手机是通话页；通话中能汇报任务结果，按积分计费。
- **左栏改版**：网页和手机都改了。网页的搜索还能搜对话正文。

个人助理和新左栏没有开关，部署后所有用户可见；长期记忆和语音通话默认关闭，需要配置后才生效。开关、数据库迁移和生产升级演练见 [docs/MEMORY_ASSISTANT_VOICE_RELEASE_20261008.md](docs/MEMORY_ASSISTANT_VOICE_RELEASE_20261008.md)。

---

## 速览

| | |
|---|---|
| **是什么** | 一个全栈平台:AI 代理在隔离容器中自主执行开发任务(改代码、跑 bash、git、浏览网页),v2 Web UI 实时展示每一次工具调用。 |
| **核心技术** | FastAPI · Pydantic AI · LiteLLM(100+ 模型)· 无影云桌面沙箱 · PostgreSQL · Redis · React 19 |
| **Agent 循环** | Pydantic AI 单轮工具调用 + 自研外层循环:多轮编排、权限检查、重试、上下文压缩 |
| **隔离** | 每个 Session 独占一个沙箱容器;用户文件 / 命令工具在沙箱内执行,控制面逻辑在宿主机 |
| **规模** | 本地 Docker,生产无影云桌面;多租户(工作区 / 项目 / 权限继承) |

---

## OpenBox 有何不同

大多数「让大模型跑代码」的 demo 一上生产就崩。OpenBox 把 Agent 当成一个**系统**来做,而不只是一段 prompt:

| 关注点 | 普通 Agent demo | OpenBox |
|---|---|---|
| **安全** | 大模型直接在宿主机执行命令 | 每个 `bash`/`read`/`write`/`edit`/`glob`/`grep` 都**在 Session 沙箱内执行**;宿主机只做控制面 |
| **上下文溢出** | 对话一直增长直到撑爆窗口 | **上下文自动压缩**(溢出时摘要历史)+ 工具输出裁剪 + 提示缓存 |
| **可靠性** | 一次失败的工具调用就崩 | 自研外层循环:逐工具重试、权限门控、优雅降级 |
| **多用户** | 单一共享进程 | 工作区 / 项目隔离、每 Session 独占容器、Logto OIDC(企业 SSO)、凭据边界 |
| **可审计** | 不透明的对话历史 | 实时事件流(SSE + WebSocket)、工具执行可视化、Session 分支 / 回滚(git 式历史) |

---

## 架构

```
┌─────────────────────────────────────────────┐
│  OpenBox API (FastAPI, 宿主机)                │
│  Agent 编排 · 权限 · Skill · MCP ·            │
│  Session · Cron · 事件总线                     │
├──────────────────┬────────────────────────────┤
│  Pydantic AI      │   沙箱(每 Session 独占)    │
│  工具调用循环      │   bash / read / write /     │
│  + LiteLLM        │   edit / glob / grep ...     │
└──────────────────┴────────────────────────────┘
                        │
                 Docker 容器      (本地)
                 无影云桌面        (生产)
```

### 执行边界(宿主机 vs 沙箱)

| 工具 / 模块 | 执行位置 | 原因 |
|---|---|---|
| `bash`、`read`、`write`、`edit`、`apply_patch`、`glob`、`grep` | **沙箱** | 文件 / 命令操作必须隔离 |
| MCP 工具调用 | 宿主机 | MCP 服务器是独立进程 |
| Skill 加载(读 `SKILL.md`) | 宿主机 | 读配置,无风险 |
| Skill 执行(LLM 按指令行动) | **沙箱** | 实际操作走 `bash`/`write` |
| `web_fetch`、`web_search` | 宿主机 | 网络请求 |
| Plugin 代码 + hooks | 宿主机 | 认证、参数修改等宿主机逻辑 |
| Agent 编排 / 权限 / 事件总线 | 宿主机 | 控制面 |

---

## 核心能力

- **Agent 循环**(`backend/agent/`):`loop.py` 外层编排、`compaction.py` 上下文自动摘要、`caching.py` 提示缓存、`retry.py` 重试、`hooks.py` 生命周期钩子。
- **沙箱管理**(`backend/sandbox/`):`wuying.py`(生产引擎)、`docker.py` / `kubernetes.py`(遗留引擎)、`manager.py` 生命周期(Session 开始建、结束销毁)。
- **22+ 内置工具**:bash、read、write、edit、glob、grep、mcp、skill、web_fetch、web_search、question、todo、plan、batch……
- **细粒度权限**(`backend/permission/`):逐工具审批流 + 用户交互式确认。
- **上下文与记忆**：内存当前轮 → 数据库压缩历史 → 长期 instruction 文件 → **用户长期记忆**（`backend/memory/`）。长期记忆每条都经过核验，分个人和项目两种范围，用 Qdrant 向量召回加精排；配套的知识库会自动生成主题页（`backend/wiki_compiler/`）。
- **个人助理**（`backend/assistant/`）：把工作派到项目对话里执行，跟到出结果再用白话汇报。它还会集中等用户回复的问题，跑定时任务和每日简报；用户的设置网页、手机和电话共用。
- **实时语音**（`backend/voice/`）：和个人助理语音通话（DashScope 实时语音）。涉及工作的问题交给助理处理，通话按积分计费。
- **Cron 代理**(`backend/cron/`):定时自主执行的 Agent。
- **Session 分支 / 回滚**:git 式的历史管理。
- **v2 前端工作台**(`frontend-v2/`):流式对话、工具 / 思考轨迹、权限 / 问题 / 计划 / Todo 卡片,以及 Diff 审阅、PTY 终端、浏览器、桌面和文件面板。
- **产品级 UI 基础**:中英文国际化、8 套主题、深浅色模式、4 档字号、无障碍交互与响应式布局。
- **手机 App**（`mobile/`）：Flutter 客户端，有对话、个人助理、语音通话和知识库；文案文件与网页逐字一致。

---

## 技术栈

**后端**(Python 3.12)
- FastAPI + Uvicorn · **Pydantic AI**(Agent 循环)· **LiteLLM**(100+ 供应商)
- PostgreSQL(SQLAlchemy async + Alembic)· Redis(session / ticket / 上下文缓存)
- Qdrant（记忆向量索引）· DashScope 向量、精排与实时语音 · JEV（记忆路由）
- Docker SDK + Kubernetes client(沙箱)· Azure Blob Storage(用户文件)
- JWT + Logto OIDC(企业 SSO)

**前端 v2**(React 19)
- Vite 8 + TypeScript 6 · Tailwind CSS 4 语义化 Token
- Zustand 5 + TanStack Query 5 · React Router 8 · i18next
- xterm.js 6(PTY)· Vitest + Testing Library · Playwright

**手机端**（Flutter）
- Riverpod · go_router · Dio · WebSocket · 语音通话用 `record` 录音、PCM 播放

**基础设施**
- AWS EC2(开发)+ 阿里云 ECS(生产),均以 Docker Compose 运行 · Docker Compose(本地依赖)· Makefile 工作流 · Python/Node monorepo

---

## 目录结构

```
OpenBox/
├── backend/
│   ├── agent/        # Agent 循环、压缩、缓存、重试、钩子
│   ├── sandbox/      # docker.py + kubernetes.py 双引擎、manager
│   ├── tool/         # 内置工具
│   ├── permission/   # 逐工具审批
│   ├── mcp/          # MCP 集成
│   ├── skill/        # Skill 加载 / 执行
│   ├── session/      # Session 生命周期、分支 / 回滚
│   ├── cron/         # 定时代理
│   ├── memory/       # 长期记忆：抽取、核验、召回、知识库
│   ├── wiki_compiler/ # 知识库主题页自动生成
│   ├── assistant/    # 个人助理：委托、汇报、定时、个人设置
│   ├── voice/        # 实时语音通话（前台提示词、桥接、计量）
│   ├── api/ · auth/ · db/ · bus/ · cache/ · blob/
│   └── main.py
├── frontend-v2/      # 主推的 React 19 UI（持续开发）
├── frontend/         # 旧版 v1 UI（仅作迁移参考）
├── mobile/           # Flutter App（iOS / Android）
├── demos/            # 独立演示（实时语音）
├── container/        # 沙箱镜像(action_server)
├── k8s/              # 遗留 GKE/AKS 清单(冻结，非生产路径)
├── docs/             # 架构与设计文档
└── docker-compose.yml   # 仅本地开发;生产 compose 在服务器上(见 docs/DEPLOY.md)
```

---

## 快速开始(本地)

```bash
# 本地依赖(PostgreSQL + Redis + Azurite)
make deps

# 配置并启动后端(FastAPI,http://localhost:8080)
cp backend/openbox.jsonc.example backend/openbox.json   # 配置模型 / 供应商
cp backend/.env.example backend/.env                    # 填密钥(切勿提交)
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

v2 生产镜像定义在 `frontend-v2/Dockerfile`。镜像如何构建、传输并发布到 AWS 开发机与阿里云生产机，见 [docs/DEPLOY.md](docs/DEPLOY.md)。`k8s/` 里的 GKE/AKS 清单属冻结遗留内容。

---

## 文档

设计文档见 [`docs/`](docs/):`OPENAGENT_DESIGN.md`(Agent 架构)、`FRONTEND_DESIGN.md`、`API_INTERFACES.md`、`MULTI_USER_STORAGE_PLAN.md`、`CRON_SYSTEM_PLAN.md`、`PTY_UPGRADE_PLAN.md`、`PERFORMANCE_OPTIMIZATION.md`、[`DEPLOY.md`](docs/DEPLOY.md)(AWS 开发 + 阿里云生产部署)、[`LOGTO_PROD.md`](docs/LOGTO_PROD.md)(各环境 Logto SSO)、[`WUYING_SANDBOX.md`](docs/WUYING_SANDBOX.md)(把沙箱跑在阿里云无影云电脑上)。

长期记忆、个人助理与语音：[`MEMORY_ASSISTANT_VOICE_RELEASE_20261008.md`](docs/MEMORY_ASSISTANT_VOICE_RELEASE_20261008.md)（更新内容与上线须知）、[`LONG_TERM_MEMORY_PLAN.md`](docs/LONG_TERM_MEMORY_PLAN.md)、[`CONSUMER_KNOWLEDGE_IMPLEMENTATION.md`](docs/CONSUMER_KNOWLEDGE_IMPLEMENTATION.md)、[`PERSONAL_ASSISTANT_DESIGN_V2.md`](docs/PERSONAL_ASSISTANT_DESIGN_V2.md)、[`VOICE_CALL_SPEC.md`](docs/VOICE_CALL_SPEC.md)，以及开发日志 [`DEVLOG.md`](docs/DEVLOG.md)。

---

## 说明

这是一份脱敏的公开副本:已移除密钥与环境文件;沙箱镜像内部及内置的 Agent 框架已剔除。请通过 `.env` / `openbox.json` 配置你自己的模型供应商与凭据。
