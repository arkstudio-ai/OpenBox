"""Actor execution inside the existing Wuying guest and Action Server.

Bindings are pre-enrolled in a root-owned file. Requests never allocate an
account, change a desktop assignment, or fall back to a shared execution uid.
The launcher gives each child a mount namespace before permanently dropping
to its actor uid; existing /workspace and /data tool paths keep their meaning.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys


PROTOCOL = "wuying_actor_uid_mount_v1"
CONFIG_ENV = "OPENBOX_PRIVATE_ACTOR_CONFIG"
SCOPE_HEADER = "x-openbox-private-scope"
ATTEMPT_HEADER = "x-openbox-private-attempt"
_HEX = re.compile(r"[0-9a-f]{64}")
_ID = re.compile(r"[a-zA-Z0-9_-]{8,96}")
_legacy_file_worker = False


class PrivateActorError(RuntimeError):
    pass


def configure(*, legacy_file_worker):
    """Called by the original AS only after mounting its unprivileged worker."""
    global _legacy_file_worker
    _legacy_file_worker = legacy_file_worker is True


def scope_id(workspace_id, actor_user_id):
    if not all(isinstance(value, str) and value and "\0" not in value
               for value in (workspace_id, actor_user_id)):
        raise PrivateActorError("Invalid actor scope")
    return hashlib.sha256(("openbox:private-wuying:v1\0" + workspace_id + "\0" + actor_user_id).encode()).hexdigest()


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


@contextmanager
def _directory(path, *, uid, mode=None):
    """Root controls every ancestor; the final directory may belong to an actor."""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise PrivateActorError("Invalid actor directory")
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for index, component in enumerate(path.parts[1:]):
            parent = os.fstat(descriptor)
            if parent.st_uid != 0 or parent.st_mode & 0o022:
                raise PrivateActorError("Actor directory has an unprotected ancestor")
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
            current = os.fstat(descriptor)
            final = index == len(path.parts) - 2
            if current.st_uid != (uid if final else 0):
                raise PrivateActorError("Actor directory owner changed")
            if final and mode is not None and stat.S_IMODE(current.st_mode) != mode:
                raise PrivateActorError("Actor directory permissions changed")
            if not final and current.st_mode & 0o022:
                raise PrivateActorError("Actor directory is replaceable")
        yield descriptor
    except OSError as exc:
        raise PrivateActorError("Actor directory is unavailable") from exc
    finally:
        os.close(descriptor)


def _read_config(path):
    path = Path(path)
    with _directory(path.parent, uid=0) as parent:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as source:
            entry = os.fstat(source.fileno())
            if not stat.S_ISREG(entry.st_mode) or entry.st_uid != 0 or stat.S_IMODE(entry.st_mode) != 0o600:
                raise PrivateActorError("Actor registry is not root protected")
            body = source.read(128 * 1024 + 1)
            if len(body) > 128 * 1024:
                raise PrivateActorError("Actor registry exceeds its limit")
    try:
        result = json.loads(body)
    except (ValueError, UnicodeError) as exc:
        raise PrivateActorError("Actor registry is invalid") from exc
    if (not isinstance(result, dict) or set(result) != {"version", "bindings"}
            or type(result["version"]) is not int or result["version"] != 1
            or not isinstance(result["bindings"], list) or len(result["bindings"]) > 128):
        raise PrivateActorError("Actor registry schema is invalid")
    return result["bindings"]


@dataclass(frozen=True)
class ActorBinding:
    id: str
    attempt_id: str
    scope_id: str
    workspace_id: str
    actor_user_id: str
    desktop_id: str
    region_id: str
    executor_user: str
    browser_user: str
    workspace_dir: Path
    data_dir: Path
    tmp_dir: Path
    browser_state: Path
    browser_home: Path
    browser_resource_id: str
    config_path: Path
    executor_uid: int
    browser_uid: int
    identity_digest: str

    @property
    def automation_owner(self):
        return self.workspace_id

    def public(self):
        return {"version": 1, "protocol": PROTOCOL, "id": self.id, "attempt_id": self.attempt_id,
            "scope_id": self.scope_id, "workspace_id": self.workspace_id, "actor_user_id": self.actor_user_id,
            "desktop_id": self.desktop_id, "region_id": self.region_id,
            "executor_uid": self.executor_uid, "browser_uid": self.browser_uid,
            "browser_resource_id": self.browser_resource_id, "identity_digest": self.identity_digest,
            "isolation_mode": "guest_uid_mount"}


class ActorRegistry:
    def __init__(self, path):
        self.path = Path(path)
        if any(self.path == Path(root) or Path(root) in self.path.parents for root in ("/workspace", "/data", "/tmp")):
            raise PrivateActorError("Actor registry must remain outside child mount targets")

    def _bindings(self):
        from execution_identity import identity
        generic = os.environ.get("OPENBOX_EXECUTOR_USER", "")
        if sys.platform != "linux" or os.geteuid() != 0 or not generic:
            raise PrivateActorError("Unprivileged legacy execution must be configured")
        generic_uid = identity(generic).pw_uid
        rows = _read_config(self.path)
        result, used_ids, used_scopes, used_uids, used_paths = [], set(), set(), {generic_uid}, set()
        fields = {"id", "attempt_id", "workspace_id", "actor_user_id", "desktop_id", "region_id",
            "executor_user", "browser_user", "workspace_dir", "data_dir", "tmp_dir",
            "browser_state", "browser_home", "browser_resource_id"}
        for row in rows:
            if not isinstance(row, dict) or set(row) != fields or any(not isinstance(value, str) or not value or "\0" in value for value in row.values()):
                raise PrivateActorError("Actor binding schema is invalid")
            if (not _ID.fullmatch(row["id"]) or not _ID.fullmatch(row["attempt_id"])
                    or not _HEX.fullmatch(row["browser_resource_id"])):
                raise PrivateActorError("Actor binding identity is invalid")
            actor_scope = scope_id(row["workspace_id"], row["actor_user_id"])
            executor, browser = identity(row["executor_user"]), identity(row["browser_user"])
            if (row["id"] in used_ids or actor_scope in used_scopes or executor.pw_uid == browser.pw_uid
                    or executor.pw_uid in used_uids or browser.pw_uid in used_uids):
                raise PrivateActorError("Actor identities must be unique and distinct from the shared executor")
            used_ids.add(row["id"])
            used_scopes.add(actor_scope)
            used_uids.update((executor.pw_uid, browser.pw_uid))
            directories, identities = {}, {}
            for name in ("workspace_dir", "data_dir", "tmp_dir", "browser_state", "browser_home"):
                path = Path(row[name])
                if any(path == Path(root) or Path(root) in path.parents for root in ("/workspace", "/data", "/tmp")):
                    raise PrivateActorError("Actor storage must remain outside child mount targets")
                uid = 0 if name == "browser_state" else browser.pw_uid if name == "browser_home" else executor.pw_uid
                with _directory(path, uid=uid, mode=0o700) as descriptor:
                    entry = os.fstat(descriptor)
                    key = (entry.st_dev, entry.st_ino)
                    if key in used_paths:
                        raise PrivateActorError("Actor directories must not be shared")
                    used_paths.add(key)
                    identities[name] = [entry.st_dev, entry.st_ino, entry.st_uid, stat.S_IMODE(entry.st_mode)]
                directories[name] = path
            digest = hashlib.sha256(_canonical({"record": row, "directories": identities,
                "executor_uid": executor.pw_uid, "executor_gid": executor.pw_gid,
                "browser_uid": browser.pw_uid, "browser_gid": browser.pw_gid,
                "generic_uid": generic_uid}).encode()).hexdigest()
            result.append(ActorBinding(**{**row, **directories}, scope_id=actor_scope, config_path=self.path,
                executor_uid=executor.pw_uid, browser_uid=browser.pw_uid, identity_digest=digest))
        return result

    def lookup(self, binding_id, scope_id=None, attempt_id=None):
        rows = [row for row in self._bindings() if row.id == binding_id
                and (scope_id is None or row.scope_id == scope_id)
                and (attempt_id is None or row.attempt_id == attempt_id)]
        if len(rows) != 1:
            raise PrivateActorError("The original actor binding is unavailable")
        return rows[0]

    def resolve(self, scope_id):
        if not isinstance(scope_id, str) or not _HEX.fullmatch(scope_id):
            raise PrivateActorError("Invalid actor scope")
        rows = [row for row in self._bindings() if row.scope_id == scope_id]
        if len(rows) != 1:
            raise PrivateActorError("The actor has no pre-enrolled execution binding")
        return rows[0]

    def proof(self, binding):
        if not _legacy_file_worker:
            raise PrivateActorError("The legacy file worker must be mounted before private execution")
        current = self.lookup(binding.id, binding.scope_id, binding.attempt_id)
        if current != binding:
            raise PrivateActorError("Actor binding changed")
        from execution_identity import private_context, prepare_child
        program = """import json,os,sys
