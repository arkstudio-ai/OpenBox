import argparse
import asyncio
import contextvars
import fcntl
import hashlib
import hmac
import inspect
import json
import logging
import os
import platform
import pty
import random
import re
import select
import shutil
import signal
import secrets
import stat
import struct
import sys
import tarfile
import termios
import time
import unicodedata
import zipfile
import zlib
from collections.abc import MutableMapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

import subprocess

import anyio
import psutil
import yaml
from fastapi import FastAPI, HTTPException, Request, UploadFile, File, Form, WebSocket, Query
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field as PydanticField, StringConstraints
from sse_starlette.sse import EventSourceResponse
import uvicorn

# The production service runs this file directly from /opt/action_server, while
# tests load it with importlib or exec() (the latter intentionally supplies no
# __file__). Put the directory containing media_jobs.py on sys.path in all
# three cases so the durable queue is the same code path.
if globals().get("__file__"):
    _ACTION_SERVER_DIR = Path(str(globals()["__file__"])).resolve().parent
else:
    _ACTION_SERVER_DIR = next(
        (
            candidate
            for candidate in (Path.cwd() / "container", Path.cwd())
            if (candidate / "media_jobs.py").is_file()
        ),
        Path.cwd(),
    )
if str(_ACTION_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_ACTION_SERVER_DIR))

from media_jobs import (  # noqa: E402
    MediaJobConflict,
    MediaJobError,
    MediaJobNotFound,
    media_job_manager,
)

# --- 启动时间记录 ---
START_TIME = time.time()
ACTION_SERVER_VERSION = "2026.08.31-run-lease-receipt-v12"
CATALOGUE_PROTOCOL_VERSION = 1
_ACTION_SERVER_BOOT_ID = hashlib.sha256(
    f"{platform.node()}:{START_TIME:.9f}".encode("utf-8")
).hexdigest()
# Uvicorn owns the configured INFO handler in both containers and the WUYING
# systemd service. A standalone child logger inherited the root WARNING level
# and silently discarded the very traces this feature exists to preserve.
trace_log = logging.getLogger("uvicorn.error")


@asynccontextmanager
async def _same_task_timeout(delay: float):
    """Apply a timeout without moving transport work into another task.

    The WUYING image currently ships Python 3.10, which predates
    ``asyncio.timeout``. MCP transports also require enter/use/exit to stay in
    the same task, so ``asyncio.wait_for`` is not a safe compatibility shim.
    AnyIO's cancel scope has the required same-task semantics on Python 3.10.
    """
    timeout_factory = getattr(asyncio, "timeout", None)
    if timeout_factory is not None:
        async with timeout_factory(delay):
            yield
        return
    try:
        with anyio.fail_after(delay):
            yield
    except TimeoutError as exc:
        # Python 3.10 still has a distinct ``asyncio.TimeoutError`` class.
        # Normalize the compatibility path so existing asyncio callers catch
        # the deadline instead of treating it as a broken MCP transport.
        raise asyncio.TimeoutError from exc

# --- Models ---
class ExecuteRequest(BaseModel):
    command: str
    timeout: int = 120       # max timeout (absolute safety cap), default 2 minutes
    idle_timeout: int = 60   # seconds without output before emitting idle event
    workdir: str | None = None

class KillRequest(BaseModel):
    pid: int

class ExecuteResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str

class DesktopLeaseRequest(BaseModel):
    owner: str
    wait_timeout: float = 90.0
    ttl_seconds: float = 180.0

class DesktopLeaseReleaseRequest(BaseModel):
    token: str


class MediaInputRequest(BaseModel):
    name: str = PydanticField(max_length=180)
    mime: str = PydanticField(default="video/mp4", max_length=128)
    size: int = PydanticField(default=0, ge=0, le=1024 * 1024 * 1024)
    cache_key: str = PydanticField(min_length=1, max_length=1024)
    url: str = PydanticField(min_length=8, max_length=8192)


class MediaOutputRequest(BaseModel):
    name: str = PydanticField(default="final.mp4", max_length=180)
    mime: str = PydanticField(default="video/mp4", max_length=128)
    put_url: str = PydanticField(min_length=8, max_length=8192)


class MediaJobSubmitRequest(BaseModel):
    operation: Literal["render", "extract_audio"] = "render"
    job_id: str = PydanticField(min_length=8, max_length=96)
    owner: str = PydanticField(min_length=1, max_length=128)
    session_id: str = PydanticField(default="", max_length=128)
    idempotency_key: str = PydanticField(min_length=1, max_length=180)
    inputs: list[MediaInputRequest] = PydanticField(min_length=1, max_length=100)
    output: MediaOutputRequest
    captions: list[Annotated[str, StringConstraints(max_length=2000)]] = PydanticField(
        default_factory=list, max_length=100
    )
    subtitles: bool = True
    channel_name: str = PydanticField(default="", max_length=100)
    render_engine: Literal["auto", "ffmpeg", "hyperframes"] = "auto"
    width: int = PydanticField(default=720, ge=320, le=3840)
    height: int = PydanticField(default=1280, ge=320, le=3840)


class MediaJobOwnerRequest(BaseModel):
    owner: str = PydanticField(min_length=1, max_length=128)


class MediaJobRetryRequest(MediaJobOwnerRequest):
    payload: dict | None = None

class ListFilesRequest(BaseModel):
    path: str = "/workspace"

class WriteFileRequest(BaseModel):
    path: str
    content: str

class DeleteFileRequest(BaseModel):
    path: str

class ResolvePathTargetRequest(BaseModel):
    path: Annotated[str, StringConstraints(min_length=1, max_length=8192)]
    allow_missing: bool = False
    allow_scoped_skills: bool = False

class ResolvePathsRequest(BaseModel):
    targets: list[ResolvePathTargetRequest] = PydanticField(
        min_length=1, max_length=100
    )

class ReadFileRequest(BaseModel):
    path: str
    offset: int = 0  # 0-based line offset
    limit: int = 2000  # max lines to return

class GlobRequest(BaseModel):
    pattern: str
    path: str = "/workspace"
    include_sensitive: bool = False

class GrepRequest(BaseModel):
    pattern: str
    path: str = "/workspace"
    type: str | None = None  # file type filter, e.g. "py", "js"
    max_results: int = 100
    include_sensitive: bool = False

# --- API Key ---
SESSION_API_KEY = os.environ.get("SESSION_API_KEY", "")


def _api_key_matches(supplied: str) -> bool:
    """Fail closed when the execution-plane credential is not configured."""
    return bool(SESSION_API_KEY) and secrets.compare_digest(
        supplied, SESSION_API_KEY
    )

# User commands run with a different OS identity from the root-owned Action
# Server.  Besides limiting filesystem damage, this prevents a command from
# reading the server's API key or MCP credentials through /proc/<parent>/environ.
RUNNER_USER = os.environ.get("OPENBOX_RUNNER_USER", "sandbox")
REQUIRE_RUNNER = os.environ.get("OPENBOX_REQUIRE_RUNNER", "0") == "1"
WORKSPACE_ROOT = Path(os.environ.get("OPENBOX_WORKSPACE_ROOT", "/workspace")).resolve()
RUNNER_HOME = Path(
    os.environ.get("OPENBOX_RUNNER_HOME", str(WORKSPACE_ROOT / ".openbox-home"))
)

# A shared WUYING desktop is an acceptance topology, not the eventual SaaS
# topology (production assigns one desktop to one user).  Until then, every
# catalogue request carries the same pseudonymous segment already used by the
# backend workspace namespace.  It is deliberately narrow enough to be safe as
# one path component and never contains a raw user id.
USER_SCOPE_HEADER = "X-OpenBox-User-Scope"
_USER_SCOPE_PATTERN = re.compile(r"^u-[0-9a-f]{20}$")
_request_user_scope: contextvars.ContextVar[str] = contextvars.ContextVar(
    "openbox_request_user_scope", default=""
)
REQUIRE_USER_SCOPE = os.environ.get("OPENBOX_REQUIRE_USER_SCOPE", "0") == "1"


def _current_user_scope() -> str:
    return _request_user_scope.get()


def _contained_scope_root(root: Path, scope: str, label: str) -> Path:
    """Join a validated scope without following a tenant-controlled symlink."""
    if not _USER_SCOPE_PATTERN.fullmatch(scope):
        raise HTTPException(status_code=400, detail="Invalid user scope")
    root_resolved = root.resolve()
    candidate = root / scope
    if candidate.is_symlink():
        raise HTTPException(status_code=409, detail=f"{label} scope cannot be a symlink")
    resolved = candidate.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise HTTPException(status_code=403, detail=f"{label} scope escapes its data root")
    return candidate


def _scoped_skill_root() -> Path:
    scope = _current_user_scope()
    return _contained_scope_root(SKILLS_DIR, scope, "Skill") if scope else SKILLS_DIR


def _scoped_export_root() -> Path:
    scope = _current_user_scope()
    if not scope:
        return SKILL_EXPORTS_DIR
    namespace = WORKSPACE_ROOT / "openbox" / "users"
    namespace_resolved = namespace.resolve()
    workspace_resolved = WORKSPACE_ROOT.resolve()
    if (
        namespace_resolved != workspace_resolved
        and workspace_resolved not in namespace_resolved.parents
    ):
        raise HTTPException(status_code=403, detail="Export namespace escapes workspace")
    user_root = _contained_scope_root(namespace, scope, "Export")
    return user_root / ".openbox" / "exports"


def _scoped_mcp_config_path(scope: str | None = None) -> Path:
    selected = _current_user_scope() if scope is None else scope
    if not selected:
        return MCP_CONFIG_PATH
    scoped_root = _contained_scope_root(MCP_CONFIG_PATH.parent, selected, "MCP")
    return scoped_root / MCP_CONFIG_PATH.name


def _scope_required_path(path: str) -> bool:
    return path == "/catalog" or path.startswith(("/catalog/", "/skills", "/mcp/"))


def _runner_account():
    try:
        import pwd

        return pwd.getpwnam(RUNNER_USER)
    except KeyError:
        if REQUIRE_RUNNER:
            raise RuntimeError(f"Required command runner user does not exist: {RUNNER_USER}")
        return None


def _demote_to_runner() -> None:
    account = _runner_account()
    if account is None or os.geteuid() != 0:
        return
    os.initgroups(account.pw_name, account.pw_gid)
    os.setgid(account.pw_gid)
    os.setuid(account.pw_uid)


def _runner_argv(argv: list[str]) -> list[str]:
    """Wrap a subprocess so untrusted code cannot retain server privileges."""
    account = _runner_account()
    if account is None or os.geteuid() != 0:
        return argv
    setpriv = shutil.which("setpriv")
    if not setpriv:
        if REQUIRE_RUNNER:
            raise RuntimeError("setpriv is required for isolated command execution")
        return argv
    return [
        setpriv,
        f"--reuid={account.pw_uid}",
        f"--regid={account.pw_gid}",
        "--init-groups",
        "--no-new-privs",
        "--",
        *argv,
    ]


def _runner_shell_argv(command: str) -> list[str]:
    """Run one model-provided command without desktop-wide login hooks.

    The WUYING image's ``/etc/profile`` configures GNOME input sources with
    ``gsettings``.  Agent commands are intentionally headless, so a login
    shell only emits a misleading dconf/DBus warning before every command.
    Runtime paths and locale are already supplied by :func:`_runner_env`.
    """
    return _runner_argv([
        "/bin/bash",
        "--noprofile",
        "--norc",
        "-c",
        command,
    ])


def _runner_env(
    extra: dict[str, str] | None = None,
    *,
    user_scope: str | None = None,
) -> dict[str, str]:
    account = _runner_account()
    home = str(RUNNER_HOME) if account is not None else os.environ.get("HOME", "/tmp")
    scope = _current_user_scope() if user_scope is None else user_scope
    if account is not None and scope:
        scoped_home = _scoped_runner_home(scope)
        _ensure_runner_directory(scoped_home)
        home = str(scoped_home)
    # Explicit allowlist: provider/API/MCP credentials held by the control
    # process never enter an arbitrary shell command.
    env = {
        "HOME": home,
        "USER": account.pw_name if account is not None else RUNNER_USER,
        "LOGNAME": account.pw_name if account is not None else RUNNER_USER,
        "PATH": os.environ.get(
            "PATH",
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        ),
        # PTY frames carry bytes, so both ends must agree on UTF-8.  Empty
        # inherited locale variables are not useful defaults and can garble
        # CJK filenames in readline/prompt rendering.
        "LANG": os.environ.get("LANG") or "C.UTF-8",
        "LC_ALL": os.environ.get("LC_ALL") or os.environ.get("LANG") or "C.UTF-8",
        "TERM": os.environ.get("TERM", "xterm-256color"),
        "SHELL": "/bin/bash",
        "XDG_CACHE_HOME": str(Path(home) / ".cache"),
        "NPM_CONFIG_CACHE": str(Path(home) / ".npm"),
    }
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    if extra:
        env.update(extra)
    return {key: value for key, value in env.items() if value != ""}


def _chown_runner_path(path: Path) -> None:
    """Give the command identity ownership without following a caller symlink."""
    account = _runner_account()
    if account is None or os.geteuid() != 0:
        return
    try:
        os.chown(path, account.pw_uid, account.pw_gid, follow_symlinks=False)
    except FileNotFoundError:
        return


def _ensure_runner_directory(path: Path) -> None:
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    path.mkdir(parents=True, exist_ok=True)
    # pathlib creates every missing parent as the privileged Action Server.
    # Chown the whole newly-created chain, not only the leaf, or the sandbox
    # identity cannot traverse a root-owned 0750 tenant namespace.
    for created in reversed(missing):
        _chown_runner_path(created)
    if not missing:
        _chown_runner_path(path)


def _scoped_runner_home(user_scope: str) -> Path:
    return (
        WORKSPACE_ROOT
        / "openbox"
        / "users"
        / user_scope
        / ".openbox"
        / "home"
    )


def _chown_runner_tree(root: Path) -> None:
    """Give a completed tenant package to the runner without following links."""
    _chown_runner_path(root)
    for current, dirnames, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in [*dirnames, *filenames]:
            _chown_runner_path(current_path / name)


def _workspace_path(
    raw_path: str,
    *,
    must_exist: bool = False,
    allow_scoped_skills: bool = False,
) -> Path:
    """Resolve an API path and reject symlink/``..`` escapes.

    File writes remain workspace-only. Read/list/search calls may opt into the
    current tenant's Skill package root so loaded skills can reference bundled
    files without exposing another tenant's packages.
    """
    if not raw_path or "\x00" in raw_path:
        raise HTTPException(status_code=400, detail="Invalid workspace path")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = WORKSPACE_ROOT / candidate
    try:
        resolved = candidate.resolve(strict=must_exist)
    except (FileNotFoundError, OSError) as exc:
        raise HTTPException(status_code=404, detail=f"Path not found: {raw_path}") from exc
    allowed_roots = [WORKSPACE_ROOT]
    if allow_scoped_skills and _current_user_scope():
        allowed_roots.append(_scoped_skill_root().resolve())
    if not any(resolved == root or root in resolved.parents for root in allowed_roots):
        raise HTTPException(status_code=403, detail="Path escapes the OpenBox workspace")
    return resolved


def _terminal_workspace_path(workdir: str, user_scope: str) -> Path:
    """Validate a terminal cwd against both workspace and tenant namespace."""
    if not _USER_SCOPE_PATTERN.fullmatch(user_scope or ""):
        raise HTTPException(status_code=400, detail="Invalid terminal user scope")
    target = _workspace_path(workdir, must_exist=False)
    tenant_root = _contained_scope_root(
        WORKSPACE_ROOT / "openbox" / "users",
        user_scope,
        "Terminal",
    ).resolve()
    if target != tenant_root and tenant_root not in target.parents:
        raise HTTPException(
            status_code=403,
            detail="Terminal directory is outside the tenant workspace",
        )
    # Project creation is intentionally available while a desktop is offline.
    # The first terminal connection therefore owns the safe, authenticated
    # repair path for a missing project directory and runner HOME.  Both must
    # be created while the Action Server is still root; the PTY child drops to
    # the runner identity before building its environment.
    _ensure_runner_directory(target)
    _ensure_runner_directory(_scoped_runner_home(user_scope))
    resolved = _workspace_path(str(target), must_exist=True)
    if resolved != tenant_root and tenant_root not in resolved.parents:
        raise HTTPException(
            status_code=403,
            detail="Terminal directory is outside the tenant workspace",
        )
    return resolved


def _workspace_glob_pattern(pattern: str) -> str:
    if not pattern or "\x00" in pattern or Path(pattern).is_absolute():
        raise HTTPException(status_code=400, detail="Invalid workspace glob pattern")
    if ".." in PurePosixPath(pattern).parts:
        raise HTTPException(status_code=403, detail="Glob pattern escapes the workspace")
    return pattern


def _is_sensitive_workspace_path(path: Path) -> bool:
    """Classify search results hidden from broad, unapproved discovery."""
    try:
        parts = path.relative_to(WORKSPACE_ROOT).parts
    except ValueError:
        parts = path.parts
    for part in parts:
        lowered = part.casefold()
        if lowered.startswith(".env"):
            return True
        if lowered == ".ssh" or "credentials" in lowered:
            return True
    return False


_GREP_ENV_EXCLUDE = ".[eE][nN][vV]*"
_GREP_SSH_EXCLUDE = ".[sS][sS][hH]"
_GREP_CREDENTIALS_EXCLUDE = (
    "*[cC][rR][eE][dD][eE][nN][tT][iI][aA][lL][sS]*"
)

# --- Protected command detection ---
# The action_server is the ONLY communication channel between the backend and the
# container. If it gets killed, all subsequent tool calls fail with 503. These
# patterns detect commands that would damage this communication channel.
import re as _re

ACTION_SERVER_PORT = int(os.environ.get("PORT", "8000"))

_PROTECTED_PATTERNS: list[tuple[str, str]] = [
    # kill PID 1 (init/tini) or -1 (all processes)
    (r'\bkill\b[^|;]*\s+(-\w+\s+)*1\b', "Cannot kill PID 1 (container init)"),
    (r'\bkill\b[^|;]*\s+-1\b', "Cannot signal all processes"),
    # pkill/killall targeting action_server runtime
    (r'\b(pkill|killall)\b[^|;]*\b(python[23]?|uvicorn|action_server)\b',
     "Cannot kill python/uvicorn/action_server — this would destroy the execution interface"),
    # fuser -k on port 8000
    (r'\bfuser\b[^|;]*-k[^|;]*\b8000\b',
     f"Cannot kill processes on port {ACTION_SERVER_PORT}"),
    (r'\bfuser\b[^|;]*\b8000\b[^|;]*-k',
     f"Cannot kill processes on port {ACTION_SERVER_PORT}"),
    # lsof piped to kill for port 8000
    (r'\blsof\b[^|]*\b8000\b.*\|\s*(xargs\s+)?kill',
     f"Cannot kill processes on port {ACTION_SERVER_PORT}"),
    # iptables/ufw blocking port 8000
    (r'\biptables\b[^|;]*(DROP|REJECT)[^|;]*\b8000\b',
     f"Cannot block port {ACTION_SERVER_PORT} with iptables"),
    (r'\bufw\b[^|;]*(deny|reject)[^|;]*\b8000\b',
     f"Cannot block port {ACTION_SERVER_PORT} with ufw"),
    # Modify/delete action_server files
    (r'\b(rm|mv|chmod|chown|truncate)\b[^|;]*/opt/action_server',
     "Cannot modify /opt/action_server — it contains the execution interface"),
    # System halt
    (r'\b(shutdown|poweroff|halt)\b', "Cannot shut down the container"),
    (r'\binit\s+[06]\b', "Cannot change runlevel"),
]

def _is_protected_command(command: str) -> str | None:
    """Return error message if command would damage the action_server, None if safe."""
    for pattern, msg in _PROTECTED_PATTERNS:
        if _re.search(pattern, command, _re.IGNORECASE):
            return msg
    return None

# --- Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    if not SESSION_API_KEY:
        raise RuntimeError("SESSION_API_KEY is required for the Action Server")
    reconnect_tasks: list[asyncio.Task] = []
    # A global /workspace/skills link exposes every scoped directory on a
    # shared desktop. Keep it only for explicit legacy/single-tenant mode.
    skills_link = Path("/workspace/skills")
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    if REQUIRE_USER_SCOPE:
        if skills_link.is_symlink():
            skills_link.unlink()
        elif skills_link.exists():
            raise RuntimeError(
                "Tenant-scoped mode refuses the legacy /workspace/skills directory"
            )
    else:
        if not skills_link.exists():
            try:
                skills_link.symlink_to(SKILLS_DIR)
            except OSError:
                pass
        _ensure_skill_symlinks()
    # MCP config outlives the container but connections do not, so a restart
    # used to leave every enabled server listed as disconnected with its tools
    # silently missing from the agent. Reconnect enabled servers in the
    # background: a server that is slow or gone must not delay startup. A
    # user's explicit disconnect is persisted and is never undone here.
    if REQUIRE_USER_SCOPE:
        reconnect_tasks.extend(
            asyncio.create_task(manager.reconnect_configured())
            for manager in _persisted_scoped_mcp_managers()
        )
    else:
        reconnect_tasks.append(
            asyncio.create_task(mcp_manager.reconnect_configured())
        )
    await media_job_manager.start()
    try:
        yield
    finally:
        for reconnect_task in reconnect_tasks:
            reconnect_task.cancel()
        if reconnect_tasks:
            await asyncio.gather(*reconnect_tasks, return_exceptions=True)
        managers = [mcp_manager, *_scoped_mcp_managers.values()]
        seen_managers: set[int] = set()
        for manager in managers:
            if id(manager) in seen_managers:
                continue
            seen_managers.add(id(manager))
            await manager.shutdown()
        _scoped_mcp_managers.clear()
        await media_job_manager.stop()

app = FastAPI(title="OpenBox Sandbox Action Server", lifespan=lifespan)

# A WUYING provider maps every OpenBox session to one physical desktop. This
# process-level lease protects the whole input -> settle/capture -> OSS upload
# transaction, rather than merely serialising individual shell commands.
_desktop_lease_condition = asyncio.Condition()
_desktop_lease: dict | None = None


def _trace_value(request: Request, name: str, limit: int = 120) -> str:
    value = request.headers.get(name, "")
    return "".join(ch for ch in value if 32 <= ord(ch) < 127)[:limit]


# Backend database fencing continues at the execution boundary. Without this
# durable high-water mark, a stale backend that lost PostgreSQL connectivity
# could still execute a shell/write/MCP side effect on the shared desktop.
_RUN_FENCE_PATH = Path(
    os.environ.get("OPENBOX_RUN_FENCE_PATH", "/data/openbox_run_fences.json")
)
_run_fence_lock = asyncio.Lock()
_RUN_LEASE_MAX_FUTURE_MS = 65_000


def _run_lease_signature(
    session_id: str,
    run_id: str,
    epoch: int,
    expires_at_ms: int,
) -> str:
    payload = f"{session_id}\n{run_id}\n{epoch}\n{expires_at_ms}".encode("utf-8")
    return hmac.new(
        SESSION_API_KEY.encode("utf-8"),
        payload,
        hashlib.sha256,
    ).hexdigest()


def _load_run_fences() -> dict[str, dict[str, object]]:
    try:
        raw = json.loads(_RUN_FENCE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, object]] = {}
    for session_id, value in raw.items():
        if not isinstance(session_id, str) or not isinstance(value, dict):
            continue
        try:
            epoch = int(value.get("epoch", 0))
        except (TypeError, ValueError):
            continue
        run_id = value.get("run_id")
        if epoch > 0 and isinstance(run_id, str) and run_id:
            result[session_id] = {"epoch": epoch, "run_id": run_id}
    return result


_run_fences = _load_run_fences()


