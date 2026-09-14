"""Admin authorization and audit for trajectory reads (SPEC §8.12).

JWT role claims never grant access. The bearer token is verified locally
(``JWT_SECRET``: type ``access``, ``sub``, ``exp``) and checked against the
shared ``jwt_bl:{jti}`` blacklist. Account state, role, the mobile session and
the ``TRAJECTORY_ADMIN_*`` allowlist come from the business backend: over
``POST /api/internal/trajectory/viewer`` from the external worker, in-process in
embedded mode. Those facts are cached per (user, client, mobile session) for
``TRAJECTORY_AUTH_CACHE_SECONDS``; ``revalidate_viewer`` after slow content reads
bypasses the cache. An unreachable authority is a 503, never a pass.

Audit facts are written to ``trajectory_audit_outbox`` and delivered in batches
to ``POST /api/internal/trajectory/audit``.
"""
import asyncio
import os
import time
from datetime import timedelta
from uuid import uuid4

import httpx
from fastapi import Depends, HTTPException, Request, Response
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from starlette.exceptions import HTTPException as StarletteHTTPException

from auth import middleware
from auth.jwt import decode_access_token
from core.log import create_logger
from trajectory.config import integer
from trajectory.types import iso, now

log = create_logger("trajectory.auth")

DEFAULT_BACKEND_URL = "http://backend:8080"
VIEWER_PATH = "/api/internal/trajectory/viewer"
AUDIT_PATH = "/api/internal/trajectory/audit"
UNAVAILABLE = "Trajectory authorization is unavailable"
AUDIT_DEDUPE_SECONDS = 60
DEDUPED_AUDIT_ACTIONS = frozenset({"admin.trajectory.list", "admin.trajectory.view"})
#: Refusals of an audit batch that resending the same batch cannot fix.
AUDIT_REJECTED_STATUSES = frozenset({400, 413, 422})

_bearer = HTTPBearer(auto_error=False)
# Desktop mode without JWT_SECRET: the same default administrator as auth.middleware.
_SINGLE_USER = {"user_id": "default", "client": "web", "sid": None, "jti": None, "exp": None}
_CACHE_LIMIT = 1024
_clock = time.monotonic


class NoStoreRoute(APIRoute):
    """Observation content and authentication tickets must not enter caches."""
    def get_route_handler(self):
        original = super().get_route_handler()
        async def handler(request):
            try:
                response = await original(request)
            except StarletteHTTPException as exc:
                response = await http_exception_handler(request, exc)
            except RequestValidationError as exc:
                response = await request_validation_exception_handler(request, exc)
            response.headers['Cache-Control'] = 'no-store'
            return response
        return handler


# -- Authority backends --

class AuditRejected(Exception):
    """The backend refused audit entries; sending the same entries again cannot succeed."""


class HttpBackend:
    """The business backend's internal endpoints, used by the external worker."""

    def __init__(self, base_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 5.0):
        self._headers = {"X-Internal-Token": token}
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), transport=transport,
                                         timeout=timeout, trust_env=False)

    @classmethod
    def from_env(cls) -> "HttpBackend":
        from core.config import get_config
        url = (os.getenv("TRAJECTORY_BACKEND_INTERNAL_URL") or "").strip() or DEFAULT_BACKEND_URL
        return cls(url, get_config().internal_api_token)

    async def viewer(self, user_id: str, *, client: str | None, sid: str | None, jti: str | None) -> dict:
        response = await self._client.post(VIEWER_PATH, headers=self._headers,
                                           json={"user_id": user_id, "client": client, "sid": sid, "jti": jti})
        response.raise_for_status()
        return response.json()

    async def audit(self, entries: list[dict]) -> None:
        response = await self._client.post(AUDIT_PATH, headers=self._headers, json={"entries": entries})
        if response.status_code in AUDIT_REJECTED_STATUSES:
            raise AuditRejected(f"HTTP {response.status_code}")
        response.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()


