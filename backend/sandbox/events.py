"""Best-effort timeline of desktop operations (the `desktop_events` table).

`emit` writes one row and never raises: a desktop operation must not fail
because its bookkeeping did. `span` wraps an operation, records its duration
and outcome, and lets the body attach a summary and detail before it returns.

Identity comes from two places. The caller names the desktop (`desktop_id`)
or the sandbox key it used (`container_key`); the session and tool-call ids
are read from the `SandboxClient`'s request trace when the caller passes the
client, so the row lines up with the `X-OpenBox-*` headers the action server
logged for the same work.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select

from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.desktop_event import DesktopEvent

log = create_logger("sandbox.events")

RETENTION_DAYS = 30
SUMMARY_LIMIT = 300
#: JSON detail is capped so one runaway log tail cannot bloat the table.
DETAIL_LIMIT = 48 * 1024

KINDS = (
    "browser.ensure",
    "browser.chrome_launch",
    "browser.relay_start",
    "browser.runtime_check",
    "browser.runtime_repair",
    "browser.diag",
    "channel.verify",
    "lease.acquire",
    "platform.probe",
)


def trace_of(client) -> dict[str, str]:
    """Session / tool-call ids the client is currently sending to the desktop."""
    trace_var = getattr(client, "_trace", None)
    if trace_var is None:
        return {}
    try:
        trace = trace_var.get()
    except Exception:
        return {}
    return {
        "session_id": getattr(trace, "session_id", "") or "",
        "tool_call_id": getattr(trace, "tool_call_id", "") or "",
    }


def _clip(text: str | None, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _bounded_detail(detail: dict | None) -> dict | None:
    if not detail:
        return None
    import json

    encoded = json.dumps(detail, default=str, separators=(",", ":"))
    if len(encoded) <= DETAIL_LIMIT:
        return json.loads(encoded)
    # Drop the largest top-level values first; keep the shape readable.
    trimmed = dict(detail)
    for key in sorted(trimmed, key=lambda k: len(json.dumps(trimmed[k], default=str)), reverse=True):
        trimmed[key] = f"<{len(json.dumps(trimmed[key], default=str))} bytes dropped>"
        if len(json.dumps(trimmed, default=str, separators=(",", ":"))) <= DETAIL_LIMIT:
            break
    trimmed["_truncated"] = True
    return json.loads(json.dumps(trimmed, default=str))


async def emit(
    kind: str,
    *,
    status: str = "ok",
    desktop_id: str = "",
    container_key: str = "",
    session_id: str = "",
    tool_call_id: str = "",
    request_id: str = "",
    duration_ms: int | None = None,
    summary: str = "",
    detail: dict | None = None,
    diag_id: str = "",
    client=None,
) -> str | None:
    """Record one event. Returns its id, or None when it could not be stored."""
    if client is not None:
        trace = trace_of(client)
        session_id = session_id or trace.get("session_id", "")
        tool_call_id = tool_call_id or trace.get("tool_call_id", "")
        desktop_id = desktop_id or (getattr(client, "desktop_id", "") or "")
    if not container_key:
        container_key = desktop_id
    event_id = ascending("dev")
    try:
        async with get_db_session() as session:
            session.add(DesktopEvent(
                id=event_id,
                ts=datetime.now(timezone.utc),
                desktop_id=desktop_id or None,
                container_key=container_key or None,
                session_id=session_id or None,
                tool_call_id=tool_call_id or None,
                request_id=request_id or None,
                kind=kind,
                status=status,
                duration_ms=duration_ms,
                summary=_clip(summary, SUMMARY_LIMIT),
                detail=_bounded_detail(detail),
                diag_id=diag_id or None,
            ))
    except Exception as exc:
        log.warning(
            "desktop event not stored kind=%s status=%s key=%s error=%s: %s",
            kind, status, container_key or desktop_id, type(exc).__name__, _clip(summary, 200),
        )
        return None
    return event_id


class Span:
    """Mutable record a `span` body fills in before it ends."""

    def __init__(self) -> None:
        self.summary = ""
        self.detail: dict[str, Any] = {}
        self.status = "ok"
        self.diag_id = ""
        self.event_id: str | None = None


@asynccontextmanager
async def span(kind: str, *, client=None, **context):
    """Time an operation and record how it ended. Exceptions propagate.

    On failure the summary is the exception's first line (unless the body set
    one), the status is `timeout` for timeouts and `fail` otherwise, and a
    `diag_id` attribute on the exception is picked up automatically.
    """
    started = time.monotonic()
    record = Span()
    try:
        yield record
    except BaseException as exc:
        if isinstance(exc, (TimeoutError, __import__("asyncio").TimeoutError)):
            record.status = "timeout"
        elif record.status == "ok":
            record.status = "fail"
        if not record.summary:
            record.summary = f"{type(exc).__name__}: {str(exc).splitlines()[0] if str(exc) else ''}"
        record.diag_id = record.diag_id or getattr(exc, "diag_id", "") or ""
        record.detail.setdefault("error", _clip(str(exc), 4000))
        record.event_id = await emit(
            kind, status=record.status, client=client, summary=record.summary,
            detail=record.detail, diag_id=record.diag_id,
            duration_ms=round((time.monotonic() - started) * 1000), **context,
        )
        raise
    record.event_id = await emit(
        kind, status=record.status, client=client, summary=record.summary,
        detail=record.detail, diag_id=record.diag_id,
        duration_ms=round((time.monotonic() - started) * 1000), **context,
    )


def _row(event: DesktopEvent, *, with_detail: bool) -> dict:
    data = {
        "id": event.id,
        "ts": event.ts.isoformat() if event.ts else None,
        "desktop_id": event.desktop_id,
        "container_key": event.container_key,
        "session_id": event.session_id,
        "tool_call_id": event.tool_call_id,
        "request_id": event.request_id,
        "kind": event.kind,
        "status": event.status,
        "duration_ms": event.duration_ms,
        "summary": event.summary,
        "diag_id": event.diag_id,
    }
    if with_detail:
        data["detail"] = event.detail
    return data


async def get(event_id: str) -> dict | None:
    async with get_db_session() as session:
        event = await session.get(DesktopEvent, event_id)
        return _row(event, with_detail=True) if event else None


async def list_events(
    *,
    desktop_id: str = "",
    container_key: str = "",
    session_id: str = "",
    kind: str = "",
    status: str = "",
    limit: int = 100,
    with_detail: bool = False,
) -> list[dict]:
    """Newest first. `desktop_id` also matches rows keyed only by container."""
    stmt = select(DesktopEvent)
    if desktop_id:
        stmt = stmt.where(
            (DesktopEvent.desktop_id == desktop_id) | (DesktopEvent.container_key == desktop_id)
        )
    if container_key:
        stmt = stmt.where(DesktopEvent.container_key == container_key)
    if session_id:
        stmt = stmt.where(DesktopEvent.session_id == session_id)
    if kind:
        stmt = stmt.where(DesktopEvent.kind == kind)
    if status:
        stmt = stmt.where(DesktopEvent.status == status)
    stmt = stmt.order_by(DesktopEvent.ts.desc(), DesktopEvent.id.desc()).limit(max(1, min(limit, 500)))
    async with get_db_session() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [_row(row, with_detail=with_detail) for row in rows]


async def purge(*, older_than_days: int = RETENTION_DAYS) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    async with get_db_session() as session:
        result = await session.execute(delete(DesktopEvent).where(DesktopEvent.ts < cutoff))
        return result.rowcount or 0


async def run_purge_task() -> None:
    removed = await purge()
    if removed:
        log.info("purged %d desktop events older than %d days", removed, RETENTION_DAYS)
