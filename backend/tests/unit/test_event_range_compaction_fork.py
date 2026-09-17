"""Stable Event-range contracts for compaction and Session forks."""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
from db.models.user import User
from models.message import (
    CompactionPart,
    MessageRole,
    TextPart,
    ToolPartData,
    ToolStatus,
)
from session.agent_event_log import (
    load_canonical_model_surface,
    prepare_agent_event_write,
    verify_agent_event_parity,
)
from session.compaction import filter_compacted
from session.event_range import (
    StableEventRangeDriftError,
    StableEventRangeError,
    SummaryNotCompactError,
    finalize_compaction_replacement,
    freeze_compaction_event_range,
    freeze_fork_event_range,
    revalidate_stable_event_range_locked,
)
from session.fork import fork_session
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
    user_id = f"range_user_{suffix}"
    project_id = f"range_project_{suffix}"
    session_id = f"range_session_{suffix}"
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
            title="stable range",
            agent="build",
            model="openai/test",
            status="idle",
            token_usage={},
            tool_exposure_state={},
            created_at=now,
            updated_at=now,
        ))
    return user_id, session_id


async def _closed_turn(
    session_id: str,
    user_id: str,
    *,
    prompt: str = "p" * 400,
    answer: str = "done",
) -> tuple[str, str]:
    user = await create_user_message(session_id, prompt, user_id=user_id)
    assistant = await create_assistant_message(
        session_id,
        user.id,
        model_id="openai/test",
        user_id=user_id,
    )
    await save_part(
        TextPart(
            text=answer,
            channel="final",
            session_id=session_id,
            message_id=assistant.id,
        ),
        is_new=True,
        user_id=user_id,
    )
    assistant.finish = "stop"
    await update_message_info(assistant, user_id=user_id)
    return user.id, assistant.id


async def _compaction_attempt(
    session_id: str,
    user_id: str,
) -> tuple[str, str, str]:
    compaction_id = f"message_compact_{uuid4().hex[:10]}"
    part = CompactionPart(
        id=f"part_compact_{uuid4().hex[:10]}",
        auto=False,
        session_id=session_id,
        message_id=compaction_id,
    )
    boundary = await create_user_message(
        session_id,
        "",
        agent="compaction",
        user_id=user_id,
        message_id=compaction_id,
        additional_parts=(part,),
    )
    assistant = await create_assistant_message(
        session_id,
        boundary.id,
        model_id="openai/test",
        agent="compaction",
        user_id=user_id,
    )
    text = TextPart(
        text="",
        session_id=session_id,
        message_id=assistant.id,
    )
    await save_part(text, is_new=True, user_id=user_id)
    return boundary.id, assistant.id, text.id


async def _closed_tool_turn(
    session_id: str,
    user_id: str,
    *,
    output: str,
) -> tuple[str, str, str]:
    user = await create_user_message(session_id, "inspect", user_id=user_id)
    assistant = await create_assistant_message(
        session_id,
        user.id,
        model_id="openai/test",
        user_id=user_id,
    )
    tool = ToolPartData(
        tool="read",
        status=ToolStatus.COMPLETED,
        input={"path": "/workspace/file"},
        output=output,
        call_id=f"call-{uuid4().hex[:10]}",
        session_id=session_id,
        message_id=assistant.id,
    )
    await save_part(tool, is_new=True, user_id=user_id)
    assistant.finish = "stop"
    await update_message_info(assistant, user_id=user_id)
    return user.id, assistant.id, tool.id


