"""One-request, unprivileged filesystem worker over anonymous pipes.

The supervisor never opens a caller-selected path. Existing HTTP handlers run
after the kernel identity transition, including multipart parsing, archive
extraction and skill install scripts. No listener or service credential is
given to the worker. Resource admission/receipts stay in the supervisor.
"""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import stat
import struct
import subprocess
import sys
import tempfile

PROTOCOL = "unprivileged_files_v1"
CHUNK = 64 * 1024
TIMEOUT = 180
_HEADER_LIMIT = 64 * 1024
_FILE_ROUTES = {"/upload", "/download", "/list_files", "/write_file", "/read_file", "/glob", "/grep", "/skills", "/kill"}
_RESPONSE_HEADERS = {"content-type", "content-length", "content-disposition", "etag", "cache-control"}
_INTERNAL_OPERATIONS = {"skill_projection", "skill_initialize"}
_JSON_LIMIT = 8 * 1024 * 1024
_STORAGE_OPERATIONS = {"json_read", "json_write", "workspace_scan", "workspace_read", "workspace_write"}


def handles(path):
    # Let the supervisor issue its normal same-origin slash redirect first.
    return not path.endswith("/") and (path in _FILE_ROUTES or path.startswith("/skills/"))


class FileWorkerError(RuntimeError):
    pass


def storage_request(operation, arguments, env, data=b""):
    """Synchronous bridge for existing config/GCS callers, with private spools.

    Cloud credentials stay in the caller. Anonymous temporary files avoid
    pipe deadlocks and extra in-memory copies for binary backup payloads.
    """
    from execution_identity import configured_user, prepare_child
    if not configured_user() or operation not in _STORAGE_OPERATIONS:
        raise FileWorkerError("Isolated storage operation is unavailable")
    envelope = json.dumps({"operation": operation, "arguments": arguments, "size": len(data)}, ensure_ascii=True).encode() + b"\n"
    if len(envelope) > _HEADER_LIMIT:
        raise FileWorkerError("Storage metadata exceeds its limit")
    argv, launch_env = prepare_child([sys.executable, "-I", str(Path(__file__).resolve())], env)
    with tempfile.TemporaryFile() as incoming, tempfile.TemporaryFile() as outgoing:
        incoming.write(envelope)
        incoming.write(data)
        incoming.seek(0)
        process = subprocess.Popen(argv, env=launch_env, stdin=incoming, stdout=outgoing,
            stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            if process.wait(timeout=TIMEOUT) != 0:
                raise FileWorkerError("Storage worker refused the operation")
            outgoing.seek(0)
            line = outgoing.readline(_HEADER_LIMIT + 1)
            if len(line) > _HEADER_LIMIT:
                raise FileWorkerError("Invalid storage response")
            header = json.loads(line)
            if not isinstance(header, dict) or header.get("status") != 200:
                raise FileWorkerError("Storage operation failed")
            if header.get("kind") == "json":
                body = outgoing.read(_JSON_LIMIT + 1)
                if len(body) > _JSON_LIMIT:
                    raise FileWorkerError("Storage metadata exceeds its limit")
                return json.loads(body)
            if operation != "workspace_read" or header.get("kind") != "binary":
                raise FileWorkerError("Invalid storage response kind")
            body = outgoing.read()
            if len(body) != header.get("size"):
                raise FileWorkerError("Incomplete workspace snapshot")
            return body
        except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
            raise FileWorkerError("Storage executor unavailable") from exc
        finally:
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)


