# OpenBox 开发日志

## 项目概述

OpenBox 是一个 Web 沙箱管理平台，参考 [OpenHands](https://github.com/All-Hands-AI/OpenHands) 的沙箱架构设计，提供通过 WebUI 创建、管理 Docker 沙箱容器并与容器内终端实时通讯的能力。

### 架构

```
WebUI (React+Vite+TailwindCSS) ←→ REST/WebSocket ←→ Backend (FastAPI) ←→ HTTP ←→ Container Action Server (FastAPI)
```

- **前端**: React 19 + Vite 6 + TailwindCSS 4 + xterm.js + lucide-react
- **后端**: Python 3.12 + FastAPI + docker-py + httpx，使用 uv 管理依赖
- **容器服务**: 每个沙箱容器内运行一个轻量 FastAPI Action Server，提供命令执行、文件管理等 API
- **通讯**: 前端通过 WebSocket 连接后端，后端作为代理转发请求到容器

---

## 第一阶段：基础平台搭建（已完成）

### 设计分析

在实现前，对 OpenHands 项目进行了深入分析：

1. **沙箱架构**: OpenHands 使用 Docker 容器作为沙箱，每个会话独立一个容器（`container_name = openhands-runtime-{sid}`）
2. **LLM Agent 位置**: LLM Agent 在外部（Python 进程），Docker 容器内只有 Action Execution Server（无 LLM）
3. **通讯模式**: 外部 Agent 通过 HTTP REST 调用容器内的 Action Server
4. **容器初始化**: 基于 Dockerfile.j2 模板构建，预装 Python 环境（micromamba + poetry）、Node.js、常用工具

### 关键设计决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 后端代理模式 | 前端不直接访问容器端口 | 安全性，API Key 对前端透明 |
| 终端通讯 | WebSocket 命令-响应模式 | 第一阶段简单可靠 |
| 端口分配 | 10000-19999 动态分配 | socket bind 检测可用端口 |
| 容器安全 | 内存 512MB / CPU 0.5 核 / tini init | 资源隔离 |
| API Key | 每容器独立 key，环境变量注入 | 容器间隔离认证 |

### 实现过程

使用 4 个并行 Agent 同时开发：

- **Agent A (容器服务)**: 创建 `container/action_server.py`、`Dockerfile`、`requirements.txt`
- **Agent B (后端 API)**: 创建 12 个后端文件（FastAPI 应用完整结构）
- **Agent C (前端 WebUI)**: 创建所有 React 组件源文件（因权限限制未完成 npm install）
- **Agent D (基础设施)**: 创建 `docker-compose.yml`、`Makefile`、`.gitignore`、Dockerfile、nginx.conf

### 后续修复

Agent 完成后进行了以下修复：

1. **前端 npm 依赖安装** — Agent C 因子进程权限限制无法执行 npm 命令，手动完成
2. **xterm 包迁移** — 从已废弃的 `xterm` 迁移到 `@xterm/xterm@^5.5.0`，更新 Terminal.tsx 中的 import
3. **添加 `@types/node`** — 修复 vite.config.ts 中 `path` 和 `__dirname` 的类型错误
4. **pyproject.toml 修复** — 添加 `[tool.hatch.build.targets.wheel] packages = ["app"]`，解决 hatchling 找不到包的问题
5. **docker_manager.py 异步修复（关键）** — 所有 Docker SDK 阻塞调用（`containers.run`、`containers.get`、`container.remove`、`container.stop`、`container.start`、`containers.list`）都用 `loop.run_in_executor()` 包装，避免阻塞 asyncio 事件循环
6. **`_wait_until_ready` 日志改进** — 添加尝试计数和分类错误处理

### 端到端验证结果

| 端点 | 状态 |
|------|------|
| `GET /health` | OK |
| `POST /api/containers` (创建) | OK |
| `GET /api/containers` (列表) | OK |
| `DELETE /api/containers/:id` (删除) | OK |
| `POST /api/containers/:id/files/list` | OK |
| `GET /api/containers/:id/files/system_info` | OK |
| `WS /ws/terminal/:id` (命令执行) | OK |

---

## 项目文件结构

```
OpenBox/
├── docker-compose.yml          # 开发编排（backend + frontend）
├── Makefile                    # 常用命令（dev, build, up, down, clean）
├── .gitignore
├── docs/
│   ├── DEVLOG.md               # 本文件 - 开发日志
│   └── PTY_UPGRADE_PLAN.md     # PTY 升级计划
├── container/
│   ├── action_server.py        # 容器内 FastAPI 服务（6个端点 + API Key 中间件）
│   ├── Dockerfile              # python:3.12-slim + 工具 + sandbox 用户
│   └── requirements.txt        # fastapi, uvicorn, psutil, python-multipart
├── backend/
│   ├── pyproject.toml          # uv 项目配置
│   ├── Dockerfile
│   └── app/
│       ├── __init__.py
│       ├── main.py             # FastAPI 入口，CORS，lifespan，/health
│       ├── models/
│       │   ├── __init__.py
│       │   └── schemas.py      # Pydantic 模型
│       ├── core/
│       │   ├── __init__.py
│       │   ├── config.py       # Settings 配置（端口范围、镜像名等）
│       │   └── docker_manager.py  # Docker 容器 CRUD + HTTP 转发（核心）
│       └── api/
│           ├── __init__.py
│           ├── containers.py   # REST: POST/GET/DELETE /api/containers
│           ├── terminal.py     # WebSocket: /ws/terminal/{id}
│           └── files.py        # 文件列表 + 系统信息代理
└── frontend/
    ├── package.json
    ├── vite.config.ts          # Vite + TailwindCSS + 路径别名 + 代理
    ├── tsconfig.json / tsconfig.app.json / tsconfig.node.json
    ├── index.html
    ├── Dockerfile              # 多阶段构建（node builder → nginx）
    ├── nginx.conf              # SPA 路由 + API/WS 反向代理
    └── src/
        ├── main.tsx
        ├── App.tsx             # 主布局（侧边栏 + 终端面板）
        ├── index.css           # TailwindCSS
        ├── vite-env.d.ts
        ├── lib/utils.ts        # cn() 工具函数
        ├── types/index.ts      # TypeScript 类型定义
        ├── services/api.ts     # API 封装（fetch + WS URL 生成）
        ├── hooks/
        │   ├── useContainers.ts  # 容器 CRUD 状态管理（5s 轮询）
        │   └── useWebSocket.ts   # WebSocket hook（自动重连）
        └── components/
            ├── layout/
            │   ├── Header.tsx    # 顶部栏（logo + 容器计数）
            │   └── Sidebar.tsx   # 侧边栏（新建按钮 + 容器列表）
            ├── containers/
            │   ├── ContainerCard.tsx          # 容器卡片（状态指示器 + 操作按钮）
            │   ├── ContainerList.tsx          # 容器列表
            │   └── CreateContainerDialog.tsx  # 创建对话框
            └── terminal/
                ├── Terminal.tsx      # xterm.js 终端（核心组件）
                └── TerminalTabs.tsx  # 多终端标签管理
```

---

## 启动方式

```bash
# 1. 构建沙箱镜像
docker build -t openbox-sandbox:latest ./container

# 2. 启动后端（终端 1）
cd backend
~/.local/bin/uv run uvicorn app.main:app --reload --port 8080

# 3. 启动前端（终端 2）
cd frontend
npm run dev
```

前端访问 `http://localhost:5173`，Vite 自动代理 `/api` 和 `/ws` 请求到后端 8080 端口。

---

## 第二阶段：PTY 终端升级（已完成）

### 概述

将终端从"命令-响应"模式升级为真正的 PTY（伪终端）交互模式，支持交互式程序（python3 REPL、vim、htop）、Shell 特性（Tab 补全、箭头键历史、Ctrl+C/D/Z）、持久会话和终端大小同步。

### 架构变更

```
升级前（命令-响应）:
  xterm.js --JSON--> Backend --HTTP POST /execute--> Container (每次新建 subprocess)

升级后（PTY 流式）:
  xterm.js --二进制帧(原始按键)--> Backend WS relay --二进制帧--> Container WS --write()--> PTY master fd
  xterm.js <--二进制帧(终端输出)<-- Backend WS relay <--二进制帧<-- Container WS <--read()<-- PTY master fd
```

### 二进制帧协议

所有 WebSocket binary frame 使用 1 字节前缀区分消息类型：

| 前缀字节 | 方向 | 含义 | payload |
|----------|------|------|---------|
| `0x00` | 双向 | 终端数据 | 原始 PTY 字节流 |
| `0x01` | 客户端→服务端 | 窗口大小变更 | cols(2B big-endian) + rows(2B big-endian) |

### 修改文件

| 文件 | 变更类型 | 说明 |
|------|----------|------|
| `container/action_server.py` | 新增端点 | 添加 `/terminal` WebSocket 端点：`pty.openpty()` + `os.fork()` + `os.execve()` 创建 PTY bash 会话 |
| `container/requirements.txt` | 修改 | `uvicorn` → `uvicorn[standard]` 以支持 WebSocket |
| `backend/app/api/terminal.py` | 重写 | 从 HTTP POST 转发改为 WS↔WS 透明中继，使用 `websockets` 库连接容器 |
| `frontend/src/hooks/useWebSocket.ts` | 增量修改 | 添加 `binaryType = "arraybuffer"`、`onBinaryMessage` 回调、`sendBinary` 方法 |
| `frontend/src/types/index.ts` | 新增常量 | `TERMINAL_MSG_DATA = 0x00`、`TERMINAL_MSG_RESIZE = 0x01` |
| `frontend/src/components/terminal/Terminal.tsx` | 重写 | 删除手动行编辑逻辑，改为每个按键即时发送二进制帧，xterm 直接渲染 PTY 输出 |

### 实现细节

**容器端 (`action_server.py`)**:
- `pty.openpty()` 创建 master/slave fd 对
- `os.fork()` 子进程中 `os.setsid()` + `TIOCSCTTY` 设置控制终端
- 切换到 sandbox 用户 (uid/gid) → `os.execve("/bin/bash", ["bash", "--login"], env)`
- master_fd 设为 non-blocking，通过 `loop.run_in_executor()` 阻塞读取
- `_blocking_read()` 使用 `select` 超时 0.5s，返回 `None` 表示 fd 错误，`b""` 表示超时
- 清理流程: SIGTERM → 0.1s → SIGKILL → waitpid → close fd → close ws

**后端中继 (`terminal.py`)**:
- `websockets.connect()` 连接容器 `/terminal?api_key=xxx`
- `frontend_to_container` + `container_to_frontend` 两个并发 task 透明转发
- binary 和 text frame 直接透传，后端不解析帧内容

**前端 (`Terminal.tsx`)**:
- `xterm.onData()` 每个按键立即构造 `[0x00, ...encoded]` 发送
- `xterm.onBinary()` 处理鼠标等二进制事件
- `xterm.onResize()` + `ResizeObserver` 发送 `[0x01, colsHi, colsLo, rowsHi, rowsLo]`
- 连接建立后发送初始 resize 同步终端尺寸

### Code Review 发现及修复

经过两轮 code review，发现并修复了以下问题：

| 优先级 | 问题 | 修复 |
|--------|------|------|
| P1 | `_blocking_read` 内 `while True` 循环永不返回，阻塞 executor 线程 | 移除循环，改为单次 `select` 调用 |
| P1 | WebSocket 认证失败时 `ws.close()` 在 `ws.accept()` 之前调用 | 先 `accept()` 再 `close()` |
| P2 | `asyncio.get_event_loop()` 已废弃 | 改为 `asyncio.get_running_loop()` |
| P2 | `asyncio.ensure_future()` 已废弃 | 改为 `asyncio.create_task()` |
| P2 | `_blocking_read` 返回 `b""` 超时导致 `if not data: break` 误退出 | 区分 `None`(fd 错误) 和 `b""`(超时) |
| P2 | 前端 `connectedRef` 未使用的死代码 | 删除 |
| P3 | `bytes.slice()` 不如 `bytes.subarray()` 高效 | 改为 `subarray()` |

### 运行时问题及修复

| 问题 | 原因 | 修复 |
|------|------|------|
| 容器 WS 握手返回 HTTP 403 | HTTP 中间件拦截了 `/terminal` 路径的 WebSocket 升级请求 | 将 `"/terminal"` 加入中间件白名单 |
| 容器返回 "No supported WebSocket library detected" | uvicorn 基础安装不含 WebSocket 支持 | `requirements.txt` 改为 `uvicorn[standard]>=0.32.0` |

### 测试结果

11/11 端到端测试通过：

- PTY 连接建立、echo 输出、pwd 工作目录
- 环境变量持久性 (`export FOO=bar` → `echo $FOO`)
- Ctrl+C 中断信号、终端 resize
- whoami 用户身份、后端 WS 中继 (3 项)、认证拒绝

---

## 当前已知限制

1. ~~**终端为命令-响应模式**~~ — ✅ 已升级为 PTY 交互模式
2. **无用户认证** — 后端 API 无登录/权限控制
3. **容器状态非持久化** — 后端重启后丢失容器映射（容器本身仍在 Docker 中）
4. **单机部署** — 不支持多节点

---

## Skill Job Runtime 阶段（2026-08-28）

按 `docs/archive/SKILL_SCRIPT_RUNTIME_REBUILD_PLAN.md`（v2，现已归档）实施，一次性落地 PR#0–17：

- **止血**（`video/job_recovery.py`）：滞留视频 finalize 的启动恢复 + cron piggyback 补扫；上线首日即在 dev 库发现并安全处理两个 8/27 遗留任务。
- **通用 Runtime**（`backend/skill_runtime/`）：九态状态机、七张表（PG 实测 migration `a2c4e6f8b0d1`）、幂等接纳（服务端从 tool_call 派生默认键）、条件 UPDATE claim + fencing token、七种 Outcome 结算、transactional outbox、Reconciler（lease 回收/外部到期/deadline）、独立 worker 角色（compose + k8s 清单）与开发 embedded 模式（单用户模式初始化 `.openbox/skill_jobs.db`）。
- **取消语义**：desired_state + handler 收敛；provider 事实优先（cancel_race 保留付费产物）；`WaitExternal.acknowledges_cancel` 保护转存中等待。
- **接口面**：`/api/skill-jobs*` + `/api/skills/settings`、通用 `skill_job` 工具（wait 每回合 2 次预算）、web/mobile Job Card 与 dock、终态聊天回执（`SkillJobPart`，零 Token）。
- **视频迁移**：`builtin_skills/video_production` 四操作（status/generate/transcribe/render），灰度闸 `SKILL_JOBS_VIDEO_WRITE` 默认关；submit_unknown 人工审计绝不自动重提；无影 media 队列线协议已对 dev 桌面实测（`wuying_dev.sh`）。
- **测试**：新增 ~120 项（含 100 并发幂等、stale lease 拒写、demo/视频 E2E、双用户 IDOR、回执解析回归）；浏览器全链路验收通过。

待办：PR#18 sandbox runtime（用户脚本，里程碑 C）、PR#19 旧视频工具删除（灰度完成后）、PR#20 生产加固；Phase 5 灰度开启为运维动作（开关 + 换用包内 v2 SKILL.md）。

---

## 视频统一渠道 + 双模型选择（2026-08-29）

### 目标

把视频调用收敛成一条统一渠道：新增模型只改配置；用户在输入框同时选择「大语言模型」与「视频模型」；视频制作逻辑走 SkillJob runtime。

### 关键决策

| 决策 | 选择 | 理由 |
|------|------|------|
| 配置化的边界 | 配置管**绑定**（模型→渠道/凭证/能力），代码管**协议**（ark/sd2/task 三个适配器） | 「轮询 `metadata.url`」「拆 `{code,message,data}` 信封」不是配置能表达的东西。说同一协议的新模型纯配置即可加，新协议仍需发版 |
| 切换语义 | 提交时把模型**冻结**到分段（`video_segments.model` 写回） | 在飞的分段保持原模型，新选择只影响尚未提交的分段。不写回的话，重试或对账会用新模型重新解析，花掉在飞任务从未批准的钱 |
| 能力声明 | `resolutions` / `max_duration_seconds` / `supports_reference_*` 提交前强校验 | 中转站**静默丢弃**不认识的参数、替换成默认值并照常计费——实测传三个非法值仍建任务出片。这是唯一能拦住的地方 |
| 两个选择器 | 并列但图标区分（场记板），菜单显示价格档位 | 二者独立且代价不同：换 LLM 免费即时，换视频模型花真钱 |

### 实测得到的两条事实（决定了配置怎么写）

对 `openapi.bossipai.com.cn` 逐模型探测（`503 No available channel` = 无，`400 prompt is required` = 有，编造的模型名做对照组）：

1. 中转站有 6 个视频模型，含 `wan3.0-video` / `wan3.0-video-prime`，且**真实出片**（1920×1080 / 5.04s，产物落在 dashscope OSS，证实上游是阿里百炼）。
2. 中转站**没有** `/v1/video/generations`（前置 nginx 404），只有 `/v1/videos`。所以 wan3.0 在此部署必须声明为 `sd2` 渠道而非 `task` —— 这正是声明式配置存在的意义。

由此还补了一个会致命的缺口：sd2 分支原先不读 `metadata.url`，而中转站的成品**只**放在那里，照原样接会「已完成却拿不到视频」，钱已经花了。

### 改动

- `core/config.py`：`VideoModelConfig`（id/name/channel/provider/能力/tier）+ `video_generation.models`
- `tool/video_providers.py`：`resolve_route` 声明优先、`_ark_route` 提取复用、`_validate_declared` 能力校验、sd2 补 `metadata.url`
- `sessions.video_model`（migration `d2f4a6b8c0e1`）+ `resolve_segment_model` 冻结写回
- `/api/agent/config` 暴露 `video_models`；`PromptBody`/`RegenerateBody` 带 `video_model`
- 前端：`VideoModelPicker`、`useVideoModelChoice`、`video-model-choice` store、`useComposerModels`（把两处选择收进一个 hook，Composer 复杂度回到 25 以内）
- `skill_jobs_video_write: true` —— legacy `video_generate/transcribe/render` 随即不再注册（33→30 个工具），符合「同时只有一个视频写控制面」

### 验证

后端 971 项、前端 165 项通过；变异测试确认冻结写回与能力校验均有回归保护（删掉即有用例失败）。浏览器实测：两个选择器并列渲染、菜单列出 6 个模型及档位、选中 Wan 3.0 后落库 `video_model=wan3.0-video`、刷新后从会话记录恢复。

### 浏览器端到端验收发现的两个缺陷（同日）

两条都只有真跑浏览器才会暴露，单测用的是精确 id 和默认模型，看不见。

**一、声明落空即静默回退推断。** agent 传 `wan3.0`，部署声明的是 `wan3.0-video`。精确匹配落空后回退到名称推断，推断按家族把它路由到 `task` 渠道——而该中转站上那个端点根本不存在。改为失败关闭：配了 `models` 之后它就是全集，未声明的 id 直接报错并列出可用 id 让 agent 自我纠正。

**二、durable handler 完全忽略分段模型，且不支持网关渠道。** handler 三处写死 `_configured_target(None)`，于是不论用户选什么，付费提交一律走部署默认模型——灰度开关打开后 durable 是唯一写路径，这让视频模型选择器**完全形同虚设**（实测分段快照 `wan3.0-video`，实际扣费 `seedance`）。顺查发现 handler 只会拼 ark payload、直接取 `submitted["id"]`、状态归一不传 target、finalize 不传 route，即多渠道路由从未接入 durable 路径：

| 症状 | 后果 |
|---|---|
| 取 `submitted["id"]` 而非 `extract_task_id` | sd2 上游覆写 `task_id`，轮询必 `task_not_exist` |
| `_finalize_segment` 缺 route | 取不到 sd2 的 `metadata.url`，付费完成的任务落到「没有视频 URL」 |
| `_advance_existing` 用默认模型 | 轮询错端点 |

同类问题还出现在 `video/job_recovery.py` 的滞留补扫——它专门救援卡住的任务，却恰好救不回非默认模型的任务。一并按 `job.model` 路由。

顺带把转写/合成里「只为拿 settings 却顺带解析一次生成路由」的写法改掉：失败关闭之后，默认模型未声明会让它们因无关原因报错。

**验收**：浏览器内选 Wan 3.0 → 三道审批 → `skill_job segment.generate` → succeeded，`video_job.model=wan3.0-video`、`provider_task_id` 为 sd2 的 `task_` 前缀、产物 42MB / 1920×1080 / 5.04s 落入 OSS，Job Card 与终态回执正常渲染。

**已知缺口**：产品没有非口播路径。`video_generate` 与 skill handler 都强制要求 `production_id`+`segment_id`，而 prompt lint 无条件要求固定镜头/中景/手势/语气/无字幕，并要求各段台词拼接后逐字等于已批准脚本。「给我来个 5 秒空镜头」目前不可行——是否加非口播模式属产品决策，未擅自实现。

---

## 直连路径清理（2026-08-30）

两天灰度证明通用 SkillJob 九态运行时的复杂度和运维成本高于当前产品收益，视频写路径
重新收敛到 `.openbox/skills/video-production` 加三个直连工具。移除实现见 `4d93463`，
Web/Mobile 清理见 `ae58de7`，恢复契约强化见 `536622a`；原设计稿已移入
`docs/archive/SKILL_SCRIPT_RUNTIME_REBUILD_PLAN.md` 并加墓碑，不能再作为实施依据。

### 清理与兼容

- 物理删除 runtime、worker、七表 ORM、API、通用 `skill_job` 工具、内置 demo/视频包，
  同步移除 Compose/Kubernetes worker 和所有 Agent/Session/WS/config 接线。
- 删表迁移先把旧 `skill_job_artifacts` 中仍是唯一事实源的 output 映射回填到历史
  `SkillJobPart.artifacts`；36 条回执全部保留，可打开产物的回执由 5 条增至 7 条。
- 保留历史 `sjr:` 回执命名空间；随 `session_inbox` 一并释放已无生产者的 `sji:`
  continuation 索引与客户端前缀限制。
- Web/Mobile 删除 live job dock/card/API/WS，历史回执继续只凭 message part 渲染，并补齐
  视频、图片、普通文件预览及 unknown/missing/unavailable 回退。
- 部署入口会在迁移前停止旧 Compose worker。按本轮最终范围，后续部署只走无影云；
  Kubernetes 不作为交付或验收目标，本轮不再追加其升级编排改动。

### 直连恢复契约

- `video_generate wait` 以 25 秒为硬上限，供应商超时和 OSS 收尾超时返回带
  `version`、`still_running`、`timed_out` 的事实快照；后台收尾 task 按 job 去重并受
  shield 保护，`finalizing` 不再快速空转。
- `video_project status` 返回审批 scope/decision/hash 是否匹配、剩余付费调用预算、
  每段冻结模型与生成 job、生成/转写/合成幂等键；当前 hash 上的拒绝证据与批准 gate
  分开表达。
- 浏览器验收时发现两条 8/27 的 TokenSpace 直连任务仍被当前 BossIP relay 路由按分钟
  错查。现在每次付费提交都会快照版本化 provider route fingerprint（provider/channel/
  wire/base/auth/credential identity），但不把恢复元数据计入逻辑 `request_hash`，保持滚动
  升级期间的幂等兼容。历史任务只要缺少完整 fingerprint 就一律隔离（wire 相同也无法证明
  endpoint/账号未变）；新任务 fingerprint 不匹配时同样隔离。后台恢复与
  `status/wait/cancel` 都在任何 provider I/O 前返回 `provider_state_unknown`：不请求错误
  账号、不改写数据库、不把 400/404 误判成付费任务失败，也绝不自动重提。

### 验证

- PostgreSQL 在快照保护下实跑 `upgrade → downgrade → upgrade`：七表删除、44 列主表/
  23 个索引/全部约束完整回滚、再删除均通过，历史回执回填保持幂等。
- 后端 `917 passed`；其中进程级零费用 E2E 用两个独立 Python 进程和 loopback provider
  验证提交后重启、恢复前抢跑重放、启动恢复、附件收敛及完成后重放均不会二次 POST/
  扣减预算。Web
  `174 passed`、TypeScript/i18n 通过；Mobile analyze 与 4 项 Flutter 测试通过。全量
  ESLint 仍仅有 `content-view.ts` 两个既有复杂度错误，无新增。
- 两项变异验证分别删除 `public_message` 分支与恢复快照预算字段，锚点测试均按预期失败，
  恢复实现后重新通过。
- 浏览器 A/B 已用 `qa_jobs` 实测通过：历史会话的 3 条视频回执均可加载且无 live job UI；
  零花费新会话在同一回合直连完成 create/set_script/request_approval/status，最终停在
  `needs_script_approval`，数据库确认 segments/jobs/approvals 均为 0，控制台无
  warning/error。冷重启后再次复测仍通过；两个 legacy route mismatch 仅在启动时各告警
  一次，跨过 60 秒恢复周期后无 provider HTTP/重复日志，原任务状态与 `updated_at` 未变。
- 另完成零费用浏览器恢复替代场景：把视频路由临时锁到 loopback 后，浏览器人工完成
  剧本/单分段/单次预算审批并提交；后端在 `wait` 后重启，后台跨过 120 秒安全窗后用保存的
  task id 恢复查询，浏览器再按版本继续等待。最终 mock 为 `POST=1 / GET=3`，数据库仅 1 个
  job、`attempt=1`、预算 `1/1`；测试作业随后由 mock 预期失败终态收敛，控制台无
  warning/error，真实供应商请求为 0。付费供应商版场景 C 仍需另行预算授权。
