"""Collect deterministic tool-exposure signals without sandbox I/O.

Only the current user turn and backend-owned, user/session-scoped state are
consulted.  Every state source fails independently and fail-small: an outage
adds a stable error code but never invents an intent or broadens exposure.
"""
from __future__ import annotations

import asyncio
import mimetypes
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.video_job import VideoJob
from db.models.video_production import VideoApproval, VideoProduction

if TYPE_CHECKING:
    from agent.tool_exposure import ExposureSignals


_URL = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)
_URL_TRAILING_PUNCTUATION = ".,;:!?)]}，。；：！？）】"

_RUNNING_VIDEO_JOB_STATUSES = (
    "submitting",
    "dispatching",
    "queued",
    "in_progress",
    "generating",
    "finalizing",
    "transcribing",
)
_RECOVERY_VIDEO_JOB_STATUSES = (
    "dispatch_unknown",
    "transfer_failed",
    "extraction_completed",
    "transcript_ready",
)
_MAX_DELIVERABLE_ASSETS = 32

# Product-state probes are deliberately cached for only a short window.  This
# is long enough to keep recovery controls visible across a transient database
# hiccup inside one run, but short enough that durable state remains the source
# of truth.  Cache values are booleans only: user text, asset IDs and provider
# payloads never enter this process-local fallback.
_PRODUCT_STATE_LKG_TTL_SECONDS = 60.0
_PRODUCT_STATE_LKG_MAX_SCOPES = 2_048
_PRODUCT_STATE_LKG_CLOCK = time.monotonic
_DELIVERABLE_LKG_MARKER = "lkg:deliverable-present"


@dataclass(frozen=True)
class _ProductStateLkgValue:
    value: bool
    expires_at: float


_PRODUCT_STATE_LKG: dict[
    tuple[str, str],
    dict[str, _ProductStateLkgValue],
] = {}


def _prune_product_state_lkg(now: float) -> None:
    """Drop expired/old scopes so the in-memory fallback stays bounded."""

    for scope, values in tuple(_PRODUCT_STATE_LKG.items()):
        current = {
            name: item for name, item in values.items() if item.expires_at > now
        }
        if current:
            _PRODUCT_STATE_LKG[scope] = current
        else:
            _PRODUCT_STATE_LKG.pop(scope, None)

    overflow = len(_PRODUCT_STATE_LKG) - _PRODUCT_STATE_LKG_MAX_SCOPES
    if overflow <= 0:
        return
    oldest = sorted(
        _PRODUCT_STATE_LKG,
        key=lambda scope: max(
            item.expires_at for item in _PRODUCT_STATE_LKG[scope].values()
        ),
    )
    for scope in oldest[:overflow]:
        _PRODUCT_STATE_LKG.pop(scope, None)


def _remember_product_state(
    scope: tuple[str, str],
    probe_name: str,
    value: bool,
    now: float,
) -> None:
    _PRODUCT_STATE_LKG.setdefault(scope, {})[probe_name] = _ProductStateLkgValue(
        value=bool(value),
        expires_at=now + _PRODUCT_STATE_LKG_TTL_SECONDS,
    )


def _recent_product_state(
    scope: tuple[str, str],
    probe_name: str,
    now: float,
) -> bool | None:
    item = _PRODUCT_STATE_LKG.get(scope, {}).get(probe_name)
    if item is None or item.expires_at <= now:
        return None
    return item.value


