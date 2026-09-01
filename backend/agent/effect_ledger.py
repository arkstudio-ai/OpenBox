"""Fenced, durable protocol for external provider effects.

The protocol separates an Agent run fence from an effect-worker claim fence:

``prepare -> claim(dispatch) -> submitting -> provider -> receipt -> projection``

Only ``prepare`` and the final pre-send assertion require the exact live Agent
run.  Once ``submitting`` is durable, recovery may *query* an adapter under a
new effect claim, but it never invokes the original dispatch body.  This is the
important response-loss rule: absence of a local receipt is not evidence that
the provider did nothing.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import hashlib
import inspect
import json
import os
import secrets
import socket
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Literal, Protocol, TypeVar
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import func, or_, select, text, update

from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.external_effect import ExternalEffect, ExternalEffectEvidence


log = create_logger("agent.effect_ledger")

EFFECT_LEASE_SECONDS = 90
PREPARED_RECOVERY_GRACE_SECONDS = 120
RECONCILE_RETRY_SECONDS = 30
MAX_DISPATCH_ATTEMPTS = 1
MAX_RECONCILE_ATTEMPTS = 8
RECOVERY_BATCH_SIZE = 32
RECONCILE_TIMEOUT_SECONDS = 30
MAX_SAFE_JSON_BYTES = 16 * 1024
MAX_SAFE_STRING_CHARS = 2_000
MAX_SAFE_ITEMS = 64
MAX_SAFE_DEPTH = 6

EFFECT_OWNER_ID = (
    f"{socket.gethostname()}:{os.getpid()}:effect:{secrets.token_hex(6)}"
)

EffectState = Literal[
    "prepared",
    "submitting",
    "accepted",
    "succeeded",
    "failed",
    "outcome_unknown",
    "manual_review",
]
_TERMINAL_STATES = frozenset({"succeeded", "failed", "manual_review"})
_RECONCILE_STATES = frozenset({"submitting", "accepted", "outcome_unknown"})

_SENSITIVE_FIELD_PARTS = (
    "authorization",
    "api_key",
    "apikey",
    "password",
    "passwd",
    "secret",
    "credential",
    "access_token",
    "refresh_token",
    "provider_token",
    "bytedtoken",
    "cookie",
    "signed_url",
    "put_url",
    "get_url",
)
_URL_FIELD_NAMES = frozenset({"url", "uri", "endpoint", "base_url", "location"})
_UNTRUSTED_PROVIDER_TEXT_FIELDS = frozenset(
    {"body", "raw", "response", "response_body", "stack", "traceback"}
)


class EffectLedgerError(RuntimeError):
    """Base class for fail-closed ledger errors."""


class EffectConflictError(EffectLedgerError):
    """A stable effect identity was reused for different request bytes."""


class EffectLeaseLostError(EffectLedgerError):
    """The effect claim changed or expired before this worker committed."""


class EffectNotDispatchableError(EffectLedgerError):
    """An effect crossed the send boundary and must not be dispatched again."""


@dataclass(frozen=True, slots=True)
class EffectRunFence:
    session_id: str
    tenant_id: str
    run_id: str
    generation: int

    @classmethod
    def from_tool_context(cls, ctx: Any) -> "EffectRunFence":
        run_fence = getattr(ctx, "run_fence", None)
        if run_fence is None:
            raise EffectNotDispatchableError(
                "external effects require an exact Agent run fence"
            )
        session_id, run_id, generation = run_fence
        return cls(
            session_id=str(session_id),
            tenant_id=str(getattr(ctx, "user_id", "") or ""),
            run_id=str(run_id),
            generation=int(generation),
        )


@dataclass(frozen=True, slots=True)
class EffectSnapshot:
    effect_id: str
    tenant_id: str
    project_id: str | None
    session_id: str
    run_id: str
    run_generation: int
    adapter: str
    provider: str
    operation: str
    idempotency_key: str
    request_hash: str
    safe_context: dict[str, Any]
    state: EffectState
    attempt_count: int
    reconcile_count: int
    provider_handle: str | None
    provider_receipt: dict[str, Any] | None
    projection: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class PreparedEffect:
    snapshot: EffectSnapshot
    created: bool


@dataclass(frozen=True, slots=True)
class EffectClaim:
    effect_id: str
    tenant_id: str
    session_id: str
    run_id: str
    run_generation: int
    kind: Literal["dispatch", "reconcile"]
    token: str
    generation: int
    owner_id: str
    lease_expires_at: datetime


@dataclass(frozen=True, slots=True)
class ReconcileDecision:
    """Safe result of querying provider/domain state; never a dispatch order."""

    state: Literal[
        "accepted", "succeeded", "failed", "outcome_unknown", "manual_review"
    ]
    provider_handle: str | None = None
    receipt: dict[str, Any] | None = None
    projection: dict[str, Any] | None = None
    evidence: dict[str, Any] | None = None
    retry_after_seconds: int = RECONCILE_RETRY_SECONDS


class EffectReconciler(Protocol):
    can_reconcile_without_handle: bool

    async def reconcile(self, effect: EffectSnapshot) -> ReconcileDecision:
        """Query a provider or deterministic domain receipt without resending."""


@dataclass(frozen=True, slots=True)
class EffectRecoveryResult:
    scanned: int = 0
    reconciled: int = 0
    deferred: int = 0
    manual_review: int = 0
    failed_before_dispatch: int = 0
    stale_skips: int = 0

    @property
    def changed(self) -> bool:
        return any(
            (
                self.reconciled,
                self.deferred,
                self.manual_review,
                self.failed_before_dispatch,
            )
        )


ProjectionCallback = Callable[[Any, ExternalEffect, dict[str, Any]], Any]

_reconcilers: dict[str, EffectReconciler] = {}


def register_effect_reconciler(adapter: str, reconciler: EffectReconciler) -> None:
    """Register one process-local query adapter before recovery starts."""
    name = _bounded_identity(adapter, "adapter", 64)
    existing = _reconcilers.get(name)
    if existing is not None and existing is not reconciler:
        raise ValueError(f"external-effect reconciler already registered: {name}")
    _reconcilers[name] = reconciler


def unregister_effect_reconciler(adapter: str, reconciler: EffectReconciler) -> None:
    if _reconcilers.get(adapter) is reconciler:
        _reconcilers.pop(adapter, None)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _database_now(db):
    if db.get_bind().dialect.name == "postgresql":
        return func.clock_timestamp()
    return func.current_timestamp()


def _database_expiry(db, seconds: int | None = None):
    if seconds is None:
        seconds = EFFECT_LEASE_SECONDS
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return func.clock_timestamp() + text(f"INTERVAL '{int(seconds)} seconds'")
    if dialect == "sqlite":
        return func.datetime("now", f"+{int(seconds)} seconds")
    return _database_now(db) + timedelta(seconds=seconds)


def _database_before(db, seconds: int):
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return func.clock_timestamp() - text(f"INTERVAL '{int(seconds)} seconds'")
    if dialect == "sqlite":
        return func.datetime("now", f"-{int(seconds)} seconds")
    return _database_now(db) - timedelta(seconds=seconds)


async def _read_database_now(db) -> datetime:
    result = await db.execute(select(_database_now(db)))
    value = result.scalar_one()
    result.close()
    return _aware(value)


def _bounded_identity(value: str, label: str, maximum: int) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{label} must contain 1..{maximum} characters")
    if any(ord(char) < 32 for char in normalized):
        raise ValueError(f"{label} contains a control character")
    return normalized


def _canonical_request_value(value: Any, *, depth: int = 0) -> Any:
    """Return deterministic hash input without ever storing it in the ledger."""
    if depth > 32:
        raise ValueError("external-effect request nesting is too deep")
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {
            "$bytes_sha256": hashlib.sha256(value).hexdigest(),
            "$bytes_size": len(value),
        }
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if hasattr(value, "model_dump"):
        return _canonical_request_value(value.model_dump(mode="json"), depth=depth + 1)
    if is_dataclass(value):
        return _canonical_request_value(asdict(value), depth=depth + 1)
    if isinstance(value, dict):
        return {
            str(key): _canonical_request_value(item, depth=depth + 1)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [
            _canonical_request_value(item, depth=depth + 1) for item in value
        ]
    raise TypeError(f"unsupported external-effect request type: {type(value).__name__}")


def request_hash(payload: Any) -> str:
    canonical = _canonical_request_value(payload)
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def stable_effect_id(
    *,
    tenant_id: str,
    session_id: str,
    adapter: str,
    operation: str,
    logical_key: str,
) -> str:
    """Derive an identity that intentionally excludes the Agent generation."""
    raw = "\x1f".join(
        (
            "openbox-effect-v1",
            tenant_id,
            session_id,
            adapter,
            operation,
            logical_key,
        )
    )
    return "effect_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def stable_idempotency_key(effect_id: str) -> str:
    digest = hashlib.sha256(
        f"openbox-provider-effect-v1:{effect_id}".encode("utf-8")
    ).hexdigest()
    return f"openbox-effect-{digest[:48]}"


def _validate_idempotency_key(value: str) -> str:
    key = _bounded_identity(value, "idempotency key", 160)
    lowered = key.casefold()
    if (
        "://" in key
        or "?" in key
        or any(char.isspace() for char in key)
        or lowered.startswith(("bearer", "basic", "sk-"))
    ):
        raise ValueError("idempotency key looks like authorization material")
    return key


def _is_sensitive_field(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return any(fragment in normalized for fragment in _SENSITIVE_FIELD_PARTS)


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except Exception:
        return "[redacted-url]"
    if not parsed.scheme or not parsed.netloc:
        return value[:MAX_SAFE_STRING_CHARS]
    # Query strings and fragments routinely carry OSS/provider credentials.
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))[
        :MAX_SAFE_STRING_CHARS
    ]


def _sanitize_value(value: Any, *, field: str = "", depth: int = 0) -> Any:
    if _is_sensitive_field(field):
        return "[redacted]"
    if field.casefold().replace("-", "_") in _UNTRUSTED_PROVIDER_TEXT_FIELDS:
        # Provider response bodies can echo prompts, credentials and signed
        # URLs. Their class/status belongs in a separate safe field; raw text
        # never belongs in this public-safe audit surface.
        return "[omitted-provider-text]"
    if depth >= MAX_SAFE_DEPTH:
        return {"truncated": True, "reason": "max_depth"}
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, bytes):
        return {
            "bytes_sha256": hashlib.sha256(value).hexdigest(),
            "bytes_size": len(value),
        }
    if isinstance(value, str):
        if field.casefold().replace("-", "_") in _URL_FIELD_NAMES:
            return _safe_url(value)
        return value[:MAX_SAFE_STRING_CHARS]
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    elif is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_SAFE_ITEMS:
                result["_truncated_items"] = len(value) - MAX_SAFE_ITEMS
                break
            name = str(key)[:128]
            result[name] = _sanitize_value(item, field=name, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        result = [
            _sanitize_value(item, field=field, depth=depth + 1)
            for item in items[:MAX_SAFE_ITEMS]
        ]
        if len(items) > MAX_SAFE_ITEMS:
            result.append({"truncated_items": len(items) - MAX_SAFE_ITEMS})
        return result
    return {"type": type(value).__name__}


def sanitize_public_evidence(value: Any) -> dict[str, Any]:
    """Return bounded JSON using field-aware redaction, never value scanning.

    Ordinary Unicode strings, including documentation examples containing
    names such as ``api_key`` or ``sk-...``, remain exact when their *field* is
    not credential-bearing.  This avoids corrupting user text while preventing
    structured authorization fields from entering the audit log.
    """
    sanitized = _sanitize_value(value)
    if not isinstance(sanitized, dict):
        sanitized = {"value": sanitized}
    encoded = json.dumps(
        sanitized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(encoded) <= MAX_SAFE_JSON_BYTES:
        return sanitized
    return {
        "truncated": True,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "bytes": len(encoded),
        "keys": sorted(str(key)[:128] for key in sanitized)[:MAX_SAFE_ITEMS],
    }


def _safe_error(error: Any) -> dict[str, Any]:
    if isinstance(error, BaseException):
        return {"error_type": type(error).__name__}
    if isinstance(error, str):
        return sanitize_public_evidence({"code": error})
    return sanitize_public_evidence(error or {"code": "external_effect_error"})


def _validate_provider_handle(handle: str | None) -> str | None:
    if handle is None:
        return None
    value = str(handle).strip()
    if not value:
        return None
    if len(value) > 256:
        raise ValueError("provider handle exceeds 256 characters")
    lowered = value.casefold()
    if (
        "://" in value
        or "?" in value
        or any(char.isspace() for char in value)
        or lowered.startswith(("bearer", "basic", "sk-"))
    ):
        raise ValueError("provider handle looks like authorization material")
    return value


def _snapshot(row: ExternalEffect) -> EffectSnapshot:
    return EffectSnapshot(
        effect_id=row.id,
        tenant_id=row.tenant_id,
        project_id=row.project_id,
        session_id=row.session_id,
        run_id=row.run_id,
        run_generation=row.run_generation,
        adapter=row.adapter,
        provider=row.provider,
        operation=row.operation,
        idempotency_key=row.idempotency_key,
        request_hash=row.request_hash,
        safe_context=dict(row.safe_context or {}),
        state=row.state,  # type: ignore[arg-type]
        attempt_count=int(row.attempt_count or 0),
        reconcile_count=int(row.reconcile_count or 0),
        provider_handle=row.provider_handle,
        provider_receipt=(
            dict(row.provider_receipt) if isinstance(row.provider_receipt, dict) else None
        ),
        projection=dict(row.projection) if isinstance(row.projection, dict) else None,
    )


async def _append_evidence(
    db,
    row: ExternalEffect,
    *,
    phase: str,
    evidence: Any,
    now: datetime,
    claim_generation: int | None = None,
) -> None:
    result = await db.execute(
        select(func.coalesce(func.max(ExternalEffectEvidence.sequence), 0)).where(
            ExternalEffectEvidence.effect_id == row.id
        )
    )
    sequence = int(result.scalar_one()) + 1
    result.close()
    db.add(
        ExternalEffectEvidence(
            id=ascending("effectev"),
            effect_id=row.id,
            sequence=sequence,
            claim_generation=(
                row.claim_generation
                if claim_generation is None
                else int(claim_generation)
            ),
            phase=_bounded_identity(phase, "effect evidence phase", 32),
            evidence=sanitize_public_evidence(evidence),
            created_at=now,
        )
    )


async def _assert_agent_fence_locked(db, fence: EffectRunFence) -> None:
    from agent.driver import assert_run_fence_locked

    await assert_run_fence_locked(
        db,
        session_id=fence.session_id,
        user_id=fence.tenant_id,
        run_id=fence.run_id,
        generation=fence.generation,
    )


def _claim_matches(claim: EffectClaim):
    return (
        ExternalEffect.id == claim.effect_id,
        ExternalEffect.tenant_id == claim.tenant_id,
        ExternalEffect.session_id == claim.session_id,
        ExternalEffect.claim_kind == claim.kind,
        ExternalEffect.claim_token == claim.token,
        ExternalEffect.claim_generation == claim.generation,
        ExternalEffect.claim_owner == claim.owner_id,
        ExternalEffect.claim_expires_at.is_not(None),
    )


def _dispatch_claim_statement(
    db,
    *,
    effect_id: str,
    fence: EffectRunFence,
    token: str,
    owner_id: str,
):
    """Build the dispatch CAS with caller identities kept as SQL binds.

    Keeping this statement construction isolated makes the PostgreSQL and
    SQLite forms directly compilable in tests.  Only the fixed, internal
    lease duration is rendered into a dialect-specific interval; effect,
    tenant, run, token, and owner values remain driver parameters.
    """
    now = _database_now(db)
    return (
        update(ExternalEffect)
        .where(
            ExternalEffect.id == effect_id,
            ExternalEffect.tenant_id == fence.tenant_id,
            ExternalEffect.session_id == fence.session_id,
            ExternalEffect.run_id == fence.run_id,
            ExternalEffect.run_generation == fence.generation,
            ExternalEffect.state == "prepared",
            ExternalEffect.attempt_count < MAX_DISPATCH_ATTEMPTS,
            or_(
                ExternalEffect.claim_token.is_(None),
                ExternalEffect.claim_expires_at <= now,
            ),
        )
        .values(
            claim_generation=ExternalEffect.claim_generation + 1,
            claim_kind="dispatch",
            claim_token=token,
            claim_owner=owner_id,
            claim_expires_at=_database_expiry(db),
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )


async def prepare_effect(
    fence: EffectRunFence,
    *,
    adapter: str,
    provider: str,
    operation: str,
    logical_key: str,
    request_payload: Any | None = None,
    request_digest: str | None = None,
    project_id: str | None = None,
    idempotency_key: str | None = None,
    safe_context: dict[str, Any] | None = None,
) -> PreparedEffect:
    """Persist intent under the exact live Agent run before any provider call."""
    adapter = _bounded_identity(adapter, "adapter", 64)
    provider = _bounded_identity(provider, "provider", 64)
    operation = _bounded_identity(operation, "operation", 64)
    logical_key = _bounded_identity(logical_key, "logical key", 512)
    if not fence.tenant_id or not fence.session_id or not fence.run_id:
        raise ValueError("effect run fence is incomplete")
    if fence.generation <= 0:
        raise ValueError("effect run generation must be positive")
    if (request_payload is None) == (request_digest is None):
        raise ValueError("provide exactly one of request_payload or request_digest")
    digest = request_digest or request_hash(request_payload)
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise ValueError("request digest must be lowercase SHA-256")
    effect_id = stable_effect_id(
        tenant_id=fence.tenant_id,
        session_id=fence.session_id,
        adapter=adapter,
        operation=operation,
        logical_key=logical_key,
    )
    provider_key = _validate_idempotency_key(
        idempotency_key or stable_idempotency_key(effect_id),
    )
    context = sanitize_public_evidence(safe_context or {})

    async with get_db_session() as db:
        await _assert_agent_fence_locked(db, fence)
        from db.models.session import Session as SessionRow

        scope_result = await db.execute(
            select(SessionRow.project_id).where(
                SessionRow.id == fence.session_id,
                SessionRow.user_id == fence.tenant_id,
                SessionRow.is_deleted.is_(False),
            )
        )
        actual_project_id = scope_result.scalar_one_or_none()
        scope_result.close()
        if actual_project_id is None:
            raise EffectNotDispatchableError("effect session/project scope is unavailable")
        if project_id is not None and project_id != actual_project_id:
            raise EffectConflictError("effect project does not own the fenced session")
        project_id = actual_project_id
        query = select(ExternalEffect).where(ExternalEffect.id == effect_id)
        if db.get_bind().dialect.name == "postgresql":
            query = query.with_for_update()
        row = (await db.execute(query)).scalar_one_or_none()
        now = await _read_database_now(db)
        if row is not None:
            expected = (
                row.tenant_id == fence.tenant_id
                and row.session_id == fence.session_id
                and row.project_id in {None, project_id}
                and row.adapter == adapter
                and row.provider == provider
                and row.operation == operation
                and row.idempotency_key == provider_key
                and row.request_hash == digest
            )
            if not expected:
                raise EffectConflictError(
                    "stable effect identity conflicts with its durable request"
                )
            if row.project_id is None:
                row.project_id = project_id
            # A *prepared* row with no recorded send may safely move to a new
            # Agent generation.  Every later state remains bound to the origin
            # generation and can only be queried/reconciled.
            if (
                row.state == "prepared"
                and row.attempt_count == 0
                and row.claim_token is None
                and (row.run_id != fence.run_id or row.run_generation != fence.generation)
            ):
                row.run_id = fence.run_id
                row.run_generation = fence.generation
                row.project_id = project_id or row.project_id
                row.safe_context = context
                row.updated_at = now
                await _append_evidence(
                    db,
                    row,
                    phase="reprepared",
                    evidence={"reason": "proven_before_dispatch"},
                    now=now,
                )
            return PreparedEffect(_snapshot(row), created=False)

        row = ExternalEffect(
            id=effect_id,
            tenant_id=fence.tenant_id,
            project_id=project_id or None,
            session_id=fence.session_id,
            run_id=fence.run_id,
            run_generation=fence.generation,
            adapter=adapter,
            provider=provider,
            operation=operation,
            idempotency_key=provider_key,
            request_hash=digest,
            safe_context=context,
            state="prepared",
            attempt_count=0,
            reconcile_count=0,
            claim_generation=0,
            claim_kind=None,
            claim_token=None,
            claim_owner=None,
            claim_expires_at=None,
            provider_handle=None,
            provider_receipt=None,
            projection=None,
            last_error=None,
            reconcile_after=None,
            prepared_at=now,
            submitting_at=None,
            accepted_at=None,
            completed_at=None,
            created_at=now,
            updated_at=now,
        )
        db.add(row)
        await db.flush()
        await _append_evidence(
            db,
            row,
            phase="prepared",
            evidence={
                "adapter": adapter,
                "provider": provider,
                "operation": operation,
                "request_hash": digest,
            },
            now=now,
            claim_generation=0,
        )
        return PreparedEffect(_snapshot(row), created=True)


async def get_effect(effect_id: str, tenant_id: str | None = None) -> EffectSnapshot | None:
    async with get_db_session() as db:
        conditions = [ExternalEffect.id == effect_id]
        if tenant_id is not None:
            conditions.append(ExternalEffect.tenant_id == tenant_id)
        row = (
            await db.execute(select(ExternalEffect).where(*conditions))
        ).scalar_one_or_none()
        return _snapshot(row) if row is not None else None


async def list_effect_evidence(
    effect_id: str, tenant_id: str | None = None
) -> list[dict[str, Any]]:
    async with get_db_session() as db:
        query = (
            select(ExternalEffectEvidence)
            .join(ExternalEffect, ExternalEffect.id == ExternalEffectEvidence.effect_id)
            .where(ExternalEffectEvidence.effect_id == effect_id)
            .order_by(ExternalEffectEvidence.sequence)
        )
        if tenant_id is not None:
            query = query.where(ExternalEffect.tenant_id == tenant_id)
        result = await db.execute(query)
        rows = list(result.scalars())
        result.close()
        return [
            {
                "sequence": row.sequence,
                "claim_generation": row.claim_generation,
                "phase": row.phase,
                "evidence": dict(row.evidence or {}),
                "created_at": _aware(row.created_at).isoformat(),
            }
            for row in rows
        ]


async def claim_effect_for_dispatch(
    effect_id: str,
    fence: EffectRunFence,
    *,
    owner_id: str = EFFECT_OWNER_ID,
) -> EffectClaim:
    """Claim the one allowed dispatch attempt under the exact Agent fence."""
    token = secrets.token_hex(24)
    async with get_db_session() as db:
        await _assert_agent_fence_locked(db, fence)
        result = await db.execute(
            _dispatch_claim_statement(
                db,
                effect_id=effect_id,
                fence=fence,
                token=token,
                owner_id=owner_id,
            )
        )
        matched = result.rowcount == 1
        result.close()
        if not matched:
            raise EffectNotDispatchableError(
                "external effect is not in its exact prepared generation"
            )
        row = (
            await db.execute(
                select(ExternalEffect).where(ExternalEffect.id == effect_id)
            )
        ).scalar_one()
        clock = await _read_database_now(db)
        await _append_evidence(
            db,
            row,
            phase="claim_acquired",
            evidence={
                "kind": "dispatch",
                "owner_digest": hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:16],
            },
            now=clock,
        )
        return EffectClaim(
            effect_id=row.id,
            tenant_id=row.tenant_id,
            session_id=row.session_id,
            run_id=row.run_id,
            run_generation=row.run_generation,
            kind="dispatch",
            token=token,
            generation=row.claim_generation,
            owner_id=owner_id,
            lease_expires_at=_aware(row.claim_expires_at),
        )


async def mark_effect_submitting(claim: EffectClaim) -> None:
    """Commit the irreversible send boundary before entering provider code."""
    if claim.kind != "dispatch":
        raise EffectLeaseLostError("a reconcile claim cannot dispatch")
    async with get_db_session() as db:
        now = _database_now(db)
        result = await db.execute(
            update(ExternalEffect)
            .where(
                *_claim_matches(claim),
                ExternalEffect.state == "prepared",
                ExternalEffect.claim_expires_at > now,
            )
            .values(
                state="submitting",
                attempt_count=ExternalEffect.attempt_count + 1,
                submitting_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        matched = result.rowcount == 1
        result.close()
        if not matched:
            raise EffectLeaseLostError("effect dispatch claim was lost before submit")
        row = (
            await db.execute(
                select(ExternalEffect).where(ExternalEffect.id == claim.effect_id)
            )
        ).scalar_one()
        clock = await _read_database_now(db)
        await _append_evidence(
            db,
            row,
            phase="submitting",
            evidence={"attempt": row.attempt_count},
            now=clock,
        )


async def abandon_effect_before_dispatch(claim: EffectClaim, *, reason: str) -> None:
    """Release a claim only while the durable state proves no send began."""
    async with get_db_session() as db:
        now = _database_now(db)
        result = await db.execute(
            update(ExternalEffect)
            .where(
                *_claim_matches(claim),
                ExternalEffect.state == "prepared",
                ExternalEffect.attempt_count == 0,
                ExternalEffect.claim_expires_at > now,
            )
            .values(
                claim_kind=None,
                claim_token=None,
                claim_owner=None,
                claim_expires_at=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        matched = result.rowcount == 1
        result.close()
        if not matched:
            raise EffectLeaseLostError("effect already crossed its send boundary")
        row = (
            await db.execute(
                select(ExternalEffect).where(ExternalEffect.id == claim.effect_id)
            )
        ).scalar_one()
        await _append_evidence(
            db,
            row,
            phase="before_dispatch_abandoned",
            evidence={"reason": reason},
            now=await _read_database_now(db),
            claim_generation=claim.generation,
        )


async def assert_effect_dispatchable(claim: EffectClaim) -> None:
    """Last durable guard; call immediately before the external request."""
    if claim.kind != "dispatch":
        raise EffectLeaseLostError("recovery workers may reconcile but never dispatch")
    fence = EffectRunFence(
        session_id=claim.session_id,
        tenant_id=claim.tenant_id,
        run_id=claim.run_id,
        generation=claim.run_generation,
    )
    async with get_db_session() as db:
        await _assert_agent_fence_locked(db, fence)
        now = _database_now(db)
        result = await db.execute(
            update(ExternalEffect)
            .where(
                *_claim_matches(claim),
                ExternalEffect.state == "submitting",
                ExternalEffect.claim_expires_at > now,
            )
            .values(updated_at=now)
            .execution_options(synchronize_session=False)
        )
        matched = result.rowcount == 1
        result.close()
        if not matched:
            raise EffectLeaseLostError("effect lease was lost before provider dispatch")


async def renew_effect_claim(claim: EffectClaim) -> EffectClaim:
    async with get_db_session() as db:
        now = _database_now(db)
        result = await db.execute(
            update(ExternalEffect)
            .where(
                *_claim_matches(claim),
                ExternalEffect.state.not_in(tuple(_TERMINAL_STATES)),
                ExternalEffect.claim_expires_at > now,
            )
            .values(claim_expires_at=_database_expiry(db), updated_at=now)
            .returning(ExternalEffect.claim_expires_at)
            .execution_options(synchronize_session=False)
        )
        expiry = result.scalar_one_or_none()
        result.close()
        if expiry is None:
            raise EffectLeaseLostError("external-effect claim renewal was fenced out")
        return EffectClaim(
            effect_id=claim.effect_id,
            tenant_id=claim.tenant_id,
            session_id=claim.session_id,
            run_id=claim.run_id,
            run_generation=claim.run_generation,
            kind=claim.kind,
            token=claim.token,
            generation=claim.generation,
            owner_id=claim.owner_id,
            lease_expires_at=_aware(expiry),
        )


_EffectResult = TypeVar("_EffectResult")


async def run_with_effect_claim_heartbeat(
    claim: EffectClaim,
    operation: Awaitable[_EffectResult],
    *,
    heartbeat_interval_seconds: float | None = None,
) -> _EffectResult:
    """Run one already-dispatched provider operation under a live claim.

    Provider timeouts can be much longer than the effect claim TTL.  The
    heartbeat is deliberately coupled to the awaited operation: if renewal is
    fenced out, the local provider task is canceled and its result is never
    projected by the stale owner.  Recovery still treats the durable
    ``submitting`` state conservatively and never resends the paid request.
    """
    interval = (
        heartbeat_interval_seconds
        if heartbeat_interval_seconds is not None
        else max(0.1, EFFECT_LEASE_SECONDS / 3)
    )
    if not isinstance(interval, (int, float)) or interval <= 0:
        raise ValueError("effect heartbeat interval must be positive")

    operation_task = asyncio.ensure_future(operation)

    async def heartbeat() -> None:
        current = claim
        while True:
            await asyncio.sleep(float(interval))
            current = await renew_effect_claim(current)

    heartbeat_task = asyncio.create_task(
        heartbeat(),
        name=f"effect-heartbeat:{claim.effect_id}:{claim.generation}",
    )
    try:
        done, _pending = await asyncio.wait(
            {operation_task, heartbeat_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if heartbeat_task in done:
            # A heartbeat only terminates on cancellation or failure.  Treat a
            # renewal failure as authority loss even if the provider response
            # raced it; the stale worker must not publish that response.
            heartbeat_error = heartbeat_task.exception()
            operation_task.cancel()
            with suppress(BaseException):
                await operation_task
            if heartbeat_error is None:
                raise EffectLeaseLostError("external-effect heartbeat stopped")
            raise heartbeat_error

        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
        return await operation_task
    except BaseException:
        operation_task.cancel()
        heartbeat_task.cancel()
        with suppress(BaseException):
            await operation_task
        with suppress(BaseException):
            await heartbeat_task
        raise


async def record_effect_accepted(
    claim: EffectClaim,
    *,
    provider_handle: str | None,
    receipt: dict[str, Any],
) -> EffectSnapshot:
    """Persist a provider receipt with the exact claim that observed it."""
    handle = _validate_provider_handle(provider_handle)
    safe_receipt = sanitize_public_evidence(receipt)
    async with get_db_session() as db:
        now = _database_now(db)
        result = await db.execute(
            update(ExternalEffect)
            .where(
                *_claim_matches(claim),
                ExternalEffect.state == "submitting",
                ExternalEffect.claim_expires_at > now,
            )
            .values(
                state="accepted",
                provider_handle=handle,
                provider_receipt=safe_receipt,
                accepted_at=now,
                reconcile_after=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        matched = result.rowcount == 1
        result.close()
        if not matched:
            # Idempotent retry after an unknown commit result is safe only when
            # the exact receipt and claim are already durable.
            row = (
                await db.execute(
                    select(ExternalEffect).where(
                        ExternalEffect.id == claim.effect_id,
                        ExternalEffect.claim_token == claim.token,
                        ExternalEffect.claim_generation == claim.generation,
                    )
                )
            ).scalar_one_or_none()
            if not (
                row is not None
                and row.state == "accepted"
                and row.provider_handle == handle
                and row.provider_receipt == safe_receipt
            ):
                raise EffectLeaseLostError("provider receipt was fenced out")
            return _snapshot(row)
        row = (
            await db.execute(
                select(ExternalEffect).where(ExternalEffect.id == claim.effect_id)
            )
        ).scalar_one()
        await _append_evidence(
            db,
            row,
            phase="accepted",
            evidence={
                "provider_handle_present": bool(handle),
                "receipt": safe_receipt,
            },
            now=await _read_database_now(db),
        )
        return _snapshot(row)


async def _invoke_projection(
    projector: ProjectionCallback | None,
    db,
    row: ExternalEffect,
    projection: dict[str, Any],
) -> None:
    if projector is None:
        return
    result = projector(db, row, projection)
    if inspect.isawaitable(result):
        await result


async def settle_effect(
    claim: EffectClaim,
    *,
    state: Literal["succeeded", "failed", "manual_review"],
    projection: dict[str, Any] | None = None,
    error: Any = None,
    provider_handle: str | None = None,
    receipt: dict[str, Any] | None = None,
    evidence: dict[str, Any] | None = None,
    projector: ProjectionCallback | None = None,
) -> EffectSnapshot:
    """Atomically write terminal receipt/projection under the exact claim."""
    if claim.kind == "reconcile" and projector is not None:
        raise EffectNotDispatchableError(
            "recovery claims cannot run an unfenced domain projector"
        )
    safe_projection = sanitize_public_evidence(projection or {})
    safe_error = _safe_error(error) if error is not None else None
    safe_receipt = sanitize_public_evidence(receipt) if receipt is not None else None
    handle = _validate_provider_handle(provider_handle)
    async with get_db_session() as db:
        if claim.kind == "dispatch":
            # A dispatch worker may publish a product/domain projection only
            # while its originating Agent generation is still exact and live.
            # Recovery claims can settle the isolated ledger projection, but
            # never receive an arbitrary projector callback.
            await _assert_agent_fence_locked(
                db,
                EffectRunFence(
                    session_id=claim.session_id,
                    tenant_id=claim.tenant_id,
                    run_id=claim.run_id,
                    generation=claim.run_generation,
                ),
            )
        now_expr = _database_now(db)
        # A conditional no-op UPDATE is both the exact CAS and SQLite's write
        # lock.  Without it, an awaited projector could let a takeover replace
        # the claim after SELECT and the ORM's primary-key UPDATE would allow
        # the old worker to publish a terminal projection.
        gate = await db.execute(
            update(ExternalEffect)
            .where(
                *_claim_matches(claim),
                ExternalEffect.state.not_in(tuple(_TERMINAL_STATES)),
                ExternalEffect.claim_expires_at > now_expr,
            )
            .values(claim_generation=ExternalEffect.claim_generation)
            .execution_options(synchronize_session=False)
        )
        matched = gate.rowcount == 1
        gate.close()
        if not matched:
            raise EffectLeaseLostError("terminal effect projection was fenced out")
        query = select(ExternalEffect).where(
            *_claim_matches(claim),
            ExternalEffect.state.not_in(tuple(_TERMINAL_STATES)),
            ExternalEffect.claim_expires_at > now_expr,
        )
        if db.get_bind().dialect.name == "postgresql":
            query = query.with_for_update()
        row = (await db.execute(query)).scalar_one_or_none()
        if row is None:  # defensive: the no-op CAS above already held the row
            raise EffectLeaseLostError("terminal effect projection disappeared")
        now = await _read_database_now(db)
        if receipt is not None:
            row.provider_receipt = safe_receipt
        if handle is not None:
            row.provider_handle = handle
        await _invoke_projection(projector, db, row, safe_projection)
        row.state = state
        row.projection = safe_projection if state == "succeeded" else row.projection
        row.last_error = safe_error
        row.completed_at = now
        row.reconcile_after = None
        row.claim_kind = None
        row.claim_token = None
        row.claim_owner = None
        row.claim_expires_at = None
        row.updated_at = now
        await _append_evidence(
            db,
            row,
            phase=state,
            evidence={
                **(evidence or {}),
                "projection": safe_projection if state == "succeeded" else {},
                "error": safe_error,
            },
            now=now,
            claim_generation=claim.generation,
        )
        await db.flush()
        return _snapshot(row)


async def record_effect_outcome_unknown(
    claim: EffectClaim,
    *,
    error: Any,
    provider_handle: str | None = None,
    receipt: dict[str, Any] | None = None,
) -> EffectSnapshot:
    """Close the dispatch claim without making a second send eligible."""
    handle = _validate_provider_handle(provider_handle)
    safe_receipt = sanitize_public_evidence(receipt) if receipt is not None else None
    safe_error = _safe_error(error)
    async with get_db_session() as db:
        now = _database_now(db)
        values: dict[str, Any] = {
            "state": "outcome_unknown",
            "last_error": safe_error,
            "reconcile_after": now,
            "claim_kind": None,
            "claim_token": None,
            "claim_owner": None,
            "claim_expires_at": None,
            "updated_at": now,
        }
        if handle is not None:
            values["provider_handle"] = handle
        if safe_receipt is not None:
            values["provider_receipt"] = safe_receipt
        result = await db.execute(
            update(ExternalEffect)
            .where(
                *_claim_matches(claim),
                ExternalEffect.state.in_(("submitting", "accepted")),
                ExternalEffect.claim_expires_at > now,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        matched = result.rowcount == 1
        result.close()
        if not matched:
            raise EffectLeaseLostError("unknown provider outcome was fenced out")
        row = (
            await db.execute(
                select(ExternalEffect).where(ExternalEffect.id == claim.effect_id)
            )
        ).scalar_one()
        await _append_evidence(
            db,
            row,
            phase="outcome_unknown",
            evidence={
                "provider_handle_present": bool(handle),
                "error": safe_error,
            },
            now=await _read_database_now(db),
            claim_generation=claim.generation,
        )
        return _snapshot(row)


async def _origin_run_is_live(db, row: ExternalEffect) -> bool:
    result = await db.execute(
        select(AgentDriverState.session_id)
        .where(
            AgentDriverState.session_id == row.session_id,
            AgentDriverState.user_id == row.tenant_id,
            AgentDriverState.run_id == row.run_id,
            AgentDriverState.generation == row.run_generation,
            AgentDriverState.phase != "idle",
            AgentDriverState.lease_expires_at.is_not(None),
            AgentDriverState.lease_expires_at > _database_now(db),
        )
        .limit(1)
    )
    live = result.scalar_one_or_none() is not None
    result.close()
    return live


async def _claim_for_reconcile(effect_id: str) -> EffectClaim | None:
    token = secrets.token_hex(24)
    async with get_db_session() as db:
        query = select(ExternalEffect).where(
            ExternalEffect.id == effect_id,
            ExternalEffect.state.not_in(tuple(_TERMINAL_STATES)),
        )
        if db.get_bind().dialect.name == "postgresql":
            query = query.with_for_update()
        row = (await db.execute(query)).scalar_one_or_none()
        if row is None:
            return None
        now = await _read_database_now(db)
        expiry = row.claim_expires_at
        if row.claim_token is not None and expiry is not None and _aware(expiry) > now:
            return None
        if row.reconcile_after is not None and _aware(row.reconcile_after) > now:
            return None
        if await _origin_run_is_live(db, row):
            return None
        if row.state == "prepared":
            if _aware(row.prepared_at) > now - timedelta(
                seconds=PREPARED_RECOVERY_GRACE_SECONDS
            ):
                return None
        elif row.state not in _RECONCILE_STATES:
            return None
        if row.reconcile_count >= MAX_RECONCILE_ATTEMPTS:
            # Claim it once more so the ordinary fenced settlement path can
            # quarantine it without an unfenced UPDATE.
            pass
        old_generation = row.claim_generation
        result = await db.execute(
            update(ExternalEffect)
            .where(
                ExternalEffect.id == row.id,
                ExternalEffect.claim_generation == old_generation,
                ExternalEffect.state == row.state,
                or_(
                    ExternalEffect.claim_token.is_(None),
                    ExternalEffect.claim_expires_at <= _database_now(db),
                ),
            )
            .values(
                claim_generation=old_generation + 1,
                claim_kind="reconcile",
                claim_token=token,
                claim_owner=EFFECT_OWNER_ID,
                claim_expires_at=_database_expiry(db),
                reconcile_count=ExternalEffect.reconcile_count + 1,
                updated_at=_database_now(db),
            )
            .execution_options(synchronize_session=False)
        )
        matched = result.rowcount == 1
        result.close()
        if not matched:
            return None
        # ``row`` was already present in this Session's identity map before
        # the Core UPDATE. Refresh it explicitly; otherwise SQLite tests (and
        # any expire_on_commit=False worker) would return the old claim token.
        await db.refresh(row)
        clock = await _read_database_now(db)
        await _append_evidence(
            db,
            row,
            phase="reconcile_claimed",
            evidence={"attempt": row.reconcile_count},
            now=clock,
        )
        return EffectClaim(
            effect_id=row.id,
            tenant_id=row.tenant_id,
            session_id=row.session_id,
            run_id=row.run_id,
            run_generation=row.run_generation,
            kind="reconcile",
            token=token,
            generation=row.claim_generation,
            owner_id=EFFECT_OWNER_ID,
            lease_expires_at=_aware(row.claim_expires_at),
        )


async def _defer_reconcile(
    claim: EffectClaim,
    decision: ReconcileDecision | None,
    *,
    error: Any = None,
) -> EffectSnapshot:
    safe_error = _safe_error(error) if error is not None else None
    safe_receipt = (
        sanitize_public_evidence(decision.receipt)
        if decision is not None and decision.receipt is not None
        else None
    )
    handle = _validate_provider_handle(
        decision.provider_handle if decision is not None else None
    )
    delay = max(
        1,
        min(
            3600,
            int(
                decision.retry_after_seconds
                if decision is not None
                else RECONCILE_RETRY_SECONDS
            ),
        ),
    )
    next_state = (
        decision.state
        if decision is not None and decision.state in {"accepted", "outcome_unknown"}
        else "outcome_unknown"
    )
    async with get_db_session() as db:
        now = _database_now(db)
        values: dict[str, Any] = {
            "state": next_state,
            "last_error": safe_error,
            "reconcile_after": (
                func.clock_timestamp() + text(f"INTERVAL '{delay} seconds'")
                if db.get_bind().dialect.name == "postgresql"
                else (
                    func.datetime("now", f"+{delay} seconds")
                    if db.get_bind().dialect.name == "sqlite"
                    else _database_now(db) + timedelta(seconds=delay)
                )
            ),
            "claim_kind": None,
            "claim_token": None,
            "claim_owner": None,
            "claim_expires_at": None,
            "updated_at": now,
        }
        if safe_receipt is not None:
            values["provider_receipt"] = safe_receipt
        if handle is not None:
            values["provider_handle"] = handle
        result = await db.execute(
            update(ExternalEffect)
            .where(
                *_claim_matches(claim),
                ExternalEffect.claim_expires_at > now,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        matched = result.rowcount == 1
        result.close()
        if not matched:
            raise EffectLeaseLostError("reconcile deferral was fenced out")
        row = (
            await db.execute(
                select(ExternalEffect).where(ExternalEffect.id == claim.effect_id)
            )
        ).scalar_one()
        await _append_evidence(
            db,
            row,
            phase="reconcile_deferred",
            evidence={
                "state": next_state,
                "error": safe_error,
                **((decision.evidence or {}) if decision is not None else {}),
            },
            now=await _read_database_now(db),
            claim_generation=claim.generation,
        )
        return _snapshot(row)


async def recover_effect_once(effect_id: str) -> str:
    """Claim and reconcile one effect without ever replaying its send body."""
    claim = await _claim_for_reconcile(effect_id)
    if claim is None:
        return "stale"
    snapshot = await get_effect(effect_id, claim.tenant_id)
    if snapshot is None:
        return "stale"
    if snapshot.state == "prepared":
        await settle_effect(
            claim,
            state="failed",
            error={"code": "abandoned_before_dispatch"},
            evidence={"provider_called": False},
        )
        return "failed_before_dispatch"
    if snapshot.reconcile_count > MAX_RECONCILE_ATTEMPTS:
        await settle_effect(
            claim,
            state="manual_review",
            error={"code": "reconcile_attempts_exhausted"},
        )
        return "manual_review"

    reconciler = _reconcilers.get(snapshot.adapter)
    if reconciler is None:
        await settle_effect(
            claim,
            state="manual_review",
            error={"code": "reconciler_unavailable", "adapter": snapshot.adapter},
        )
        return "manual_review"
    if not snapshot.provider_handle and not bool(
        getattr(reconciler, "can_reconcile_without_handle", False)
    ):
        await settle_effect(
            claim,
            state="manual_review",
            error={"code": "provider_receipt_unavailable"},
        )
        return "manual_review"
    try:
        decision = await asyncio.wait_for(
            reconciler.reconcile(snapshot),
            timeout=RECONCILE_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        if snapshot.reconcile_count >= MAX_RECONCILE_ATTEMPTS:
            await settle_effect(
                claim,
                state="manual_review",
                error={"code": "reconcile_failed", "error_type": type(exc).__name__},
            )
            return "manual_review"
        await _defer_reconcile(claim, None, error=exc)
        return "deferred"

    if decision.state == "succeeded":
        await settle_effect(
            claim,
            state="succeeded",
            projection=decision.projection,
            provider_handle=decision.provider_handle,
            receipt=decision.receipt,
            evidence=decision.evidence,
        )
        return "reconciled"
    if decision.state == "failed":
        await settle_effect(
            claim,
            state="failed",
            error=decision.evidence or {"code": "provider_failed"},
            provider_handle=decision.provider_handle,
            receipt=decision.receipt,
        )
        return "reconciled"
    if decision.state == "manual_review":
        await settle_effect(
            claim,
            state="manual_review",
            error=decision.evidence or {"code": "manual_review_required"},
            provider_handle=decision.provider_handle,
            receipt=decision.receipt,
        )
        return "manual_review"
    await _defer_reconcile(claim, decision)
    return "deferred"


async def recover_external_effects_once(
    *, limit: int = RECOVERY_BATCH_SIZE
) -> EffectRecoveryResult:
    """Bounded startup/periodic scan; every candidate still uses exact CAS."""
    bounded_limit = max(1, min(int(limit), RECOVERY_BATCH_SIZE))
    async with get_db_session() as db:
        now = _database_now(db)
        result = await db.execute(
            select(ExternalEffect.id)
            .where(
                ExternalEffect.state.not_in(tuple(_TERMINAL_STATES)),
                or_(
                    ExternalEffect.claim_token.is_(None),
                    ExternalEffect.claim_expires_at <= now,
                ),
                or_(
                    ExternalEffect.reconcile_after.is_(None),
                    ExternalEffect.reconcile_after <= now,
                ),
                or_(
                    ExternalEffect.state != "prepared",
                    ExternalEffect.prepared_at
                    <= _database_before(db, PREPARED_RECOVERY_GRACE_SECONDS),
                ),
            )
            .order_by(ExternalEffect.updated_at, ExternalEffect.id)
            .limit(bounded_limit)
        )
        rows = list(result.scalars())
        result.close()

    counts = {
        "reconciled": 0,
        "deferred": 0,
        "manual_review": 0,
        "failed_before_dispatch": 0,
        "stale": 0,
    }
    for effect_id in rows:
        try:
            outcome = await recover_effect_once(effect_id)
        except EffectLeaseLostError:
            outcome = "stale"
        except Exception:
            # One broken provider must not prevent unrelated effects from
            # converging on the same bounded pass.
            log.exception("External-effect reconciliation failed effect_id=%s", effect_id)
            outcome = "stale"
        counts[outcome] = counts.get(outcome, 0) + 1
    return EffectRecoveryResult(
        scanned=len(rows),
        reconciled=counts["reconciled"],
        deferred=counts["deferred"],
        manual_review=counts["manual_review"],
        failed_before_dispatch=counts["failed_before_dispatch"],
        stale_skips=counts["stale"],
    )
