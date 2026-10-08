"""Authenticated complete-card display evidence, never model-created consent."""
import base64
from datetime import timedelta
from hashlib import sha256
import hmac
import json
import secrets
import time

from sqlalchemy import select

from assistant.commands import _authority, command_digest
from assistant.history import _cursor_key
from assistant.policy import AssistantError, main_session_locked
from assistant.request_reads import get_request, original
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from question import runtime
from session.agent_event_log import append_agent_event_locked
from session.internal_parts import begin_session_write

TTL_SECONDS = 300
DOMAIN = b"assistant-request-display-v1:"
DISPLAYED = "assistant.request.displayed"
#: Where else a complete display can happen: a call reads the whole request
#: aloud (docs/ASSISTANT_VOICE_FIX_PLAN.md 1.3). No channel means the card UI.
CHANNELS = frozenset({"voice"})


def _unavailable():
    return AssistantError(409, "ASSISTANT_REQUEST_DISPLAY", "Review the complete current request before replying in chat")


def _token(payload):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(_cursor_key(), DOMAIN + raw, sha256).digest()
    return base64.urlsafe_b64encode(signature + raw).decode().rstrip("=")


def _verify(token, scope):
    try:
        if not isinstance(token, str) or len(token) > 4096:
            raise ValueError()
        value = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        raw = value[32:]
        signature = hmac.new(_cursor_key(), DOMAIN + raw, sha256).digest()
        payload = json.loads(raw)
        if (not hmac.compare_digest(signature, value[:32]) or payload["scope"] != scope
                or payload["expires"] < time.time()):
            raise ValueError()
        return payload
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise _unavailable() from None


def segments(value):
    """All original review content, split into visible, measurable text blocks."""
    raw = json.dumps({"task": value["task_title"], "project": value["project_name"],
        **value["request"]}, ensure_ascii=False, indent=2)
    return [line[pos:pos + 240] for line in raw.splitlines() for pos in range(0, max(1, len(line)), 240)]


async def review(*, user_id, workspace_id, main_id, kind, request_id):
    from assistant.business_context import _safe
    value = await get_request(user_id=user_id, workspace_id=workspace_id, main_id=main_id,
        kind=kind, request_id=request_id)
    if value["state"] != "pending":
        raise _unavailable()
    # A masked operation is not a completely reviewed operation. Native cards
    # remain available, but no language approval can rely on missing bytes.
    if _safe(original(value)) != original(value):
        raise _unavailable()
    body = segments(value)
    if sum(map(len, body)) > 32000:
        raise _unavailable()
    stamp = int(time.time())
    payload = {"scope": [user_id, workspace_id, main_id], "kind": kind, "request_id": request_id,
        "revision": value["assistant"]["request_revision"], "digest": command_digest(original(value)),
        "issued": stamp, "expires": stamp + TTL_SECONDS, "nonce": secrets.token_hex(16)}
    return {"segments": body, "display_token": _token(payload), "request_revision": payload["revision"]}


async def displayed(*, user_id, workspace_id, main_id, display_token, channel=None, call_id=None):
    """Record that the complete current request was shown to the user.

    ``channel="voice"`` (with the call's ``call_id``) records that a call read
    it aloud in full: the same evidence as the card UI's display, and still no
    decision. Only the user's own next answer can follow it (request_reply).
    """
    if channel is not None and channel not in CHANNELS:
        raise ValueError("Unknown display channel")
    if call_id is not None and (channel is None or not isinstance(call_id, str) or not 1 <= len(call_id) <= 64):
        raise ValueError("A call ID of 1..64 characters belongs to a voice display")
    payload = _verify(display_token, [user_id, workspace_id, main_id])
    async with get_db_session() as db:
        await begin_session_write(db)
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        main = await main_session_locked(db, user_id, workspace_id, lock=True)
        value = await get_request(db=db, user_id=user_id, workspace_id=workspace_id, main_id=main_id,
            kind=payload["kind"], request_id=payload["request_id"])
        if value["state"] != "pending" or command_digest(original(value)) != payload["digest"]:
            raise _unavailable()
        key = "request-display:" + sha256(display_token.encode()).hexdigest()
        if channel is not None:
            payload = {**payload, "channel": channel, **({"call_id": call_id} if call_id else {})}
            key = f"request-display:{channel}:" + sha256(display_token.encode()).hexdigest()
        event = await append_agent_event_locked(db, main, kind=DISPLAYED, payload=payload, idempotency_key=key)
        return {"display_id": event.id, "state": "displayed"}


async def reply_context_locked(db, main):
    """Freeze prior complete displays into the authenticated next human input.

    Multiple recent requests stay ambiguous across devices. A model cannot
    choose one merely by naming it in a later tool call. A new human input
    consumes this display window; retries reuse that input's frozen context.
    """
    from assistant.permission_requests import json_text
    previous = await db.scalar(select(AgentEvent.sequence).where(
        AgentEvent.session_id == main.id, AgentEvent.user_id == main.user_id,
        AgentEvent.kind == "inbox.accepted", json_text(db, AgentEvent.payload, "origin") == "human")
        .order_by(AgentEvent.sequence.desc()).limit(1))
    since = runtime.now() - timedelta(seconds=TTL_SECONDS)
    events = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == main.id,
        AgentEvent.user_id == main.user_id, AgentEvent.kind == DISPLAYED,
        AgentEvent.sequence > (previous or 0), AgentEvent.created_at >= since)
        .order_by(AgentEvent.sequence.desc()).limit(51))).all())
    if len(events) > 50:
        return {"ambiguous": True, "displays": []}
    unique = {}
    for event in events:
        identity = (event.payload["kind"], event.payload["request_id"], event.payload["revision"])
        unique.setdefault(identity, {"event_id": event.id, "digest": command_digest(event.payload)})
    return {"ambiguous": len(unique) > 1, "displays": list(unique.values())}


async def accepted_event(db, main, inbox):
    """Use canonical event order, not rounded/read-model Inbox timestamps."""
    from assistant.permission_requests import json_text
    return await db.scalar(select(AgentEvent).where(AgentEvent.session_id == main.id,
        AgentEvent.user_id == main.user_id, AgentEvent.kind == "inbox.accepted",
        json_text(db, AgentEvent.payload, "item_id") == inbox.id))
