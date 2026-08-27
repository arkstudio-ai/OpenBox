import argparse
import asyncio
import fcntl
import hashlib
import json
import logging
import os
import platform
import pty
import select
import shutil
import signal
import secrets
import stat
import struct
import sys
import termios
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal

import subprocess

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
ACTION_SERVER_VERSION = "2026.08.27-video-production-v3"
# Uvicorn owns the configured INFO handler in both containers and the WUYING
# systemd service. A standalone child logger inherited the root WARNING level
# and silently discarded the very traces this feature exists to preserve.
trace_log = logging.getLogger("uvicorn.error")

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

class ReadFileRequest(BaseModel):
    path: str
    offset: int = 0  # 0-based line offset
    limit: int = 2000  # max lines to return

class GlobRequest(BaseModel):
    pattern: str
    path: str = "/workspace"

class GrepRequest(BaseModel):
    pattern: str
    path: str = "/workspace"
    type: str | None = None  # file type filter, e.g. "py", "js"
    max_results: int = 100

# --- API Key ---
SESSION_API_KEY = os.environ.get("SESSION_API_KEY", "")

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
    # /workspace/skills → /data/skills/ convenience symlink for agent scripts
    skills_link = Path("/workspace/skills")
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    if not skills_link.exists():
        try:
            skills_link.symlink_to(SKILLS_DIR)
        except OSError:
            pass
    # Create name-based symlinks for user-installed skills (skill packs, etc.)
    _ensure_skill_symlinks()
    # MCP config outlives the container but connections do not, so a restart
    # used to leave every configured server listed as disconnected with its
    # tools silently missing from the agent. Reconnect in the background: a
    # server that is slow or gone must not delay the container coming up.
    reconnect_task = asyncio.create_task(mcp_manager.reconnect_configured())
    await media_job_manager.start()
    try:
        yield
    finally:
        reconnect_task.cancel()
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


def _lease_is_live(now: float | None = None) -> bool:
    return bool(_desktop_lease and _desktop_lease["expires_at"] > (now or time.monotonic()))


def _desktop_command_kind(command: str) -> str:
    """Classify without logging command contents, which may contain secrets."""
    lowered = command.lower()
    if "xdotool" in lowered:
        return "desktop_input"
    if "obx-shot" in lowered or "scrot" in lowered:
        return "desktop_capture"
    if "/tmp/obx-screen.png" in lowered and "obx-file" in lowered:
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
    if request.url.path in ("/alive", "/docs", "/openapi.json", "/terminal"):
        return await call_next(request)
    api_key = request.headers.get("X-API-Key", "")
    if SESSION_API_KEY and api_key != SESSION_API_KEY:
        return JSONResponse(status_code=403, content={"detail": "Invalid API Key"})
    return await call_next(request)

