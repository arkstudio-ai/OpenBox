"""HTTP client for communicating with the Action Server inside a sandbox container."""
import asyncio
import base64
import copy
import contextvars
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import socket
import time
from dataclasses import dataclass
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable, Literal
from urllib.parse import quote

import httpx

from core.log import create_logger
from skill.archive import (
    SKILL_ARCHIVE_MAX_COMPRESSED_BYTES,
    SkillArchiveValidationError,
    validate_skill_zip,
)

log = create_logger("sandbox.client")

#: Backstop for an MCP tool call, in seconds.
#: Deliberately well above the container's own per-server timeout (60s by
#: default) so that the container decides when a server has taken too long and
#: can say so. An outer budget equal to the inner one just races it and
#: replaces a useful message with a timeout.
MCP_CALL_TIMEOUT_SECONDS = 180.0
CATALOGUE_CACHE_TTL_SECONDS = 2.0

# Request-level callers may add conditional or content headers, but transport
# identity is owned exclusively by SandboxClient. Clearing the complete set
# before each send prevents an old/spoofed run header from surviving when the
# current context intentionally has no Agent lease.
_PROTECTED_REQUEST_HEADER_NAMES = (
    "X-API-Key",
    "X-OpenBox-User-Scope",
    "X-OpenBox-Instance",
    "X-OpenBox-Request",
    "X-OpenBox-Session",
    "X-OpenBox-Tool-Call",
    "X-OpenBox-Operation",
    "X-OpenBox-Desktop-Lease",
    "X-OpenBox-Run",
    "X-OpenBox-Run-Epoch",
    "X-OpenBox-Run-Lease-Expires",
    "X-OpenBox-Run-Lease-Signature",
)


def _run_lease_signature(
    api_key: str,
    session_id: str,
    run_id: str,
    generation: int,
    expires_at_ms: int,
) -> str:
    payload = (
        f"{session_id}\n{run_id}\n{generation}\n{expires_at_ms}"
    ).encode("utf-8")
    return hmac.new(api_key.encode("utf-8"), payload, hashlib.sha256).hexdigest()


# Older long-lived WUYING desktops expose the generic file/execute API and the
# existing Skill install routes, but not the newer dedicated create/export
# endpoints. This self-contained helper is sent only when that endpoint is
# absent. It creates a deterministic, symlink-free snapshot on the desktop;
# no user content is interpolated into the command itself.
_LEGACY_SKILL_EXPORT_SCRIPT = r'''
import json
import os
import re
import secrets
import stat
import sys
import zipfile
import zlib
from pathlib import Path, PurePosixPath

ROOT = Path("/data/skills")
EXPORTS = Path("/workspace/exports")
MAX_FILES = 1000
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_BYTES = 50 * 1024 * 1024
MAX_ARCHIVE_BYTES = 50 * 1024 * 1024
MAX_RATIO = 200
RATIO_MIN_BYTES = 1024 * 1024
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build", ".next", ".cache"}
SECRET_NAMES = {"credentials", "credentials.json", "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa", "password", "passwords", "secret", "secrets", "token", "tokens"}
SECRET_DIRS = {"credentials", "keys", "private-keys", "secrets"}
SECRET_SUFFIXES = (".jks", ".key", ".keystore", ".p12", ".pem", ".pfx", ".secret")
SECRET_STEMS = {"access-key", "api-key", "api_key", "apikey", "credential", "credentials", "password", "passwords", "private-key", "secret", "secrets", "service-account", "token", "tokens"}

def secret_path(path):
    parts = PurePosixPath(path.as_posix()).parts
    if any(part.casefold() in SECRET_DIRS for part in parts[:-1]):
        return True
    name = parts[-1].casefold()
    if name == ".env" or name.startswith(".env."):
        return True
    if name in SECRET_NAMES or name.endswith(SECRET_SUFFIXES):
        return True
    return name.split(".", 1)[0] in SECRET_STEMS

def skipped(path):
    if any(part.startswith(".") or part in SKIP_DIRS for part in path.parts):
        return True
    if path.name.casefold() == "install.sh":
        return True
    return secret_path(path)

name = sys.argv[1]
if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name) or len(name) > 64:
    raise SystemExit("invalid skill slug")
target = ROOT / name
if target.is_symlink() or not target.is_dir():
    raise SystemExit("personal skill not found")
skill_md = target / "SKILL.md"
if skill_md.is_symlink() or not skill_md.is_file():
    raise SystemExit("root SKILL.md not found")
target_root = target.resolve()
files = []
total = 0
# The archive adds ``name/`` as an implicit top-level directory.
entries = 1
for current, dirs, names in os.walk(target, topdown=True, followlinks=False):
    current_path = Path(current)
    kept = []
    for dirname in sorted(dirs):
        child = current_path / dirname
        relative = child.relative_to(target)
        if not child.is_symlink() and not skipped(relative):
            kept.append(dirname)
            entries += 1
            if entries > MAX_FILES:
                raise SystemExit("skill archive exceeds safety limits")
    dirs[:] = kept
    for filename in sorted(names):
        source = current_path / filename
        relative = source.relative_to(target)
        if skipped(relative):
            continue
        descriptor = None
        try:
            if not stat.S_ISREG(source.lstat().st_mode):
                continue
            if not source.resolve(strict=True).is_relative_to(target_root):
                continue
            flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(source, flags)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                continue
            size = opened.st_size
            entries += 1
            if size > MAX_FILE_BYTES or entries > MAX_FILES or total + size > MAX_BYTES:
                raise SystemExit("skill archive exceeds safety limits")
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = None
                content = stream.read(size + 1)
            if len(content) != size:
                raise SystemExit("skill changed during archive")
        finally:
            if descriptor is not None:
                os.close(descriptor)
        files.append((relative, content))
        total += size

EXPORTS.mkdir(parents=True, exist_ok=True)
if EXPORTS.is_symlink():
    raise SystemExit("export directory cannot be a symlink")
destination = EXPORTS / (name + ".zip")
temporary = EXPORTS / ("." + name + "." + secrets.token_hex(6) + ".tmp")
try:
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for relative, content in files:
            archive_name = (PurePosixPath(name) / PurePosixPath(relative.as_posix())).as_posix()
            entry = zipfile.ZipInfo(archive_name, date_time=(1980, 1, 1, 0, 0, 0))
            compressed = zlib.compress(content, 6)
            entry.compress_type = (
                zipfile.ZIP_STORED
                if len(content) >= RATIO_MIN_BYTES
                and len(content) > max(1, len(compressed)) * MAX_RATIO
                else zipfile.ZIP_DEFLATED
            )
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            bundle.writestr(entry, content, compresslevel=6)
    if temporary.stat().st_size > MAX_ARCHIVE_BYTES:
        raise SystemExit("skill archive exceeds compressed safety limit")
    os.replace(temporary, destination)
finally:
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
print(json.dumps({"path": str(destination), "filename": destination.name, "size": destination.stat().st_size}))
'''

