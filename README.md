# OpenBox

English | [中文](README.zh-CN.md)

**An AI Agent execution platform** — give an LLM a safe, isolated sandbox to read, write, run code, and drive a browser, with production-grade orchestration, context management, and multi-tenant isolation.

> A general-purpose agent runtime (inspired by OpenCode/Claude Code), implemented in Python around a **custom durable Agent kernel** with LiteLLM and OpenAI Responses adapters. The FastAPI control plane owns agent state and policy; file, command, desktop and sandbox MCP execution runs on an Alibaba Cloud **WUYING** desktop. Docker Compose is used only for local infrastructure such as PostgreSQL, Redis and optional Azurite.

> **Frontend direction:** [`frontend-v2/`](frontend-v2/) is the primary and actively developed OpenBox web UI. The original [`frontend/`](frontend/) is retained only as a legacy migration reference.

---

## At a glance

| | |
|---|---|
| **What it is** | A full-stack platform where an AI agent edits code, runs commands and drives a desktop through WUYING, while the v2 web UI and mobile client show every durable turn and tool call. |
| **Core stack** | FastAPI · custom Agent kernel · LiteLLM/Responses · WUYING Action Server · PostgreSQL · Redis · React 19 · Flutter |
| **Agent loop** | Durable single-flight driver, fenced recovery, bounded portable tool schemas, ordered concurrent tool bodies, permission checks and context compaction |
| **Isolation** | The backend owns tenant/project policy; WUYING workspaces, Skill/MCP catalogues, assets and snapshots use stable user/project namespaces |
| **Scale** | Multi-worker SaaS control plane; the current shared desktop is for trusted single-user acceptance, with one WUYING desktop per user as the production isolation boundary |

---

## Why OpenBox is different

Most "let an LLM run code" demos break the moment they hit production. OpenBox treats the agent as a *system*, not a prompt:

| Concern | Typical agent demo | OpenBox |
|---|---|---|
| **Safety** | LLM runs commands on the host | File/command/desktop actions cross an authenticated, tenant-scoped and generation-fenced WUYING boundary |
| **Context overflow** | Conversation grows until it blows the window | Full internal history + compaction, bounded portable tool schemas, deferred capability discovery and output truncation |
| **Reliability** | One bad tool call kills the run | Durable run ownership, cold-tail repair, ordered tool commits, conservative unknown-outcome handling and periodic recovery |
| **Multi-user** | Single shared process | PostgreSQL/Redis control plane, stable user/project namespaces, scoped Skill/MCP state and JWT/Logto support |
| **Auditability** | Opaque chat history | Real-time SSE/WebSocket events, tool traces and append-before-delete Surface provenance for regenerate/dismiss |

---

## Architecture

```
Web / Mobile
      │ REST + SSE/WebSocket
      ▼
FastAPI control plane
  ├── Agent driver, model/context policy and ordered tool scheduler
  ├── PostgreSQL + Redis: durable ownership, sessions, Cron and events
  └── permissions, tenant/project identity, Skill/MCP catalogue policy
      │ authenticated SandboxClient + scope/fencing headers
      ▼
WUYING execution plane
  ├── root-owned Action Server
  ├── non-root sandbox runner
  └── tenant/project namespaced workspaces and Skill/MCP state
```

### Execution boundary (host vs sandbox)

| Tool / module | Runs in | Why |
|---|---|---|
| `bash`, `read`, `write`, `edit`, `apply_patch`, `glob`, `grep` | **Sandbox** | File/command ops must be isolated |
| Sandbox MCP processes | **WUYING** | Stdio servers and their filesystem state stay in the execution plane |
| MCP catalogue/policy and host MCP | Backend | Scope, permission, canonical identity and lifecycle remain control-plane decisions |
| Skill load | Backend or **WUYING** | Project/host instructions remain authoritative; user-installed bundles are read in their scoped sandbox |
| Skill execution (LLM acting) | **Sandbox** | Real actions go through `bash`/`write` |
| Trusted platform plugins | Backend runtime | Dependency-ordered generations, async setup/dispose, atomic hot replacement, LKG rollback and in-flight drain; administrator-controlled code only |
| `web_fetch`, `web_search` | Host | Network requests |
| Agent hooks and registered integrations | Backend | Auth, argument policy, tracing and commit ordering are control-plane logic |
| Agent orchestration / permission / event bus | Host | Control plane |

---

## Key capabilities