@contextlib.contextmanager
def _workspace_parent(relative, *, create=False):
    # Resolve each component through an anchored directory FD. Checking
    # resolve() first would leave a symlink-swap window before the real write.
    from pathlib import PurePosixPath
    if not isinstance(relative, str) or not relative or "\\" in relative or "\0" in relative:
        raise FileWorkerError("Invalid workspace path")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts) or PurePosixPath(relative).is_absolute():
        raise FileWorkerError("Invalid workspace path")
    descriptor = os.open("/workspace", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in parts[:-1]:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(component, mode=0o755, dir_fd=descriptor)
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def _atomic_json_write(path, value):
    encoded = json.dumps(value, ensure_ascii=False, indent=2).encode()
    if len(encoded) > _JSON_LIMIT:
        raise FileWorkerError("Stored JSON exceeds its limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    import secrets
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    name = ".openbox-json-" + secrets.token_hex(16)
    try:
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(descriptor, "wb") as output:
            output.write(encoded)
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
        os.fsync(parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name, dir_fd=parent)
        os.close(parent)


def _storage_operation(envelope):
    operation, args = envelope["operation"], envelope.get("arguments", {})
    if operation == "json_read":
        path = Path(args["path"])
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise FileWorkerError("Stored JSON must be a regular file")
                data = source.read(_JSON_LIMIT + 1)
        except FileNotFoundError:
            result = args.get("default", {})
        else:
            if len(data) > _JSON_LIMIT:
                raise FileWorkerError("Stored JSON exceeds its limit")
            result = json.loads(data)
            if not isinstance(result, dict):
                raise FileWorkerError("Stored JSON must be an object")
    elif operation == "json_write":
        size = envelope.get("size", 0)
        if type(size) is not int or not 0 <= size <= _JSON_LIMIT:
            raise FileWorkerError("Stored JSON exceeds its limit")
        value = json.loads(_read_exact(size))
        if not isinstance(value, dict):
            raise FileWorkerError("Stored JSON must be an object")
        _atomic_json_write(Path(args["path"]), value)
        result = {"written": True}
    elif operation == "workspace_scan":
        # An unreadable file is not a deletion. Refuse the snapshot before
        # the cloud caller can remove an object absent from an incomplete scan.
        excluded = set(args.get("exclude", []))
        files, skipped = {}, []
        def fail(error):
            raise error
        for directory, dirs, names in os.walk("/workspace", followlinks=False, onerror=fail):
            dirs[:] = [name for name in dirs if name not in excluded]
            for name in dirs:
                path = Path(directory) / name
                if path.is_symlink():
                    skipped.append(path.relative_to("/workspace").as_posix())
            for name in names:
                relative = (Path(directory) / name).relative_to("/workspace").as_posix()
                if any(part in excluded for part in relative.split("/")):
                    continue
                with _workspace_parent(relative) as (parent, leaf):
                    descriptor = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                    try:
                        entry = os.fstat(descriptor)
                        if not stat.S_ISREG(entry.st_mode):
                            raise FileWorkerError("Backup source is not a regular file")
                        files[relative] = entry.st_mtime
                    finally:
                        os.close(descriptor)
        result = {"files": files, "skipped_directories": skipped}
    elif operation == "workspace_read":
        with _workspace_parent(args["path"]) as (parent, leaf):
            descriptor = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(descriptor, "rb") as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise FileWorkerError("Backup source is not a regular file")
                if before.st_mtime != args["mtime"]:
                    raise FileWorkerError("Backup source changed after scanning")
                _write(json.dumps({"status": 200, "kind": "binary", "size": before.st_size}).encode() + b"\n")
                remaining = before.st_size
                while remaining:
                    chunk = source.read(min(CHUNK, remaining))
                    if not chunk:
                        raise FileWorkerError("Backup source was truncated")
                    _write(chunk)
                    remaining -= len(chunk)
                after = os.fstat(source.fileno())
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise FileWorkerError("Backup source changed while reading")
        return
    elif operation == "workspace_write":
        size = envelope.get("size", 0)
        if type(size) is not int or size < 0:
            raise FileWorkerError("Invalid restore payload")
        with _workspace_parent(args["path"], create=True) as (parent, leaf):
            import secrets
            staging = ".openbox-restore-" + secrets.token_hex(16)
            descriptor = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                with os.fdopen(descriptor, "wb") as target:
                    remaining = size
                    while remaining:
                        chunk = _read_exact(min(CHUNK, remaining))
                        target.write(chunk)
                        remaining -= len(chunk)
                    target.flush()
                    os.utime(target.fileno(), (args["mtime"], args["mtime"]))
                    os.fsync(target.fileno())
                # Replace the directory entry, never follow an old symlink.
                os.replace(staging, leaf, src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(staging, dir_fd=parent)
        result = {"written": size}
    else:
        raise FileWorkerError("Unsupported storage operation")
    encoded = json.dumps(result, ensure_ascii=True).encode()
    if len(encoded) > _JSON_LIMIT:
        raise FileWorkerError("Storage metadata exceeds its limit")
    _write(b'{"status":200,"kind":"json"}\n' + encoded)


async def json_operation(operation, env):
    if operation not in _INTERNAL_OPERATIONS:
        raise FileWorkerError("Unsupported internal file operation")
    body, status = bytearray(), None
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(message):
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
        else:
            body.extend(message.get("body", b""))
            if len(body) > _JSON_LIMIT:
                raise FileWorkerError("File metadata exceeds the response limit")
    await run_request({"operation": operation}, receive, send, env)
    if status != 200:
        raise FileWorkerError("File metadata worker failed")
    return json.loads(body)


async def _feed(process, encoded, receive):
    process.stdin.write(encoded)
    await process.stdin.drain()
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise FileWorkerError("File request disconnected")
        body = message.get("body", b"")
        for offset in range(0, len(body), CHUNK):
            chunk = body[offset:offset + CHUNK]
            process.stdin.write(struct.pack("!I", len(chunk)) + chunk)
            await process.stdin.drain()
        if not message.get("more_body", False):
            process.stdin.write(struct.pack("!I", 0))
            await process.stdin.drain()
            process.stdin.close()
            return


async def run_request(envelope, receive, send, env):
    from execution_identity import configured_user, prepare_child
    if not configured_user():
        raise FileWorkerError("File worker requires an execution identity")
    encoded = json.dumps(envelope, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
    if len(encoded) > _HEADER_LIMIT:
        raise FileWorkerError("File request metadata is too large")
    argv, launch_env = prepare_child([sys.executable, "-I", str(Path(__file__).resolve())], env)
    process = await asyncio.create_subprocess_exec(*argv, env=launch_env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    feeder = asyncio.create_task(_feed(process, encoded, receive))
    feeder.add_done_callback(lambda _task: process.stdin.close())
    async def receive_response():
        line = await process.stdout.readline()
        if not line or len(line) > _HEADER_LIMIT:
            raise FileWorkerError("File worker returned no valid response")
        header = json.loads(line)
        if not isinstance(header, dict):
            raise FileWorkerError("Invalid file worker response")
        status = header.get("status")
        if type(status) is not int or not 200 <= status <= 599:
            raise FileWorkerError("Invalid file worker response status")
        headers = []
        fields = header.get("headers", [])
        if not isinstance(fields, list) or any(not isinstance(pair, list) or len(pair) != 2
                or not all(isinstance(value, str) for value in pair) for pair in fields):
            raise FileWorkerError("Invalid file worker response headers")
        for name, value in fields:
            if name.lower() in _RESPONSE_HEADERS and not any(c in name + value for c in "\r\n"):
                headers.append((name.lower().encode("ascii"), value.encode("latin1")))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        while chunk := await process.stdout.read(CHUNK):
            await send({"type": "http.response.body", "body": chunk, "more_body": True})
        if await process.wait() != 0:
            raise FileWorkerError("File worker exited before completing its response")
        if feeder.done() and not feeder.cancelled():
            failure = feeder.exception()
            # Validation may intentionally reject before reading a body.
            if failure and status < 400:
                raise FileWorkerError("File request did not finish") from failure
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    try:
        # The existing Wuying guest uses Python 3.10, before asyncio.timeout.
        await asyncio.wait_for(receive_response(), timeout=TIMEOUT)
    finally:
        feeder.cancel()
        # Kill before any await. Escaped descendants are not certified drained.
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
        if process.stdin is not None:
            process.stdin.close()
        with contextlib.suppress(BaseException):
            await feeder
        with contextlib.suppress(BaseException):
            await asyncio.shield(process.wait())


class FileOperationMiddleware:
    def __init__(self, app, enabled, get_env):
        self.app, self.enabled, self.get_env = app, enabled, get_env

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.enabled() or not handles(scope["path"]):
            return await self.app(scope, receive, send)
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from resource_gate import checkpoint
        from execution_identity import IsolationError
        # Inside ResourceMiddleware: recheck the admitted operation at spawn.
        await checkpoint(Request(scope))
        envelope = {"path": scope["path"], "method": scope["method"],
            "query": scope.get("query_string", b"").decode("latin1"),
            "headers": [(k.decode("latin1"), v.decode("latin1")) for k, v in scope.get("headers", [])
                        if k.lower() in (b"content-type", b"content-length", b"if-none-match")]}
        started = False
        async def tracked_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)
        try:
            await run_request(envelope, receive, tracked_send, self.get_env())
        except (FileWorkerError, IsolationError, asyncio.TimeoutError, TimeoutError, ValueError, OSError):
            if started:
                raise  # Truncated output must never become a success receipt.
            await JSONResponse({"detail": "File executor unavailable"}, status_code=502)(scope, receive, send)


def _read_exact(size):
    body = bytearray()
    while len(body) < size:
        chunk = sys.stdin.buffer.read(size - len(body))
        if not chunk:
            raise FileWorkerError("Incomplete file request")
        body.extend(chunk)
    return bytes(body)


def _read_chunk():
    size, = struct.unpack("!I", _read_exact(4))
    if size > CHUNK:
        raise FileWorkerError("Invalid request frame")
    return _read_exact(size) if size else b""


async def worker_main():
    if os.geteuid() == 0 or os.getgroups():
        raise FileWorkerError("Unprivileged worker identity required")
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    if status.get("NoNewPrivs", "").strip() != "1":
        raise FileWorkerError("Worker requires permanent privilege restrictions")
    line = sys.stdin.buffer.readline(_HEADER_LIMIT + 1)
    if len(line) > _HEADER_LIMIT or not line.endswith(b"\n"):
        raise FileWorkerError("Invalid request metadata")
    envelope = json.loads(line)
    operation = envelope.get("operation")
    if operation in _STORAGE_OPERATIONS:
        _storage_operation(envelope)
        return
    if operation not in _INTERNAL_OPERATIONS and not handles(envelope.get("path", "")):
        raise FileWorkerError("Unsupported file operation")
    # ASGI is called in-process; this key never authenticates the supervisor.
    os.environ["SESSION_API_KEY"] = "one-request-file-worker"
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import action_server
    if operation in _INTERNAL_OPERATIONS:
        if operation == "skill_projection":
            result = action_server._skill_catalogue_projection()
        else:
            action_server._initialize_skill_paths()
            result = {"initialized": True}
        data = json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode()
        if len(data) > _JSON_LIMIT:
            raise FileWorkerError("File metadata exceeds the response limit")
        header = {"status": 200, "headers": [["content-type", "application/json"]]}
        await asyncio.to_thread(_write, json.dumps(header).encode() + b"\n" + data)
        return
    finished_body = False
    response_complete = False
    disconnected = asyncio.Event()
    async def receive():
        nonlocal finished_body
        if finished_body:
            await disconnected.wait()
            return {"type": "http.disconnect"}
        chunk = await asyncio.to_thread(_read_chunk)
        finished_body = not chunk
        return {"type": "http.request", "body": chunk, "more_body": bool(chunk)}
    async def send(message):
        nonlocal response_complete
        if message["type"] == "http.response.start":
            header = {"status": message["status"], "headers": [
                (k.decode("latin1"), v.decode("latin1")) for k, v in message.get("headers", [])]}
            data = json.dumps(header, ensure_ascii=True).encode() + b"\n"
        else:
            data = message.get("body", b"")
            response_complete = not message.get("more_body", False)
        await asyncio.to_thread(_write, data)
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1", "method": envelope["method"], "scheme": "http",
        "path": envelope["path"], "raw_path": envelope["path"].encode(),
        "query_string": envelope.get("query", "").encode("latin1"), "root_path": "",
        "headers": [(k.encode("latin1"), v.encode("latin1")) for k, v in envelope.get("headers", [])]
            + [(b"x-api-key", b"one-request-file-worker")],
        "client": ("127.0.0.1", 0), "server": ("file-worker", 0)}
    try:
        await action_server.app(scope, receive, send)
    except Exception:
        if not response_complete:
            raise


def _write(data):
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    try:
        asyncio.run(worker_main())
    except Exception:
        raise SystemExit(126)