_PROCESS_INSTANCE_ID = (
    os.environ.get("OPENBOX_INSTANCE_ID", "").strip()
    or f"{socket.gethostname()}-{os.getpid()}-{secrets.token_hex(4)}"
)


@dataclass
class ExecuteResult:
    """Result of a command execution."""
    exit_code: int
    stdout: str
    stderr: str


@dataclass
class OutputChunk:
    """A streaming output chunk from execute_stream."""
    type: str  # "stdout", "stderr", "system"
    content: str


@dataclass
class IdleNotification:
    """Notification that the command has produced no output for a while."""
    idle_seconds: int
    total_seconds: int
    pid: int


@dataclass(frozen=True)
class PathResolveTarget:
    """One execution-plane path that must be resolved before authorization."""

    path: str
    allow_missing: bool = False
    allow_scoped_skills: bool = False


@dataclass(frozen=True)
class ResolvedPath:
    """Canonical path projections returned by the confined resolver."""

    canonical_path: str
    workspace_relative: str | None = None


@dataclass(frozen=True)
class RequestTrace:
    session_id: str = ""
    tool_call_id: str = ""
    operation: str = ""
    lease_token: str = ""


@dataclass
class _CatalogueCacheEntry:
    snapshot: dict
    etag: str | None
    expires_at: float


CatalogueAvailability = Literal["available", "stale", "unavailable"]


@dataclass(frozen=True)
class CatalogueProjectionState:
    """One catalogue read plus how trustworthy that read is.

    ``stale`` is a last-known-good snapshot retained across a transient tunnel
    failure. ``unavailable`` means this process has never obtained a snapshot;
    callers must not mistake an empty fail-small tool set for an authoritative
    empty remote catalogue.
    """

    availability: CatalogueAvailability
    snapshot: dict | None


@dataclass(frozen=True)
class _CatalogueLoad:
    availability: Literal["available", "stale"]
    snapshot: dict


class SkillArchiveAlreadyExistsError(FileExistsError):
    """A create-only archive upload found a live package at its install path."""

    def __init__(self, install_dir: str, message: str | None = None):
        self.install_dir = install_dir
        super().__init__(message or f"Skill '{install_dir}' already exists")


class SkillRestoreFencedError(RuntimeError):
    """A durable uninstall generation rejected a stale snapshot restore."""

    def __init__(self, install_dir: str, fenced_through_generation: int):
        self.install_dir = install_dir
        self.fenced_through_generation = fenced_through_generation
        super().__init__(
            f"Skill '{install_dir}' restore is fenced through generation "
            f"{fenced_through_generation}"
        )


