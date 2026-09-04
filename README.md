# OpenBox

English | [中文](README.zh-CN.md)

**An AI Agent execution platform** — give an LLM a safe, isolated sandbox to read, write, run code, and drive a browser, with production-grade orchestration, context management, and multi-tenant isolation.

> A general-purpose agent runtime (inspired by OpenCode/Claude Code), rewritten in Python around **Pydantic AI + LiteLLM**, with every file/command operation confined to a per-session **WUYING cloud-desktop sandbox** (the Docker / Kubernetes providers are retained but are no longer the production path). Deployed on AWS (dev) and Alibaba Cloud (prod) — see [docs/DEPLOY.md](docs/DEPLOY.md).

> **Frontend direction:** [`frontend-v2/`](frontend-v2/) is the primary and actively developed OpenBox web UI. The original [`frontend/`](frontend/) is retained only as a legacy migration reference.

---

## At a glance

| | |
|---|---|
| **What it is** | A full-stack platform where an AI agent autonomously executes development tasks (edit code, run bash, git, browse) inside an isolated container, with the v2 web UI showing every tool call in real time. |
| **Core stack** | FastAPI · Pydantic AI · LiteLLM (100+ models) · WUYING cloud-desktop sandbox · PostgreSQL · Redis · React 19 |
| **Agent loop** | Pydantic AI single-turn tool calls wrapped by a custom outer loop: multi-turn orchestration, permission checks, retries, context compaction |
| **Isolation** | Each session owns a dedicated sandbox container; user file/command tools run inside it, control-plane logic runs on the host |
| **Scale** | Docker locally, WUYING cloud desktops in production; multi-tenant (workspace / project / permission inheritance) |

---

## Why OpenBox is different

Most "let an LLM run code" demos break the moment they hit production. OpenBox treats the agent as a *system*, not a prompt:

| Concern | Typical agent demo | OpenBox |
|---|---|---|
| **Safety** | LLM runs commands on the host | Every `bash`/`read`/`write`/`edit`/`glob`/`grep` runs **inside a per-session sandbox**; host only does control-plane work |
| **Context overflow** | Conversation grows until it blows the window | **Automatic context compaction** (summarize history on overflow) + tool-output truncation + prompt cache |
| **Reliability** | One bad tool call kills the run | Custom outer loop with per-tool retry, permission gating, and graceful degradation |
| **Multi-user** | Single shared process | Workspace/project isolation, per-session container, Logto OIDC (enterprise SSO), credential boundary |
| **Auditability** | Opaque chat history | Real-time event stream (SSE + WebSocket), tool-execution visualization, session branch/rollback (git-like history) |

---

## Architecture

```
┌─────────────────────────────────────────────┐
│  OpenBox API (FastAPI, host)                  │
│  Agent orchestration · permission · skill ·   │
│  MCP · session · cron · event bus             │
├──────────────────┬────────────────────────────┤
│  Pydantic AI      │   Sandbox (per session)     │
│  tool-call loop   │   bash / read / write /     │
│  + LiteLLM        │   edit / glob / grep ...     │
└──────────────────┴────────────────────────────┘
                        │
                 Docker container     (local)
                 WUYING cloud desktop (production)
```

### Execution boundary (host vs sandbox)

| Tool / module | Runs in | Why |
|---|---|---|
| `bash`, `read`, `write`, `edit`, `apply_patch`, `glob`, `grep` | **Sandbox** | File/command ops must be isolated |
| MCP tool calls | Host | MCP servers are separate processes |
| Skill load (read `SKILL.md`) | Host | Config read, no risk |
| Skill execution (LLM acting) | **Sandbox** | Real actions go through `bash`/`write` |
| `web_fetch`, `web_search` | Host | Network requests |
| Plugin code + hooks | Host | Auth, arg mutation, host logic |
| Agent orchestration / permission / event bus | Host | Control plane |

---

## Key capabilities