@pytest.mark.asyncio
async def test_dangling_late_user_is_not_a_closed_fork_or_compaction_boundary():
    from agent.driver import reserve_run

    user_id, session_id = await _seed_session()
    trigger_id = f"message_{uuid4().hex[:16]}"
    lease = await reserve_run(
        session_id,
        user_id,
        run_id=f"late-{uuid4().hex[:10]}",
        trigger_message_id=trigger_id,
    )
    fence = (session_id, lease.run_id, lease.generation)
    try:
        await create_user_message(
            session_id,
            "first prompt",
            user_id=user_id,
            message_id=trigger_id,
            run_fence=fence,
        )
        first_assistant = await create_assistant_message(
            session_id,
            trigger_id,
            model_id="openai/test",
            user_id=user_id,
            run_fence=fence,
        )
        first_assistant.finish = "stop"
        await update_message_info(
            first_assistant,
            user_id=user_id,
            run_fence=fence,
        )
        late = await create_user_message(
            session_id,
            "late steer",
            user_id=user_id,
            run_fence=fence,
        )
    finally:
        await lease.release(session_status="idle")

    report = await verify_agent_event_parity(
        session_id,
        user_id=user_id,
        require_closed=True,
    )
    assert report.ok is False
    with pytest.raises(StableEventRangeError, match="no complete closed turn"):
        await freeze_fork_event_range(
            session_id,
            user_id=user_id,
            up_to_message_id=first_assistant.id,
        )
    with pytest.raises(StableEventRangeError, match="no complete closed turn"):
        await freeze_fork_event_range(
            session_id,
            user_id=user_id,
            up_to_message_id=None,
        )

    boundary_id, _assistant_id, _text_id = await _compaction_attempt(
        session_id,
        user_id,
    )
    with pytest.raises(StableEventRangeError, match="no complete closed turn"):
        await freeze_compaction_event_range(
            session_id,
            user_id=user_id,
            compaction_user_id=boundary_id,
            requested_tail_start_id=late.id,
            run_fence=None,
        )


@pytest.mark.asyncio
async def test_fork_range_rejects_user_middle_assistant_and_open_tail():
    user_id, session_id = await _seed_session()
    user = await create_user_message(session_id, "use a tool", user_id=user_id)
    middle = await create_assistant_message(
        session_id, user.id, model_id="openai/test", user_id=user_id
    )
    middle.finish = "tool_calls"
    await update_message_info(middle, user_id=user_id)
    final = await create_assistant_message(
        session_id, user.id, model_id="openai/test", user_id=user_id
    )
    final.finish = "stop"
    await update_message_info(final, user_id=user_id)
    open_user = await create_user_message(session_id, "still running", user_id=user_id)
    open_assistant = await create_assistant_message(
        session_id, open_user.id, model_id="openai/test", user_id=user_id
    )

    with pytest.raises(StableEventRangeError, match="terminal Assistant"):
        await freeze_fork_event_range(
            session_id, user_id=user_id, up_to_message_id=user.id
        )
    with pytest.raises(StableEventRangeError, match="terminal Assistant"):
        await freeze_fork_event_range(
            session_id, user_id=user_id, up_to_message_id=middle.id
        )
    with pytest.raises(StableEventRangeError, match="terminal Assistant"):
        await freeze_fork_event_range(
            session_id, user_id=user_id, up_to_message_id=open_assistant.id
        )

    frozen = await freeze_fork_event_range(
        session_id, user_id=user_id, up_to_message_id=None
    )
    assert frozen.covered_message_ids == (user.id, middle.id, final.id)
    assert open_user.id not in frozen.covered_message_ids
    async with get_db_session() as db:
        open_start = (await db.execute(select(AgentEvent.sequence).where(
            AgentEvent.session_id == session_id,
            AgentEvent.kind == "turn.started",
            AgentEvent.message_id == open_user.id,
        ))).scalar_one()
    assert frozen.end_sequence < open_start

    # An append concerning the excluded open turn cannot invalidate the
    # closed prefix. Covered-state mutations remain fail-closed below.
    await set_message_reaction(
        open_assistant.id,
        session_id,
        "up",
        user_id=user_id,
    )
    async with get_db_session() as db:
        row = await prepare_agent_event_write(
            db,
            session_id=session_id,
            user_id=user_id,
            run_fence=None,
        )
        selected = await revalidate_stable_event_range_locked(db, row, frozen)
    assert tuple(message["id"] for message in selected) == frozen.covered_message_ids