from pathlib import Path
def denied(path):
    try: fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK)
    except PermissionError: return True
    except OSError: return False
    os.close(fd)
    return False
def signal_denied(pid):
    try: os.kill(pid,0)
    except PermissionError: return True
    return False
s=dict(x.split(':',1) for x in Path('/proc/self/status').read_text().splitlines())
protected=json.loads(sys.argv[1])
print(json.dumps({'uid':os.getuid(),'groups':os.getgroups(),'nnp':s['NoNewPrivs'].strip(),
 'caps':[int(s[k].strip(),16) for k in ('CapInh','CapPrm','CapEff','CapAmb')],
 'mounts':{p:[os.stat(p).st_dev,os.stat(p).st_ino] for p in ('/workspace','/data','/tmp')},
 'namespace':os.readlink('/proc/self/ns/mnt'),
 'control_denied':all(denied(p) for p in protected), 'signal_denied':signal_denied(int(sys.argv[2]))}))
"""
        protected_actor = [str(binding.config_path), str(binding.browser_state), str(binding.browser_home),
                           f"/proc/{os.getpid()}/environ"]
        with private_context(binding):
            argv, env = prepare_child([sys.executable, "-I", "-S", "-c", program,
                                      _canonical(protected_actor), str(os.getpid())], {}, workdir="/workspace")
        try:
            completed = subprocess.run(argv, env=env, capture_output=True, timeout=5)
            actual = json.loads(completed.stdout) if completed.returncode == 0 else None
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise PrivateActorError("Actor execution probe failed") from exc
        checks = {"legacy_executor_is_unprivileged": True, "legacy_file_worker": True,
            "actor_uid": isinstance(actual, dict) and actual.get("uid") == binding.executor_uid,
            "groups_empty": isinstance(actual, dict) and actual.get("groups") == [],
            "no_new_privs": isinstance(actual, dict) and actual.get("nnp") == "1",
            "capabilities_empty": isinstance(actual, dict) and actual.get("caps") == [0, 0, 0, 0],
            "actor_cannot_read_control_or_browser": isinstance(actual, dict) and actual.get("control_denied") is True,
            "actor_cannot_signal_supervisor": isinstance(actual, dict) and actual.get("signal_denied") is True,
            "private_mount_namespace": isinstance(actual, dict) and actual.get("namespace") != os.readlink("/proc/self/ns/mnt")}
        for target, source in (("/workspace", binding.workspace_dir), ("/data", binding.data_dir), ("/tmp", binding.tmp_dir)):
            entry = source.stat()
            checks[target[1:] + "_mount"] = isinstance(actual, dict) and actual.get("mounts", {}).get(target) == [entry.st_dev, entry.st_ino]
        # The old, unprefixed API must not become a more powerful neighbour.
        # Opening checks permission without reading or emitting any file bytes.
        denial_program = """import json,os,sys
