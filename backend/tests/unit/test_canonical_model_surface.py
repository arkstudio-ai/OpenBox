"""Canonical Agent events are the only model-context authority."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import delete, select

from agent.driver import LeaseLostError, reserve_run
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.internal_part import InternalPart
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
from db.models.user import User
from models.message import (
    MessageInfo,
    MessageRole,
    StepStartPart,
    TextPart,
    ToolPartData,
    ToolStatus,
)
from session.agent_event_log import (
    AgentEventPrefixDriftError,
    AgentEventProjectionError,
    checkpoint_model_request,
    load_canonical_model_surface,
    model_prompt_shape_digest,
    model_tool_definition_digest,
    model_tool_schema_digest,
    project_agent_events,
    rebuild_sql_read_model_from_events,
    verify_agent_event_parity,
)
from session.internal_parts import (
    PROVIDER_TRANSCRIPT_KIND,
    ProviderCapabilityBinding,
    save_internal_part,
)
from session.fork import fork_session
from session.session import (
    create_assistant_message,
    create_user_message,
    get_messages,
    save_part,
    update_message_info,
)
from session.tool_part_identity import resolve_projected_tool_part_for_replay


async def _seed_session() -> tuple[str, str]:
    suffix = uuid4().hex[:12]
    user_id = f"canonical_user_{suffix}"
    project_id = f"canonical_project_{suffix}"
    session_id = f"canonical_session_{suffix}"
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
            title="canonical",
            agent="build",
            model="openai/test",
            status="idle",
            token_usage={},
            tool_exposure_state={},
            created_at=now,
            updated_at=now,
        ))
    return user_id, session_id


def _binding() -> ProviderCapabilityBinding:
    return ProviderCapabilityBinding(
        provider="openai",
        endpoint="https://api.example.test/v1",
        account_id="acct-test",
        api_version="2026-08-31",
        model="gpt-test",
        dialect="responses",
        beta_headers=("tools-v2",),
    )


async def _closed_tool_turn(
    user_id: str,
    session_id: str,
) -> tuple[str, str, ToolPartData]:
    user = await create_user_message(
        session_id,
        '保留原文 {"api_key":"sk-example-12345678"} 中文😀 literal \\u0000',
        user_id=user_id,
    )
    assistant = await create_assistant_message(
        session_id,
        user.id,
        model_id="openai/test",
        user_id=user_id,
    )
    tool = ToolPartData(
        tool="read",
        status=ToolStatus.COMPLETED,
        input={
            "api_key": "sk-example-12345678",
            "path": "资料/你好.txt",
            "example": r"literal → \\u0000",
        },
        output=r'normal output api_key sk-example-12345678 中文😀 \\u0000',
        metadata={
            "api_key": "sk-real-provider-secret-12345678",
            "public": "kept",
        },
        call_id="call-canonical",
        session_id=session_id,
        message_id=assistant.id,
        canonical_tool_id="builtin:read",
        wire_tool_name="read_original",
        provider_binding_digest="a" * 64,
        provider_dialect="responses",
        stream_seq=0,
    )
    await save_part(tool, is_new=True, user_id=user_id)
    await save_part(
        TextPart(
            text="done",
            channel="final",
            session_id=session_id,
            message_id=assistant.id,
        ),
        is_new=True,
        user_id=user_id,
    )
    assistant.finish = "stop"
    assistant.error = {
        "message": "Authorization: Bearer sk-provider-error-12345678 failed"
    }
    await update_message_info(assistant, user_id=user_id)
    return user.id, assistant.id, tool


@pytest.mark.asyncio
async def test_public_projection_is_exact_but_private_replay_never_leaks():
    user_id, session_id = await _seed_session()
    user_message_id, assistant_id, tool = await _closed_tool_turn(user_id, session_id)

    model = await load_canonical_model_surface(
        session_id, user_id=user_id, repair_tail=False,
    )
    user = next(message for message in model.messages if message.id == user_message_id)
    assert user.parts[0].text == (
        '保留原文 {"api_key":"sk-example-12345678"} '
        '中文😀 literal \\u0000'
    )
    assistant = next(message for message in model.messages if message.id == assistant_id)
    projected_tool = next(part for part in assistant.parts if part.type == "tool")
    assert projected_tool.input == tool.input
    assert projected_tool.output == tool.output
    assert projected_tool.metadata == {"public": "kept"}
    assert projected_tool.canonical_tool_id == "builtin:read"
    assert projected_tool.wire_tool_name == "read_original"

    async with get_db_session() as db:
        events = list((await db.execute(select(AgentEvent).where(
            AgentEvent.session_id == session_id,
        ).order_by(AgentEvent.sequence))).scalars().all())
        row = await db.get(Message, assistant_id)
        assert row is not None
        assert "sk-provider-error" not in json.dumps(row.error)
        tool_row = await db.get(Part, tool.id)
        assert tool_row is not None
        stored_tool = json.dumps(tool_row.data, ensure_ascii=False)
        assert "sk-real-provider-secret" not in stored_tool
        assert "sk-example-12345678" in stored_tool
        assert "资料/你好.txt" in stored_tool
    public = project_agent_events(events)
    encoded = json.dumps(public, ensure_ascii=False, sort_keys=True)
    assert "canonical_tool_id" not in encoded
    assert "provider_binding_digest" not in encoded
    assert "sk-real-provider-secret" not in encoded
    assert 'sk-example-12345678' in encoded
    assert "中文😀" in encoded


@pytest.mark.asyncio
async def test_hidden_identity_replays_same_binding_and_remaps_provider_switch():
    user_id, session_id = await _seed_session()
    _, assistant_id, _ = await _closed_tool_turn(user_id, session_id)
    model = await load_canonical_model_surface(
        session_id, user_id=user_id, repair_tail=False,
    )
    assistant = next(message for message in model.messages if message.id == assistant_id)
    part = next(item for item in assistant.parts if item.type == "tool")
    same = resolve_projected_tool_part_for_replay(
        part=part,
        current_binding_digest="a" * 64,
        current_provider_dialect="responses",
        current_wire_by_canonical={"builtin:read": "read_rebuilt"},
    )
    switched = resolve_projected_tool_part_for_replay(
        part=part,
        current_binding_digest="b" * 64,
        current_provider_dialect="litellm",
        current_wire_by_canonical={"builtin:read": "read_switched"},
    )
    assert same.wire_tool_name == "read_original"
    assert switched.wire_tool_name == "read_switched"


@pytest.mark.asyncio
async def test_events_rebuild_deleted_sql_surface_and_provider_replay_equivalently():
    user_id, session_id = await _seed_session()
    _, assistant_id, _ = await _closed_tool_turn(user_id, session_id)
    binding = _binding()
    stored = await save_internal_part(
        session_id=session_id,
        user_id=user_id,
        message_id=assistant_id,
        kind=PROVIDER_TRANSCRIPT_KIND,
        data={
            "type": "tool_search_call",
            "execution": "server",
            "continuation_token": "page-2",
            "authorization": "Bearer sk-never-store-12345678",
        },
        binding=binding,
        response_chain_id="response-chain-1",
        stream_seq=1,
        idempotency_key="native-1",
    )
    before_public = [message.model_dump() for message in await get_messages(
        session_id, user_id=user_id,
    )]
    before_model = await load_canonical_model_surface(
        session_id, user_id=user_id, repair_tail=False,
    )
    before_replay = before_model.provider_replay_for(binding.digest())
    assert before_replay[assistant_id][0]["data"] == {
        "type": "tool_search_call",
        "execution": "server",
        "continuation_token": "page-2",
    }
    async with get_db_session() as db:
        transcript_event = (await db.execute(select(AgentEvent).where(
            AgentEvent.session_id == session_id,
            AgentEvent.kind == "provider.transcript",
        ))).scalar_one()
    raw_private_event = json.dumps(transcript_event.payload, sort_keys=True)
    assert "authorization" not in raw_private_event.casefold()
    assert "sk-never-store" not in raw_private_event
    assert "continuation_token" in raw_private_event

    async with get_db_session() as db:
        await db.execute(delete(InternalPart).where(InternalPart.session_id == session_id))
        await db.execute(delete(Part).where(Part.session_id == session_id))
        await db.execute(delete(Message).where(Message.session_id == session_id))
    await rebuild_sql_read_model_from_events(session_id, user_id=user_id)

    after_public = [message.model_dump() for message in await get_messages(
        session_id, user_id=user_id,
    )]
    after_model = await load_canonical_model_surface(
        session_id, user_id=user_id, repair_tail=False,
    )
    assert after_public == before_public
    assert after_model.provider_replay_for(binding.digest()) == before_replay
    assert [message.model_dump() for message in after_model.messages] == [
        message.model_dump() for message in before_model.messages
    ]
    deduped = await save_internal_part(
        session_id=session_id,
        user_id=user_id,
        message_id=assistant_id,
        kind=PROVIDER_TRANSCRIPT_KIND,
        data={
            "type": "tool_search_call",
            "execution": "server",
            "continuation_token": "page-2",
            "authorization": "Bearer sk-never-store-12345678",
        },
        binding=binding,
        response_chain_id="response-chain-1",
        stream_seq=1,
        idempotency_key="native-1",
    )
    assert deduped.id == stored.id


def test_tool_schema_digest_supports_runtime_tool_info_deterministically():
    from pydantic import BaseModel, Field

    from tool.tool import ToolInfo, ToolResult

    class Args(BaseModel):
        path: str = Field(description="路径😀")

    async def execute(_args, _ctx):
        return ToolResult(output="ok")

    tool = ToolInfo(
        id="read",
        description="Read a UTF-8 path",
        parameters=Args,
        execute=execute,
    )
    first = model_tool_schema_digest({"read": tool})
    second = model_tool_schema_digest({"read": tool})
    changed = model_tool_schema_digest({
        "read": ToolInfo(
            id="read",
            description="Changed",
            parameters=Args,
            execute=execute,
        )
    })
    assert first == second
    assert len(first) == 64
    assert changed != first


@pytest.mark.asyncio
async def test_fork_imports_private_replay_from_events_and_advances_read_model_order():
    user_id, session_id = await _seed_session()
    _, assistant_id, _ = await _closed_tool_turn(user_id, session_id)
    binding = _binding()
    await save_internal_part(
        session_id=session_id,
        user_id=user_id,
        message_id=assistant_id,
        kind=PROVIDER_TRANSCRIPT_KIND,
        data={"type": "tool_search_call", "continuation_token": "fork-page"},
        binding=binding,
        response_chain_id="fork-chain",
        stream_seq=1,
        idempotency_key="fork-source",
    )
    child = await fork_session(
        session_id,
        up_to_message_id=assistant_id,
        user_id=user_id,
    )
    child_surface = await load_canonical_model_surface(
        child.id,
        user_id=user_id,
        repair_tail=False,
    )
    child_assistant = next(
        message for message in child_surface.messages
        if message.role == MessageRole.ASSISTANT
    )
    assert child_surface.provider_replay_for(binding.digest())[
        child_assistant.id
    ][0]["data"]["continuation_token"] == "fork-page"
    async with get_db_session() as db:
        child_rows = list((await db.execute(select(InternalPart).where(
            InternalPart.session_id == child.id,
        ))).scalars().all())
    assert len(child_rows) == 1
    assert child_rows[0].origin_seq == 1

    next_record = await save_internal_part(
        session_id=child.id,
        user_id=user_id,
        message_id=child_assistant.id,
        kind=PROVIDER_TRANSCRIPT_KIND,
        data={"type": "tool_search_call", "continuation_token": "fork-next"},
        binding=binding,
        response_chain_id="fork-chain-2",
        stream_seq=2,
        idempotency_key="fork-child-next",
    )
    assert next_record.origin_seq == 2


@pytest.mark.asyncio
async def test_model_requested_cites_the_same_immutable_prefix_and_conflicts_on_reuse():
    user_id, session_id = await _seed_session()
    lease = await reserve_run(session_id, user_id, run_id="run-checkpoint")
    fence = (session_id, lease.run_id, lease.generation)
    user = await create_user_message(
        session_id, "checkpoint", user_id=user_id, run_fence=fence,
    )
    await lease.bind_trigger_message(user.id)
    assistant = await create_assistant_message(
        session_id, user.id, model_id="openai/test", user_id=user_id,
        run_fence=fence,
    )
    candidate = await load_canonical_model_surface(
        session_id,
        user_id=user_id,
        run_fence=fence,
    )
    snapshot = await checkpoint_model_request(
        session_id,
        user_id=user_id,
        run_fence=fence,
        request_id="request-1",
        model_id="openai/test",
        provider_binding_digest="a" * 64,
        tool_schema_digest="b" * 64,
        prompt_shape_digest="c" * 64,
        expected_event_sequence=candidate.event_sequence,
        expected_event_digest=candidate.event_digest,
        turn_id=user.id,
        step_id="run-checkpoint:1:1",
        message_id=assistant.id,
    )
    async with get_db_session() as db:
        checkpoint = (await db.execute(select(AgentEvent).where(
            AgentEvent.session_id == session_id,
            AgentEvent.kind == "model.requested",
        ))).scalar_one()
    assert checkpoint.payload["event_sequence"] == snapshot.event_sequence
    assert checkpoint.payload["event_digest"] == snapshot.event_digest
    assert checkpoint.sequence == snapshot.event_sequence + 1

    await save_part(
        TextPart(
            text="new event",
            session_id=session_id,
            message_id=assistant.id,
        ),
        is_new=True,
        user_id=user_id,
        run_fence=fence,
    )
    current = await load_canonical_model_surface(
        session_id,
        user_id=user_id,
        run_fence=fence,
    )
    with pytest.raises(AgentEventProjectionError, match="idempotency conflict"):
        await checkpoint_model_request(
            session_id,
            user_id=user_id,
            run_fence=fence,
            request_id="request-1",
            model_id="openai/test",
            provider_binding_digest="a" * 64,
            tool_schema_digest="b" * 64,
            prompt_shape_digest="c" * 64,
            expected_event_sequence=current.event_sequence,
            expected_event_digest=current.event_digest,
            turn_id=user.id,
            step_id="run-checkpoint:1:1",
            message_id=assistant.id,
        )
    await lease.release(session_status="error")


@pytest.mark.asyncio
async def test_model_request_checkpoint_rejects_prefix_drift_without_append():
    user_id, session_id = await _seed_session()
    lease = await reserve_run(session_id, user_id, run_id="run-cas-drift")
    fence = (session_id, lease.run_id, lease.generation)
    user = await create_user_message(
        session_id, "before drift", user_id=user_id, run_fence=fence,
    )
    await lease.bind_trigger_message(user.id)
    assistant = await create_assistant_message(
        session_id, user.id, model_id="openai/test", user_id=user_id,
        run_fence=fence,
    )
    candidate = await load_canonical_model_surface(
        session_id,
        user_id=user_id,
        run_fence=fence,
    )
    await save_part(
        TextPart(
            text="arrived while request was built",
            session_id=session_id,
            message_id=assistant.id,
        ),
        is_new=True,
        user_id=user_id,
        run_fence=fence,
    )

    with pytest.raises(AgentEventPrefixDriftError, match="prefix drift"):
        await checkpoint_model_request(
            session_id,
            user_id=user_id,
            run_fence=fence,
            request_id="request-drift",
            model_id="openai/test",
            provider_binding_digest="a" * 64,
            tool_schema_digest="b" * 64,
            prompt_shape_digest="c" * 64,
            expected_event_sequence=candidate.event_sequence,
            expected_event_digest=candidate.event_digest,
            turn_id=user.id,
            message_id=assistant.id,
        )
    async with get_db_session() as db:
        checkpoints = list((await db.execute(select(AgentEvent).where(
            AgentEvent.session_id == session_id,
            AgentEvent.kind == "model.requested",
        ))).scalars().all())
    assert checkpoints == []
    await lease.release(session_status="error")


@pytest.mark.asyncio
async def test_model_request_checkpoint_shape_digest_is_exact_and_secret_free():
    user_id, session_id = await _seed_session()
    lease = await reserve_run(session_id, user_id, run_id="run-shape")
    fence = (session_id, lease.run_id, lease.generation)
    user = await create_user_message(
        session_id, "shape", user_id=user_id, run_fence=fence,
    )
    await lease.bind_trigger_message(user.id)
    assistant = await create_assistant_message(
        session_id, user.id, model_id="openai/test", user_id=user_id,
        run_fence=fence,
    )
    candidate = await load_canonical_model_surface(
        session_id,
        user_id=user_id,
        run_fence=fence,
    )
    tool_digest = model_tool_definition_digest([{
        "type": "function",
        "function": {
            "name": "demo",
            "description": "api_key sk-tool-example",
            "parameters": {"type": "object"},
        },
    }])
    system = ["secret system sk-system-example"]
    messages = [{
        "role": "user",
        "content": "中文 🚀 api_key sk-user-example",
        "_images": ["data:image/png;base64,raw-image-secret"],
    }]
    shape_digest = model_prompt_shape_digest(
        system=system,
        messages=messages,
        model_id="openai/test",
        provider_binding_digest="a" * 64,
        tool_schema_digest=tool_digest,
        tool_choice="required",
        variant="high",
    )
    await checkpoint_model_request(
        session_id,
        user_id=user_id,
        run_fence=fence,
        request_id="request-shape",
        model_id="openai/test",
        provider_binding_digest="a" * 64,
        tool_schema_digest=tool_digest,
        prompt_shape_digest=shape_digest,
        expected_event_sequence=candidate.event_sequence,
        expected_event_digest=candidate.event_digest,
        turn_id=user.id,
        message_id=assistant.id,
    )
    async with get_db_session() as db:
        checkpoint = (await db.execute(select(AgentEvent).where(
            AgentEvent.session_id == session_id,
            AgentEvent.kind == "model.requested",
        ))).scalar_one()
    assert checkpoint.payload["prompt_shape_digest"] == shape_digest
    encoded = json.dumps(checkpoint.payload, ensure_ascii=False)
    for secret in (
        "sk-system-example",
        "sk-user-example",
        "sk-tool-example",
        "raw-image-secret",
        "api_key",
    ):
        assert secret not in encoded
    await lease.release(session_status="error")


@pytest.mark.asyncio
async def test_stale_generation_cannot_append_model_request_checkpoint():
    user_id, session_id = await _seed_session()
    stale = await reserve_run(session_id, user_id, run_id="run-stale-checkpoint")
    stale_fence = (session_id, stale.run_id, stale.generation)
    user = await create_user_message(
        session_id, "stale", user_id=user_id, run_fence=stale_fence,
    )
    await stale.bind_trigger_message(user.id)
    assistant = await create_assistant_message(
        session_id, user.id, model_id="openai/test", user_id=user_id,
        run_fence=stale_fence,
    )
    candidate = await load_canonical_model_surface(
        session_id,
        user_id=user_id,
        run_fence=stale_fence,
    )
    assert await stale.release(session_status="idle") is True
    current = await reserve_run(session_id, user_id, run_id="run-current-checkpoint")

    with pytest.raises(LeaseLostError):
        await checkpoint_model_request(
            session_id,
            user_id=user_id,
            run_fence=stale_fence,
            request_id="request-stale",
            model_id="openai/test",
            provider_binding_digest="a" * 64,
            tool_schema_digest="b" * 64,
            prompt_shape_digest="c" * 64,
            expected_event_sequence=candidate.event_sequence,
            expected_event_digest=candidate.event_digest,
            turn_id=user.id,
            message_id=assistant.id,
        )
    async with get_db_session() as db:
        checkpoints = list((await db.execute(select(AgentEvent).where(
            AgentEvent.session_id == session_id,
            AgentEvent.kind == "model.requested",
        ))).scalars().all())
    assert checkpoints == []
    await current.release(session_status="error")


@pytest.mark.asyncio
async def test_idle_open_tail_repairs_pending_running_step_and_missing_assistant():
    user_id, session_id = await _seed_session()
    user = await create_user_message(session_id, "repair tools", user_id=user_id)
    assistant = await create_assistant_message(
        session_id, user.id, model_id="openai/test", user_id=user_id,
    )
    await save_part(
        StepStartPart(step=1, session_id=session_id, message_id=assistant.id),
        is_new=True,
        user_id=user_id,
    )
    for index, status in enumerate((ToolStatus.PENDING, ToolStatus.RUNNING)):
        await save_part(
            ToolPartData(
                tool="read",
                status=status,
                call_id=f"call-{index}",
                session_id=session_id,
                message_id=assistant.id,
            ),
            is_new=True,
            user_id=user_id,
        )
    repaired = await load_canonical_model_surface(session_id, user_id=user_id)
    repaired_assistant = next(
        message for message in repaired.messages if message.id == assistant.id
    )
    assert repaired_assistant.finish == "aborted"
    repaired_tools = [part for part in repaired_assistant.parts if part.type == "tool"]
    assert [part.status.value for part in repaired_tools] == ["error", "error"]
    assert {part.metadata["recovery_code"] for part in repaired_tools} == {
        "tool_not_started", "tool_outcome_unknown",
    }
    assert any(part.type == "step-finish" for part in repaired_assistant.parts)
    assert (await verify_agent_event_parity(
        session_id, user_id=user_id, require_closed=True,
    )).ok is True

    user_id2, session_id2 = await _seed_session()
    orphan = await create_user_message(session_id2, "no assistant", user_id=user_id2)
    repaired2 = await load_canonical_model_surface(session_id2, user_id=user_id2)
    terminal = next(
        message for message in repaired2.messages if message.parent_id == orphan.id
    )
    assert terminal.finish == "aborted"


@pytest.mark.asyncio
async def test_current_exact_generation_open_tail_is_never_repaired():
    user_id, session_id = await _seed_session()
    lease = await reserve_run(session_id, user_id, run_id="run-live-tail")
    fence = (session_id, lease.run_id, lease.generation)
    user = await create_user_message(
        session_id, "still live", user_id=user_id, run_fence=fence,
    )
    await lease.bind_trigger_message(user.id)
    assistant = await create_assistant_message(
        session_id, user.id, model_id="openai/test", user_id=user_id,
        run_fence=fence,
    )
    pending = ToolPartData(
        tool="read",
        status=ToolStatus.PENDING,
        call_id="live-call",
        session_id=session_id,
        message_id=assistant.id,
    )
    await save_part(pending, is_new=True, user_id=user_id, run_fence=fence)
    surface = await load_canonical_model_surface(
        session_id, user_id=user_id, run_fence=fence,
    )
    live = next(message for message in surface.messages if message.id == assistant.id)
    assert next(part for part in live.parts if part.type == "tool").status.value == "pending"
    assert live.finish is None
    await lease.release(session_status="error")


@pytest.mark.asyncio
async def test_unfenced_loader_uses_database_clock_for_live_driver(monkeypatch):
    import session.agent_event_log as event_log

    user_id, session_id = await _seed_session()
    lease = await reserve_run(session_id, user_id, run_id="run-db-clock")
    fence = (session_id, lease.run_id, lease.generation)
    user = await create_user_message(
        session_id, "live under DB clock", user_id=user_id, run_fence=fence,
    )
    await lease.bind_trigger_message(user.id)
    assistant = await create_assistant_message(
        session_id, user.id, model_id="openai/test", user_id=user_id,
        run_fence=fence,
    )
    pending = ToolPartData(
        tool="read",
        status=ToolStatus.PENDING,
        call_id="db-clock-live-call",
        session_id=session_id,
        message_id=assistant.id,
    )
    await save_part(pending, is_new=True, user_id=user_id, run_fence=fence)

    real_datetime = datetime

    class SkewedWorkerDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            value = real_datetime.now(tz or timezone.utc) + timedelta(days=30)
            return value if tz is not None else value.replace(tzinfo=None)

    monkeypatch.setattr(event_log, "datetime", SkewedWorkerDateTime)
    surface = await load_canonical_model_surface(
        session_id,
        user_id=user_id,
        run_fence=None,
    )
    live = next(message for message in surface.messages if message.id == assistant.id)
    assert live.finish is None
    assert next(part for part in live.parts if part.type == "tool").status.value == "pending"
    await lease.release(session_status="error")


def test_sequence_gap_fails_model_projection_without_sql_fallback():
    event = {
        "session_id": "s1",
        "sequence": 2,
        "event_key": hashlib.sha256(b"x").hexdigest(),
        "kind": "surface.seed",
        "run_id": None,
        "generation": None,
        "turn_id": None,
        "step_id": None,
        "message_id": None,
        "part_id": None,
        "tool_call_id": None,
        "payload": {
            "version": 1,
            "surface": {"version": 1, "session_id": "s1", "messages": []},
            "model": {"version": 1, "part_replay": {}, "provider_replay": []},
        },
    }
    from session.agent_event_log import project_model_agent_events

    with pytest.raises(AgentEventProjectionError, match="sequence gap"):
        project_model_agent_events([event])