- **Agent kernel** (`backend/agent/`): durable Driver leases/generations, periodic recovery, per-step model selection, portable tool exposure, ordered tool scheduling and bounded Task handoffs.
- **WUYING sandbox** (`backend/sandbox/`, `container/action_server.py`): one supported execution provider, scoped catalogues, non-root command execution, desktop leases and run fencing.
- **22+ built-in tools**: bash, read, write, edit, glob, grep, mcp, skill, web_fetch, web_search, question, todo, plan, batch, …
- **Fine-grained permissions** (`backend/permission/`): per-tool approval flow with interactive user confirmation.
- **Context/memory**: complete internal transcript, public latest-window pagination, compaction, sandbox instruction discovery and append-only destructive-projection snapshots.
- **Cron agents** (`backend/cron/`): scheduled autonomous runs with DB leases, fencing, heartbeats, takeover and project ownership.
- **Session branch / recovery**: complete closed-turn event-prefix forks, explicit lineage, snapshots and fenced regenerate/recovery flows.
- **Frontend v2 workbench** (`frontend-v2/`): streaming chat, tool/thinking traces, permission/question/plan/todo cards, diff review, PTY terminal, browser, desktop and file panels.
- **Product-grade UI foundation**: Chinese/English localization, eight theme families, light/dark modes, four font sizes, accessible interactions and responsive layouts.

---

## Tech stack

**Backend** (Python 3.12)
- FastAPI + Uvicorn · custom **Agent Driver / Processor / Inbox** · **LiteLLM + OpenAI Responses** provider adapters
- PostgreSQL (SQLAlchemy async + Alembic) · Redis (session / ticket / context cache)
- WUYING Action Server client · Alibaba OSS / optional Azure Blob integrations
- JWT + Logto OIDC (enterprise SSO)

**Frontend v2** (React 19)
- Vite 8 + TypeScript 6 · Tailwind CSS 4 semantic tokens
- Zustand 5 + TanStack Query 5 · React Router 8 · i18next
- xterm.js 6 (PTY) · Vitest + Testing Library · Playwright

**Infrastructure**
- Alibaba Cloud WUYING execution plane · Docker Compose for local PostgreSQL/Redis/Azurite only · Makefile workflow · Python/Node/Flutter monorepo

---

## Project structure

```
OpenBox/
├── backend/
│   ├── agent/        # agent loop, compaction, caching, retry, hooks
│   ├── sandbox/      # WUYING provider, scoped client and manager
│   ├── tool/         # built-in tools
│   ├── permission/   # per-tool approval
│   ├── mcp/          # MCP integration
│   ├── skill/        # skill loading/execution
│   ├── session/      # session lifecycle, branch/rollback
│   ├── cron/         # scheduled agents
│   ├── api/ · auth/ · db/ · bus/ · cache/ · blob/
│   └── main.py
├── frontend-v2/      # Primary React 19 UI (active development)
├── frontend/         # Legacy v1 UI (migration reference only)
├── container/        # WUYING Action Server
├── docs/             # architecture & design docs
└── docker-compose.yml
```

---

## Quick start (local)

```bash
# Local dependencies (PostgreSQL + Redis + Azurite)
make deps

# Configure the WUYING execution plane and model providers
cp backend/openbox.jsonc.example backend/openbox.json   # configure models/providers
cp backend/.env.example backend/.env.wuying-dev         # fill keys (never commit)
cd backend && ./scripts/wuying_tunnel.sh                 # local forward to Action Server

# Run the backend (FastAPI, http://localhost:8080)
cd backend && uv sync && cd ..
make backend
```

In a new terminal, start the primary frontend:

```bash
cd frontend-v2
npm ci
npm run dev          # proxies /api and /ws to localhost:8080
```

Run the v2 quality gate before opening a pull request:

```bash
cd frontend-v2
npm run check          # i18n parity + ESLint + TypeScript + Vitest
npx playwright test    # E2E; requires the backend and a devtest account
```

The backend refuses Docker, Kubernetes and unknown sandbox providers at configuration and runtime boundaries. See [`docs/WUYING_SANDBOX.md`](docs/WUYING_SANDBOX.md) before deploying the Action Server.

---

## Documentation

Current implementation docs: [`AGENT_KERNEL_ARCHITECTURE.md`](docs/AGENT_KERNEL_ARCHITECTURE.md), [`WORKSPACE_NAMESPACING.md`](docs/WORKSPACE_NAMESPACING.md), [`WUYING_SANDBOX.md`](docs/WUYING_SANDBOX.md), [`PREVIEW_ORIGIN_ISOLATION.md`](docs/PREVIEW_ORIGIN_ISOLATION.md) and [`DeepSeek-Harness-vs-OpenBox-source-analysis.md`](docs/DeepSeek-Harness-vs-OpenBox-source-analysis.md).

---

## Note

Secrets and environment files are not committed. Configure model providers and WUYING credentials through ignored environment files and `openbox.json`; never expose Action Server credentials through browser-facing APIs.