def denied(path):
    try:
        fd=os.open(path,os.O_RDONLY|os.O_NONBLOCK)
    except PermissionError:
        return True
    except OSError:
        return False
    os.close(fd)
    return False
paths=json.loads(sys.argv[1])
print(json.dumps({key:all(denied(path) for path in values) for key,values in paths.items()}))
"""
        protected = {"generic_cannot_read_actor_storage": [str(binding.workspace_dir), str(binding.data_dir), str(binding.tmp_dir)],
            "generic_cannot_read_browser_storage": [str(binding.browser_home), str(binding.browser_state)],
            "generic_cannot_read_control": [str(binding.config_path), f"/proc/{os.getpid()}/environ"]}
        with private_context(None):
            argv, env = prepare_child([sys.executable, "-I", "-S", "-c", denial_program, _canonical(protected)], {})
        try:
            completed = subprocess.run(argv, env=env, capture_output=True, timeout=5)
            denied = json.loads(completed.stdout) if completed.returncode == 0 else None
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise PrivateActorError("Legacy execution privacy probe failed") from exc
        for name in protected:
            checks[name] = isinstance(denied, dict) and denied.get(name) is True
        if not all(checks.values()) or self.lookup(binding.id, binding.scope_id, binding.attempt_id) != binding:
            raise PrivateActorError("Actor isolation proof is unavailable")
        return {**binding.public(), "isolation": {"mode": "guest_uid_mount", "verification": "passed", "checks": checks}}


def registry():
    value = os.environ.get(CONFIG_ENV, "")
    if not value:
        raise PrivateActorError("Private actor bindings are not configured")
    return ActorRegistry(value)


def enter_mounts(binding):
    """Only the fresh privileged launcher calls this; no mounts escape it."""
    if sys.platform != "linux" or os.geteuid() != 0:
        raise PrivateActorError("Actor mount isolation requires a root guest supervisor")
    if ActorRegistry(binding.config_path).lookup(binding.id, binding.scope_id, binding.attempt_id) != binding:
        raise PrivateActorError("Actor binding changed before launch")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.unshare.argtypes = [ctypes.c_int]
    libc.mount.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_void_p]
    if libc.unshare(0x00020000) != 0 or libc.mount(None, b"/", None, (1 << 14) | (1 << 18), None) != 0:
        raise PrivateActorError("The guest kernel refused private mount isolation")
    for source, target in ((binding.workspace_dir, "/workspace"), (binding.data_dir, "/data"), (binding.tmp_dir, "/tmp")):
        # Root owns target ancestors; an existing link must never redirect a
        # privileged bind into another part of the guest filesystem.
        descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            with _directory(source, uid=binding.executor_uid, mode=0o700) as src:
                if libc.mount(f"/proc/self/fd/{src}".encode(), f"/proc/self/fd/{descriptor}".encode(),
                              None, (1 << 12) | (1 << 14), None) != 0:
                    raise PrivateActorError("The guest kernel refused an actor mount")
        finally:
            os.close(descriptor)


class PrivateActorMiddleware:
    """Authenticate one fixed guest binding, then reuse existing AS handlers."""
    def __init__(self, app, get_api_key, get_env=None):
        self.app, self.get_api_key = app, get_api_key

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"} or not scope.get("path", "").startswith("/private-runtime/"):
            return await self.app(scope, receive, send)
        from starlette.datastructures import Headers
        from starlette.responses import JSONResponse
        import asyncio
        headers = Headers(scope=scope)
        expected = self.get_api_key()
        if not expected or not secrets.compare_digest(headers.get("x-api-key", ""), expected):
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 4403})
            return await JSONResponse({"detail": "Invalid API Key"}, 403)(scope, receive, send)
        started = False
        async def tracked_send(message):
            nonlocal started
            if message["type"] in {"http.response.start", "websocket.accept"}:
                started = True
            await send(message)
        try:
            parts = scope["path"].split("/", 3)
            binding_id, remaining = parts[2], "/" + parts[3] if len(parts) == 4 else "/"
            selected = registry()
            actor_scope = headers.get(SCOPE_HEADER, "")
            if binding_id == "resolve" and remaining == "/" and scope["type"] == "http" and scope["method"] == "GET":
                binding = selected.resolve(actor_scope)
                proof = await asyncio.to_thread(selected.proof, binding)
                return await JSONResponse(proof)(scope, receive, send)
            binding = selected.lookup(binding_id, actor_scope, headers.get(ATTEMPT_HEADER, ""))
            if remaining.startswith("/browser/"):
                # A separate finite router and browser UID own this path.
                return await self.app(scope, receive, tracked_send)
            if scope["type"] != "http":
                return await send({"type": "websocket.close", "code": 4403})
            if remaining in {"/alive", "/identity"} and scope["method"] == "GET":
                proof = await asyncio.to_thread(selected.proof, binding)
                return await JSONResponse({**proof, "status": "ok"})(scope, receive, send)
            from file_worker import handles
            allowed = (handles(remaining) or remaining in {"/execute", "/execute_stream", "/catalog", "/catalog/version"}
                       or remaining.startswith("/resource-control/"))
            if not allowed:
                raise PrivateActorError("This endpoint is not available in an actor execution scope")
            from execution_identity import private_context
            forwarded = {**scope, "path": remaining, "raw_path": remaining.encode(),
                         "openbox.original_path": scope["path"], "openbox.private_actor": binding}
            with private_context(binding):
                await self.app(forwarded, receive, tracked_send)
        except (PrivateActorError, OSError, ValueError):
            if started:
                raise
            if scope["type"] == "websocket":
                return await send({"type": "websocket.close", "code": 4403})
            return await JSONResponse({"detail": "Private actor execution is unavailable"}, 409)(scope, receive, send)