class SandboxClient:
    """HTTP client for the Action Server running inside a sandbox container.

    All file and command operations go through this client to the sandbox.
    """

    def __init__(
        self,
        host: str,
        port: int,
        api_key: str,
        base_url: str | None = None,
        *,
        user_scope: str = "",
        catalogue_ttl_seconds: float = CATALOGUE_CACHE_TTL_SECONDS,
        catalogue_clock: Callable[[], float] | None = None,
    ):
        # base_url wins when set — remote providers (wuying) address the action
        # server through a tunnel endpoint rather than a host/port pair.
        self.base_url = base_url.rstrip("/") if base_url else f"http://{host}:{port}"
        self.api_key = api_key
        self._headers = {"X-API-Key": api_key}
        # The shared WUYING acceptance desktop has one Action Server but may be
        # reached by more than one backend tenant.  Only a backend-derived,
        # pseudonymous segment crosses that boundary; raw user ids never do.
        # The Action Server validates this value before using it in a path.
        self.user_scope = user_scope
        if user_scope:
            if not re.fullmatch(r"u-[0-9a-f]{20}", user_scope):
                raise ValueError("invalid sandbox user scope")
            self._headers["X-OpenBox-User-Scope"] = user_scope
        self._trace: contextvars.ContextVar[RequestTrace] = contextvars.ContextVar(
            f"sandbox_request_trace_{id(self)}", default=RequestTrace()
        )
        self._catalogue_ttl_seconds = max(0.0, float(catalogue_ttl_seconds))
        self._catalogue_clock = catalogue_clock or time.monotonic
        self._catalogue_cache: _CatalogueCacheEntry | None = None
        self._catalogue_inflight: asyncio.Task[_CatalogueLoad] | None = None
        self._catalogue_epoch = 0
        # Skill provider lifecycle is owned by this tenant-scoped client, not
        # by a process-global registry. It is created lazily by
        # skill.provider.skill_registry_for().
        self._openbox_skill_registry = None
        self._action_server_capabilities: frozenset[str] | None = None
        # Capability discovery itself uses this client's request hook.  Mark
        # that one nested /alive request so the critical run-receipt gate does
        # not recursively try to discover capabilities again.
        self._capability_probe: contextvars.ContextVar[bool] = contextvars.ContextVar(
            f"sandbox_capability_probe_{id(self)}", default=False
        )
        self._tenant_scope_capable: bool | None = None

    @staticmethod
    def _header_value(value: str, limit: int = 120) -> str:
        """Bound request metadata to visible ASCII safe for HTTP headers."""
        return "".join(ch for ch in (value or "") if 32 <= ord(ch) < 127)[:limit]

    def _request_headers(self) -> dict[str, str]:
        trace = self._trace.get()
        headers = {
            "X-OpenBox-Instance": self._header_value(_PROCESS_INSTANCE_ID),
            "X-OpenBox-Request": secrets.token_hex(8),
        }
        if trace.session_id:
            headers["X-OpenBox-Session"] = self._header_value(trace.session_id)
        if trace.tool_call_id:
            headers["X-OpenBox-Tool-Call"] = self._header_value(trace.tool_call_id)
        if trace.operation:
            headers["X-OpenBox-Operation"] = self._header_value(trace.operation, 48)
        if trace.lease_token:
            headers["X-OpenBox-Desktop-Lease"] = self._header_value(trace.lease_token, 160)
        # Database fencing must continue across the WUYING transport boundary.
        # A backend worker that lost its generation may still have network
        # access; the Action Server rejects its next side-effect by epoch.
        # Do not downgrade an import/context failure to an unfenced request.
        # A legitimate control-plane caller receives ``None`` from this helper;
        # an unexpected failure must stop transport before a stale write escapes.
        from agent.driver import current_run_transport_lease

        transport_lease = current_run_transport_lease()
        if transport_lease is not None:
            fence_session, run_id, generation, expires_at = transport_lease
            # The database lease is authoritative. A nested request context is
            # useful for tool-call metadata, but must not re-key a run fence to
            # a different session on the Action Server.
            headers["X-OpenBox-Session"] = self._header_value(fence_session)
            headers["X-OpenBox-Run"] = self._header_value(run_id)
            headers["X-OpenBox-Run-Epoch"] = str(generation)
            expires_at_ms = int(expires_at.timestamp() * 1000)
            headers["X-OpenBox-Run-Lease-Expires"] = str(expires_at_ms)
            headers["X-OpenBox-Run-Lease-Signature"] = _run_lease_signature(
                self.api_key,
                fence_session,
                run_id,
                generation,
                expires_at_ms,
            )
        return headers

    async def _merge_request_headers(self, request: httpx.Request) -> None:
        """Attach protected transport identity immediately before each send.

        ``httpx`` merges client and request headers before request hooks run.
        Writing the protected headers here therefore preserves unrelated
        business headers (conditional GETs, multipart content types, and so on)
        while preventing a per-request header mapping from replacing the API
        key, tenant scope, trace identity, or the current database run fence.

        The fence is deliberately read for every request rather than when the
        AsyncClient is created. A client can issue several requests and the
        active context may change between them; forwarding a cached generation
        would reintroduce the stale-worker write window this boundary closes.
        """
        from agent.driver import current_run_transport_lease

        transport_lease = current_run_transport_lease()
        if transport_lease is not None and not self._capability_probe.get():
            capabilities = await self._load_action_server_capabilities()
            if "run_lease_receipt_v2" not in capabilities:
                raise RuntimeError(
                    "Action Server does not enforce signed Agent run lease receipts"
                )

        for name in _PROTECTED_REQUEST_HEADER_NAMES:
            if name in request.headers:
                del request.headers[name]
        protected = {**self._headers, **self._request_headers()}
        for name, value in protected.items():
            request.headers[name] = value

    @asynccontextmanager
    async def request_context(
        self,
        *,
        session_id: str = "",
        tool_call_id: str = "",
        operation: str = "",
    ):
        """Attach caller identity to sandbox requests in the current task."""
        current = self._trace.get()
        token = self._trace.set(RequestTrace(
            session_id=session_id or current.session_id,
            tool_call_id=tool_call_id or current.tool_call_id,
            operation=operation or current.operation,
            lease_token=current.lease_token,
        ))
        try:
            yield
        finally:
            self._trace.reset(token)

    @asynccontextmanager
    async def desktop_lease(
        self,
        *,
        session_id: str,
        tool_call_id: str,
        operation: str = "computer",
        wait_timeout: float = 90.0,
        ttl_seconds: float = 180.0,
    ):
        """Hold the remote desktop across input, capture and OSS upload.

        The lease lives on the Action Server, so it serializes separate users,
        backend processes and even separate OpenBox deployments that point at
        one long-lived WUYING desktop.
        """
        current = self._trace.get()
        if current.lease_token:
            yield {"token": current.lease_token, "wait_ms": 0, "nested": True}
            return

        async with self.request_context(
            session_id=session_id,
            tool_call_id=tool_call_id,
            operation=operation,
        ):
            owner = ":".join(part for part in (
                _PROCESS_INSTANCE_ID, session_id, tool_call_id or secrets.token_hex(6)
            ) if part)
            async with self._client(timeout=wait_timeout + 15) as client:
                response = await client.post(
                    "/desktop/lease/acquire",
                    json={
                        "owner": owner,
                        "wait_timeout": wait_timeout,
                        "ttl_seconds": ttl_seconds,
                    },
                )
                response.raise_for_status()
                lease = response.json()

            active = self._trace.get()
            lease_context = self._trace.set(RequestTrace(
                session_id=active.session_id,
                tool_call_id=active.tool_call_id,
                operation=active.operation,
                lease_token=lease["token"],
            ))
            try:
                yield lease
            finally:
                try:
                    async with self._client(timeout=10) as client:
                        response = await client.post(
                            "/desktop/lease/release",
                            json={"token": lease["token"]},
                        )
                        response.raise_for_status()
                except Exception as exc:
                    # The server-side TTL releases a lease after a crashed or
                    # disconnected backend. Do not mask the actual tool result.
                    log.warning(f"Failed to release desktop lease: {exc}")
                finally:
                    self._trace.reset(lease_context)

    def _client(self, timeout: float = 30.0) -> httpx.AsyncClient:
        """Create an httpx async client.

        trust_env=False is deliberate: the sandbox endpoint is always directly
        reachable infrastructure (local container, in-cluster service, or an
        SSH tunnel on loopback). Honouring HTTP_PROXY/HTTPS_PROXY from the
        environment sends those requests through a developer's proxy, which
        typically cannot reach them and fails with an opaque timeout.
        """
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers=self._headers,
            timeout=timeout,
            trust_env=False,
            event_hooks={"request": [self._merge_request_headers]},
        )

    async def _load_action_server_capabilities(self) -> frozenset[str]:
        """Load a successful /alive capability set once for protocol gating."""
        if self._action_server_capabilities is not None:
            return self._action_server_capabilities
        probe_token = self._capability_probe.set(True)
        try:
            async with self._client(timeout=5.0) as client:
                response = await client.get("/alive")
                response.raise_for_status()
                capabilities = response.json().get("capabilities", [])
            if not isinstance(capabilities, list) or not all(
                isinstance(item, str) for item in capabilities
            ):
                raise RuntimeError("Action Server returned invalid capabilities")
        except Exception:
            # A transport outage is retryable; do not permanently cache it as a
            # protocol downgrade. The caller still fails before a side effect.
            raise
        finally:
            self._capability_probe.reset(probe_token)
        self._action_server_capabilities = frozenset(capabilities)
        return self._action_server_capabilities

    async def _require_tenant_scope_support(self) -> None:
        """Fail closed before a scoped client talks to an older global server."""
        if not self.user_scope or self._tenant_scope_capable is True:
            return
        if self._tenant_scope_capable is False:
            raise RuntimeError("Action Server does not support tenant-scoped catalogues")
        capabilities = await self._load_action_server_capabilities()
        supported = "tenant_catalogue_scopes_v1" in capabilities
        self._tenant_scope_capable = supported
        if not supported:
            raise RuntimeError("Action Server does not support tenant-scoped catalogues")

    async def _require_skill_archive_create_only_support(self) -> None:
        """Never send create-only recovery to a server that could ignore it."""
        capabilities = await self._load_action_server_capabilities()
        if "skill_archive_create_only_v1" not in capabilities:
            raise RuntimeError(
                "Action Server does not support create-only Skill archive uploads"
            )

    async def _require_skill_restore_fence_support(self) -> None:
        """Require the execution-plane half of durable uninstall ordering."""
        capabilities = await self._load_action_server_capabilities()
        if "skill_restore_fence_v1" not in capabilities:
            raise RuntimeError(
                "Action Server does not support durable Skill restore fencing"
            )

    async def _require_filesystem_capability(
        self,
        capability: str,
        operation: str,
    ) -> None:
        """Fail closed when an older server could ignore a safety contract."""
        capabilities = await self._load_action_server_capabilities()
        if capability not in capabilities:
            raise RuntimeError(
                f"Action Server does not support safe {operation}"
            )

    async def execute(
        self,
        command: str,
        timeout: int = 120,
        workdir: str = "/workspace",
    ) -> ExecuteResult:
        """Execute a command in the sandbox."""
        async with self._client(timeout=timeout + 10) as client:
            resp = await client.post("/execute", json={
                "command": command,
                "timeout": timeout,
                "workdir": workdir,
            })
            resp.raise_for_status()
            data = resp.json()
            return ExecuteResult(
                exit_code=data["exit_code"],
                stdout=data["stdout"],
                stderr=data["stderr"],
            )

    async def submit_media_job(self, payload: dict) -> dict:
        """Idempotently enqueue a render on this sandbox's durable media queue."""
        async with self._client(timeout=30.0) as client:
            response = await client.post(
                "/media/jobs",
                json=payload,
            )
            response.raise_for_status()
            return response.json()

    async def get_media_job(self, job_id: str, owner: str) -> dict:
        async with self._client(timeout=15.0) as client:
            response = await client.get(
                f"/media/jobs/{job_id}",
                params={"owner": owner},
            )
            response.raise_for_status()
            return response.json()

    async def wait_media_job(
        self,
        job_id: str,
        owner: str,
        *,
        after_version: int = 0,
        timeout: float = 25.0,
    ) -> dict:
        """Long-poll one render without tying up an unbounded backend request."""
        bounded = max(0.0, min(25.0, float(timeout)))
        async with self._client(timeout=bounded + 15.0) as client:
            response = await client.get(
                f"/media/jobs/{job_id}/wait",
                params={
                    "owner": owner,
                    "after_version": max(0, int(after_version)),
                    "timeout": bounded,
                },
            )
            response.raise_for_status()
            return response.json()

    async def cancel_media_job(self, job_id: str, owner: str) -> dict:
        async with self._client(timeout=15.0) as client:
            response = await client.post(
                f"/media/jobs/{job_id}/cancel",
                json={"owner": owner},
            )
            response.raise_for_status()
            return response.json()

    async def retry_media_job(
        self, job_id: str, owner: str, replacement_payload: dict | None = None
    ) -> dict:
        body: dict = {"owner": owner}
        if replacement_payload is not None:
            body["payload"] = replacement_payload
        async with self._client(timeout=15.0) as client:
            response = await client.post(
                f"/media/jobs/{job_id}/retry",
                json=body,
            )
            response.raise_for_status()
            return response.json()

    async def media_queue_status(self) -> dict:
        async with self._client(timeout=15.0) as client:
            response = await client.get(
                "/media/jobs/status",
            )
            response.raise_for_status()
            return response.json()

    async def execute_stream(
        self,
        command: str,
        timeout: int = 120,
        idle_timeout: int = 60,
        workdir: str = "/workspace",
    ) -> AsyncIterator[OutputChunk | IdleNotification | int]:
        """Execute a command with streaming output.

        Yields OutputChunk for each line of output,
        IdleNotification when no output for idle_timeout seconds,
        and finally the exit code (int) at the end.
        """
        pid = 0
        async with self._client(timeout=timeout + 10) as client:
            async with client.stream("POST", "/execute_stream", json={
                "command": command,
                "timeout": timeout,
                "idle_timeout": idle_timeout,
                "workdir": workdir,
            }) as resp:
                resp.raise_for_status()
                buffer = ""
                async for chunk in resp.aiter_text():
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        if line.startswith("data: "):
                            data_str = line[6:]
                            try:
                                data = json.loads(data_str)
                            except json.JSONDecodeError:
                                continue

                            if "type" in data and "content" in data:
                                yield OutputChunk(
                                    type=data["type"],
                                    content=data["content"],
                                )
                            elif "exit_code" in data:
                                yield data["exit_code"]
                            elif "pid" in data and "idle_seconds" not in data:
                                pid = data["pid"]
                            elif "idle_seconds" in data:
                                yield IdleNotification(
                                    idle_seconds=data["idle_seconds"],
                                    total_seconds=data["total_seconds"],
                                    pid=pid,
                                )

    async def read_file(
        self,
        path: str,
        offset: int = 0,
        limit: int = 2000,
    ) -> str:
        """Read a file from the sandbox with line numbers."""
        async with self._client() as client:
            resp = await client.post("/read_file", json={
                "path": path,
                "offset": offset,
                "limit": limit,
            })
            resp.raise_for_status()
            data = resp.json()
            return data["content"]

    async def write_file(self, path: str, content: str) -> None:
        """Write content to a file in the sandbox."""
        async with self._client() as client:
            resp = await client.post("/write_file", json={
                "path": path,
                "content": content,
            })
            resp.raise_for_status()

    async def download_file_bytes(
        self,
        path: str,
        *,
        max_bytes: int = 8 * 1024 * 1024,
    ) -> bytes:
        """Download one workspace file through a strict wire-size budget."""
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        async with self._client(timeout=30.0) as client:
            async with client.stream("GET", "/download", params={"path": path}) as resp:
                resp.raise_for_status()
                declared = resp.headers.get("content-length")
                if declared:
                    try:
                        declared_size = int(declared)
                    except ValueError as exc:
                        raise RuntimeError("Sandbox returned an invalid file size") from exc
                    if declared_size < 0 or declared_size > max_bytes:
                        raise RuntimeError("Sandbox file exceeds the download limit")
                content = bytearray()
                if resp.is_stream_consumed:
                    content.extend(resp.content)
                else:
                    async for chunk in resp.aiter_raw():
                        content.extend(chunk)
                        if len(content) > max_bytes:
                            raise RuntimeError("Sandbox file exceeds the download limit")
                if len(content) > max_bytes:
                    raise RuntimeError("Sandbox file exceeds the download limit")
                return bytes(content)

    async def delete_file(self, path: str) -> None:
        """Delete one workspace file through the server's confined path API."""
        await self._require_filesystem_capability(
            "confined_file_delete_v1", "workspace file deletion"
        )
        async with self._client() as client:
            resp = await client.post("/delete_file", json={"path": path})
            resp.raise_for_status()

    async def resolve_paths(
        self,
        targets: list[PathResolveTarget],
    ) -> list[ResolvedPath]:
        """Resolve static filesystem targets before permission evaluation."""
        if not targets:
            return []
        await self._require_filesystem_capability(
            "confined_path_resolve_v1", "canonical path resolution"
        )
        async with self._client() as client:
            resp = await client.post("/resolve_paths", json={
                "targets": [
                    {
                        "path": target.path,
                        "allow_missing": target.allow_missing,
                        "allow_scoped_skills": target.allow_scoped_skills,
                    }
                    for target in targets
                ],
            })
            resp.raise_for_status()
            data = resp.json()
        raw_targets = data.get("targets")
        if not isinstance(raw_targets, list) or len(raw_targets) != len(targets):
            raise RuntimeError("Action Server returned invalid resolved paths")
        resolved: list[ResolvedPath] = []
        for item in raw_targets:
            if not isinstance(item, dict):
                raise RuntimeError("Action Server returned invalid resolved paths")
            canonical = item.get("canonical_path")
            relative = item.get("workspace_relative")
            if not isinstance(canonical, str) or not canonical.startswith("/"):
                raise RuntimeError("Action Server returned invalid resolved paths")
            if relative is not None and not isinstance(relative, str):
                raise RuntimeError("Action Server returned invalid resolved paths")
            resolved.append(
                ResolvedPath(
                    canonical_path=canonical,
                    workspace_relative=relative,
                )
            )
        return resolved

    async def glob(
        self,
        pattern: str,
        path: str = "/workspace",
        *,
        include_sensitive: bool = False,
    ) -> list[str]:
        """Find files matching a glob pattern in the sandbox."""
        await self._require_filesystem_capability(
            "sensitive_search_filter_v1", "filesystem search filtering"
        )
        async with self._client() as client:
            resp = await client.post("/glob", json={
                "pattern": pattern,
                "path": path,
                "include_sensitive": include_sensitive,
            })
            resp.raise_for_status()
            data = resp.json()
            return data["files"]

    async def grep(
        self,
        pattern: str,
        path: str = "/workspace",
        file_type: str | None = None,
        max_results: int = 100,
        *,
        include_sensitive: bool = False,
    ) -> str:
        """Search file contents in the sandbox."""
        await self._require_filesystem_capability(
            "sensitive_search_filter_v1", "filesystem search filtering"
        )
        async with self._client() as client:
            resp = await client.post("/grep", json={
                "pattern": pattern,
                "path": path,
                "type": file_type,
                "max_results": max_results,
                "include_sensitive": include_sensitive,
            })
            resp.raise_for_status()
            data = resp.json()
            return data["output"]

    async def list_files(self, path: str = "/workspace") -> list[dict]:
        """List directory contents in the sandbox."""
        async with self._client() as client:
            resp = await client.post("/list_files", json={"path": path})
            resp.raise_for_status()
            data = resp.json()
            return data["entries"]

    async def read_file_raw(self, path: str) -> str:
        """Read raw file content from the sandbox (no line numbers)."""
        result = await self.execute(f"cat {shlex.quote(path)}", timeout=10)
        if result.exit_code != 0:
            raise FileNotFoundError(f"File not found: {path}")
        return result.stdout

    async def kill_command(self, pid: int) -> None:
        """Kill a running command by PID via the action server."""
        try:
            async with self._client(timeout=5) as client:
                resp = await client.post("/kill", json={"pid": pid})
                resp.raise_for_status()
        except Exception as e:
            log.warning(f"Failed to kill process {pid}: {e}")

    async def alive(self) -> bool:
        """Check if the sandbox is alive."""
        try:
            async with self._client(timeout=5) as client:
                resp = await client.get("/alive")
                return resp.status_code == 200
        except Exception:
            return False

    # ---- Generic HTTP helpers ----

    async def _get(self, path: str, timeout: float = 15.0):
        """Generic GET request to action server."""
        if path.startswith(("/skills", "/mcp/", "/catalog")):
            await self._require_tenant_scope_support()
        async with self._client(timeout=timeout) as client:
            resp = await client.get(path)
            resp.raise_for_status()
            return resp.json()

    async def _post(self, path: str, timeout: float = 30.0, **kwargs):
        """Generic POST request to action server."""
        if path.startswith(("/skills", "/mcp/", "/catalog")):
            await self._require_tenant_scope_support()
        async with self._client(timeout=timeout) as client:
            resp = await client.post(path, **kwargs)
            resp.raise_for_status()
            return resp.json()

    async def _delete(self, path: str, timeout: float = 15.0):
        """Generic DELETE request to action server."""
        if path.startswith(("/skills", "/mcp/", "/catalog")):
            await self._require_tenant_scope_support()
        async with self._client(timeout=timeout) as client:
            resp = await client.request("DELETE", path)
            resp.raise_for_status()
            return resp.json()

    # ---- Sandbox catalogue projection ----

    def _invalidate_catalogue_cache(self) -> None:
        """Force revalidation without discarding the last-known-good view."""
        self._catalogue_epoch += 1
        if self._catalogue_cache is not None:
            self._catalogue_cache = _CatalogueCacheEntry(
                snapshot=self._catalogue_cache.snapshot,
                etag=self._catalogue_cache.etag,
                expires_at=float("-inf"),
            )
        # Do not cancel a request another caller is awaiting. Detaching it
        # lets the next caller load the post-mutation generation immediately;
        # the epoch guard prevents the old task from repopulating the cache.
        self._catalogue_inflight = None
        registry = self._openbox_skill_registry
        if registry is not None:
            # Mutation invalidation is the primary freshness signal. Provider
            # TTLs remain only a bounded fallback for changes made elsewhere.
            try:
                registry.invalidate("wuying-scoped")
                registry.invalidate("personal-user-library")
            except Exception as exc:
                log.debug(
                    "Could not invalidate Skill provider cache error_type=%s",
                    type(exc).__name__,
                )

    async def dispose_skill_registry(self) -> None:
        """Dispose this client's scoped Skill providers and in-flight reads."""
        registry = self._openbox_skill_registry
        self._openbox_skill_registry = None
        if registry is not None:
            await registry.dispose()

    @staticmethod
    def _resource_metadata(resources) -> list[dict]:
        """Whitelist resource catalogue fields; bodies never cross this path."""
        projected = []
        for raw in resources if isinstance(resources, list) else []:
            if not isinstance(raw, dict):
                continue
            projected.append({
                "server": str(raw.get("server") or ""),
                "uri": str(raw.get("uri") or ""),
                "name": str(raw.get("name") or ""),
                "description": str(raw.get("description") or ""),
                "mimeType": str(raw.get("mimeType") or raw.get("mime_type") or ""),
            })
        return projected

    @classmethod
    def _normalize_catalogue_snapshot(cls, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("sandbox catalogue projection must be an object")
        skills = payload.get("skills")
        tools = payload.get("mcp_tools")
        resources = payload.get("mcp_resources")
        servers = payload.get("mcp_servers", [])
        if not isinstance(skills, list) or not isinstance(tools, list):
            raise ValueError("sandbox catalogue projection is missing directory lists")
        if not isinstance(resources, list) or not isinstance(servers, list):
            raise ValueError("sandbox catalogue projection has malformed MCP metadata")
        snapshot = copy.deepcopy(payload)
        snapshot["skills"] = copy.deepcopy(skills)
        snapshot["mcp_tools"] = copy.deepcopy(tools)
        snapshot["mcp_resources"] = cls._resource_metadata(resources)
        snapshot["mcp_servers"] = copy.deepcopy(servers)
        return snapshot

    async def _legacy_catalogue_projection(self, client: httpx.AsyncClient) -> dict:
        """Build an equivalent snapshot from an older Action Server."""
        responses = []
        for path in ("/skills", "/mcp/tools", "/mcp/resources"):
            response = await client.get(path)
            response.raise_for_status()
            responses.append(response.json())
        skills, tools, resources = responses
        if not isinstance(skills, list) or not isinstance(tools, list):
            raise ValueError("legacy sandbox catalogue returned malformed lists")
        resource_metadata = self._resource_metadata(resources)
        return {
            "catalogue_version": 0,
            "boot_id": "",
            "started_at": None,
            # Do not invent an authoritative generation for old servers.
            "skills_generation": "",
            "mcp_generation": "",
            "generation": "",
            "counts": {
                "skills": len(skills),
                "mcp_servers": 0,
                "mcp_tools": len(tools),
                "mcp_resources": len(resource_metadata),
            },
            "skills": copy.deepcopy(skills),
            "mcp_servers": [],
            "mcp_tools": copy.deepcopy(tools),
            "mcp_resources": resource_metadata,
        }

    async def _reload_catalogue_projection(self) -> _CatalogueLoad:
        await self._require_tenant_scope_support()
        load_epoch = self._catalogue_epoch
        previous = self._catalogue_cache
        availability: Literal["available", "stale"] = "available"
        try:
            async with self._client(timeout=15.0) as client:
                headers = {}
                if previous is not None and previous.etag:
                    headers["If-None-Match"] = previous.etag
                response = await client.get("/catalog", headers=headers or None)

                if response.status_code == 304:
                    if previous is None or not previous.etag:
                        raise RuntimeError("sandbox returned 304 without a cached catalogue")
                    entry = _CatalogueCacheEntry(
                        snapshot=previous.snapshot,
                        etag=previous.etag,
                        expires_at=(
                            self._catalogue_clock() + self._catalogue_ttl_seconds
                        ),
                    )
                elif response.status_code in {404, 405}:
                    legacy = await self._legacy_catalogue_projection(client)
                    # Three legacy endpoints cannot provide one atomic
                    # generation. The snapshot is usable but never destructive
                    # validation authority.
                    availability = "stale"
                    entry = _CatalogueCacheEntry(
                        snapshot=legacy,
                        etag=None,
                        expires_at=(
                            self._catalogue_clock() + self._catalogue_ttl_seconds
                        ),
                    )
                else:
                    response.raise_for_status()
                    etag = response.headers.get("etag")
                    if not etag:
                        # An endpoint without ETag cannot provide the projection
                        # protocol's consistency guarantee. Treat it as legacy.
                        legacy = await self._legacy_catalogue_projection(client)
                        availability = "stale"
                        entry = _CatalogueCacheEntry(
                            snapshot=legacy,
                            etag=None,
                            expires_at=(
                                self._catalogue_clock() + self._catalogue_ttl_seconds
                            ),
                        )
                    else:
                        snapshot = self._normalize_catalogue_snapshot(response.json())
                        entry = _CatalogueCacheEntry(
                            snapshot=snapshot,
                            etag=etag,
                            expires_at=(
                                self._catalogue_clock() + self._catalogue_ttl_seconds
                            ),
                        )
        except Exception:
            # A tunnel outage retains the last known good projection, but its
            # expired deadline is not extended. The very next request retries,
            # so failures never become a long negative cache.
            if previous is not None:
                return _CatalogueLoad("stale", previous.snapshot)
            raise

        if self._catalogue_epoch == load_epoch:
            self._catalogue_cache = entry
            return _CatalogueLoad(availability, entry.snapshot)
        # A local mutation raced this read. Its response is still a bounded,
        # last-known snapshot, but it cannot authoritatively describe the
        # post-mutation directory and must not drive destructive validation.
        return _CatalogueLoad("stale", entry.snapshot)

    async def _load_catalogue_projection(self) -> _CatalogueLoad:
        current = self._catalogue_cache
        if current is not None and self._catalogue_clock() < current.expires_at:
            # A TTL hit is last-known-good, not proof that another worker has
            # not already observed a newer remote generation.
            return _CatalogueLoad("stale", current.snapshot)

        task = self._catalogue_inflight
        if task is None:
            task = asyncio.create_task(self._reload_catalogue_projection())
            self._catalogue_inflight = task
        try:
            return await asyncio.shield(task)
        finally:
            if self._catalogue_inflight is task and task.done():
                self._catalogue_inflight = None

    async def get_catalogue_projection_state(self) -> CatalogueProjectionState:
        """Return a copy-on-read snapshot with explicit outage semantics.

        A cold failure is represented as ``unavailable`` and is never cached.
        This status-oriented API is used by tool resolution so a transient
        empty view cannot destructively invalidate persisted reveal evidence.
        The compatibility ``get_catalogue_projection`` API below continues to
        raise the original transport error on a cold failure.
        """
        try:
            loaded = await self._load_catalogue_projection()
        except Exception as exc:
            log.debug(
                "Sandbox catalogue unavailable error_type=%s",
                type(exc).__name__,
            )
            return CatalogueProjectionState("unavailable", None)
        return CatalogueProjectionState(
            loaded.availability,
            copy.deepcopy(loaded.snapshot),
        )

    async def get_catalogue_projection(self) -> dict:
        """Return one copy-on-read Skill/MCP directory snapshot.

        All three legacy list methods below use this function. Within TTL they
        perform no tunnel request; after TTL, concurrent callers share one
        conditional request and a 304 carries no directory bytes.
        """
        loaded = await self._load_catalogue_projection()
        return copy.deepcopy(loaded.snapshot)

    async def get_catalogue_version(self) -> dict:
        """Return version fields from the shared projection without another GET."""
        snapshot = await self.get_catalogue_projection()
        return {
            key: copy.deepcopy(snapshot.get(key))
            for key in (
                "catalogue_version", "boot_id", "started_at",
                "skills_generation", "mcp_generation", "generation", "counts",
            )
        }

    # ---- Dev-browser management ----

    async def start_dev_browser(self) -> dict:
        """Start the dev-browser relay server inside the container."""
        return await self._post("/dev-browser/start", timeout=15.0)

    async def stop_dev_browser(self) -> dict:
        """Stop the dev-browser relay server."""
        return await self._post("/dev-browser/stop")

    async def get_dev_browser_status(self) -> dict:
        """Get dev-browser relay status and extension connection state."""
        return await self._get("/dev-browser/status")

    # ---- Skill management ----

    async def list_skills(self) -> list[dict]:
        """List all installed skills in the container."""
        return (await self.get_catalogue_projection())["skills"]

    async def get_skill(self, name: str) -> dict:
        """Get a specific skill by name."""
        return await self._get(f"/skills/{name}")

    async def create_skill(
        self,
        name: str,
        skill_md: str,
        files: list[dict[str, str]] | None = None,
    ) -> dict:
        """Atomically create a validated user skill package.

        New action servers own the strict validation endpoint. Long-lived
        WUYING desktops are upgraded independently, so a 404/405 falls back to
        their existing generic file API while preserving new-only semantics.
        """
        payload_files = files or []
        try:
            created = await self._post(
                "/skills/create",
                timeout=30.0,
                json={"name": name, "skill_md": skill_md, "files": payload_files},
            )
            self._invalidate_catalogue_cache()
            return created
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {404, 405}:
                raise
            if self.user_scope:
                raise RuntimeError(
                    "Scoped skill creation requires the current Action Server"
                ) from exc

        if len(name) > 64 or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
            raise ValueError("invalid skill slug")
        validated: list[tuple[str, str]] = []
        seen: set[str] = set()
        for item in payload_files:
            path = item.get("path", "") if isinstance(item, dict) else ""
            content = item.get("content", "") if isinstance(item, dict) else ""
            parts = path.split("/")
            if (
                not path
                or path != path.strip()
                or path.startswith("/")
                or "\\" in path
                or any(part in {"", ".", ".."} or part.startswith(".") for part in parts)
                or any(ord(char) < 32 or ord(char) == 127 for char in path)
                or len(path.encode("utf-8")) > 240
            ):
                raise ValueError(f"unsafe skill file path: {path!r}")
            lowered = parts[-1].casefold()
            if lowered in {"skill.md", "install.sh"} or path in seen:
                raise ValueError(f"reserved or duplicate skill file path: {path!r}")
            if not isinstance(content, str) or len(content.encode("utf-8")) > 512 * 1024:
                raise ValueError(f"invalid skill file content: {path!r}")
            seen.add(path)
            validated.append((path, content))

        token = secrets.token_hex(6)
        target = f"/data/skills/{name}"
        staging = f"/data/skills/.{name}.{token}.incoming"
        prepare = await self.execute(
            f"umask 077; test ! -e {shlex.quote(target)} && mkdir -p {shlex.quote(staging)}",
            timeout=10,
            workdir="/data/skills",
        )
        if prepare.exit_code != 0:
            raise FileExistsError(f"Skill '{name}' already exists")

        try:
            await self.write_file(f"{staging}/SKILL.md", skill_md)
            for relative, content in validated:
                await self.write_file(f"{staging}/{relative}", content)
            publish = await self.execute(
                f"test ! -e {shlex.quote(target)} && mv -- {shlex.quote(staging)} {shlex.quote(target)}",
                timeout=10,
                workdir="/data/skills",
            )
            if publish.exit_code != 0:
                raise FileExistsError(f"Skill '{name}' already exists")
        except Exception:
            await self.execute(
                f"rm -rf -- {shlex.quote(staging)}",
                timeout=10,
                workdir="/data/skills",
            )
            raise

        created = await self.get_skill(name)
        self._invalidate_catalogue_cache()
        return {**created, "created": True}

    async def _legacy_export_skill_archive(self, name: str) -> dict:
        if self.user_scope:
            raise RuntimeError("Legacy global skill export is disabled for scoped clients")
        encoded_script = base64.b64encode(
            _LEGACY_SKILL_EXPORT_SCRIPT.encode("utf-8")
        ).decode("ascii")
        launcher = f"import base64;exec(base64.b64decode({encoded_script!r}))"
        result = await self.execute(
            f"python3 -c {shlex.quote(launcher)} {shlex.quote(name)}",
            timeout=60,
            workdir="/workspace",
        )
        if result.exit_code != 0:
            raise RuntimeError((result.stderr or result.stdout or "Skill export failed")[:500])
        try:
            return json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise RuntimeError("WUYING skill export returned an invalid response") from exc

    @staticmethod
    async def _validated_skill_archive_response(resp: httpx.Response) -> bytes:
        """Read one identity-encoded ZIP response through the wire-size budget."""
        encoding = resp.headers.get("content-encoding", "identity").strip().casefold()
        if encoding not in {"", "identity"}:
            raise SkillArchiveValidationError(
                "http_encoding",
                "Compressed HTTP encoding is not allowed for Skill ZIPs",
            )
        declared = resp.headers.get("content-length")
        if declared:
            try:
                declared_size = int(declared)
            except ValueError as exc:
                raise SkillArchiveValidationError(
                    "invalid_size",
                    "Skill ZIP response has an invalid Content-Length",
                ) from exc
            if declared_size < 0 or declared_size > SKILL_ARCHIVE_MAX_COMPRESSED_BYTES:
                raise SkillArchiveValidationError(
                    "compressed_too_large",
                    "Skill ZIP exceeds the compressed size limit",
                )

        archive = bytearray()
        if resp.is_stream_consumed:
            archive.extend(resp.content)
        else:
            async for chunk in resp.aiter_raw():
                archive.extend(chunk)
                if len(archive) > SKILL_ARCHIVE_MAX_COMPRESSED_BYTES:
                    raise SkillArchiveValidationError(
                        "compressed_too_large",
                        "Skill ZIP exceeds the compressed size limit",
                    )
        if len(archive) > SKILL_ARCHIVE_MAX_COMPRESSED_BYTES:
            raise SkillArchiveValidationError(
                "compressed_too_large",
                "Skill ZIP exceeds the compressed size limit",
            )
        result = bytes(archive)
        await asyncio.to_thread(validate_skill_zip, result)
        return result

    async def download_skill_archive(self, name: str) -> bytes:
        """Download a user skill as a ZIP archive."""
        await self._require_tenant_scope_support()
        encoded_name = quote(name, safe="")
        try:
            async with self._client(timeout=60.0) as client:
                async with client.stream(
                    "GET",
                    f"/skills/{encoded_name}/archive",
                ) as resp:
                    resp.raise_for_status()
                    return await self._validated_skill_archive_response(resp)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {404, 405}:
                raise

        exported = await self._legacy_export_skill_archive(name)
        async with self._client(timeout=60.0) as client:
            async with client.stream(
                "GET",
                "/download",
                params={"path": exported["path"]},
            ) as resp:
                resp.raise_for_status()
                return await self._validated_skill_archive_response(resp)

    async def export_skill_archive(self, name: str) -> dict:
        """Export a user skill ZIP into the sandbox workspace."""
        await self._require_tenant_scope_support()
        encoded_name = quote(name, safe="")
        try:
            return await self._post(f"/skills/{encoded_name}/export", timeout=60.0)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in {404, 405}:
                raise
        return await self._legacy_export_skill_archive(name)

    async def install_skill(
        self,
        url: str | None = None,
        name: str | None = None,
        content: str | None = None,
    ) -> dict:
        """Install a skill in the container."""
        installed = await self._post(
            "/skills/install",
            timeout=90.0,
            json={"url": url, "name": name, "content": content},
        )
        self._invalidate_catalogue_cache()
        return installed

    async def upload_skill_archive(
        self,
        file_bytes: bytes,
        filename: str,
        name: str = "",
        *,
        create_only: bool = False,
        restore_generation: int | None = None,
    ) -> dict:
        """Upload a Skill archive, optionally with atomic create-only semantics."""
        await self._require_tenant_scope_support()
        # Durable personal snapshots are ZIPs. Validate them before any bytes
        # cross the trust boundary; the Action Server repeats the same checks
        # immediately before extraction so neither a corrupt database row nor
        # a bypassing API client can consume unbounded execution-plane space.
        if filename.casefold().endswith(".zip"):
            await asyncio.to_thread(validate_skill_zip, file_bytes)
        if restore_generation is not None:
            if not create_only or restore_generation < 1:
                raise ValueError(
                    "restore_generation requires create_only and must be positive"
                )
            await self._require_skill_restore_fence_support()
        if create_only:
            await self._require_skill_archive_create_only_support()
        files = {"file": (filename, file_bytes)}
        data = {
            "name": name or "",
            "create_only": "true" if create_only else "false",
        }
        if restore_generation is not None:
            data["restore_generation"] = str(restore_generation)
        async with self._client(timeout=90.0) as client:
            resp = await client.post(
                "/skills/upload",
                files=files,
                data=data,
            )
            if resp.status_code != 200:
                detail = (
                    resp.json().get("detail", resp.text)
                    if resp.headers.get("content-type", "").startswith("application/json")
                    else resp.text
                )
                if (
                    create_only
                    and resp.status_code == 409
                    and isinstance(detail, dict)
                    and detail.get("code") == "skill_already_exists"
                ):
                    conflict_name = detail.get("name")
                    message = detail.get("message")
                    # The conflicting package may have been published by a
                    # different backend process after our cached negative.
                    # Force the caller's convergence read past that stale view.
                    self._invalidate_catalogue_cache()
                    raise SkillArchiveAlreadyExistsError(
                        conflict_name if isinstance(conflict_name, str) else name,
                        message if isinstance(message, str) else None,
                    )
                if (
                    create_only
                    and resp.status_code == 409
                    and isinstance(detail, dict)
                    and detail.get("code") == "skill_restore_fenced"
                ):
                    conflict_name = detail.get("name")
                    fenced_through = detail.get("fenced_through_generation")
                    self._invalidate_catalogue_cache()
                    raise SkillRestoreFencedError(
                        conflict_name if isinstance(conflict_name, str) else name,
                        (
                            fenced_through
                            if isinstance(fenced_through, int)
                            else restore_generation or 1
                        ),
                    )
                raise Exception(detail)
            installed = resp.json()
        self._invalidate_catalogue_cache()
        return installed

    async def uninstall_skill(
        self,
        name: str,
        *,
        mutation_generation: int | None = None,
    ) -> dict:
        """Uninstall a skill from the container."""
        if mutation_generation is not None:
            if mutation_generation < 1:
                raise ValueError("mutation_generation must be positive")
            await self._require_skill_restore_fence_support()
        encoded_name = quote(name, safe="")
        async with self._client(timeout=15.0) as client:
            removed_response = await client.request(
                "DELETE",
                f"/skills/{encoded_name}",
                params=(
                    {"mutation_generation": mutation_generation}
                    if mutation_generation is not None
                    else None
                ),
            )
            removed_response.raise_for_status()
            removed = removed_response.json()
        self._invalidate_catalogue_cache()
        return removed

    # ---- MCP server management ----

    async def list_mcp_servers(self) -> list[dict]:
        """List all MCP servers configured in the container."""
        return await self._get("/mcp/servers")

    async def add_mcp_server(self, name: str, config: dict) -> dict:
        """Add a new MCP server configuration."""
        added = await self._post("/mcp/servers", json={"name": name, **config})
        self._invalidate_catalogue_cache()
        return added

    async def remove_mcp_server(self, name: str) -> dict:
        """Remove an MCP server configuration."""
        removed = await self._delete(f"/mcp/servers/{name}")
        self._invalidate_catalogue_cache()
        return removed

    async def connect_mcp(self, name: str) -> dict:
        """Connect to an MCP server."""
        try:
            return await self._post(f"/mcp/servers/{name}/connect")
        finally:
            self._invalidate_catalogue_cache()

    async def disconnect_mcp(self, name: str) -> dict:
        """Disconnect from an MCP server."""
        try:
            return await self._post(f"/mcp/servers/{name}/disconnect")
        finally:
            self._invalidate_catalogue_cache()

    async def list_mcp_tools(self) -> list[dict]:
        """List all tools from connected MCP servers."""
        return (await self.get_catalogue_projection())["mcp_tools"]

    async def call_mcp_tool(self, server: str, tool: str, arguments: dict) -> dict:
        """Call a tool on a connected MCP server.

        The budget here has to exceed the container's own per-server timeout,
        which defaults to 60s. When both were 60s the two expired together, so
        the container's explanation never made it back — the caller got a bare
        httpx.ReadTimeout, which stringifies to nothing.

        The container is the layer that knows which server it is talking to and
        what that server's limit is, so it should be the one to give up first.
        This is only a backstop against the container itself hanging. A remote
        call also costs two round trips under the per-operation session model
        (initialize, then the call), so 60s was not even the real ceiling.
        """
        return await self._post(
            f"/mcp/tools/{server}/{tool}",
            timeout=MCP_CALL_TIMEOUT_SECONDS,
            json={"arguments": arguments},
        )

    async def list_mcp_resources(self) -> list[dict]:
        """List all resources from connected MCP servers."""
        return (await self.get_catalogue_projection())["mcp_resources"]

    async def read_mcp_resource(self, server: str, uri: str) -> dict:
        """Read a specific MCP resource."""
        return await self._post("/mcp/resources/read", timeout=30.0, json={"server": server, "uri": uri})

    async def list_mcp_prompts(self) -> list[dict]:
        """List all prompts from connected MCP servers."""
        return await self._get("/mcp/prompts")

    async def get_mcp_prompt(self, server: str, name: str, arguments: dict | None = None) -> dict:
        """Get a specific MCP prompt."""
        return await self._post("/mcp/prompts/get", timeout=30.0, json={"server": server, "name": name, "arguments": arguments})

    async def refresh_mcp_server(self, name: str) -> dict:
        """Refresh tools/resources/prompts for a connected MCP server."""
        try:
            return await self._post(f"/mcp/servers/{name}/refresh", timeout=30.0)
        finally:
            self._invalidate_catalogue_cache()