@pytest.mark.asyncio
async def test_covered_surface_mutation_after_freeze_fails_cas():
    user_id, session_id = await _seed_session()
    _, assistant_id = await _closed_turn(session_id, user_id)
    frozen = await freeze_fork_event_range(
        session_id, user_id=user_id, up_to_message_id=assistant_id
    )
    await set_message_reaction(
        assistant_id,
        session_id,
        "up",
        user_id=user_id,
    )
    async with get_db_session() as db:
        row = await prepare_agent_event_write(
            db,
            session_id=session_id,
            user_id=user_id,
            run_fence=None,
        )
        with pytest.raises(StableEventRangeDriftError, match="changed"):
            await revalidate_stable_event_range_locked(db, row, frozen)


@pytest.mark.asyncio
async def test_compaction_commits_descriptor_summary_and_replacement_atomically():
    user_id, session_id = await _seed_session()
    source_ids = await _closed_turn(session_id, user_id)
    boundary_id, assistant_id, text_id = await _compaction_attempt(
        session_id, user_id
    )
    frozen = await freeze_compaction_event_range(
        session_id,
        user_id=user_id,
        compaction_user_id=boundary_id,
        requested_tail_start_id=None,
        run_fence=None,
    )
    assert frozen.source.covered_message_ids == source_ids

    replacement_sequence = await finalize_compaction_replacement(
        frozen=frozen.source,
        user_id=user_id,
        compaction_user_id=boundary_id,
        assistant_message_id=assistant_id,
        text_part_id=text_id,
        summary_text="short summary",
        tail_start_id=frozen.tail_start_id,
        source_token_count=100,
        summary_token_count=3,
        model_id="openai/test",
        usage={"input": 10, "output": 3, "total": 13},
        run_fence=None,
    )

    messages = await get_messages(session_id, user_id=user_id)
    boundary = next(message for message in messages if message.id == boundary_id)
    summary = next(message for message in messages if message.id == assistant_id)
    descriptor = next(
        part.model_dump() if hasattr(part, "model_dump") else part
        for part in boundary.parts
        if getattr(part, "type", None) == "compaction"
        or (isinstance(part, dict) and part.get("type") == "compaction")
    )
    assert summary.summary is True
    assert summary.finish == "stop"
    assert descriptor["source_event_start"] == frozen.source.start_sequence
    assert descriptor["source_event_end"] == frozen.source.end_sequence
    assert descriptor["source_event_digest"] == frozen.source.canonical_digest
    assert descriptor["covered_message_ids"] == list(source_ids)

    async with get_db_session() as db:
        replacement = (await db.execute(select(AgentEvent).where(
            AgentEvent.session_id == session_id,
            AgentEvent.kind == "surface.replacement",
        ))).scalar_one()
    assert replacement.sequence == replacement_sequence
    assert replacement.payload["source"]["canonical_digest"] == frozen.source.canonical_digest
    assert [message.id for message in await filter_compacted(messages)] == [
        boundary_id,
        assistant_id,
    ]
    canonical = await load_canonical_model_surface(
        session_id,
        user_id=user_id,
        repair_tail=False,
    )
    assert [message.id for message in canonical.messages] == [
        boundary_id,
        assistant_id,
    ]
    report = await verify_agent_event_parity(
        session_id,
        user_id=user_id,
        require_closed=True,
    )
    assert report.ok is True


