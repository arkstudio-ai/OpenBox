"""Dedicated finite browser-profile service, separate from Action Server.

Only this process owns its Chromium pipe. A persistent operation journal is
written before dispatch; close and first pipe write have one event-loop order.
The entire finite operation (including paired key/button release) drains before
another owner is granted. Unknown outcomes never become terminal on a timer.
"""
import argparse
import asyncio
from contextlib import closing, contextmanager, asynccontextmanager
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import pwd
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.responses import JSONResponse
from starlette.datastructures import Headers

from browser_pipe import BrowserPipe
from browser_isolation import pending as isolation_pending, verify as verify_isolation


PROTOCOL = "browser_resource_v1"
ID = r"^[A-Za-z0-9_:-]{1,160}$"
HEX = r"^[0-9a-f]{32}$"
OPERATIONS = ("capture", "navigate", "back", "reload", "mouse", "key", "text", "wheel")


class BrowserError(Exception):
    def __init__(self, status, code, detail="Browser control requires inspection"):
        self.status, self.code, self.detail = status, code, detail
        super().__init__(code)

    def payload(self):
        return {"error": {"code": self.code, "detail": self.detail}}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Identity(Strict):
    resource_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    profile_id: str = Field(pattern=HEX)
    journal_id: str = Field(pattern=HEX)
    runtime_id: str = Field(pattern=HEX)


class Fence(Strict):
    resource_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    epoch: int = Field(ge=1, lt=2**31)
    owner_kind: Literal["automation", "human"]
    owner_id: str = Field(pattern=ID, max_length=64)


class Operation(Strict):
    identity: Identity
    fence: Fence
    operation_id: str = Field(pattern=ID)
    kind: Literal["capture", "navigate", "back", "reload", "mouse", "key", "text", "wheel"]
    args: dict = Field(default_factory=dict)
    human_token: str | None = Field(default=None, max_length=128)
    observation_id: str | None = Field(default=None, pattern=HEX)


class Command(Strict):
    identity: Identity
    fence: Fence
    command_id: str = Field(pattern=ID)
    actor_id: str = Field(pattern=ID, max_length=64)
    next_owner_id: str | None = Field(default=None, pattern=ID, max_length=64)
    ttl_seconds: int | None = Field(default=None, ge=1, le=120)
    human_token: str | None = Field(default=None, max_length=128)


class Hello(Strict):
    identity: Identity
    fence: Fence
    human_token: str = Field(min_length=1, max_length=128)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def validate_args(kind, args):
    fields = {"capture": set(), "navigate": {"url"}, "back": set(), "reload": set(),
              "mouse": {"x", "y", "button"}, "key": {"key"}, "text": {"text"},
              "wheel": {"x", "y", "delta_x", "delta_y"}}
    valid = set(args) == fields[kind]
    if kind == "navigate":
        url = args.get("url")
        valid = valid and isinstance(url, str) and len(url) <= 4096
        if valid:
            try:
                parsed = urlsplit(url)
                valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password
            except ValueError:
                valid = False
    if kind in {"mouse", "wheel"}:
        valid = valid and type(args.get("x")) is int and type(args.get("y")) is int
        valid = valid and 0 <= args.get("x", -1) < 1024 and 0 <= args.get("y", -1) < 768
    if kind == "mouse":
        valid = valid and args.get("button") in {"left", "right", "middle"}
    if kind == "wheel":
        valid = valid and all(type(args.get(k)) is int and abs(args[k]) <= 4096 for k in ("delta_x", "delta_y"))
    if kind == "key":
        valid = valid and args.get("key") in {"Enter", "Tab", "Backspace", "Delete", "Escape", "ArrowLeft",
            "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown", "Space"}
    if kind == "text":
        valid = valid and isinstance(args.get("text"), str) and 0 < len(args["text"]) <= 4096
    if not valid:
        raise BrowserError(422, "BROWSER_INVALID_OPERATION", "A supported finite browser operation is required")