# --- 健康检查 ---
@app.get("/alive")
async def alive():
    return {
        "status": "ok",
        "version": ACTION_SERVER_VERSION,
        "capabilities": [
            "desktop_lease_v1",
            "execution_trace_v1",
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
    workdir = req.workdir or "/workspace"
    try:
        process = await asyncio.create_subprocess_shell(
            req.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            start_new_session=True,
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
    dest_path = Path(destination)
    dest_path.mkdir(parents=True, exist_ok=True)
    file_path = dest_path / file.filename
    try:
        content = await file.read()
        file_path.write_bytes(content)
        return {"message": "File uploaded", "path": str(file_path), "size": len(content)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- 下载文件 ---
@app.get("/download")
async def download_file(path: str):
    file_path = Path(path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {path}")
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
            if f.is_file():
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
    target = Path(req.path)
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {req.path}")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a directory: {req.path}")
    entries = []
    for item in sorted(target.iterdir()):
        try:
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

# --- Write File ---
@app.post("/write_file")
async def write_file(req: WriteFileRequest):
    """Write content to a file. Creates parent directories if needed."""
    file_path = Path(req.path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(req.content, encoding="utf-8")
    return {"message": "File written", "path": str(file_path), "size": len(req.content)}

# --- Read File ---
@app.post("/read_file")
async def read_file(req: ReadFileRequest):
    """Read file content with line numbers (cat -n format)."""
    file_path = Path(req.path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {req.path}")
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
    base = Path(req.path)
    if not base.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {req.path}")

    matches = []
    try:
        for p in base.glob(req.pattern):
            if p.is_file():
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
    cmd_parts = ["grep", "-rn", "--color=never"]

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

    cmd_parts.extend(["-m", str(req.max_results), "--", req.pattern, req.path])

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd_parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
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
    workdir = req.workdir or "/workspace"

    async def event_generator():
        try:
            process = await asyncio.create_subprocess_shell(
                req.command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                start_new_session=True,
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
async def terminal_ws(ws: WebSocket, api_key: str = Query("")):
    # Authenticate via query parameter (WebSocket doesn't go through HTTP middleware)
    if SESSION_API_KEY and api_key != SESSION_API_KEY:
        await ws.accept()
        await ws.close(code=4003, reason="Invalid API Key")
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

        # Switch to sandbox user
        try:
            import pwd
            pw = pwd.getpwnam("sandbox")
            os.setgid(pw.pw_gid)
            os.setuid(pw.pw_uid)
            home = pw.pw_dir
        except (KeyError, PermissionError):
            home = os.environ.get("HOME", "/root")

        env = {
            "TERM": "xterm-256color",
            "HOME": home,
            "USER": os.environ.get("USER", "sandbox"),
            "LANG": "C.UTF-8",
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "SHELL": "/bin/bash",
        }

        os.chdir("/workspace")
        os.execve("/bin/bash", ["bash", "--login"], env)

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
            ["npm", "run", "start-relay"],
            cwd=str(relay_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "HOST": "127.0.0.1", "PORT": "9222"},
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
    if SESSION_API_KEY and api_key != SESSION_API_KEY:
        await ws.accept()
        await ws.close(code=4003, reason="Invalid API Key")
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


#: URL schemes accepted for skill installation.
#: Git's ``ext::`` transport runs an arbitrary shell command as part of the
#: clone, so an unrestricted URL is remote code execution rather than a
#: download. Clones additionally pass -c protocol.ext.allow=never as defence in
#: depth for redirects and submodules.
_SKILL_URL_SCHEMES = ("https://", "http://", "git://", "ssh://", "git@")


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
    # Belt and braces: confirm the join really lands inside the skills tree.
    try:
        resolved = (SKILLS_DIR / cleaned).resolve()
        if not resolved.is_relative_to(SKILLS_DIR.resolve()):
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
            ["bash", str(install_sh)],
            capture_output=True, text=True, timeout=120,
            cwd=str(target),
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
    if not SKILLS_DIR.exists():
        return
    for skill_dir in SKILLS_DIR.iterdir():
        if not skill_dir.is_dir():
            continue
        for skill_md in _find_skill_mds(skill_dir):
            skill_data_dir = skill_md.parent
            meta = _parse_skill_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
            skill_name = meta.get("name") or skill_data_dir.name
            # If the name differs from the install dir, create a convenience symlink
            if skill_name != skill_dir.name:
                link_path = SKILLS_DIR / skill_name
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
    user = _scan_skills_in_dir(SKILLS_DIR, source="container")

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


def _user_skill_directory(name: str) -> tuple[str, Path]:
    """Resolve a chat-created user skill, never a builtin or alias."""
    skill_name = _strict_skill_slug(name)
    target = SKILLS_DIR / skill_name
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

    for current, dirnames, filenames in os.walk(target, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_dirs = []
        for dirname in sorted(dirnames):
            child = current_path / dirname
            relative = child.relative_to(target)
            if child.is_symlink() or _skip_skill_archive_path(relative):
                continue
            kept_dirs.append(dirname)
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
                if len(files) + 1 > _SKILL_ARCHIVE_MAX_FILES:
                    raise HTTPException(status_code=413, detail="Skill contains too many files to archive")
                if total_size + size > _SKILL_ARCHIVE_MAX_TOTAL_BYTES:
                    raise HTTPException(status_code=413, detail="Skill is too large to archive")
                with os.fdopen(descriptor, "rb", closefd=True) as input_file:
                    descriptor = None
                    content = input_file.read(size + 1)
                if len(content) > size:
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
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            bundle.writestr(entry, content, compresslevel=6)
    return skill_name, archive.getvalue()


@app.get("/skills")
async def list_skills():
    """List all installed skills (builtin + user)."""
    return _scan_skills()


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

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    target = SKILLS_DIR / skill_name
    if target.exists() or target.is_symlink():
        raise HTTPException(status_code=409, detail=f"Skill '{skill_name}' already exists")

    staging = SKILLS_DIR / f".{skill_name}.{secrets.token_hex(6)}.incoming"
    try:
        staging.mkdir(mode=0o700)
        (staging / "SKILL.md").write_text(req.skill_md, encoding="utf-8")
        for relative, content in validated_files:
            destination = staging.joinpath(*relative.parts)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")

        # The staging directory lives beside the destination, so rename is an
        # atomic publication of a completely validated package. Creation never
        # overwrites an existing install.
        staging.replace(target)
    except FileExistsError:
        raise HTTPException(status_code=409, detail=f"Skill '{skill_name}' already exists")
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    created = next(
        (
            skill for skill in _scan_skills_in_dir(SKILLS_DIR, source="container")
            if skill.get("install_dir") == skill_name and skill.get("name") == skill_name
        ),
        None,
    )
    if not created:
        # This should be unreachable because the request was validated before
        # publication, but do not report success for an undiscoverable skill.
        shutil.rmtree(target, ignore_errors=True)
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
    if SKILL_EXPORTS_DIR.is_symlink():
        raise HTTPException(status_code=400, detail="Skill export directory cannot be a symlink")
    SKILL_EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    if not SKILL_EXPORTS_DIR.is_dir():
        raise HTTPException(status_code=500, detail="Skill export directory is unavailable")

    filename = f"{skill_name}.zip"
    destination = SKILL_EXPORTS_DIR / filename
    staging = SKILL_EXPORTS_DIR / f".{filename}.{secrets.token_hex(6)}.tmp"
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
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)

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
        target = SKILLS_DIR / skill_name
        # Clone into a staging directory and swap it in only once it is whole.
        # Removing the old copy up front meant a failed clone left the user with
        # neither the new skill nor the one they already had.
        staging = SKILLS_DIR / f".{skill_name}.incoming"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        try:
            result = subprocess.run(
                [
                    "git",
                    # ext:: hands the URL to a shell; no skill install needs it.
                    "-c", "protocol.ext.allow=never",
                    "-c", "protocol.file.allow=never",
                    "clone", "--depth=1", "--", url, str(staging),
                ],
                capture_output=True, text=True, timeout=60,
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
            if target.exists():
                shutil.rmtree(target)
            staging.replace(target)
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
        target = SKILLS_DIR / skill_name
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(req.content, encoding="utf-8")
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
async def upload_skill_archive(file: UploadFile = File(...), name: str = Form("")):
    """Install a skill from an uploaded archive (zip/tar/tar.gz/tgz/rar).

    Extracts the archive, validates it contains SKILL.md in a standard layout,
    and installs to /data/skills/{name}/.
    """
    import zipfile
    import tarfile

    SKILLS_DIR.mkdir(parents=True, exist_ok=True)

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

    # Read file content
    content_bytes = await file.read()
    if len(content_bytes) > 50 * 1024 * 1024:  # 50MB limit
        raise HTTPException(status_code=400, detail="Archive too large (max 50MB)")

    # Determine skill name
    skill_name = name.strip() if name.strip() else filename.rsplit(".", 1)[0]
    # Clean up double extensions like .tar.gz
    for suffix in (".tar", ".tgz"):
        if skill_name.endswith(suffix):
            skill_name = skill_name[:-len(suffix)]
    # Both the explicit name and the uploaded filename are caller-controlled and
    # end up joined onto SKILLS_DIR, where the existing copy is rmtree'd.
    skill_name = _safe_skill_name(skill_name)

    # Extract to temp dir first for validation
    tmp_dir = Path(f"/tmp/skill_upload_{skill_name}_{os.getpid()}")
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)

    try:
        # Extract archive
        if archive_type == "zip":
            import io
            with zipfile.ZipFile(io.BytesIO(content_bytes)) as zf:
                # Security: check for path traversal
                for member in zf.namelist():
                    if ".." in member or member.startswith("/"):
                        raise HTTPException(status_code=400, detail=f"Unsafe path in archive: {member}")
                zf.extractall(tmp_dir)

        elif archive_type in ("tar", "tar.gz"):
            import io
            mode = "r:gz" if archive_type == "tar.gz" else "r:"
            with tarfile.open(fileobj=io.BytesIO(content_bytes), mode=mode) as tf:
                # Security: check for path traversal
                for member in tf.getmembers():
                    if ".." in member.name or member.name.startswith("/"):
                        raise HTTPException(status_code=400, detail=f"Unsafe path in archive: {member.name}")
                # The name check above cannot see a link escape: an archive can
                # carry `link -> /` and then `link/etc/x`, and no member name
                # contains "..". filter="data" is what refuses absolute and
                # upward links, plus devices and setuid bits, during extraction.
                try:
                    tf.extractall(tmp_dir, filter="data")
                except tarfile.TarError as e:
                    raise HTTPException(status_code=400, detail=f"Unsafe archive rejected: {e}")

        elif archive_type == "rar":
            # Write to temp file for rarfile/unrar
            tmp_file = tmp_dir / "archive.rar"
            tmp_file.write_bytes(content_bytes)
            try:
                result = subprocess.run(
                    ["unrar", "x", "-y", str(tmp_file), str(tmp_dir) + "/"],
                    capture_output=True, text=True, timeout=30,
                )
                if result.returncode != 0:
                    raise HTTPException(status_code=400, detail=f"RAR extraction failed: {result.stderr.strip()}")
            except FileNotFoundError:
                raise HTTPException(status_code=400, detail="RAR not supported: 'unrar' not installed in container")
            finally:
                tmp_file.unlink(missing_ok=True)

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
        target = SKILLS_DIR / skill_name
        staging = SKILLS_DIR / f".{skill_name}.incoming"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        shutil.move(str(extract_root), str(staging))
        if target.exists():
            shutil.rmtree(target)
        staging.replace(target)

    finally:
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
async def uninstall_skill(name: str):
    """Uninstall a user-installed skill. Builtin skills cannot be deleted."""
    # Check if it's a builtin skill — reject deletion
    builtin_skills = _scan_skills_in_dir(BUILTIN_SKILLS_DIR, source="builtin")
    for skill in builtin_skills:
        if skill["name"] == name or skill.get("install_dir") == name:
            raise HTTPException(status_code=403, detail=f"Cannot uninstall builtin skill '{name}'")

    # Direct match by directory name in user skills. The name is joined onto
    # SKILLS_DIR and handed to rmtree, so it has to be checked first.
    name = _safe_skill_name(name)
    target = SKILLS_DIR / name
    if target.exists() and not target.is_symlink():
        shutil.rmtree(target)
        # Also remove any symlinks that pointed into this directory
        _cleanup_broken_symlinks()
        return {"ok": True, "message": f"Skill '{name}' uninstalled"}

    # If it's a symlink (alias), remove the symlink
    if target.is_symlink():
        target.unlink()
        return {"ok": True, "message": f"Skill alias '{name}' removed"}

    # Search by skill name (from frontmatter) in user skills only
    user_skills = _scan_skills_in_dir(SKILLS_DIR, source="container")
    for skill in user_skills:
        if skill["name"] == name:
            install_dir = skill.get("install_dir")
            if install_dir:
                t = SKILLS_DIR / install_dir
                if t.exists():
                    shutil.rmtree(t)
                    _cleanup_broken_symlinks()
                    return {"ok": True, "message": f"Skill '{name}' uninstalled"}
    raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")


def _cleanup_broken_symlinks():
    """Remove broken symlinks in SKILLS_DIR after a skill directory is deleted."""
    if not SKILLS_DIR.exists():
        return
    for entry in SKILLS_DIR.iterdir():
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

    def __init__(self, url: str, headers: dict[str, str] | None = None, timeout: int = 60):
        self.url = url
        self._session_id: str | None = None
        self._request_id = 0
        self._server_info: dict = {}
        self._client: "httpx.AsyncClient | None" = None
        self._extra_headers = headers or {}
        self._timeout = timeout

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _ensure_client(self):
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(timeout=float(self._timeout))

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

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._extra_headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        resp = await self._client.post(self.url, json=payload, headers=headers)
        resp.raise_for_status()

        # Store session ID from response
        if "mcp-session-id" in resp.headers:
            self._session_id = resp.headers["mcp-session-id"]

        content_type = resp.headers.get("content-type", "")

        if "text/event-stream" in content_type:
            # Parse SSE response to extract JSON-RPC result
            return self._parse_sse_response(resp.text, req_id)
        else:
            # Direct JSON response
            data = resp.json()
            if "error" in data:
                raise Exception(f"MCP error: {data['error']}")
            return data.get("result", {})

    def _parse_sse_response(self, text: str, expected_id: int) -> dict:
        """Parse SSE text to find the JSON-RPC response matching our request ID."""
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("data:"):
                data_str = line[5:].strip()
                if not data_str:
                    continue
                try:
                    data = json.loads(data_str)
                    if data.get("id") == expected_id:
                        if "error" in data:
                            raise Exception(f"MCP error: {data['error']}")
                        return data.get("result", {})
                except json.JSONDecodeError:
                    continue
        raise Exception("No matching JSON-RPC response found in SSE stream")

    async def _send_notification(self, method: str, params: dict | None = None):
        """Send a JSON-RPC notification (no id, no response expected)."""
        await self._ensure_client()

        payload = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id

        resp = await self._client.post(self.url, json=payload, headers=headers)
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

    async def list_tools(self) -> list[dict]:
        """List available tools from the MCP server."""
        result = await self._send_request("tools/list", {})
        return result.get("tools", [])

    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """Call a tool on the MCP server."""
        result = await self._send_request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })
        return result

    async def list_resources(self) -> list[dict]:
        """List available resources from the MCP server."""
        result = await self._send_request("resources/list", {})
        return result.get("resources", [])

    async def read_resource(self, uri: str) -> dict:
        """Read a specific resource by URI."""
        return await self._send_request("resources/read", {"uri": uri})

    async def list_prompts(self) -> list[dict]:
        """List available prompts from the MCP server."""
        result = await self._send_request("prompts/list", {})
        return result.get("prompts", [])

    async def get_prompt(self, name: str, arguments: dict | None = None) -> dict:
        """Get a specific prompt by name."""
        params: dict = {"name": name}
        if arguments:
            params["arguments"] = arguments
        return await self._send_request("prompts/get", params)

    async def close(self):
        """Close the HTTP client."""
        if self._client:
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


class ContainerMcpManager:
    """Manages MCP servers reachable from the container.

    Sessions are opened per operation rather than held open across requests.

    The SDK's stdio_client / sse_client and ClientSession are all built on
    ``anyio.create_task_group()``, and anyio requires a task group to be exited
    by the same task that entered it. A manager that opened a transport inside
    the POST /connect request, stashed the context manager, and closed it later
    from POST /disconnect violated that rule outright. It also never entered
    ClientSession at all, so BaseSession._receive_loop never ran and the very
    first initialize() blocked forever waiting for a reply nobody was reading.

    Opening and closing inside one ``async with`` keeps every task group inside
    a single task, which is the only shape anyio supports here. ``connect`` is
    therefore a probe: it dials the server, caches the tool/resource/prompt
    listing, and hangs up. Each later call redials. For stdio that costs one
    process spawn per call, which is the price of a transport that cannot be
    parked between requests.

    Remote streamable-HTTP keeps using RawStreamableHttpSession, which is
    stateless httpx and has no task-group constraint.
    """

    #: Seconds allowed for initialize() before a server is declared unreachable.
    #: A stdio server that never speaks used to hang the request forever.
    DEFAULT_TIMEOUT = 60

    def __init__(self):
        self._servers: dict[str, dict] = {}  # name -> {"status", "error"}
        self._tools: dict[str, list[dict]] = {}  # name -> list of tool dicts
        self._resources: dict[str, list[dict]] = {}  # name -> list of resource dicts
        self._prompts: dict[str, list[dict]] = {}  # name -> list of prompt dicts
        #: Remote servers whose raw HTTP probe failed, so call paths skip
        #: straight to the SSE transport instead of paying the failure twice.
        self._remote_transport: dict[str, str] = {}  # name -> "raw" | "sse"

    # -- config persistence --

    def _load_config(self) -> dict:
        """Load MCP config from persistent storage."""
        if MCP_CONFIG_PATH.exists():
            try:
                return json.loads(MCP_CONFIG_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {"servers": {}}

    def _save_config(self, config: dict):
        """Save MCP config to persistent storage."""
        MCP_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        MCP_CONFIG_PATH.write_text(json.dumps(config, indent=2))

    def _server_config(self, name: str) -> dict:
        cfg = self._load_config().get("servers", {}).get(name)
        if not cfg:
            raise KeyError(f"MCP server '{name}' not configured")
        return cfg

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
            connected = status == "connected"
            result.append({
                "name": name,
                "type": cfg.get("type", "stdio"),
                "status": status,
                "tools": self._tools.get(name, []) if connected else [],
                "resources": self._resources.get(name, []) if connected else [],
                "prompts": self._prompts.get(name, []) if connected else [],
                "error": error,
                "command": cfg.get("command"),
                "args": cfg.get("args"),
                "url": cfg.get("url"),
            })
        return result

    def add_server(self, name: str, config: dict):
        """Add a new MCP server configuration."""
        full_config = self._load_config()
        full_config.setdefault("servers", {})[name] = config
        self._save_config(full_config)

    def remove_server(self, name: str):
        """Remove an MCP server configuration."""
        full_config = self._load_config()
        servers = full_config.get("servers", {})
        if name not in servers:
            raise KeyError(f"MCP server '{name}' not found")
        del servers[name]
        self._save_config(full_config)
        self._forget(name)

    def _forget(self, name: str) -> None:
        self._servers.pop(name, None)
        self._tools.pop(name, None)
        self._resources.pop(name, None)
        self._prompts.pop(name, None)
        self._remote_transport.pop(name, None)

    # -- session helpers: every one opens and closes within a single task --

    @asynccontextmanager
    async def _stdio_session(self, cfg: dict):
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
        env = {k: v for k, v in os.environ.items() if k not in _MCP_ENV_DENYLIST}
        env.update(cfg.get("env") or {})

        params = StdioServerParameters(
            command=command, args=cfg.get("args") or [], env=env,
        )
        timeout = self._timeout(cfg)
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await asyncio.wait_for(session.initialize(), timeout=timeout)
                yield session

    @asynccontextmanager
    async def _sse_session(self, cfg: dict):
        """Open an SSE MCP session for the duration of the block."""
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        url = cfg.get("url", "")
        headers = cfg.get("headers") or None
        timeout = self._timeout(cfg)
        async with sse_client(url, headers=headers) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await asyncio.wait_for(session.initialize(), timeout=timeout)
                yield session

    @asynccontextmanager
    async def _raw_session(self, cfg: dict):
        """Open a raw streamable-HTTP MCP session for the duration of the block."""
        url = cfg.get("url", "")
        session = RawStreamableHttpSession(
            url, headers=cfg.get("headers") or {}, timeout=int(self._timeout(cfg)),
        )
        try:
            await asyncio.wait_for(session.initialize(), timeout=self._timeout(cfg))
            yield session
        finally:
            try:
                await session.close()
            except Exception:
                pass

    @asynccontextmanager
    async def _session(self, name: str):
        """Open a session to ``name`` using whichever transport it needs.

        Remote servers try raw streamable HTTP first and fall back to SSE. The
        winning transport is remembered so later calls do not re-pay a failed
        probe.
        """
        cfg = self._server_config(name)
        server_type = cfg.get("type", "stdio")

        if server_type == "stdio":
            async with self._stdio_session(cfg) as session:
                yield session
            return

        if server_type != "remote":
            raise ValueError(f"Unknown MCP server type: {server_type}")

        if not cfg.get("url"):
            raise ValueError("Remote MCP server requires 'url'")

        preferred = self._remote_transport.get(name)
        if preferred == "sse":
            async with self._sse_session(cfg) as session:
                yield session
            return

        try:
            cm = self._raw_session(cfg)
            session = await cm.__aenter__()
        except Exception as e:
            # The raw probe failed before yielding, so nothing needs unwinding
            # here and SSE is still worth a try.
            print(f"[MCP] Raw HTTP failed for '{name}': {e}, trying SDK SSE...")
            self._remote_transport[name] = "sse"
            async with self._sse_session(cfg) as session:
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
    def _tools_from(session, name: str, raw: list | object) -> list[dict]:
        if isinstance(session, RawStreamableHttpSession):
            return [
                {"name": t.get("name", ""), "description": t.get("description", "") or "",
                 "input_schema": t.get("inputSchema", {}) or {}, "server": name}
                for t in (raw or [])
            ]
        return [
            {"name": t.name, "description": t.description or "",
             "input_schema": _mcp_attr(t, "input_schema", "inputSchema", default={}) or {}, "server": name}
            for t in raw.tools
        ]

    async def _discover(self, name: str, session) -> None:
        """Cache the tool/resource/prompt listing for ``name``."""
        is_raw = isinstance(session, RawStreamableHttpSession)

        raw_tools = await (session.list_tools() if is_raw else session.list_tools())
        self._tools[name] = self._tools_from(session, name, raw_tools)

        # Resources and prompts are optional in the protocol; a server that does
        # not implement them answers with an error, which is not a connect
        # failure. Tools have already been cached by this point.
        try:
            res = await session.list_resources()
            if is_raw:
                self._resources[name] = [
                    {"uri": r.get("uri", ""), "name": r.get("name", ""),
                     "description": r.get("description", ""),
                     "mimeType": r.get("mimeType", ""), "server": name}
                    for r in (res or [])
                ]
            else:
                self._resources[name] = [
                    {"uri": str(r.uri), "name": r.name or "",
                     "description": getattr(r, "description", "") or "",
                     "mimeType": _mcp_attr(r, "mime_type", "mimeType", default="") or "", "server": name}
                    for r in res.resources
                ]
        except Exception:
            self._resources[name] = []

        try:
            pr = await session.list_prompts()
            if is_raw:
                self._prompts[name] = [
                    {"name": p.get("name", ""), "description": p.get("description", ""),
                     "arguments": p.get("arguments", []), "server": name}
                    for p in (pr or [])
                ]
            else:
                self._prompts[name] = [
                    {"name": p.name, "description": p.description or "",
                     "arguments": [
                         {"name": a.name, "description": getattr(a, "description", "") or "",
                          "required": getattr(a, "required", False)}
                         for a in (p.arguments or [])
                     ],
                     "server": name}
                    for p in pr.prompts
                ]
        except Exception:
            self._prompts[name] = []

    async def connect(self, name: str):
        """Probe an MCP server and cache what it offers.

        Nothing stays open afterwards — 'connected' means the last probe
        succeeded and the cached listing is usable.
        """
        self._server_config(name)  # raises KeyError when unknown
        try:
            async with self._session(name) as session:
                await self._discover(name, session)
            self._servers[name] = {"status": "connected", "error": None}
        except asyncio.TimeoutError:
            self._servers[name] = {
                "status": "error",
                "error": "Timed out waiting for the MCP server to initialize",
            }
            self._tools.pop(name, None)
            raise
        except Exception as e:
            self._servers[name] = {"status": "error", "error": str(e)}
            self._tools.pop(name, None)
            raise

    async def disconnect(self, name: str):
        """Drop a server's cached listing and mark it disconnected.

        No live resources are held between requests, so there is nothing to
        close here.
        """
        self._forget(name)

    def get_all_tools(self) -> list[dict]:
        """Get all tools from all connected servers."""
        all_tools = []
        for name, tools in self._tools.items():
            if (self._servers.get(name) or {}).get("status") == "connected":
                all_tools.extend(tools)
        return all_tools

    def _require_connected(self, name: str) -> None:
        if (self._servers.get(name) or {}).get("status") != "connected":
            raise KeyError(f"MCP server '{name}' is not connected")

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> dict:
        """Call a tool on a connected MCP server."""
        self._require_connected(server_name)
        async with self._session(server_name) as session:
            if isinstance(session, RawStreamableHttpSession):
                result = await session.call_tool(tool_name, arguments)
                content_parts = []
                for part in result.get("content", []):
                    if isinstance(part, dict):
                        content_parts.append(part)
                    else:
                        content_parts.append({"type": "text", "text": str(part)})
                return {"content": content_parts, "isError": result.get("isError", False)}

            result = await session.call_tool(tool_name, arguments)
            content_parts = []
            for part in result.content:
                if hasattr(part, "text"):
                    content_parts.append({"type": "text", "text": part.text})
                elif hasattr(part, "data"):
                    content_parts.append({"type": "resource", "data": str(part.data)})
                else:
                    content_parts.append({"type": "unknown", "value": str(part)})
            return {"content": content_parts,
                    "isError": bool(_mcp_attr(result, "is_error", "isError", default=False))}

    def get_all_resources(self) -> list[dict]:
        """Get all resources from all connected servers."""
        all_res = []
        for name, res in self._resources.items():
            if (self._servers.get(name) or {}).get("status") == "connected":
                all_res.extend(res)
        return all_res

    async def read_resource(self, server_name: str, uri: str) -> dict:
        """Read a resource from a connected MCP server."""
        self._require_connected(server_name)
        async with self._session(server_name) as session:
            if isinstance(session, RawStreamableHttpSession):
                return await session.read_resource(uri)
            result = await session.read_resource(uri)
            contents = []
            for part in result.contents:
                if hasattr(part, "text"):
                    contents.append({"uri": str(part.uri), "text": part.text,
                                     "mimeType": _mcp_attr(part, "mime_type", "mimeType", default="")})
                elif hasattr(part, "blob"):
                    contents.append({"uri": str(part.uri), "blob": part.blob,
                                     "mimeType": _mcp_attr(part, "mime_type", "mimeType", default="")})
                else:
                    contents.append({"uri": str(part.uri), "text": str(part)})
            return {"contents": contents}

    def get_all_prompts(self) -> list[dict]:
        """Get all prompts from all connected servers."""
        all_pr = []
        for name, pr in self._prompts.items():
            if (self._servers.get(name) or {}).get("status") == "connected":
                all_pr.extend(pr)
        return all_pr

    async def get_prompt(self, server_name: str, prompt_name: str, arguments: dict | None = None) -> dict:
        """Get a prompt from a connected MCP server."""
        self._require_connected(server_name)
        async with self._session(server_name) as session:
            if isinstance(session, RawStreamableHttpSession):
                return await session.get_prompt(prompt_name, arguments)
            result = await session.get_prompt(prompt_name, arguments)
            messages = []
            for msg in result.messages:
                parts = []
                content = msg.content
                if isinstance(content, list):
                    for p in content:
                        if hasattr(p, "text"):
                            parts.append({"type": "text", "text": p.text})
                elif hasattr(content, "text"):
                    parts.append({"type": "text", "text": content.text})
                messages.append({"role": msg.role, "content": parts})
            return {"messages": messages}

    async def refresh_server(self, name: str) -> dict:
        """Re-fetch tools/resources/prompts from a connected server."""
        self._require_connected(name)
        old_tools = len(self._tools.get(name, []))
        async with self._session(name) as session:
            await self._discover(name, session)
        return {
            "tools": len(self._tools.get(name, [])),
            "resources": len(self._resources.get(name, [])),
            "prompts": len(self._prompts.get(name, [])),
            "tools_changed": old_tools != len(self._tools.get(name, [])),
        }

    async def reconnect_configured(self) -> None:
        """Reconnect every configured server, as a background startup task.

        Runs after the app is serving rather than inside lifespan setup: a
        server that is slow or gone must not hold the container's startup, and
        each failure is recorded for the UI instead of raised.
        """
        names = list(self._load_config().get("servers", {}).keys())
        if not names:
            return
        print(f"[MCP] Reconnecting {len(names)} configured server(s)...")
        for name in names:
            try:
                await self.connect(name)
                print(f"[MCP] Reconnected '{name}' ({len(self._tools.get(name, []))} tools)")
            except Exception as e:
                print(f"[MCP] Reconnect failed for '{name}': {e}")


# Singleton MCP manager
mcp_manager = ContainerMcpManager()


@app.get("/mcp/servers")
async def list_mcp_servers():
    """List all configured MCP servers and their status."""
    return mcp_manager.list_servers()


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
    mcp_manager.add_server(req.name, config)
    return {"ok": True, "message": f"MCP server '{req.name}' added"}


@app.delete("/mcp/servers/{name}")
async def remove_mcp_server(name: str):
    """Remove an MCP server configuration."""
    try:
        # remove_server drops the cached listing too; nothing is held open
        # between requests, so there is no session to close first.
        mcp_manager.remove_server(name)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"MCP server '{name}' not found")
    return {"ok": True, "message": f"MCP server '{name}' removed"}


@app.post("/mcp/servers/{name}/connect")
async def connect_mcp_server(name: str):
    """Start and connect to an MCP server."""
    try:
        await mcp_manager.connect(name)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to connect: {e}")
    return {"ok": True, "message": f"Connected to '{name}'"}


@app.post("/mcp/servers/{name}/disconnect")
async def disconnect_mcp_server(name: str):
    """Disconnect from an MCP server."""
    await mcp_manager.disconnect(name)
    return {"ok": True, "message": f"Disconnected from '{name}'"}


@app.get("/mcp/tools")
async def list_mcp_tools():
    """List all tools from all connected MCP servers."""
    return mcp_manager.get_all_tools()


@app.post("/mcp/tools/{server_name}/{tool_name}")
async def call_mcp_tool(server_name: str, tool_name: str, req: CallMcpToolRequest):
    """Call a tool on a connected MCP server."""
    try:
        result = await mcp_manager.call_tool(server_name, tool_name, req.arguments)
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
async def list_mcp_resources():
    """List all resources from all connected MCP servers."""
    return mcp_manager.get_all_resources()


@app.post("/mcp/resources/read")
async def read_mcp_resource(req: ReadMcpResourceRequest):
    """Read a specific resource from a connected MCP server."""
    try:
        return await mcp_manager.read_resource(req.server, req.uri)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Resource read failed: {e}")


@app.get("/mcp/prompts")
async def list_mcp_prompts():
    """List all prompts from all connected MCP servers."""
    return mcp_manager.get_all_prompts()


@app.post("/mcp/prompts/get")
async def get_mcp_prompt(req: GetMcpPromptRequest):
    """Get a specific prompt from a connected MCP server."""
    try:
        return await mcp_manager.get_prompt(req.server, req.name, req.arguments)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Prompt get failed: {e}")


@app.post("/mcp/servers/{name}/refresh")
async def refresh_mcp_server(name: str):
    """Re-fetch tools/resources/prompts from a connected MCP server."""
    try:
        return await mcp_manager.refresh_server(name)
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
