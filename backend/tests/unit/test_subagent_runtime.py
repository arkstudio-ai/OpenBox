"""Durable continuable-subagent acceptance, claims, fences, and bounds."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlalchemy import delete, func, select

from agent.driver import get_driver_state, reserve_run
from agent.subagent_runtime import (
    OUTCOME_ERROR,
    OUTCOME_INTERRUPTED,
    OUTCOME_UNKNOWN,
    SubagentBusyError,
    SubagentFenceError,
    accept_follow_up,
    accept_spawn,
    abandon_claim,
    bind_claimed_activation,
    claim_activation,
    claim_is_dispatchable,
    complete_activation_from_transcript,
    consume_interrupt_requests,
    interrupt_subagent,
    report_subagent,
    recover_subagent_outboxes,
    apply_ready_subagent_outboxes_locked,
    ready_subagent_parent_sessions,
)
from agent.subagent_authority import (
    compose_subagent_authority,
    parse_subagent_authority,
    with_subagent_composition,
)
from permission.permission import Rule
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
from db.models.subagent import (
    SubagentActivation,
    SubagentDescriptor,
    SubagentOutbox,
)
from db.models.user import User


@pytest.fixture(autouse=True)
async def _isolate_subagent_rows(ensure_test_db):
    async with get_db_session() as db:
        await db.execute(delete(SubagentOutbox))
        await db.execute(delete(SubagentActivation))
        await db.execute(delete(SubagentDescriptor))
    yield
    async with get_db_session() as db:
        await db.execute(delete(SubagentOutbox))
        await db.execute(delete(SubagentActivation))
        await db.execute(delete(SubagentDescriptor))


async def _parent_tool(*, user_prefix: str = "subagent") -> dict:
    suffix = uuid.uuid4().hex[:12]
    user_id = f"{user_prefix}-user-{suffix}"
    project_id = f"{user_prefix}-project-{suffix}"
    session_id = f"{user_prefix}-parent-{suffix}"
    message_id = f"{user_prefix}-message-{suffix}"
    part_id = f"{user_prefix}-part-{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(
            id=user_id,
            username=f"{user_prefix}-{suffix}",
            created_at=now,
            updated_at=now,
        ))
        db.add(Project(
            id=project_id,
            user_id=user_id,
            name="Subagent project",
            slug=f"{user_prefix}-{suffix}",
            created_at=now,
            updated_at=now,
        ))
        db.add(Session(
            id=session_id,
            user_id=user_id,
            project_id=project_id,
            title="Parent",
            agent="build",
            model="provider/parent",
            status="idle",
            kind="normal",
            token_usage={},
            tool_exposure_state={},
            created_at=now,
            updated_at=now,
        ))
        db.add(Message(
            id=message_id,
            session_id=session_id,
            user_id=user_id,
            role="assistant",
            finish="tool_calls",
            created_at=now,
        ))
        db.add(Part(
            id=part_id,
            message_id=message_id,
            session_id=session_id,
            user_id=user_id,
            type="tool",
            canonical_tool_id="task",
            wire_tool_name="task",
            provider_binding_digest="a" * 64,
            provider_dialect="litellm",
            stream_seq=1,
            data={
                "type": "tool",
                "id": part_id,
                "tool": "task",
                "status": "running",
                "session_id": session_id,
                "message_id": message_id,
            },
            created_at=now,
        ))
    lease = await reserve_run(session_id, user_id)
    await lease.set_phase("running")
    return {
        "user_id": user_id,
        "project_id": project_id,
        "session_id": session_id,
        "message_id": message_id,
        "part_id": part_id,
        "lease": lease,
    }


async def _prepare_fork_parent(*, user_prefix: str = "subagent-fork") -> dict:
    """Give a live Task parent one closed turn and one currently open turn."""
    parent = await _parent_tool(user_prefix=user_prefix)
    now = datetime.now(timezone.utc)
    closed_user = f"{user_prefix}-closed-user-{uuid.uuid4().hex[:10]}"
    closed_assistant = f"{user_prefix}-closed-assistant-{uuid.uuid4().hex[:10]}"
    open_user = f"{user_prefix}-open-user-{uuid.uuid4().hex[:10]}"
    async with get_db_session() as db:
        current = await db.get(Message, parent["message_id"])
        current_part = await db.get(Part, parent["part_id"])
        assert current is not None and current_part is not None
        current.parent_id = open_user
        current.created_at = now
        current_part.created_at = now
        db.add_all((
            Message(
                id=closed_user,
                session_id=parent["session_id"],
                user_id=parent["user_id"],
                role="user",
                agent="build",
                model="openai/gpt-5.6-luna",
                created_at=now - timedelta(seconds=4),
            ),
            Part(
                id=f"{closed_user}-part",
                message_id=closed_user,
                session_id=parent["session_id"],
                user_id=parent["user_id"],
                type="text",
                data={
                    "type": "text",
                    "id": f"{closed_user}-part",
                    "text": "closed parent prompt",
                    "session_id": parent["session_id"],
                    "message_id": closed_user,
                },
                created_at=now - timedelta(seconds=4),
            ),
            Message(
                id=closed_assistant,
                session_id=parent["session_id"],
                user_id=parent["user_id"],
                role="assistant",
                parent_id=closed_user,
                model_id="openai/gpt-5.6-luna",
                finish="stop",
                created_at=now - timedelta(seconds=3),
            ),
            Part(
                id=f"{closed_assistant}-part",
                message_id=closed_assistant,
                session_id=parent["session_id"],
                user_id=parent["user_id"],
                type="text",
                data={
                    "type": "text",
                    "id": f"{closed_assistant}-part",
                    "text": "closed parent answer",
                    "channel": "final",
                    "session_id": parent["session_id"],
                    "message_id": closed_assistant,
                },
                created_at=now - timedelta(seconds=3),
            ),
            Message(
                id=open_user,
                session_id=parent["session_id"],
                user_id=parent["user_id"],
                role="user",
                agent="build",
                model="openai/gpt-5.6-luna",
                created_at=now - timedelta(seconds=2),
            ),
            Part(
                id=f"{open_user}-part",
                message_id=open_user,
                session_id=parent["session_id"],
                user_id=parent["user_id"],
                type="text",
                data={
                    "type": "text",
                    "id": f"{open_user}-part",
                    "text": "open parent prompt must not be copied",
                    "session_id": parent["session_id"],
                    "message_id": open_user,
                },
                created_at=now - timedelta(seconds=2),
            ),
        ))
    parent.update({
        "closed_user_id": closed_user,
        "closed_assistant_id": closed_assistant,
        "open_user_id": open_user,
    })
    return parent


def _authority_snapshot(
    *,
    tools: set[str] | None = None,
    permission_rules: list[Rule] | None = None,
) -> dict:
    return compose_subagent_authority(
        tool_ids=tools or {"task", "read", "grep", "glob"},
        permission_rules=permission_rules or [
            Rule(permission="*", pattern="*", action="allow")
        ],
        guard_rules=[],
    ).to_json()


def _v2_authority(
    *,
    seed_mode: str = "fresh",
    tools: list[str] | None = None,
    reasoning: str | None = "medium",
    persona: str | None = "Answer as a careful repository investigator.",
    output_schema: dict | None = None,
    config=None,
) -> dict:
    from agent.agent import get_agent
    from agent.subagent_composition import build_subagent_composition
    from core.config import get_config

    base = parse_subagent_authority(_authority_snapshot())
    config = config or _ready_composition_config()
    composition = build_subagent_composition(
        agent_def=get_agent("explore"),
        parent_tool_ids=base.tool_ids,
        config=config,
        inherited_model=config.model,
        requested_model=config.model,
        reasoning=reasoning,
        persona=persona,
        requested_tools=tools,
        output_schema=output_schema,
        seed_mode=seed_mode,
    )
    return with_subagent_composition(base, composition).to_json()


def _ready_composition_config():
    """A deterministic, explicitly declared provider binding for tests.

    The ambient deployment owns ``config.model``, and a checkout with no
    credentials declares no provider slot at all — so the slot is built here
    rather than looked up, or this file would only pass on a machine that
    happens to have that exact provider configured. Composition accepts only
    explicitly declared capabilities, never a provider-name heuristic, so the
    full provider set is declared too: anything a model declares is then a
    subset, and the binding stays consistent.
    """
    from core.config import ProviderConfig, get_config

    config = get_config().model_copy(deep=True)
    slot = config.model.split("/", 1)[0]
    config.provider[slot] = ProviderConfig(
        api_key="subagent-test-key",
        base_url="https://subagent-provider.invalid/v1",
        subagent_capabilities=[
            "model", "tool_filter", "reasoning", "persona", "output_schema",
        ],
        subagent_reasoning_variants=["medium"],
    )
    return config


async def _accept(parent: dict, *, lifecycle: str = "continuable", prompt: str = "inspect"):
    return await accept_spawn(
        user_id=parent["user_id"],
        parent_session_id=parent["session_id"],
        parent_message_id=parent["message_id"],
        parent_part_id=parent["part_id"],
        parent_run_id=parent["lease"].run_id,
        parent_generation=parent["lease"].generation,
        task_title="Inspect",
        prompt=prompt,
        subagent_type="explore",
        child_model="provider/child",
        lifecycle=lifecycle,
        authority_snapshot=_authority_snapshot(),
    )


async def _add_answer(
    parent: dict,
    ref,
    text: str,
    *,
    finish: str = "stop",
    error: dict | None = None,
    structured: dict | None = None,
    include_text: bool = True,
) -> None:
    now = datetime.now(timezone.utc)
    message_id = f"subagent-answer-{uuid.uuid4().hex[:12]}"
    async with get_db_session() as db:
        db.add(Message(
            id=message_id,
            session_id=ref.child_session_id,
            user_id=parent["user_id"],
            role="assistant",
            parent_id=ref.child_trigger_message_id,
            finish=finish,
            error=error,
            structured=structured,
            created_at=now,
        ))
        if include_text:
            db.add(Part(
                id=f"subagent-text-{uuid.uuid4().hex[:12]}",
                message_id=message_id,
                session_id=ref.child_session_id,
                user_id=parent["user_id"],
                type="text",
                data={
                    "type": "text",
                    "text": text,
                    "session_id": ref.child_session_id,
                    "message_id": message_id,
                },
                created_at=now,
            ))


async def _new_parent_part(parent: dict) -> dict:
    await parent["lease"].release(session_status="idle")
    now = datetime.now(timezone.utc)
    message_id = f"follow-message-{uuid.uuid4().hex[:12]}"
    part_id = f"follow-part-{uuid.uuid4().hex[:12]}"
    async with get_db_session() as db:
        db.add(Message(
            id=message_id,
            session_id=parent["session_id"],
            user_id=parent["user_id"],
            role="assistant",
            finish="tool_calls",
            created_at=now,
        ))
        db.add(Part(
            id=part_id,
            message_id=message_id,
            session_id=parent["session_id"],
            user_id=parent["user_id"],
            type="tool",
            canonical_tool_id="task",
            wire_tool_name="task",
            provider_binding_digest="b" * 64,
            provider_dialect="litellm",
            stream_seq=2,
            data={
                "type": "tool",
                "id": part_id,
                "tool": "task",
                "status": "running",
                "session_id": parent["session_id"],
                "message_id": message_id,
            },
            created_at=now,
        ))
    lease = await reserve_run(parent["session_id"], parent["user_id"])
    await lease.set_phase("running")
    return {**parent, "message_id": message_id, "part_id": part_id, "lease": lease}


@pytest.mark.asyncio
async def test_accept_spawn_is_one_atomic_identity_and_session_parity():
    parent = await _parent_tool()
    try:
        ref = await _accept(parent)
        async with get_db_session() as db:
            descriptor = (await db.execute(
                select(SubagentDescriptor).where(SubagentDescriptor.id == ref.descriptor_id)
            )).scalar_one()
            activation = (await db.execute(
                select(SubagentActivation).where(SubagentActivation.id == ref.id)
            )).scalar_one()
            outbox = (await db.execute(
                select(SubagentOutbox).where(SubagentOutbox.activation_id == ref.id)
            )).scalar_one()
            child = (await db.execute(
                select(Session).where(Session.id == ref.child_session_id)
            )).scalar_one()
            trigger = (await db.execute(
                select(Message).where(Message.id == ref.child_trigger_message_id)
            )).scalar_one()
            part = (await db.execute(
                select(Part).where(Part.id == parent["part_id"])
            )).scalar_one()

        assert descriptor.child_session_id == child.id
        assert descriptor.project_id == parent["project_id"]
        assert descriptor.parent_session_id == parent["session_id"]
        assert descriptor.generation == activation.descriptor_generation == 1
        assert descriptor.active_activation_id == activation.id
        assert descriptor.authority_snapshot["version"] == 1
        assert descriptor.authority_snapshot["tool_ids"] == [
            "glob", "grep", "read", "task",
        ]
        assert activation.child_trigger_message_id == trigger.id
        assert outbox.state == outbox.outcome == "waiting"
        assert trigger.session_id == child.id and trigger.role == "user"
        # Narrow Session construction helper preserves ordinary invariants.
        assert child.status == "idle"
        assert child.agent == "explore"
        assert child.model == "provider/child"
        assert child.kind == "normal"
        assert child.parent_id == parent["session_id"]
        assert child.token_usage == {}
        assert child.tool_exposure_state == {}
        assert part.data["metadata"]["subagent_activation_id"] == activation.id
    finally:
        await parent["lease"].release(session_status="idle")


@pytest.mark.asyncio
async def test_cold_child_load_restores_exact_authority_and_rejects_corruption():
    from agent.subagent_authority import (
        SubagentAuthorityError,
        load_subagent_authority,
    )

    parent = await _parent_tool(user_prefix="authority-cold")
    try:
        ref = await _accept(parent)
        async with get_db_session() as db:
            child = (await db.execute(
                select(Session).where(Session.id == ref.child_session_id)
            )).scalar_one()
        restored = await load_subagent_authority(child)
        assert restored is not None
        assert restored.tool_ids == frozenset({"task", "read", "grep", "glob"})

        async with get_db_session() as db:
            descriptor = (await db.execute(
                select(SubagentDescriptor).where(
                    SubagentDescriptor.id == ref.descriptor_id
                )
            )).scalar_one()
            descriptor.authority_snapshot = {}
        with pytest.raises(SubagentAuthorityError, match="unsupported"):
            await load_subagent_authority(child)
    finally:
        await parent["lease"].release(session_status="idle")


@pytest.mark.asyncio
async def test_accept_duplicate_parent_part_is_idempotent_and_claim_race_has_one_winner():
    parent = await _parent_tool(user_prefix="duplicate")
    try:
        first = await _accept(parent)
        second = await _accept(parent)
        assert first.id == second.id
        claims = await asyncio.gather(
            claim_activation(first.id, user_id=parent["user_id"], owner="worker-a"),
            claim_activation(first.id, user_id=parent["user_id"], owner="worker-b"),
        )
        assert sum(claim is not None for claim in claims) == 1
        async with get_db_session() as db:
            child_count = (await db.execute(
                select(func.count()).select_from(Session).where(
                    Session.parent_id == parent["session_id"]
                )
            )).scalar_one()
            activation_count = (await db.execute(
                select(func.count()).select_from(SubagentActivation).where(
                    SubagentActivation.parent_part_id == parent["part_id"]
                )
            )).scalar_one()
        assert child_count == activation_count == 1
    finally:
        await parent["lease"].release(session_status="idle")


@pytest.mark.asyncio
async def test_interrupt_commit_prevents_claim_and_reports_interrupted_without_partial_text():
    parent = await _parent_tool(user_prefix="interrupt")
    try:
        ref = await _accept(parent)
        result = await interrupt_subagent(
            ref.descriptor_id,
            user_id=parent["user_id"],
            parent_session_id=parent["session_id"],
            project_id=parent["project_id"],
        )
        assert result["interrupt_requested"] is True
        assert await claim_activation(ref.id, user_id=parent["user_id"]) is None
        report = await report_subagent(
            ref.descriptor_id,
            user_id=parent["user_id"],
            parent_session_id=parent["session_id"],
            project_id=parent["project_id"],
        )
        assert report["outcome"] == OUTCOME_INTERRUPTED
        assert report["result"]["metadata"]["error"] is True
        assert "interrupted before" in report["result"]["output"]
    finally:
        await parent["lease"].release(session_status="idle")


@pytest.mark.asyncio
async def test_interrupt_committed_after_claim_fences_reserve_and_scanner_settles():
    parent = await _parent_tool(user_prefix="interrupt-race")
    try:
        ref = await _accept(parent)
        claim = await claim_activation(ref.id, user_id=parent["user_id"])
        assert claim is not None
        result = await interrupt_subagent(
            ref.descriptor_id,
            user_id=parent["user_id"],
            parent_session_id=parent["session_id"],
            project_id=parent["project_id"],
        )
        assert result["interrupt_requested"] is True
        # The dispatcher must perform this generation check immediately before
        # reserve; periodic interrupt convergence reaches the same outbox.
        assert await claim_is_dispatchable(claim) is False
        assert await consume_interrupt_requests() == 1
        report = await report_subagent(
            ref.descriptor_id,
            user_id=parent["user_id"],
            parent_session_id=parent["session_id"],
            project_id=parent["project_id"],
        )
        assert report["outcome"] == OUTCOME_INTERRUPTED
    finally:
        await parent["lease"].release(session_status="idle")


@pytest.mark.asyncio
async def test_old_activation_interrupt_does_not_abort_replacement_child_generation():
    parent = await _parent_tool(user_prefix="interrupt-generation")
    replacement = None
    try:
        ref = await _accept(parent)
        claim = await claim_activation(ref.id, user_id=parent["user_id"])
        assert claim is not None
        old = await reserve_run(
            ref.child_session_id,
            parent["user_id"],
            trigger_message_id=ref.child_trigger_message_id,
        )
        assert await bind_claimed_activation(claim, old)
        await old.release(session_status="idle")
        replacement = await reserve_run(ref.child_session_id, parent["user_id"])

        await interrupt_subagent(
            ref.descriptor_id,
            user_id=parent["user_id"],
            parent_session_id=parent["session_id"],
            project_id=parent["project_id"],
        )
        assert replacement.abort.is_set() is False
        # The periodic consumer uses the activation's same old run/generation,
        # not merely its child Session id.
        assert await consume_interrupt_requests() == 0
        assert replacement.abort.is_set() is False
        async with get_db_session() as db:
            state = (await db.execute(
                select(AgentDriverState).where(
                    AgentDriverState.session_id == ref.child_session_id
                )
            )).scalar_one()
        assert state.run_id == replacement.run_id
        assert state.abort_requested_at is None
    finally:
        if replacement is not None:
            await replacement.release(session_status="idle")
        await parent["lease"].release(session_status="idle")


@pytest.mark.asyncio
async def test_cross_tenant_and_wrong_parent_report_fail_closed():
    parent = await _parent_tool(user_prefix="owner")
    other = await _parent_tool(user_prefix="other")
    try:
        ref = await _accept(parent)
        with pytest.raises(SubagentFenceError):
            await report_subagent(
                ref.descriptor_id,
                user_id=other["user_id"],
                parent_session_id=other["session_id"],
                project_id=other["project_id"],
            )
        with pytest.raises(SubagentFenceError):
            await report_subagent(
                ref.descriptor_id,
                user_id=parent["user_id"],
                parent_session_id=other["session_id"],
                project_id=parent["project_id"],
            )
    finally:
        await parent["lease"].release(session_status="idle")
        await other["lease"].release(session_status="idle")


@pytest.mark.asyncio
async def test_prompt_bound_rejects_without_orphan_child():
    parent = await _parent_tool(user_prefix="bounds")
    try:
        with pytest.raises(ValueError, match="1..65536"):
            await _accept(parent, prompt="x" * 65_537)
        async with get_db_session() as db:
            child_count = (await db.execute(
                select(func.count()).select_from(Session).where(
                    Session.parent_id == parent["session_id"]
                )
            )).scalar_one()
            descriptor_count = (await db.execute(
                select(func.count()).select_from(SubagentDescriptor)
            )).scalar_one()
        assert child_count == descriptor_count == 0
    finally:
        await parent["lease"].release(session_status="idle")


@pytest.mark.asyncio
async def test_continuable_follow_up_monotonically_persists_current_parent_boundary():
    parent = await _parent_tool(user_prefix="follow")
    current_parent = parent
    try:
        first = await _accept(parent, prompt="first")
        async with get_db_session() as db:
            original_authority = (await db.execute(
                select(SubagentDescriptor.authority_snapshot).where(
                    SubagentDescriptor.id == first.descriptor_id
                )
            )).scalar_one()
        first_claim = await claim_activation(first.id, user_id=parent["user_id"])
        assert first_claim is not None
        child_lease = await reserve_run(
            first.child_session_id,
            parent["user_id"],
            trigger_message_id=first.child_trigger_message_id,
        )
        assert await bind_claimed_activation(first_claim, child_lease)
        await _add_answer(parent, first, "x" * 80_000)
        await child_lease.release(session_status="idle")
        projected = await complete_activation_from_transcript(
            first.id,
            child_run_id=child_lease.run_id,
            child_generation=child_lease.generation,
        )
        assert projected["metadata"]["truncated"] is True
        assert len(projected["output"].encode()) < 60_000

        current_parent = await _new_parent_part(parent)
        narrower_authority = _authority_snapshot(
            tools={"task", "read"},
            permission_rules=[
                Rule(permission="*", pattern="*", action="allow"),
                Rule(permission="read", pattern="secret/**", action="deny"),
            ],
        )
        follow = await accept_follow_up(
            descriptor_id=first.descriptor_id,
            user_id=parent["user_id"],
            parent_session_id=parent["session_id"],
            parent_message_id=current_parent["message_id"],
            parent_part_id=current_parent["part_id"],
            parent_run_id=current_parent["lease"].run_id,
            parent_generation=current_parent["lease"].generation,
            task_title="Second",
            prompt="second",
            authority_snapshot=narrower_authority,
        )
        assert follow.descriptor_id == first.descriptor_id
        assert follow.child_session_id == first.child_session_id
        assert follow.descriptor_generation == 2
        assert follow.parent_part_id != first.parent_part_id
        async with get_db_session() as db:
            old_part = (await db.execute(
                select(Part).where(Part.id == first.parent_part_id)
            )).scalar_one()
            new_part = (await db.execute(
                select(Part).where(Part.id == follow.parent_part_id)
            )).scalar_one()
            continued_authority = (await db.execute(
                select(SubagentDescriptor.authority_snapshot).where(
                    SubagentDescriptor.id == first.descriptor_id
                )
            )).scalar_one()
        assert old_part.data["metadata"]["subagent_activation_id"] == first.id
        assert new_part.data["metadata"]["subagent_activation_id"] == follow.id
        assert continued_authority != original_authority
        assert continued_authority["tool_ids"] == ["read", "task"]
        assert len(continued_authority["permission_planes"]) == 2
    finally:
        await current_parent["lease"].release(session_status="idle")


@pytest.mark.asyncio
async def test_nonterminal_partial_answer_is_error_not_success():
    parent = await _parent_tool(user_prefix="partial")
    try:
        ref = await _accept(parent)
        claim = await claim_activation(ref.id, user_id=parent["user_id"])
        assert claim is not None
        child_lease = await reserve_run(
            ref.child_session_id,
            parent["user_id"],
            trigger_message_id=ref.child_trigger_message_id,
        )
        assert await bind_claimed_activation(claim, child_lease)
        await _add_answer(parent, ref, "partial secret", finish="tool_calls")
        await child_lease.release(session_status="idle")
        result = await complete_activation_from_transcript(
            ref.id,
            child_run_id=child_lease.run_id,
            child_generation=child_lease.generation,
        )
        assert result["metadata"]["subagent_outcome"] == OUTCOME_ERROR
        assert result["metadata"]["error"] is True
        assert "partial secret" not in result["output"]
    finally:
        await parent["lease"].release(session_status="idle")


@pytest.mark.asyncio
async def test_finish_stop_message_with_error_is_not_success():
    parent = await _parent_tool(user_prefix="terminal-error")
    try:
        ref = await _accept(parent)
        claim = await claim_activation(ref.id, user_id=parent["user_id"])
        assert claim is not None
        child_lease = await reserve_run(
            ref.child_session_id,
            parent["user_id"],
            trigger_message_id=ref.child_trigger_message_id,
        )
        assert await bind_claimed_activation(claim, child_lease)
        await _add_answer(
            parent,
            ref,
            "must not escape as success",
            error={"message": "provider failed"},
        )
        await child_lease.release(session_status="error")
        result = await complete_activation_from_transcript(
            ref.id,
            child_run_id=child_lease.run_id,
            child_generation=child_lease.generation,
        )
        assert result["metadata"]["subagent_outcome"] == OUTCOME_ERROR
        assert "must not escape as success" not in result["output"]
    finally:
        await parent["lease"].release(session_status="idle")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("structured", "succeeds"),
    [
        ({"answer": "validated"}, True),
        ({"answer": 7}, False),
        ({"answer": "validated", "extra": True}, False),
    ],
)
async def test_structured_terminal_result_is_locally_validated_and_preferred(
    structured,
    succeeds,
    monkeypatch,
):
    parent = await _parent_tool(user_prefix="structured-result")
    try:
        schema = {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
            "additionalProperties": False,
        }
        config = _ready_composition_config()
        monkeypatch.setattr("core.config.get_config", lambda: config)
        snapshot = _v2_authority(
            tools=["read"],
            output_schema=schema,
            config=config,
        )
        composition = parse_subagent_authority(snapshot).composition
        assert composition is not None
        ref = await accept_spawn(
            user_id=parent["user_id"],
            parent_session_id=parent["session_id"],
            parent_message_id=parent["message_id"],
            parent_part_id=parent["part_id"],
            parent_run_id=parent["lease"].run_id,
            parent_generation=parent["lease"].generation,
            task_title="Structured result",
            prompt="return exact schema",
            subagent_type="explore",
            child_model=composition.model,
            lifecycle="continuable",
            authority_snapshot=snapshot,
        )
        claim = await claim_activation(ref.id, user_id=parent["user_id"])
        assert claim is not None
        child_lease = await reserve_run(
            ref.child_session_id,
            parent["user_id"],
            trigger_message_id=ref.child_trigger_message_id,
        )
        assert await bind_claimed_activation(claim, child_lease)
        await _add_answer(
            parent,
            ref,
            "text must not be required",
            structured=structured,
            include_text=False,
        )
        await child_lease.release(session_status="idle")
        result = await complete_activation_from_transcript(
            ref.id,
            child_run_id=child_lease.run_id,
            child_generation=child_lease.generation,
        )
        if succeeds:
            assert result["metadata"]["subagent_outcome"] == "succeeded"
            assert result["metadata"]["structured_result"] == structured
            assert '"answer": "validated"' in result["output"]
            assert "no text output" not in result["output"]
        else:
            assert result["metadata"]["subagent_outcome"] == OUTCOME_ERROR
            assert result["metadata"]["recovery_code"] == (
                "subagent_structured_result_invalid"
            )
            assert "structured_result" not in result["metadata"]
    finally:
        await parent["lease"].release(session_status="idle")


def test_terminal_error_predicate_supports_postgres_json_null_bind_semantics():
    from sqlalchemy.dialects import postgresql
    from agent.subagent_runtime import _message_error_is_empty

    dialect = postgresql.dialect()
    json_type = Message.__table__.c.error.type.dialect_impl(dialect)
    bind = json_type.bind_processor(dialect)
    assert bind is not None and bind(None) == "null"
    sql = str(select(Message.id).where(_message_error_is_empty()).compile(
        dialect=dialect,
        compile_kwargs={"literal_binds": True},
    ))
    assert "messages.error IS NULL" in sql
    assert "CAST(messages.error AS VARCHAR) = 'null'" in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [OUTCOME_ERROR, OUTCOME_UNKNOWN])
async def test_continuable_terminal_failure_can_accept_exact_next_follow_up(outcome):
    from agent.driver import recover_expired_driver_records
    from agent.recovery import repair_expired_sessions

    parent = await _parent_tool(user_prefix=f"continue-{outcome}")
    current_parent = parent
    try:
        ref = await _accept(parent)
        claim = await claim_activation(ref.id, user_id=parent["user_id"])
        assert claim is not None
        child_lease = await reserve_run(
            ref.child_session_id,
            parent["user_id"],
            trigger_message_id=ref.child_trigger_message_id,
        )
        assert await bind_claimed_activation(claim, child_lease)
        if outcome == OUTCOME_ERROR:
            await child_lease.release(session_status="error")
            await complete_activation_from_transcript(
                ref.id,
                child_run_id=child_lease.run_id,
                child_generation=child_lease.generation,
            )
        else:
            await child_lease.set_phase("running")
            await child_lease.stop_monitor()
            async with get_db_session() as db:
                state = (await db.execute(
                    select(AgentDriverState).where(
                        AgentDriverState.session_id == ref.child_session_id
                    )
                )).scalar_one()
                state.lease_expires_at = (
                    datetime.now(timezone.utc) - timedelta(seconds=1)
                )
            records = [
                record
                for record in await recover_expired_driver_records()
                if record.session_id == ref.child_session_id
            ]
            assert len(records) == 1
            assert await recover_subagent_outboxes(records) == 1
            repairs = await repair_expired_sessions(records)
            assert len(repairs) == 1 and repairs[0].skipped is False

        report = await report_subagent(
            ref.descriptor_id,
            user_id=parent["user_id"],
            parent_session_id=parent["session_id"],
            project_id=parent["project_id"],
        )
        assert report["outcome"] == outcome
        current_parent = await _new_parent_part(parent)
        follow = await accept_follow_up(
            descriptor_id=ref.descriptor_id,
            user_id=parent["user_id"],
            parent_session_id=parent["session_id"],
            parent_message_id=current_parent["message_id"],
            parent_part_id=current_parent["part_id"],
            parent_run_id=current_parent["lease"].run_id,
            parent_generation=current_parent["lease"].generation,
            task_title="Continue after terminal failure",
            prompt="next exact activation",
            authority_snapshot=_authority_snapshot(),
        )
        assert follow.child_session_id == ref.child_session_id
        assert follow.descriptor_generation == 2
        async with get_db_session() as db:
            child_status = (await db.execute(
                select(Session.status).where(Session.id == ref.child_session_id)
            )).scalar_one()
        assert child_status == "idle"
    finally:
        await current_parent["lease"].release(session_status="idle")


@pytest.mark.asyncio
async def test_expired_claim_is_taken_over_but_live_claim_is_not():
    parent = await _parent_tool(user_prefix="claim")
    try:
        ref = await _accept(parent)
        first = await claim_activation(ref.id, user_id=parent["user_id"], owner="dead")
        assert first is not None
        assert await claim_activation(ref.id, user_id=parent["user_id"], owner="early") is None
        async with get_db_session() as db:
            row = (await db.execute(
                select(SubagentActivation).where(SubagentActivation.id == ref.id)
            )).scalar_one()
            row.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        takeover = await claim_activation(
            ref.id, user_id=parent["user_id"], owner="recovery"
        )
        assert takeover is not None
        assert takeover.token != first.token
        assert await abandon_claim(takeover)
    finally:
        await parent["lease"].release(session_status="idle")


@pytest.mark.asyncio
async def test_expired_running_activation_becomes_unknown_and_is_not_replayed():
    from agent.driver import recover_expired_driver_records

    parent = await _parent_tool(user_prefix="unknown")
    try:
        ref = await _accept(parent)
        claim = await claim_activation(ref.id, user_id=parent["user_id"])
        assert claim is not None
        child_lease = await reserve_run(
            ref.child_session_id,
            parent["user_id"],
            trigger_message_id=ref.child_trigger_message_id,
        )
        assert await bind_claimed_activation(claim, child_lease)
        await child_lease.set_phase("running")
        await child_lease.stop_monitor()
        async with get_db_session() as db:
            state = (await db.execute(
                select(AgentDriverState).where(
                    AgentDriverState.session_id == ref.child_session_id
                )
            )).scalar_one()
            state.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        records = [
            record
            for record in await recover_expired_driver_records()
            if record.session_id == ref.child_session_id
        ]
        assert len(records) == 1
        assert await recover_subagent_outboxes(records) == 1
        report = await report_subagent(
            ref.descriptor_id,
            user_id=parent["user_id"],
            parent_session_id=parent["session_id"],
            project_id=parent["project_id"],
        )
        assert report["outcome"] == "outcome_unknown"
        assert report["result"]["metadata"]["error"] is True
    finally:
        await parent["lease"].release(session_status="idle")


@pytest.mark.asyncio
async def test_accept_transaction_failure_rolls_back_child_trigger_and_descriptor(monkeypatch):
    import session.session as session_mod

    parent = await _parent_tool(user_prefix="rollback")
    original = session_mod._insert_user_message_locked

    async def fail_after_child_trigger(*args, **kwargs):
        # Simulate the historical child/message -> handoff crash window. Both
        # rows already exist in this transaction when the process boundary
        # fails, so the gateway must roll them back with the descriptor.
        await original(*args, **kwargs)
        raise RuntimeError("crash inside acceptance")

    monkeypatch.setattr(
        session_mod,
        "_insert_user_message_locked",
        fail_after_child_trigger,
    )
    try:
        with pytest.raises(RuntimeError, match="crash inside acceptance"):
            await _accept(parent)
        async with get_db_session() as db:
            assert (await db.execute(
                select(func.count()).select_from(Session).where(
                    Session.parent_id == parent["session_id"]
                )
            )).scalar_one() == 0
            assert (await db.execute(
                select(func.count()).select_from(SubagentDescriptor)
            )).scalar_one() == 0
            assert (await db.execute(
                select(func.count()).select_from(Message).where(
                    Message.user_id == parent["user_id"],
                    Message.role == "user",
                )
            )).scalar_one() == 0
            part = (await db.execute(
                select(Part).where(Part.id == parent["part_id"])
            )).scalar_one()
        assert "metadata" not in part.data
    finally:
        monkeypatch.setattr(session_mod, "_insert_user_message_locked", original)
        await parent["lease"].release(session_status="idle")


@pytest.mark.asyncio
async def test_accept_commit_before_dispatch_is_cold_claimed_and_bound(monkeypatch):
    import agent.recovery as recovery_mod

    parent = await _parent_tool(user_prefix="cold")
    completed = asyncio.Event()
    seen = {}

    async def fake_drive(lease, asset_ids):
        seen.update(
            session_id=lease.session_id,
            generation=lease.generation,
            asset_ids=asset_ids,
        )
        await lease.release(session_status="idle")
        completed.set()

    monkeypatch.setattr(recovery_mod, "_run_recovered_prompt", fake_drive)
    try:
        ref = await _accept(parent)
        resumed = await recovery_mod.resume_claimable_subagent_activations()
        assert resumed == [ref.id]
        await asyncio.wait_for(completed.wait(), timeout=1)
        async with get_db_session() as db:
            activation = (await db.execute(
                select(SubagentActivation).where(SubagentActivation.id == ref.id)
            )).scalar_one()
        assert activation.state == "bound"
        assert activation.child_generation == seen["generation"]
        assert seen["session_id"] == ref.child_session_id
        assert seen["asset_ids"] == []
    finally:
        await parent["lease"].release(session_status="idle")


@pytest.mark.asyncio
async def test_pre_running_failure_replays_exact_trigger_once_then_blocks_old_marker(
    monkeypatch,
):
    from agent.driver import recover_expired_driver_records
    from agent.recovery import resume_reserved_prompts
    import agent.loop as loop_mod
    from tool.task import _dispatch_activation
    from tool.tool import ToolContext

    parent = await _parent_tool(user_prefix="pre-running")
    first_failed = asyncio.Event()
    calls = 0
    try:
        ref = await _accept(parent)

        async def fail_then_finish(session_id, user_id, *, lease):
            nonlocal calls
            calls += 1
            assert session_id == ref.child_session_id
            assert user_id == parent["user_id"]
            if calls == 1:
                # Match run_loop's swallowed pre-running failure: the exact
                # reserved marker is expired for safe recovery, not released.
                await lease.preserve_for_recovery(session_status="error")
                first_failed.set()
                return None
            await _add_answer(parent, ref, "executed exactly once after recovery")
            await lease.release(session_status="idle")
            return None

        monkeypatch.setattr(loop_mod, "run_loop", fail_then_finish)
        dispatch = asyncio.create_task(_dispatch_activation(
            ToolContext(
                session_id=parent["session_id"],
                user_id=parent["user_id"],
            ),
            ref,
            project_id=parent["project_id"],
        ))
        await asyncio.wait_for(first_failed.wait(), timeout=1)
        records = [
            record
            for record in await recover_expired_driver_records()
            if record.session_id == ref.child_session_id
        ]
        assert len(records) == 1
        resumed, invalid = await resume_reserved_prompts(records)
        assert resumed == [ref.child_session_id]
        assert invalid == []
        result = await asyncio.wait_for(dispatch, timeout=2)
        assert "executed exactly once after recovery" in result.output
        assert calls == 2

        # A stale copy of the old reserved marker can no longer replay after
        # the activation/outbox has become terminal.
        resumed_again, invalid_again = await resume_reserved_prompts(records)
        assert resumed_again == []
        assert invalid_again == records
        assert calls == 2
    finally:
        await parent["lease"].release(session_status="idle")


@pytest.mark.asyncio
async def test_terminalization_after_precheck_blocks_replay_after_takeover(
    monkeypatch,
):
    from agent.driver import recover_expired_driver_records
    import agent.recovery as recovery_mod
    import agent.loop as loop_mod

    parent = await _parent_tool(user_prefix="terminal-takeover")
    ran = 0
    try:
        ref = await _accept(parent)
        claim = await claim_activation(ref.id, user_id=parent["user_id"])
        assert claim is not None
        old = await reserve_run(
            ref.child_session_id,
            parent["user_id"],
            trigger_message_id=ref.child_trigger_message_id,
        )
        assert await bind_claimed_activation(claim, old)
        await old.stop_monitor()
        async with get_db_session() as db:
            state = (await db.execute(
                select(AgentDriverState).where(
                    AgentDriverState.session_id == ref.child_session_id
                )
            )).scalar_one()
            state.lease_expires_at = (
                datetime.now(timezone.utc) - timedelta(seconds=1)
            )
        records = [
            record
            for record in await recover_expired_driver_records()
            if record.session_id == ref.child_session_id
        ]
        assert len(records) == 1
        record = records[0]

        original_takeover = recovery_mod.reserve_recovered_run
        interleaved = False

        async def terminalize_then_takeover(recovered, *, initial_phase):
            nonlocal interleaved
            if not interleaved:
                interleaved = True
                await complete_activation_from_transcript(
                    ref.id,
                    child_run_id=old.run_id,
                    child_generation=old.generation,
                )
            return await original_takeover(
                recovered,
                initial_phase=initial_phase,
            )

        async def forbidden_run(*_args, **_kwargs):
            nonlocal ran
            ran += 1
            raise AssertionError("terminal activation trigger must not replay")

        monkeypatch.setattr(
            recovery_mod,
            "reserve_recovered_run",
            terminalize_then_takeover,
        )
        monkeypatch.setattr(loop_mod, "run_loop", forbidden_run)

        resumed, invalid = await recovery_mod.resume_reserved_prompts([record])
        assert resumed == []
        assert len(invalid) == 1
        repair_record = invalid[0]
        assert repair_record.run_id != record.run_id
        assert repair_record.generation == record.generation + 1
        assert repair_record.trigger_message_id == record.trigger_message_id
        assert ran == 0

        # The delayed old owner cannot clear the replacement repair marker.
        assert await old.release(session_status="idle") is False
        state = await get_driver_state(ref.child_session_id)
        assert state is not None
        assert state.run_id == repair_record.run_id
        assert state.generation == repair_record.generation
        assert state.phase == "reserved"

        repairs = await recovery_mod.repair_expired_sessions(invalid)
        assert len(repairs) == 1 and repairs[0].skipped is False
        assert ran == 0
        state = await get_driver_state(ref.child_session_id)
        assert state is not None and state.phase == "idle"
    finally:
        await parent["lease"].release(session_status="idle")


@pytest.mark.asyncio
async def test_ready_outbox_projects_exact_parent_part_once():
    parent = await _parent_tool(user_prefix="delivery")
    maintenance = None
    try:
        ref = await _accept(parent)
        claim = await claim_activation(ref.id, user_id=parent["user_id"])
        assert claim is not None
        child_lease = await reserve_run(
            ref.child_session_id,
            parent["user_id"],
            trigger_message_id=ref.child_trigger_message_id,
        )
        assert await bind_claimed_activation(claim, child_lease)
        await _add_answer(parent, ref, "exact result")
        await child_lease.release(session_status="idle")
        await complete_activation_from_transcript(
            ref.id,
            child_run_id=child_lease.run_id,
            child_generation=child_lease.generation,
        )
        await parent["lease"].release(session_status="idle")
        maintenance = await reserve_run(
            parent["session_id"], parent["user_id"], initial_phase="finalizing"
        )
        async with get_db_session() as db:
            first = await apply_ready_subagent_outboxes_locked(
                db,
                parent_session_id=parent["session_id"],
                user_id=parent["user_id"],
                maintenance_run_id=maintenance.run_id,
                maintenance_generation=maintenance.generation,
            )
        assert first.rejoined == 1
        async with get_db_session() as db:
            part = (await db.execute(
                select(Part).where(Part.id == parent["part_id"])
            )).scalar_one()
            outbox = (await db.execute(
                select(SubagentOutbox).where(SubagentOutbox.activation_id == ref.id)
            )).scalar_one()
        assert part.data["status"] == "completed"
        assert part.data["metadata"]["subagent_activation_id"] == ref.id
        assert "exact result" in part.data["output"]
        assert outbox.state == "delivered"
        async with get_db_session() as db:
            second = await apply_ready_subagent_outboxes_locked(
                db,
                parent_session_id=parent["session_id"],
                user_id=parent["user_id"],
                maintenance_run_id=maintenance.run_id,
                maintenance_generation=maintenance.generation,
            )
        assert second.rejoined == 0
    finally:
        if maintenance is not None:
            await maintenance.release(session_status="idle")


@pytest.mark.asyncio
async def test_live_parent_delivery_is_acknowledged_after_terminal_message():
    """Exact live delivery remains provable after the Task part is historical."""
    parent = await _parent_tool(user_prefix="subagent-live-ack")
    child_lease = None
    try:
        ref = await _accept(parent, lifecycle="one_shot")
        claim = await claim_activation(ref.id, user_id=parent["user_id"])
        assert claim is not None
        child_lease = await reserve_run(
            ref.child_session_id,
            parent["user_id"],
            trigger_message_id=ref.child_trigger_message_id,
        )
        assert await bind_claimed_activation(claim, child_lease)
        await _add_answer(parent, ref, "live exact result")
        await child_lease.release(session_status="idle")
        projected = await complete_activation_from_transcript(
            ref.id,
            child_run_id=child_lease.run_id,
            child_generation=child_lease.generation,
        )
        now = datetime.now(timezone.utc)
        async with get_db_session() as db:
            part = await db.get(Part, parent["part_id"])
            assert part is not None
            part.data = {
                **dict(part.data or {}),
                "status": "completed",
                "output": projected["output"],
                "metadata": projected["metadata"],
            }
            db.add(Message(
                id=f"subagent-parent-final-{uuid.uuid4().hex[:12]}",
                session_id=parent["session_id"],
                user_id=parent["user_id"],
                role="assistant",
                parent_id=parent["message_id"],
                finish="stop",
                created_at=now + timedelta(seconds=1),
            ))
        await parent["lease"].release(session_status="idle")

        assert await ready_subagent_parent_sessions() == []
        async with get_db_session() as db:
            outbox = await db.get(SubagentOutbox, ref.id)
            assert outbox is not None
            assert outbox.state == "delivered"
            assert outbox.delivered_at is not None
    finally:
        if child_lease is not None:
            await child_lease.release(session_status="idle")
        await parent["lease"].release(session_status="idle")


@pytest.mark.asyncio
async def test_v2_composition_is_durable_propagated_and_follow_up_monotonic(
    monkeypatch,
):
    from agent.agent import get_agent
    from agent.subagent_authority import (
        SubagentAuthorityError,
        load_subagent_authority,
    )
    from agent.subagent_composition import SubagentCompositionError
    from core.config import get_config
    from session.session import get_session

    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    parent = await _parent_tool(user_prefix="composition-v2")
    current_parent = parent
    try:
        config = _ready_composition_config()
        monkeypatch.setattr("core.config.get_config", lambda: config)
        snapshot = _v2_authority(
            tools=["read"],
            reasoning="medium",
            persona="Use the frozen investigator persona.",
            output_schema=schema,
            config=config,
        )
        parsed = parse_subagent_authority(snapshot)
        assert parsed.composition is not None
        ref = await accept_spawn(
            user_id=parent["user_id"],
            parent_session_id=parent["session_id"],
            parent_message_id=parent["message_id"],
            parent_part_id=parent["part_id"],
            parent_run_id=parent["lease"].run_id,
            parent_generation=parent["lease"].generation,
            task_title="Frozen composition",
            prompt="inspect exactly",
            subagent_type="explore",
            child_model=parsed.composition.model,
            lifecycle="continuable",
            authority_snapshot=snapshot,
        )
        async with get_db_session() as db:
            descriptor = await db.get(SubagentDescriptor, ref.descriptor_id)
            trigger = await db.get(Message, ref.child_trigger_message_id)
            assert descriptor is not None and trigger is not None
            assert descriptor.authority_snapshot["version"] == 2
            assert descriptor.authority_snapshot["composition"]["digest"]
            assert trigger.model == config.model
            assert trigger.variant == "medium"
            assert trigger.format == schema

        # A cold worker restores the accepted preset, including the private
        # persona overlay, rather than consulting a later live registry value.
        child = await get_session(ref.child_session_id, user_id=parent["user_id"])
        restored = await load_subagent_authority(child)
        assert restored is not None and restored.composition is not None
        frozen_agent = get_agent("explore")
        assert frozen_agent.tools == ["read"] or set(frozen_agent.tools) >= {"read"}
        assert frozen_agent.model == config.model
        assert "Use the frozen investigator persona." in (frozen_agent.prompt or "")
        assert restored.tool_ids == frozenset({"read"})

        claim = await claim_activation(ref.id, user_id=parent["user_id"])
        assert claim is not None
        child_lease = await reserve_run(
            ref.child_session_id,
            parent["user_id"],
            trigger_message_id=ref.child_trigger_message_id,
        )
        assert await bind_claimed_activation(claim, child_lease)
        await _add_answer(
            parent,
            ref,
            "text is secondary",
            structured={"answer": "finished"},
            include_text=False,
        )
        await child_lease.release(session_status="idle")
        completed = await complete_activation_from_transcript(
            ref.id,
            child_run_id=child_lease.run_id,
            child_generation=child_lease.generation,
        )
        assert completed["metadata"]["structured_result"] == {
            "answer": "finished"
        }
        current_parent = await _new_parent_part(parent)

        with pytest.raises(SubagentCompositionError, match="cannot change"):
            await accept_follow_up(
                descriptor_id=ref.descriptor_id,
                user_id=parent["user_id"],
                parent_session_id=parent["session_id"],
                parent_message_id=current_parent["message_id"],
                parent_part_id=current_parent["part_id"],
                parent_run_id=current_parent["lease"].run_id,
                parent_generation=current_parent["lease"].generation,
                task_title="illegal widening",
                prompt="change model",
                authority_snapshot=_authority_snapshot(),
                requested_model="openai/gpt-5.4",
            )
        follow = await accept_follow_up(
            descriptor_id=ref.descriptor_id,
            user_id=parent["user_id"],
            parent_session_id=parent["session_id"],
            parent_message_id=current_parent["message_id"],
            parent_part_id=current_parent["part_id"],
            parent_run_id=current_parent["lease"].run_id,
            parent_generation=current_parent["lease"].generation,
            task_title="exact continuation",
            prompt="continue",
            authority_snapshot=_authority_snapshot(tools={"task", "read"}),
            requested_tools=["read"],
        )
        async with get_db_session() as db:
            descriptor = await db.get(SubagentDescriptor, ref.descriptor_id)
            trigger = await db.get(Message, follow.child_trigger_message_id)
            assert descriptor is not None and trigger is not None
            narrowed = parse_subagent_authority(descriptor.authority_snapshot)
            assert narrowed.tool_ids == frozenset({"read"})
            assert narrowed.composition is not None
            assert narrowed.composition.model == config.model
            assert trigger.variant == "medium"
            assert trigger.format == schema

        slot = config.model.split("/", 1)[0]
        config.provider[slot].base_url = "https://drifted-provider.invalid/v1"
        with pytest.raises(SubagentAuthorityError, match="capabilities changed"):
            await load_subagent_authority(child)
    finally:
        await current_parent["lease"].release(session_status="idle")


@pytest.mark.asyncio
async def test_task_fork_copies_only_last_closed_canonical_prefix(monkeypatch):
    from db.models.agent_event import AgentEvent
    from session.event_range import freeze_fork_event_range
    from session.session import get_messages

    parent = await _prepare_fork_parent()
    try:
        config = _ready_composition_config()
        monkeypatch.setattr("core.config.get_config", lambda: config)
        frozen = await freeze_fork_event_range(
            parent["session_id"],
            user_id=parent["user_id"],
            up_to_message_id=None,
        )
        assert frozen.covered_message_ids == (
            parent["closed_user_id"],
            parent["closed_assistant_id"],
        )
        snapshot = _v2_authority(
            seed_mode="fork", tools=["read"], config=config,
        )
        parsed = parse_subagent_authority(snapshot)
        assert parsed.composition is not None
        ref = await accept_spawn(
            user_id=parent["user_id"],
            parent_session_id=parent["session_id"],
            parent_message_id=parent["message_id"],
            parent_part_id=parent["part_id"],
            parent_run_id=parent["lease"].run_id,
            parent_generation=parent["lease"].generation,
            task_title="Fork closed context",
            prompt="delegated fork prompt",
            subagent_type="explore",
            child_model=parsed.composition.model,
            lifecycle="continuable",
            authority_snapshot=snapshot,
            fork_seed=frozen,
        )
        child_messages = await get_messages(
            ref.child_session_id,
            user_id=parent["user_id"],
        )
        text = "\n".join(
            str(getattr(part, "text", ""))
            for message in child_messages
            for part in message.parts
        )
        assert "closed parent prompt" in text
        assert "closed parent answer" in text
        assert "delegated fork prompt" in text
        assert "open parent prompt must not be copied" not in text
        assert len(child_messages) == 3
        async with get_db_session() as db:
            lineage = (await db.execute(select(AgentEvent).where(
                AgentEvent.session_id == ref.child_session_id,
                AgentEvent.kind == "session.forked",
            ))).scalar_one()
        assert lineage.payload["source"]["canonical_digest"] == frozen.canonical_digest
        assert lineage.payload["source"]["covered_message_ids"] == list(
            frozen.covered_message_ids
        )
    finally:
        await parent["lease"].release(session_status="idle")


@pytest.mark.asyncio
async def test_task_fork_fault_rolls_back_child_descriptor_and_parent_pointer(monkeypatch):
    from session.event_range import freeze_fork_event_range
    import session.fork as fork_mod

    parent = await _prepare_fork_parent(user_prefix="fork-fault")
    try:
        config = _ready_composition_config()
        monkeypatch.setattr("core.config.get_config", lambda: config)
        frozen = await freeze_fork_event_range(
            parent["session_id"],
            user_id=parent["user_id"],
            up_to_message_id=None,
        )
        snapshot = _v2_authority(
            seed_mode="fork", tools=["read"], config=config,
        )
        parsed = parse_subagent_authority(snapshot)
        assert parsed.composition is not None
        original_append = fork_mod.append_agent_event_locked

        async def fail_lineage(*args, **kwargs):
            if kwargs.get("kind") == "session.forked":
                raise RuntimeError("injected fork lineage failure")
            return await original_append(*args, **kwargs)

        monkeypatch.setattr(fork_mod, "append_agent_event_locked", fail_lineage)
        with pytest.raises(RuntimeError, match="injected fork lineage failure"):
            await accept_spawn(
                user_id=parent["user_id"],
                parent_session_id=parent["session_id"],
                parent_message_id=parent["message_id"],
                parent_part_id=parent["part_id"],
                parent_run_id=parent["lease"].run_id,
                parent_generation=parent["lease"].generation,
                task_title="Faulted fork",
                prompt="must roll back",
                subagent_type="explore",
                child_model=parsed.composition.model,
                lifecycle="continuable",
                authority_snapshot=snapshot,
                fork_seed=frozen,
            )
        async with get_db_session() as db:
            children = list((await db.execute(select(Session).where(
                Session.parent_id == parent["session_id"],
                Session.user_id == parent["user_id"],
            ))).scalars().all())
            descriptors = list((await db.execute(select(SubagentDescriptor).where(
                SubagentDescriptor.parent_session_id == parent["session_id"],
            ))).scalars().all())
            part = await db.get(Part, parent["part_id"])
        assert children == []
        assert descriptors == []
        assert part is not None
        assert "subagent_id" not in (part.data.get("metadata") or {})
    finally:
        await parent["lease"].release(session_status="idle")


@pytest.mark.asyncio
async def test_real_loop_structured_child_closes_and_exports_task_outbox(
    monkeypatch,
):
    from agent import loop
    from agent.processor import StepOutcome, StepResult
    from agent.structured_output import TOOL_NAME as STRUCTURED_OUTPUT_TOOL
    from session.agent_event_log import verify_agent_event_parity
    from session.event_range import freeze_fork_event_range
    from session.fork import fork_session
    from tests.unit.test_agent_loop_terminal_steps import (
        _assert_balanced_steps,
        _loop_config,
        _patch_real_loop_runtime,
    )

    schema = {
        "type": "object",
        "properties": {"answer": {"type": "string"}},
        "required": ["answer"],
    }
    parent = await _parent_tool(user_prefix="real-loop-structured")
    child_lease = None
    try:
        config = _loop_config()
        config.provider["openai"].subagent_capabilities = [
            "model",
            "tool_filter",
            "reasoning",
            "persona",
            "output_schema",
        ]
        config.provider["openai"].subagent_reasoning_variants = [
            "low",
            "medium",
            "high",
        ]
        config.tool_exposure.mode = "legacy_eager"
        monkeypatch.setattr("core.config.get_config", lambda: config)
        snapshot = _v2_authority(
            tools=["read"],
            output_schema=schema,
            config=config,
        )
        parsed = parse_subagent_authority(snapshot)
        assert parsed.composition is not None
        ref = await accept_spawn(
            user_id=parent["user_id"],
            parent_session_id=parent["session_id"],
            parent_message_id=parent["message_id"],
            parent_part_id=parent["part_id"],
            parent_run_id=parent["lease"].run_id,
            parent_generation=parent["lease"].generation,
            task_title="Structured child",
            prompt="Return the accepted structured result.",
            subagent_type="explore",
            child_model=parsed.composition.model,
            lifecycle="continuable",
            authority_snapshot=snapshot,
        )
        claim = await claim_activation(ref.id, user_id=parent["user_id"])
        assert claim is not None
        child_lease = await reserve_run(
            ref.child_session_id,
            parent["user_id"],
            trigger_message_id=ref.child_trigger_message_id,
        )
        assert await bind_claimed_activation(claim, child_lease)
        provider_calls = 0

        async def _structured_step(**kwargs):
            nonlocal provider_calls
            provider_calls += 1
            result = await kwargs["tools"][STRUCTURED_OUTPUT_TOOL].execute(
                {"answer": "finished by real loop"},
                kwargs["ctx"],
            )
            assert result.metadata["structured"] is True
            return StepResult(
                outcome=StepOutcome.CONTINUE,
                finish_reason="tool_calls",
                usage={
                    "input": 17,
                    "output": 4,
                    "total": 21,
                    "cost": 0.02,
                },
                duration=0.5,
            )

        _patch_real_loop_runtime(
            monkeypatch,
            config=config,
            process_step=_structured_step,
        )
        terminal = await loop.run_loop(
            ref.child_session_id,
            user_id=parent["user_id"],
            lease=child_lease,
        )
        assert provider_calls == 1
        assert terminal is not None
        assert terminal.structured == {"answer": "finished by real loop"}
        assistants = await _assert_balanced_steps(
            ref.child_session_id,
            parent["user_id"],
            1,
        )
        parity = await verify_agent_event_parity(
            ref.child_session_id,
            user_id=parent["user_id"],
            require_closed=True,
        )
        assert parity.ok is True, parity.model_dump()
        frozen = await freeze_fork_event_range(
            ref.child_session_id,
            user_id=parent["user_id"],
            up_to_message_id=assistants[0].id,
        )
        assert frozen.covered_message_ids[-1] == assistants[0].id
        forked = await fork_session(
            ref.child_session_id,
            up_to_message_id=assistants[0].id,
            user_id=parent["user_id"],
        )
        fork_parity = await verify_agent_event_parity(
            forked.id,
            user_id=parent["user_id"],
            require_closed=True,
        )
        assert fork_parity.ok is True, fork_parity.model_dump()

        completed = await complete_activation_from_transcript(
            ref.id,
            child_run_id=child_lease.run_id,
            child_generation=child_lease.generation,
        )
        assert completed["metadata"]["structured_result"] == {
            "answer": "finished by real loop",
        }
        assert completed["metadata"]["task_outbox_completed"] is True
        async with get_db_session() as db:
            outbox = await db.get(SubagentOutbox, ref.id)
        assert outbox is not None
        assert outbox.state == "ready"
        assert outbox.result_payload["projected"] == completed
    finally:
        if child_lease is not None:
            await child_lease.release(session_status="error")
        await parent["lease"].release(session_status="error")


def test_partial_task_tool_replay_identity_stays_fail_closed():
    from types import SimpleNamespace

    from session.agent_event_log import AgentEventProjectionError, private_part_state

    partial = SimpleNamespace(
        canonical_tool_id="task",
        wire_tool_name=None,
        provider_binding_digest=None,
        provider_dialect=None,
        stream_seq=None,
    )
    with pytest.raises(AgentEventProjectionError, match="partial"):
        private_part_state(partial)