class BrowserJournal:
    """One protected file per browser resource, and one live pipe supervisor."""
    def __init__(self, directory, resource_id, automation_owner):
        if not re.fullmatch(r"[0-9a-f]{64}", resource_id) or not re.fullmatch(ID, automation_owner):
            raise ValueError("Invalid browser binding")
        directory = Path(directory).absolute()
        for parent in (directory, *directory.parents):
            if parent.is_symlink():
                raise ValueError("Symlinked browser journal directories are forbidden")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.path = directory / "browser.sqlite3"
        if self.path.is_symlink():
            raise ValueError("Symlinked browser journal is forbidden")
        self._lock = open(directory / "supervisor.lock", "a+b")
        fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.runtime_id = secrets.token_hex(16)
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS binding (singleton INTEGER PRIMARY KEY, resource_id TEXT NOT NULL,
                    profile_id TEXT NOT NULL, journal_id TEXT NOT NULL, runtime_id TEXT NOT NULL,
                    automation_owner TEXT NOT NULL, secret TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS control (singleton INTEGER PRIMARY KEY, epoch INTEGER NOT NULL,
                    owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, admission TEXT NOT NULL,
                    status TEXT NOT NULL, expires_at REAL, token_hash TEXT, grant_id TEXT,
                    observation_id TEXT, updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
                    request TEXT NOT NULL, state TEXT NOT NULL, receipt TEXT, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
                    receipt TEXT NOT NULL);
            """)
            row = db.execute("SELECT resource_id,automation_owner FROM binding WHERE singleton=1").fetchone()
            if row and row != (resource_id, automation_owner):
                raise ValueError("A browser journal cannot change resource or automation owner")
            if row:
                db.execute("UPDATE binding SET runtime_id=? WHERE singleton=1", (self.runtime_id,))
                db.execute("UPDATE control SET admission='closed',status='hold',token_hash=NULL,observation_id=NULL WHERE singleton=1")
                for op_id, raw, created, state in db.execute("SELECT id,request,created_at,state FROM operations WHERE state IN ('admitted','running')").fetchall():
                    # Running is persisted before the first pipe write. An
                    # admitted row is therefore provably unsent even after a
                    # crash; only submitted work must remain unknown.
                    target = "canceled" if state == "admitted" else "unknown"
                    reason = "not_dispatched_before_restart" if state == "admitted" else "supervisor_restarted"
                    receipt = self._receipt(json.loads(raw), target, {"reason": reason}, created)
                    db.execute("UPDATE operations SET state=?,receipt=? WHERE id=?", (target, json.dumps(receipt), op_id))
            else:
                db.execute("INSERT INTO binding VALUES (1,?,?,?,?,?,?)", (resource_id, secrets.token_hex(16),
                    secrets.token_hex(16), self.runtime_id, automation_owner, secrets.token_hex(32)))
                db.execute("INSERT INTO control VALUES (1,1,'automation',?,'open','active',NULL,NULL,NULL,NULL,?)",
                    (automation_owner, time.time()))
            db.commit()
        self.path.chmod(0o600)
        with self.transaction() as db:
            binding = db.execute("SELECT * FROM binding").fetchone()
            self.identity = {k: binding[k] for k in ("resource_id", "profile_id", "journal_id", "runtime_id")}
            self.automation_owner, self._secret = binding["automation_owner"], binding["secret"]

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def release_lock(self):
        self._lock.close()

    def _fence(self, row):
        return {"resource_id": self.identity["resource_id"], **{k: row[k] for k in ("epoch", "owner_kind", "owner_id")}}

    def _require(self, db, identity, fence, *, open_required=False, token=None):
        if identity != self.identity:
            raise BrowserError(409, "BROWSER_IDENTITY_CHANGED")
        row = db.execute("SELECT * FROM control WHERE singleton=1").fetchone()
        if self._fence(row) != fence:
            raise BrowserError(409, "BROWSER_FENCE_CHANGED")
        if open_required and (row["admission"] != "open" or row["status"] != "active"):
            raise BrowserError(423, "BROWSER_CONTROL_HELD")
        if open_required and row["owner_kind"] == "human":
            if not token or not row["token_hash"] or not hmac.compare_digest(digest(token), row["token_hash"]):
                raise BrowserError(423, "BROWSER_TOKEN_INVALID")
        return row

    @staticmethod
    def _receipt(request, state, result, created):
        return {"operation_id": request["operation_id"], "identity": request["identity"],
            "fence": request["fence"], "kind": request["kind"], "request_hash": digest(request),
            "state": state, "result": result, "created_at": created, "finished_at": time.time()}

    def _close(self, db, *, status="hold"):
        for op in db.execute("SELECT * FROM operations WHERE state='admitted'").fetchall():
            receipt = self._receipt(json.loads(op["request"]), "canceled", {"reason": "not_dispatched"}, op["created_at"])
            db.execute("UPDATE operations SET state='canceled',receipt=? WHERE id=?", (json.dumps(receipt), op["id"]))
        db.execute("UPDATE control SET admission='closed',status=?,token_hash=NULL,observation_id=NULL,updated_at=? WHERE singleton=1",
            (status, time.time()))

    def tick(self):
        # Commit expiry before a later authorization exception; rolling back
        # the refusing request must not restore an expired human lease.
        with self.transaction() as db:
            row = db.execute("SELECT * FROM control").fetchone()
            if row["owner_kind"] == "human" and row["expires_at"] <= time.time() and row["admission"] == "open":
                self._close(db)

    def hold(self):
        with self.transaction() as db:
            self._close(db)

    def status(self, browser_live):
        self.tick()
        with self.transaction() as db:
            row = db.execute("SELECT * FROM control").fetchone()
            blockers = [dict(r) for r in db.execute("SELECT id,state FROM operations WHERE state NOT IN ('completed','canceled') ORDER BY created_at,id")]
            return {"protocol": PROTOCOL, "identity": dict(self.identity), "control": {
                "fence": self._fence(row), "admission": row["admission"], "status": row["status"],
                "expires_at": row["expires_at"]}, "blocking_operations": blockers,
                "last_observation_id": row["observation_id"], "fresh_observation_required": row["observation_id"] is None,
                "browser_live": browser_live, "supported_operations": list(OPERATIONS)}

    def _token(self, receipt):
        return hmac.new(bytes.fromhex(self._secret), json.dumps({"command_id": receipt["command_id"],
            "identity": receipt["identity"], "fence": receipt["target_fence"]}, sort_keys=True).encode(), hashlib.sha256).hexdigest()

    def command(self, action, command, *, browser_live):
        self.tick()
        payload = command.model_dump(exclude={"human_token"}, exclude_none=True)
        payload["action"] = action
        request_hash = digest(payload)
        with self.transaction() as db:
            # Replay requires the original runtime identity even if the source
            # epoch has advanced. It never grants a replacement runtime access.
            if command.identity.model_dump() != self.identity:
                raise BrowserError(409, "BROWSER_IDENTITY_CHANGED")
            prior = db.execute("SELECT * FROM commands WHERE id=?", (command.command_id,)).fetchone()
            if prior:
                if prior["request_hash"] != request_hash:
                    raise BrowserError(409, "BROWSER_COMMAND_CONFLICT")
                receipt = json.loads(prior["receipt"])
            else:
                row = self._require(db, payload["identity"], payload["fence"])
                if action in {"takeover", "giveback"}:
                    if row["admission"] != "closed":
                        raise BrowserError(409, "BROWSER_MUST_BE_CLOSED")
                    if db.execute("SELECT 1 FROM operations WHERE state NOT IN ('completed','canceled') LIMIT 1").fetchone():
                        raise BrowserError(423, "BROWSER_NOT_DRAINED")
                    if not browser_live:
                        raise BrowserError(423, "BROWSER_RUNTIME_UNAVAILABLE")
                    if row["epoch"] >= 2**31 - 1:
                        raise BrowserError(409, "BROWSER_EPOCH_EXHAUSTED")
                expires = row["expires_at"]
                if action == "close":
                    self._close(db, status="draining")
                elif action == "takeover":
                    if row["owner_kind"] != "automation" or row["owner_id"] != self.automation_owner:
                        raise BrowserError(409, "BROWSER_FENCE_CHANGED")
                    if command.next_owner_id != command.actor_id or command.ttl_seconds is None:
                        raise BrowserError(422, "BROWSER_INVALID_COMMAND")
                    expires = time.time() + command.ttl_seconds
                    db.execute("UPDATE control SET epoch=epoch+1,owner_kind='human',owner_id=?,admission='open',status='active',expires_at=?,grant_id=?,observation_id=NULL,updated_at=?",
                        (command.actor_id, expires, command.command_id, time.time()))
                elif action == "giveback":
                    if row["owner_kind"] != "human" or row["owner_id"] != command.actor_id:
                        raise BrowserError(423, "BROWSER_TOKEN_INVALID")
                    expires = None
                    db.execute("UPDATE control SET epoch=epoch+1,owner_kind='automation',owner_id=?,admission='open',status='active',expires_at=NULL,token_hash=NULL,grant_id=NULL,observation_id=NULL,updated_at=?",
                        (self.automation_owner, time.time()))
                elif action == "heartbeat":
                    if row["owner_kind"] != "human" or row["owner_id"] != command.actor_id or command.ttl_seconds is None:
                        raise BrowserError(423, "BROWSER_TOKEN_INVALID")
                    self._require(db, payload["identity"], payload["fence"], open_required=True, token=command.human_token)
                    expires = max(row["expires_at"], time.time() + command.ttl_seconds)
                    db.execute("UPDATE control SET expires_at=?,updated_at=?", (expires, time.time()))
                else:
                    raise BrowserError(404, "BROWSER_UNSUPPORTED_COMMAND")
                target = db.execute("SELECT * FROM control").fetchone()
                receipt = {"command_id": command.command_id, "action": action, "identity": self.identity,
                    "source_fence": payload["fence"], "target_fence": self._fence(target),
                    "request_hash": request_hash, "state": "applied", "expires_at": expires}
                if action == "takeover":
                    db.execute("UPDATE control SET token_hash=?", (digest(self._token(receipt)),))
                db.execute("INSERT INTO commands VALUES (?,?,?)", (command.command_id, request_hash, json.dumps(receipt)))
        result = {"command_receipt": receipt}
        if action == "takeover":
            result["human_token"] = self._token(receipt)
        return result

    def admit(self, operation):
        self.tick()
        request = operation.model_dump(exclude={"human_token"}, exclude_none=True)
        request_hash = digest(request)
        with self.transaction() as db:
            # A replay still needs current actor/fence access; receipts can be
            # read separately by the backend after control moved.
            row = self._require(db, request["identity"], request["fence"], open_required=True, token=operation.human_token)
            prior = db.execute("SELECT * FROM operations WHERE id=?", (operation.operation_id,)).fetchone()
            if prior:
                if prior["request_hash"] != request_hash:
                    raise BrowserError(409, "BROWSER_OPERATION_CONFLICT")
                if prior["receipt"]:
                    return json.loads(prior["receipt"])
                raise BrowserError(409, "BROWSER_OPERATION_PENDING")
            self._observation(row, operation)
            db.execute("INSERT INTO operations VALUES (?,?,?,'admitted',NULL,?)",
                (operation.operation_id, request_hash, json.dumps(request), time.time()))
        return None

    @staticmethod
    def _observation(row, operation):
        if operation.fence.owner_kind == "automation" and operation.kind != "capture":
            if not row["observation_id"] or operation.observation_id != row["observation_id"]:
                raise BrowserError(423, "BROWSER_OBSERVATION_REQUIRED")

    def begin(self, operation):
        self.tick()
        with self.transaction() as db:
            op = db.execute("SELECT * FROM operations WHERE id=?", (operation.operation_id,)).fetchone()
            if op["state"] == "canceled":
                return json.loads(op["receipt"])
            try:
                row = self._require(db, operation.identity.model_dump(), operation.fence.model_dump(),
                    open_required=True, token=operation.human_token)
                self._observation(row, operation)
            except BrowserError as error:
                receipt = self._receipt(json.loads(op["request"]), "canceled", {"reason": error.code}, op["created_at"])
                db.execute("UPDATE operations SET state='canceled',receipt=? WHERE id=?", (json.dumps(receipt), operation.operation_id))
                return receipt
            db.execute("UPDATE operations SET state='running' WHERE id=? AND state='admitted'", (operation.operation_id,))
            if operation.kind != "capture":
                db.execute("UPDATE control SET observation_id=NULL")
        return None

    def finish(self, operation, result, *, unknown=False):
        with self.transaction() as db:
            op = db.execute("SELECT * FROM operations WHERE id=?", (operation.operation_id,)).fetchone()
            if unknown:
                self._close(db)
            elif operation.kind == "capture":
                row = db.execute("SELECT * FROM control").fetchone()
                competing = db.execute("SELECT 1 FROM operations WHERE id!=? AND state NOT IN ('completed','canceled') LIMIT 1", (operation.operation_id,)).fetchone()
                eligible = (self._fence(row) == operation.fence.model_dump() and row["admission"] == "open" and not competing)
                observation = {"observation_id": secrets.token_hex(16), "identity": self.identity,
                    "fence": operation.fence.model_dump(), "eligible": bool(eligible),
                    **{k: result[k] for k in ("url", "frame_id", "loader_id", "sha256", "width", "height")}}
                result = {"png_base64": result["png_base64"], "observation": observation}
                if eligible:
                    db.execute("UPDATE control SET observation_id=?", (observation["observation_id"],))
            receipt = self._receipt(json.loads(op["request"]), "unknown" if unknown else "completed", result, op["created_at"])
            db.execute("UPDATE operations SET state=?,receipt=? WHERE id=?", (receipt["state"], json.dumps(receipt), operation.operation_id))
            return receipt

    def check_human(self, hello):
        self.tick()
        with self.transaction() as db:
            if hello.fence.owner_kind != "human":
                raise BrowserError(423, "BROWSER_TOKEN_INVALID")
            self._require(db, hello.identity.model_dump(), hello.fence.model_dump(), open_required=True, token=hello.human_token)

    def disconnected(self, hello):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM control").fetchone()
            if hello.identity.model_dump() == self.identity and self._fence(row) == hello.fence.model_dump() and row["owner_kind"] == "human":
                self._close(db)

    def receipt(self, kind, key):
        if not re.fullmatch(ID, key):
            raise BrowserError(404, "BROWSER_RECEIPT_NOT_FOUND")
        table = "commands" if kind == "command" else "operations"
        with self.transaction() as db:
            row = db.execute(f"SELECT receipt FROM {table} WHERE id=?", (key,)).fetchone()
            if not row:
                raise BrowserError(404, "BROWSER_RECEIPT_NOT_FOUND")
            if row["receipt"] is None:
                raise BrowserError(409, "BROWSER_OPERATION_PENDING")
            return json.loads(row["receipt"])


class BrowserSupervisor:
    def __init__(self, journal, pipe, *, authority_check=None):
        self.journal, self.pipe = journal, pipe
        self.authority_check = authority_check
        self._operation_lock = asyncio.Lock()
        self._expiry_task = None
        self.isolation = isolation_pending(pipe)

    @property
    def ready(self):
        return self.pipe.live and self.isolation["verification"] in {"passed", "diagnostic"}

    async def start(self):
        try:
            await self.pipe.start()
            self.isolation = await verify_isolation(self.journal, self.pipe)
        except BaseException:
            self.journal.hold()
            await self.pipe.stop()
            raise
        self._expiry_task = asyncio.create_task(self._expire())

    async def _expire(self):
        while True:
            await asyncio.sleep(.1)
            self.journal.tick()
            if not self.ready:
                self.journal.hold()

    async def stop(self):
        self.journal.hold()
        if self._expiry_task:
            self._expiry_task.cancel()
            await asyncio.gather(self._expiry_task, return_exceptions=True)
        await self.pipe.stop()
        self.journal.release_lock()

    def status(self):
        if not self.ready:
            self.journal.hold()
        return {**self.journal.status(self.ready), "browser_sandbox": not self.pipe.fixture_no_sandbox and self.pipe.isolation in {"chromium_sandbox", "wuying_guest_uid"},
                "isolation": self.isolation,
                **({"guest_binding": self.pipe.guest_binding.public()} if getattr(self.pipe, "guest_binding", None) is not None else {})}

    async def operate(self, operation):
        validate_args(operation.kind, operation.args)
        if self.authority_check is not None:
            self.authority_check()
        if not self.ready:
            self.journal.hold()
            raise BrowserError(423, "BROWSER_RUNTIME_UNAVAILABLE")
        prior = self.journal.admit(operation)
        if prior is not None:
            return {**self.status(), "receipt": prior}
        async with self._operation_lock:
            if self.authority_check is not None:
                self.authority_check()
            canceled = self.journal.begin(operation)
            if canceled is not None:
                return {**self.status(), "receipt": canceled}
            try:
                # No await separates the durable running checkpoint from the
                # first pipe write inside execute/call. Close on this sole
                # event loop cannot pass that actual dispatch boundary.
                result = await self.pipe.execute(operation.kind, operation.args)
            except BaseException as error:
                receipt = self.journal.finish(operation, {"reason": "browser_response_unconfirmed"}, unknown=True)
                if not isinstance(error, Exception):
                    raise
                return {**self.status(), "receipt": receipt}
            receipt = self.journal.finish(operation, result)
            return {**self.status(), "receipt": receipt}


def create_app(supervisor, api_key, *, manage_lifespan=True):
    if not isinstance(api_key, str) or len(api_key) < 16:
        raise ValueError("A nonempty backend-only browser credential is required")

    @asynccontextmanager
    async def lifespan(_app):
        if manage_lifespan:
            await supervisor.start()
        try:
            yield
        finally:
            if manage_lifespan:
                await supervisor.stop()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def authenticate(request, call_next):
        if not hmac.compare_digest(request.headers.get("x-api-key", ""), api_key):
            return JSONResponse(BrowserError(403, "BROWSER_AUTH_REQUIRED").payload(), status_code=403)
        if int(request.headers.get("content-length", "0")) > 64 * 1024:
            return JSONResponse(BrowserError(413, "BROWSER_REQUEST_TOO_LARGE").payload(), status_code=413)
        return await call_next(request)

    @app.exception_handler(BrowserError)
    async def browser_error(_request, error):
        return JSONResponse(error.payload(), status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, _error):
        # Do not echo rejected payloads that might contain a human token.
        return JSONResponse(BrowserError(422, "BROWSER_INVALID_REQUEST").payload(), status_code=422)

    @app.get("/v1/status")
    async def status():
        return supervisor.status()

    @app.post("/v1/operations")
    async def operation(body: Operation):
        return await supervisor.operate(body)

    @app.get("/v1/operations/{operation_id}")
    async def operation_receipt(operation_id: str):
        return {"protocol": PROTOCOL, "identity": supervisor.journal.identity,
                "receipt": supervisor.journal.receipt("operation", operation_id)}

    @app.get("/v1/control/receipts/{command_id}")
    async def command_receipt(command_id: str):
        return {"protocol": PROTOCOL, "identity": supervisor.journal.identity,
                "command_receipt": supervisor.journal.receipt("command", command_id)}

    @app.post("/v1/control/{action}")
    async def control(action: str, body: Command):
        result = supervisor.journal.command(action, body, browser_live=supervisor.ready)
        return {**supervisor.status(), **result}

    @app.websocket("/v1/ws")
    async def human(ws: WebSocket):
        if not hmac.compare_digest(ws.headers.get("x-api-key", ""), api_key):
            await ws.close(code=4003)
            return
        await ws.accept()
        hello = None
        bound = None
        try:
            hello = Hello.model_validate(await asyncio.wait_for(ws.receive_json(), 5))
            supervisor.journal.check_human(hello)
            bound = hello
            await ws.send_json(supervisor.status())
            while True:
                supervisor.journal.check_human(hello)
                try:
                    raw = await asyncio.wait_for(ws.receive_json(), .2)
                except asyncio.TimeoutError:
                    continue
                body = Operation.model_validate(raw)
                if body.identity != hello.identity or body.fence != hello.fence or body.human_token != hello.human_token:
                    raise BrowserError(423, "BROWSER_TOKEN_INVALID")
                supervisor.journal.check_human(hello)
                await ws.send_json(await supervisor.operate(body))
        except (ValidationError, ValueError):
            await ws.send_json(BrowserError(422, "BROWSER_INVALID_REQUEST").payload())
            await ws.close(code=4423)
        except BrowserError as error:
            await ws.send_json(error.payload())
            await ws.close(code=4423)
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        finally:
            if bound is not None:
                supervisor.journal.disconnected(bound)

    return app


class WuyingBrowserMount:
    """Finite browser resources inside the original guest Action Server.

    Only pre-enrolled root-owned ActorBindings may start a browser. Discovery
    never starts or substitutes a process. All entry points keep the original
    binding/attempt, including both directions of an established WebSocket.
    """
    def __init__(self, *, get_registry=None, get_api_key, chromium=None):
        if get_registry is None:
            from private_actor import registry
            get_registry = registry
        self.get_registry, self.get_api_key = get_registry, get_api_key
        self.chromium = chromium or os.environ.get("OPENBOX_PRIVATE_CHROMIUM", "/opt/google/chrome/chrome")
        self._entries = {}
        self._locks = {}

    def lookup(self, binding_id, headers):
        selected = self.get_registry()
        binding = selected.lookup(binding_id, headers.get("x-openbox-private-scope", ""),
                                  headers.get("x-openbox-private-attempt", ""))
        return selected, binding

    def current(self, original):
        latest = self.get_registry().lookup(original.id, original.scope_id, original.attempt_id)
        if latest != original:
            raise BrowserError(423, "BROWSER_GUEST_BINDING_CHANGED")
        return latest

    async def prepare(self, selected, binding):
        lock = self._locks.setdefault(binding.id, asyncio.Lock())
        async with lock:
            self.current(binding)
            prior = self._entries.get(binding.id)
            if prior is not None:
                if prior[0] != binding:
                    prior[1].journal.hold()
                    raise BrowserError(423, "BROWSER_GUEST_BINDING_CHANGED")
                return prior[1].status()
            # This is an actual namespace/UID launch, not a configured flag.
            # Never hold a SQL transaction or cloud request while it runs.
            proof = await asyncio.to_thread(selected.proof, binding)
            self.current(binding)
            browser = pwd.getpwnam(binding.browser_user)
            journal = BrowserJournal(binding.browser_state / "control", binding.browser_resource_id,
                                     binding.workspace_id)
            pipe = BrowserPipe(self.chromium, binding.browser_home / "profile",
                uid=browser.pw_uid, gid=browser.pw_gid, isolation="wuying_guest_uid",
                guest_binding=binding, diagnostics_dir=binding.browser_state)
            supervisor = BrowserSupervisor(journal, pipe, authority_check=lambda: self.current(binding))
            try:
                await supervisor.start()
                self.current(binding)
                application = create_app(supervisor, self.get_api_key(), manage_lifespan=False)
            except BaseException:
                await supervisor.stop()
                raise
            self._entries[binding.id] = binding, supervisor, application, proof
            return supervisor.status()

    async def stop(self):
        for _, supervisor, _, _ in list(self._entries.values()):
            await supervisor.stop()
        self._entries.clear()

    async def dispatch(self, scope, receive, send):
        match = re.fullmatch(r"/private-runtime/([A-Za-z0-9_-]{8,96})/browser(/.*)?", scope.get("path", ""))
        if match is None:
            return False
        headers = Headers(scope=scope)
        key = self.get_api_key()
        if not key or not hmac.compare_digest(headers.get("x-api-key", ""), key):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4003})
            else:
                await JSONResponse(BrowserError(403, "BROWSER_AUTH_REQUIRED").payload(), 403)(scope, receive, send)
            return True
        entry = None
        response_started = False
        try:
            selected, binding = self.lookup(match.group(1), headers)
            path = match.group(2) or "/"
            if path == "/prepare" and scope["type"] == "http" and scope["method"] == "POST":
                result = await self.prepare(selected, binding)
                self.current(binding)
                await JSONResponse(result)(scope, receive, send)
                return True
            entry = self._entries.get(binding.id)
            if entry is None:
                raise BrowserError(409, "BROWSER_PREPARATION_REQUIRED")
            if entry[0] != binding:
                raise BrowserError(423, "BROWSER_GUEST_BINDING_CHANGED")

            def check():
                try:
                    self.current(binding)
                except Exception:
                    entry[1].journal.hold()
                    raise BrowserError(423, "BROWSER_GUEST_BINDING_CHANGED") from None

            async def checked_receive():
                check()
                message = await receive()
                check()
                return message

            async def checked_send(message):
                nonlocal response_started
                check()
                if message["type"] in {"http.response.start", "websocket.accept"}:
                    response_started = True
                await send(message)

            check()
            forwarded = {**scope, "path": path, "raw_path": path.encode(),
                         "root_path": scope.get("root_path", "") + scope["path"][:-len(path)]}
            await entry[2](forwarded, checked_receive, checked_send)
        except Exception as error:
            from private_actor import PrivateActorError
            if entry is not None:
                entry[1].journal.hold()
            if isinstance(error, BrowserError):
                failure = error
            elif isinstance(error, (PrivateActorError, OSError, ValueError)):
                failure = BrowserError(423, "BROWSER_GUEST_UNAVAILABLE")
            else:
                raise
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 4423, "reason": failure.code})
            elif not response_started:
                await JSONResponse(failure.payload(), failure.status)(scope, receive, send)
            else:
                raise
        return True


class WuyingBrowserMiddleware:
    def __init__(self, app, mount):
        self.app, self.mount = app, mount

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"http", "websocket"} and await self.mount.dispatch(scope, receive, send):
            return
        await self.app(scope, receive, send)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--resource-id", required=True)
    parser.add_argument("--automation-owner", required=True)
    parser.add_argument("--chromium", required=True)
    parser.add_argument("--browser-uid", type=int)
    parser.add_argument("--browser-gid", type=int)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--isolation", choices=("chromium_sandbox", "container_uid"), default="chromium_sandbox")
    parser.add_argument("--fixture-no-sandbox", action="store_true")
    args = parser.parse_args()
    state = Path(args.state_dir).absolute()
    state.mkdir(mode=0o755, parents=True, exist_ok=True)
    journal = BrowserJournal(state / "control", args.resource_id, args.automation_owner)
    browser_home = state / ("browser-" + journal.identity["profile_id"])
    browser_home.mkdir(mode=0o700, exist_ok=True)
    pipe = BrowserPipe(args.chromium, browser_home / "profile", uid=args.browser_uid, gid=args.browser_gid,
                       isolation=args.isolation, fixture_no_sandbox=args.fixture_no_sandbox)
    import uvicorn
    uvicorn.run(create_app(BrowserSupervisor(journal, pipe), os.environ.get("BROWSER_RESOURCE_API_KEY", "")),
                host=args.host, port=args.port, workers=1, ws_max_size=64 * 1024, access_log=False)


if __name__ == "__main__":
    main()
