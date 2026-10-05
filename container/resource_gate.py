"""Durable Action Server admission, including deferred streams and sockets.

The journal is a remote receipt for existing backend effect/operation IDs, not
a second task queue. Unknown subprocess/background outcomes never time out to
success. Native desktop/CDP channels are not certified by this protocol, so it
offers no human grant, reopen or force-drain operation. A closed epoch may
advance only after every journaled operation is confirmed terminal; the new
owner remains closed while a later handoff proves its other prerequisites.
"""
import asyncio
from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib.parse import parse_qs

from starlette.datastructures import Headers
from starlette.responses import JSONResponse


PROTOCOL = "resource_admission_v2"
_IDENTITY = re.compile(r"[A-Za-z0-9_:-]{1,160}\Z")


class GateError(Exception):
    def __init__(self, status, code):
        self.status, self.code = status, code
        super().__init__(code)


@dataclass(frozen=True)
class Fence:
    resource_id: str
    epoch: int
    owner_kind: str
    owner_id: str

    def __post_init__(self):
        if (not isinstance(self.resource_id, str) or not re.fullmatch(r"[0-9a-f]{64}", self.resource_id)
                or type(self.epoch) is not int or not 1 <= self.epoch < 2**63
                or self.owner_kind not in {"automation", "human"}
                or not isinstance(self.owner_id, str) or not 1 <= len(self.owner_id) <= 64
                or not _IDENTITY.fullmatch(self.owner_id)):
            raise GateError(400, "INVALID_RESOURCE_FENCE")


def identity(value):
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise GateError(400, "INVALID_OPERATION_ID")
    return value


def fence_from_headers(headers):
    values = [headers.get(name) for name in (
        "x-openbox-resource", "x-openbox-resource-epoch", "x-openbox-resource-owner", "x-openbox-resource-owner-id")]
    if all(value is None for value in values):
        return None
    if any(value is None for value in values) or not re.fullmatch(r"[1-9][0-9]{0,18}", values[1]):
        raise GateError(400, "INVALID_RESOURCE_FENCE")
    return Fence(values[0], int(values[1]), values[2], values[3])


