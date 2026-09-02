# WUYING Cloud Desktop as a Sandbox

`SANDBOX_PROVIDER=wuying` runs the sandbox execution plane on an Alibaba Cloud
WUYING cloud desktop (无影云电脑). It is the only supported Agent execution
provider. Docker Compose is limited to local PostgreSQL, Redis and Azurite; it
does not execute Agent tools. The control plane — agent loop, permissions,
event bus and API — still runs wherever you started the backend.

Use it when you want a persistent, full-fat Linux desktop as the agent's
workspace: it survives restarts, keeps installed tooling between sessions, and
can be attached to over the WUYING client to see what the agent did.

## Contents

- [Why the connection looks the way it does](#why-the-connection-looks-the-way-it-does)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Configuration](#configuration)
- [Daily use](#daily-use)
- [Operations](#operations)
- [Troubleshooting](#troubleshooting)
- [Why execution is WUYING-only](#why-execution-is-wuying-only)
- [Per-user desktops (WUYING_MODE=per_user)](#per-user-desktops-wuying_modeper_user)

---

## Why the connection looks the way it does

Two constraints shape the whole design, and neither is obvious:

**1. The desktop has no inbound route.** It sits on a WUYING-managed VPC with a
private address (`10.x.x.x`). There is no elastic IP, no port mapping, and no
security-group rule that will expose a port on it. Outbound works fine; inbound
does not exist. So the desktop has to *dial out* to somewhere the backend can
reach — hence a relay host and a reverse tunnel.

**2. A TUN-mode proxy on the developer machine eats non-standard ports.** Many
mainland-China setups run Clash/Surge-style transparent proxying, which hijacks
all TCP below the application layer. The symptom is confusing: `nc` reports a
port as open even when nothing is listening, and an HTTP request to a
non-standard port hangs and returns nothing. `curl --noproxy` does not help,
because the interception is at the routing layer, not in curl.

Loopback is the one address such proxies never intercept. That is why the
backend talks to `127.0.0.1:18000` and a local `ssh -L` carries it out — rather
than the backend connecting to the relay's public IP directly, which would be
one fewer hop but does not survive the proxy.

The same reasoning is why `SandboxClient` passes `trust_env=False`: without it,
httpx picks up `HTTP_PROXY` from the environment and routes sandbox traffic
through the developer's proxy, which fails with an opaque timeout.

> Even so, prefer the **local `ssh -L`** shape over exposing the relay port
> publicly. Nothing is reachable from the internet, so the action server's API
> key is defence in depth rather than the only thing standing between the
> sandbox and the world.

## Architecture

```
developer laptop                          Alibaba Cloud
┌───────────────────────────┐
│ frontend  :3000           │
│ backend   :8080           │
│   └─ WUYING_ENDPOINT      │
│      http://127.0.0.1:18000
│              │            │
│      ssh -L (wuying_tunnel.sh)
└──────────────┼────────────┘
               │  :22                    ┌──────────────────────────┐
               └────────────────────────►│ relay ECS (public IP)    │
                                         │   sshd, loopback :18000  │
                                         └────────────┬─────────────┘
                                                      │ reverse tunnel
                                                      │ (openbox-tunnel.service)
                                         ┌────────────┴─────────────┐
                                         │ WUYING desktop           │
                                         │   action_server   :8000  │
                                         │   dev-browser relay :9222│
                                         │   /workspace/sessions/…  │
                                         └──────────────────────────┘
```

The relay ECS forwards TCP and nothing else. It never holds an API key, never
sees decrypted traffic beyond what SSH carries, and its `authorized_keys` entry
for the desktop is scoped to a single forwarded port:

```
restrict,port-forwarding,permitlisten="18000" ssh-ed25519 AAAA…
```

`restrict` disables shell, agent forwarding, X11 and PTY allocation;
`port-forwarding` re-enables just the forward; `permitlisten` pins the port.

> **Gotcha:** `permitlisten="18000"` with no host means *loopback only*, and it
> silently overrides `GatewayPorts`. That is what we want here — the port must
> not be public — but if you ever do want a public bind you need
> `permitlisten="0.0.0.0:18000"` *and* `GatewayPorts clientspecified`.

## Prerequisites

| | |
|---|---|
| A WUYING desktop | Linux image. Ubuntu 22.04 is what this was built against. Note its `ecd-…` id. |
| A relay host | Any always-on host with a public IP and sshd that both the desktop and your laptop can reach. An ECS in the same account is the obvious choice. |
| `aliyun` CLI | Configured with credentials that can call `ecd` and (optionally) `ecs`. Check with `aliyun sts GetCallerIdentity`. |
| Outbound from the desktop | Package mirrors must be reachable. The bootstrap uses mainland mirrors throughout. |
| Docker on the developer machine | Builds the pinned Linux/amd64 Node 22 + HyperFrames + GSAP media bundle locally; the desktop never runs `npm install`. |
| OpenBox OSS configuration | Used as a short-lived internal transfer channel for the media bundle. The temporary object is deleted after installation. |

The bootstrap needs **no SSH credentials for either machine**: it drives the
desktop through `aliyun ecd run-command` and the relay through `aliyun ecs
RunCommand`, both of which execute as root over the cloud-assistant channel.

## Setup

### 1. Bootstrap the desktop

```bash
python backend/scripts/wuying_bootstrap.py \
    --desktop-id ecd-xxxxxxxxxxxxxxxxx \
    --relay root@<relay-public-ip> \
    --relay-instance i-xxxxxxxxxxxxxxxxx \
    --relay-region cn-hangzhou \
    --tunnel-port 18001
```

`--relay-region` is only needed when the relay ECS and desktop are in different
regions; otherwise it defaults to `--region`. Give every simultaneously active
desktop its own `--tunnel-port` so their reverse forwards cannot replace one
another on the relay.

Idempotent — re-run it to repair or upgrade a desktop. It will:

1. Create `/workspace`, `/data/skills`, `/data/mcp/logs` and the persistent and
   temporary media-job directories.
2. Upload `container/action_server.py`, `container/media_jobs.py`, the pinned
   media configuration, and the system-owned `video-production` skill under
   `/opt/openbox/skills`. Then install the Python dependencies from the
   Tsinghua PyPI mirror. The skill carries workflow instructions only; provider
   credentials remain in the backend environment.
3. Build the Linux/amd64 Node 22 + HyperFrames 0.7.94 + GSAP 3.14.2 bundle in
   local Docker, upload it as a temporary OSS object, let the desktop download
   it through the OSS internal endpoint, verify its SHA-256, and atomically
   install it under `/opt/openbox/media`. Both the local archive and temporary
   OSS object are removed afterwards.
4. Install the `dev-browser` relay and its npm dependencies. Skip with
   `--skip-dev-browser` if you do not need browser automation.
5. Generate an ed25519 key on the desktop and install
   `openbox-action-server.service` + `openbox-tunnel.service`.
6. Add the desktop's public key to the relay's `authorized_keys`, scoped as
   above. Without `--relay-instance` it prints the line for you to add by hand.

Files are shipped as gzip + base64 chunks because `run-command` caps its payload
at 16KB; `action_server.py` alone is ~85KB.

It prints a generated `SESSION_API_KEY` at the end. Keep it — the backend and the
desktop must agree on it.

The WUYING service also enables `OPENBOX_REQUIRE_USER_SCOPE=1`. Skill packages,
Skill exports, MCP configuration (including credentials), and MCP runtime
caches are then keyed by the backend's pseudonymous user scope. Requests to
those catalogue APIs without a valid scope are rejected rather than falling
back to the old shared `/data/skills` or `/data/mcp/config.json` state.

### 2. Configure the backend

```bash
# backend/.env
SANDBOX_PROVIDER=wuying
WUYING_ENDPOINT=http://127.0.0.1:18000
WUYING_API_KEY=<the key the bootstrap printed>
WUYING_DESKTOP_ID=ecd-xxxxxxxxxxxxxxxxx
```

### 3. Install the laptop's tunnel key

The laptop needs its own key on the relay, scoped to *opening* the forward
rather than *listening* on it:

```bash
ssh-keygen -t ed25519 -N "" -C openbox-mac -f ~/.ssh/openbox_wuying
```

Then add to the relay's `/root/.ssh/authorized_keys` — via `aliyun ecs
RunCommand` if you have no SSH access:

```
restrict,port-forwarding,permitopen="127.0.0.1:18000" ssh-ed25519 AAAA… openbox-mac
```

Note `permitopen` (limits `-L` destinations), not `permitlisten` (limits `-R`
bind addresses). The desktop and the laptop need different restrictions because
they use opposite halves of the tunnel.

### 4. Open the tunnel and verify

```bash
backend/scripts/wuying_tunnel.sh          # keeps reconnecting; leave it running
curl http://127.0.0.1:18000/alive
```

For the isolated acceptance desktop, use the explicit dev launcher instead.
It selects `.env.wuying-dev`, forces trusted single-user auth, uses tunnel port
`18001`, and starts the backend on the frontend-v2 proxy port `8080`:

```bash
backend/scripts/wuying_dev.sh tunnel
backend/scripts/wuying_dev.sh backend
```

A healthy response names the desktop:

```json
{"status":"ok","uptime":1319.09,"hostname":"0zd5sxxe1uw10r6","timestamp":"…"}
```

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `SANDBOX_PROVIDER` | `wuying` | The only supported Agent execution value. Any other value fails configuration/startup; Docker Compose is infrastructure-only. |
| `WUYING_ENDPOINT` | `http://127.0.0.1:18000` | Where the action server is reachable. Normally the local end of the tunnel. |
| `WUYING_API_KEY` | *(empty)* | Must equal `SESSION_API_KEY` on the desktop. The backend refuses to start this provider without it, and the Action Server itself refuses to start without `SESSION_API_KEY`. |
| `WUYING_DESKTOP_ID` | *(empty)* | `ecd-…`, informational — surfaced in logs and the container listing. |

On startup the provider calls `/alive` and logs the result. The process can
start while WUYING is temporarily unavailable so diagnostics remain reachable,
but `/ready` stays non-ready until the required Action Server version and
capabilities respond successfully.

## Daily use

Only the laptop-side forward needs starting by hand:

```bash
backend/scripts/wuying_tunnel.sh
```

Everything on the desktop is systemd and self-heals across reboots:

| Unit | Purpose |
|---|---|
| `openbox-action-server.service` | The execution plane on `:8000` |
| `openbox-tunnel.service` | Reverse tunnel to the relay |

The action server also owns the durable media queue. The build agent receives
the media tools from its fixed allowlist; loading `video-production` on demand
adds detailed workflow guidance only and never changes schema availability.
The queue's SQLite state lives in
`/data/openbox-media`, while per-attempt files and the reusable input cache live
under `/tmp/openbox-media`. Queue concurrency and FFmpeg threads come from
`container/media-jobs.json` (defaults: one render and four FFmpeg threads).
Linear spoken-video concatenation uses the pure-FFmpeg fast path; HyperFrames
and Chrome start only when the request explicitly selects the HTML-animation
engine.

### MCP connection supervisor (v10)

MCP connections are tenant-scoped and persistent. For each enabled server the
Action Server starts one owner task; that same task opens, initializes, uses,
and closes the stdio, SSE, or raw Streamable HTTP transport. Tool calls,
resource reads, prompt reads, and manual refreshes enter a bounded per-server
queue instead of spawning a new MCP subprocess or session for every request.
An explicit disconnect or delete first persists the disabled/removed desired
state, then stops and awaits the owner, so a slow connect or refresh cannot
resurrect the server. Startup restores enabled owners and shutdown awaits all
of them.

Discovery follows every pagination cursor for tools, resources, and prompts,
builds a detached temporary generation, and publishes all three catalogues in
one atomic swap with one revision increment. A failed refresh retains the
last-known-good generation and exposes the failure in `GET /mcp/servers`.
Servers that advertise MCP `listChanged` use SDK callbacks or an authenticated
Streamable HTTP GET/SSE receiver. If the transport cannot receive those
notifications, that server is reported as `poll` and receives coalesced full
refreshes; capabilities that the server did not advertise are reported as
`unsupported`. The raw notification GET carries both configured credentials
and the negotiated `Mcp-Session-Id`, as does the initialized notification.

`GET /alive` reports version `2026.08.31-run-lease-receipt-v12` and capabilities
`mcp_supervisor_v1`, `terminal_project_cwd_v1`, and `run_lease_receipt_v2`;
all earlier capabilities remain present. Agent requests carry a signed database
lease expiry in addition to the durable generation high-water mark, so an old
worker cannot make its first late desktop request after its PostgreSQL lease has
expired. The Backend requires `run_lease_receipt_v2` before sending an Agent
side effect, so an older Action Server fails closed instead of ignoring the new
headers. Lease settlement revokes transport locally before committing idle.
This boundary assumes the PostgreSQL host and desktop clocks are synchronized;
an already accepted command is not retroactively killed when its receipt later
expires. Terminal sessions start in the authenticated project directory and use
the same pseudonymous tenant scope as Agent tools.

Headless Agent commands use Bash with `--noprofile --norc`; desktop-wide login
hooks are intentionally not sourced. This prevents the WUYING image's GNOME
`gsettings` profile hook from emitting dconf/DBus warnings into every command
while keeping the explicit UTF-8 locale and safe PATH. Closing an
`/execute_stream` response kills and reaps that command's process group. The
Bash tool also watches the owning Agent abort signal, so pressing Stop closes
the stream immediately instead of waiting for the idle judge or hard timeout.

Recommended values for the current 4-core, 8-GB-class desktop are:

| Media setting | Default | Purpose |
|---|---:|---|
| `render_engine` | `auto` | Select FFmpeg for the current linear timeline; `hyperframes` is an explicit opt-in. |
| `max_concurrency` | `1` | Other sessions remain queued and receive a queue position. |
| `output_fps` | `24` | Avoids rendering unused 30-fps frames for normal speech. |
| `ffmpeg_threads` | `4` | Uses the available CPU cores on the fast path. |
| `ffmpeg_preset` / `ffmpeg_crf` | `veryfast` / `21` | Production-speed H.264 with good short-video quality. |
| `hyperframes_workers` | `1` | Prevents multiple Chrome renderers from exhausting RAM. |
| `hyperframes_low_memory_mode` | `true` | Forces the safe low-memory profile. |
| `hyperframes_video_frame_format` | `jpg` | Appropriate for camera footage; use PNG for UI/screen recordings. |

The **dev-browser relay is not** a systemd unit. It is started on demand by
`POST /dev-browser/start` (the *Enable Dev Browser* button), and does not come
back on its own after a desktop reboot — press the button again.

## Operations

```bash
# Desktop service state
aliyun ecd run-command --api-version 2020-09-30 \
  --region cn-hangzhou --biz-region-id cn-hangzhou \
  --desktop-id "$WUYING_DESKTOP_ID" --type RunShellScript \
  --command-content 'systemctl status openbox-action-server openbox-tunnel --no-pager'

# Is the reverse tunnel actually bound on the relay?
aliyun ecs RunCommand --RegionId cn-hangzhou --InstanceId.1 <relay-instance> \
  --Type RunShellScript --CommandContent 'ss -lntp | grep 18000'

# Authenticated media queue status through the laptop tunnel
curl -H "X-API-Key: $WUYING_API_KEY" \
  http://127.0.0.1:18000/media/jobs/status
```

Expect `127.0.0.1:18000` on the relay. `0.0.0.0:18000` means the port is exposed
publicly — check `permitlisten` and `GatewayPorts`.

To rotate the API key, set it on both sides and restart:

```bash
python backend/scripts/wuying_bootstrap.py --desktop-id … --relay … --api-key <new>
# then update WUYING_API_KEY in backend/.env and restart the backend
```

To refresh only the action server and media runtime, use the narrow deploy
script. It reuses an already healthy pinned runtime by default; pass
`--force-media-bundle` when the lockfile or Node bundle changed:

```bash
OPENBOX_ENV_FILE=.env.wuying-dev \
OPENBOX_BASE_ENV_FILE=.env \
  python backend/scripts/wuying_deploy_action_server.py \
    --desktop-id ecd-<development-desktop> \
    --force-media-bundle
```

The deploy command rejects an explicit `--desktop-id` that differs from
`WUYING_DESKTOP_ID` in the selected profile. This prevents a valid key for one
desktop from being installed on another desktop by mistake.

## Troubleshooting

**`WUYING sandbox unreachable at …` on startup**
The laptop-side forward is down. Start `wuying_tunnel.sh`. If it exits
immediately, the relay rejected the key — check `permitopen` in its
`authorized_keys`.

**`/alive` hangs and returns nothing, but `nc` says the port is open**
A TUN-mode proxy is intercepting. Confirm by connecting to a port where nothing
listens; if that also "succeeds", everything is being hijacked. Use the loopback
tunnel rather than the relay's public IP, and make sure nothing has reintroduced
`trust_env=True` on a sandbox HTTP client.

**Every request comes back 403**
`WUYING_API_KEY` does not match `SESSION_API_KEY` in
`openbox-action-server.service`. Select the intended profile explicitly and
re-run the narrow deploy command above; never reuse a different desktop's
profile merely because its API key is valid.

**Tunnel is up, relay listens, but the backend still cannot reach it**
Check which address the relay bound. `permitlisten` with no host restricts the
bind to loopback, which is correct for this layout but will not serve a backend
connecting from elsewhere.

**`dev-browser not installed in container` (HTTP 500)**
The desktop was bootstrapped with `--skip-dev-browser`, or the npm install
failed. Re-run the bootstrap without that flag.

**Sandbox reachable but browser automation never connects**
`GET /dev-browser/status` should report `{"status":"running","extensionConnected":true}`.
`stopped` means nobody pressed *Enable Dev Browser* (or the desktop rebooted).
`extensionConnected: false` points at the Chrome extension — check its Server URL
under *Advanced* in the popup.

## Why execution is WUYING-only

The Docker and Kubernetes provider implementations and their runtime
dependencies have been removed. The product configuration and provider factory
accept only `wuying`; missing or misspelled configuration fails closed rather
than silently starting an execution environment on the backend host.

The desktop is provisioned out of band, reached through the two-hop tunnel and
persists installed packages and files across backend restarts. `create` is an
idempotent control-plane projection; `delete` and `stop` are no-ops because
OpenBox does not own the cloud desktop lifecycle.

The no-op `delete_container` matters: `SandboxManager.release()` destroys a
container once its last session ends. Left alone, that would try to delete
someone's cloud desktop.

The shared-desktop model also means **there is no hard isolation boundary
between users**. File APIs constrain paths to the claimed project workspace,
but arbitrary shell execution uses one shared `sandbox` Unix identity and can
inspect the global workspace. Treat this topology as single-user/trusted
acceptance only; public SaaS requires one desktop (or equivalent OS boundary)
per user — or switch to per-user desktops, below.

## Per-user desktops (WUYING_MODE=per_user)

`WUYING_MODE=per_user` closes the single-tenant gap: OpenBox provisions **one
ECD desktop per user** through the ECD OpenAPI, with a dedicated convenience
EndUser per user, instead of pointing everyone at the shared desktop. The
implementation is ported from bossip's wuying-bridge and keeps its hard-won
behaviours (EndUser sync wait before CreateDesktops, tag reads through
ListTagResources, ghost-desktop hard-delete, environment tagging).

**How it works**

- Identity: each user id derives a stable EndUser (`obx-<sha256[:16]>`) and a
  salted password (`WUYING_PASSWORD_SALT`). Display names never feed the id.
- Ownership: desktops carry `openbox-user` / `openbox-eu-id` / `openbox-env`
  tags. The ticket API verifies the tag before minting a ticket, so one user
  cannot view another's desktop. `openbox-env` keeps prod and dev sharing one
  Alibaba Cloud account from adopting or reaping each other's desktops.
- State: the `cloud_desktops` table records each user's desktop
  (`backend/db/models/cloud_desktop.py`); a unique partial index enforces one
  live desktop per user. If the DB forgets a desktop, it is re-adopted by tag.
- Flow: the 云桌面 tab shows a provisioning opt-in for users without a
  desktop; `POST /api/desktop/provision` creates (2-3 min) or wakes it, the
  frontend polls `GET /api/desktop/status`, and the ticket API rides the same
  202 retry channel while the desktop is creating/starting.

**Extra configuration** (see `.env.example`): `WUYING_IMAGE_ID` (golden image
— required; there is deliberately no fallback to a community image),
`WUYING_OFFICE_SITE_ID`, `WUYING_PASSWORD_SALT`, and optionally
`WUYING_DESKTOP_TYPE` / `WUYING_SYSTEM_DISK_SIZE` / `WUYING_POLICY_GROUP_ID` /
`WUYING_CHARGE_TYPE` / `WUYING_ENV_TAG`. Build the golden image by
bootstrapping one desktop with `scripts/wuying_bootstrap.py` and imaging it
from the ECD console.

**Testing** — `scripts/wuying_provision_smoke.py` exercises the chain in three
tiers: `check` (read-only: lists office sites, images, OpenBox desktops),
`enduser` (free: real EndUser create → sync → remove), and `full` (billable:
provisions a real desktop through the same service the API uses, waits for
Running, mints a connection ticket, then deletes everything; `--yes` required,
`--disk` must cover the image size). Unit coverage lives in
`tests/unit/test_wuying_provisioning.py` with the ECD calls stubbed.

**Not yet wired**: the sandbox execution plane (action server) still uses the
single `WUYING_ENDPOINT` tunnel — per-desktop connectivity (frpc reverse
tunnels or per-desktop SSH) is the next step. Until then, per_user mode gives
each user their own *viewable* desktop while command execution stays on the
shared one.

That gap is now enforced rather than merely written down. Production was
switched to `WUYING_MODE=per_user` on 2026-09-01 anyway, and for the next
half-day the cloud-desktop tab streamed each caller's own fresh desktop while
their agent kept working on the shared one. Nothing failed: the agent reported
"opened Baidu" truthfully, and Baidu really was open — on a machine the person
could not see.

So `api/desktop._per_user()` now requires two things, not one: the deployment
asked for per-user desktops **and** the sandbox provider says it routes per
user (`SandboxProvider.routes_per_user`). `WuyingProvider` declares `False`.
With only the config half, the view falls back to the shared desktop and logs
an ERROR, keeping the property that matters: *what you watch is where it runs*.
When per-desktop connectivity lands, flip that flag and per_user works end to
end; no other change is needed.

Startup now logs both planes on one line, e.g.

```
Cloud desktop — agent runs on: ecd-4zjxaq5g45dr5qr0i;
                view streams: ecd-4zjxaq5g45dr5qr0i in cn-shanghai
```

### The region has to match the desktop

`WUYING_REGION_ID` is not cosmetic: `GetConnectionTicket` is a regional call,
so a desktop id that is perfectly real in another region comes back as
`NotFindDesktopId` — which reads like a deleted desktop and sends you looking
in the wrong place. Production had `cn-hangzhou` while the shared desktop
(`ecd-4zjxaq5g45dr5qr0i`, `bossip-sh-007`) lives in `cn-shanghai`, so the tab
could not have worked in shared mode at all. It is `cn-shanghai` now.

`WUYING_OFFICE_SITE_ID` is regional in the same way and is only read when
creating per-user desktops; if per_user is ever completed, that value has to
belong to `WUYING_REGION_ID`'s directory too.