@pytest.mark.asyncio
async def test_inbox_batch_is_one_closed_canonical_fork_boundary():
    from agent.driver import reserve_run
    from agent.inbox import accept_inbox_item, claim_inbox_boundary

    user_id, session_id = await _seed_session()
    await accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="steer",
        prompt="第一条 😀",
    )
    await accept_inbox_item(
        session_id=session_id,
        user_id=user_id,
        delivery="followup",
        prompt="第二条 你好",
    )
    lease = await reserve_run(session_id, user_id)
    run_fence = (session_id, lease.run_id, lease.generation)
    try:
        batch = await claim_inbox_boundary(
            lease,
            step=1,
            include_next_turn=True,
        )
        assert len(batch.receipts) == 2
        boundary_id = batch.receipts[-1].message_id
        assistant = await create_assistant_message(
            session_id,
            boundary_id,
            model_id="openai/test",
            user_id=user_id,
            run_fence=run_fence,
        )
        assistant.finish = "stop"
        await update_message_info(
            assistant,
            user_id=user_id,
            run_fence=run_fence,
        )

        frozen = await freeze_fork_event_range(
            session_id,
            user_id=user_id,
            up_to_message_id=assistant.id,
        )
        assert frozen.covered_message_ids == (
            batch.receipts[0].message_id,
            batch.receipts[1].message_id,
            assistant.id,
        )
        canonical = await load_canonical_model_surface(
            session_id,
            user_id=user_id,
            run_fence=run_fence,
            repair_tail=False,
        )
        assert [message.id for message in canonical.messages] == list(
            frozen.covered_message_ids
        )
    finally:
        await lease.release(session_status="idle")


@pytest.mark.asyncio
async def test_interleaved_inbox_steps_share_one_strict_compaction_fork_turn():
    from agent.driver import reserve_run
    from agent.inbox import (
        accept_inbox_item,
        claim_inbox_boundary,
        settle_claimed_inbox_items,
    )

    user_id, session_id = await _seed_session()
    for delivery, prompt in (
        ("steer", "step-one-a"),
        ("inject", "step-one-b"),
        ("followup", "turn-tail-c"),
    ):
        await accept_inbox_item(
            session_id=session_id,
            user_id=user_id,
            delivery=delivery,
            prompt=prompt,
        )
    lease = await reserve_run(session_id, user_id, run_id="inbox-logical-turn")
    run_fence = (session_id, lease.run_id, lease.generation)
    source_ids: tuple[str, ...]
    try:
        first = await claim_inbox_boundary(
            lease,
            step=1,
            include_next_turn=True,
        )
        assert len(first.receipts) == 3
        assistant_one = await create_assistant_message(
            session_id,
            first.receipts[-1].message_id,
            model_id="openai/test",
            user_id=user_id,
            run_fence=run_fence,
        )
        assistant_one.finish = "tool_calls"
        await update_message_info(
            assistant_one,
            user_id=user_id,
            run_fence=run_fence,
        )

        await accept_inbox_item(
            session_id=session_id,
            user_id=user_id,
            delivery="steer",
            prompt="step-two-late",
        )
        second = await claim_inbox_boundary(
            lease,
            step=2,
            include_next_turn=False,
        )
        assert len(second.receipts) == 1
        assistant_two = await create_assistant_message(
            session_id,
            second.receipts[-1].message_id,
            model_id="openai/test",
            user_id=user_id,
            run_fence=run_fence,
        )
        assistant_two.finish = "stop"
        await update_message_info(
            assistant_two,
            user_id=user_id,
            run_fence=run_fence,
        )
        source_ids = (
            *(receipt.message_id for receipt in first.receipts),
            assistant_one.id,
            second.receipts[0].message_id,
            assistant_two.id,
        )

        report = await verify_agent_event_parity(
            session_id,
            user_id=user_id,
            require_closed=True,
        )
        assert report.ok is True, report.model_dump()
        async with get_db_session() as db:
            lifecycle = list((await db.execute(select(AgentEvent).where(
                AgentEvent.session_id == session_id,
                AgentEvent.run_id == lease.run_id,
                AgentEvent.generation == lease.generation,
                AgentEvent.kind.in_(("turn.started", "turn.finished")),
            ).order_by(AgentEvent.sequence))).scalars().all())
        assert [event.kind for event in lifecycle] == [
            "turn.started",
            "turn.finished",
        ]
        assert {event.turn_id for event in lifecycle} == {
            first.receipts[0].turn_id
        }

        frozen = await freeze_fork_event_range(
            session_id,
            user_id=user_id,
            up_to_message_id=assistant_two.id,
        )
        assert frozen.covered_message_ids == source_ids
        await settle_claimed_inbox_items(
            lease,
            result_message_id=assistant_two.id,
            outcome="succeeded",
        )
    finally:
        await lease.release(session_status="idle")

    boundary_id, _assistant_id, _text_id = await _compaction_attempt(
        session_id,
        user_id,
    )
    compaction = await freeze_compaction_event_range(
        session_id,
        user_id=user_id,
        compaction_user_id=boundary_id,
        requested_tail_start_id=None,
        run_fence=None,
    )
    assert compaction.source.covered_message_ids == source_ids


