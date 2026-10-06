"""Queued frames and reconnect snapshots obey current audience at egress.

V2 (PERSONAL_ASSISTANT_DESIGN_V2.md 4.2, 11.2): assistant and task sessions
stream like ordinary chats to their current readers; there is no
"assistant.history.changed" collapsing.
"""
import asyncio
import json
import pytest
from fastapi import HTTPException

from db.base import get_db_session
from db.models.workspace import WorkspaceMember
from assistant.service import ensure_main_session
from session.public_events import public_event
from session.session import create_session
from api.ws import _enqueue_recovery_snapshot, _send_loop
from auth.socket_access import SocketAccess
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


async def test_private_main_frames_stream_to_the_owner_and_other_members_cannot_subscribe():
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    for kind in ("message.created", "message.updated", "tool.completed", "session.compaction.complete",
                 "part.delta", "message.text_delta"):
        event = {"type": kind, "data": {"sessionId": main.id, "userId": owner, "generation": 1,
            "message": {"parts": [{"text": "OWNER_TRANSCRIPT"}]}, "delta": "OWNER_TRANSCRIPT"}}
        assert await public_event(owner, event) == event
        assert await public_event(other, event) is None


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
    with pytest.raises(HTTPException) as denied:
        await _send_loop(Socket(), queue,
            access=SocketAccess(owner, workspace, "web", None, True))
    assert denied.value.status_code == 403
    assert sent == []
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


async def test_private_descendant_frames_reach_only_their_owner():
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    child = await create_session(user_id=owner, workspace_id=workspace, parent_id=main.id)
    for kind in ('message.created', 'message.updated', 'tool.completed', 'session.title', 'question.asked',
                 'question.updated', 'permission.asked', 'todo.updated', 'part.delta', 'part.created',
                 'part.updated', 'message.text_delta', 'tool.running', 'session.status'):
        event = {'type': kind, 'data': {'sessionId': child.id, 'generation': 4, 'status': 'waiting_input',
            'text': 'OWNER_ONLY_TEXT', 'title': 'OWNER_ONLY_TEXT', 'question': {'text': 'OWNER_ONLY_TEXT'}}}
        assert await public_event(owner, event) == event
        assert await public_event(other, event) is None


async def test_failure_frame_follows_the_current_audience():
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    for code in ("ASSISTANT_TURN_BUDGET", "PROVIDER_CODE"):
        event = {"type": "session.error", "data": {"sessionId": main.id, "generation": 1,
            "error": {"code": code, "message": "Provider detail for the owner"}}}
        assert await public_event(owner, event) == event
        assert await public_event(other, event) is None
    async with get_db_session() as db:
        (await db.get(WorkspaceMember, (workspace, owner))).status = "removed"
    assert await public_event(owner, event) is None