def _value(value: object, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _parts(last_user: object | None) -> tuple[object, ...]:
    if last_user is None:
        return ()
    if isinstance(last_user, (list, tuple)):
        return tuple(last_user)
    values = _value(last_user, "parts", ())
    return (
        tuple(values)
        if isinstance(values, Iterable) and not isinstance(values, str)
        else ()
    )


def _stable_unique(values: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return tuple(result)


def _attachment_kind(part: object) -> str:
    mime = str(
        _value(part, "mime_type", None)
        or _value(part, "mimeType", None)
        or _value(part, "mime", None)
        or ""
    ).lower()
    if not mime:
        candidate = str(
            _value(part, "path", None)
            or _value(part, "name", None)
            or _value(part, "url", None)
            or ""
        )
        mime = (mimetypes.guess_type(candidate)[0] or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("video/"):
        return "video"
    if mime.startswith("audio/"):
        return "audio"
    if mime == "application/pdf":
        return "pdf"
    if mime.startswith("text/"):
        return "text"
    return "file"


def extract_user_part_signals(
    last_user: object | None,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """Extract visible task text, text URLs, and attachment kinds.

    Synthetic/ignored text is backend control material rather than current
    user intent.  Attachment preview URLs are deliberately not treated as task
    URLs; they are storage locations and would spuriously activate research.
    """

    texts: list[str] = []
    kinds: list[str] = []
    for part in _parts(last_user):
        part_type = str(_value(part, "type", "")).lower()
        if part_type == "text":
            if bool(_value(part, "synthetic", False)) or bool(
                _value(part, "ignored", False)
            ):
                continue
            text = str(_value(part, "text", "") or "").strip()
            if text:
                texts.append(text)
        elif part_type == "file":
            kinds.append(_attachment_kind(part))

    task_text = "\n".join(texts)
    urls = _stable_unique(
        match.group(0).rstrip(_URL_TRAILING_PUNCTUATION)
        for match in _URL.finditer(task_text)
    )
    return task_text, urls, _stable_unique(kinds)


async def _has_open_todos(session_id: str) -> bool:
    from session.todo import get_todo

    todo = await get_todo(session_id)
    return any(item.status not in {"completed", "cancelled"} for item in todo.items)


async def _has_active_video_production(user_id: str, session_id: str) -> bool:
    async with get_db_session() as db:
        value = await db.execute(
            select(VideoProduction.id)
            .where(
                VideoProduction.user_id == user_id,
                VideoProduction.session_id == session_id,
                # Delivered is the only terminal production status. Error,
                # approval and revision states still need status/recovery UI.
                VideoProduction.status != "delivered",
            )
            .limit(1)
        )
        return value.scalar_one_or_none() is not None


async def _has_active_video_job(user_id: str, session_id: str) -> bool:
    async with get_db_session() as db:
        value = await db.execute(
            select(VideoJob.id)
            .where(
                VideoJob.user_id == user_id,
                VideoJob.session_id == session_id,
                VideoJob.status.in_(_RUNNING_VIDEO_JOB_STATUSES),
            )
            .limit(1)
        )
        return value.scalar_one_or_none() is not None


async def _has_video_approval_state(user_id: str, session_id: str) -> bool:
    """Find approval evidence only through its owned, active production."""

    async with get_db_session() as db:
        value = await db.execute(
            select(VideoApproval.id)
            .join(VideoProduction, VideoApproval.production_id == VideoProduction.id)
            .where(
                VideoApproval.user_id == user_id,
                VideoApproval.session_id == session_id,
                VideoProduction.user_id == user_id,
                VideoProduction.session_id == session_id,
                VideoProduction.status != "delivered",
            )
            .limit(1)
        )
        return value.scalar_one_or_none() is not None


async def _has_video_recovery_state(user_id: str, session_id: str) -> bool:
    async with get_db_session() as db:
        value = await db.execute(
            select(VideoJob.id)
            .where(
                VideoJob.user_id == user_id,
                VideoJob.session_id == session_id,
                VideoJob.status.in_(_RECOVERY_VIDEO_JOB_STATUSES),
            )
            .limit(1)
        )
        return value.scalar_one_or_none() is not None


async def _deliverable_asset_ids(user_id: str, session_id: str) -> tuple[str, ...]:
    """Return bounded agent outputs with an exact user/session ownership link."""

    async with get_db_session() as db:
        rows = await db.execute(
            select(FileAsset.id)
            .where(
                FileAsset.user_id == user_id,
                FileAsset.session_id == session_id,
                FileAsset.source == "agent",
                FileAsset.status == "ready",
                FileAsset.transient.is_(False),
                FileAsset.is_deleted.is_(False),
            )
            .order_by(FileAsset.created_at.desc(), FileAsset.id)
            .limit(_MAX_DELIVERABLE_ASSETS)
        )
        # IDs, not database row order, are the stable planner input.
        return tuple(sorted(str(asset_id) for (asset_id,) in rows))


def _browser_workflow_active() -> bool:
    # The repository has a trusted user-wide browser *preference*, but no
    # session-scoped "workflow active" state. Treating the default preference
    # as activity would load browser tools for every turn. Stay fail-small
    # until a backend-owned session state exists; do not inspect relay sockets
    # or a sandbox browser from this zero-I/O collector.
    return False


async def collect_exposure_signals(
    last_user: object | None,
    *,
    session_id: str,
    user_id: str,
) -> "ExposureSignals":
    """Collect one immutable exposure snapshot with independent fault domains.

    A failed product-state probe may reuse only that probe's most recent,
    unexpired boolean for the same user and session.  A successful result,
    including an explicit ``False``, always replaces the fallback.
    """

    from agent.tool_exposure import ExposureSignals

    task_text, urls, attachment_kinds = extract_user_part_signals(last_user)
    probes = (
        ("todo_state_unavailable", _has_open_todos(session_id)),
        (
            "video_production_state_unavailable",
            _has_active_video_production(user_id, session_id),
        ),
        ("video_job_state_unavailable", _has_active_video_job(user_id, session_id)),
        (
            "video_approval_state_unavailable",
            _has_video_approval_state(user_id, session_id),
        ),
        (
            "video_recovery_state_unavailable",
            _has_video_recovery_state(user_id, session_id),
        ),
        (
            "deliverable_asset_state_unavailable",
            _deliverable_asset_ids(user_id, session_id),
        ),
    )
    values = await asyncio.gather(
        *(probe for _error_code, probe in probes),
        return_exceptions=True,
    )

    now = _PRODUCT_STATE_LKG_CLOCK()
    scope = (str(user_id), str(session_id))
    _prune_product_state_lkg(now)
    resolved: list[object] = []
    errors: list[str] = []
    defaults: tuple[object, ...] = (False, False, False, False, False, ())
    for (error_code, _probe), value, default in zip(probes, values, defaults):
        if isinstance(value, asyncio.CancelledError):
            raise value
        if isinstance(value, BaseException):
            errors.append(error_code)
            cached = _recent_product_state(scope, error_code, now)
            if cached is None:
                resolved.append(default)
            elif error_code == "deliverable_asset_state_unavailable":
                resolved.append((_DELIVERABLE_LKG_MARKER,) if cached else ())
            else:
                resolved.append(cached)
        else:
            resolved.append(value)
            _remember_product_state(scope, error_code, bool(value), now)

    _prune_product_state_lkg(now)

    open_todos, production, job, approval, recovery, assets = resolved
    return ExposureSignals(
        user_task_text=task_text,
        urls=urls,
        attachment_kinds=attachment_kinds,
        has_open_todos=bool(open_todos),
        has_active_video_production=bool(production or approval),
        has_active_video_job=bool(job or recovery),
        browser_workflow_active=_browser_workflow_active(),
        deliverable_asset_ids=tuple(assets) if isinstance(assets, tuple) else (),
        signal_errors=tuple(errors),
    )