class ResourceGate:
    def __init__(self, path, *, require_bound=False, automation_owner=None, authority_check=None):
        self.require_bound = require_bound
        self.automation_owner = automation_owner
        self.authority_check = authority_check
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS identity (singleton INTEGER PRIMARY KEY CHECK(singleton=1), journal_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS control (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), resource_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL CHECK(epoch>0), owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL,
                    admission TEXT NOT NULL CHECK(admission IN ('open','closed')), command_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY, effect_id TEXT, resource_id TEXT, epoch INTEGER, owner_kind TEXT, owner_id TEXT,
                    request_hash TEXT NOT NULL, claim_token TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('admitted','running','completed','canceled','unknown')),
                    created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS ix_remote_resource_state ON operations(state, created_at);
                CREATE INDEX IF NOT EXISTS ix_remote_resource_effect ON operations(effect_id);
                CREATE TABLE IF NOT EXISTS control_commands (
                    id TEXT PRIMARY KEY, action TEXT NOT NULL, payload_hash TEXT NOT NULL,
                    receipt TEXT NOT NULL, created_at REAL NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO identity VALUES (1, ?)", (secrets.token_hex(16),))
            db.commit()
        self.path.chmod(0o600)

    @contextmanager
    def transaction(self):
        # Each process/connection participates in the same SQLite write order.
        # There is no network, subprocess wait or async yield under this lock.
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

    @staticmethod
    def _same(row, fence):
        return fence is not None and tuple(row[key] for key in ("resource_id", "epoch", "owner_kind", "owner_id")) == (
            fence.resource_id, fence.epoch, fence.owner_kind, fence.owner_id)

    def _require(self, db, fence):
        self._authority()
        row = db.execute("SELECT * FROM control WHERE singleton=1").fetchone()
        if self.require_bound and (row is None or fence is None):
            raise GateError(423, "RESOURCE_BINDING_REQUIRED")
        if row and (row["admission"] != "open" or not self._same(row, fence)):
            raise GateError(423, "RESOURCE_CONTROL_HELD")

    def _authority(self):
        if self.authority_check is not None:
            self.authority_check()

    @staticmethod
    def _journal(db, expected):
        actual = db.execute("SELECT journal_id FROM identity WHERE singleton=1").fetchone()[0]
        if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{32}", expected) or not secrets.compare_digest(actual, expected):
            raise GateError(409, "RESOURCE_JOURNAL_CHANGED")
        return actual

    def bind(self, fence, command_id, journal_id):
        return self._control("bind", fence, command_id, journal_id)

    def close(self, fence, command_id, journal_id):
        return self._control("close", fence, command_id, journal_id)

    def advance_closed(self, fence, next_fence, command_id, journal_id):
        """Atomically retire an epoch without granting either owner input.

        This is an internal handoff step, not a claim of physical exclusivity.
        All epochs and legacy operations participate in drainage. A recorded
        command can be replayed after a lost response or later transition;
        matching current state alone never proves this command succeeded.
        """
        self._authority()
        identity(command_id)
        if (next_fence.resource_id != fence.resource_id or next_fence.epoch != fence.epoch + 1
                or next_fence.owner_kind == fence.owner_kind):
            raise GateError(400, "INVALID_RESOURCE_TRANSITION")
        payload = {"action": "advance_closed", "resource_id": fence.resource_id,
            "epoch": fence.epoch, "owner_kind": fence.owner_kind, "owner_id": fence.owner_id,
            "next_epoch": next_fence.epoch, "next_owner_kind": next_fence.owner_kind,
            "next_owner_id": next_fence.owner_id, "journal_id": journal_id}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.transaction() as db:
            self._journal(db, journal_id)
            prior = db.execute("SELECT * FROM control_commands WHERE id=?", (command_id,)).fetchone()
            if prior:
                if prior["payload_hash"] != digest:
                    raise GateError(409, "RESOURCE_COMMAND_CONFLICT")
                return {**self._status(db), "command_receipt": json.loads(prior["receipt"])}
            row = db.execute("SELECT * FROM control WHERE singleton=1").fetchone()
            if row is None or not self._same(row, fence):
                raise GateError(409, "RESOURCE_FENCE_CHANGED")
            if row["admission"] != "closed":
                raise GateError(409, "RESOURCE_MUST_BE_CLOSED")
            if db.execute("SELECT 1 FROM operations WHERE state NOT IN ('completed','canceled') LIMIT 1").fetchone():
                raise GateError(423, "RESOURCE_NOT_DRAINED")
            db.execute("UPDATE control SET epoch=?,owner_kind=?,owner_id=?,command_id=? WHERE singleton=1",
                (next_fence.epoch, next_fence.owner_kind, next_fence.owner_id, command_id))
            receipt = {"command_id": command_id, **payload, "admission": "closed"}
            db.execute("INSERT INTO control_commands VALUES (?,?,?,?,?)", (
                command_id, "advance_closed", digest, json.dumps(receipt, sort_keys=True), time.time()))
            return {**self._status(db), "command_receipt": receipt}

    def _control(self, action, fence, command_id, journal_id):
        self._authority()
        if self.automation_owner is not None and fence.owner_kind == "automation" and fence.owner_id != self.automation_owner:
            raise GateError(403, "RESOURCE_OWNER_MISMATCH")
        identity(command_id)
        payload = {"action": action, "resource_id": fence.resource_id, "epoch": fence.epoch,
            "owner_kind": fence.owner_kind, "owner_id": fence.owner_id, "journal_id": journal_id}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.transaction() as db:
            self._journal(db, journal_id)
            prior = db.execute("SELECT * FROM control_commands WHERE id=?", (command_id,)).fetchone()
            if prior:
                if prior["payload_hash"] != digest:
                    raise GateError(409, "RESOURCE_COMMAND_CONFLICT")
                receipt = json.loads(prior["receipt"])
                return {**self._status(db), "command_receipt": receipt}
            row = db.execute("SELECT * FROM control WHERE singleton=1").fetchone()
            if row:
                if not self._same(row, fence):
                    raise GateError(409, "RESOURCE_ALREADY_BOUND")
                # Retrying initial bind must never reopen a closed gate.
            else:
                if fence.owner_kind != "automation" or fence.epoch != 1:
                    raise GateError(409, "INITIAL_AUTOMATION_EPOCH_REQUIRED")
                mismatched = db.execute("SELECT 1 FROM operations WHERE resource_id IS NOT NULL AND "
                    "(resource_id != ? OR epoch != ? OR owner_kind != ? OR owner_id != ?) LIMIT 1",
                    (fence.resource_id, fence.epoch, fence.owner_kind, fence.owner_id)).fetchone()
                if mismatched:
                    raise GateError(409, "RESOURCE_HISTORY_CONFLICT")
                # An initial close never opens an admission window first.
                db.execute("INSERT INTO control VALUES (1,?,?,?,?,?,?)", (
                    fence.resource_id, fence.epoch, fence.owner_kind, fence.owner_id,
                    "closed" if action == "close" else "open", command_id))
            if action == "close":
                db.execute("UPDATE control SET admission='closed', command_id=? WHERE singleton=1", (command_id,))
            status = self._status(db)
            receipt = {"command_id": command_id, **payload, "admission": status["control"]["admission"]}
            db.execute("INSERT INTO control_commands VALUES (?,?,?,?,?)", (
                command_id, action, digest, json.dumps(receipt, sort_keys=True), time.time()))
            return {**status, "command_receipt": receipt}

    def command_receipt(self, command_id):
        identity(command_id)
        with self.transaction() as db:
            row = db.execute("SELECT receipt FROM control_commands WHERE id=?", (command_id,)).fetchone()
            if row is None:
                raise GateError(404, "RESOURCE_COMMAND_NOT_FOUND")
            return json.loads(row[0])

    def admit(self, headers, method, path, query=b""):
        fence = fence_from_headers(headers)
        parent = headers.get("x-openbox-resource-operation")
        step = headers.get("x-openbox-resource-step")
        if parent is not None:
            identity(parent)
        if step is not None:
            identity(step)
        if fence is not None and (parent is None or step is None):
            raise GateError(400, "RESOURCE_OPERATION_ID_REQUIRED")
        if fence is None and (parent is not None or step is not None):
            raise GateError(400, "RESOURCE_FENCE_REQUIRED")
        operation_id = step or "legacy_" + secrets.token_hex(24)
        claim = secrets.token_hex(24)
        digest = hashlib.sha256(method.encode() + b"\0" + path.encode() + b"\0" + query).hexdigest()
        stamp = time.time()
        with self.transaction() as db:
            if fence is not None:
                self._journal(db, headers.get("x-openbox-resource-journal"))
            self._require(db, fence)
            if db.execute("SELECT 1 FROM operations WHERE id=?", (operation_id,)).fetchone():
                # Never repeat even when the prior response was lost or its
                # request body differs. Receipt lookup is separate and read-only.
                raise GateError(409, "RESOURCE_OPERATION_ALREADY_RECORDED")
            db.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?, 'admitted',?,?)", (
                operation_id, parent, fence.resource_id if fence else None, fence.epoch if fence else None,
                fence.owner_kind if fence else None, fence.owner_id if fence else None, digest, claim, stamp, stamp))
            journal_id = db.execute("SELECT journal_id FROM identity").fetchone()[0]
        return {"id": operation_id, "claim": claim, "fence": fence, "journal_id": journal_id, "quiescent": False}

    def checkpoint(self, operation):
        with self.transaction() as db:
            self._journal(db, operation["journal_id"])
            self._require(db, operation["fence"])
            result = db.execute("UPDATE operations SET state='running', updated_at=? "
                "WHERE id=? AND claim_token=? AND state IN ('admitted','running')", (
                    time.time(), operation["id"], operation["claim"]))
            if result.rowcount != 1:
                raise GateError(409, "RESOURCE_OPERATION_RETIRED")

    def finish(self, operation, *, canceled=False):
        state = "canceled" if canceled else "completed" if operation["quiescent"] else "unknown"
        with self.transaction() as db:
            db.execute("UPDATE operations SET state=?, updated_at=? WHERE id=? AND claim_token=? "
                "AND state IN ('admitted','running')", (state, time.time(), operation["id"], operation["claim"]))

    def status(self):
        with self.transaction() as db:
            return self._status(db)

    @staticmethod
    def _status(db):
        control = db.execute("SELECT * FROM control WHERE singleton=1").fetchone()
        count = db.execute("SELECT count(*) FROM operations WHERE state NOT IN ('completed','canceled')").fetchone()[0]
        blockers = db.execute("SELECT id,effect_id,state FROM operations WHERE state NOT IN ('completed','canceled') "
            "ORDER BY created_at,id LIMIT 64").fetchall()
        return {"protocol": PROTOCOL, "supported_commands": ["bind", "close", "advance_closed"],
            "journal_id": db.execute("SELECT journal_id FROM identity").fetchone()[0],
            "control": dict(control) if control else None, "blocking_count": count,
            "blocking_operations": [dict(row) for row in blockers],
            "tracked_operations_drained": count == 0, "remote_exclusivity_verified": False}

    def receipt(self, operation_id):
        identity(operation_id)
        with self.transaction() as db:
            row = db.execute("SELECT id,effect_id,resource_id,epoch,owner_kind,owner_id,state,created_at,updated_at "
                "FROM operations WHERE id=?", (operation_id,)).fetchone()
            if row is None:
                raise GateError(404, "RESOURCE_OPERATION_NOT_FOUND")
            return dict(row)