@pytest.mark.asyncio
async def test_process_compaction_uses_frozen_event_input_end_to_end(monkeypatch):
    from agent.compaction import process_compaction

    user_id, session_id = await _seed_session()
    await _closed_turn(session_id, user_id, prompt="long context " * 200)
    compaction_id = f"message_compact_{uuid4().hex[:10]}"
    await create_user_message(
        session_id,
        "",
        agent="compaction",
        user_id=user_id,
        message_id=compaction_id,
        additional_parts=(CompactionPart(
            id=f"part_compact_{uuid4().hex[:10]}",
            auto=False,
            session_id=session_id,
            message_id=compaction_id,
        ),),
    )

    async def fake_stream(**_kwargs):
        yield {"type": "text_delta", "text": "brief checkpoint"}
        yield {
            "type": "finish",
            "usage": {"input": 700, "output": 4, "total": 704},
        }

    monkeypatch.setattr("agent.llm.stream_llm", fake_stream)
    result = await process_compaction(
        session_id,
        await get_messages(session_id, user_id=user_id),
        "openai/test",
        auto=False,
        user_id=user_id,
        run_fence=None,
    )
    assert result == "stop"
    messages = await get_messages(session_id, user_id=user_id)
    compacted = await filter_compacted(messages)
    assert compacted[0].id == compaction_id
    assert compacted[1].summary is True
    assert compacted[1].finish == "stop"
    async with get_db_session() as db:
        replacement_count = len(list((await db.execute(select(AgentEvent).where(
            AgentEvent.session_id == session_id,
            AgentEvent.kind == "surface.replacement",
        ))).scalars().all()))
    assert replacement_count == 1


@pytest.mark.asyncio
async def test_provider_failure_keeps_original_tool_output_while_using_detached_prune(
    monkeypatch,
):
    from agent.compaction import process_compaction

    user_id, session_id = await _seed_session()
    original = "x" * 41_000
    oldest_tool_id = ""
    for index in range(3):
        _, _, tool_id = await _closed_tool_turn(
            session_id,
            user_id,
            output=original if index == 0 else f"newer-{index}",
        )
        if index == 0:
            oldest_tool_id = tool_id
    compaction_id = f"message_compact_{uuid4().hex[:10]}"
    await create_user_message(
        session_id,
        "",
        agent="compaction",
        user_id=user_id,
        message_id=compaction_id,
        additional_parts=(CompactionPart(
            id=f"part_compact_{uuid4().hex[:10]}",
            auto=False,
            session_id=session_id,
            message_id=compaction_id,
        ),),
    )
    provider_messages: list[dict] = []

    async def failed_stream(**kwargs):
        provider_messages.extend(kwargs["messages"])
        yield {"type": "error", "error": "provider failed"}

    monkeypatch.setattr("agent.llm.stream_llm", failed_stream)
    assert await process_compaction(
        session_id,
        await get_messages(session_id, user_id=user_id),
        "openai/test",
        auto=False,
        user_id=user_id,
        run_fence=None,
    ) == "stop"
    assert "[Old tool result content cleared]" in str(provider_messages)
    async with get_db_session() as db:
        row = await db.get(Part, oldest_tool_id)
        assert row is not None
        assert row.data["output"] == original
        assert not ((row.data.get("state") or {}).get("time") or {}).get("compacted")


