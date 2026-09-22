# OpenBox

English | [中文](README.zh-CN.md)

OpenBox is an AI Agent platform for research, file and code work, desktop automation,
media production and team collaboration. Its Python backend manages model calls,
permissions and durable execution; the Web workbench shows conversations, tool calls,
cloud desktops and delivered results.

[Documentation](docs/README.md) · [Capability architecture](docs/architecture/AGENT_CAPABILITIES.md) ·
[Tool catalogue](docs/reference/TOOLS.md) · [Skills](docs/reference/SKILLS.md) ·
[Development guide](docs/contributing/ADDING_CAPABILITIES.md)

## Capabilities

- **Agent runtime:** model/provider adaptation, context compaction, durable session history,
  user interruption, pending questions and recovery.
- **51 builtin tools in 11 domains:** files and execution, web, desktop, skills and memory,
  planning, Agents and teams, user interaction, scheduling, media, marketing, and discovery.
- **Skills and instructions:** eight builtin skills in six manifest-defined groups, scoped project/personal/sandbox
  skill providers, optional scripts and references, project instructions and command templates.
  Builtin and user-added skills support Chinese/English display names and summaries while keeping stable invocation identifiers.
- **Reusable Agents and teams:** model/tool/skill configurations, versioned definitions,
  delegation, task dependencies, messages, deliverables and a final response. Team spending
  uses the user's account credit ledger.
  Every reusable Agent includes ten core tools; explicitly selected skills automatically include and lock their required tools and MCP services.
- **External integrations:** trusted platform plugins, scoped MCP tools/resources and OAuth.
  Installation, authorization and current availability remain separate concerns.
- **Web workbench:** streaming chat, tool and thinking traces, questions, plans, todos,
  file changes, terminal, browser and cloud-desktop panels.

The runtime, tools, skills, business services and execution environments have separate
responsibilities. A skill teaches a workflow; loading it does not grant tool permissions.
The tool catalogue describes implementations, not what every Agent is authorized to use.

## Architecture and execution boundaries

```text
Web / Mobile clients
        |
FastAPI: authentication, workspace scope, sessions and Agent definitions
        |
Agent runtime: models, context, tool selection, scheduling and recovery
        |
Tools by functional domain ---- Skills, instructions and scoped resources
        |
Business services / MCP & plugins / SandboxClient
        |
WUYING desktop & browser / external providers / object storage
```

| Component | Boundary |
|---|---|
| Agent loop, permissions and business services | Backend control plane |
| File reads/writes, searches and shell commands | Configured sandbox / Action Server, normally WUYING in production |
| Desktop and browser | Cloud desktop or the user's connected browser, depending on the operation and configured mode |
| Skill content | Loaded from its scoped provider; scripts/actions use the corresponding execution tools |
| MCP | Executed through its configured connection and adapter; not assumed to run on the backend host |
| Media and file delivery | Provider, asset and object-storage services; some operations also require the sandbox |

WUYING supports a shared development configuration and per-user desktop provisioning.
Sessions have project working directories; they do **not** universally own separate desktops.
Docker and Kubernetes providers remain in the codebase. A desktop outage should block
operations that need that desktop while ordinary conversation can continue. See the
[capability architecture](docs/architecture/AGENT_CAPABILITIES.md) and
[WUYING guide](docs/operations/WUYING_SANDBOX.md) for the actual boundaries.

## Project structure

```text
backend/
  agent/ · session/             Agent runtime, model adaptation and history
  tool/                         Domain tool entry points and shared protocol
    workspace/ · web/ · desktop/ · knowledge/ · planning/
    collaboration/ · interaction/ · automation/ · media/ · marketing/ · discovery/
    integrations/               Dynamic MCP and platform-plugin adapters
    catalog.py · registry.py · tool.py · truncation.py
  skill/ · skill/builtins/      Skill services and classified builtin packages
  agent_catalog/ · team/        Reusable definitions and durable collaboration
  command/ · memory/            Command templates and user memory
  sandbox/ · mcp/               Execution environments and external connections
  video/ · publish/ · trends/ · autopilot/ · platforms/
  cron/ · question/ · notifications/
  api/ · auth/ · permission/ · billing/ · db/ · trajectory/
frontend-v2/                    Primary Web UI
frontend/                       Legacy Web UI, retained for migration reference
mobile/                         Mobile client
container/ · extension/         Action Server, browser runtime and browser extension
k8s/                            Legacy deployment manifests
docs/                           Indexed architecture, references, operations and evidence
```

The main stack is Python 3.12, FastAPI, Pydantic AI, LiteLLM, PostgreSQL and Redis;
the primary Web UI uses React, TypeScript, Vite, Tailwind CSS, Zustand and TanStack Query.
Media transfer uses the configured object-storage services, including OSS. See
[deployment](docs/operations/DEPLOY.md) for environment-specific infrastructure.

## Local development

From the repository root, create configuration files if they do not already exist:

```bash
cp -n backend/openbox.jsonc.example backend/openbox.json
cp -n backend/.env.example backend/.env
make deps
```

Configure your model routes, database and execution environment in those files. The
backend entrypoint loads `backend/.env`; a differently named environment file is not selected
just because it exists. See [development commands](docs/contributing/DEV_COMMANDS.md) and the
[WUYING guide](docs/operations/WUYING_SANDBOX.md) for configuration and tunnel setup.

Start the backend in one terminal:

```bash
cd backend
uv sync --extra test
cd ..
make backend
```

Start the Web UI in another:

```bash
cd frontend-v2
npm ci
npm run dev -- --port 3000
```

The backend listens on `8080`; the Web UI on `3000` proxies `/api` and `/ws` to it.
`make backend` applies migrations before serving. Development dependencies are described in
`docker-compose.dev.yml`; local configuration can override their ports and addresses.

## Validation

```bash
cd backend
uv run python -m tool.catalog
uv run pytest tests/unit -q
```

For Web changes:

```bash
cd frontend-v2
npm run check
```

Browser E2E requires its configured backend and test account; see
[frontend-v2/README.md](frontend-v2/README.md). Integration tests that use real external
services require their own test configuration. Tool grouping does not require paid model or
media calls. Never commit credentials or environment files.

## Documentation

Component guides: [Backend](backend/README.md), [Web](frontend-v2/README.md), [Mobile](mobile/README.md), [Execution environment](container/README.md), [Browser extension](extension/README.md), [Deployment](deploy/README.md). See [Contributing](CONTRIBUTING.md) for development and documentation maintenance.

Start with the [documentation index](docs/README.md). It separates current references and
operations from design proposals, implementation records and historical evidence.

- [Agent capability boundaries](docs/architecture/AGENT_CAPABILITIES.md)
- [Tools and dependencies](docs/reference/TOOLS.md)
- [Skills, instructions and resources](docs/reference/SKILLS.md)
- [Adding capabilities](docs/contributing/ADDING_CAPABILITIES.md)
- [Agent kernel](docs/architecture/AGENT_KERNEL_ARCHITECTURE.md)
- [Team API](docs/reference/AGENT_TEAM_API_HANDOFF.md) and [operations](docs/operations/AGENT_TEAM_OPERATIONS.md)
- [Local commands](docs/contributing/DEV_COMMANDS.md), [deployment](docs/operations/DEPLOY.md) and [SSO](docs/operations/LOGTO_PROD.md)
