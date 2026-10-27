# WUYING Cloud Desktop as a Sandbox

`SANDBOX_PROVIDER=wuying` runs the sandbox execution plane on an Alibaba Cloud
WUYING cloud desktop (无影云电脑) instead of a local Docker container or a
Kubernetes pod. The control plane — agent loop, permissions, event bus, the API
— still runs wherever you started the backend.

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
- [现有无影云组件更新：视频准备与交互延迟修复](#wuying-latency-rollout)
- [Troubleshooting](#troubleshooting)
- [What differs from the Docker and Kubernetes providers](#what-differs-from-the-docker-and-kubernetes-providers)
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

A healthy response names the desktop:

```json
{"status":"ok","uptime":1319.09,"hostname":"0zd5sxxe1uw10r6","timestamp":"…"}
```

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `SANDBOX_PROVIDER` | `docker` | Set to `wuying`. |
| `WUYING_ENDPOINT` | `http://127.0.0.1:18000` | Where the action server is reachable. Normally the local end of the tunnel. |
| `WUYING_API_KEY` | *(empty)* | Must equal `SESSION_API_KEY` on the desktop. Empty means every request is rejected with 403. |
| `WUYING_DESKTOP_ID` | *(empty)* | `ecd-…`, informational — surfaced in logs and the container listing. |

On startup the provider calls `/alive` and logs the result. A failure is logged
loudly but does **not** abort startup, so the backend still comes up with a dead
sandbox rather than refusing to boot.

## Daily use

### Local forwarding through Alibaba Session Manager

When SSH to the relay is unavailable, `ali-instance-cli portforward` can reach
the relay's private reverse-forward port. Its process can remain running after
the cached WebSocket URL expires: new requests then fail with
`websocket: bad handshake` / `Server disconnected without sending a response`.
A listening local port alone does not prove the desktop is reachable.
[StartTerminalSession documents the ten-minute URL lifetime](https://help.aliyun.com/zh/ecs/developer-reference/api-ecs-2014-05-26-startterminalsession).

Use `backend/scripts/wuying_session_tunnel.py` around the CLI for a persistent
development forward. The wrapper needs only Python 3.12's standard library:

```bash
python backend/scripts/wuying_session_tunnel.py --port 18002 -- \
  /path/to/ali-instance-cli portforward --profile default --region cn-shanghai \
  -i i-RELAY_INSTANCE -l '{port}' -r 172.17.0.1:DESKTOP_REVERSE_PORT
```

Every eight minutes it starts a new CLI process on a private ephemeral port,
checks the desktop's `/alive`, and switches new connections to it. In-flight
connections drain on the previous process before that process exits. A failed
candidate never replaces a working route; a thirty-second end-to-end probe also
detects a dead process or broken route. Requests containing commands are never
replayed after a disconnect because their execution outcome may be unknown.

For Docker, mount the script, Linux CLI and existing Aliyun configuration read
only, use `python:3.12-alpine`, and pass `--host 0.0.0.0` to the wrapper. Publish
only `127.0.0.1:18002:18002`, leaving the CLI's ephemeral ports unexposed. Keep
the image read only, provide writable tmpfs mounts at `/tmp`, `/opt/log` and
`/root/.aliyun`, and use `--restart unless-stopped`. No desktop, relay, API key,
or remote service configuration change is needed for this repair.

Validation on 2026-09-18: seven regression tests cover session renewal, a dead
forwarder, failed candidates, draining in-flight responses, shutdown, and no
replay after an uncertain command result. Forty related tool/runtime/Wuying
tests also passed. With a temporarily shortened twenty-second renewal interval,
a real forty-second desktop command completed across two renewals while eight
fresh connections all succeeded. The browser's actual `bash` tool returned
`exit 0` and the expected desktop hostname. The running development forward was
then set to the normal eight-minute renewal interval.

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

The **dev-browser relay is not** a systemd unit. The backend starts it on demand
when a browser skill loads, and verifies live CDP/relay endpoints rather than
trusting an in-memory cache after reboot. `openbox-browser-runtime.service`
checks/repairs the pinned runtime before the action server starts. Channel
activation and pool acceptance run the same installer; a failed repair remains
retryable on the same desktop and cannot report a successful activation.

Runtime `20260907.3` also requires an action-service task budget of at least
2048 (threads count toward `TasksMax`). The inherited 512 limit caused real
`pthread_create` failures and CDP page-init hangs when several browser processes
coexisted. The installer persists a dedicated resource drop-in and raises a
running service's limit in place, without a restart or changing `MemoryMax`.
Existing higher/unlimited operator limits are preserved. Read-only checks verify
the persistent setting and, where visible, the live cgroup limit. Raising a cap
cannot recover every renderer already damaged by thread-creation failures;
such a browser may need a separately approved, backed-up restart.
The resource drop-in is named `zz-openbox-browser-resources.conf` so legacy
`media.conf` cannot restore the old cap when systemd reloads its configuration.
The existing media/memory configuration is retained, and overwritten property
files are backed up before an in-place `systemctl set-property` update.

An unconnected Wuying desktop may have no X session yet. In that case browser
automation uses a sandboxed, unprivileged headless Chrome with a fresh private
profile under `/var/lib/openbox/browser`. It does not copy image/user profiles,
create a competing X display, or require a manual sidebar connection. The API
reports `presentation: headless`, and browser/computer tool messages explicitly
state that this browser is not visible in desktop screenshots. Once a browser
is running, it is reused; connecting the Web SDK never force-closes a headless
session to switch presentation. Visible desktop availability is a separate check.

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

For a reviewed, full Action Server module update, the existing deploy script
supports a retained-backup mode. This updates the five service modules, but
does **not** synchronize the video-production skill:

```bash
backend/.venv/bin/python backend/scripts/wuying_deploy_action_server.py \
  --env-file backend/.env.wuying-dev --action-server-only --dry-run
```

Remove `--dry-run` only for the approved desktop and maintenance window. Use
`--no-restart` to stage the modules before restarting the service. The default
mode without `--action-server-only` also copies the video skill and may install
media packages; it has a different scope and does not provide the same retained
module backup workflow. `--force-media-bundle` is not a supported option in the
current script. For the latency/video preparation release, use the bounded
procedure below instead of re-running bootstrap or the default deploy mode.

<a id="wuying-latency-rollout"></a>

### 现有无影云组件更新：视频准备与交互延迟修复（2026-10-28）

本节对应 `82927482`、`3209dfe6` 的更新操作，**是发布手册，不是生产已发布记录**。
截至编写时，本轮只在 `wuying-dev` 真机验证过；生产桌面需要单独盘点和发布。
命令均从仓库根目录执行，使用已配置阿里云 CLI 的运维机器。

#### 1. 更新范围与依赖

| 位置 | 更新内容 | 生效方式 |
|---|---|---|
| Web 后端 | `browser_action`、减少请求重复准备、shell 空闲判断与输出接收并行、移除估价 | 发布后端；单独合并 Git 不会更新线上进程 |
| 每台旧版桌面的 `/opt/action_server/action_server.py` | 按块接收输出、退出后排空输出、返回 `output_complete` | 下发流式输出补丁，重启 `openbox-action-server` |
| 每台旧版桌面的 `/opt/openbox/skills/video-production/` | `prepare.py`、配套脚本、技能指引和参考文件，共 10 个固定文件 | 按第 4 步同步，并校验源文件 SHA-256 |
| 新建桌面的镜像/初始化资源、已有预热池 | 与上述组件保持一致 | 更新模板或供应流程；预热桌面验收后才可分配 |

`browser_action` 只在 shell 退出码为 0 **且** `output_complete=true` 时继续读取截图。
旧服务缺少该字段，可能已经完成浏览器动作，却不能通过结果完整性检查。因此要先更新
桌面组件并验收，再发布使用新流程的后端；不能先全量启用新工具、再慢慢补桌面。

本次没有提升 `ACTION_SERVER_VERSION` 或浏览器 `RUNTIME_VERSION`。只看 `/alive`
版本号不能判断补丁是否存在，必须检查文件/函数摘要和实际 SSE 回执。现有浏览器自检也
不会自动下发 Action Server 补丁或视频脚本。旧 `dev-browser/SKILL.md` 的路由提示由
新后端补充，本次不要求升级 Chrome、Node 或整个浏览器运行时；不要直接改该目录中的
单个文件，否则可能与 `dev-browser-sources.json` 不一致。

#### 2. 冻结目标、等待空闲、准备发布记录

1. 从生产桌面绑定和桌面池盘点目标，固定每台的 `desktop_id`、区域、归属、池状态和
   当前组件摘要。只处理已确认的目标，不把测试配置当作生产配置，也不按旧文档中的
   历史桌面数量决定本次范围。
2. 先测试桌面，再少量空闲用户桌面，最后分批覆盖其余用户桌面和预热池。离线桌面记录为
   待更新，在下次恢复并通过验收前不得进入新流程。当前没有通用的自动版本准入开关，
   发布方必须安排维护窗口或先补齐准入机制，不能假定已有自动保护。
3. 阻止目标桌面接收新任务，等待该桌面的 Agent run、shell、浏览器/桌面操作、媒体任务
   和资源操作租约结束；Web 页面对话空闲或 `/media/jobs/status` 为空，单独都不足以证明
   整台桌面已空闲。保持这个门禁直到文件校验和服务验收完成。
4. 固定本次源码提交并记录哈希；不要在上传过程中切换分支或修改源文件。保留本轮输出的
   release 路径、更新前后摘要和验收结果。保留用户工作区、浏览器数据、用户技能、密钥、
   桌面绑定以及现有执行身份/资源控制逻辑；不运行 `wuying_bootstrap.py`。

下面的默认示例只指向开发环境。生产执行时，把配置文件替换为该台桌面的专用配置；文件
需以无引号的 `KEY=value` 形式提供 `WUYING_DESKTOP_ID` 和 `WUYING_REGION_ID`。
`--env-file` 不会切换阿里云 CLI 账号，也不会自动建立到桌面的应用隧道。

```bash
export WUYING_UPDATE_ENV=backend/.env.wuying-dev
export WUYING_SKILL_RELEASE="video_prep_$(date -u +%Y%m%dT%H%M%SZ)"
git rev-parse HEAD
aliyun sts GetCallerIdentity
```

#### 3. 更新流式输出，保留原有执行服务实现

```bash
# 只检查本地源文件和目标配置、展示计划；不访问桌面，也不证明远端布局兼容。
backend/.venv/bin/python backend/scripts/wuying_patch_stream_output.py \
  --env-file "$WUYING_UPDATE_ENV" --dry-run

# 已通过维护窗口门禁后执行；暂不重启，待技能文件也同步完再统一重启。
backend/.venv/bin/python backend/scripts/wuying_patch_stream_output.py \
  --env-file "$WUYING_UPDATE_ENV" --no-restart
```

此脚本只替换 `execute_stream` 中的 reader 和退出回执，保留桌面上其他实现。它会检查
目标布局、root 保护和发布前哈希，并将原文件保存到输出中的
`/opt/action_server/releases/stream_output_<时间>/original-action_server.py`。
执行前还需通过云助手记录 `stat -c '%a %u %g %n' /opt/action_server/action_server.py`
的结果；备份副本采用私有权限保存，回滚时应使用记录的原权限，不直接套用备份文件的权限。
失败时停止该台发布；布局不兼容时需先审查整套模块升级，不能自动改用 bootstrap。
任何超时都先检查真实远端结果，不能根据“没有收到回复”直接重复执行或重启。

#### 4. 同步视频技能的 10 个文件

下面的独立发布段通过现有 `Desktop.run` 使用云助手，固定文件集合，上传前备份原文件，
校验所有新旧摘要后才覆盖代码；传输分片和备份均保留。它不调用会清理临时文件的
`Desktop.put`，不安装依赖，不修改其他技能或用户内容。`release` 必须是新的目录名，
失败后保留同一目录检查部分发布状态，不能换一个 release 盲目重试。
本段已做本地临时目录中的传输、备份和摘要模拟校验；真实发布仍须先从开发桌面开始验收。

```bash
PYTHONPATH=backend/scripts backend/.venv/bin/python - \
  "$WUYING_UPDATE_ENV" "$WUYING_SKILL_RELEASE" <<'PY'
import base64, gzip, hashlib, json, re, shlex, sys
from pathlib import Path
from wuying_bootstrap import Desktop
from wuying_deploy_action_server import read_env

profile, release = sys.argv[1:]
if not re.fullmatch(r"[A-Za-z0-9_-]{8,96}", release):
    raise SystemExit("Invalid release name")
desktop_id = read_env("WUYING_DESKTOP_ID", profile)
region = read_env("WUYING_REGION_ID", profile)
if not desktop_id or not region:
    raise SystemExit("Explicit desktop ID and region are required")
names = (
    "SKILL.md", "scripts/prepare.py", "scripts/state.py", "scripts/lint_prompt.py",
    "references/compose-timeline.md", "references/material-plan.md",
    "references/minimax-turbo-frames.md", "references/model-guide.md",
    "references/prompt-recipes.md", "references/quality.md",
)
source = Path("backend/.openbox/skills/video-production")
contents = {name: (source / name).read_bytes() for name in names}
for name, content in contents.items():
    if name.endswith(".py"):
        compile(content, name, "exec")
expected = {name: hashlib.sha256(content).hexdigest() for name, content in contents.items()}
packed = base64.b64encode(gzip.compress(json.dumps({
    name: base64.b64encode(content).decode() for name, content in contents.items()
}).encode())).decode()
chunks = [packed[i:i + 8500] for i in range(0, len(packed), 8500)]
desktop = Desktop(desktop_id, region)

common = f"""
import base64, gzip, hashlib, json, os, pathlib, stat
root = pathlib.Path('/opt/openbox/skills/video-production')
stage = pathlib.Path('/opt/openbox/skill-backups') / {release!r}
names = {names!r}
expected = {expected!r}
def protected(path):
    for item in (path, *path.parents):
        meta = item.lstat()
        if stat.S_ISLNK(meta.st_mode) or meta.st_uid != 0 or meta.st_mode & 0o022:
            raise RuntimeError('Expected root-protected path: ' + str(item))
if os.geteuid() != 0:
    raise RuntimeError('Existing root deployment authority required')
protected(root)
"""

def run(body):
    command = "python3 -I -S -c " + shlex.quote(common + body)
    if len(base64.b64encode(command.encode())) > 16 * 1024:
        raise RuntimeError("Cloud Assistant command exceeds transport limit")
    return desktop.run(command, timeout=60)

print(run("""
stage.parent.mkdir(mode=0o700, exist_ok=True)
protected(stage.parent)
stage.mkdir(mode=0o700, exist_ok=False)
before = {}
for name in names:
    target = root / name
    protected(target.parent)
    if target.exists() or target.is_symlink():
        protected(target)
        if not target.is_file():
            raise RuntimeError('Expected regular file: ' + name)
        old = target.read_bytes()
        saved = stage / 'originals' / name
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_bytes(old)
        before[name] = {'sha256': hashlib.sha256(old).hexdigest(),
                        'mode': stat.S_IMODE(target.stat().st_mode)}
    else:
        before[name] = {'sha256': None, 'mode': 0o644}
(stage / 'manifest.json').write_text(json.dumps({'before': before, 'after': expected}))
print(json.dumps({'release': str(stage), 'files': len(names)}))
"""))
for index, chunk in enumerate(chunks):
    run(f"protected(stage)\nwith (stage / 'chunk-{index:04d}').open('xb') as out:\n    out.write({chunk.encode()!r})\n")
print(run(f"""
protected(stage)
manifest = json.loads((stage / 'manifest.json').read_text())
if manifest['after'] != expected:
    raise RuntimeError('Release manifest changed')
encoded = b''.join((stage / f'chunk-{{i:04d}}').read_bytes() for i in range({len(chunks)}))
payload = json.loads(gzip.decompress(base64.b64decode(encoded)))
if set(payload) != set(names):
    raise RuntimeError('Unexpected skill files')
new = {{name: base64.b64decode(payload[name]) for name in names}}
for name in names:
    if hashlib.sha256(new[name]).hexdigest() != expected[name]:
        raise RuntimeError('Uploaded hash mismatch: ' + name)
    if name.endswith('.py'):
        compile(new[name], name, 'exec')
    target = root / name
    protected(target.parent)
    if target.exists() or target.is_symlink():
        protected(target)
    actual = hashlib.sha256(target.read_bytes()).hexdigest() if target.exists() else None
    if actual != manifest['before'][name]['sha256']:
        raise RuntimeError('Target changed since backup: ' + name)
for name in names:
    target = root / name
    target.write_bytes(new[name])
    target.chmod(manifest['before'][name]['mode'])
    if hashlib.sha256(target.read_bytes()).hexdigest() != expected[name]:
        raise RuntimeError('Installed hash mismatch: ' + name)
print(json.dumps({{'release': str(stage), 'verified_files': len(names), 'sha256': expected}}))
"""))
PY
```

#### 5. 重启执行服务并验收

仅在目标桌面仍空闲、两项代码发布均成功时执行。此命令不重启整个云电脑或 Chrome。

```bash
PYTHONPATH=backend/scripts backend/.venv/bin/python - "$WUYING_UPDATE_ENV" <<'PY'
import sys
from wuying_bootstrap import Desktop
from wuying_deploy_action_server import read_env
profile = sys.argv[1]
desktop_id = read_env("WUYING_DESKTOP_ID", profile)
region = read_env("WUYING_REGION_ID", profile)
if not desktop_id or not region:
    raise SystemExit("Explicit desktop ID and region are required")
desktop = Desktop(desktop_id, region)
print(desktop.run("systemctl restart openbox-action-server && "
                  "systemctl is-active openbox-action-server", timeout=60))
PY
```

每台至少记录以下证据，失败的桌面保持维护状态：

- 服务为 `active`，经该桌面实际应用通道请求 `/alive` 成功，返回的主机身份与目标一致。
  root 云助手的文件校验不能替代普通用户执行身份验收。
- 在该用户原有资源授权上下文中，用后端 `bash` 执行
  `printf 'update-check-start\n'; sleep 2; printf 'update-check-done\n'`。
  两段输出齐全、退出码 0，原始 SSE exit 的 `output_complete=true`，工具结果中的
  `stream_output_complete=true`。只看退出码或 `/alive` 版本不能作为发布成功依据。
- 校验第 4 步 10 个文件的安装后摘要与本次源码一致，`prepare.py` 的导入/语法检查通过。
  用新建的独立 QA 草稿验证准备流程，不改用户已有草稿或已生成片段。
- 新后端先在指定测试用户上验证浏览器普通请求：一个 `browser_action` 返回页面状态和
  截图，内部 bash/view_image 均完成；查看已有页面时使用空 script，不重复点击或刷新。
- 视频准备验证到内容确认卡为止：没有积分估价/费用卡，没有开始付费生成。记录模型轮次、
  shell 次数、总时长和 provider/runtime/internal 分解；模型等待波动不能当作固定性能收益。

开发环境的受控慢判断对照曾为 shell 21.1 秒 → 约 2.1 秒；普通浏览器观察为 2 轮模型、
1 个模型可见工具调用；视频准备样例为 7 轮模型、1 次 shell、约 111 秒。这些是开发验收
样例，不是所有生产桌面的时延承诺，也不是每次发布必须重复付费调用的门槛。

#### 6. 发布后端、覆盖预热池和新建桌面

已分配桌面和可被分配的预热桌面完成上述验收后，按 [DEPLOY.md](DEPLOY.md) 的后端发布
流程切换；本次 Web 估价入口的移除也需随前端构建发布。保留原有权限、资源身份和实际计费。
休眠或离线桌面继续挂起新流程准入，恢复后补更新及验收。镜像/初始化资源更新必须覆盖
Action Server 和视频技能，两者不能依赖浏览器运行时版本自检；新建一台测试桌面复验后，
再允许该模板供应新的桌面。不要仅修复现有桌面而继续从旧模板补充预热池。

发布记录逐台写入：目标 ID/区域、源码提交、更新前后摘要、两类备份路径、服务重启时间、
SSE/浏览器/视频验收结果、已更新/待更新/失败状态。不得记录 API key 或登录凭据。

#### 7. 回滚与失败处理

1. 先暂停新流程、保持目标桌面不接新任务；如需恢复旧执行服务，先回滚或停用依赖
   `output_complete` 的新后端，避免新工具继续访问旧回执服务。
2. 检查实际安装文件是否仍等于该次发布记录的 after SHA。若不一致，视为后续修改或
   不确定状态，停止覆盖并调查；不按“最新时间的备份”猜测回滚目标。
3. 执行服务只恢复该台发布输出给出的 `original-action_server.py`，恢复原权限并重启
   `openbox-action-server`。视频技能按同一台同一 release 的 `manifest.json` 恢复
   `originals/` 中有备份的文件和原权限；原本不存在的新增文件先保留，不通过删除清理回滚。
4. 重新检查服务、通道、用户执行身份和旧流程。保留分片、备份、失败日志与用户数据。
   部分上传或超时不是“没有发生写入”，先读真实状态，再决定恢复或继续。

### Pinned resource-control commands

When `OPENBOX_RESOURCE_CONTROL_DB` is configured, Action Server now advertises
`resource_admission_v2`. Its authenticated bind/close requests require the
existing `journal_id`, resource identity, owner, epoch and a stable command ID.
The journal commits an immutable control-command receipt with the control
change. Replaying the same ID returns that receipt and the current status;
changing its payload returns a conflict. An initial close creates a closed
binding directly, and a delayed bind never reopens it. Existing v1 journal
identity, control and operation rows are retained when the receipt table is
added. A fresh journal is a different resource history even at the same URL.

Backend migration `pa5f6a7b8c9d` adds the journal pin and a remote observation
to `resource_control_leases`. The actor-bound assistant API accepts `bind` and
`close` at `POST /api/assistant/resources/{resource_id}/control`, with
`idempotency_key` and `expected_epoch`. The resource must already be enrolled
against the current assigned desktop. Close commits local admission closure
before network IO. The existing AssistantCommand records the frozen fence;
the backend pins the authenticated remote journal before sending the command.
Periodic Agent recovery retries a lost response using the original command ID
and pin. It resolves the assigned desktop's existing channel without creating,
starting or reassigning a desktop. Read the command's current state through
`GET /api/assistant/commands/{command_id}`; the acceptance receipt itself is
not proof that the remote change has applied.
`GET /api/assistant/resources/{resource_id}` returns backend effect state and
the last remote control observation with its observation time. A remote snapshot
does not assert that later operations or direct channels have drained.

Bound computer requests send the pinned journal ID as well as their original
resource fence and operation ID. A replacement journal rejects those requests
before admission, including when its control table is empty. Missing or changed
receipts cannot settle a pinned operation successfully. A command encountering
a changed pinned identity enters `blocked` and closes local admission; it does
not erase the old pin or automatically bind a replacement. Recover the original
journal and inspect the persisted commands and operation receipts before any
future ownership transition.

This protocol upgrade requires coordinated rollout: v1 fenced clients omit the
journal header and are refused by a v2 gate. First migrate the backend schema
and preserve the existing remote journal, then explicitly bind the enrolled
resource before using its v2 operation channel. Unconfigured legacy servers
remain outside verified takeover coverage. A pre-write unsupported-protocol
failure records a blocked bind without changing an unpinned local admission.
The APIs expose no human grant or giveback yet: standalone/direct/native
channels, descendant drainage, token revocation and fresh-observation handling
remain required. Neither an applied close nor an empty local effect list proves
exclusive physical control.

### Offline migration of existing control storage

`container/storage_migration.py` prepares existing `/data` and `/workspace`
ownership for the optional `OPENBOX_EXECUTOR_USER` boundary. Both deployment
scripts and the Docker image now include this utility. It runs separately from
deployment; neither a service restart nor uploading the utility migrates data.

Record the existing `journal_id` from the authenticated resource-control status
and its current control/operation receipts. Stop every service and container
using either volume, including executor processes, and ensure no other host or
container mounts them for writing. Run as root with the intended ordinary
executor account and the previously recorded identity:

```bash
python3 -I /opt/action_server/storage_migration.py \
  --executor sandbox \
  --expected-journal-id "$OPENBOX_MIGRATION_JOURNAL_ID" \
  --offline
```

The default roots are `/data` and `/workspace`; alternate roots require explicit
`--data-root` and `--workspace-root`. The journal stays at
`/data/openbox-control/control.sqlite3`. A root-owned sticky `/data` permits
ordinary user entries while protecting its root-owned `0700` control directory.
User file contents and executable bits remain; ownership moves to the selected
account and owner read/write access is restored. User symlinks retain their
targets; links outside the approved ownership scope are not followed, and
cross-scope hard links, nested mounts and special files block migration.

The utility pins the existing journal identity and hashes its logical rows,
including committed WAL data. Missing, changed or unfamiliar journals are
refused without creating replacements. It preserves closed admission and all
running/unknown operations. A durable `storage-migration.json` checkpoint binds
the two roots and executor; rerunning the same command resumes an interrupted
migration only while its journal snapshot remains unchanged. An unfinished
checkpoint or a replaced journal prevents isolated Action Server startup.
After a `ready` result, compare the retained receipts before enabling the
executor account and starting the service. New operation rows after successful
migration do not invalidate startup.

The Dockerfile now provisions this protected layout for fresh volumes. Existing volumes
retain their old ownership until this explicit procedure runs; executor
isolation remains opt-in. The offline flag asserts exclusive administrative
access to these volumes: local process checks and directory locks cannot prove
that another host or namespace is stopped. Migration does not certify remote
operation drainage, enable human takeover, settle unknown effects or reset
history. The procedure was tested in disposable Linux containers; no existing
cloud desktop was migrated as part of this change.

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
`openbox-action-server.service`. Re-run the bootstrap with an explicit
`--api-key` to set both.

**Tunnel is up, relay listens, but the backend still cannot reach it**
Check which address the relay bound. `permitlisten` with no host restricts the
bind to loopback, which is correct for this layout but will not serve a backend
connecting from elsewhere.

**`dev-browser not installed in container` (HTTP 500)**
The desktop was bootstrapped with `--skip-dev-browser`, or the npm install
failed. Re-run the bootstrap without that flag.

**Sandbox reachable but browser automation never connects**
First distinguish cloud-local mode from extension mode. On the desktop, local
mode requires both `http://127.0.0.1:9333/json/version` (Chrome CDP) and
`http://127.0.0.1:9222/` (relay) to respond. `extensionConnected: false` is normal
in local mode; it is not evidence that cloud Chrome is broken. In extension
mode, check the extension's Server URL under *Advanced* in its popup.

On a reused BossIP image, two independent initialization failures can prevent
local mode from starting:

- The inherited Chrome launcher rewrites OpenBox's dedicated profile to an old
  worker's directory. If the current desktop user cannot write it, Chrome falls
  back to its default profile, where Chrome 136+ disables remote debugging.
- Node/npm exist only under `/opt/bossip/runtime/node/bin`, outside the service
  PATH, and the relay's `node_modules` are missing. A piped `npm install | tail`
  formerly masked the installation failure.

`backend/sandbox/browser_runtime_repair.py` repairs these initialization issues without
restarting services or touching IBus/profile data. It narrowly permits the
current user's own `.config/obx-chrome` directory through the inherited gate,
preserves other profile restrictions, exposes missing Node commands without
overwriting existing ones, and stages/validates dependencies before promotion.
The bootstrap and backend install it automatically, together with
`backend/sandbox/assets/dev-browser-package-lock.json`. The repository entry
point `container/repair_browser_runtime.py` is only a local forwarding CLI;
upload the standalone backend implementation and its lock file when doing
manual guest maintenance. On the guest, run as root:

```bash
python3 /opt/openbox/tools/repair_browser_runtime.py --install-deps --register-service
python3 /opt/openbox/tools/repair_browser_runtime.py --check
```

Backups and installation logs stay under `/opt/openbox/backups/browser-runtime-*`.
Repairs are serialized with a guest file lock. Dependencies use `npm ci` with
an isolated configuration and disabled install scripts, are validated in a
staging directory, and roll back if promotion fails. A healthy runtime is a
no-op; every readiness check still executes TypeScript and imports the runtime.
Boot service registration does not restart a healthy action server or Chrome.
If Chrome previously fell back to the default profile, first save pending browser
work, gracefully close that user's browser, and back up/copy only that user's
profile into its dedicated directory before relaunching. Never copy a legacy
worker's login data or loosen another user's directory permissions. Verify CDP,
relay discovery, and a real `npx tsx` client interaction; an open Chrome window
alone is not a successful browser-automation check.

**Reading a broken desktop without logging into it (browser diagnostics)**
Every browser failure the backend hits (`ChromeUnavailable`, `RelayUnavailable`,
`BrowserRuntimeUnavailable`, a failed channel verify) now takes one read-only
snapshot of the desktop and cites it: the tool message the model sees ends in
`[diag:<id>]`, `cloud_desktops.channel_error` carries the same tag, and the
backend log line `browser failure diag=<id> …` lists the five lights
(chrome/relay/x/unit/runtime) and the findings. Snapshots for one desktop are
spaced at least five minutes apart and are stored as `browser.diag` rows in
`desktop_events` (`GET /api/admin/fleet/diag/recent?desktop_id=`,
`GET /api/admin/fleet/diag/<id>`), kept for 30 days.

The same table is the desktop's timeline: every browser bring-up
(`browser.ensure`, with requested vs effective mode and any fallback reason),
Chrome and relay launches, failed runtime checks and the repairs they trigger,
each channel verify with its attempt count, leases that waited over two
seconds or were refused, and the snapshots themselves. Rows carry the session
and tool-call ids the action server saw in `X-OpenBox-*`, so
`GET /api/admin/fleet/desktops/<ecd-id>/events?session=<id>` shows one
conversation's browser history end to end.

To take a fresh one: `POST /api/admin/fleet/desktops/<ecd-id>/diag` with
`{"via": "auto"}` uses the application channel and falls back to Cloud
Assistant when the tunnel is down. The collector is `backend/sandbox/obx_diag.py` (`container/obx_diag.py` only forwards to it);
the backend ships its own copy inline with every request, so the report format
never depends on what the desktop has installed. The runtime repair also
installs it at `/opt/openbox/tools/obx_diag.py` for hands-on use:

```bash
python3 /opt/openbox/tools/obx_diag.py --pretty            # on the desktop
curl -H "X-API-Key: $KEY" 'http://127.0.0.1:18000/diag/browser?session=<id>'   # through the tunnel
```

What it reports: the OpenBox Chrome process (allow-listed argv, uid, threads,
RSS, age, DISPLAY), `/json/version` and the open targets, the profile's
`DevToolsActivePort` and `SingletonLock` owner, the relay's `GET /` and pid
file, `obx-x`/`xrandr` resolution and the streaming manager's client count,
the systemd units' state/`NRestarts`/`TasksCurrent`/`TasksMax` and drop-ins,
`repair_browser_runtime.py --check`, tails of `/tmp/obx-chrome.log`,
`/tmp/obx-relay.log` and the action server's `execute_trace` journal lines.
Command text never appears anywhere: the action server hashes commands and
classifies them (`browser_probe`, `browser_launch`, `browser_script`, …), and
now echoes `X-OpenBox-Request` on every response so a backend log line can be
matched to its journald record by id instead of by timestamp.

The probes behind `GET /api/browser/status` and the `browser_mode` tool tell
`transport` (the desktop did not answer at all) apart from `connect` (the
desktop answered, the port did not), `http` and `parse`; a grey dot in the
settings page now comes with that word.

## What differs from the Docker and Kubernetes providers

| | Docker / Kubernetes | WUYING |
|---|---|---|
| Lifecycle | OpenBox creates and destroys containers | The desktop is provisioned out of band; `create` is idempotent, `delete`/`stop` are **no-ops** |
| Isolation | Container per user (Docker) or pod (K8s) | One shared desktop; sessions separated by `/workspace/sessions/<id>` only |
| Reachability | Local port / in-cluster service | Two-hop tunnel |
| Clean slate | Docker starts fresh each boot | State persists — installed packages, files, everything |
| Startup | Docker cleans up; K8s reconciles | Reconciles (health-checks the endpoint) |

The no-op `delete_container` matters: `SandboxManager.release()` destroys a
container once its last session ends. Left alone, that would try to delete
someone's cloud desktop.

The shared-desktop model also means **there is no isolation boundary between
users** beyond the working directory, and the action server does not constrain
paths — an absolute path escapes the session directory. Treat a WUYING sandbox
as single-tenant — or switch to per-user desktops, below.

## Per-user desktops (WUYING_MODE=per_user)

`WUYING_MODE=per_user` closes the single-tenant gap: OpenBox provisions **one
ECD desktop per workspace** through the ECD OpenAPI, with a dedicated convenience
EndUser per workspace, instead of pointing everyone at the shared desktop. The
implementation is ported from bossip's wuying-bridge and keeps its hard-won
behaviours (EndUser sync wait before CreateDesktops, tag reads through
ListTagResources, charge-aware ghost handling, environment tagging).

**How it works**

- Identity: each workspace id derives a stable EndUser (`obx-<sha256[:16]>`) and a
  salted password (`WUYING_PASSWORD_SALT`). Display names never feed the id.
- Ownership: desktops carry `openbox-workspace` / `openbox-user` (legacy-compatible)
  / `openbox-eu-id` / `openbox-env` tags. The ticket API verifies the workspace
  tag before minting a ticket, so a member cannot cross workspace boundaries.
  `openbox-env` keeps prod and dev sharing one
  Alibaba Cloud account from adopting or reaping each other's desktops.
- State: the `cloud_desktops` table records each workspace's desktop
  (`backend/db/models/cloud_desktop.py`); a unique partial index enforces one
  live desktop per workspace. If the DB forgets a desktop, it is re-adopted by tag.
- Flow: the 云桌面 tab shows a provisioning opt-in for workspaces without a
  desktop; `POST /api/desktop/provision` creates (2-3 min) or wakes it, the
  frontend polls `GET /api/desktop/status`, and the ticket API rides the same
  202 retry channel while the desktop is creating/starting.

**Extra configuration** (see `.env.example`): `WUYING_IMAGE_ID` (golden image
— required; there is deliberately no fallback to a community image),
`WUYING_OFFICE_SITE_ID`, `WUYING_PASSWORD_SALT`, and optionally
`WUYING_DESKTOP_TYPE` / `WUYING_SYSTEM_DISK_SIZE` / `WUYING_POLICY_GROUP_ID` /
`WUYING_CHARGE_TYPE` / `WUYING_ENV_TAG`. `WUYING_ROUTING=per_desktop` switches
the execution plane from the legacy endpoint to the caller's assigned row.
The default is `shared`, so deploying the code alone cannot move an existing
installation.

For a subscription desktop, set `WUYING_CHARGE_TYPE=PrePaid` together with
`WUYING_PERIOD=1`, `WUYING_PERIOD_UNIT=Month`, `WUYING_AUTO_PAY=true`, and
normally `WUYING_AUTO_RENEW=false`. Month accepts 1/2/3/6; Year accepts 1-5.
`PrePaid` with `auto_pay=false` is rejected at configuration load because the
ECD API would only create an unpaid order, not a usable desktop. PostPaid
requests deliberately omit all four subscription fields. Use
`POOL_MAX_UNIT_PRICE_CNY` (default 300) as the read-only smoke-test threshold.

Ghost cleanup is charge-aware. The channel is always revoked first. A confirmed
PostPaid desktop is then deleted; a PrePaid or unclassified desktop is only
marked reclaimed and soft-deleted locally, preserving paid subscription value.
Both paths emit audit records. Alibaba Cloud documents a 15-day retention
window after a subscription expires: the desktop cannot be started, stopped,
rebooted, connected, or otherwise used during that window, but its data is
retained and it can still be renewed; automatic release begins on day 16.
Whether `RenewDesktops` restores the existing Shanghai desktop cleanly remains
a live-observation item around 2026-10-01.

### Fleet snapshots and the warm pool

The A3/A4 fleet layer keeps the ECD account, `cloud_desktops`, and account
balance observable as three independent sources. The `fleet_snapshot` internal
task runs every `FLEET_SNAPSHOT_INTERVAL_SEC` (600 by default), stores one row
per source, retains seven days, and evaluates ten deterministic reconciliation
rules. Open, acknowledged, muted, and resolved alerts are available under
`/api/admin/fleet`; a failed source suppresses its dependent findings and no
alert auto-resolves unless all three sources succeeded in that run.

Pool desktops use six persisted states: `reserve`, `prewarm`, `assigned`,
`released`, `recycling`, and `retired`. `assigning` is the sole transient state
used by the atomic `FOR UPDATE SKIP LOCKED` claim. A prewarm row has no workspace
or channel credentials. Assignment grants the workspace EndUser, writes owner
tags, installs a fresh per-desktop channel, and verifies authenticated command
execution plus 1920×1080 before becoming assigned. Release revokes access but
preserves the disk; an explicitly approved recycle erases it by rebuilding from
`WUYING_IMAGE_ID` before it can return to prewarm.

Existing desktops can enter the pool only when their exact id is present in
`POOL_ADOPT_ALLOWLIST`. Adoption records the original tags and EndUsers before
any destructive call. A different image requires both `rebuild=true` and
`approve=true`; failure to clear the old EndUser quarantines the row instead of
making it assignable. The shared desktop `ecd-4zjxaq5g45dr5qr0i` is a hard
exclusion and must never carry the production OpenBox environment tag.

Both pool use and automatic purchasing are off by default:
`POOL_ENABLED=false` and `POOL_AUTO_PURCHASE=false`. Production first enables
pool assignment with automatic purchase still off. Procurement and renewal
rules must retain their separate price/impact approval gate before any real
`CreateDesktops`, `RenewDesktops`, `RebuildDesktops`, charge conversion, or
deletion call.

Build the secret-free golden image with:

```bash
python backend/scripts/wuying_bootstrap.py --image-mode \
  --desktop-id ecd-... --region cn-shanghai
python backend/scripts/wuying_image_verify.py \
  --desktop-id ecd-... --region cn-shanghai \
  --baseline docs/image-baseline-dpkg.txt
```

The verified Shanghai image produced by this procedure on 2026-09-03 is
`m-ccceuit7jn3xzwx45` (`openbox-image-v2-shanghai`, CUSTOM Linux, 50G). It
reached `Available` after the verifier passed, and its source desktop was then
deleted. Configure `WUYING_IMAGE_ID=m-ccceuit7jn3xzwx45` and
`WUYING_SYSTEM_DISK_SIZE=50` for the Shanghai per-desktop fleet.

Image mode installs runtime, action server, browser relay and the fixed-display
helpers, but leaves both systemd units disabled. `/etc/openbox` is empty and
`/root/.ssh` is absent. Instance provisioning writes `/etc/openbox/action.env`
and, for SSH channels, a new `/etc/openbox/tunnel_key`, `tunnel.env`, and pinned
`known_hosts`; secrets are therefore unique to the clone and never enter the
image.

**Testing** — `scripts/wuying_provision_smoke.py` exercises the chain in five
tiers: `check` (read-only: lists office sites, images, OpenBox desktops),
`price` (read-only: prints raw and parsed PostPaid/PrePaid quotes),
`enduser` (free: real EndUser create → sync → remove), and `full` (billable:
provisions a real desktop through the same service the API uses, waits for
Running, mints a connection ticket, then deletes everything; `--yes` required,
`--disk` must cover the image size). `channel` is also billable and uses the
deployment database to exercise create → per-instance credentials → channel
verify → authenticated hostname → revoke/delete. Under PrePaid, `full` and
`channel` refuse to run unless both `--allow-prepaid` and `--keep` are supplied:
active subscription desktops cannot be safely removed with `DeleteDesktops`,
so refund/退订 is a separate, identity-checked acceptance step. Unit coverage lives in
`tests/unit/test_wuying_provisioning.py` with the ECD calls stubbed.

The execution gap is wired when both `WUYING_MODE=per_user` and
`WUYING_ROUTING=per_desktop` are set. The provider then resolves the owner in
`cloud_desktops`, decrypts that row's action key, and routes through either:

- `WUYING_CHANNEL=direct`: `private_ip:8000`, for a standard office network
  connected to the backend VPC.
- `WUYING_CHANNEL=ssh`: one reverse-forward port and ed25519 key per desktop.
  The gateway's isolated sshd asks
  `GET /api/internal/tunnel-keys?fingerprint=...` for every authentication and
  receives a `permitlisten`-scoped key only while the row is not revoked.

The action key is AES-GCM encrypted under the 32-byte `WUYING_CHANNEL_KEY` and
stored with a `v1:` envelope; only its SHA-256 is stored separately. A desktop
does not reach `running/channel.up` until authenticated command execution and
the 1920×1080 display both verify. Systemd restores the channel after a reboot,
and a revoked row is rejected by provider routing immediately.

`api/desktop._per_user()` still requires both the provisioning mode and
`SandboxProvider.routes_per_user`. Shared routing therefore retains the old
single endpoint and forces the viewer back to that same desktop; per-desktop
routing makes both the viewer and agent resolve the caller's assigned desktop.

Startup now logs both planes on one line, e.g.

```
Cloud desktop — agent and view route to each caller's assigned desktop in cn-shanghai
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