@pytest.mark.asyncio
async def test_compaction_cas_drift_keeps_original_tool_output(monkeypatch):
    from agent.compaction import process_compaction

    user_id, session_id = await _seed_session()
    original = "y" * 41_000
    covered_assistant_id = ""
    oldest_tool_id = ""
    for index in range(3):
        _, assistant_id, tool_id = await _closed_tool_turn(
            session_id,
            user_id,
            output=original if index == 0 else f"recent-{index}",
        )
        if index == 0:
            covered_assistant_id = assistant_id
            oldest_tool_id = tool_id
    compaction_id = f"message_compact_{uuid4().hex[:10]}"
    await create_user_message(
        session_id,
        "",
        agent="compaction",
        user_id=user_id,
        message_id=compaction_id,
        additional_parts=(CompactionPart(
            id=f"part_compact_{uuid4().hex[:10]}",
            auto=False,
            session_id=session_id,
            message_id=compaction_id,
        ),),
    )

    async def drifting_stream(**_kwargs):
        yield {"type": "text_delta", "text": "short checkpoint"}
        await set_message_reaction(
            covered_assistant_id,
            session_id,
            "up",
            user_id=user_id,
        )
        yield {"type": "finish", "usage": {"input": 100, "output": 3}}

    monkeypatch.setattr("agent.llm.stream_llm", drifting_stream)
    assert await process_compaction(
        session_id,
        await get_messages(session_id, user_id=user_id),
        "openai/test",
        auto=False,
        user_id=user_id,
        run_fence=None,
    ) == "stop"
    async with get_db_session() as db:
        row = await db.get(Part, oldest_tool_id)
        assert row is not None
        assert row.data["output"] == original
        assert not ((row.data.get("state") or {}).get("time") or {}).get("compacted")
        replacements = (await db.execute(select(func.count()).select_from(
            AgentEvent
        ).where(
            AgentEvent.session_id == session_id,
            AgentEvent.kind == "surface.replacement",
        ))).scalar_one()
    assert replacements == 0


@pytest.mark.asyncio
async def test_compaction_cas_drift_never_creates_a_boundary_or_replacement():
    user_id, session_id = await _seed_session()
    _, source_assistant_id = await _closed_turn(session_id, user_id)
    boundary_id, assistant_id, text_id = await _compaction_attempt(
        session_id, user_id
    )
    frozen = await freeze_compaction_event_range(
        session_id,
        user_id=user_id,
        compaction_user_id=boundary_id,
        requested_tail_start_id=None,
        run_fence=None,
    )
    await set_message_reaction(
        source_assistant_id,
        session_id,
        "down",
        user_id=user_id,
    )
    with pytest.raises(StableEventRangeDriftError):
        await finalize_compaction_replacement(
            frozen=frozen.source,
            user_id=user_id,
            compaction_user_id=boundary_id,
            assistant_message_id=assistant_id,
            text_part_id=text_id,
            summary_text="summary",
            tail_start_id=None,
            source_token_count=100,
            summary_token_count=2,
            model_id="openai/test",
            usage=None,
            run_fence=None,
        )
    messages = await get_messages(session_id, user_id=user_id)
    attempt = next(message for message in messages if message.id == assistant_id)
    assert attempt.finish is None
    assert [message.id for message in await filter_compacted(messages)] == [
        message.id for message in messages
    ]
    async with get_db_session() as db:
        replacements = list((await db.execute(select(AgentEvent).where(
            AgentEvent.session_id == session_id,
            AgentEvent.kind == "surface.replacement",
        ))).scalars().all())
    assert replacements == []


@pytest.mark.asyncio
async def test_summary_must_be_strictly_shorter_than_replaced_input():
    user_id, session_id = await _seed_session()
    await _closed_turn(session_id, user_id)
    boundary_id, assistant_id, text_id = await _compaction_attempt(
        session_id, user_id
    )
    frozen = await freeze_compaction_event_range(
        session_id,
        user_id=user_id,
        compaction_user_id=boundary_id,
        requested_tail_start_id=None,
        run_fence=None,
    )
    with pytest.raises(SummaryNotCompactError, match="not shorter"):
        await finalize_compaction_replacement(
            frozen=frozen.source,
            user_id=user_id,
            compaction_user_id=boundary_id,
            assistant_message_id=assistant_id,
            text_part_id=text_id,
            summary_text="too long",
            tail_start_id=None,
            source_token_count=10,
            summary_token_count=10,
            model_id="openai/test",
            usage=None,
            run_fence=None,
        )