async def checkpoint(request):
    operation = request.scope.get("openbox.resource_operation")
    if operation:
        await asyncio.to_thread(request.scope["openbox.resource_gate"].checkpoint, operation)


def quiescent(request):
    operation = request.scope.get("openbox.resource_operation")
    if operation:
        operation["quiescent"] = True


class ResourceMiddleware:
    def __init__(self, app, get_gate, get_api_key):
        self.app, self.get_gate, self.get_api_key = app, get_gate, get_api_key

    async def __call__(self, scope, receive, send):
        gate = self.get_gate()
        if gate is None or scope["type"] not in {"http", "websocket"}:
            return await self.app(scope, receive, send)
        path, kind = scope.get("path", ""), scope["type"]
        # A closed resource must still expose liveness, receipts and its close
        # protocol. Exact temporary-token release can only relinquish access.
        if kind == "http" and (path in {"/alive", "/docs", "/openapi.json", "/desktop/lease/release"}
                or path.startswith("/resource-control/")):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        api_key = headers.get("x-api-key", "")
        if kind == "websocket":
            api_key = parse_qs(scope.get("query_string", b"").decode()).get("api_key", [api_key])[0]
        expected = self.get_api_key()
        # HTTP normally reaches this after existing authentication; sockets
        # need the same check before they create any journal entry or process.
        if not expected or not secrets.compare_digest(api_key, expected):
            if kind == "websocket":
                return await send({"type": "websocket.close", "code": 4003})
            return await JSONResponse({"detail": "Invalid API Key"}, status_code=403)(scope, receive, send)
        operation = None
        started = False
        response_started = False

        async def checked_receive():
            message = await receive()
            if kind == "websocket" and message["type"] == "websocket.receive":
                await asyncio.to_thread(gate.checkpoint, operation)
            return message

        async def checked_send(message):
            nonlocal response_started
            if message["type"] == "websocket.send":
                await asyncio.to_thread(gate.checkpoint, operation)
            if message["type"] in {"http.response.start", "websocket.accept"}:
                response_started = True
            if message["type"] == "http.response.start":
                protected = {b"x-openbox-remote-operation", b"x-openbox-resource-journal"}
                message = {**message, "headers": [
                    *(item for item in message.get("headers", []) if item[0].lower() not in protected),
                    (b"x-openbox-remote-operation", operation["id"].encode()),
                    (b"x-openbox-resource-journal", operation["journal_id"].encode())]}
            await send(message)

        try:
            operation = await asyncio.to_thread(gate.admit, headers, scope.get("method", "WS"), scope.get("openbox.original_path", path),
                                               scope.get("query_string", b""))
            scope["openbox.resource_operation"], scope["openbox.resource_gate"] = operation, gate
            await asyncio.to_thread(gate.checkpoint, operation)
            started = True
            await self.app(scope, checked_receive, checked_send)
        except GateError as exc:
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 4423, "reason": exc.code})
            elif not response_started:
                await JSONResponse({"detail": exc.code}, status_code=exc.status)(scope, receive, send)
            else:
                raise
        finally:
            if operation:
                await asyncio.to_thread(gate.finish, operation, canceled=not started)