def _persist_run_fences() -> None:
    _RUN_FENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = _RUN_FENCE_PATH.with_name(
        f".{_RUN_FENCE_PATH.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    )
    temporary.write_text(
        json.dumps(_run_fences, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, _RUN_FENCE_PATH)


async def _validate_run_fence(request: Request) -> None:
    """Reject expired receipts, then advance one session epoch atomically.

    The durable epoch prevents an older generation after a newer request has
    reached this desktop. The signed database-lease expiry closes the earlier
    gap as well: after PostgreSQL permits takeover, a paused old worker cannot
    issue its *first* late request while the remote high-water mark is still on
    the previous generation.
    """
    session_id = _trace_value(request, "X-OpenBox-Session")
    run_id = _trace_value(request, "X-OpenBox-Run")
    raw_epoch = _trace_value(request, "X-OpenBox-Run-Epoch", 24)
    raw_expires = _trace_value(request, "X-OpenBox-Run-Lease-Expires", 24)
    signature = _trace_value(request, "X-OpenBox-Run-Lease-Signature", 80)
    if not any((run_id, raw_epoch, raw_expires, signature)):
        return  # non-Agent control-plane/readiness operation
    if not all((session_id, run_id, raw_epoch, raw_expires, signature)):
        raise HTTPException(status_code=400, detail="Incomplete OpenBox run fence")
    try:
        epoch = int(raw_epoch)
        expires_at_ms = int(raw_expires)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid OpenBox run lease") from exc
    if epoch <= 0 or expires_at_ms <= 0 or not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise HTTPException(status_code=400, detail="Invalid OpenBox run lease")
    expected_signature = _run_lease_signature(
        session_id,
        run_id,
        epoch,
        expires_at_ms,
    )
    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(status_code=403, detail="Invalid OpenBox run lease signature")
    now_ms = int(time.time() * 1000)
    if expires_at_ms <= now_ms:
        raise HTTPException(status_code=409, detail="Expired OpenBox agent run lease")
    if expires_at_ms - now_ms > _RUN_LEASE_MAX_FUTURE_MS:
        raise HTTPException(status_code=400, detail="Invalid OpenBox run lease lifetime")
    async with _run_fence_lock:
        current = _run_fences.get(session_id)
        if current is not None:
            current_epoch = int(current["epoch"])
            current_run = str(current["run_id"])
            if epoch < current_epoch or (epoch == current_epoch and run_id != current_run):
                raise HTTPException(
                    status_code=409,
                    detail="Stale OpenBox agent run fence",
                )
            if epoch == current_epoch:
                return
        previous = current
        _run_fences[session_id] = {"epoch": epoch, "run_id": run_id}
        try:
            _persist_run_fences()
        except OSError as exc:
            if previous is None:
                _run_fences.pop(session_id, None)
            else:
                _run_fences[session_id] = previous
            raise HTTPException(
                status_code=503,
                detail="OpenBox run fence store unavailable",
            ) from exc


def _lease_is_live(now: float | None = None) -> bool:
    return bool(_desktop_lease and _desktop_lease["expires_at"] > (now or time.monotonic()))


def _desktop_command_kind(command: str) -> str:
    """Classify without logging command contents, which may contain secrets."""
    lowered = command.lower()
    if "xdotool" in lowered:
        return "desktop_input"
    if "obx-shot" in lowered or "scrot" in lowered:
        return "desktop_capture"
    if "obx-file" in lowered and any(
        path in lowered
        for path in ("/tmp/obx-screen.png", "/tmp/obx-sandbox-screen.png")
    ):
        return "desktop_oss_upload"
    if "obx-x" in lowered:
        return "desktop_session"
    return "shell"


def _requires_desktop_lease(request: Request, command: str) -> bool:
    operation = _trace_value(request, "X-OpenBox-Operation", 48)
    return operation == "computer" or _desktop_command_kind(command) != "shell"


async def _validate_desktop_lease(request: Request, command: str) -> None:
    """Reject old/unleased desktop clients so they cannot corrupt a live turn."""
    global _desktop_lease
    if not _requires_desktop_lease(request, command):
        return
    token = _trace_value(request, "X-OpenBox-Desktop-Lease", 160)
    async with _desktop_lease_condition:
        now = time.monotonic()
        if not _lease_is_live(now):
            _desktop_lease = None
        if not _desktop_lease or not secrets.compare_digest(token, _desktop_lease["token"]):
            raise HTTPException(
                status_code=423,
                detail="Desktop command requires an active OpenBox desktop lease",
            )
        # Active work renews the crash-recovery deadline.
        _desktop_lease["expires_at"] = now + _desktop_lease["ttl_seconds"]


def _emit_execute_trace(
    request: Request,
    command: str,
    *,
    started: float,
    exit_code: int,
) -> None:
    trace_log.info(
        "execute_trace %s",
        json.dumps({
            "request": _trace_value(request, "X-OpenBox-Request"),
            "instance": _trace_value(request, "X-OpenBox-Instance"),
            "session": _trace_value(request, "X-OpenBox-Session"),
            "run": _trace_value(request, "X-OpenBox-Run"),
            "run_epoch": _trace_value(request, "X-OpenBox-Run-Epoch", 24),
            "tool_call": _trace_value(request, "X-OpenBox-Tool-Call"),
            "operation": _trace_value(request, "X-OpenBox-Operation", 48),
            "kind": _desktop_command_kind(command),
            "command_sha256": hashlib.sha256(command.encode()).hexdigest()[:16],
            "duration_ms": round((time.monotonic() - started) * 1000),
            "exit_code": exit_code,
        }, separators=(",", ":"), sort_keys=True),
    )

# --- API Key 中间件 ---
@app.middleware("http")
async def authenticate(request: Request, call_next):
    if request.url.path == "/alive":
        return await call_next(request)
    api_key = request.headers.get("X-API-Key", "")
    if not SESSION_API_KEY:
        return JSONResponse(
            status_code=503,
            content={"detail": "Action Server API key is not configured"},
        )
    if not _api_key_matches(api_key):
        return JSONResponse(status_code=403, content={"detail": "Invalid API Key"})
    supplied_scope = request.headers.get(USER_SCOPE_HEADER, "").strip()
    if supplied_scope and not _USER_SCOPE_PATTERN.fullmatch(supplied_scope):
        return JSONResponse(status_code=400, content={"detail": "Invalid user scope"})
    if REQUIRE_USER_SCOPE and _scope_required_path(request.url.path) and not supplied_scope:
        return JSONResponse(
            status_code=428,
            content={"detail": "A tenant scope is required for catalogue access"},
        )
    scope_token = _request_user_scope.set(supplied_scope)
    try:
        await _validate_run_fence(request)
    except HTTPException as exc:
        _request_user_scope.reset(scope_token)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    try:
        return await call_next(request)
    finally:
        _request_user_scope.reset(scope_token)

# --- 健康检查 ---
@app.get("/alive")
async def alive():
    return {
        "status": "ok",
        "version": ACTION_SERVER_VERSION,
        "capabilities": [
            "desktop_lease_v1",
            "execution_trace_v1",
            "run_fencing_v1",
            "run_lease_receipt_v2",
            "catalogue_projection_v1",
            "tenant_catalogue_scopes_v1",
            "skill_archive_create_only_v1",
            "skill_restore_fence_v1",
            "skill_archive_bounded_v1",
            "confined_file_delete_v1",
            "sensitive_search_filter_v1",
            "confined_path_resolve_v1",
            "mcp_desired_state_v1",
            "mcp_supervisor_v1",
            "terminal_project_cwd_v1",
            "media_jobs_v1",
            "media_jobs_fastpath_v2",
            "media_jobs_audio_extract_v3",
        ],
        "media_jobs": media_job_manager.capabilities(),
        "uptime": round(time.time() - START_TIME, 2),
        "hostname": platform.node(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _media_http_error(exc: MediaJobError) -> HTTPException:
    if isinstance(exc, MediaJobNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, MediaJobConflict):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@app.post("/media/jobs")
async def submit_media_job(req: MediaJobSubmitRequest):
    """Idempotently enqueue one HyperFrames/FFmpeg render on this desktop."""
    try:
        return await media_job_manager.submit(req.model_dump())
    except MediaJobError as exc:
        raise _media_http_error(exc) from exc


@app.get("/media/jobs/status")
async def media_queue_status():
    return await media_job_manager.queue_status()


@app.get("/media/jobs/{job_id}")
async def get_media_job(job_id: str, owner: str = Query(...)):
    try:
        return await media_job_manager.get(job_id, owner)
    except MediaJobError as exc:
        raise _media_http_error(exc) from exc


@app.get("/media/jobs/{job_id}/wait")
async def wait_media_job(
    job_id: str,
    owner: str = Query(...),
    after_version: int = Query(0, ge=0),
    timeout: float = Query(25.0, ge=0, le=25),
):
    """Bounded long-poll; callers repeat while queued/in_progress."""
    try:
        return await media_job_manager.wait(
            job_id, owner, after_version=after_version, timeout=timeout
        )
    except MediaJobError as exc:
        raise _media_http_error(exc) from exc


@app.post("/media/jobs/{job_id}/cancel")
async def cancel_media_job(job_id: str, req: MediaJobOwnerRequest):
    try:
        return await media_job_manager.cancel(job_id, req.owner)
    except MediaJobError as exc:
        raise _media_http_error(exc) from exc


@app.post("/media/jobs/{job_id}/retry")
async def retry_media_job(job_id: str, req: MediaJobRetryRequest):
    """Requeue a terminal failure while retaining the verified input cache."""
    try:
        return await media_job_manager.retry(job_id, req.owner, req.payload)
    except MediaJobError as exc:
        raise _media_http_error(exc) from exc


@app.post("/desktop/lease/acquire")
async def acquire_desktop_lease(req: DesktopLeaseRequest, request: Request):
    """Wait for exclusive access to the shared physical desktop."""
    global _desktop_lease
    instance = _trace_value(request, "X-OpenBox-Instance")
    if not instance:
        raise HTTPException(status_code=400, detail="X-OpenBox-Instance is required")
    owner = "".join(ch for ch in req.owner if 32 <= ord(ch) < 127)[:300]
    if not owner:
        raise HTTPException(status_code=400, detail="lease owner is required")

    wait_timeout = max(0.0, min(300.0, float(req.wait_timeout)))
    ttl_seconds = max(30.0, min(600.0, float(req.ttl_seconds)))
    wait_started = time.monotonic()
    deadline = wait_started + wait_timeout

    async with _desktop_lease_condition:
        while True:
            now = time.monotonic()
            if not _lease_is_live(now):
                _desktop_lease = None
            if _desktop_lease is None or _desktop_lease["owner"] == owner:
                reused = _desktop_lease is not None
                token = _desktop_lease["token"] if reused else secrets.token_urlsafe(32)
                _desktop_lease = {
                    "token": token,
                    "owner": owner,
                    "instance": instance,
                    "session": _trace_value(request, "X-OpenBox-Session"),
                    "tool_call": _trace_value(request, "X-OpenBox-Tool-Call"),
                    "ttl_seconds": ttl_seconds,
                    "expires_at": now + ttl_seconds,
                }
                wait_ms = round((now - wait_started) * 1000)
                trace_log.info(
                    "desktop_lease_acquired %s",
                    json.dumps({
                        "instance": instance,
                        "session": _desktop_lease["session"],
                        "tool_call": _desktop_lease["tool_call"],
                        "wait_ms": wait_ms,
                        "reused": reused,
                    }, separators=(",", ":"), sort_keys=True),
                )
                return {"token": token, "wait_ms": wait_ms, "ttl_seconds": ttl_seconds}

            remaining = deadline - now
            if remaining <= 0:
                raise HTTPException(status_code=423, detail={
                    "message": "Timed out waiting for the shared desktop",
                    "holder_instance": _desktop_lease["instance"],
                    "holder_session": _desktop_lease["session"],
                })
            until_expiry = max(0.05, _desktop_lease["expires_at"] - now)
            try:
                await asyncio.wait_for(
                    _desktop_lease_condition.wait(),
                    timeout=min(remaining, until_expiry),
                )
            except asyncio.TimeoutError:
                pass


@app.post("/desktop/lease/release")
async def release_desktop_lease(req: DesktopLeaseReleaseRequest, request: Request):
    """Release a desktop lease; expiration remains the crash fallback."""
    global _desktop_lease
    released = False
    async with _desktop_lease_condition:
        if _desktop_lease and secrets.compare_digest(req.token, _desktop_lease["token"]):
            holder = _desktop_lease
            _desktop_lease = None
            released = True
            _desktop_lease_condition.notify_all()
            trace_log.info(
                "desktop_lease_released %s",
                json.dumps({
                    "instance": holder["instance"],
                    "session": holder["session"],
                    "tool_call": holder["tool_call"],
                    "released_by": _trace_value(request, "X-OpenBox-Instance"),
                }, separators=(",", ":"), sort_keys=True),
            )
    return {"released": released}

# --- Helper: kill entire process group ---
def _kill_process_tree(process):
    """Kill the process and its entire process group (children, grandchildren, etc.)."""
    try:
        pgid = os.getpgid(process.pid)
    except (OSError, ProcessLookupError):
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


def _kill_process_tree_by_pid(pid: int):
    """Kill a process group by PID (for external kill requests)."""
    try:
        pgid = os.getpgid(pid)
    except (OSError, ProcessLookupError):
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass


async def _terminate_process_tree(process) -> bool:
    """Kill and reap a still-running subprocess after a canceled stream."""
    if process.returncode is not None:
        return False
    _kill_process_tree(process)
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except (asyncio.TimeoutError, OSError, ProcessLookupError):
        pass
    return True


# --- Kill endpoint ---
@app.post("/kill")
async def kill_command(req: KillRequest):
    """Kill a running command by PID."""
    protected_pids = {1, os.getpid(), os.getppid()}
    if req.pid in protected_pids:
        raise HTTPException(
            status_code=403,
            detail=f"Cannot kill PID {req.pid} — it is a protected system process",
        )
    _kill_process_tree_by_pid(req.pid)
    return {"ok": True}


# --- 执行命令 ---
@app.post("/execute", response_model=ExecuteResponse)
async def execute(req: ExecuteRequest, request: Request):
    started = time.monotonic()
    exit_code = 1
    await _validate_desktop_lease(request, req.command)
    blocked = _is_protected_command(req.command)
    if blocked:
        _emit_execute_trace(request, req.command, started=started, exit_code=exit_code)
        return ExecuteResponse(exit_code=exit_code, stdout="", stderr=f"[BLOCKED] {blocked}")
    workdir = _workspace_path(req.workdir or "/workspace", must_exist=True)
    try:
        process = await asyncio.create_subprocess_exec(
            *_runner_shell_argv(req.command),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(workdir),
            start_new_session=True,
            env=_runner_env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=req.timeout
            )
        except asyncio.TimeoutError:
            _kill_process_tree(process)
            try:
                await asyncio.wait_for(process.communicate(), timeout=2)
            except asyncio.TimeoutError:
                pass
            exit_code = -1
            _emit_execute_trace(request, req.command, started=started, exit_code=exit_code)
            return ExecuteResponse(exit_code=exit_code, stdout="", stderr=f"Command timed out after {req.timeout}s")
        exit_code = process.returncode or 0
        response = ExecuteResponse(
            exit_code=exit_code,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )
        _emit_execute_trace(request, req.command, started=started, exit_code=exit_code)
        return response
    except Exception as e:
        _emit_execute_trace(request, req.command, started=started, exit_code=exit_code)
        raise HTTPException(status_code=500, detail=str(e))

# --- 上传文件 ---
@app.post("/upload")
async def upload_file(file: UploadFile = File(...), destination: str = Form("/workspace")):
    dest_path = _workspace_path(destination)
    _ensure_runner_directory(dest_path)
    filename = file.filename or "upload.bin"
    if Path(filename).name != filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Unsafe upload filename")
    file_path = _workspace_path(str(dest_path / filename))
    try:
        content = await file.read()
        file_path.write_bytes(content)
        _chown_runner_path(file_path)
        return {"message": "File uploaded", "path": str(file_path), "size": len(content)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- 下载文件 ---
@app.get("/download")
async def download_file(path: str):
    file_path = _workspace_path(path, must_exist=True)
    if file_path.is_file():
        return StreamingResponse(
            open(file_path, "rb"),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{file_path.name}"'},
        )
    # 目录则打包为 zip
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in file_path.rglob("*"):
            if f.is_symlink():
                continue
            if f.is_file():
                _workspace_path(str(f), must_exist=True)
                zf.write(f, f.relative_to(file_path))
    buffer.seek(0)
    return StreamingResponse(
        buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{file_path.name}.zip"'},
    )

# --- 列出文件 ---
@app.post("/list_files")
async def list_files(req: ListFilesRequest):
    target = _workspace_path(req.path, must_exist=True, allow_scoped_skills=True)
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {req.path}")
    entries = []
    for item in sorted(target.iterdir()):
        try:
            if item.is_symlink():
                entries.append({
                    "name": item.name,
                    "is_dir": False,
                    "size": None,
                    "modified": None,
                })
                continue
            stat = item.stat()
            entries.append({
                "name": item.name,
                "is_dir": item.is_dir(),
                "size": stat.st_size if item.is_file() else None,
                "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            })
        except PermissionError:
            entries.append({"name": item.name, "is_dir": item.is_dir(), "size": None, "modified": None})
    return {"path": req.path, "entries": entries}

# --- 系统信息 ---
@app.get("/system_info")
async def system_info():
    cpu_percent = psutil.cpu_percent(interval=0.1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return {
        "cpu": {"percent": cpu_percent, "count": psutil.cpu_count()},
        "memory": {"total": memory.total, "used": memory.used, "percent": memory.percent},
        "disk": {"total": disk.total, "used": disk.used, "free": disk.free, "percent": disk.percent},
        "hostname": platform.node(),
        "platform": platform.platform(),
    }

# --- Canonical path permission preflight ---
@app.post("/resolve_paths")
async def resolve_paths(req: ResolvePathsRequest):
    """Resolve permission targets inside the execution plane's path boundary.

    This closes static symlink aliases before backend policy evaluation. It is
    intentionally only a preflight snapshot; callers must not treat it as a
    lock or as protection against a path being replaced after this response.
    """
    resolved_targets = []
    for target in req.targets:
        resolved = _workspace_path(
            target.path,
            must_exist=not target.allow_missing,
            allow_scoped_skills=(
                target.allow_scoped_skills and not target.allow_missing
            ),
        )
        try:
            workspace_relative = resolved.relative_to(WORKSPACE_ROOT).as_posix()
        except ValueError:
            workspace_relative = None
        resolved_targets.append({
            "canonical_path": str(resolved),
            "workspace_relative": workspace_relative,
        })
    return {"targets": resolved_targets}


# --- Write File ---
@app.post("/write_file")
async def write_file(req: WriteFileRequest):
    """Write content to a file. Creates parent directories if needed."""
    file_path = _workspace_path(req.path)
    _ensure_runner_directory(file_path.parent)
    file_path.write_text(req.content, encoding="utf-8")
    _chown_runner_path(file_path)
    return {"message": "File written", "path": str(file_path), "size": len(req.content)}

# --- Delete File ---
@app.post("/delete_file")
async def delete_file(req: DeleteFileRequest):
    """Delete one regular workspace file without invoking a shell."""
    candidate = Path(req.path)
    if not candidate.is_absolute():
        candidate = WORKSPACE_ROOT / candidate
    if candidate.name in {"", ".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid file path")
    # Resolve and validate the parent, then use dir_fd operations for the final
    # component. This never follows a final symlink and closes the usual
    # lstat/unlink path-replacement race.
    parent = _workspace_path(str(candidate.parent), must_exist=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(parent, flags)
    except OSError as exc:
        raise HTTPException(status_code=409, detail="File parent is unavailable") from exc
    try:
        try:
            target_stat = os.stat(candidate.name, dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return {"message": "File deleted", "path": str(candidate), "deleted": False}
        if stat.S_ISLNK(target_stat.st_mode):
            raise HTTPException(status_code=409, detail="File path cannot be a symlink")
        if not stat.S_ISREG(target_stat.st_mode):
            raise HTTPException(status_code=400, detail="Path is not a regular file")
        try:
            os.unlink(candidate.name, dir_fd=descriptor)
        except OSError as exc:
            raise HTTPException(status_code=409, detail="File could not be deleted") from exc
    finally:
        os.close(descriptor)
    return {"message": "File deleted", "path": str(candidate), "deleted": True}

# --- Read File ---
@app.post("/read_file")
async def read_file(req: ReadFileRequest):
    """Read file content with line numbers (cat -n format)."""
    file_path = _workspace_path(req.path, must_exist=True, allow_scoped_skills=True)
    if not file_path.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {req.path}")

    content = file_path.read_text(encoding="utf-8", errors="replace")
    lines = content.split("\n")
    total_lines = len(lines)

    # Apply offset and limit
    start = max(0, req.offset)
    end = min(total_lines, start + req.limit)
    selected = lines[start:end]

    # Format with line numbers (cat -n style)
    numbered = []
    for i, line in enumerate(selected, start=start + 1):
        # Truncate very long lines
        if len(line) > 2000:
            line = line[:2000] + "... (truncated)"
        numbered.append(f"     {i}\t{line}")

    return {
        "content": "\n".join(numbered),
        "total_lines": total_lines,
        "start_line": start + 1,
        "end_line": end,
        "path": req.path,
    }

# --- Glob ---
@app.post("/glob")
async def glob_files(req: GlobRequest):
    """Find files matching a glob pattern, sorted by modification time (newest first)."""
    base = _workspace_path(req.path, must_exist=True, allow_scoped_skills=True)
    pattern = _workspace_glob_pattern(req.pattern)
    pattern_sensitive = _is_sensitive_workspace_path(Path(pattern))
    allow_sensitive = req.include_sensitive and (
        _is_sensitive_workspace_path(base) or pattern_sensitive
    )

    matches = []
    try:
        for p in base.glob(pattern):
            if p.is_symlink():
                continue
            if p.is_file():
                _workspace_path(str(p), must_exist=True, allow_scoped_skills=True)
                if not allow_sensitive and _is_sensitive_workspace_path(p):
                    continue
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    mtime = 0
                matches.append((str(p), mtime))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid glob pattern: {e}")

    # Sort by modification time, newest first, limit to 1000
    matches.sort(key=lambda x: x[1], reverse=True)
    files = [m[0] for m in matches[:1000]]

    return {"files": files, "total": len(matches), "truncated": len(matches) > 1000}

# --- Grep ---
@app.post("/grep")
async def grep_files(req: GrepRequest):
    """Search file contents using grep."""
    search_path = _workspace_path(req.path, must_exist=True, allow_scoped_skills=True)
    target_sensitive = _is_sensitive_workspace_path(search_path)
    allow_sensitive = req.include_sensitive and target_sensitive
    if not allow_sensitive and target_sensitive:
        return {"output": "", "exit_code": 0, "error": ""}
    cmd_parts = ["grep", "-rn", "--color=never"]

    if not allow_sensitive:
        cmd_parts.extend([
            "--exclude", _GREP_ENV_EXCLUDE,
            "--exclude", _GREP_CREDENTIALS_EXCLUDE,
            "--exclude-dir", _GREP_ENV_EXCLUDE,
            "--exclude-dir", _GREP_SSH_EXCLUDE,
            "--exclude-dir", _GREP_CREDENTIALS_EXCLUDE,
        ])

    # Add file type filter
    if req.type:
        ext_map = {
            "py": "*.py", "js": "*.js", "ts": "*.ts", "tsx": "*.tsx",
            "jsx": "*.jsx", "go": "*.go", "rs": "*.rs", "java": "*.java",
            "c": "*.c", "cpp": "*.cpp", "h": "*.h", "rb": "*.rb",
            "php": "*.php", "css": "*.css", "html": "*.html", "md": "*.md",
            "json": "*.json", "yaml": "*.yaml", "yml": "*.yml",
            "toml": "*.toml", "xml": "*.xml", "sh": "*.sh",
            "sql": "*.sql", "vue": "*.vue", "svelte": "*.svelte",
        }
        glob_pattern = ext_map.get(req.type, f"*.{req.type}")
        cmd_parts.extend(["--include", glob_pattern])

    cmd_parts.extend(["-m", str(req.max_results), "--", req.pattern, str(search_path)])

    try:
        process = await asyncio.create_subprocess_exec(
            *_runner_argv(cmd_parts),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_runner_env(),
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
        output = stdout.decode("utf-8", errors="replace")

        return {
            "output": output,
            "exit_code": process.returncode or 0,
            "error": stderr.decode("utf-8", errors="replace") if stderr else "",
        }
    except asyncio.TimeoutError:
        return {"output": "", "exit_code": -1, "error": "grep timed out after 30s"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- Execute Stream (SSE) ---
@app.post("/execute_stream")
async def execute_stream(req: ExecuteRequest, request: Request):
    """Execute a command with streaming output via SSE."""
    started = time.monotonic()
    await _validate_desktop_lease(request, req.command)
    blocked = _is_protected_command(req.command)
    if blocked:
        async def blocked_gen():
            yield {"event": "output", "data": json.dumps({
                "type": "system", "content": f"[BLOCKED] {blocked}\n",
            })}
            yield {"event": "exit", "data": json.dumps({
                "exit_code": 1, "timed_out": False,
            })}
        return EventSourceResponse(blocked_gen())
    workdir = _workspace_path(req.workdir or "/workspace", must_exist=True)

    async def event_generator():
        try:
            process = await asyncio.create_subprocess_exec(
                *_runner_shell_argv(req.command),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(workdir),
                start_new_session=True,
                env=_runner_env(),
            )
        except Exception as e:
            yield {"event": "error", "data": json.dumps({"message": str(e)})}
            return

        import json as json_mod

        # Emit started event with PID so the client can kill the process later
        yield {"event": "started", "data": json_mod.dumps({"pid": process.pid})}

        # Global timeout killer: runs in parallel with the reader.
        # If the process exceeds the timeout, kill it so the reader can finish.
        timeout_seconds = req.timeout or 120
        idle_timeout_seconds = req.idle_timeout or 30
        timed_out_flag = False
        start_time = time.time()

        async def _timeout_killer():
            nonlocal timed_out_flag
            await asyncio.sleep(timeout_seconds)
            if process.returncode is None:  # still running
                timed_out_flag = True
                _kill_process_tree(process)

        timeout_task = asyncio.create_task(_timeout_killer())

        # Read both streams concurrently, but also watch for process exit.
        # When the shell process exits, give remaining output a short grace
        # period then stop — background processes (nohup, &) keep pipes open
        # indefinitely and would block forever otherwise.
        stdout_done = False
        stderr_done = False
        process_exited = False

        async def reader():
            nonlocal stdout_done, stderr_done, process_exited
            tasks = {}
            wait_task = asyncio.create_task(process.wait())
            last_output_time = time.time()

            while not (stdout_done and stderr_done):
                if not stdout_done and "stdout" not in tasks:
                    tasks["stdout"] = asyncio.create_task(process.stdout.readline())
                if not stderr_done and "stderr" not in tasks:
                    tasks["stderr"] = asyncio.create_task(process.stderr.readline())

                if not tasks:
                    break

                # Also watch for process exit
                waitables = set(tasks.values())
                if not process_exited:
                    waitables.add(wait_task)

                done, _ = await asyncio.wait(
                    waitables,
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=idle_timeout_seconds,
                )

                # Idle detection: no output for idle_timeout seconds
                if not done:
                    idle_secs = int(time.time() - last_output_time)
                    total_secs = int(time.time() - start_time)
                    yield {"event": "idle", "data": json_mod.dumps({
                        "idle_seconds": idle_secs,
                        "total_seconds": total_secs,
                    })}
                    continue  # Go back to waiting, don't kill

                if wait_task in done:
                    process_exited = True

                for name in list(tasks.keys()):
                    if tasks[name] in done:
                        line = tasks.pop(name).result()
                        if not line:
                            if name == "stdout":
                                stdout_done = True
                            else:
                                stderr_done = True
                        else:
                            last_output_time = time.time()
                            yield {"event": "output", "data": json_mod.dumps({
                                "type": name,
                                "content": line.decode("utf-8", errors="replace"),
                            })}

                # If process exited, drain remaining output with a short timeout
                if process_exited and not (stdout_done and stderr_done):
                    remaining = {k: v for k, v in tasks.items() if v not in done}
                    if remaining:
                        try:
                            drain_done, drain_pending = await asyncio.wait(
                                remaining.values(), timeout=0.5
                            )
                            for name in list(remaining.keys()):
                                if remaining[name] in drain_done:
                                    line = remaining[name].result()
                                    if line:
                                        yield {"event": "output", "data": json_mod.dumps({
                                            "type": name,
                                            "content": line.decode("utf-8", errors="replace"),
                                        })}
                            for t in drain_pending:
                                t.cancel()
                        except Exception:
                            pass
                    break

            if not wait_task.done():
                wait_task.cancel()

        try:
            async for event in reader():
                yield event

            if not process_exited:
                exit_code = await asyncio.wait_for(process.wait(), timeout=5)
            else:
                exit_code = process.returncode or 0
        except asyncio.TimeoutError:
            _kill_process_tree(process)
            try:
                await asyncio.wait_for(process.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
            exit_code = -1
        finally:
            timeout_task.cancel()
            # Closing the SSE connection (for example when the user presses
            # Stop) cancels this generator.  The shell otherwise survives on
            # the desktop until its timeout or the idle judge notices it.
            # Always terminate the process group before letting the generator
            # unwind; normal completions already have a return code and no-op.
            await _terminate_process_tree(process)

        if timed_out_flag:
            exit_code = -1
            yield {"event": "output", "data": json.dumps({
                "type": "system",
                "content": f"Command terminated: timeout {timeout_seconds}s exceeded\n",
            })}

        _emit_execute_trace(
            request,
            req.command,
            started=started,
            exit_code=exit_code,
        )

        yield {"event": "exit", "data": json.dumps({
            "exit_code": exit_code,
            "timed_out": exit_code == -1,
        })}

    return EventSourceResponse(event_generator())

# --- Listening Ports Detection ---
@app.get("/listening_ports")
async def listening_ports():
    """Detect TCP ports with services actively listening inside this container.

    Excludes the action server's own port and returns process info for each port.
    """
    result = []
    seen_ports = set()
    for conn in psutil.net_connections(kind="tcp"):
        # Only LISTEN state
        if conn.status != psutil.CONN_LISTEN:
            continue
        port = conn.laddr.port
        # Skip action server port and duplicates
        if port == ACTION_SERVER_PORT or port in seen_ports:
            continue
        # Only 0.0.0.0 or 127.0.0.1 bindings (not random ephemeral)
        if conn.laddr.ip not in ("0.0.0.0", "127.0.0.1", "::", "::1"):
            continue
        seen_ports.add(port)
        # Try to get process name
        proc_name = ""
        cmd = ""
        if conn.pid:
            try:
                p = psutil.Process(conn.pid)
                proc_name = p.name()
                cmd = " ".join(p.cmdline()[:5])  # first 5 args
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        result.append({
            "port": port,
            "pid": conn.pid,
            "process": proc_name,
            "command": cmd,
        })
    result.sort(key=lambda x: x["port"])
    return {"ports": result}


# --- Port Proxy (forward requests to user apps running inside container) ---
@app.api_route("/proxy/{port:int}/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
@app.api_route("/proxy/{port:int}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy_to_port(request: Request, port: int, path: str = ""):
    """Reverse-proxy requests to a user application running on the given port inside the container."""
    import httpx
    target_url = f"http://127.0.0.1:{port}/{path}"
    if request.url.query:
        target_url += f"?{request.url.query}"

    body = await request.body()
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "x-api-key", "connection")}

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
            )
        # Forward the response back
        excluded_headers = {"transfer-encoding", "connection", "content-encoding"}
        response_headers = {k: v for k, v in resp.headers.items()
                           if k.lower() not in excluded_headers}
        from starlette.responses import Response
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=response_headers,
            media_type=resp.headers.get("content-type"),
        )
    except httpx.ConnectError:
        return JSONResponse(status_code=502, content={"detail": f"No service running on port {port}"})
    except httpx.TimeoutException:
        return JSONResponse(status_code=504, content={"detail": f"Timeout connecting to port {port}"})


# --- PTY Terminal WebSocket ---
def _blocking_read(fd: int, size: int = 4096) -> bytes | None:
    """Blocking read from file descriptor, used in executor.
    Returns data bytes, empty bytes on timeout, or None on fd error."""
    r, _, _ = select.select([fd], [], [], 0.5)
    if r:
        try:
            return os.read(fd, size)
        except OSError:
            return None
    return b""


@app.websocket("/terminal")
async def terminal_ws(
    ws: WebSocket,
    api_key: str = Query(""),
    workdir: str = Query(""),
    user_scope: str = Query(""),
    prompt_label: str = Query(""),
):
    # Authenticate via query parameter (WebSocket doesn't go through HTTP middleware)
    if not _api_key_matches(api_key):
        await ws.accept()
        reason = (
            "Action Server API key is not configured"
            if not SESSION_API_KEY
            else "Invalid API Key"
        )
        await ws.close(code=4003, reason=reason)
        return

    try:
        terminal_workdir = _terminal_workspace_path(workdir, user_scope)
        account = _runner_account()
        if REQUIRE_RUNNER and account is None:
            raise RuntimeError("required terminal runner is unavailable")
    except HTTPException as exc:
        await ws.accept()
        await ws.close(
            code=4003 if exc.status_code == 403 else 4000,
            reason=str(exc.detail)[:120],
        )
        return
    except RuntimeError as exc:
        await ws.accept()
        await ws.close(code=1011, reason=str(exc)[:120])
        return

    await ws.accept()

    # Create PTY
    master_fd, slave_fd = pty.openpty()

    # Set initial terminal size (80x24)
    winsize = struct.pack("HHHH", 24, 80, 0, 0)
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)

    # Fork child process
    pid = os.fork()
    if pid == 0:
        # Child process
        os.close(master_fd)
        os.setsid()
        fcntl.ioctl(slave_fd, termios.TIOCSCTTY, 0)

        # Redirect stdio to slave PTY
        os.dup2(slave_fd, 0)
        os.dup2(slave_fd, 1)
        os.dup2(slave_fd, 2)
        if slave_fd > 2:
            os.close(slave_fd)

        _demote_to_runner()
        env = _runner_env(user_scope=user_scope)
        # The physical cwd contains pseudonymous tenant/project segments. Keep
        # it authoritative for execution while presenting the user-facing
        # project name in the prompt. Restrict characters because Bash expands
        # command substitutions inside PS1 when promptvars is enabled.
        safe_prompt_label = "".join(
            char if (char.isalnum() or char in " ._-") else "-"
            for char in str(prompt_label or "")[:80]
        ).strip() or "project"
        env["PS1"] = f"\\u@\\h:{safe_prompt_label}\\$ "

        os.chdir(str(terminal_workdir))
        # A login shell executes desktop-wide /etc/profile hooks (including
        # GUI dconf commands) even though this PTY is intentionally headless.
        # A clean interactive shell avoids those warnings and still inherits
        # the explicit, credential-free runtime environment above.
        os.execve("/bin/bash", ["bash", "--noprofile", "--norc", "-i"], env)

    # Parent process
    os.close(slave_fd)

    # Set master_fd to non-blocking
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

    loop = asyncio.get_running_loop()

    async def read_pty_to_ws():
        """Read from PTY master and send to WebSocket."""
        try:
            while True:
                try:
                    data = await loop.run_in_executor(None, _blocking_read, master_fd)
                    if data is None:
                        break  # fd error, PTY closed
                    if data:
                        await ws.send_bytes(b"\x00" + data)
                    # empty bytes means select timeout, continue loop
                except OSError:
                    break
        except Exception:
            pass

    async def read_ws_to_pty():
        """Read from WebSocket and write to PTY master."""
        try:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    break

                if "bytes" in message and message["bytes"]:
                    raw = message["bytes"]
                    if len(raw) < 1:
                        continue
                    prefix = raw[0]
                    payload = raw[1:]

                    if prefix == 0x00:
                        os.write(master_fd, payload)
                    elif prefix == 0x01:
                        if len(payload) >= 4:
                            cols = (payload[0] << 8) | payload[1]
                            rows = (payload[2] << 8) | payload[3]
                            winsize = struct.pack("HHHH", rows, cols, 0, 0)
                            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
                            os.kill(pid, signal.SIGWINCH)
                elif "text" in message and message["text"]:
                    pass
        except Exception:
            pass

    try:
        done, pending = await asyncio.wait(
            [asyncio.create_task(read_pty_to_ws()), asyncio.create_task(read_ws_to_pty())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        await asyncio.sleep(0.1)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            await ws.close()
        except Exception:
            pass


# ============================================================
# Dev-Browser Relay Management
# ============================================================

_dev_browser_process: subprocess.Popen | None = None


def _is_dev_browser_running() -> bool:
    """Check if the dev-browser relay process is alive."""
    global _dev_browser_process
    if _dev_browser_process is None:
        return False
    if _dev_browser_process.poll() is not None:
        _dev_browser_process = None
        return False
    return True


@app.post("/dev-browser/start")
async def dev_browser_start():
    """Start the dev-browser relay server in the background."""
    global _dev_browser_process
    if _is_dev_browser_running():
        return {"status": "running", "pid": _dev_browser_process.pid}

    relay_dir = BUILTIN_SKILLS_DIR / "dev-browser"
    if not relay_dir.exists():
        raise HTTPException(status_code=500, detail="dev-browser not installed in container")

    try:
        _dev_browser_process = subprocess.Popen(
            _runner_argv(["npm", "run", "start-relay"]),
            cwd=str(relay_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_runner_env({"HOST": "127.0.0.1", "PORT": "9222"}),
            start_new_session=True,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start relay: {e}")

    # Wait for port 9222 to become available (up to 5 seconds)
    import socket
    for _ in range(50):
        if _dev_browser_process.poll() is not None:
            _dev_browser_process = None
            raise HTTPException(status_code=500, detail="Relay process exited unexpectedly")
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.1)
                s.connect(("127.0.0.1", 9222))
                return {"status": "running", "pid": _dev_browser_process.pid}
        except (ConnectionRefusedError, OSError):
            await asyncio.sleep(0.1)

    return {"status": "starting", "pid": _dev_browser_process.pid}


@app.post("/dev-browser/stop")
async def dev_browser_stop():
    """Stop the dev-browser relay server."""
    global _dev_browser_process
    if not _is_dev_browser_running():
        return {"status": "stopped"}

    try:
        _dev_browser_process.terminate()
        try:
            _dev_browser_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _dev_browser_process.kill()
    except Exception:
        pass
    _dev_browser_process = None
    return {"status": "stopped"}


@app.get("/dev-browser/status")
async def dev_browser_status():
    """Get dev-browser relay status and extension connection state."""
    if not _is_dev_browser_running():
        return {"status": "stopped", "extensionConnected": False}

    # Query the relay server for extension connection status
    import httpx
    extension_connected = False
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get("http://127.0.0.1:9222/")
            if resp.status_code == 200:
                data = resp.json()
                extension_connected = data.get("extensionConnected", False)
    except Exception:
        pass

    return {
        "status": "running",
        "pid": _dev_browser_process.pid if _dev_browser_process else None,
        "extensionConnected": extension_connected,
    }


@app.websocket("/dev-browser/ws")
async def dev_browser_ws(ws: WebSocket, api_key: str = Query("")):
    """WebSocket proxy: forwards extension traffic to the relay server at localhost:9222."""
    # Authenticate
    if not _api_key_matches(api_key):
        await ws.accept()
        reason = (
            "Action Server API key is not configured"
            if not SESSION_API_KEY
            else "Invalid API Key"
        )
        await ws.close(code=4003, reason=reason)
        return

    await ws.accept()

    import websockets

    relay_url = "ws://127.0.0.1:9222/extension"
    try:
        async with websockets.connect(relay_url, max_size=2**20) as relay_ws:

            async def client_to_relay():
                try:
                    while True:
                        message = await ws.receive()
                        if message["type"] == "websocket.disconnect":
                            break
                        if "text" in message and message["text"]:
                            await relay_ws.send(message["text"])
                        elif "bytes" in message and message["bytes"]:
                            await relay_ws.send(message["bytes"])
                except Exception:
                    pass

            async def relay_to_client():
                try:
                    async for msg in relay_ws:
                        if isinstance(msg, str):
                            await ws.send_text(msg)
                        else:
                            await ws.send_bytes(msg)
                except Exception:
                    pass

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(client_to_relay()),
                    asyncio.create_task(relay_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

    except Exception as e:
        try:
            await ws.send_json({"type": "error", "data": f"Failed to connect to relay: {e}"})
        except Exception:
            pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# ============================================================
# Skill Management
# ============================================================

SKILLS_DIR = Path("/data/skills")            # User-installed skills (bind-mounted, persistent)
BUILTIN_SKILLS_DIR = Path("/opt/openbox/skills")  # System built-in skills (baked into image)
SKILL_EXPORTS_DIR = Path("/workspace/exports")
SKILL_PUBLISH_LOCKS_DIR = Path(
    os.environ.get("OPENBOX_SKILL_PUBLISH_LOCKS_DIR", "/tmp/openbox-skill-publish-locks")
)
SKILL_MUTATION_STATE_DIR = os.environ.get("OPENBOX_SKILL_MUTATION_STATE_DIR", "")


#: URL schemes accepted for skill installation.
#: Git's ``ext::`` transport runs an arbitrary shell command as part of the
#: clone, so an unrestricted URL is remote code execution rather than a
#: download. Clones additionally pass -c protocol.ext.allow=never as defence in
#: depth for redirects and submodules.
_SKILL_URL_SCHEMES = ("https://", "http://", "git://", "ssh://", "git@")


def _skill_mutation_key(skills_dir: Path, skill_name: str) -> str:
    return hashlib.sha256(
        f"{skills_dir.resolve()}\0{skill_name}".encode("utf-8")
    ).hexdigest()


@contextmanager
def _skill_publish_lock(skills_dir: Path, skill_name: str):
    """Serialize publication of one tenant Skill across server processes.

    The lock lives outside the runner-owned Skill tree, so sandbox commands
    cannot unlink a held lock and split future publishers into two lock
    domains. The resolved tenant root is part of the digest: two users may
    install the same slug without blocking one another, while uvicorn workers
    targeting the same user and slug share one advisory lock.
    """
    lock_root = SKILL_PUBLISH_LOCKS_DIR
    try:
        lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if lock_root.is_symlink() or not lock_root.is_dir():
            raise OSError("Skill publication lock root is not a directory")
        lock_root_stat = lock_root.stat()
        if lock_root_stat.st_uid != os.geteuid():
            raise OSError("Skill publication lock root has an unexpected owner")
        os.chmod(lock_root, 0o700)
        lock_key = _skill_mutation_key(skills_dir, skill_name)
        flags = os.O_CREAT | os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_root / f"{lock_key}.lock", flags, 0o600)
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail="Skill publication lock is unavailable",
        ) from exc

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _skill_exists_conflict(skill_name: str) -> HTTPException:
    """Return the stable wire error for create-if-absent conflicts."""
    return HTTPException(
        status_code=409,
        detail={
            "code": "skill_already_exists",
            "name": skill_name,
            "message": f"Skill '{skill_name}' already exists",
        },
    )


def _skill_mutation_state_path(skills_dir: Path, skill_name: str) -> Path:
    """Return a root-owned durable restore-fence record for one scoped slug."""
    root = (
        Path(SKILL_MUTATION_STATE_DIR)
        if SKILL_MUTATION_STATE_DIR
        else SKILLS_DIR.parent / ".openbox-skill-mutations"
    )
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise OSError("Skill mutation state root is not a directory")
        root_stat = root.stat()
        if root_stat.st_uid != os.geteuid():
            raise OSError("Skill mutation state root has an unexpected owner")
        os.chmod(root, 0o700)
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail="Skill mutation state is unavailable",
        ) from exc
    return root / f"{_skill_mutation_key(skills_dir, skill_name)}.json"


def _skill_restore_fence_generation(skills_dir: Path, skill_name: str) -> int:
    state_path = _skill_mutation_state_path(skills_dir, skill_name)
    if not state_path.exists():
        return 0
    try:
        if state_path.is_symlink():
            raise OSError("Skill mutation state cannot be a symlink")
        payload = json.loads(state_path.read_text(encoding="utf-8"))
        generation = payload.get("fenced_through_generation")
        if not isinstance(generation, int) or generation < 0:
            raise ValueError("invalid Skill mutation generation")
        return generation
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        # Corrupt fencing state must fail closed; treating it as generation 1
        # could allow an already-deleted snapshot to recreate the package.
        raise HTTPException(
            status_code=503,
            detail="Skill mutation state is invalid",
        ) from exc


def _advance_skill_restore_generation(
    skills_dir: Path,
    skill_name: str,
    generation: int,
) -> int:
    current = _skill_restore_fence_generation(skills_dir, skill_name)
    advanced = max(current, generation)
    if advanced == current:
        return current
    state_path = _skill_mutation_state_path(skills_dir, skill_name)
    temporary = state_path.parent / f".{state_path.name}.{secrets.token_hex(6)}.tmp"
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"fenced_through_generation": advanced}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, state_path)
        os.chmod(state_path, 0o600)
        directory_fd = os.open(
            state_path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise HTTPException(
            status_code=503,
            detail="Skill mutation state could not be persisted",
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)
    return advanced


def _skill_restore_fenced_conflict(
    skill_name: str,
    fenced_through_generation: int,
) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "skill_restore_fenced",
            "name": skill_name,
            "fenced_through_generation": fenced_through_generation,
            "message": "A newer durable uninstall fenced this Skill restore",
        },
    )


def _publish_skill_staging(
    skills_dir: Path,
    skill_name: str,
    staging: Path,
    *,
    create_only: bool,
    restore_generation: int | None = None,
) -> Path:
    """Atomically publish a validated staging directory under one name lock."""
    target = skills_dir / skill_name
    with _skill_publish_lock(skills_dir, skill_name):
        if restore_generation is not None:
            fenced_through_generation = _skill_restore_fence_generation(
                skills_dir,
                skill_name,
            )
            if restore_generation <= fenced_through_generation:
                raise _skill_restore_fenced_conflict(
                    skill_name,
                    fenced_through_generation,
                )
        if create_only and (target.exists() or target.is_symlink()):
            # This branch is deliberately non-destructive. In particular, it
            # must never share the explicit-update rmtree below.
            raise _skill_exists_conflict(skill_name)
        if not create_only and target.exists():
            shutil.rmtree(target)
        try:
            staging.replace(target)
        except OSError as exc:
            # Defence in depth for a non-cooperating writer. All Action Server
            # upload workers take the lock, but create-only still fails closed
            # if another process creates the target by some other route.
            if create_only and (target.exists() or target.is_symlink()):
                raise _skill_exists_conflict(skill_name) from exc
            raise
    return target


def _safe_skill_name(name: str) -> str:
    """Validate a skill directory name, or raise 400.

    The name is joined onto SKILLS_DIR and the result is both written to and
    (on reinstall or uninstall) removed with shutil.rmtree. ``Path('/data/skills')
    / '../../opt/openbox/skills'`` resolves cleanly outside the skills tree, so
    an unchecked name is an arbitrary-directory delete, not a naming nit.
    """
    cleaned = (name or "").strip().replace(" ", "-")
    if not cleaned:
        raise HTTPException(status_code=400, detail="Skill name is required")
    if cleaned in (".", "..") or cleaned.startswith("."):
        raise HTTPException(status_code=400, detail=f"Invalid skill name: {name!r}")
    if "/" in cleaned or "\\" in cleaned or "\x00" in cleaned:
        raise HTTPException(status_code=400, detail=f"Invalid skill name: {name!r}")
    # Belt and braces: confirm the join really lands inside this tenant's tree.
    skills_dir = _scoped_skill_root()
    try:
        resolved = (skills_dir / cleaned).resolve()
        if not resolved.is_relative_to(skills_dir.resolve()):
            raise HTTPException(status_code=400, detail=f"Invalid skill name: {name!r}")
    except HTTPException:
        raise
    except OSError:
        raise HTTPException(status_code=400, detail=f"Invalid skill name: {name!r}")
    return cleaned


def _validate_skill_url(url: str) -> str:
    """Reject clone URLs whose scheme can execute a command."""
    candidate = (url or "").strip()
    if not candidate:
        raise HTTPException(status_code=400, detail="Skill URL is required")
    if not candidate.startswith(_SKILL_URL_SCHEMES):
        raise HTTPException(
            status_code=400,
            detail=(
                "Unsupported skill URL. Use https://, http://, git://, ssh:// "
                "or git@host:path."
            ),
        )
    return candidate


def _run_skill_install_script(target: Path) -> str:
    """Run a skill's install.sh, returning its combined output.

    Shared by both install routes so a skill pack sets up its dependencies the
    same way whether it arrived as an archive or a git clone.
    """
    install_sh = target / "install.sh"
    if not install_sh.exists():
        return ""
    try:
        result = subprocess.run(
            _runner_argv(["bash", str(install_sh)]),
            capture_output=True, text=True, timeout=120,
            cwd=str(target),
            env=_runner_env(),
        )
        log = result.stdout + result.stderr
        if result.returncode != 0:
            log = f"install.sh exited with code {result.returncode}:\n{log}"
        return log
    except subprocess.TimeoutExpired:
        return "install.sh timed out (120s)"
    except Exception as e:
        return f"install.sh failed: {e}"


class InstallSkillRequest(BaseModel):
    url: str | None = None
    name: str | None = None
    content: str | None = None


class CreateSkillFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = PydanticField(min_length=1, max_length=240)
    content: str = PydanticField(max_length=512 * 1024)


class CreateSkillRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = PydanticField(min_length=1, max_length=64)
    skill_md: str = PydanticField(min_length=1, max_length=512 * 1024)
    files: list[CreateSkillFile] = PydanticField(default_factory=list, max_length=64)


# Chat-created skills are intentionally small, text-only packages. Keeping the
# limits here (rather than only in the caller) protects every future API client
# and prevents one request from filling the persistent per-user data volume.
_CREATED_SKILL_MAX_FILES = 64
_CREATED_SKILL_MAX_FILE_BYTES = 512 * 1024
_CREATED_SKILL_MAX_TOTAL_BYTES = 2 * 1024 * 1024
_CREATED_SKILL_MAX_PATH_BYTES = 240
_SKILL_ARCHIVE_MAX_FILES = 1_000
_SKILL_ARCHIVE_MAX_TOTAL_BYTES = 50 * 1024 * 1024
_SKILL_ARCHIVE_POLICY_VERSION = "bounded-zip-v1"
_SKILL_ARCHIVE_MAX_COMPRESSED_BYTES = 50 * 1024 * 1024
_SKILL_ARCHIVE_MAX_FILE_BYTES = 10 * 1024 * 1024
_SKILL_ARCHIVE_MAX_RATIO = 200
_SKILL_ARCHIVE_RATIO_MIN_BYTES = 1024 * 1024
_SKILL_ARCHIVE_MAX_PATH_BYTES = 512
_SKILL_ARCHIVE_MAX_DEPTH = 32
_SKILL_ARCHIVE_READ_CHUNK_BYTES = 64 * 1024
_SKILL_ARCHIVE_ALLOWED_COMPRESSION = {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
_SKILL_ARCHIVE_EOCD_SIGNATURE = b"PK\x05\x06"
_SKILL_ARCHIVE_CENTRAL_FILE_SIGNATURE = b"PK\x01\x02"
_SKILL_ARCHIVE_EOCD_FIXED_BYTES = 22
_SKILL_ARCHIVE_CENTRAL_FILE_FIXED_BYTES = 46
_STRICT_SKILL_SLUG = _re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_SECRET_SKILL_FILENAMES = {
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "password",
    "passwords",
    "secret",
    "secrets",
    "token",
    "tokens",
}
_SECRET_SKILL_SUFFIXES = (
    ".jks", ".key", ".keystore", ".p12", ".pem", ".pfx", ".secret",
)
_SECRET_SKILL_STEMS = {
    "access-key", "api-key", "api_key", "apikey", "credential", "credentials",
    "password", "passwords", "private-key", "secret", "secrets",
    "service-account", "token", "tokens",
}
_SECRET_SKILL_DIRNAMES = {"credentials", "keys", "private-keys", "secrets"}


def _skill_archive_reject(code: str, message: str, *, status_code: int = 400) -> None:
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


def _preflight_skill_zip_central_directory(content: bytes) -> None:
    """Bound raw central records before ``zipfile`` creates ZipInfo objects."""
    search_start = max(
        0,
        len(content) - (_SKILL_ARCHIVE_EOCD_FIXED_BYTES + 0xFFFF),
    )
    search_end = len(content)
    eocd_offset = -1
    while search_end > search_start:
        candidate = content.rfind(
            _SKILL_ARCHIVE_EOCD_SIGNATURE,
            search_start,
            search_end,
        )
        if candidate < 0:
            break
        if candidate + _SKILL_ARCHIVE_EOCD_FIXED_BYTES <= len(content):
            comment_size = struct.unpack_from("<H", content, candidate + 20)[0]
            if candidate + _SKILL_ARCHIVE_EOCD_FIXED_BYTES + comment_size == len(content):
                eocd_offset = candidate
                break
        search_end = candidate
    if eocd_offset < 0:
        _skill_archive_reject(
            "invalid_zip",
            "Skill ZIP has no valid end-of-directory record",
        )

    (
        _signature,
        disk_number,
        central_disk,
        entries_on_disk,
        total_entries,
        central_size,
        central_offset,
        _comment_size,
    ) = struct.unpack_from("<4s4H2LH", content, eocd_offset)
    if disk_number != 0 or central_disk != 0 or entries_on_disk != total_entries:
        _skill_archive_reject("multi_disk", "Multi-disk Skill ZIPs are not supported")
    if (
        total_entries == 0xFFFF
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
    ):
        _skill_archive_reject("zip64", "ZIP64 Skill archives are not supported")
    if total_entries > _SKILL_ARCHIVE_MAX_FILES:
        _skill_archive_reject("too_many_entries", "Skill ZIP contains too many entries")

    concatenated_prefix = eocd_offset - central_size - central_offset
    central_start = central_offset + concatenated_prefix
    central_end = central_start + central_size
    if central_start < 0 or central_end != eocd_offset:
        _skill_archive_reject(
            "invalid_zip",
            "Skill ZIP central directory has invalid bounds",
        )
    count = 0
    cursor = central_start
    while cursor < central_end:
        if (
            cursor + _SKILL_ARCHIVE_CENTRAL_FILE_FIXED_BYTES > central_end
            or content[cursor : cursor + 4] != _SKILL_ARCHIVE_CENTRAL_FILE_SIGNATURE
        ):
            _skill_archive_reject(
                "invalid_zip",
                "Skill ZIP central directory is malformed",
            )
        name_size, extra_size, comment_size = struct.unpack_from(
            "<3H", content, cursor + 28
        )
        if name_size > _SKILL_ARCHIVE_MAX_PATH_BYTES:
            _skill_archive_reject(
                "path_too_long",
                "Skill ZIP path exceeds the safety limit",
            )
        cursor += (
            _SKILL_ARCHIVE_CENTRAL_FILE_FIXED_BYTES
            + name_size
            + extra_size
            + comment_size
        )
        if cursor > central_end:
            _skill_archive_reject(
                "invalid_zip",
                "Skill ZIP central directory is truncated",
            )
        count += 1
        if count > _SKILL_ARCHIVE_MAX_FILES:
            _skill_archive_reject(
                "too_many_entries",
                "Skill ZIP contains too many entries",
            )
    if cursor != central_end or count != total_entries:
        _skill_archive_reject(
            "invalid_zip",
            "Skill ZIP central-directory count is inconsistent",
        )


def _safe_skill_archive_parts(name: str) -> tuple[str, ...]:
    """Return one unambiguous relative archive path or reject the package."""
    if not isinstance(name, str) or not name or "\x00" in name:
        _skill_archive_reject("unsafe_path", "Skill archive contains an invalid path")
    if "\\" in name or name.startswith("/"):
        _skill_archive_reject(
            "unsafe_path",
            "Skill archive paths must be relative POSIX paths",
        )
    try:
        encoded_size = len(name.encode("utf-8"))
    except UnicodeEncodeError:
        _skill_archive_reject("unsafe_path", "Skill archive path is not valid UTF-8 text")
    if encoded_size > _SKILL_ARCHIVE_MAX_PATH_BYTES:
        _skill_archive_reject("path_too_long", "Skill archive path exceeds the safety limit")

    raw_parts = name.split("/")
    if raw_parts[-1] == "":
        raw_parts = raw_parts[:-1]
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        _skill_archive_reject("unsafe_path", "Skill archive contains an ambiguous path")
    if len(raw_parts) > _SKILL_ARCHIVE_MAX_DEPTH:
        _skill_archive_reject("path_too_deep", "Skill archive nesting exceeds the safety limit")
    if raw_parts[0].endswith(":"):
        _skill_archive_reject("unsafe_path", "Skill archive contains a drive-qualified path")

    normalized = tuple(unicodedata.normalize("NFC", part) for part in raw_parts)
    if tuple(raw_parts) != normalized:
        _skill_archive_reject(
            "ambiguous_path",
            "Skill archive paths must use NFC Unicode normalization",
        )
    if PurePosixPath(*normalized).as_posix() != "/".join(normalized):
        _skill_archive_reject("unsafe_path", "Skill archive contains an ambiguous path")
    return normalized


def _register_skill_archive_member(
    seen: dict[str, str],
    explicit: set[str],
    parts: tuple[str, ...],
    *,
    is_dir: bool,
) -> None:
    """Reject duplicates and order-independent file/directory collisions."""
    keys = ["/".join(parts[:index]).casefold() for index in range(1, len(parts) + 1)]
    key = keys[-1]
    if key in explicit:
        _skill_archive_reject("duplicate_path", "Skill archive contains a duplicate path")
    for parent in keys[:-1]:
        if seen.get(parent) == "file":
            _skill_archive_reject(
                "path_collision",
                "Skill archive path traverses an archived file",
            )
        seen.setdefault(parent, "dir")
    kind = "dir" if is_dir else "file"
    existing = seen.get(key)
    if existing is not None and existing != kind:
        _skill_archive_reject(
            "path_collision",
            "Skill archive contains a file/directory collision",
        )
    if not is_dir and any(candidate.startswith(key + "/") for candidate in seen):
        _skill_archive_reject(
            "path_collision",
            "Skill archive file shadows an archived directory",
        )
    seen[key] = kind
    explicit.add(key)


def _preflight_skill_zip(
    zf: zipfile.ZipFile,
) -> tuple[list[tuple[zipfile.ZipInfo, tuple[str, ...]]], int]:
    infos = zf.infolist()
    if not infos:
        _skill_archive_reject("empty_archive", "Skill ZIP is empty")
    if len(infos) > _SKILL_ARCHIVE_MAX_FILES:
        _skill_archive_reject("too_many_entries", "Skill ZIP contains too many entries")

    seen: dict[str, str] = {}
    explicit: set[str] = set()
    members: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
    total_size = 0
    total_compressed = 0
    regular_files = 0
    for info in infos:
        if info.orig_filename != info.filename:
            _skill_archive_reject(
                "unsafe_path",
                "Skill ZIP path contains a NUL byte",
            )
        is_dir = info.is_dir()
        parts = _safe_skill_archive_parts(info.filename)
        _register_skill_archive_member(seen, explicit, parts, is_dir=is_dir)
        if len(seen) > _SKILL_ARCHIVE_MAX_FILES:
            _skill_archive_reject(
                "too_many_entries",
                "Skill ZIP expands to too many filesystem entries",
            )

        if info.flag_bits & 0x1:
            _skill_archive_reject("encrypted_entry", "Encrypted Skill ZIP entries are not supported")
        if info.compress_type not in _SKILL_ARCHIVE_ALLOWED_COMPRESSION:
            _skill_archive_reject(
                "unsupported_compression",
                "Skill ZIP uses an unsupported compression method",
            )
        unix_mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(unix_mode)
        if info.create_system == 3:
            if is_dir and file_type not in {0, stat.S_IFDIR}:
                _skill_archive_reject(
                    "special_entry",
                    "Skill ZIP directory has an unsafe file type",
                )
            if not is_dir and file_type not in {0, stat.S_IFREG}:
                _skill_archive_reject(
                    "special_entry",
                    "Skill ZIP links and special files are not allowed",
                )
        if is_dir:
            if info.file_size != 0:
                _skill_archive_reject(
                    "invalid_directory",
                    "Skill ZIP directory carries file data",
                )
            members.append((info, parts))
            continue

        regular_files += 1
        if info.file_size < 0 or info.compress_size < 0:
            _skill_archive_reject("invalid_size", "Skill ZIP contains an invalid entry size")
        if info.file_size > _SKILL_ARCHIVE_MAX_FILE_BYTES:
            _skill_archive_reject(
                "file_too_large",
                "Skill ZIP contains a file that exceeds the safety limit",
                status_code=413,
            )
        total_size += info.file_size
        total_compressed += info.compress_size
        if total_size > _SKILL_ARCHIVE_MAX_TOTAL_BYTES:
            _skill_archive_reject(
                "archive_too_large",
                "Skill ZIP expands beyond the total safety limit",
                status_code=413,
            )
        if (
            info.file_size >= _SKILL_ARCHIVE_RATIO_MIN_BYTES
            and (
                info.compress_size == 0
                or info.file_size > info.compress_size * _SKILL_ARCHIVE_MAX_RATIO
            )
        ):
            _skill_archive_reject(
                "compression_ratio",
                "Skill ZIP entry has an unsafe compression ratio",
                status_code=413,
            )
        members.append((info, parts))

    if regular_files == 0:
        _skill_archive_reject("empty_archive", "Skill ZIP contains no regular files")
    if (
        total_size >= _SKILL_ARCHIVE_RATIO_MIN_BYTES
        and (
            total_compressed == 0
            or total_size > total_compressed * _SKILL_ARCHIVE_MAX_RATIO
        )
    ):
        _skill_archive_reject(
            "compression_ratio",
            "Skill ZIP has an unsafe aggregate compression ratio",
            status_code=413,
        )
    return members, total_size


def _extract_bounded_skill_zip(content: bytes, destination_root: Path) -> None:
    """Preflight, CRC-check, and stream one ZIP into a new private directory."""
    if len(content) > _SKILL_ARCHIVE_MAX_COMPRESSED_BYTES:
        _skill_archive_reject(
            "compressed_too_large",
            "Skill ZIP exceeds the compressed size limit",
            status_code=413,
        )
    try:
        _preflight_skill_zip_central_directory(content)
        with zipfile.ZipFile(BytesIO(content), mode="r") as zf:
            members, expected_total = _preflight_skill_zip(zf)
            actual_total = 0
            for info, parts in members:
                destination = destination_root.joinpath(*parts)
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                actual_file = 0
                with zf.open(info, mode="r") as source, destination.open("xb") as output:
                    while chunk := source.read(_SKILL_ARCHIVE_READ_CHUNK_BYTES):
                        actual_file += len(chunk)
                        actual_total += len(chunk)
                        if actual_file > _SKILL_ARCHIVE_MAX_FILE_BYTES:
                            _skill_archive_reject(
                                "file_too_large",
                                "Skill ZIP stream exceeds the per-file limit",
                                status_code=413,
                            )
                        if actual_total > _SKILL_ARCHIVE_MAX_TOTAL_BYTES:
                            _skill_archive_reject(
                                "archive_too_large",
                                "Skill ZIP stream exceeds the total limit",
                                status_code=413,
                            )
                        output.write(chunk)
                if actual_file != info.file_size:
                    _skill_archive_reject(
                        "size_mismatch",
                        "Skill ZIP entry size does not match its directory record",
                    )
            if actual_total != expected_total:
                _skill_archive_reject(
                    "size_mismatch",
                    "Skill ZIP total size does not match its directory record",
                )
    except HTTPException:
        raise
    except (zipfile.BadZipFile, RuntimeError, OSError, EOFError, zlib.error):
        _skill_archive_reject(
            "invalid_zip",
            "Skill ZIP is corrupt or cannot be safely decoded",
        )


def _preflight_skill_tar(
    tf: tarfile.TarFile,
    *,
    compressed_bytes: int,
    compressed: bool,
) -> list[tarfile.TarInfo]:
    """Bound a TAR before extraction and reject all links/special entries."""
    members: list[tarfile.TarInfo] = []
    seen: dict[str, str] = {}
    explicit: set[str] = set()
    total_size = 0
    regular_files = 0
    for member in tf:
        members.append(member)
        if len(members) > _SKILL_ARCHIVE_MAX_FILES:
            _skill_archive_reject(
                "too_many_entries",
                "Skill archive contains too many entries",
            )
        is_dir = member.isdir()
        parts = _safe_skill_archive_parts(member.name + ("/" if is_dir and not member.name.endswith("/") else ""))
        _register_skill_archive_member(seen, explicit, parts, is_dir=is_dir)
        if len(seen) > _SKILL_ARCHIVE_MAX_FILES:
            _skill_archive_reject(
                "too_many_entries",
                "Skill archive expands to too many filesystem entries",
            )
        if is_dir:
            continue
        if not member.isfile():
            _skill_archive_reject(
                "special_entry",
                "Skill archive links and special files are not allowed",
            )
        regular_files += 1
        if member.size < 0 or member.size > _SKILL_ARCHIVE_MAX_FILE_BYTES:
            _skill_archive_reject(
                "file_too_large",
                "Skill archive contains a file that exceeds the safety limit",
                status_code=413,
            )
        total_size += member.size
        if total_size > _SKILL_ARCHIVE_MAX_TOTAL_BYTES:
            _skill_archive_reject(
                "archive_too_large",
                "Skill archive expands beyond the total safety limit",
                status_code=413,
            )
    if not members or regular_files == 0:
        _skill_archive_reject("empty_archive", "Skill archive contains no regular files")
    if (
        compressed
        and total_size >= _SKILL_ARCHIVE_RATIO_MIN_BYTES
        and (
            compressed_bytes == 0
            or total_size > compressed_bytes * _SKILL_ARCHIVE_MAX_RATIO
        )
    ):
        _skill_archive_reject(
            "compression_ratio",
            "Skill archive has an unsafe compression ratio",
            status_code=413,
        )
    return members


def _validate_extracted_skill_tree(root: Path) -> tuple[int, int]:
    """Postcondition for legacy extractors: only a bounded regular tree survives."""
    entries = 0
    total_size = 0
    seen: dict[str, str] = {}
    explicit: set[str] = set()
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in sorted(dirnames):
            path = current_path / name
            if path.is_symlink():
                _skill_archive_reject("special_entry", "Skill archive links are not allowed")
            parts = _safe_skill_archive_parts(path.relative_to(root).as_posix() + "/")
            _register_skill_archive_member(seen, explicit, parts, is_dir=True)
            entries += 1
        for name in sorted(filenames):
            path = current_path / name
            entry_stat = path.lstat()
            if not stat.S_ISREG(entry_stat.st_mode):
                _skill_archive_reject(
                    "special_entry",
                    "Skill archive special files are not allowed",
                )
            parts = _safe_skill_archive_parts(path.relative_to(root).as_posix())
            _register_skill_archive_member(seen, explicit, parts, is_dir=False)
            entries += 1
            if entry_stat.st_size > _SKILL_ARCHIVE_MAX_FILE_BYTES:
                _skill_archive_reject(
                    "file_too_large",
                    "Skill archive contains a file that exceeds the safety limit",
                    status_code=413,
                )
            total_size += entry_stat.st_size
            if total_size > _SKILL_ARCHIVE_MAX_TOTAL_BYTES:
                _skill_archive_reject(
                    "archive_too_large",
                    "Skill archive expands beyond the total safety limit",
                    status_code=413,
                )
        if entries > _SKILL_ARCHIVE_MAX_FILES:
            _skill_archive_reject(
                "too_many_entries",
                "Skill archive contains too many entries",
                status_code=413,
            )
    return entries, total_size


def _strict_skill_slug(name: str) -> str:
    """Validate the stable directory/store id used by a created skill."""
    candidate = (name or "").strip()
    if (
        candidate != name
        or len(candidate) > 64
        or not _STRICT_SKILL_SLUG.fullmatch(candidate)
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Skill name must be a lowercase slug of 1-64 letters and numbers "
                "separated by single hyphens"
            ),
        )
    # Keep the existing containment check as defence in depth. A future change
    # to the slug expression must not silently weaken the filesystem boundary.
    if _safe_skill_name(candidate) != candidate:
        raise HTTPException(status_code=400, detail=f"Invalid skill name: {name!r}")
    return candidate


def _is_secret_skill_path(path: PurePosixPath) -> bool:
    """Return whether a package path looks like credentials or key material."""
    if any(part.casefold() in _SECRET_SKILL_DIRNAMES for part in path.parts[:-1]):
        return True
    name = path.name.casefold()
    if name == ".env" or name.startswith(".env."):
        return True
    if name in _SECRET_SKILL_FILENAMES or name.endswith(_SECRET_SKILL_SUFFIXES):
        return True
    # Covers secrets.yaml, credentials.toml, token.json, and similar common
    # variants without rejecting ordinary documentation that merely mentions
    # authentication in its filename.
    stem = name.split(".", 1)[0]
    return stem in _SECRET_SKILL_STEMS


def _validated_created_skill_path(raw_path: str) -> PurePosixPath:
    """Validate one caller-supplied relative path without normalising it."""
    if not isinstance(raw_path, str) or raw_path != raw_path.strip():
        raise HTTPException(status_code=400, detail=f"Invalid skill file path: {raw_path!r}")
    if not raw_path or "\\" in raw_path or "\x00" in raw_path:
        raise HTTPException(status_code=400, detail=f"Invalid skill file path: {raw_path!r}")
    if len(raw_path.encode("utf-8")) > _CREATED_SKILL_MAX_PATH_BYTES:
        raise HTTPException(status_code=400, detail=f"Skill file path is too long: {raw_path!r}")

    # Inspect the raw components before PurePosixPath can collapse `.` or
    # duplicate separators. Rejecting rather than cleaning makes the API
    # unambiguous and closes both traversal and archive-name tricks.
    parts = raw_path.split("/")
    if raw_path.startswith("/") or any(part in ("", ".", "..") for part in parts):
        raise HTTPException(status_code=400, detail=f"Unsafe skill file path: {raw_path!r}")
    if any(part.startswith(".") for part in parts):
        raise HTTPException(status_code=400, detail=f"Hidden skill paths are not allowed: {raw_path!r}")
    if any(any(ord(ch) < 32 or ord(ch) == 127 for ch in part) for part in parts):
        raise HTTPException(status_code=400, detail=f"Invalid skill file path: {raw_path!r}")

    path = PurePosixPath(raw_path)
    lowered_name = path.name.casefold()
    if lowered_name == "skill.md":
        raise HTTPException(status_code=400, detail="SKILL.md must be supplied via skill_md")
    if lowered_name == "install.sh":
        raise HTTPException(status_code=400, detail="install.sh is not allowed in created skills")
    if _is_secret_skill_path(path):
        raise HTTPException(status_code=400, detail=f"Secret files are not allowed: {raw_path!r}")
    return path


def _validate_created_skill_markdown(skill_name: str, content: str) -> None:
    """Require a well-formed SKILL.md whose identity matches its directory."""
    if not isinstance(content, str):
        raise HTTPException(status_code=400, detail="skill_md must be text")
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise HTTPException(status_code=400, detail="SKILL.md must start with YAML frontmatter")
    try:
        closing = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise HTTPException(status_code=400, detail="SKILL.md frontmatter is not closed")
    try:
        metadata = yaml.safe_load("\n".join(lines[1:closing])) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid SKILL.md frontmatter: {exc}")
    if not isinstance(metadata, dict):
        raise HTTPException(status_code=400, detail="SKILL.md frontmatter must be a mapping")
    if metadata.get("name") != skill_name:
        raise HTTPException(
            status_code=400,
            detail="SKILL.md frontmatter name must exactly match the skill directory name",
        )
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise HTTPException(
            status_code=400,
            detail="SKILL.md frontmatter requires a non-empty description of at most 1024 characters",
        )
    if not "\n".join(lines[closing + 1:]).strip():
        raise HTTPException(status_code=400, detail="SKILL.md must contain instructions after frontmatter")


def _validate_created_skill_files(files: list[CreateSkillFile]) -> list[tuple[PurePosixPath, str]]:
    if len(files) > _CREATED_SKILL_MAX_FILES:
        raise HTTPException(status_code=400, detail="Too many skill files")

    validated: list[tuple[PurePosixPath, str]] = []
    seen: set[str] = set()
    for item in files:
        path = _validated_created_skill_path(item.path)
        key = path.as_posix()
        if key in seen:
            raise HTTPException(status_code=400, detail=f"Duplicate skill file path: {key!r}")
        size = len(item.content.encode("utf-8"))
        if size > _CREATED_SKILL_MAX_FILE_BYTES:
            raise HTTPException(status_code=400, detail=f"Skill file is too large: {key!r}")
        seen.add(key)
        validated.append((path, item.content))

    # A file cannot also be another file's parent. Catch this before any write
    # so malformed packages never leave a partial staging tree behind.
    for key in seen:
        parts = PurePosixPath(key).parts
        for index in range(1, len(parts)):
            if PurePosixPath(*parts[:index]).as_posix() in seen:
                raise HTTPException(status_code=400, detail=f"Conflicting skill file path: {key!r}")
    return validated


def _normalize_requires_mcp(value) -> list[str]:
    """Read a skill's declared MCP dependencies into a list of server names.

    Authors write this either as a YAML list or as one comma-separated string,
    and the key itself appears both hyphenated and underscored in the wild, so
    all four spellings resolve to the same thing rather than silently yielding
    a skill that installs without the server it needs.
    """
    if not value:
        return []
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace(",", " ").split()]
    elif isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item.strip())
            elif isinstance(item, dict) and item.get("name"):
                parts.append(str(item["name"]).strip())
    else:
        return []
    seen, out = set(), []
    for p in parts:
        if p and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _parse_skill_frontmatter(md_content: str) -> dict:
    """Parse YAML frontmatter from a SKILL.md file.

    Beyond name/description this carries the two fields the skill centre needs:
    an ``icon`` (an emoji, so there is no asset to serve or cache) and
    ``requires-mcp``, the MCP servers the skill's instructions actually call.
    A skill whose dependency is missing loads fine and then fails at the first
    tool call, which is why the dependency has to be declarable.
    """
    md_content = md_content.strip()
    empty = {"name": "", "description": "", "icon": "", "requires_mcp": [], "homepage": ""}
    if not md_content.startswith("---"):
        return dict(empty)
    parts = md_content.split("---", 2)
    if len(parts) < 3:
        return dict(empty)
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    return {
        "name": meta.get("name", ""),
        "description": meta.get("description", ""),
        "icon": str(meta.get("icon", "") or "")[:8],
        "requires_mcp": _normalize_requires_mcp(
            meta.get("requires-mcp") or meta.get("requires_mcp")
            or meta.get("requiresMcp") or meta.get("mcp")
        ),
        "homepage": str(meta.get("homepage", "") or "")[:300],
    }


def _ensure_skill_symlinks() -> None:
    """Create name-based symlinks for skills whose frontmatter name differs from the install directory.

    For example, if a skill is installed at /data/skills/ui-ux-pro-max-skill/
    but the SKILL.md name is "ui-ux-pro-max", create:
      /data/skills/ui-ux-pro-max -> /data/skills/ui-ux-pro-max-skill/.claude/skills/ui-ux-pro-max/

    This makes paths like "skills/ui-ux-pro-max/scripts/search.py" work from /workspace/.
    """
    skills_dir = _scoped_skill_root()
    if not skills_dir.exists():
        return
    for skill_dir in skills_dir.iterdir():
        if not skill_dir.is_dir():
            continue
        for skill_md in _find_skill_mds(skill_dir):
            skill_data_dir = skill_md.parent
            meta = _parse_skill_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
            skill_name = meta.get("name") or skill_data_dir.name
            # If the name differs from the install dir, create a convenience symlink
            if skill_name != skill_dir.name:
                link_path = skills_dir / skill_name
                if not link_path.exists():
                    try:
                        link_path.symlink_to(skill_data_dir)
                    except OSError:
                        pass


# Directories and files that are never worth listing to the model or shipping
# over the tunnel. dev-browser alone carries ~890 node_modules entries, which
# crowded the genuinely useful files (scripts/, references/) out of a 50-line
# listing and made GET /skills a 55KB response on every agent step.
_SKILL_FILE_SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv",
                         "venv", "dist", "build", ".next", ".cache"}
_SKILL_FILE_LIST_LIMIT = 20    # /skills — the UI shows three and a count
_SKILL_FILE_DETAIL_LIMIT = 50  # /skills/{name} — what the model sees


def _skill_files(skill_data_dir, limit: int = _SKILL_FILE_DETAIL_LIMIT) -> list:
    """Files bundled with a skill, minus the noise.

    SKILL.md is excluded because its content is inlined right above the
    listing. Dotfiles and macOS AppleDouble stubs (._foo, created when a skill
    is zipped on a Mac) are excluded because they are not addressable content.
    """
    out = []
    try:
        for f in sorted(skill_data_dir.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(skill_data_dir)
            parts = rel.parts
            if any(p in _SKILL_FILE_SKIP_DIRS for p in parts):
                continue
            if any(p.startswith(".") or p.startswith("._") for p in parts):
                continue
            if rel.name == "SKILL.md":
                continue
            out.append(str(rel))
            if len(out) >= limit:
                break
    except OSError:
        pass
    return out


def _find_skill_mds(skill_dir: Path) -> list[Path]:
    """Find all SKILL.md files in a skill directory.

    Supports three layouts:
    1. Simple: {skill_dir}/SKILL.md
    2. Claude Code plugin: {skill_dir}/.claude/skills/*/SKILL.md
    3. Skills collection: {skill_dir}/skills/*/SKILL.md
    """
    results = []
    # Simple layout
    simple = skill_dir / "SKILL.md"
    if simple.exists():
        results.append(simple)
    # Claude Code plugin layout
    claude_skills = skill_dir / ".claude" / "skills"
    if claude_skills.is_dir():
        for sub in sorted(claude_skills.iterdir()):
            md = sub / "SKILL.md"
            if sub.is_dir() and md.exists():
                results.append(md)
    # Skills collection layout: skills/*/SKILL.md
    skills_dir = skill_dir / "skills"
    if skills_dir.is_dir():
        for sub in sorted(skills_dir.iterdir()):
            md = sub / "SKILL.md"
            if sub.is_dir() and md.exists():
                results.append(md)
    return results


def _scan_skills_in_dir(skills_dir: Path, source: str) -> list[dict]:
    """Scan a single directory for skills. Skips symlinks to avoid duplicates."""
    skills = []
    if not skills_dir.exists():
        return skills
    for skill_dir in sorted(skills_dir.iterdir()):
        # Skip symlinks (they are aliases created by _ensure_skill_symlinks,
        # the real skills are found via _find_skill_mds on actual directories)
        if not skill_dir.is_dir() or skill_dir.is_symlink():
            continue
        # Dot-directories are bookkeeping, not skills — an install in flight
        # stages into `.<name>.incoming` and must not surface as a half-written
        # skill while it is being swapped in.
        if skill_dir.name.startswith("."):
            continue
        skill_mds = _find_skill_mds(skill_dir)
        if not skill_mds:
            continue
        for skill_md in skill_mds:
            content = skill_md.read_text(encoding="utf-8", errors="replace")
            meta = _parse_skill_frontmatter(content)
            skill_data_dir = skill_md.parent
            files = _skill_files(skill_data_dir, _SKILL_FILE_LIST_LIMIT)
            skills.append({
                "name": meta.get("name") or skill_data_dir.name,
                "description": meta.get("description", ""),
                "icon": meta.get("icon", ""),
                "requires_mcp": meta.get("requires_mcp", []),
                "homepage": meta.get("homepage", ""),
                "source": source,
                "content": content,
                "files": files,
                "install_dir": skill_dir.name,
                "base_dir": str(skill_data_dir),
            })
    return skills


def _scan_skills() -> list[dict]:
    """Scan both system and user skill directories.

    System skills (/opt/openbox/skills/) are baked into the image, source="builtin".
    User skills (/data/skills/) are installed by the user, source="container".
    If same name exists in both, user version takes precedence.
    """
    builtin = _scan_skills_in_dir(BUILTIN_SKILLS_DIR, source="builtin")
    user = _scan_skills_in_dir(_scoped_skill_root(), source="container")

    # Merge: user skills take precedence over builtin with same name
    seen_names = set()
    merged = []
    for skill in user:
        seen_names.add(skill["name"])
        merged.append(skill)
    for skill in builtin:
        if skill["name"] not in seen_names:
            merged.append(skill)
    return merged


def _stable_catalogue_digest(value) -> str:
    """Hash one normalized directory view without leaking its source bytes."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _skill_package_digest(skill: dict) -> tuple[str, int]:
    """Hash addressable files in one skill while returning no file bodies.

    The projection must notice script/reference edits as well as SKILL.md
    edits. Files are streamed into the digest and symlinks/noise directories
    are ignored using the same rules as the public skill listing.
    """
    digest = hashlib.sha256()
    count = 0
    try:
        base_dir = str(skill.get("base_dir") or "")
        if not base_dir:
            raise OSError("skill root is missing")
        root = Path(base_dir).resolve(strict=True)
        if not root.is_dir():
            raise OSError("skill root is not a directory")
        for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root)
            if any(
                part in _SKILL_FILE_SKIP_DIRS or part.startswith(".")
                for part in relative.parts
            ):
                continue
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root):
                continue
            relative_bytes = relative.as_posix().encode("utf-8")
            digest.update(len(relative_bytes).to_bytes(8, "big"))
            digest.update(relative_bytes)
            with resolved.open("rb") as stream:
                while chunk := stream.read(64 * 1024):
                    digest.update(chunk)
            count += 1
    except (OSError, ValueError):
        # A concurrently removed package still gets a deterministic fallback;
        # the next request rescans and publishes the stable post-mutation view.
        fallback = {
            "content": str(skill.get("content") or ""),
            "files": sorted(str(item) for item in (skill.get("files") or [])),
        }
        return _stable_catalogue_digest(fallback), len(fallback["files"]) + 1
    return digest.hexdigest(), count


def _skill_catalogue_projection(skills: list[dict] | None = None) -> dict:
    """Return bounded Skill metadata plus a content-derived generation."""
    source = _scan_skills() if skills is None else skills
    projected = []
    for skill in source:
        package_digest, file_count = _skill_package_digest(skill)
        projected.append({
            "name": str(skill.get("name") or ""),
            "description": str(skill.get("description") or "")[:500],
            "icon": str(skill.get("icon") or "")[:8],
            "requires_mcp": [
                str(item)[:200] for item in (skill.get("requires_mcp") or [])[:20]
            ],
            "homepage": str(skill.get("homepage") or "")[:300],
            "source": str(skill.get("source") or ""),
            "install_dir": str(skill.get("install_dir") or ""),
            "file_count": file_count,
            "package_digest": package_digest,
        })
    projected.sort(key=lambda item: (
        item["name"], item["source"], item["install_dir"], item["package_digest"]
    ))
    return {
        "items": projected,
        "count": len(projected),
        "generation": _stable_catalogue_digest(projected),
    }


def _catalogue_etag(generation: str) -> str:
    return f'"{generation}"'


def _etag_matches(request: Request, etag: str) -> bool:
    supplied = request.headers.get("if-none-match", "")
    for candidate in supplied.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:].strip()
        if candidate == etag:
            return True
    return False


def _catalogue_json_response(request: Request, payload, generation: str) -> Response:
    etag = _catalogue_etag(generation)
    headers = {"ETag": etag, "Cache-Control": "no-cache"}
    if _etag_matches(request, etag):
        return Response(status_code=304, headers=headers)
    return JSONResponse(content=payload, headers=headers)


def _user_skill_directory(name: str) -> tuple[str, Path]:
    """Resolve a chat-created user skill, never a builtin or alias."""
    skill_name = _strict_skill_slug(name)
    target = _scoped_skill_root() / skill_name
    skill_md = target / "SKILL.md"
    if (
        target.is_symlink()
        or not target.is_dir()
        or skill_md.is_symlink()
        or not skill_md.is_file()
    ):
        raise HTTPException(status_code=404, detail=f"User skill '{skill_name}' not found")
    return skill_name, target


def _skip_skill_archive_path(relative: Path) -> bool:
    parts = relative.parts
    if any(part.startswith(".") for part in parts):
        return True
    if any(part in _SKILL_FILE_SKIP_DIRS for part in parts):
        return True
    # Uploaded archives execute a root install.sh for legacy compatibility.
    # A user-published snapshot must therefore never carry that filename, even
    # if the source skill predates the safe chat-creation endpoint.
    if relative.name.casefold() == "install.sh":
        return True
    return _is_secret_skill_path(PurePosixPath(relative.as_posix()))


def _skill_archive_bytes(name: str) -> tuple[str, bytes]:
    """Build a bounded, symlink-free ZIP with one top-level skill directory."""
    skill_name, target = _user_skill_directory(name)
    target_root = target.resolve()
    files: list[tuple[Path, bytes]] = []
    total_size = 0
    # Every stored filename is prefixed with the implicit top-level Skill dir.
    tree_entries = 1

    for current, dirnames, filenames in os.walk(target, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_dirs = []
        for dirname in sorted(dirnames):
            child = current_path / dirname
            relative = child.relative_to(target)
            if child.is_symlink() or _skip_skill_archive_path(relative):
                continue
            kept_dirs.append(dirname)
            tree_entries += 1
            if tree_entries > _SKILL_ARCHIVE_MAX_FILES:
                raise HTTPException(status_code=413, detail="Skill contains too many entries to archive")
        dirnames[:] = kept_dirs

        for filename in sorted(filenames):
            source = current_path / filename
            relative = source.relative_to(target)
            if _skip_skill_archive_path(relative):
                continue
            descriptor = None
            try:
                entry_stat = source.lstat()
                if not stat.S_ISREG(entry_stat.st_mode):
                    continue
                resolved = source.resolve(strict=True)
                if not resolved.is_relative_to(target_root):
                    continue
                # O_NONBLOCK prevents a malicious FIFO swapped into place from
                # hanging the whole action server; fstat below still requires
                # the opened object itself to be a regular file.
                flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                descriptor = os.open(source, flags)
                file_stat = os.fstat(descriptor)
                if not stat.S_ISREG(file_stat.st_mode):
                    continue
                size = file_stat.st_size
                tree_entries += 1
                if tree_entries > _SKILL_ARCHIVE_MAX_FILES:
                    raise HTTPException(status_code=413, detail="Skill contains too many entries to archive")
                if size > _SKILL_ARCHIVE_MAX_FILE_BYTES:
                    raise HTTPException(status_code=413, detail="Skill contains a file that is too large to archive")
                if total_size + size > _SKILL_ARCHIVE_MAX_TOTAL_BYTES:
                    raise HTTPException(status_code=413, detail="Skill is too large to archive")
                with os.fdopen(descriptor, "rb", closefd=True) as input_file:
                    descriptor = None
                    content = input_file.read(size + 1)
                if len(content) != size:
                    raise HTTPException(status_code=409, detail="Skill changed while it was being archived")
            except OSError:
                continue
            finally:
                if descriptor is not None:
                    os.close(descriptor)
            files.append((relative, content))
            total_size += size

    archive = BytesIO()
    with zipfile.ZipFile(
        archive,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as bundle:
        for relative, content in files:
            archive_name = (PurePosixPath(skill_name) / PurePosixPath(relative.as_posix())).as_posix()
            # Fixed metadata makes the package a content snapshot: exporting
            # unchanged files twice must yield the same checksum, otherwise a
            # harmless download looks like a new unpublished version.
            entry = zipfile.ZipInfo(archive_name, date_time=(1980, 1, 1, 0, 0, 0))
            compressed = zlib.compress(content, 6)
            entry.compress_type = (
                zipfile.ZIP_STORED
                if len(content) >= _SKILL_ARCHIVE_RATIO_MIN_BYTES
                and len(content) > max(1, len(compressed)) * _SKILL_ARCHIVE_MAX_RATIO
                else zipfile.ZIP_DEFLATED
            )
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            bundle.writestr(entry, content, compresslevel=6)
    result = archive.getvalue()
    if len(result) > _SKILL_ARCHIVE_MAX_COMPRESSED_BYTES:
        raise HTTPException(status_code=413, detail="Skill archive exceeds the compressed size limit")
    # The producer must never emit a snapshot that its restore path rejects.
    # Reuse both bounded directory preflights before persistence; CRC streaming
    # is unnecessary here because these bytes were generated in this process.
    _preflight_skill_zip_central_directory(result)
    with zipfile.ZipFile(BytesIO(result), mode="r") as bundle:
        _preflight_skill_zip(bundle)
    return skill_name, result


@app.get("/skills")
async def list_skills(request: Request):
    """List all installed skills (builtin + user)."""
    skills = _scan_skills()
    generation = _skill_catalogue_projection(skills)["generation"]
    return _catalogue_json_response(request, skills, generation)


@app.post("/skills/create")
async def create_skill(req: CreateSkillRequest):
    """Atomically create one validated, text-only user skill package."""
    skill_name = _strict_skill_slug(req.name)
    _validate_created_skill_markdown(skill_name, req.skill_md)
    validated_files = _validate_created_skill_files(req.files)

    skill_md_size = len(req.skill_md.encode("utf-8"))
    if skill_md_size > _CREATED_SKILL_MAX_FILE_BYTES:
        raise HTTPException(status_code=400, detail="SKILL.md is too large")
    total_size = skill_md_size + sum(len(content.encode("utf-8")) for _, content in validated_files)
    if total_size > _CREATED_SKILL_MAX_TOTAL_BYTES:
        raise HTTPException(status_code=400, detail="Skill package is too large")

    skills_dir = _scoped_skill_root()
    _ensure_runner_directory(skills_dir)
    target = skills_dir / skill_name

    staging = skills_dir / f".{skill_name}.{secrets.token_hex(6)}.incoming"
    try:
        staging.mkdir(mode=0o700)
        (staging / "SKILL.md").write_text(req.skill_md, encoding="utf-8")
        for relative, content in validated_files:
            destination = staging.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")

        _chown_runner_tree(staging)

        target = _publish_skill_staging(
            skills_dir,
            skill_name,
            staging,
            create_only=True,
        )
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    created = next(
        (
            skill for skill in _scan_skills_in_dir(skills_dir, source="container")
            if skill.get("install_dir") == skill_name and skill.get("name") == skill_name
        ),
        None,
    )
    if not created:
        # This should be unreachable because the request was validated before
        # publication. Do not delete here: a concurrent explicit update may
        # have legitimately replaced this generation after the name lock was
        # released, and cleanup must never remove that newer package.
        raise HTTPException(status_code=500, detail="Created skill could not be discovered")
    return {**created, "created": True}


@app.get("/skills/{name}")
async def get_skill(name: str):
    """Get a specific skill by name."""
    all_skills = _scan_skills()
    for skill in all_skills:
        if skill["name"] == name or skill.get("install_dir") == name:
            return skill
    raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")


@app.get("/skills/{name}/archive")
async def download_skill_archive(name: str):
    """Download a clean ZIP snapshot of a user-created skill."""
    skill_name, content = _skill_archive_bytes(name)
    filename = f"{skill_name}.zip"
    return Response(
        content=content,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(content)),
        },
    )


@app.post("/skills/{name}/export")
async def export_skill_archive(name: str):
    """Write a clean ZIP snapshot into the user's workspace exports folder."""
    skill_name, content = _skill_archive_bytes(name)
    exports_dir = _scoped_export_root()
    if exports_dir.is_symlink():
        raise HTTPException(status_code=400, detail="Skill export directory cannot be a symlink")
    _ensure_runner_directory(exports_dir)
    if not exports_dir.is_dir():
        raise HTTPException(status_code=500, detail="Skill export directory is unavailable")

    filename = f"{skill_name}.zip"
    destination = exports_dir / filename
    staging = exports_dir / f".{filename}.{secrets.token_hex(6)}.tmp"
    try:
        with staging.open("xb") as output:
            output.write(content)
        staging.replace(destination)
    finally:
        staging.unlink(missing_ok=True)
    return {
        "path": str(destination),
        "filename": filename,
        "size": len(content),
    }


@app.post("/skills/install")
async def install_skill(req: InstallSkillRequest):
    """Install a skill from URL (git clone) or from pasted content."""
    skills_dir = _scoped_skill_root()
    _ensure_runner_directory(skills_dir)

    install_log = ""

    if req.url:
        url = _validate_skill_url(req.url)
        # Determine name from URL or explicit name
        skill_name = req.name
        if not skill_name:
            # Extract name from git URL: https://github.com/user/repo.git -> repo
            url_path = url.rstrip("/")
            if url_path.endswith(".git"):
                url_path = url_path[:-4]
            skill_name = url_path.split("/")[-1] or "unnamed-skill"
        skill_name = _safe_skill_name(skill_name)
        target = skills_dir / skill_name
        # Clone into a staging directory and swap it in only once it is whole.
        # Removing the old copy up front meant a failed clone left the user with
        # neither the new skill nor the one they already had.
        staging = skills_dir / f".{skill_name}.{secrets.token_hex(6)}.incoming"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        try:
            result = subprocess.run(
                _runner_argv([
                    "git",
                    # ext:: hands the URL to a shell; no skill install needs it.
                    "-c", "protocol.ext.allow=never",
                    "-c", "protocol.file.allow=never",
                    "clone", "--depth=1", "--", url, str(staging),
                ]),
                capture_output=True, text=True, timeout=60,
                env=_runner_env(),
            )
            if result.returncode != 0:
                shutil.rmtree(staging, ignore_errors=True)
                raise HTTPException(
                    status_code=400,
                    detail=f"git clone failed: {result.stderr.strip()}",
                )
            # Remove .git directory to save space
            git_dir = staging / ".git"
            if git_dir.exists():
                shutil.rmtree(git_dir)
            if not _find_skill_mds(staging):
                shutil.rmtree(staging, ignore_errors=True)
                raise HTTPException(status_code=400, detail="No SKILL.md found after install")
            _chown_runner_tree(staging)
            target = _publish_skill_staging(
                skills_dir,
                skill_name,
                staging,
                create_only=False,
            )
        except subprocess.TimeoutExpired:
            shutil.rmtree(staging, ignore_errors=True)
            raise HTTPException(status_code=504, detail="git clone timed out")
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
        # A cloned skill pack gets the same dependency setup an uploaded archive
        # does; running install.sh on only one of the two install routes made
        # the same skill work as a zip and fail from git.
        install_log = _run_skill_install_script(target)
    elif req.content:
        skill_name = req.name
        if not skill_name:
            # Try to extract name from frontmatter
            meta = _parse_skill_frontmatter(req.content)
            skill_name = meta.get("name") or "unnamed-skill"
        skill_name = _safe_skill_name(skill_name)
        target = skills_dir / skill_name
        with _skill_publish_lock(skills_dir, skill_name):
            target.mkdir(parents=True, exist_ok=True)
            (target / "SKILL.md").write_text(req.content, encoding="utf-8")
            _chown_runner_tree(target)
    else:
        raise HTTPException(status_code=400, detail="Provide either 'url' or 'content'")

    # Create name-based symlinks so skill paths work from /workspace/
    _ensure_skill_symlinks()

    # Read back and return the installed skill(s)
    skill_mds = _find_skill_mds(target)
    if not skill_mds:
        raise HTTPException(status_code=400, detail="No SKILL.md found after install")
    # Return the first skill found (most repos have one)
    skill_md = skill_mds[0]
    content = skill_md.read_text(encoding="utf-8", errors="replace")
    meta = _parse_skill_frontmatter(content)
    skill_data_dir = skill_md.parent
    files = _skill_files(skill_data_dir)
    return {
        "name": meta.get("name") or skill_name,
        "description": meta.get("description", ""),
        "source": "container",
        "content": content,
        "files": files,
        "install_dir": skill_name,
        "base_dir": str(skill_data_dir),
        "install_log": install_log,
    }


@app.post("/skills/upload")
async def upload_skill_archive(
    file: UploadFile = File(...),
    name: str = Form(""),
    create_only: bool = Form(False),
    restore_generation: int | None = Form(None),
):
    """Install a bounded skill archive (zip/tar/tar.gz/tgz/rar).

    Every native archive is preflighted before extraction. ZIPs are then
    decoded member-by-member with an independently enforced byte budget; this
    keeps automatic personal-Skill restore safe even if a durable snapshot or
    a caller has forged its central directory.
    """
    skills_dir = _scoped_skill_root()
    _ensure_runner_directory(skills_dir)

    filename = file.filename or "archive"
    ext = filename.lower()

    # Determine archive type
    if ext.endswith(".zip"):
        archive_type = "zip"
    elif ext.endswith((".tar.gz", ".tgz")):
        archive_type = "tar.gz"
    elif ext.endswith(".tar"):
        archive_type = "tar"
    elif ext.endswith(".rar"):
        archive_type = "rar"
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported archive format: {filename}. Use zip, tar, tar.gz, tgz, or rar.")

    # Read at most one byte beyond the wire budget. UploadFile is disk-spooled,
    # but an unbounded read would still copy an attacker-controlled body into
    # the Action Server process before the old size check ran.
    content_bytes = await file.read(_SKILL_ARCHIVE_MAX_COMPRESSED_BYTES + 1)
    if len(content_bytes) > _SKILL_ARCHIVE_MAX_COMPRESSED_BYTES:
        _skill_archive_reject(
            "compressed_too_large",
            "Skill archive exceeds the compressed size limit",
            status_code=413,
        )

    # Determine skill name
    skill_name = name.strip() if name.strip() else filename.rsplit(".", 1)[0]
    # Clean up double extensions like .tar.gz
    for suffix in (".tar", ".tgz"):
        if skill_name.endswith(suffix):
            skill_name = skill_name[:-len(suffix)]
    # Both the explicit name and the uploaded filename are caller-controlled and
    # end up joined onto SKILLS_DIR, where the existing copy is rmtree'd.
    skill_name = _safe_skill_name(skill_name)
    if restore_generation is not None and (
        not create_only or restore_generation < 1
    ):
        raise HTTPException(
            status_code=400,
            detail="restore_generation requires create_only and must be positive",
        )

    # Extract to temp dir first for validation
    scope_label = _current_user_scope() or "legacy"
    tmp_dir = Path("/tmp") / (
        f"skill_upload_{scope_label}_{skill_name}_{secrets.token_hex(6)}"
    )
    tmp_dir.mkdir(parents=True)

    staging: Path | None = None
    try:
        # Extract archive
        if archive_type == "zip":
            _extract_bounded_skill_zip(content_bytes, tmp_dir)

        elif archive_type in ("tar", "tar.gz"):
            mode = "r:gz" if archive_type == "tar.gz" else "r:"
            try:
                with tarfile.open(fileobj=BytesIO(content_bytes), mode=mode) as tf:
                    tar_members = _preflight_skill_tar(
                        tf,
                        compressed_bytes=len(content_bytes),
                        compressed=archive_type == "tar.gz",
                    )
                    # filter="data" remains defense in depth after links and
                    # special entries were rejected during bounded preflight.
                    tf.extractall(tmp_dir, members=tar_members, filter="data")
            except HTTPException:
                raise
            except (tarfile.TarError, OSError, EOFError):
                _skill_archive_reject(
                    "invalid_archive",
                    "Skill archive is corrupt or cannot be safely decoded",
                )

        elif archive_type == "rar":
            # RAR has no decoder in Python's standard library. Keep legacy
            # compatibility, but run quietly under a short wall-clock budget
            # and enforce the same filesystem postcondition before publication.
            # Personal snapshots and restores are always the fully preflighted
            # ZIP format above.
            tmp_file = tmp_dir / "archive.rar"
            tmp_file.write_bytes(content_bytes)
            try:
                result = subprocess.run(
                    _runner_argv([
                        "unrar", "x", "-y", "-idq", str(tmp_file), str(tmp_dir) + "/",
                    ]),
                    capture_output=True, text=True, timeout=30,
                    env=_runner_env(),
                )
                if result.returncode != 0:
                    raise HTTPException(status_code=400, detail=f"RAR extraction failed: {result.stderr.strip()[:500]}")
            except FileNotFoundError:
                raise HTTPException(status_code=400, detail="RAR not supported: 'unrar' not installed in container")
            finally:
                tmp_file.unlink(missing_ok=True)

        _entries, extracted_bytes = _validate_extracted_skill_tree(tmp_dir)
        if (
            archive_type == "rar"
            and extracted_bytes >= _SKILL_ARCHIVE_RATIO_MIN_BYTES
            and extracted_bytes > len(content_bytes) * _SKILL_ARCHIVE_MAX_RATIO
        ):
            _skill_archive_reject(
                "compression_ratio",
                "Skill archive has an unsafe compression ratio",
                status_code=413,
            )

        # If archive extracted into a single subdirectory, use that as root
        entries = list(tmp_dir.iterdir())
        extract_root = tmp_dir
        if len(entries) == 1 and entries[0].is_dir():
            extract_root = entries[0]

        # Validate: must have SKILL.md in standard layout
        skill_mds = _find_skill_mds(extract_root)
        if not skill_mds:
            found_files = [str(f.relative_to(extract_root)) for f in extract_root.rglob("*") if f.is_file()][:20]
            raise HTTPException(
                status_code=400,
                detail=f"No SKILL.md found in archive. Expected SKILL.md at root, skills/*/SKILL.md, or .claude/skills/*/SKILL.md.\nFiles found: {', '.join(found_files)}",
            )

        # Validation passed — move to skills directory. Stage the new copy
        # beside the target first so a failed move cannot leave the user with
        # the old skill deleted and nothing in its place.
        staging = skills_dir / f".{skill_name}.{secrets.token_hex(6)}.incoming"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        shutil.move(str(extract_root), str(staging))
        _chown_runner_tree(staging)
        target = _publish_skill_staging(
            skills_dir,
            skill_name,
            staging,
            create_only=create_only,
            restore_generation=restore_generation,
        )

    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # Run install.sh if present (for dependency setup)
    install_log = _run_skill_install_script(target)

    # Create symlinks
    _ensure_skill_symlinks()

    # Read README if present
    readme = ""
    for readme_name in ("README.md", "readme.md", "README.txt"):
        readme_path = target / readme_name
        if readme_path.exists():
            readme = readme_path.read_text(encoding="utf-8", errors="replace")[:2000]
            break

    # Return all installed skills
    skill_mds = _find_skill_mds(target)
    all_skills = []
    for skill_md in skill_mds:
        md_content = skill_md.read_text(encoding="utf-8", errors="replace")
        meta = _parse_skill_frontmatter(md_content)
        skill_data_dir = skill_md.parent
        files = _skill_files(skill_data_dir)
        all_skills.append({
            "name": meta.get("name") or skill_md.parent.name,
            "description": meta.get("description", ""),
            "source": "container",
            "content": md_content,
            "files": files,
            "base_dir": str(skill_data_dir),
        })

    return {
        "name": skill_name,
        "skills": all_skills,
        "skills_count": len(all_skills),
        "install_dir": skill_name,
        "install_log": install_log,
        "readme": readme,
    }


@app.delete("/skills/{name}")
async def uninstall_skill(
    name: str,
    mutation_generation: int | None = None,
):
    """Uninstall a user-installed skill. Builtin skills cannot be deleted."""
    # Check if it's a builtin skill — reject deletion
    builtin_skills = _scan_skills_in_dir(BUILTIN_SKILLS_DIR, source="builtin")
    for skill in builtin_skills:
        if skill["name"] == name or skill.get("install_dir") == name:
            raise HTTPException(status_code=403, detail=f"Cannot uninstall builtin skill '{name}'")

    # Direct match by directory name in user skills. The name is joined onto
    # SKILLS_DIR and handed to rmtree, so it has to be checked first.
    name = _safe_skill_name(name)
    if mutation_generation is not None and mutation_generation < 1:
        raise HTTPException(
            status_code=400,
            detail="mutation_generation must be positive",
        )
    skills_dir = _scoped_skill_root()
    target = skills_dir / name
    with _skill_publish_lock(skills_dir, name):
        applied_generation = None
        if mutation_generation is not None:
            # Persist the fence before deletion. A stale restore either
            # publishes first and is removed below, or arrives later and is
            # rejected while holding this same name lock.
            applied_generation = _advance_skill_restore_generation(
                skills_dir,
                name,
                mutation_generation,
            )
        if target.exists() and not target.is_symlink():
            shutil.rmtree(target)
            _cleanup_broken_symlinks()
            return {
                "ok": True,
                "message": f"Skill '{name}' uninstalled",
                "mutation_generation": applied_generation,
            }

        # If it's a symlink (alias), remove the symlink.
        if target.is_symlink():
            target.unlink()
            return {
                "ok": True,
                "message": f"Skill alias '{name}' removed",
                "mutation_generation": applied_generation,
            }

        # A fenced deletion is idempotent. The durable tombstone must still be
        # allowed to commit when the live package was already absent.
        if mutation_generation is not None:
            return {
                "ok": True,
                "message": f"Skill '{name}' was already absent",
                "already_absent": True,
                "mutation_generation": applied_generation,
            }

    # Search by skill name (from frontmatter) in user skills only
    user_skills = _scan_skills_in_dir(skills_dir, source="container")
    for skill in user_skills:
        if skill["name"] == name:
            install_dir = skill.get("install_dir")
            if install_dir:
                t = skills_dir / install_dir
                with _skill_publish_lock(skills_dir, install_dir):
                    if t.exists():
                        shutil.rmtree(t)
                        _cleanup_broken_symlinks()
                        return {"ok": True, "message": f"Skill '{name}' uninstalled"}
    raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")


def _cleanup_broken_symlinks():
    """Remove broken symlinks in SKILLS_DIR after a skill directory is deleted."""
    skills_dir = _scoped_skill_root()
    if not skills_dir.exists():
        return
    for entry in skills_dir.iterdir():
        if entry.is_symlink() and not entry.exists():
            entry.unlink()


# ============================================================
# MCP Server Management
# ============================================================

MCP_CONFIG_PATH = Path("/data/mcp/config.json")

#: Environment variables never handed to a stdio MCP subprocess.
#: An MCP server is third-party code — usually an npx package chosen by whoever
#: added it. SESSION_API_KEY authenticates every caller of this action server,
#: so passing it down would let that package drive the sandbox as the backend.
#: The cloud credentials are here for the same reason: nothing an MCP server
#: does should be able to reach the account that owns the sandbox.
_MCP_ENV_DENYLIST = frozenset({
    "SESSION_API_KEY",
    # Runtime identity/caches are selected by the tenant scope. Letting a
    # package redirect them can collide with or inspect another tenant's state
    # on the shared acceptance desktop.
    "HOME",
    "USER",
    "LOGNAME",
    "XDG_CACHE_HOME",
    "NPM_CONFIG_CACHE",
    # Model provider keys bill to whoever owns the account, so an MCP server
    # that can read one can spend real money. Verified reachable: an
    # `@modelcontextprotocol/server-everything` child listed ANTHROPIC_AUTH_TOKEN
    # among its inherited variables before this list covered it.
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "DEEPSEEK_API_KEY",
    "MOONSHOT_API_KEY",
    "DASHSCOPE_API_KEY",
    "ALIBABA_CLOUD_ACCESS_KEY_ID",
    "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
    "ALIBABA_CLOUD_SECURITY_TOKEN",
    "ALICLOUD_ACCESS_KEY_ID",
    "ALICLOUD_ACCESS_KEY_SECRET",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "JWT_SECRET",
    "DATABASE_URL",
    "POSTGRES_PASSWORD",
    "REDIS_URL",
})


class AddMcpServerRequest(BaseModel):
    name: str
    type: str = "stdio"          # "stdio" or "remote"
    command: str | None = None   # for stdio
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None       # for remote
    headers: dict[str, str] | None = None  # Custom HTTP headers for remote
    timeout: int = 60                      # Per-server request timeout


class CallMcpToolRequest(BaseModel):
    arguments: dict = {}


class ReadMcpResourceRequest(BaseModel):
    server: str
    uri: str


class GetMcpPromptRequest(BaseModel):
    server: str
    name: str
    arguments: dict | None = None


class RawStreamableHttpSession:
    """Raw HTTP-based MCP client that bypasses the SDK's buggy streamablehttp_client.

    Implements MCP JSON-RPC over HTTP POST. The server responds with either:
    - Direct JSON (application/json) for simple request/response
    - SSE stream (text/event-stream) for streaming responses

    This works around a known issue where the SDK's session.initialize() hangs
    on servers like Tavily that use Streamable HTTP transport.
    """

    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: int = 60,
        *,
        notification_handler=None,
    ):
        self.url = url
        self._session_id: str | None = None
        self._request_id = 0
        self._server_info: dict = {}
        self._client: "httpx.AsyncClient | None" = None
        self._extra_headers = headers or {}
        self._timeout = timeout
        self._notification_handler = notification_handler
        self._notification_context = None
        self._notification_response = None
        self._notification_iterator = None
        self._notification_data_lines: list[str] = []
        self._notification_receiver_live = False
        self._notification_receiver_error: str | None = None
        self._closing = False

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _ensure_client(self):
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(timeout=float(self._timeout))

    def _request_headers(self) -> dict[str, str]:
        """Build one authenticated header set for requests and notifications."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._extra_headers,
        }
        if self._session_id:
            # The negotiated server value is authoritative; configured custom
            # headers must not pin a stale transport session.
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    async def _send_request(self, method: str, params: dict | None = None) -> dict:
        """Send a JSON-RPC request and return the result."""
        await self._ensure_client()

        req_id = self._next_id()
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        resp = await self._client.post(
            self.url,
            json=payload,
            headers=self._request_headers(),
        )
        resp.raise_for_status()

        # Store session ID from response
        if "mcp-session-id" in resp.headers:
            self._session_id = resp.headers["mcp-session-id"]

        content_type = resp.headers.get("content-type", "")

        if "text/event-stream" in content_type:
            # Parse SSE response to extract JSON-RPC result
            return await self._parse_sse_response(resp.text, req_id)
        else:
            # Direct JSON response
            data = resp.json()
            await self._dispatch_notification(data)
            if "error" in data:
                raise Exception(f"MCP error: {data['error']}")
            return data.get("result", {})

    async def _parse_sse_response(self, text: str, expected_id: int) -> dict:
        """Parse SSE text to find the JSON-RPC response matching our request ID."""
        response: dict | None = None
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                data_str = line[5:].strip()
                if not data_str:
                    continue
                try:
                    data = json.loads(data_str)
                    await self._dispatch_notification(data)
                    if data.get("id") == expected_id:
                        if "error" in data:
                            raise Exception(f"MCP error: {data['error']}")
                        response = data.get("result", {})
                except json.JSONDecodeError:
                    continue
        if response is None:
            raise Exception("No matching JSON-RPC response found in SSE stream")
        return response

    async def _dispatch_notification(self, message) -> None:
        """Forward protocol list-changed notifications to the owner task."""
        if not isinstance(message, dict) or "id" in message:
            return
        method = str(message.get("method") or "")
        if method not in {
            "notifications/tools/list_changed",
            "notifications/resources/list_changed",
            "notifications/prompts/list_changed",
        }:
            return
        if self._notification_handler is None:
            return
        result = self._notification_handler(method)
        if inspect.isawaitable(result):
            await result

    def supports_list_changed(self, kind: str) -> bool:
        capabilities = self._server_info.get("capabilities") or {}
        section = capabilities.get(kind) if isinstance(capabilities, dict) else None
        if not isinstance(section, dict):
            return False
        value = section.get("listChanged", section.get("list_changed", False))
        return bool(value)

    @property
    def notification_receiver_live(self) -> bool:
        return self._notification_receiver_live

    @property
    def notification_receiver_error(self) -> str | None:
        return self._notification_receiver_error

    async def start_notification_receiver(self) -> bool:
        """Open the Streamable HTTP server-to-client GET channel when offered.

        Streamable HTTP notifications are delivered on a long-lived GET using
        the negotiated session id. The configured authentication headers are
        deliberately rebuilt for this request just like they are for POST
        requests and the initialized notification.
        """
        if self._notification_handler is None:
            return False
        if not any(
            self.supports_list_changed(kind)
            for kind in ("tools", "resources", "prompts")
        ):
            return False
        await self._ensure_client()
        if not hasattr(self._client, "stream"):
            self._notification_receiver_error = "HTTP client has no streaming GET support"
            return False
        context = self._client.stream(
            "GET",
            self.url,
            headers=self._request_headers(),
        )
        try:
            async with _same_task_timeout(float(self._timeout)):
                response = await context.__aenter__()
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                raise RuntimeError(
                    "MCP notification GET did not return text/event-stream"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._notification_receiver_error = (
                str(exc).strip() or type(exc).__name__
            )
            try:
                async with _same_task_timeout(float(self._timeout)):
                    await context.__aexit__(type(exc), exc, exc.__traceback__)
            except Exception:
                pass
            return False
        self._notification_context = context
        self._notification_response = response
        self._notification_iterator = response.aiter_lines().__aiter__()
        self._notification_data_lines.clear()
        self._notification_receiver_error = None
        self._notification_receiver_live = True
        return True

    async def receive_notification(self, timeout: float) -> bool:
        """Read at most one SSE line in the current MCP owner task.

        The owner checks its command queue between reads, so a persistent GET
        never starves tool calls. ``asyncio.timeout`` cancels this coroutine in
        place rather than wrapping the transport receive in a different task.
        """
        if not self._notification_receiver_live or self._notification_iterator is None:
            return False
        try:
            async with _same_task_timeout(max(0.001, float(timeout))):
                line = await anext(self._notification_iterator)
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            raise
        except StopAsyncIteration:
            self._notification_receiver_error = "MCP notification stream ended"
            await self.stop_notification_receiver()
            return False
        except Exception as exc:
            self._notification_receiver_error = (
                str(exc).strip() or type(exc).__name__
            )
            await self.stop_notification_receiver()
            return False

        if not line:
            if self._notification_data_lines:
                try:
                    await self._dispatch_notification(
                        json.loads("\n".join(self._notification_data_lines))
                    )
                except json.JSONDecodeError:
                    pass
                self._notification_data_lines.clear()
            return True
        if line.startswith("data:"):
            self._notification_data_lines.append(line[5:].lstrip())
        return True

    async def stop_notification_receiver(self) -> None:
        context = self._notification_context
        self._notification_receiver_live = False
        self._notification_context = None
        self._notification_response = None
        self._notification_iterator = None
        self._notification_data_lines.clear()
        if context is None:
            return
        try:
            async with _same_task_timeout(float(self._timeout)):
                await context.__aexit__(None, None, None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._notification_receiver_error is None:
                self._notification_receiver_error = (
                    str(exc).strip() or type(exc).__name__
                )

    async def _send_notification(self, method: str, params: dict | None = None):
        """Send a JSON-RPC notification (no id, no response expected)."""
        await self._ensure_client()

        payload = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        resp = await self._client.post(
            self.url,
            json=payload,
            headers=self._request_headers(),
        )
        # Notifications may return 200 or 204
        if resp.status_code not in (200, 202, 204):
            resp.raise_for_status()
        # Update session ID if present
        if "mcp-session-id" in resp.headers:
            self._session_id = resp.headers["mcp-session-id"]

    async def initialize(self):
        """Initialize the MCP session."""
        result = await self._send_request("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "openbox-container", "version": "1.0.0"},
        })
        self._server_info = result
        # Send initialized notification
        await self._send_notification("notifications/initialized")

    async def list_tools_page(self, cursor: str | None = None) -> dict:
        params = {"cursor": cursor} if cursor else {}
        return await self._send_request("tools/list", params)

    async def list_tools(self) -> list[dict]:
        """List available tools from the MCP server."""
        result = await self.list_tools_page()
        return result.get("tools", [])

    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """Call a tool on the MCP server."""
        result = await self._send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        return result

    async def list_resources_page(self, cursor: str | None = None) -> dict:
        params = {"cursor": cursor} if cursor else {}
        return await self._send_request("resources/list", params)

    async def list_resources(self) -> list[dict]:
        """List available resources from the MCP server."""
        result = await self.list_resources_page()
        return result.get("resources", [])

    async def read_resource(self, uri: str) -> dict:
        """Read a specific resource by URI."""
        return await self._send_request("resources/read", {"uri": uri})

    async def list_prompts_page(self, cursor: str | None = None) -> dict:
        params = {"cursor": cursor} if cursor else {}
        return await self._send_request("prompts/list", params)

    async def list_prompts(self) -> list[dict]:
        """List available prompts from the MCP server."""
        result = await self.list_prompts_page()
        return result.get("prompts", [])

    async def get_prompt(self, name: str, arguments: dict | None = None) -> dict:
        """Get a specific prompt by name."""
        params: dict = {"name": name}
        if arguments:
            params["arguments"] = arguments
        return await self._send_request("prompts/get", params)

    async def close(self):
        """Close the HTTP client."""
        self._closing = True
        await self.stop_notification_receiver()
        if self._client:
            async with _same_task_timeout(float(self._timeout)):
                await self._client.aclose()
            self._client = None


def _mcp_attr(obj, *names, default=None):
    """Read the first attribute in ``names`` that ``obj`` actually has.

    The MCP Python SDK renamed its model fields to snake_case in 2.0
    (Tool.inputSchema -> input_schema, Resource.mimeType -> mime_type,
    CallToolResult.isError -> is_error). Reading only the camelCase name made
    every tool arrive with an empty schema — so the model had no parameters to
    fill in and every call failed validation — and made isError read as False
    on results that were errors, reporting failures as successes. Accepting
    both keeps one action server working against either SDK line.
    """
    for n in names:
        if hasattr(obj, n):
            value = getattr(obj, n)
            if value is not None:
                return value
    return default


@dataclass(frozen=True)
class _McpCatalogueGeneration:
    """One immutable, all-or-nothing server catalogue generation."""

    tools: tuple[dict, ...] = ()
    resources: tuple[dict, ...] = ()
    prompts: tuple[dict, ...] = ()
    generation: str = ""
    list_changed: dict[str, str] = field(default_factory=dict)


class _McpCatalogueKindView(MutableMapping):
    """Compatibility mapping backed by the manager's atomic generations.

    A few focused tests and older extensions inject fixture catalogues through
    ``manager._tools[name]``. Keeping that small surface avoids a flag day,
    while production refreshes publish all three kinds with one pointer swap.
    """

    def __init__(self, manager: "ContainerMcpManager", kind: str):
        self._manager = manager
        self._kind = kind

    def __getitem__(self, key):
        generation = self._manager._catalogue_state[key]
        return list(getattr(generation, self._kind))

    def __setitem__(self, key, value):
        self._manager._replace_catalogue_kind(str(key), self._kind, value)

    def __delitem__(self, key):
        self._manager._replace_catalogue_kind(str(key), self._kind, None)

    def __iter__(self):
        return iter(self._manager._catalogue_state)

    def __len__(self):
        return len(self._manager._catalogue_state)

    def pop(self, key, default=None):
        if key not in self._manager._catalogue_state:
            return default
        old = self[key]
        self._manager._replace_catalogue_kind(str(key), self._kind, None)
        return old


@dataclass
class _McpOwnerCommand:
    operation: str
    args: tuple
    future: asyncio.Future


class _McpServerOwner:
    """The sole task allowed to own and operate one MCP transport session."""

    def __init__(self, manager: "ContainerMcpManager", name: str):
        self.manager = manager
        self.name = name
        self.queue: asyncio.Queue[_McpOwnerCommand] = asyncio.Queue(
            maxsize=manager.command_queue_size
        )
        loop = asyncio.get_running_loop()
        self.ready: asyncio.Future = loop.create_future()
        self.refresh_requested = asyncio.Event()
        self.stop_requested = asyncio.Event()
        self.task = asyncio.create_task(
            self._run(),
            name=f"openbox-mcp-owner:{manager.user_scope or 'legacy'}:{name}",
        )
        self._active_command: _McpOwnerCommand | None = None

    def request_stop(self) -> None:
        self.stop_requested.set()
        if not self.task.done():
            # Cancellation is the stop fence for a slow initialize, request or
            # refresh. The task's finally block still exits the transport in
            # this same owner task and completes every queued waiter.
            self.task.cancel()

    async def stop(self) -> None:
        self.request_stop()
        try:
            await self.task
        except asyncio.CancelledError:
            pass

    async def submit(self, operation: str, *args):
        if self.task.done() or self.stop_requested.is_set():
            raise RuntimeError(f"MCP server '{self.name}' supervisor is stopped")
        future = asyncio.get_running_loop().create_future()
        command = _McpOwnerCommand(operation=operation, args=args, future=future)
        try:
            self.queue.put_nowait(command)
        except asyncio.QueueFull as exc:
            raise RuntimeError(
                f"MCP server '{self.name}' request queue is full"
            ) from exc
        return await future

    def list_changed(self, _method: str = "") -> None:
        # Event.set is idempotent, so a notification burst results in one full
        # refresh and can never publish a tools/resources/prompts mixed state.
        self.refresh_requested.set()

    async def _sleep_backoff(self, failures: int) -> None:
        base = min(
            self.manager.backoff_max_seconds,
            self.manager.backoff_initial_seconds * (2 ** max(0, failures - 1)),
        )
        jitter = base * self.manager.backoff_jitter_ratio
        delay = max(0.0, base + random.uniform(-jitter, jitter))
        if delay:
            await asyncio.sleep(delay)

    async def _run(self) -> None:
        failures = 0
        connected_since: float | None = None
        try:
            while self.manager._connection_still_desired(self.name):
                self.manager._set_server_state(
                    self.name,
                    status="connecting" if failures == 0 else "reconnecting",
                    error=None if failures == 0 else (
                        self.manager._servers.get(self.name, {}).get("error")
                    ),
                    consecutive_failures=failures,
                    reconnect_exhausted=False,
                )
                try:
                    session_factory = self.manager._session
                    try:
                        supports_handler = "notification_handler" in inspect.signature(
                            session_factory
                        ).parameters
                    except (TypeError, ValueError):
                        supports_handler = True
                    session_context = (
                        session_factory(
                            self.name,
                            notification_handler=self.list_changed,
                        )
                        if supports_handler
                        else session_factory(self.name)
                    )
                    async with session_context as session:
                        timeout = self.manager._timeout(
                            self.manager._server_config(self.name)
                        )
                        async with _same_task_timeout(timeout):
                            discovered = await self.manager._discover(
                                self.name, session
                            )
                        if not self.manager._connection_still_desired(self.name):
                            if not self.ready.done():
                                self.ready.set_result(False)
                            return
                        modes = await self.manager._notification_modes(session)
                        if isinstance(discovered, _McpCatalogueGeneration):
                            discovered = _McpCatalogueGeneration(
                                tools=discovered.tools,
                                resources=discovered.resources,
                                prompts=discovered.prompts,
                                generation=discovered.generation,
                                list_changed=modes,
                            )
                            self.manager._publish_catalogue(self.name, discovered)
                        elif self.name not in self.manager._catalogue_state:
                            # Compatibility for a monkeypatched discovery hook
                            # that intentionally publishes through the legacy
                            # views but returned no value.
                            self.manager._publish_catalogue(
                                self.name,
                                _McpCatalogueGeneration(list_changed=modes),
                            )
                        else:
                            self.manager._set_catalogue_modes(self.name, modes)

                        connected_since = time.monotonic()
                        self.manager._set_server_state(
                            self.name,
                            status="connected",
                            error=None,
                            consecutive_failures=0,
                            reconnect_exhausted=False,
                            connected_at=datetime.now(timezone.utc).isoformat(),
                            list_changed=modes,
                            catalogue_generation=(
                                self.manager._catalogue_state[self.name].generation
                            ),
                        )
                        if not self.ready.done():
                            self.ready.set_result(True)
                        await self._serve(session, modes)
                        if self.stop_requested.is_set():
                            return
                        raise ConnectionError("MCP session ended unexpectedly")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if not self.manager._connection_still_desired(self.name):
                        if not self.ready.done():
                            self.ready.set_result(False)
                        return
                    if (
                        connected_since is not None
                        and time.monotonic() - connected_since
                        >= self.manager.stable_window_seconds
                    ):
                        failures = 0
                    connected_since = None
                    failures += 1
                    error = str(exc).strip() or type(exc).__name__
                    exhausted = failures >= self.manager.reconnect_failure_budget
                    self.manager._set_server_state(
                        self.name,
                        status="error" if exhausted else "reconnecting",
                        error=error,
                        consecutive_failures=failures,
                        reconnect_exhausted=exhausted,
                        last_failure_at=datetime.now(timezone.utc).isoformat(),
                        last_known_good=self.name in self.manager._catalogue_state,
                    )
                    if not self.ready.done():
                        self.ready.set_exception(exc)
                    if exhausted:
                        return
                    await self._sleep_backoff(failures)
        except asyncio.CancelledError:
            pass
        finally:
            if not self.ready.done():
                self.ready.set_result(False)
            stopped = RuntimeError(f"MCP server '{self.name}' supervisor stopped")
            if self._active_command is not None:
                self._finish_future(self._active_command.future, error=stopped)
                self._active_command = None
            while True:
                try:
                    command = self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self._finish_future(command.future, error=stopped)
                self.queue.task_done()

    @staticmethod
    def _finish_future(future: asyncio.Future, *, result=None, error=None) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)

    async def _serve(self, session, modes: dict[str, str]) -> None:
        poll_needed = any(value == "poll" for value in modes.values())
        next_poll = time.monotonic() + self.manager.notification_poll_seconds
        while not self.stop_requested.is_set():
            if isinstance(session, RawStreamableHttpSession):
                if (
                    any(value == "notification" for value in modes.values())
                    and not session.notification_receiver_live
                ):
                    # A receiver can fail after its initial scheduling turn.
                    # Downgrade only the capabilities that claimed listChanged;
                    # polling remains coalesced through the same refresh event.
                    modes = {
                        kind: ("poll" if value == "notification" else value)
                        for kind, value in modes.items()
                    }
                    poll_needed = any(value == "poll" for value in modes.values())
                    self.manager._set_catalogue_modes(self.name, modes)
                    self.manager._set_server_state(
                        self.name,
                        list_changed=modes,
                        notification_error=session.notification_receiver_error,
                    )

            now = time.monotonic()
            if self.refresh_requested.is_set() or (poll_needed and now >= next_poll):
                self.refresh_requested.clear()
                await self._refresh(session, modes, notify_waiter=None)
                next_poll = time.monotonic() + self.manager.notification_poll_seconds
                continue

            timeout = self.manager.owner_tick_seconds
            if poll_needed:
                timeout = min(timeout, max(0.01, next_poll - now))
            if (
                isinstance(session, RawStreamableHttpSession)
                and session.notification_receiver_live
            ):
                try:
                    command = self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    await session.receive_notification(timeout)
                    continue
            else:
                try:
                    async with _same_task_timeout(timeout):
                        command = await self.queue.get()
                except asyncio.TimeoutError:
                    continue
            self._active_command = command
            try:
                if command.future.cancelled():
                    continue
                if command.operation == "refresh":
                    await self._refresh(session, modes, notify_waiter=command.future)
                else:
                    cfg = self.manager._server_config(self.name)
                    async with _same_task_timeout(self.manager._timeout(cfg)):
                        result = await self.manager._dispatch_session_operation(
                            session, command.operation, command.args
                        )
                    self._finish_future(command.future, result=result)
            except asyncio.CancelledError:
                self._finish_future(
                    command.future,
                    error=RuntimeError(
                        f"MCP server '{self.name}' supervisor stopped"
                    ),
                )
                raise
            except Exception as exc:
                self._finish_future(command.future, error=exc)
                if self.manager._is_transport_failure(exc):
                    raise
                # A protocol/tool error does not prove that a persistent
                # transport died. Subsequent commands may still be valid.
            finally:
                self._active_command = None
                self.queue.task_done()

    async def _refresh(self, session, modes, notify_waiter: asyncio.Future | None):
        old = self.manager._catalogue_state.get(self.name)
        try:
            cfg = self.manager._server_config(self.name)
            async with _same_task_timeout(self.manager._timeout(cfg)):
                discovered = await self.manager._discover(self.name, session)
            if not self.manager._connection_still_desired(self.name):
                raise RuntimeError(f"MCP server '{self.name}' is no longer enabled")
            if isinstance(discovered, _McpCatalogueGeneration):
                discovered = _McpCatalogueGeneration(
                    tools=discovered.tools,
                    resources=discovered.resources,
                    prompts=discovered.prompts,
                    generation=discovered.generation,
                    list_changed=dict(modes),
                )
                self.manager._publish_catalogue(self.name, discovered)
            current = self.manager._catalogue_state.get(self.name)
            self.manager._set_server_state(
                self.name,
                status="connected",
                error=None,
                catalogue_generation=current.generation if current else "",
                last_refresh_at=datetime.now(timezone.utc).isoformat(),
            )
            result = {
                "tools": len(current.tools) if current else 0,
                "resources": len(current.resources) if current else 0,
                "prompts": len(current.prompts) if current else 0,
                "tools_changed": bool(
                    old is None or current is None or old.tools != current.tools
                ),
            }
            if notify_waiter is not None:
                self._finish_future(notify_waiter, result=result)
            return result
        except Exception as exc:
            # Never clear or partially update the last-known-good generation.
            self.manager._set_server_state(
                self.name,
                status="connected",
                error=None,
                refresh_error=str(exc).strip() or type(exc).__name__,
                last_refresh_failure_at=datetime.now(timezone.utc).isoformat(),
                last_known_good=old is not None,
            )
            if notify_waiter is not None:
                self._finish_future(notify_waiter, error=exc)
                return None
            return None


class ContainerMcpManager:
    """Persistent, tenant-scoped MCP connection supervisor.

    Each configured server has exactly one owner task. That task enters the
    transport, initializes the SDK session, serves every request from a bounded
    queue, and exits the transport. This satisfies anyio's same-task context
    manager requirement while avoiding a subprocess or handshake per tool
    call. Catalogue discovery is paginated into a temporary generation and
    published with one pointer swap only after all advertised kinds succeed.
    """

    DEFAULT_TIMEOUT = 60
    DEFAULT_QUEUE_SIZE = 64
    DEFAULT_RECONNECT_FAILURE_BUDGET = 5
    DEFAULT_BACKOFF_INITIAL_SECONDS = 0.25
    DEFAULT_BACKOFF_MAX_SECONDS = 10.0
    DEFAULT_BACKOFF_JITTER_RATIO = 0.20
    DEFAULT_STABLE_WINDOW_SECONDS = 30.0
    DEFAULT_NOTIFICATION_POLL_SECONDS = 30.0
    DEFAULT_OWNER_TICK_SECONDS = 0.25

    def __init__(
        self,
        config_path: Path | None = None,
        *,
        user_scope: str = "",
        command_queue_size: int = DEFAULT_QUEUE_SIZE,
        reconnect_failure_budget: int = DEFAULT_RECONNECT_FAILURE_BUDGET,
        backoff_initial_seconds: float = DEFAULT_BACKOFF_INITIAL_SECONDS,
        backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
        backoff_jitter_ratio: float = DEFAULT_BACKOFF_JITTER_RATIO,
        stable_window_seconds: float = DEFAULT_STABLE_WINDOW_SECONDS,
        notification_poll_seconds: float = DEFAULT_NOTIFICATION_POLL_SECONDS,
        owner_tick_seconds: float = DEFAULT_OWNER_TICK_SECONDS,
    ):
        self.config_path = config_path or MCP_CONFIG_PATH
        self.user_scope = user_scope
        self.command_queue_size = max(1, int(command_queue_size))
        self.reconnect_failure_budget = max(1, int(reconnect_failure_budget))
        self.backoff_initial_seconds = max(0.0, float(backoff_initial_seconds))
        self.backoff_max_seconds = max(
            self.backoff_initial_seconds, float(backoff_max_seconds)
        )
        self.backoff_jitter_ratio = min(1.0, max(0.0, float(backoff_jitter_ratio)))
        self.stable_window_seconds = max(0.0, float(stable_window_seconds))
        self.notification_poll_seconds = max(0.01, float(notification_poll_seconds))
        self.owner_tick_seconds = max(0.01, float(owner_tick_seconds))
        self._servers: dict[str, dict] = {}
        self._catalogue_state: dict[str, _McpCatalogueGeneration] = {}
        self._tools = _McpCatalogueKindView(self, "tools")
        self._resources = _McpCatalogueKindView(self, "resources")
        self._prompts = _McpCatalogueKindView(self, "prompts")
        self._remote_transport: dict[str, str] = {}
        self._owners: dict[str, _McpServerOwner] = {}
        self._catalogue_revision = 0

    @property
    def catalogue_revision(self) -> int:
        return self._catalogue_revision

    def _bump_catalogue_revision(self) -> None:
        self._catalogue_revision += 1

    def _set_server_state(self, name: str, **changes) -> None:
        current = dict(self._servers.get(name) or {})
        was_exhausted = bool(current.get("reconnect_exhausted"))
        current.update(changes)
        self._servers[name] = current
        # The cached generation remains useful diagnostics while a server is
        # offline, but an exhausted owner cannot execute any advertised item.
        # Treat exhaustion as a live-catalogue visibility generation change so
        # backend caches promptly withdraw (and later re-publish) those items.
        is_exhausted = bool(current.get("reconnect_exhausted"))
        if (
            was_exhausted != is_exhausted
            and name in self._catalogue_state
        ):
            self._bump_catalogue_revision()

    def _catalogue_is_visible(self, name: str, cfg: dict | None) -> bool:
        """Whether one last-known-good generation is executable/model-visible."""
        state = self._servers.get(name) or {}
        if state.get("reconnect_exhausted"):
            return False
        if cfg is None:
            return state.get("status") == "connected"
        return self._desired_enabled(cfg)

    @staticmethod
    def _is_transport_failure(exc: Exception) -> bool:
        """Separate broken channels from ordinary MCP tool/protocol errors."""
        if isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)):
            return True
        names = {base.__name__ for base in type(exc).__mro__}
        return bool(names & {
            "BrokenResourceError",
            "ClosedResourceError",
            "EndOfStream",
            "ConnectError",
            "ConnectTimeout",
            "NetworkError",
            "ReadError",
            "ReadTimeout",
            "RemoteProtocolError",
            "WriteError",
            "WriteTimeout",
        })

    @staticmethod
    def _catalogue_digest(
        name: str,
        tools: tuple[dict, ...],
        resources: tuple[dict, ...],
        prompts: tuple[dict, ...],
    ) -> str:
        return _stable_catalogue_digest({
            "server": name,
            "tools": tools,
            "resources": resources,
            "prompts": prompts,
        })

    def _replace_catalogue_kind(self, name: str, kind: str, value) -> None:
        old = self._catalogue_state.get(name) or _McpCatalogueGeneration()
        values = {
            "tools": old.tools,
            "resources": old.resources,
            "prompts": old.prompts,
        }
        values[kind] = tuple(_catalogue_json_value(value or []))
        if value is None and not any(values.values()):
            updated = dict(self._catalogue_state)
            updated.pop(name, None)
            self._catalogue_state = updated
            return
        generation = self._catalogue_digest(
            name, values["tools"], values["resources"], values["prompts"]
        )
        updated = dict(self._catalogue_state)
        updated[name] = _McpCatalogueGeneration(
            tools=values["tools"],
            resources=values["resources"],
            prompts=values["prompts"],
            generation=generation,
            list_changed=dict(old.list_changed),
        )
        self._catalogue_state = updated

    def _publish_catalogue(
        self, name: str, generation: _McpCatalogueGeneration
    ) -> None:
        if not self._connection_still_desired(name):
            return
        updated = dict(self._catalogue_state)
        updated[name] = generation
        self._catalogue_state = updated
        self._bump_catalogue_revision()

    def _set_catalogue_modes(self, name: str, modes: dict[str, str]) -> None:
        current = self._catalogue_state.get(name)
        if current is None:
            return
        updated = dict(self._catalogue_state)
        updated[name] = _McpCatalogueGeneration(
            tools=current.tools,
            resources=current.resources,
            prompts=current.prompts,
            generation=current.generation,
            list_changed=dict(modes),
        )
        self._catalogue_state = updated

    # -- config persistence --

    def _load_config(self) -> dict:
        """Load MCP config from persistent storage."""
        if self.config_path.is_symlink() or self.config_path.parent.is_symlink():
            raise ValueError("MCP config path cannot be a symlink")
        if self.config_path.exists():
            try:
                return json.loads(self.config_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {"servers": {}}

    def _save_config(self, config: dict):
        """Atomically save one tenant's MCP config with control-plane-only mode."""
        parent = self.config_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        if parent.is_symlink() or self.config_path.is_symlink():
            raise ValueError("MCP config path cannot be a symlink")
        temporary = parent / f".{self.config_path.name}.{secrets.token_hex(6)}.tmp"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(config, stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.config_path)
            self.config_path.chmod(0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def _server_config(self, name: str) -> dict:
        cfg = self._load_config().get("servers", {}).get(name)
        if not cfg:
            raise KeyError(f"MCP server '{name}' not configured")
        return cfg

    @staticmethod
    def _desired_enabled(cfg: dict) -> bool:
        """Return the durable desired state, preserving legacy config behavior.

        Config files created before desired-state persistence have no
        ``enabled`` field. They used to reconnect on every startup, so absence
        remains enabled. Invalid non-boolean values also fail compatibility-
        safe (enabled); every write below normalizes the field to a bool.
        """
        value = cfg.get("enabled", True)
        return value if isinstance(value, bool) else True

    def _set_desired_enabled(self, name: str, enabled: bool) -> None:
        """Persist one server's desired state before changing runtime state."""
        full_config = self._load_config()
        servers = full_config.get("servers", {})
        if name not in servers:
            raise KeyError(f"MCP server '{name}' not configured")
        cfg = dict(servers[name])
        # Explicit connects migrate legacy entries even though their effective
        # default is already enabled. This leaves an unambiguous durable intent.
        if isinstance(cfg.get("enabled"), bool) and cfg["enabled"] is enabled:
            return
        cfg["enabled"] = enabled
        servers[name] = cfg
        self._save_config(full_config)

    def _connection_still_desired(self, name: str) -> bool:
        """Recheck state after an awaited probe so a later disconnect wins."""
        try:
            return self._desired_enabled(self._server_config(name))
        except KeyError:
            # Removal racing a startup probe must not resurrect the server.
            return False

    def _timeout(self, cfg: dict) -> float:
        try:
            return float(cfg.get("timeout") or self.DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            return float(self.DEFAULT_TIMEOUT)

    def list_servers(self) -> list[dict]:
        """List all configured MCP servers with their status."""
        config = self._load_config()
        result = []
        for name, cfg in config.get("servers", {}).items():
            state = self._servers.get(name) or {}
            status = state.get("status", "disconnected")
            error = state.get("error")
            generation = self._catalogue_state.get(name)
            visible = generation is not None and self._catalogue_is_visible(name, cfg)
            result.append({
                "name": name,
                "type": cfg.get("type", "stdio"),
                "enabled": self._desired_enabled(cfg),
                "status": status,
                "tools": list(generation.tools) if visible else [],
                "resources": list(generation.resources) if visible else [],
                "prompts": list(generation.prompts) if visible else [],
                "error": error,
                "refresh_error": state.get("refresh_error"),
                "last_known_good": bool(generation),
                "catalogue_generation": generation.generation if generation else "",
                "list_changed": (
                    dict(generation.list_changed) if generation else {}
                ),
                "consecutive_failures": state.get("consecutive_failures", 0),
                "reconnect_exhausted": bool(state.get("reconnect_exhausted")),
                "command": cfg.get("command"),
                "args": cfg.get("args"),
                "url": cfg.get("url"),
            })
        return result

    def add_server(self, name: str, config: dict):
        """Add a new MCP server configuration."""
        full_config = self._load_config()
        normalized = dict(config)
        normalized["enabled"] = self._desired_enabled(normalized)
        full_config.setdefault("servers", {})[name] = normalized
        self._save_config(full_config)
        self._bump_catalogue_revision()

    def remove_server(self, name: str):
        """Durably remove a server and synchronously erect its stop fence."""
        full_config = self._load_config()
        servers = full_config.get("servers", {})
        if name not in servers:
            raise KeyError(f"MCP server '{name}' not found")
        del servers[name]
        self._save_config(full_config)
        owner = self._owners.get(name)
        if owner is not None:
            owner.request_stop()
        self._forget(name)
        self._bump_catalogue_revision()

    async def remove_server_and_stop(self, name: str) -> None:
        self.remove_server(name)
        await self._stop_owner(name)

    def _forget(self, name: str) -> None:
        self._servers.pop(name, None)
        if name in self._catalogue_state:
            updated = dict(self._catalogue_state)
            updated.pop(name, None)
            self._catalogue_state = updated
        self._remote_transport.pop(name, None)

    # -- session helpers: every one opens and closes within a single task --

    @asynccontextmanager
    async def _stdio_session(self, cfg: dict, notification_handler=None):
        """Open a stdio MCP session for the duration of the block."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        command = cfg.get("command", "")
        if not command:
            raise ValueError("stdio MCP server requires 'command'")

        # The child inherits the action server's environment minus the secrets
        # it has no business seeing. SESSION_API_KEY authenticates every caller
        # of this server, so handing it to an arbitrary npx package would let
        # that package impersonate the backend.
        requested_env = cfg.get("env") or {}
        protected = {key for key in requested_env if key in _MCP_ENV_DENYLIST}
        if protected:
            raise ValueError(
                "MCP environment contains protected control-plane variables: "
                + ", ".join(sorted(protected))
            )
        env = _runner_env(requested_env)
        if self.user_scope:
            mcp_home = (
                WORKSPACE_ROOT / "openbox" / "users" / self.user_scope
                / ".openbox" / "mcp-home"
            )
            _ensure_runner_directory(mcp_home)
            env.update({
                "HOME": str(mcp_home),
                "XDG_CACHE_HOME": str(mcp_home / ".cache"),
                "NPM_CONFIG_CACHE": str(mcp_home / ".npm"),
            })
        argv = _runner_argv([command, *(cfg.get("args") or [])])

        params = StdioServerParameters(
            command=argv[0], args=argv[1:], env=env,
        )
        timeout = self._timeout(cfg)
        async with stdio_client(params) as (read_stream, write_stream):
            session, registered = self._new_sdk_session(
                ClientSession, read_stream, write_stream, notification_handler
            )
            async with session:
                async with _same_task_timeout(timeout):
                    initialized = await session.initialize()
                session._openbox_server_capabilities = _mcp_attr(
                    initialized, "capabilities", default={}
                )
                session._openbox_notification_handler_registered = registered
                yield session

    @asynccontextmanager
    async def _sse_session(self, cfg: dict, notification_handler=None):
        """Open an SSE MCP session for the duration of the block."""
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        url = cfg.get("url", "")
        headers = cfg.get("headers") or None
        timeout = self._timeout(cfg)
        async with sse_client(url, headers=headers) as (read_stream, write_stream):
            session, registered = self._new_sdk_session(
                ClientSession, read_stream, write_stream, notification_handler
            )
            async with session:
                async with _same_task_timeout(timeout):
                    initialized = await session.initialize()
                session._openbox_server_capabilities = _mcp_attr(
                    initialized, "capabilities", default={}
                )
                session._openbox_notification_handler_registered = registered
                yield session

    @staticmethod
    def _new_sdk_session(
        session_type, read_stream, write_stream, notification_handler
    ):
        """Register SDK notification delivery only when that SDK supports it."""
        registered = False
        kwargs = {}
        try:
            parameters = inspect.signature(session_type).parameters
        except (TypeError, ValueError):
            parameters = {}
        if notification_handler is not None and "message_handler" in parameters:
            async def handle(message):
                method = ContainerMcpManager._notification_method(message)
                if method:
                    result = notification_handler(method)
                    if inspect.isawaitable(result):
                        await result

            kwargs["message_handler"] = handle
            registered = True
        return session_type(read_stream, write_stream, **kwargs), registered

    @staticmethod
    def _notification_method(message) -> str:
        candidates = [message, getattr(message, "root", None)]
        for candidate in candidates:
            if candidate is None:
                continue
            if isinstance(candidate, dict):
                method = candidate.get("method")
            else:
                method = getattr(candidate, "method", None)
            if method in {
                "notifications/tools/list_changed",
                "notifications/resources/list_changed",
                "notifications/prompts/list_changed",
            }:
                return str(method)
        return ""

    @asynccontextmanager
    async def _raw_session(self, cfg: dict, notification_handler=None):
        """Open a raw streamable-HTTP MCP session for the duration of the block."""
        url = cfg.get("url", "")
        session = RawStreamableHttpSession(
            url,
            headers=cfg.get("headers") or {},
            timeout=int(self._timeout(cfg)),
            notification_handler=notification_handler,
        )
        try:
            async with _same_task_timeout(self._timeout(cfg)):
                await session.initialize()
            yield session
        finally:
            try:
                await session.close()
            except Exception:
                pass

    @asynccontextmanager
    async def _session(self, name: str, notification_handler=None):
        """Open a session to ``name`` using whichever transport it needs.

        Remote servers try raw streamable HTTP first and fall back to SSE. The
        winning transport is remembered so later calls do not re-pay a failed
        probe.
        """
        cfg = self._server_config(name)
        server_type = cfg.get("type", "stdio")

        if server_type == "stdio":
            async with self._stdio_session(cfg, notification_handler) as session:
                yield session
            return

        if server_type != "remote":
            raise ValueError(f"Unknown MCP server type: {server_type}")

        if not cfg.get("url"):
            raise ValueError("Remote MCP server requires 'url'")

        preferred = self._remote_transport.get(name)
        if preferred == "sse":
            async with self._sse_session(cfg, notification_handler) as session:
                yield session
            return

        try:
            cm = self._raw_session(cfg, notification_handler)
            session = await cm.__aenter__()
        except Exception as e:
            # The raw probe failed before yielding, so nothing needs unwinding
            # here and SSE is still worth a try.
            print(f"[MCP] Raw HTTP failed for '{name}': {e}, trying SDK SSE...")
            self._remote_transport[name] = "sse"
            async with self._sse_session(cfg, notification_handler) as session:
                yield session
            return

        # Past this point the raw session is live; its own context manager owns
        # cleanup, and a failure inside the body is the caller's, not a reason
        # to retry on SSE.
        self._remote_transport[name] = "raw"
        try:
            yield session
        finally:
            await cm.__aexit__(None, None, None)

    # -- discovery --

    @staticmethod
    def _tools_from(session, name: str, raw: list) -> list[dict]:
        if isinstance(session, RawStreamableHttpSession):
            return [
                {"name": t.get("name", ""), "description": t.get("description", "") or "",
                 "input_schema": t.get("inputSchema", {}) or {}, "server": name}
                for t in (raw or [])
            ]
        return [
            {"name": t.name, "description": t.description or "",
             "input_schema": _mcp_attr(t, "input_schema", "inputSchema", default={}) or {}, "server": name}
            for t in raw
        ]

    @staticmethod
    def _capability_value(session, kind: str, field_name: str | None = None):
        if isinstance(session, RawStreamableHttpSession):
            capabilities = session._server_info.get("capabilities") or {}
        else:
            capabilities = getattr(session, "_openbox_server_capabilities", {})
        if isinstance(capabilities, dict):
            section = capabilities.get(kind)
        else:
            section = getattr(capabilities, kind, None)
        if field_name is None:
            return section
        if isinstance(section, dict):
            return section.get(field_name, section.get("listChanged"))
        return _mcp_attr(section, field_name, "listChanged", default=False)

    async def _notification_modes(self, session) -> dict[str, str]:
        advertised = {
            kind: bool(self._capability_value(session, kind, "list_changed"))
            for kind in ("tools", "resources", "prompts")
        }
        receiver = False
        if isinstance(session, RawStreamableHttpSession):
            receiver = await session.start_notification_receiver()
        else:
            receiver = bool(
                getattr(session, "_openbox_notification_handler_registered", False)
            )
        return {
            kind: ("notification" if receiver else "poll") if supported else "unsupported"
            for kind, supported in advertised.items()
        }

    async def _list_all(self, session, kind: str) -> list:
        items: list = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(1000):
            if isinstance(session, RawStreamableHttpSession):
                response = await getattr(session, f"list_{kind}_page")(cursor)
                page = response.get(kind, []) if isinstance(response, dict) else []
                next_cursor = (
                    response.get("nextCursor") or response.get("next_cursor")
                    if isinstance(response, dict) else None
                )
            else:
                method = getattr(session, f"list_{kind}")
                response = await method(cursor=cursor) if cursor else await method()
                page = getattr(response, kind, []) or []
                next_cursor = _mcp_attr(
                    response, "next_cursor", "nextCursor", default=None
                )
            items.extend(page or [])
            if not next_cursor:
                return items
            cursor = str(next_cursor)
            if cursor in seen:
                raise RuntimeError(f"MCP {kind} pagination repeated cursor")
            seen.add(cursor)
        raise RuntimeError(f"MCP {kind} pagination exceeded 1000 pages")

    async def _discover(self, name: str, session) -> _McpCatalogueGeneration:
        """Build a complete generation without mutating the live catalogue."""
        is_raw = isinstance(session, RawStreamableHttpSession)
        raw_tools = await self._list_all(session, "tools")
        tools = self._tools_from(session, name, raw_tools)

        resources: list[dict] = []
        resource_capability = self._capability_value(session, "resources")
        if resource_capability is not None:
            res = await self._list_all(session, "resources")
            if is_raw:
                resources = [
                    {"uri": r.get("uri", ""), "name": r.get("name", ""),
                     "description": r.get("description", ""),
                     "mimeType": r.get("mimeType", ""), "server": name}
                    for r in res
                ]
            else:
                resources = [
                    {"uri": str(r.uri), "name": r.name or "",
                     "description": getattr(r, "description", "") or "",
                     "mimeType": _mcp_attr(r, "mime_type", "mimeType", default="") or "", "server": name}
                    for r in res
                ]

        prompts: list[dict] = []
        prompt_capability = self._capability_value(session, "prompts")
        if prompt_capability is not None:
            pr = await self._list_all(session, "prompts")
            if is_raw:
                prompts = [
                    {"name": p.get("name", ""), "description": p.get("description", ""),
                     "arguments": p.get("arguments", []), "server": name}
                    for p in pr
                ]
            else:
                prompts = [
                    {"name": p.name, "description": p.description or "",
                     "arguments": [
                         {"name": a.name, "description": getattr(a, "description", "") or "",
                          "required": getattr(a, "required", False)}
                         for a in (p.arguments or [])
                     ],
                     "server": name}
                    for p in pr
                ]

        detached_tools = tuple(_catalogue_json_value(tools))
        detached_resources = tuple(_catalogue_json_value(resources))
        detached_prompts = tuple(_catalogue_json_value(prompts))
        return _McpCatalogueGeneration(
            tools=detached_tools,
            resources=detached_resources,
            prompts=detached_prompts,
            generation=self._catalogue_digest(
                name, detached_tools, detached_resources, detached_prompts
            ),
        )

    async def connect(self, name: str, *, persist_desired: bool = True) -> bool:
        """Start the persistent owner and wait for its first full generation."""
        if persist_desired:
            self._set_desired_enabled(name, True)
        elif not self._connection_still_desired(name):
            self._forget(name)
            return False
        owner = self._ensure_owner(name)
        try:
            return bool(await owner.ready)
        except asyncio.CancelledError:
            if not self._connection_still_desired(name):
                return False
            raise

    def _ensure_owner(self, name: str) -> _McpServerOwner:
        owner = self._owners.get(name)
        if owner is not None and not owner.task.done():
            return owner
        owner = _McpServerOwner(self, name)
        self._owners[name] = owner
        return owner

    async def _stop_owner(self, name: str) -> None:
        owner = self._owners.pop(name, None)
        if owner is not None:
            await owner.stop()

    async def disconnect(self, name: str):
        """Persist disabled intent, then drop the server's cached listing.

        Persistence happens first so a failed disk write cannot claim success.
        The owner is then cancelled and awaited, guaranteeing its transport is
        disposed before the cached generation is forgotten.
        """
        self._set_desired_enabled(name, False)
        await self._stop_owner(name)
        self._forget(name)
        self._bump_catalogue_revision()

    def get_all_tools(self) -> list[dict]:
        """Get all tools from all connected servers."""
        all_tools = []
        snapshot = self._catalogue_state
        config = self._load_config().get("servers", {})
        for name, generation in snapshot.items():
            cfg = config.get(name)
            if not self._catalogue_is_visible(name, cfg):
                continue
            all_tools.extend(generation.tools)
        return all_tools

    def _require_connected(self, name: str) -> None:
        owner = self._owners.get(name)
        if (
            (self._servers.get(name) or {}).get("status") != "connected"
            or owner is None
            or owner.task.done()
        ):
            raise KeyError(f"MCP server '{name}' is not connected")

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> dict:
        """Call a tool on a connected MCP server."""
        self._require_connected(server_name)
        owner = self._owners[server_name]
        return await owner.submit("call_tool", tool_name, arguments)

    def get_all_resources(self) -> list[dict]:
        """Get all resources from all connected servers."""
        all_res = []
        snapshot = self._catalogue_state
        config = self._load_config().get("servers", {})
        for name, generation in snapshot.items():
            cfg = config.get(name)
            if not self._catalogue_is_visible(name, cfg):
                continue
            all_res.extend(generation.resources)
        return all_res

    async def read_resource(self, server_name: str, uri: str) -> dict:
        """Read a resource from a connected MCP server."""
        self._require_connected(server_name)
        return await self._owners[server_name].submit("read_resource", uri)

    def get_all_prompts(self) -> list[dict]:
        """Get all prompts from all connected servers."""
        all_pr = []
        snapshot = self._catalogue_state
        config = self._load_config().get("servers", {})
        for name, generation in snapshot.items():
            cfg = config.get(name)
            if not self._catalogue_is_visible(name, cfg):
                continue
            all_pr.extend(generation.prompts)
        return all_pr

    async def get_prompt(self, server_name: str, prompt_name: str, arguments: dict | None = None) -> dict:
        """Get a prompt from a connected MCP server."""
        self._require_connected(server_name)
        return await self._owners[server_name].submit(
            "get_prompt", prompt_name, arguments
        )

    async def _dispatch_session_operation(self, session, operation: str, args: tuple):
        if operation == "call_tool":
            tool_name, arguments = args
            result = await session.call_tool(tool_name, arguments)
            if isinstance(session, RawStreamableHttpSession):
                content_parts = [
                    part if isinstance(part, dict)
                    else {"type": "text", "text": str(part)}
                    for part in result.get("content", [])
                ]
                return {
                    "content": content_parts,
                    "isError": result.get("isError", False),
                }
            content_parts = []
            for part in result.content:
                if hasattr(part, "text"):
                    content_parts.append({"type": "text", "text": part.text})
                elif hasattr(part, "data"):
                    content_parts.append({"type": "resource", "data": str(part.data)})
                else:
                    content_parts.append({"type": "unknown", "value": str(part)})
            return {
                "content": content_parts,
                "isError": bool(
                    _mcp_attr(result, "is_error", "isError", default=False)
                ),
            }
        if operation == "read_resource":
            result = await session.read_resource(args[0])
            if isinstance(session, RawStreamableHttpSession):
                return result
            contents = []
            for part in result.contents:
                if hasattr(part, "text"):
                    contents.append({
                        "uri": str(part.uri),
                        "text": part.text,
                        "mimeType": _mcp_attr(
                            part, "mime_type", "mimeType", default=""
                        ),
                    })
                elif hasattr(part, "blob"):
                    contents.append({
                        "uri": str(part.uri),
                        "blob": part.blob,
                        "mimeType": _mcp_attr(
                            part, "mime_type", "mimeType", default=""
                        ),
                    })
                else:
                    contents.append({"uri": str(part.uri), "text": str(part)})
            return {"contents": contents}
        if operation == "get_prompt":
            result = await session.get_prompt(args[0], args[1])
            if isinstance(session, RawStreamableHttpSession):
                return result
            messages = []
            for msg in result.messages:
                parts = []
                content = msg.content
                if isinstance(content, list):
                    for part in content:
                        if hasattr(part, "text"):
                            parts.append({"type": "text", "text": part.text})
                elif hasattr(content, "text"):
                    parts.append({"type": "text", "text": content.text})
                messages.append({"role": msg.role, "content": parts})
            return {"messages": messages}
        raise ValueError(f"Unknown MCP owner operation: {operation}")

    async def refresh_server(self, name: str) -> dict:
        """Re-fetch tools/resources/prompts from a connected server."""
        owner = self._owners.get(name)
        if owner is None and (self._servers.get(name) or {}).get("status") == "connected":
            # Compatibility path for isolated fixture managers that inject a
            # connected state without starting the production supervisor.
            old = self._catalogue_state.get(name)
            async with self._session(name) as session:
                discovered = await self._discover(name, session)
            if isinstance(discovered, _McpCatalogueGeneration):
                self._publish_catalogue(name, discovered)
            else:
                self._bump_catalogue_revision()
            current = self._catalogue_state.get(name)
            return {
                "tools": len(current.tools) if current else 0,
                "resources": len(current.resources) if current else 0,
                "prompts": len(current.prompts) if current else 0,
                "tools_changed": bool(
                    old is None or current is None or old.tools != current.tools
                ),
            }
        self._require_connected(name)
        return await owner.submit("refresh")

    async def reconnect_configured(self) -> None:
        """Reconnect desired-enabled servers as a background startup task.

        Runs after the app is serving rather than inside lifespan setup: a
        server that is slow or gone must not hold the container's startup, and
        each failure is recorded for the UI instead of raised. Desired state
        is re-read immediately before each probe, so a disconnect that arrives
        while an earlier server is starting is respected.
        """
        names = list(self._load_config().get("servers", {}).keys())
        if not names:
            return
        enabled_count = sum(
            1
            for name in names
            if self._connection_still_desired(name)
        )
        if not enabled_count:
            return
        print(f"[MCP] Reconnecting {enabled_count} enabled server(s)...")
        for name in names:
            if not self._connection_still_desired(name):
                continue
            try:
                connected = await self.connect(name, persist_desired=False)
                if connected:
                    print(f"[MCP] Reconnected '{name}' ({len(self._tools.get(name, []))} tools)")
            except Exception as e:
                print(f"[MCP] Reconnect failed for '{name}': {e}")

    async def shutdown(self) -> None:
        """Stop every owner and dispose all process-local runtime projections."""
        owners = list(self._owners.values())
        self._owners.clear()
        for owner in owners:
            owner.request_stop()
        if owners:
            await asyncio.gather(
                *(owner.stop() for owner in owners), return_exceptions=True
            )
        had_catalogue = bool(self._catalogue_state)
        self._catalogue_state = {}
        self._servers.clear()
        self._remote_transport.clear()
        if had_catalogue:
            self._bump_catalogue_revision()


# Singleton MCP manager
mcp_manager = ContainerMcpManager()
_scoped_mcp_managers: dict[str, ContainerMcpManager] = {}


def _active_mcp_manager() -> ContainerMcpManager:
    """Return the request tenant's stateful MCP runtime and config store."""
    scope = _current_user_scope()
    if not scope:
        return mcp_manager
    manager = _scoped_mcp_managers.get(scope)
    if manager is None:
        manager = ContainerMcpManager(
            _scoped_mcp_config_path(scope),
            user_scope=scope,
        )
        _scoped_mcp_managers[scope] = manager
    return manager


def _persisted_scoped_mcp_managers() -> list[ContainerMcpManager]:
    """Discover scoped configs at startup without treating names as identity."""
    root = MCP_CONFIG_PATH.parent
    if not root.is_dir():
        return []
    managers: list[ContainerMcpManager] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        scope = child.name
        if not _USER_SCOPE_PATTERN.fullmatch(scope):
            continue
        path = child / MCP_CONFIG_PATH.name
        if not path.is_file() or path.is_symlink():
            continue
        manager = _scoped_mcp_managers.get(scope)
        if manager is None:
            manager = ContainerMcpManager(path, user_scope=scope)
            _scoped_mcp_managers[scope] = manager
        managers.append(manager)
    return managers


def _catalogue_json_value(value):
    """Detach remote SDK models into JSON values before publishing/caching."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _mcp_catalogue_projection(manager: ContainerMcpManager | None = None) -> dict:
    """Return connected MCP definition metadata, never resource bodies."""
    active = manager or _active_mcp_manager()
    config = active._load_config()
    servers = []
    for name, server_config in sorted(config.get("servers", {}).items()):
        state = active._servers.get(name) or {}
        catalogue = active._catalogue_state.get(name)
        servers.append({
            "name": str(name),
            "type": str(server_config.get("type") or "stdio"),
            "enabled": active._desired_enabled(server_config),
            "status": str(state.get("status") or "disconnected"),
            "list_changed": dict(catalogue.list_changed) if catalogue else {},
            "catalogue_generation": catalogue.generation if catalogue else "",
            "last_known_good": catalogue is not None,
            "reconnect_exhausted": bool(state.get("reconnect_exhausted")),
        })

    tools = []
    for raw in active.get_all_tools():
        if not isinstance(raw, dict):
            continue
        schema = raw.get("input_schema", {})
        tools.append({
            "server": str(raw.get("server") or ""),
            "name": str(raw.get("name") or ""),
            "description": str(raw.get("description") or ""),
            "input_schema": _catalogue_json_value(
                schema if isinstance(schema, dict) else {}
            ),
        })
    tools.sort(key=lambda item: (item["server"], item["name"]))

    resources = []
    for raw in active.get_all_resources():
        if not isinstance(raw, dict):
            continue
        # Whitelist metadata. Never forward text/blob/contents or unknown
        # extension fields from an untrusted MCP server into the catalogue.
        resources.append({
            "server": str(raw.get("server") or ""),
            "uri": str(raw.get("uri") or ""),
            "name": str(raw.get("name") or ""),
            "description": str(raw.get("description") or ""),
            "mimeType": str(raw.get("mimeType") or raw.get("mime_type") or ""),
        })
    resources.sort(key=lambda item: (item["server"], item["uri"], item["name"]))

    generation_input = {
        "revision": active.catalogue_revision,
        "servers": servers,
        "tools": tools,
        "resources": resources,
    }
    return {
        "servers": servers,
        "tools": tools,
        "resources": resources,
        "server_count": len(servers),
        "tool_count": len(tools),
        "resource_count": len(resources),
        "revision": active.catalogue_revision,
        "generation": _stable_catalogue_digest(generation_input),
    }


def _catalogue_version_payload(skills: dict, mcp: dict) -> dict:
    generation = _stable_catalogue_digest({
        "boot_id": _ACTION_SERVER_BOOT_ID,
        "skills_generation": skills["generation"],
        "mcp_generation": mcp["generation"],
    })
    return {
        "catalogue_version": CATALOGUE_PROTOCOL_VERSION,
        "boot_id": _ACTION_SERVER_BOOT_ID,
        "started_at": START_TIME,
        "skills_generation": skills["generation"],
        "mcp_generation": mcp["generation"],
        "generation": generation,
        "counts": {
            "skills": skills["count"],
            "mcp_servers": mcp["server_count"],
            "mcp_tools": mcp["tool_count"],
            "mcp_resources": mcp["resource_count"],
        },
    }


def _build_catalogue_projection() -> dict:
    skills = _skill_catalogue_projection()
    mcp = _mcp_catalogue_projection(_active_mcp_manager())
    version = _catalogue_version_payload(skills, mcp)
    return {
        **version,
        "skills": skills["items"],
        "mcp_servers": mcp["servers"],
        "mcp_tools": mcp["tools"],
        "mcp_resources": mcp["resources"],
    }


@app.get("/catalog/version")
async def get_catalogue_version(request: Request):
    """Publish stable sandbox boot and directory generations."""
    skills = _skill_catalogue_projection()
    mcp = _mcp_catalogue_projection(_active_mcp_manager())
    payload = _catalogue_version_payload(skills, mcp)
    return _catalogue_json_response(request, payload, payload["generation"])


@app.get("/catalog")
async def get_catalogue_projection(request: Request):
    """Publish one body-free directory snapshot for the backend control plane."""
    payload = _build_catalogue_projection()
    return _catalogue_json_response(request, payload, payload["generation"])


@app.get("/mcp/servers")
async def list_mcp_servers():
    """List all configured MCP servers and their status."""
    return _active_mcp_manager().list_servers()


@app.post("/mcp/servers")
async def add_mcp_server(req: AddMcpServerRequest):
    """Add a new MCP server configuration."""
    config = {"type": req.type, "timeout": req.timeout}
    if req.type == "stdio":
        if not req.command:
            raise HTTPException(status_code=400, detail="stdio server requires 'command'")
        config["command"] = req.command
        config["args"] = req.args or []
        config["env"] = req.env or {}
    elif req.type == "remote":
        if not req.url:
            raise HTTPException(status_code=400, detail="remote server requires 'url'")
        config["url"] = req.url
        if req.headers:
            config["headers"] = req.headers
    else:
        raise HTTPException(status_code=400, detail=f"Unknown type: {req.type}")
    _active_mcp_manager().add_server(req.name, config)
    return {"ok": True, "message": f"MCP server '{req.name}' added"}


@app.delete("/mcp/servers/{name}")
async def remove_mcp_server(name: str):
    """Remove an MCP server configuration."""
    try:
        await _active_mcp_manager().remove_server_and_stop(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    return {"ok": True, "message": f"MCP server '{name}' removed"}


@app.post("/mcp/servers/{name}/connect")
async def connect_mcp_server(name: str):
    """Start and connect to an MCP server."""
    try:
        await _active_mcp_manager().connect(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to connect: {e}")
    return {"ok": True, "message": f"Connected to '{name}'"}


@app.post("/mcp/servers/{name}/disconnect")
async def disconnect_mcp_server(name: str):
    """Disconnect from an MCP server."""
    try:
        await _active_mcp_manager().disconnect(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, "message": f"Disconnected from '{name}'"}


@app.get("/mcp/tools")
async def list_mcp_tools(request: Request):
    """List all tools from all connected MCP servers."""
    manager = _active_mcp_manager()
    projection = _mcp_catalogue_projection(manager)
    return _catalogue_json_response(
        request,
        manager.get_all_tools(),
        projection["generation"],
    )


@app.post("/mcp/tools/{server_name}/{tool_name}")
async def call_mcp_tool(server_name: str, tool_name: str, req: CallMcpToolRequest):
    """Call a tool on a connected MCP server."""
    try:
        result = await _active_mcp_manager().call_tool(server_name, tool_name, req.arguments)
        return result
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        # Return MCP errors as tool results (isError=true) instead of HTTP 500.
        # This lets the LLM see the error and decide how to handle it.
        #
        # Timeouts and connection errors stringify to nothing, so interpolating
        # them produced "MCP tool error:" and stopped — the one case where the
        # model most needs to know whether to retry or change approach.
        import httpx as _httpx

        if isinstance(e, (asyncio.TimeoutError, _httpx.TimeoutException)):
            text = (
                f"MCP tool '{tool_name}' on server '{server_name}' timed out. "
                f"The server did not answer within its configured timeout. "
                f"Try a narrower request, or a tool that returns less."
            )
        elif isinstance(e, _httpx.HTTPError):
            text = (
                f"Could not reach MCP server '{server_name}': "
                f"{str(e).strip() or type(e).__name__}"
            )
        else:
            text = f"MCP tool error: {str(e).strip() or type(e).__name__}"
        return {"content": [{"type": "text", "text": text}], "isError": True}


@app.get("/mcp/resources")
async def list_mcp_resources(request: Request):
    """List all resources from all connected MCP servers."""
    manager = _active_mcp_manager()
    projection = _mcp_catalogue_projection(manager)
    return _catalogue_json_response(
        request,
        manager.get_all_resources(),
        projection["generation"],
    )


@app.post("/mcp/resources/read")
async def read_mcp_resource(req: ReadMcpResourceRequest):
    """Read a specific resource from a connected MCP server."""
    try:
        return await _active_mcp_manager().read_resource(req.server, req.uri)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Resource read failed: {e}")


@app.get("/mcp/prompts")
async def list_mcp_prompts():
    """List all prompts from all connected MCP servers."""
    return _active_mcp_manager().get_all_prompts()


@app.post("/mcp/prompts/get")
async def get_mcp_prompt(req: GetMcpPromptRequest):
    """Get a specific prompt from a connected MCP server."""
    try:
        return await _active_mcp_manager().get_prompt(req.server, req.name, req.arguments)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Prompt get failed: {e}")


@app.post("/mcp/servers/{name}/refresh")
async def refresh_mcp_server(name: str):
    """Re-fetch tools/resources/prompts from a connected MCP server."""
    try:
        return await _active_mcp_manager().refresh_server(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Refresh failed: {e}")


# ============================================================
# Backup / Restore (GKE mode)
# ============================================================

MANIFEST_PATH = Path("/data/.manifest.json")
BACKUP_EXCLUDE = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    ".next", ".nuxt", "dist", "build", ".cache",
}


class BackupRequest(BaseModel):
    provider: str = "gcs"
    bucket: str = ""
    prefix: str = ""


def _scan_workspace(base: Path) -> dict[str, float]:
    """Scan /workspace and return {relative_path: mtime} excluding large dirs."""
    result = {}
    for p in base.rglob("*"):
        if p.is_file():
            parts = p.relative_to(base).parts
            if any(part in BACKUP_EXCLUDE for part in parts):
                continue
            try:
                result[str(p.relative_to(base))] = p.stat().st_mtime
            except OSError:
                pass
    return result


def _load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        try:
            return json.loads(MANIFEST_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"files": {}}


def _save_manifest(manifest: dict):
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2))


@app.post("/backup")
def backup_workspace(req: BackupRequest):
    """Incremental backup of /workspace to cloud storage.

    Runs synchronously (FastAPI thread pool) to avoid blocking the event loop
    with GCS SDK calls, which would stall health checks.
    """
    if not req.bucket:
        return JSONResponse(status_code=200, content={"uploaded": 0, "message": "no bucket configured"})

    try:
        from google.cloud import storage as gcs
    except ImportError:
        raise HTTPException(status_code=501, detail="google-cloud-storage not installed")

    workspace = Path("/workspace")
    current_files = _scan_workspace(workspace)
    manifest = _load_manifest()
    old_files = manifest.get("files", {})

    client = gcs.Client()
    bucket = client.bucket(req.bucket)

    uploaded = 0
    deleted = 0
    total_size = 0

    # Upload new or modified files
    for rel_path, mtime in current_files.items():
        if rel_path in old_files and old_files[rel_path] >= mtime:
            continue
        file_path = workspace / rel_path
        blob_key = f"{req.prefix}{rel_path}" if req.prefix else rel_path
        blob = bucket.blob(blob_key)
        data = file_path.read_bytes()
        blob.upload_from_string(data)
        total_size += len(data)
        uploaded += 1

    # Delete removed files from cloud
    for rel_path in old_files:
        if rel_path not in current_files:
            blob_key = f"{req.prefix}{rel_path}" if req.prefix else rel_path
            blob = bucket.blob(blob_key)
            try:
                blob.delete()
            except Exception:
                pass
            deleted += 1

    manifest["files"] = {k: v for k, v in current_files.items()}
    _save_manifest(manifest)

    # Upload manifest to cloud as well
    manifest_key = f"{req.prefix}.manifest.json" if req.prefix else ".manifest.json"
    bucket.blob(manifest_key).upload_from_string(json.dumps(manifest))

    return {
        "uploaded": uploaded,
        "deleted": deleted,
        "total_size": f"{total_size / (1024 * 1024):.1f}MB",
    }


@app.post("/restore")
def restore_workspace(req: BackupRequest):
    """Restore /workspace from cloud storage.

    Runs synchronously (FastAPI thread pool) to avoid blocking the event loop
    with GCS SDK calls, which would stall health checks.
    """
    if not req.bucket:
        return JSONResponse(status_code=200, content={"restored": 0, "message": "no bucket configured"})

    try:
        from google.cloud import storage as gcs
    except ImportError:
        raise HTTPException(status_code=501, detail="google-cloud-storage not installed")

    client = gcs.Client()
    bucket = client.bucket(req.bucket)
    workspace = Path("/workspace")

    # Download manifest
    manifest_key = f"{req.prefix}.manifest.json" if req.prefix else ".manifest.json"
    manifest_blob = bucket.blob(manifest_key)
    if not manifest_blob.exists():
        return {"restored": 0, "message": "no backup found"}

    manifest = json.loads(manifest_blob.download_as_bytes())
    files_map = manifest.get("files", {})

    restored = 0
    total_size = 0

    for rel_path, mtime in files_map.items():
        blob_key = f"{req.prefix}{rel_path}" if req.prefix else rel_path
        blob = bucket.blob(blob_key)
        if not blob.exists():
            continue
        data = blob.download_as_bytes()
        file_path = workspace / rel_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(data)
        os.utime(file_path, (mtime, mtime))
        total_size += len(data)
        restored += 1

    _save_manifest(manifest)

    return {
        "restored": restored,
        "total_size": f"{total_size / (1024 * 1024):.1f}MB",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--host", type=str, default="0.0.0.0")
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