@pytest.mark.asyncio
async def test_fork_writes_lineage_and_preserves_child_surface_parity():
    user_id, session_id = await _seed_session()
    user_id_source, assistant_id = await _closed_turn(session_id, user_id)
    open_user = await create_user_message(session_id, "open tail", user_id=user_id)
    child = await fork_session(
        session_id,
        up_to_message_id=assistant_id,
        user_id=user_id,
    )
    child_messages = await get_messages(child.id, user_id=user_id)
    assert len(child_messages) == 2
    assert child_messages[0].id != user_id_source
    assert child_messages[1].parent_id == child_messages[0].id
    assert open_user.id not in {message.id for message in child_messages}

    async with get_db_session() as db:
        events = list((await db.execute(select(AgentEvent).where(
            AgentEvent.session_id == child.id,
        ).order_by(AgentEvent.sequence))).scalars().all())
    assert [event.kind for event in events] == ["surface.seed", "session.forked"]
    lineage = events[-1].payload
    assert lineage["source"]["session_id"] == session_id
    assert lineage["source"]["covered_message_ids"] == [
        user_id_source,
        assistant_id,
    ]
    assert (await verify_agent_event_parity(
        child.id,
        user_id=user_id,
        require_closed=True,
    )).ok is True


@pytest.mark.asyncio
async def test_fork_lineage_fault_rolls_back_child_and_publishes_nothing(monkeypatch):
    user_id, session_id = await _seed_session()
    _, assistant_id = await _closed_turn(session_id, user_id)
    async with get_db_session() as db:
        before = (await db.execute(select(func.count()).select_from(Session).where(
            Session.user_id == user_id,
        ))).scalar_one()
    published: list[tuple] = []
    monkeypatch.setattr(
        "session.session.bus.publish",
        lambda *args, **kwargs: published.append((args, kwargs)),
    )

    async def fail_lineage(*_args, **_kwargs):
        raise RuntimeError("lineage fault")

    monkeypatch.setattr("session.fork.append_agent_event_locked", fail_lineage)
    with pytest.raises(RuntimeError, match="lineage fault"):
        await fork_session(
            session_id,
            up_to_message_id=assistant_id,
            user_id=user_id,
        )
    async with get_db_session() as db:
        after = (await db.execute(select(func.count()).select_from(Session).where(
            Session.user_id == user_id,
        ))).scalar_one()
    assert after == before
    assert published == []


@pytest.mark.asyncio
async def test_fork_rebuilds_missing_part_read_model_from_canonical_events(monkeypatch):
    user_id, session_id = await _seed_session()
    _, assistant_id = await _closed_turn(session_id, user_id)
    messages = await get_messages(session_id, user_id=user_id)
    source_part_id = next(
        part.id
        for message in messages
        for part in message.parts
        if getattr(part, "id", None)
    )
    async with get_db_session() as db:
        row = await db.get(Part, source_part_id)
        assert row is not None
        await db.delete(row)
        before = (await db.execute(select(func.count()).select_from(Session).where(
            Session.user_id == user_id,
        ))).scalar_one()
    monkeypatch.setattr("session.session.bus.publish", lambda *_args, **_kwargs: None)
    child = await fork_session(
        session_id,
        up_to_message_id=assistant_id,
        user_id=user_id,
    )
    child_messages = await get_messages(child.id, user_id=user_id)
    assert any(message.parts for message in child_messages)
    async with get_db_session() as db:
        after = (await db.execute(select(func.count()).select_from(Session).where(
            Session.user_id == user_id,
        ))).scalar_one()
    assert after == before + 1