class LocalBackend:
    """The same lookups and writes against this process's business database (embedded mode)."""

    async def viewer(self, user_id: str, *, client: str | None, sid: str | None, jti: str | None) -> dict:
        from api.internal import trajectory_viewer_facts
        return await trajectory_viewer_facts(user_id, client=client, sid=sid)

    async def audit(self, entries: list[dict]) -> None:
        from pydantic import ValidationError
        from api.internal import TrajectoryAuditBatch, write_trajectory_audit
        try:
            batch = TrajectoryAuditBatch.model_validate({"entries": entries})
        except ValidationError as exc:
            raise AuditRejected("Invalid trajectory audit entries") from exc
        await write_trajectory_audit(batch.entries)

    async def close(self) -> None:
        return None


_backend: HttpBackend | LocalBackend | None = None
_local = LocalBackend()
# (user_id, client, sid) -> (fetch start on _clock, facts)
_facts: dict[tuple, tuple[float, dict]] = {}
# (viewer, session, action) -> last recorded on _clock
_audited: dict[tuple, float] = {}


def configure_backend(backend: HttpBackend | LocalBackend | None) -> None:
    """Install the viewer and audit backend (None: this process's business database)."""
    global _backend
    _backend = backend
    _facts.clear()


def get_backend() -> HttpBackend | LocalBackend:
    return _backend if _backend is not None else _local


def cache_seconds() -> int:
    return integer("TRAJECTORY_AUTH_CACHE_SECONDS", 5)


def clear_viewer_cache() -> None:
    _facts.clear()


def _checked(user_id: str, value) -> dict:
    """Viewer facts read fail-closed: anything missing or malformed denies."""
    if not isinstance(value, dict) or value.get("user_id") != user_id:
        raise ValueError("Viewer facts do not describe the requested user")
    return {"user_id": user_id, "role": value.get("role"), "is_active": value.get("is_active") is True,
            "is_deleted": value.get("is_deleted") is not False,
            "mobile_session_valid": value.get("mobile_session_valid") is True,
            "admin_enabled": value.get("admin_enabled") is True}


async def viewer_facts(user_id: str, *, client: str | None = "web", sid: str | None = None,
                       jti: str | None = None, fresh: bool = False) -> dict:
    """Backend authority facts for one viewer, cached unless ``fresh``."""
    key = (user_id, client, sid)
    started = _clock()
    cached = _facts.get(key)
    if not fresh and cached is not None and started - cached[0] < cache_seconds():
        return cached[1]
    try:
        facts = _checked(user_id, await get_backend().viewer(user_id, client=client, sid=sid, jti=jti))
    except Exception as exc:
        log.warning("Trajectory viewer introspection failed user_id=%s error_type=%s", user_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail=UNAVAILABLE) from exc
    if len(_facts) >= _CACHE_LIMIT:
        ttl = cache_seconds()
        for stale in [item for item, (fetched, _) in _facts.items() if started - fetched >= ttl]:
            del _facts[stale]
        if len(_facts) >= _CACHE_LIMIT:
            _facts.clear()
    # A slower lookup that started earlier must not replace newer facts.
    current = _facts.get(key)
    if current is None or current[0] <= started:
        _facts[key] = (started, facts)
    return facts


def _mobile_session_error(client: str | None) -> HTTPException:
    """The 401 of ``auth.mobile.session_error`` for a token whose mobile session is not valid."""
    code = "AUTH_MOBILE_SESSION_REPLACED" if client == "mobile" else "AUTH_MOBILE_LOGIN_REQUIRED"
    return HTTPException(401, detail={"code": code, "message": "Please sign in again on this device."},
                         headers={"X-Error-Code": code})


async def assert_admin(user_id: str, *, client: str | None = "web", sid: str | None = None,
                       jti: str | None = None, fresh: bool = False) -> dict:
    """401 inactive, deleted or an invalid mobile session; 403 not an admin; 404 administration disabled."""
    facts = await viewer_facts(user_id, client=client, sid=sid, jti=jti, fresh=fresh)
    if facts["is_deleted"] or not facts["is_active"]:
        raise HTTPException(status_code=401, detail="Account is inactive or deleted")
    if not facts["mobile_session_valid"]:
        raise _mobile_session_error(client)
    if facts["role"] != "admin":
        raise HTTPException(status_code=403, detail="Platform administrator access required")
    if not facts["admin_enabled"]:
        raise HTTPException(status_code=404, detail="Trajectory administration is disabled")
    return {"user_id": user_id, "role": "admin"}