- **Agent loop** (`backend/agent/`): `loop.py` outer orchestration, `compaction.py` auto context summarization, `caching.py` prompt cache, `retry.py` resilient retries, `hooks.py` lifecycle hooks.
- **Sandbox manager** (`backend/sandbox/`): `wuying.py` (production provider), `docker.py` / `kubernetes.py` (legacy providers), `manager.py` lifecycle (create on session start, destroy on end).
- **22+ built-in tools**: bash, read, write, edit, glob, grep, mcp, skill, web_fetch, web_search, question, todo, plan, batch, …
- **Fine-grained permissions** (`backend/permission/`): per-tool approval flow with interactive user confirmation.
- **Three-tier context/memory**: in-memory current turn → DB-persisted compacted history → long-term instruction files.
- **Cron agents** (`backend/cron/`): scheduled autonomous agent runs.
- **Session branch / rollback**: git-like session history management.
- **Frontend v2 workbench** (`frontend-v2/`): streaming chat, tool/thinking traces, permission/question/plan/todo cards, diff review, PTY terminal, browser, desktop and file panels.
- **Product-grade UI foundation**: Chinese/English localization, eight theme families, light/dark modes, four font sizes, accessible interactions and responsive layouts.

---

## Tech stack

**Backend** (Python 3.12)
- FastAPI + Uvicorn · **Pydantic AI** (agent loop) · **LiteLLM** (100+ providers)
- PostgreSQL (SQLAlchemy async + Alembic) · Redis (session / ticket / context cache)
- Docker SDK + Kubernetes client (sandbox) · Azure Blob Storage (user files)
- JWT + Logto OIDC (enterprise SSO)

**Frontend v2** (React 19)
- Vite 8 + TypeScript 6 · Tailwind CSS 4 semantic tokens
- Zustand 5 + TanStack Query 5 · React Router 8 · i18next
- xterm.js 6 (PTY) · Vitest + Testing Library · Playwright

**Infrastructure**
- AWS EC2 (dev) + Alibaba Cloud ECS (prod), both running Docker Compose · Docker Compose (local dependencies) · Makefile workflow · Python/Node monorepo

---

## Project structure

```
OpenBox/
├── backend/
│   ├── agent/        # agent loop, compaction, caching, retry, hooks
│   ├── sandbox/      # docker.py + kubernetes.py providers, manager
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
├── container/        # sandbox image (action_server)
├── k8s/              # legacy GKE/AKS manifests (frozen — not the production path)
├── docs/             # architecture & design docs
└── docker-compose.yml   # local dev only; the production compose lives on the servers (docs/DEPLOY.md)
```

---

## Quick start (local)

```bash
# Local dependencies (PostgreSQL + Redis + Azurite)
make deps

# Configure and run the backend (FastAPI, http://localhost:8080)
cp backend/openbox.jsonc.example backend/openbox.json   # configure models/providers
cp backend/.env.example backend/.env                    # fill keys (never commit)
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

The v2 production image is defined in `frontend-v2/Dockerfile`. How that image is built, shipped and released to the AWS dev host and the Alibaba Cloud prod host is documented in [docs/DEPLOY.md](docs/DEPLOY.md). The GKE/AKS manifests in `k8s/` are frozen legacy.

---

## Documentation

Design docs in [`docs/`](docs/): `OPENAGENT_DESIGN.md` (agent architecture), `FRONTEND_DESIGN.md`, `API_INTERFACES.md`, `MULTI_USER_STORAGE_PLAN.md`, `CRON_SYSTEM_PLAN.md`, `PTY_UPGRADE_PLAN.md`, `PERFORMANCE_OPTIMIZATION.md`, [`DEPLOY.md`](docs/DEPLOY.md) (AWS dev + Alibaba Cloud prod deployment), [`LOGTO_PROD.md`](docs/LOGTO_PROD.md) (Logto SSO per environment), [`WUYING_SANDBOX.md`](docs/WUYING_SANDBOX.md) (running the sandbox on an Alibaba Cloud desktop).

---

## Note

This is a sanitized public copy: secrets and environment files have been removed; sandbox image internals and a vendored agent framework are excluded. Configure your own model providers and credentials via `.env` / `openbox.json`.
