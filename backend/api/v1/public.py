"""Public projections of sessions, messages and parts.

Everything the partner sees passes through here, so the contract's shape
lives in one file: internal fields are dropped, ids are renamed, and the
step-per-message transcript is folded into one assistant message per turn.
"""
from __future__ import annotations

import posixpath
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from api.v1.ids import assistant_turn_id, public_id
from models.message import id_to_iso

#: Session statuses the contract reports as ``busy``.
ACTIVE_STATUSES = frozenset({"busy", "compacting", "retry", "queued", "waiting_input"})

_QUESTION_STATUS = {
    "pending": "pending",
    "answered": "answered",
    "rejected": "rejected",
    "expired": "timeout",
    "cancelled": "rejected",
    "superseded": "rejected",
}

#: ``result`` is what a tool hands the person as its deliverable (a shared
#: file, a generated image, a composed video), so it is a final too.
_FILE_ROLE = {"final": "final", "result": "final", "input": "input"}

#: Client ids the platform writes for its own continuations. Their user
#: messages are carriers, not turns (see ``_is_carrier``).
CARRIER_CLIENT_PREFIXES = ("vjob:", "ask:", "cron:", "sjr:", "tabort:")

Presign = Callable[[str, str | None], str | None]


def iso_z(value: datetime | str | None) -> str | None:
    """ISO 8601 in UTC with a ``Z`` suffix, from a datetime or an ISO string."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def session_status(status: str | None, *, busy: bool = False) -> str:
    if busy or status in ACTIVE_STATUSES:
        return "busy"
    if status == "error":
        return "error"
    return "idle"


def credits_text(value: Decimal | None) -> str:
    if value is None:
        return "0"
    text = format(value.normalize(), "f")
    return text if text != "-0" else "0"


def session_view(row, credits_used: Decimal | None = None, *, busy: bool = False) -> dict:
    """``GET /v1/sessions/{id}`` body from a sessions row.

    ``busy`` says a video job or a platform continuation is still running
    for the session even though the model's own run has ended.
    """
    return {
        "id": public_id(row.id),
        "title": row.title or "",
        "status": session_status(row.status, busy=busy),
        "quality": getattr(row, "quality", None),
        "metadata": dict(getattr(row, "metadata_", None) or {}),
        "credits_used": credits_text(credits_used),
        "created_at": iso_z(row.created_at),
        "updated_at": iso_z(row.updated_at),
    }


# ─── Parts ───

def _question_from_checkpoint(part: dict, checkpoint) -> dict:
    """A ``question`` part synthesised from the tool call that asked it.

    Until the question tool writes its own ``question`` part (task C), the
    durable checkpoint row beside the tool part carries everything the card
    needs. Once native parts exist they are preferred; see ``turn_parts``.
    """
    status = checkpoint.status if checkpoint is not None else (
        (part.get("metadata") or {}).get("question_status") or "pending"
    )
    expires_at = getattr(checkpoint, "expires_at", None) if checkpoint is not None else None
    public_status = _QUESTION_STATUS.get(status, "rejected")
    if public_status == "pending" and expires_at is not None:
        expiry = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=timezone.utc)
        if expiry <= datetime.now(timezone.utc):
            public_status = "timeout"
    if checkpoint is not None:
        questions = [_public_question(q) for q in (checkpoint.questions or [])]
    else:
        questions = [
            {"header": "", "question": q, "options": [], "multiple": False, "custom": True}
            for q in ((part.get("metadata") or {}).get("questions") or [])
        ]
    return {
        "type": "question",
        "id": public_id(part.get("id")),
        "question_id": public_id((part.get("metadata") or {}).get("question_id"), "question"),
        "status": public_status,
        "expires_at": iso_z(expires_at),
        "questions": questions,
    }


def _public_question(q: dict) -> dict:
    return {
        "header": q.get("header") or "",
        "question": q.get("question") or "",
        "options": [
            {"label": o.get("label") or "", "description": o.get("description") or ""}
            for o in (q.get("options") or [])
        ],
        "multiple": bool(q.get("multiple")),
        "custom": bool(q.get("custom", True)),
    }


def _native_question(part: dict) -> dict:
    """Pass a first-class ``question`` part through, renaming its ids."""
    return {
        "type": "question",
        "id": public_id(part.get("id")),
        "question_id": public_id(part.get("question_id"), "question"),
        "status": _QUESTION_STATUS.get(part.get("status") or "pending", part.get("status") or "pending"),
        "expires_at": iso_z(part.get("expires_at")),
        "questions": [_public_question(q) for q in (part.get("questions") or [])],
    }


def _file_part(part: dict, presign: Presign | None) -> dict | None:
    asset_id = part.get("asset_id")
    if not asset_id or part.get("transient"):
        return None
    relation = part.get("relation") or {}
    role = _FILE_ROLE.get(relation.get("role"), "intermediate")
    name = relation.get("label") or posixpath.basename(part.get("path") or "") or asset_id
    url = None
    if presign is not None and part.get("oss_key"):
        url = presign(part["oss_key"], name)
    return {
        "type": "file",
        "id": public_id(part.get("id")),
        "file": {
            "id": public_id(asset_id),
            "filename": name,
            "mime_type": part.get("mime_type"),
            "size": part.get("size"),
            # Media probing is out of scope for the first release (plan §10.2).
            "duration_s": None,
            "width": None,
            "height": None,
            "url": url,
            "role": role,
        },
    }


def turn_parts(parts: list[dict], checkpoints: dict[str, Any], presign: Presign | None) -> list[dict]:
    """Public parts for one message (or one folded turn), in transcript order."""
    native_questions = {
        p.get("question_id") for p in parts if p.get("type") == "question" and p.get("question_id")
    }
    out: list[dict] = []
    for part in parts:
        kind = part.get("type")
        if kind == "text":
            if part.get("synthetic") or part.get("ignored"):
                continue
            out.append({"type": "text", "id": public_id(part.get("id")), "text": part.get("text") or ""})
        elif kind == "question":
            out.append(_native_question(part))
        elif kind == "tool":
            question_id = (part.get("metadata") or {}).get("question_id")
            if question_id and question_id not in native_questions:
                out.append(_question_from_checkpoint(part, checkpoints.get(question_id)))
        elif kind == "file":
            item = _file_part(part, presign)
            if item is not None:
                out.append(item)
    return out


# ─── Messages ───

def _part_dicts(message) -> list[dict]:
    """Parts as plain dicts: repositories hand back dicts, models hand back models."""
    out = []
    for part in message.parts:
        if isinstance(part, dict):
            out.append(part)
        elif hasattr(part, "model_dump"):
            out.append(part.model_dump())
    return out


def _is_carrier(message) -> bool:
    """A user message the loop wrote for itself (plan approval, reminders).

    It has no visible text, so the partner never sees it; the assistant steps
    it triggered still belong to the last real turn.
    """
    client_id = getattr(message, "client_message_id", None) or ""
    if client_id.startswith(CARRIER_CLIENT_PREFIXES):
        return True
    text_parts = [p for p in _part_dicts(message) if p.get("type") == "text"]
    return bool(text_parts) and all(p.get("synthetic") for p in text_parts)


def _public_error(error: dict | None) -> dict | None:
    if not error:
        return None
    code = error.get("code") or error.get("name") or "INTERNAL_ERROR"
    return {"code": str(code), "message": str(error.get("message") or code)}


def _part_time(part: dict) -> str | None:
    part_id = part.get("id")
    return iso_z(id_to_iso(part_id)) if part_id else None


def _finish_of(rows: list, *, latest: bool, session_active: bool, session_error: bool,
               work_pending: bool = False, aborted: bool = False) -> str | None:
    last = rows[-1] if rows else None
    raw = getattr(last, "finish", None) if last is not None else None
    if latest and work_pending and raw != "error":
        # The model's run ended, but a paid generation of this turn is still
        # in flight and a continuation will deliver it: the turn is not over.
        return None
    if raw in ("stop", "length"):
        return "stop"
    if raw == "error":
        return "error"
    if raw == "aborted":
        return "aborted"
    # No terminal step yet: still running, or a run that ended without one.
    if latest and session_active:
        return None
    if latest and session_error:
        return "error"
    if aborted:
        # Stopped before the model wrote a single step.
        return "aborted"
    return "stop"


def _promote_final(parts: list[dict]) -> None:
    """A finished turn without a declared final delivers its last video.

    The production skill marks single takes ``intermediate`` and only a
    composed cut ``final``; when the model ends the turn with one take as the
    whole deliverable, that take is the 成片 the contract promises.
    """
    files = [p for p in parts if p.get("type") == "file"]
    if not files or any(p["file"]["role"] == "final" for p in files):
        return
    videos = [p for p in files if (p["file"].get("mime_type") or "").startswith("video/")]
    if videos:
        videos[-1]["file"]["role"] = "final"


def public_messages(
    messages: list,
    *,
    session_status_value: str | None,
    checkpoints: dict[str, Any],
    presign: Presign | None,
    work_pending: bool = False,
    aborted_user_message_ids: frozenset[str] | set[str] = frozenset(),
) -> list[dict]:
    """Fold a transcript into the contract's user / assistant message list.

    Each real user message is followed by exactly one assistant message: the
    concatenation of every assistant step the loop wrote for that turn, with
    the newest step's finish and error. A turn whose loop has not started yet
    still appears, empty, so the id handed out at send time resolves at once.
    """
    turns: list[dict] = []
    current: dict | None = None
    for message in messages:
        role = message.role if isinstance(message.role, str) else message.role.value
        if role == "user":
            if _is_carrier(message):
                continue
            current = {"user": message, "rows": []}
            turns.append(current)
        elif role == "assistant" and current is not None:
            current["rows"].append(message)

    active = session_status_value in ACTIVE_STATUSES
    errored = session_status_value == "error"
    out: list[dict] = []
    for index, turn in enumerate(turns):
        user = turn["user"]
        rows = turn["rows"]
        latest = index == len(turns) - 1
        user_parts = _part_dicts(user)
        out.append({
            "id": public_id(user.id),
            "role": "user",
            "finish": "stop",
            "error": None,
            "created_at": iso_z(user.created_at),
            "updated_at": iso_z(user.created_at),
            "parts": turn_parts(user_parts, checkpoints, presign),
        })
        parts = [p for row in rows for p in _part_dicts(row)]
        finish = _finish_of(
            rows, latest=latest, session_active=active, session_error=errored,
            work_pending=work_pending, aborted=user.id in aborted_user_message_ids,
        )
        error = _public_error(getattr(rows[-1], "error", None)) if rows and finish == "error" else None
        if finish == "error" and error is None:
            error = {"code": "INTERNAL_ERROR", "message": "The run ended without a result"}
        created_at = iso_z(rows[0].created_at) if rows else iso_z(user.created_at)
        stamps = [iso_z(r.created_at) for r in rows] + [_part_time(p) for p in parts[-1:]]
        updated_at = max((s for s in stamps if s), default=created_at)
        public_parts = turn_parts(parts, checkpoints, presign)
        if finish == "stop":
            _promote_final(public_parts)
        out.append({
            "id": assistant_turn_id(user.id),
            "role": "assistant",
            "finish": finish,
            "error": error,
            "created_at": created_at,
            "updated_at": updated_at,
            "parts": public_parts,
        })
    return out
