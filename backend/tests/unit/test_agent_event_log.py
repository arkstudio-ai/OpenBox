"""Canonical Agent events reconstruct and verify the public SQL read model."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from agent.driver import LeaseLostError, reserve_run
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
from db.models.user import User
from models.message import (
    MessageInfo,
    MessageRole,
    StepFinishPart,
    StepStartPart,
    TextPart,
    TokenUsage,
    ToolPartData,
    ToolStatus,
)
from session.agent_event_log import (
    AgentEventProjectionError,
    bootstrap_agent_event_log,
    project_agent_events,
    verify_agent_event_parity,
)
from session.session import (
    create_assistant_message,
    create_user_message,
    get_messages,
    save_part,
    set_message_reaction,
    update_message_info,
)


async def _seed_session() -> tuple[str, str]:
    suffix = uuid4().hex[:12]
    user_id = f"event_user_{suffix}"
    project_id = f"event_project_{suffix}"
    session_id = f"event_session_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=user_id, username=user_id, created_at=now, updated_at=now))
        db.add(Project(
            id=project_id,
            user_id=user_id,
            name=project_id,
            slug=project_id,
            created_at=now,
            updated_at=now,
        ))
        db.add(Session(
            id=session_id,
            user_id=user_id,
            project_id=project_id,
            title="Agent event test",
            agent="build",
            model="openai/base",
            status="idle",
            token_usage={},
            tool_exposure_state={},
            created_at=now,
            updated_at=now,
        ))
    return user_id, session_id


@pytest.mark.asyncio
async def test_fenced_agent_lifecycle_projects_complete_public_surface_without_provider_secrets():
    user_id, session_id = await _seed_session()
    lease = await reserve_run(session_id, user_id, run_id="run-canonical-events")
    fence = (session_id, lease.run_id, lease.generation)
    user = await create_user_message(
        session_id,
        "Build the report",
        model="openai/requested",
        user_id=user_id,
        run_fence=fence,
    )
    await lease.bind_trigger_message(user.id)
    assistant = await create_assistant_message(
        session_id,
        user.id,
        model_id="anthropic/claude-sonnet",
        agent="build",
        user_id=user_id,
        run_fence=fence,
    )
    await save_part(
        StepStartPart(
            step=1,
            session_id=session_id,
            message_id=assistant.id,
        ),
        is_new=True,
        user_id=user_id,
        run_fence=fence,
    )
    tool = ToolPartData(
        tool="web_search",
        status=ToolStatus.PENDING,
        input={"query": "public query"},
        call_id="provider-call-1",
        metadata={
            "api_key": "sk-provider-must-not-appear",
            "AWS_SECRET_ACCESS_KEY": "aws-provider-must-not-appear",
            "GOOGLE_API_KEY": "google-provider-must-not-appear",
            "x-api-key": "header-provider-must-not-appear",
            "public": "kept",
        },
        session_id=session_id,
        message_id=assistant.id,
        canonical_tool_id="builtin:web_search",
        wire_tool_name="web_search",
        provider_binding_digest="a" * 64,
        provider_dialect="responses",
        stream_seq=1,
    )
    await save_part(
        tool,
        is_new=True,
        user_id=user_id,
        run_fence=fence,
    )
    completed = tool.model_copy(update={
        "status": ToolStatus.COMPLETED,
        "output": "done",
    })
    await save_part(completed, user_id=user_id, run_fence=fence)
    await save_part(
        TextPart(
            text="Finished",
            channel="final",
            session_id=session_id,
            message_id=assistant.id,
        ),
        is_new=True,
        user_id=user_id,
        run_fence=fence,
    )
    await save_part(
        StepFinishPart(
            step=1,
            input_tokens=10,
            output_tokens=4,
            session_id=session_id,
            message_id=assistant.id,
        ),
        is_new=True,
        user_id=user_id,
        run_fence=fence,
    )
    await update_message_info(
        MessageInfo(
            id=assistant.id,
            sessionID=session_id,
            role=MessageRole.ASSISTANT,
            model_id="anthropic/claude-sonnet",
            finish="stop",
            tokens=TokenUsage(input=10, output=4, total=14),
            error={
                "message": (
                    "upstream Authorization: Bearer sk-another-secret-123456 "
                    "AWS_SECRET_ACCESS_KEY=aws-secret-in-text failed"
                )
            },
        ),
        user_id=user_id,
        run_fence=fence,
    )

    report = await verify_agent_event_parity(
        session_id,
        user_id=user_id,
        require_closed=True,
    )
    assert report.ok is True
    assert report.sequence_contiguous is True
    assert report.projection_matches is True
    assert report.balanced is True

    async with get_db_session() as db:
        events = list((await db.execute(
            select(AgentEvent).where(
                AgentEvent.session_id == session_id
            ).order_by(AgentEvent.sequence)
        )).scalars().all())
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    kinds = {event.kind for event in events}
    assert {
        "surface.seed",
        "turn.started",
        "message.created",
        "step.started",
        "tool.called",
        "tool.result",
        "part.created",
        "step.finished",
        "message.updated",
        "turn.finished",
    } <= kinds
    assert all(
        event.run_id == "run-canonical-events"
        and event.generation == lease.generation
        for event in events
        if event.kind != "surface.seed"
    )
    encoded = json.dumps([event.payload for event in events], sort_keys=True)
    assert "sk-provider-must-not-appear" not in encoded
    assert "sk-another-secret-123456" not in encoded
    assert "aws-secret-in-text" not in encoded
    assert "aws-provider-must-not-appear" not in encoded
    assert "google-provider-must-not-appear" not in encoded
    assert "header-provider-must-not-appear" not in encoded
    # Canonical private sidecars live in events but are never returned by the
    # public projector/API Surface.
    public_encoded = json.dumps(project_agent_events(events), sort_keys=True)
    assert "provider_binding_digest" not in public_encoded
    assert "canonical_tool_id" not in public_encoded
    assert "provider_binding_digest" in encoded
    assert "canonical_tool_id" in encoded
    assert "anthropic/claude-sonnet" in encoded

    reconnected = await get_messages(session_id, user_id=user_id)
    assistant_snapshot = next(message for message in reconnected if message.id == assistant.id)
    assert assistant_snapshot.model == "anthropic/claude-sonnet"
    assert await lease.release(session_status="idle") is True


@pytest.mark.asyncio
async def test_message_state_can_return_to_an_earlier_value_without_losing_parity():
    user_id, session_id = await _seed_session()
    user = await create_user_message(session_id, "hello", user_id=user_id)
    assistant = await create_assistant_message(
        session_id,
        user.id,
        model_id="openai/loopback",
        user_id=user_id,
    )
    await update_message_info(
        MessageInfo(
            id=assistant.id,
            sessionID=session_id,
            role=MessageRole.ASSISTANT,
            finish="stop",
        ),
        user_id=user_id,
    )

    await set_message_reaction(assistant.id, session_id, "up", user_id=user_id)
    await set_message_reaction(assistant.id, session_id, "down", user_id=user_id)
    await set_message_reaction(assistant.id, session_id, "up", user_id=user_id)

    report = await verify_agent_event_parity(
        session_id,
        user_id=user_id,
        require_closed=True,
    )
    assert report.ok is True
    assert report.projection_matches is True
    reconnected = await get_messages(session_id, user_id=user_id)
    assert next(message for message in reconnected if message.id == assistant.id).reaction == "up"


@pytest.mark.asyncio
async def test_event_append_and_part_insert_roll_back_together(monkeypatch):
    user_id, session_id = await _seed_session()
    now = datetime.now(timezone.utc)
    message_id = f"message_{uuid4().hex[:12]}"
    async with get_db_session() as db:
        db.add(Message(
            id=message_id,
            session_id=session_id,
            user_id=user_id,
            role="assistant",
            created_at=now,
        ))

    async def fail_append(*_args, **_kwargs):
        raise RuntimeError("event append failed")

    monkeypatch.setattr(
        "session.agent_event_log.append_part_event_locked",
        fail_append,
    )
    part = TextPart(
        text="must roll back",
        session_id=session_id,
        message_id=message_id,
    )
    with pytest.raises(RuntimeError, match="event append failed"):
        await save_part(part, is_new=True, user_id=user_id)

    async with get_db_session() as db:
        assert await db.get(Part, part.id) is None
        count = (await db.execute(
            select(func.count()).select_from(AgentEvent).where(
                AgentEvent.session_id == session_id
            )
        )).scalar_one()
    assert count == 0


@pytest.mark.asyncio
async def test_repeated_part_create_is_read_model_and_event_idempotent():
    user_id, session_id = await _seed_session()
    message_id = f"message_{uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(Message(
            id=message_id,
            session_id=session_id,
            user_id=user_id,
            role="assistant",
            finish="stop",
            created_at=now,
        ))
    part = TextPart(
        id=f"part_{uuid4().hex[:12]}",
        text="same payload",
        session_id=session_id,
        message_id=message_id,
    )
    await save_part(part, is_new=True, user_id=user_id)
    async with get_db_session() as db:
        first_count = (await db.execute(
            select(func.count()).select_from(AgentEvent).where(
                AgentEvent.session_id == session_id
            )
        )).scalar_one()
    await save_part(part, is_new=True, user_id=user_id)
    async with get_db_session() as db:
        second_count = (await db.execute(
            select(func.count()).select_from(AgentEvent).where(
                AgentEvent.session_id == session_id
            )
        )).scalar_one()
        part_count = (await db.execute(
            select(func.count()).select_from(Part).where(Part.id == part.id)
        )).scalar_one()
    assert second_count == first_count
    assert part_count == 1
    assert (await verify_agent_event_parity(
        session_id,
        user_id=user_id,
        require_closed=True,
    )).ok is True


@pytest.mark.asyncio
async def test_strict_verifier_rejects_open_retry_style_tail_but_parity_still_matches():
    user_id, session_id = await _seed_session()
    lease = await reserve_run(session_id, user_id, run_id="run-open-tail")
    fence = (session_id, lease.run_id, lease.generation)
    user = await create_user_message(
        session_id,
        "retry me",
        user_id=user_id,
        run_fence=fence,
    )
    assistant = await create_assistant_message(
        session_id,
        user.id,
        model_id="anthropic/claude-sonnet",
        user_id=user_id,
        run_fence=fence,
    )
    await save_part(
        StepStartPart(
            step=1,
            session_id=session_id,
            message_id=assistant.id,
        ),
        is_new=True,
        user_id=user_id,
        run_fence=fence,
    )

    parity_only = await verify_agent_event_parity(
        session_id,
        user_id=user_id,
        require_closed=False,
    )
    strict = await verify_agent_event_parity(
        session_id,
        user_id=user_id,
        require_closed=True,
    )
    assert parity_only.ok is True
    assert parity_only.projection_matches is True
    assert strict.ok is False
    assert strict.open_turn_ids
    assert strict.open_step_ids == (f"{assistant.id}:1",)
    assert strict.unfinished_message_ids == (assistant.id,)
    assert await lease.release(session_status="error") is True


@pytest.mark.asyncio
async def test_legacy_surface_can_be_bootstrapped_and_sequence_gap_fails_closed():
    user_id, session_id = await _seed_session()
    message_id = f"message_{uuid4().hex[:12]}"
    part_id = f"part_{uuid4().hex[:12]}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(Message(
            id=message_id,
            session_id=session_id,
            user_id=user_id,
            role="assistant",
            model_id="openai/legacy",
            finish="stop",
            created_at=now,
        ))
        db.add(Part(
            id=part_id,
            message_id=message_id,
            session_id=session_id,
            user_id=user_id,
            type="text",
            data={
                "type": "text",
                "id": part_id,
                "text": "legacy",
                "session_id": session_id,
                "message_id": message_id,
            },
            created_at=now,
        ))
    assert await bootstrap_agent_event_log(session_id, user_id=user_id) is not None
    report = await verify_agent_event_parity(session_id, user_id=user_id)
    assert report.ok is True

    async with get_db_session() as db:
        event = (await db.execute(
            select(AgentEvent).where(AgentEvent.session_id == session_id)
        )).scalar_one()
    broken = {
        column.name: getattr(event, column.name)
        for column in AgentEvent.__table__.columns
    }
    broken["sequence"] = 2
    with pytest.raises(AgentEventProjectionError, match="sequence gap"):
        project_agent_events([broken])


@pytest.mark.asyncio
async def test_stale_generation_cannot_write_read_model_or_shadow_event():
    user_id, session_id = await _seed_session()
    stale = await reserve_run(session_id, user_id, run_id="run-stale")
    stale_fence = (session_id, stale.run_id, stale.generation)
    assert await stale.release(session_status="idle") is True
    current = await reserve_run(session_id, user_id, run_id="run-current")

    with pytest.raises(LeaseLostError):
        await create_user_message(
            session_id,
            "must not commit",
            user_id=user_id,
            run_fence=stale_fence,
        )
    async with get_db_session() as db:
        message_count = (await db.execute(
            select(func.count()).select_from(Message).where(
                Message.session_id == session_id
            )
        )).scalar_one()
        event_count = (await db.execute(
            select(func.count()).select_from(AgentEvent).where(
                AgentEvent.session_id == session_id
            )
        )).scalar_one()
    assert message_count == 0
    assert event_count == 0
    assert await current.release(session_status="idle") is True
