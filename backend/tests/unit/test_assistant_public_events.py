"""Queued frames and reconnect snapshots obey current audience at egress."""
import asyncio
import json

from db.base import get_db_session
from db.models.workspace import WorkspaceMember
from assistant.service import ensure_main_session
from session.public_events import public_event
from session.session import create_session
from api.ws import _enqueue_recovery_snapshot, _send_loop
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


async def test_private_main_frames_are_invalidation_only_and_other_owners_cannot_subscribe():
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    for kind in ("message.created", "message.updated", "tool.completed", "session.compaction.complete"):
        event = {"type": kind, "data": {"sessionId": main.id, "userId": owner, "generation": 1,
            "message": {"parts": [{"text": "SECRET_TRANSCRIPT"}]}, "summary": "SECRET_TRANSCRIPT"}}
        assert await public_event(owner, event) == {"type": "assistant.history.changed",
            "data": {"sessionId": main.id, "generation": 1}}
        assert await public_event(other, event) is None
    assert await public_event(owner, {"type": "part.delta", "data": {
        "sessionId": main.id, "delta": "SECRET_TRANSCRIPT"}}) is None


async def test_send_pump_checks_membership_after_payload_was_queued_and_recovery_omits_revoked_sessions():
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    queue = asyncio.Queue()
    queue.put_nowait({"type": "message.created", "data": {"sessionId": main.id, "message": {"text": "OLD_SECRET"}}})
    async with get_db_session() as db:
        (await db.get(WorkspaceMember, (workspace, owner))).status = "removed"
    queue.put_nowait({"type": "server.heartbeat", "data": {}})
    sent = []
    class Socket:
        async def send_json(self, event):
            sent.append(event)
            raise asyncio.CancelledError()
    try:
        await _send_loop(Socket(), queue, user_id=owner)
    except asyncio.CancelledError:
        pass
    assert sent == [{"type": "server.heartbeat", "data": {}}]
    recovery = asyncio.Queue()
    await _enqueue_recovery_snapshot(owner, recovery)
    assert recovery.empty()
    assert "OLD_SECRET" not in json.dumps(sent)


async def test_workspace_session_streaming_is_preserved_for_authorized_viewers():
    owner, other, workspace = await accounts()
    normal = await create_session(user_id=owner, workspace_id=workspace)
    event = {"type": "part.delta", "data": {"sessionId": normal.id, "delta": "Ordinary live text"}}
    assert await public_event(owner, event) == event
    assert await public_event(other, event) == event


async def test_budget_failure_frame_keeps_only_the_safe_code_and_current_audience():
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    for code, expected in (("ASSISTANT_TURN_BUDGET", "ASSISTANT_TURN_BUDGET"),
                           ("PRIVATE_PROVIDER_CODE", "ASSISTANT_RUN_FAILED")):
        event = {"type": "session.error", "data": {"sessionId": main.id, "generation": 1,
            "error": {"code": code, "message": "PRIVATE_PROVIDER_DETAIL"}}}
        assert await public_event(owner, event) == {"type": "session.error", "data": {
            "sessionId": main.id, "generation": 1, "error": {"code": expected}}}
        assert await public_event(other, event) is None
    async with get_db_session() as db:
        (await db.get(WorkspaceMember, (workspace, owner))).status = "removed"
    assert await public_event(owner, event) is None