# -- Tokens --

async def token_revoked(jti: str | None) -> bool:
    """``jwt_bl:{jti}`` in the shared cache; an unreachable cache is a 503."""
    cache = middleware._cache
    if not jti or cache is None:
        return False
    try:
        return bool(await cache.exists(f"jwt_bl:{jti}"))
    except Exception as exc:
        log.warning("Token revocation check failed error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail=UNAVAILABLE) from exc


async def authenticate(request: Request, credentials: HTTPAuthorizationCredentials | None) -> dict:
    """The verified bearer identity: user_id, client, sid, jti and exp."""
    if not middleware.is_auth_enabled():
        return dict(_SINGLE_USER)
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_access_token(credentials.credentials)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    jti = payload.get("jti")
    if await token_revoked(jti):
        raise HTTPException(status_code=401, detail="Token has been revoked")
    client = payload.get("client")
    if request.headers.get("X-Client-Type", "").lower() == "mobile" and client != "mobile":
        raise _mobile_session_error(None)
    return {"user_id": user_id, "client": client, "sid": payload.get("sid"), "jti": jti, "exp": payload.get("exp")}


async def require_trajectory_admin(request: Request, response: Response,
                                   credentials: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> dict:
    response.headers['Cache-Control'] = 'no-store'
    identity = await authenticate(request, credentials)
    viewer = await assert_admin(identity["user_id"], client=identity["client"], sid=identity["sid"],
                                jti=identity["jti"])
    request.state.trajectory_identity = identity
    return viewer


async def revalidate_viewer(request: Request, viewer_id: str) -> dict:
    """Recheck revocable authority after potentially slow content I/O, bypassing cached facts."""
    identity = dict(_SINGLE_USER)
    if middleware.is_auth_enabled():
        credentials = await _bearer(request)
        if credentials is None:
            raise HTTPException(401, detail='Authentication is no longer valid')
        identity = await authenticate(request, credentials)
    if identity['user_id'] != viewer_id:
        raise HTTPException(401, detail='Authentication is no longer valid')
    return await assert_admin(viewer_id, client=identity["client"], sid=identity["sid"], jti=identity["jti"],
                              fresh=True)


# -- Audit --

def _forget_old_audits(moment: float) -> None:
    if len(_audited) > 4096:
        for key in [key for key, last in _audited.items() if moment - last >= AUDIT_DEDUPE_SECONDS]:
            del _audited[key]


async def record_audit(user_id: str, action: str, *, target_id: str | None = None, details: dict | None = None,
                       request: Request | None = None, resource_type: str = "trajectory") -> bool:
    """Queue one audit fact; best effort, so a failure never fails the read.

    ``list`` and ``view`` repeat on every poll and are recorded once per
    (viewer, session, action) per minute. Returns whether a row was queued.
    """
    key = (user_id, target_id, action)
    moment = _clock()
    if action in DEDUPED_AUDIT_ACTIONS:
        last = _audited.get(key)
        if last is not None and moment - last < AUDIT_DEDUPE_SECONDS:
            return False
        _audited[key] = moment
        _forget_old_audits(moment)
    try:
        from trajectory.store.database import trace_session
        from trajectory.store.models import TrajectoryAuditOutbox
        timestamp = now()
        payload = {"id": uuid4().hex, "user_id": user_id, "workspace_id": None, "action": action,
                   "resource_type": resource_type, "resource_id": target_id, "details": details,
                   "ip_address": request.client.host if request is not None and request.client else None,
                   "user_agent": request.headers.get("user-agent") if request is not None else None,
                   "created_at": iso(timestamp)}
        async with trace_session() as db:
            db.add(TrajectoryAuditOutbox(payload=payload, attempts=0, next_attempt_at=timestamp, created_at=timestamp))
        return True
    except Exception as exc:
        if _audited.get(key) == moment:
            del _audited[key]
        log.warning("Failed to record trajectory audit action=%s actor=%s error_type=%s",
                    action, user_id, type(exc).__name__)
        return False


class AuditDelivery:
    """Delivers ``trajectory_audit_outbox`` rows to the backend in batches.

    A pass claims due rows by moving ``next_attempt_at`` past a lease in one
    conditional UPDATE, so concurrent workers never send a row twice within the
    lease. Delivered rows are deleted and failed rows back off (5 s up to 1 h);
    entry ids make a redelivery after a crash idempotent in the backend.
    """
    BATCH = 100
    LEASE_SECONDS = 120
    INTERVAL_SECONDS = 2.0
    MAX_BACKOFF_SECONDS = 3600

    def __init__(self, backend: HttpBackend | LocalBackend | None = None, *, interval: float | None = None):
        self._backend = backend
        self._interval = self.INTERVAL_SECONDS if interval is None else interval
        self._task: asyncio.Task | None = None

    @classmethod
    def backoff(cls, attempts: int) -> float:
        return float(min(5 * 2 ** min(max(0, attempts - 1), 20), cls.MAX_BACKOFF_SECONDS))

    async def deliver_once(self) -> int:
        """One batch; returns the number of rows the backend accepted."""
        from sqlalchemy import delete, select, update
        from trajectory.store.database import trace_session
        from trajectory.store.models import TrajectoryAuditOutbox as Outbox

        claimed_at = now()
        async with trace_session() as db:
            due = (await db.scalars(select(Outbox.id).where(Outbox.next_attempt_at <= claimed_at)
                                    .order_by(Outbox.next_attempt_at, Outbox.id).limit(self.BATCH))).all()
            if not due:
                return 0
            rows = (await db.execute(
                update(Outbox).where(Outbox.id.in_(due), Outbox.next_attempt_at <= claimed_at)
                .values(next_attempt_at=claimed_at + timedelta(seconds=self.LEASE_SECONDS),
                        attempts=Outbox.attempts + 1)
                .returning(Outbox.id, Outbox.payload, Outbox.attempts)
                .execution_options(synchronize_session=False))).all()
        if not rows:
            return 0
        delivered, failed = await self._send(self._backend or get_backend(), rows)
        async with trace_session() as db:
            if delivered:
                await db.execute(delete(Outbox).where(Outbox.id.in_(delivered))
                                 .execution_options(synchronize_session=False))
            retry_from = now()
            for row_id, attempts in failed:
                await db.execute(update(Outbox).where(Outbox.id == row_id)
                                 .values(next_attempt_at=retry_from + timedelta(seconds=self.backoff(attempts)))
                                 .execution_options(synchronize_session=False))
        return len(delivered)

    async def _send(self, backend, rows) -> tuple[list[int], list[tuple[int, int]]]:
        try:
            await backend.audit([row.payload for row in rows])
            return [row.id for row in rows], []
        except AuditRejected as exc:
            rejected = exc
        except Exception as exc:
            log.warning("Trajectory audit delivery failed rows=%s error_type=%s", len(rows), type(exc).__name__)
            return [], [(row.id, row.attempts) for row in rows]
        if len(rows) == 1:
            log.warning("Trajectory audit entry refused id=%s reason=%s", rows[0].payload.get("id"), rejected)
            return [], [(rows[0].id, rows[0].attempts)]
        # One refused entry must not hold back the rest of its batch.
        delivered, failed = [], []
        for row in rows:
            try:
                await backend.audit([row.payload])
                delivered.append(row.id)
            except Exception as exc:
                log.warning("Trajectory audit entry not delivered id=%s error_type=%s",
                            row.payload.get("id"), type(exc).__name__)
                failed.append((row.id, row.attempts))
        return delivered, failed

    def start(self) -> asyncio.Task:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="trajectory-audit-delivery")
        return self._task

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        while True:
            try:
                delivered = await self.deliver_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Trajectory audit delivery pass failed error_type=%s", type(exc).__name__)
                delivered = 0
            if delivered < self.BATCH:
                await asyncio.sleep(self._interval)