@pytest.mark.asyncio
async def test_fork_cas_drift_never_creates_or_publishes_child(monkeypatch):
    user_id, session_id = await _seed_session()
    _, assistant_id = await _closed_turn(session_id, user_id)
    async with get_db_session() as db:
        before = (await db.execute(select(func.count()).select_from(Session).where(
            Session.user_id == user_id,
        ))).scalar_one()
    published: list[tuple] = []
    monkeypatch.setattr(
        "session.session.bus.publish",
        lambda *args, **kwargs: published.append((args, kwargs)),
    )

    async def drift(*_args, **_kwargs):
        raise StableEventRangeDriftError("fork CAS drift")

    monkeypatch.setattr("session.fork.revalidate_stable_event_range_locked", drift)
    with pytest.raises(StableEventRangeDriftError, match="fork CAS drift"):
        await fork_session(
            session_id,
            up_to_message_id=assistant_id,
            user_id=user_id,
        )
    async with get_db_session() as db:
        after = (await db.execute(select(func.count()).select_from(Session).where(
            Session.user_id == user_id,
        ))).scalar_one()
    assert after == before
    assert published == []


@pytest.mark.asyncio
async def test_fork_remaps_compaction_replacement_links_and_preserved_tail():
    user_id, session_id = await _seed_session()
    first_turn = await _closed_turn(session_id, user_id, prompt="old context" * 80)
    second_turn = await _closed_turn(session_id, user_id, prompt="keep this turn")
    boundary_id, summary_id, text_id = await _compaction_attempt(session_id, user_id)
    frozen = await freeze_compaction_event_range(
        session_id,
        user_id=user_id,
        compaction_user_id=boundary_id,
        requested_tail_start_id=second_turn[0],
        run_fence=None,
    )
    assert frozen.source.covered_message_ids == first_turn
    assert frozen.tail_start_id == second_turn[0]
    await finalize_compaction_replacement(
        frozen=frozen.source,
        user_id=user_id,
        compaction_user_id=boundary_id,
        assistant_message_id=summary_id,
        text_part_id=text_id,
        summary_text="checkpoint",
        tail_start_id=frozen.tail_start_id,
        source_token_count=100,
        summary_token_count=2,
        model_id="openai/test",
        usage=None,
        run_fence=None,
    )

    child = await fork_session(
        session_id,
        up_to_message_id=summary_id,
        user_id=user_id,
    )
    raw = await get_messages(child.id, user_id=user_id)
    by_role = [message for message in raw if message.role == MessageRole.USER]
    child_first, child_second, child_boundary = by_role
    child_summary = next(
        message for message in raw
        if message.parent_id == child_boundary.id and message.summary
    )
    descriptor = next(
        part.model_dump() if hasattr(part, "model_dump") else part
        for part in child_boundary.parts
        if getattr(part, "type", None) == "compaction"
        or (isinstance(part, dict) and part.get("type") == "compaction")
    )
    assert descriptor["tail_start_id"] == child_second.id
    assert descriptor["covered_message_ids"] == [
        child_first.id,
        next(message for message in raw if message.parent_id == child_first.id).id,
    ]
    assert [message.id for message in await filter_compacted(raw)] == [
        child_boundary.id,
        child_summary.id,
        child_second.id,
        next(message for message in raw if message.parent_id == child_second.id).id,
    ]
    canonical = await load_canonical_model_surface(
        child.id,
        user_id=user_id,
        repair_tail=False,
    )
    assert [message.id for message in canonical.messages] == [
        child_boundary.id,
        child_summary.id,
        child_second.id,
        next(message for message in raw if message.parent_id == child_second.id).id,
    ]
    async with get_db_session() as db:
        imported_replacement = (await db.execute(select(AgentEvent).where(
            AgentEvent.session_id == child.id,
            AgentEvent.kind == "surface.replacement",
        ))).scalar_one()
    assert imported_replacement.payload["source"]["session_id"] == session_id
    assert imported_replacement.payload["projected_covered_message_ids"] == [
        child_first.id,
        next(message for message in raw if message.parent_id == child_first.id).id,
    ]
    assert (await verify_agent_event_parity(
        child.id,
        user_id=user_id,
        require_closed=True,
    )).ok is True
