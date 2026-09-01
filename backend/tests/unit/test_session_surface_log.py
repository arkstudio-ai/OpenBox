"""Append-only provenance for regenerate/dismiss Surface rewrites."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from agent.driver import reserve_run
from db.base import get_db_session
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session as SessionORM
from db.models.session_surface_event import SessionSurfaceEvent
from db.models.user import User
from session.session import delete_failed_turn, delete_messages_from, get_messages
from session.agent_event_log import verify_agent_event_parity


async def _seed_surface() -> tuple[str, str, list[str]]:
    suffix = uuid4().hex[:12]
    user_id = f"usr_surface_{suffix}"
    project_id = f"prj_surface_{suffix}"
    session_id = f"ses_surface_{suffix}"
    message_ids = [f"msg_surface_{suffix}_{index}" for index in range(4)]
    now = datetime.now(timezone.utc)

    async with get_db_session() as db:
        db.add(User(
            id=user_id,
            username=user_id,
            created_at=now,
            updated_at=now,
        ))
        db.add(Project(
            id=project_id,
            user_id=user_id,
            name=project_id,
            slug=project_id,
            created_at=now,
            updated_at=now,
        ))
        db.add(SessionORM(
            id=session_id,
            user_id=user_id,
            project_id=project_id,
            title="surface-log-test",
            agent="build",
            model="test-model",
            status="idle",
            token_usage={},
            tool_exposure_state={},
            created_at=now,
            updated_at=now,
        ))
        for index, message_id in enumerate(message_ids):
            created_at = now + timedelta(microseconds=index)
            is_user = index % 2 == 0
            parent_id = None if is_user else message_ids[index - 1]
            db.add(Message(
                id=message_id,
                session_id=session_id,
                user_id=user_id,
                role="user" if is_user else "assistant",
                parent_id=parent_id,
                error=None if is_user else {"name": "ProviderError", "message": "failed"},
                model="test-model",
                created_at=created_at,
            ))
            db.add(Part(
                id=f"part_surface_{suffix}_{index}",
                message_id=message_id,
                session_id=session_id,
                user_id=user_id,
                type="text",
                data={
                    "type": "text",
                    "id": f"part_surface_{suffix}_{index}",
                    "session_id": session_id,
                    "message_id": message_id,
                    "text": f"public-{index}",
                },
                created_at=created_at,
            ))
    return user_id, session_id, message_ids


@pytest.mark.asyncio
async def test_regenerate_archives_complete_branch_before_delete_and_keeps_live_projection(monkeypatch):
    import session.surface_log as surface_log

    user_id, session_id, message_ids = await _seed_surface()
    original_append = surface_log.append_surface_change_locked
    observed_before_delete = False

    async def observe_append(db, session_row, **kwargs):
        nonlocal observed_before_delete
        event = await original_append(db, session_row, **kwargs)
        assert await db.get(SessionSurfaceEvent, event.id) is event
        still_live = set((await db.execute(
            select(Message.id).where(Message.id.in_(kwargs["hidden_message_ids"]))
        )).scalars().all())
        assert still_live == set(kwargs["hidden_message_ids"])
        observed_before_delete = True
        return event

    monkeypatch.setattr(surface_log, "append_surface_change_locked", observe_append)
    lease = await reserve_run(
        session_id,
        user_id,
        run_id="run-replacement-1",
    )
    survivor = await delete_messages_from(
        session_id,
        message_ids[1],
        user_id=user_id,
        replacement_run_id="run-replacement-1",
        replacement_generation=lease.generation,
    )

    assert observed_before_delete is True
    assert survivor == message_ids[0]
    live = await get_messages(session_id, user_id=user_id)
    assert [message.id for message in live] == [message_ids[0]]

    async with get_db_session() as db:
        event = (await db.execute(
            select(SessionSurfaceEvent).where(
                SessionSurfaceEvent.session_id == session_id
            )
        )).scalar_one()
    assert event.sequence == 1
    assert event.kind == "regenerate"
    assert event.anchor_message_id == message_ids[1]
    assert event.replacement_run_id == "run-replacement-1"
    assert event.replacement_generation == lease.generation
    assert event.hidden_message_ids == message_ids[1:]
    snapshot_messages = event.public_snapshot["messages"]
    assert [message["id"] for message in snapshot_messages] == message_ids[1:]
    assert [message["parts"][0]["data"]["text"] for message in snapshot_messages] == [
        "public-1",
        "public-2",
        "public-3",
    ]
    assert all(
        "canonical_tool_id" not in message["parts"][0]["data"]
        and "provider_binding_digest" not in message["parts"][0]["data"]
        for message in snapshot_messages
    )
    assert await lease.release(session_status="idle") is True


@pytest.mark.asyncio
async def test_dismiss_events_have_complete_hidden_ids_and_monotonic_session_sequence():
    user_id, session_id, message_ids = await _seed_surface()

    assert await delete_failed_turn(
        session_id,
        message_ids[1],
        user_id=user_id,
    ) == 2
    assert await delete_failed_turn(
        session_id,
        message_ids[3],
        user_id=user_id,
    ) == 2

    async with get_db_session() as db:
        events = list((await db.execute(
            select(SessionSurfaceEvent).where(
                SessionSurfaceEvent.session_id == session_id
            ).order_by(SessionSurfaceEvent.sequence)
        )).scalars().all())
    assert [event.sequence for event in events] == [1, 2]
    assert [event.kind for event in events] == ["dismiss", "dismiss"]
    assert events[0].hidden_message_ids == message_ids[:2]
    assert events[1].hidden_message_ids == message_ids[2:]
    assert [
        message["id"] for message in events[0].public_snapshot["messages"]
    ] == message_ids[:2]
    assert [
        message["id"] for message in events[1].public_snapshot["messages"]
    ] == message_ids[2:]
    assert await get_messages(session_id, user_id=user_id) == []
    assert (await verify_agent_event_parity(
        session_id,
        user_id=user_id,
        require_closed=True,
    )).ok is True


@pytest.mark.asyncio
async def test_surface_event_and_live_delete_roll_back_as_one_transaction(monkeypatch):
    import session.internal_parts as internal_parts

    user_id, session_id, message_ids = await _seed_surface()

    async def fail_after_archive(*_args, **_kwargs):
        raise RuntimeError("injected delete failure")

    monkeypatch.setattr(
        internal_parts,
        "delete_internal_parts_for_messages_locked",
        fail_after_archive,
    )
    lease = await reserve_run(
        session_id,
        user_id,
        run_id="run-rollback",
    )
    with pytest.raises(RuntimeError, match="injected delete failure"):
        await delete_messages_from(
            session_id,
            message_ids[1],
            user_id=user_id,
            replacement_run_id="run-rollback",
            replacement_generation=lease.generation,
        )

    async with get_db_session() as db:
        event_count = (await db.execute(
            select(func.count()).select_from(SessionSurfaceEvent).where(
                SessionSurfaceEvent.session_id == session_id
            )
        )).scalar_one()
        live_ids = list((await db.execute(
            select(Message.id).where(
                Message.session_id == session_id
            ).order_by(Message.created_at, Message.id)
        )).scalars().all())
    assert event_count == 0
    assert live_ids == message_ids
    assert await lease.release(session_status="idle") is True
