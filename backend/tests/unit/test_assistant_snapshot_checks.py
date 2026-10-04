"""Projection reuse must preserve fresh authority and per-answer graph limits."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select

from assistant.evidence import validate_business_reads, validate_message_sources
from assistant.policy import AssistantError
from assistant.public_history import public_messages
from assistant.results import part_hash, validate_result_source
from assistant.service import ensure_main_session
from assistant.snapshot import get_snapshot
from assistant.task_context import validate_task_snapshots
from assistant.transactions import SnapshotChecks, begin_snapshot
from db.base import get_db_session, get_engine
from db.models.agent_event import AgentEvent
from db.models.assistant import TaskResult
from db.models.message import Message
from db.models.part import Part
from db.models.session import Session
from session.session import get_messages, get_session
from tests.unit.assistant_source_fixtures import consume_context
from tests.unit.test_assistant_business_context import add_task
from tests.unit.test_assistant_context_sources import finish, next_turn
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_reads import call_tool, cursor_key, read_turn  # noqa: F401


def stable(value):
    if isinstance(value, dict):
        return {k: stable(v) for k, v in value.items()
                if k not in {"source_checked_at", "display_token", "event_cursor"}}
    if isinstance(value, list):
        return [stable(v) for v in value]
    return value


@pytest.mark.parametrize("surface", ["history", "snapshot"])
async def test_repeated_answer_dependencies_reduce_sql_without_changing_projection_or_revocation(monkeypatch, surface):
    ctx, lease, answer, accepted, report = await read_turn()
    try:
        await call_tool(ctx, "tasks.list", {})
        await call_tool(ctx, "history.read", {"session_id": accepted["execution_session_id"], "message_ids": [report.id]})
        for index in range(4):
            await consume_context(ctx)
            await finish(ctx, lease, answer, f"PRIVATE_REUSED_ANSWER_{index}")
            if index < 3:
                ctx, lease, answer = await next_turn(ctx)
        session = await get_session(ctx.session_id, user_id=ctx.user_id)
        messages = await get_messages(ctx.session_id, user_id=ctx.user_id)

        async def read():
            if surface == "history":
                return await public_messages(session, messages, actor_user_id=ctx.user_id)
            return await get_snapshot(user_id=ctx.user_id, workspace_id=ctx.workspace_id)

        statements = []
        def count(*args):
            statements.append(args[2])
        engine = get_engine().sync_engine
        event.listen(engine, "before_cursor_execute", count)
        try:
            optimized = await read()
            optimized_count = len(statements)
            statements.clear()
            async def no_reuse(db):
                await begin_snapshot(db)
                return None
            with monkeypatch.context() as patch:
                patch.setattr(f"assistant.{'public_history' if surface == 'history' else 'snapshot'}.begin_snapshot", no_reuse)
                baseline = await read()
            baseline_count = len(statements)
        finally:
            event.remove(engine, "before_cursor_execute", count)
        assert stable(optimized) == stable(baseline)
        assert optimized_count < baseline_count * 0.65, (optimized_count, baseline_count)
        # The memory bound changes work, never the result or graph budget.
        with monkeypatch.context() as patch:
            patch.setattr(SnapshotChecks, "MAX_ENTRIES", 1)
            assert stable(await read()) == stable(baseline)

        async with get_db_session() as db:
            source = await db.scalar(select(Part).where(Part.message_id == report.id, Part.type == "text"))
            source.data = {**source.data, "text": "Replaced original evidence"}
        revoked = await read()
        if surface == "history":
            assert all(row["parts"] == [] for row in revoked if row["id"] == answer.id)
        else:
            assert not next(row for row in revoked["answers"] if row["message_id"] == answer.id)["available"]
    finally:
        await lease.release(session_status="idle")


async def test_historical_reuse_cannot_certify_fresh_tampered_oversized_or_wrong_scope_reads():
    ctx, lease, _, _, _ = await read_turn()
    try:
        await call_tool(ctx, "tasks.list", {})
        await consume_context(ctx)
        read = deepcopy(ctx._assistant_context["business_reads"][0])
        refs = deepcopy(ctx._assistant_context["task_snapshots"])
        await add_task(ctx)
        async with get_db_session() as db:
            checks = await begin_snapshot(db)
            main = await db.get(Session, ctx.session_id)
            scope = dict(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
            await validate_business_reads(db, [read], **scope, snapshot_checks=checks)
            await validate_task_snapshots(db, main, refs, snapshot_checks=checks)
            with pytest.raises(AssistantError, match="refresh"):
                await validate_business_reads(db, [read], **scope, fresh=True, snapshot_checks=checks)
            damaged = deepcopy(read)
            damaged["projection"]["items"][0]["title"] = "Tampered observation"
            for invalid in ([damaged], [read] * 201, [{**read, "version": 99}]):
                with pytest.raises(AssistantError):
                    await validate_business_reads(db, invalid, **scope, snapshot_checks=checks)
            changed = deepcopy(refs)
            changed[0]["snapshot"]["task"]["title"] = "Tampered task"
            for invalid in (changed, [refs[0]] * 201):
                with pytest.raises(AssistantError):
                    await validate_task_snapshots(db, main, invalid, snapshot_checks=checks)
            for wrong in ({"main_id": "another-main"}, {"user_id": "another-user"}, {"workspace_id": "another-workspace"}):
                with pytest.raises(AssistantError):
                    await validate_business_reads(db, [read], **(scope | wrong), snapshot_checks=checks)
            await db.rollback()
            await begin_snapshot(db)
            with pytest.raises(RuntimeError, match="original read-only snapshot"):
                await validate_business_reads(db, [read], **scope, snapshot_checks=checks)
        async with get_db_session() as db:
            await begin_snapshot(db)
            with pytest.raises(RuntimeError, match="original read-only snapshot"):
                await validate_business_reads(db, [read], **scope, snapshot_checks=checks)
    finally:
        await lease.release(session_status="idle")


async def test_cached_result_still_checks_exact_reference_bytes_and_scope():
    ctx, lease, _, accepted, _ = await read_turn()
    try:
        async with get_db_session() as db:
            checks = await begin_snapshot(db)
            result = await db.scalar(select(TaskResult).where(TaskResult.task_id == accepted["task_id"]))
            scope = dict(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
            await validate_result_source(db, result, **scope, snapshot_checks=checks)
            damaged = SimpleNamespace(id=result.id, task_id=result.task_id, output_refs=deepcopy(result.output_refs))
            damaged.output_refs[-1]["content_hash"] = "0" * 64
            with pytest.raises(AssistantError):
                await validate_result_source(db, damaged, **scope, snapshot_checks=checks)
            with pytest.raises(AssistantError):
                await validate_result_source(db, result, **(scope | {"main_id": "another-main"}), snapshot_checks=checks)
    finally:
        await lease.release(session_status="idle")


async def test_shared_original_rows_do_not_share_graph_depth_cycles_or_cardinality():
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    now = datetime.now(timezone.utc)
    messages, parts, events = [], [], []
    def node(role, refs=()):
        message = Message(id=uuid4().hex, session_id=main.id, user_id=owner, role=role,
                          finish="stop", summary=False, created_at=now)
        part = Part(id=uuid4().hex, session_id=main.id, user_id=owner, message_id=message.id,
                    type="text", data={"text": message.id}, created_at=now)
        messages.append(message)
        parts.append(part)
        if role == "assistant":
            events.append(AgentEvent(id=uuid4().hex, event_key=uuid4().hex, sequence=len(events) + 1,
                session_id=main.id, user_id=owner, message_id=message.id, kind="assistant.message.committed",
                payload={"provenance_version": 2, "context_verified": True, "source_refs": list(refs)}, created_at=now))
        return message, {"session_id": main.id, "message_id": message.id, "part_id": part.id, "content_hash": part_hash(part)}
    originals = [node("user")[1] for _ in range(240)]
    left, left_ref = node("assistant", originals[:120])
    right, right_ref = node("assistant", originals[120:])
    over, _ = node("assistant", [left_ref, right_ref])
    chain, reference = node("assistant")
    for _ in range(32):
        chain, reference = node("assistant", [reference])
    too_deep, _ = node("assistant", [reference])
    cycle_a, a_ref = node("assistant")
    cycle_b, b_ref = node("assistant", [a_ref])
    events[-2].payload["source_refs"] = [b_ref]
    async with get_db_session() as db:
        high_water = await db.scalar(select(func.coalesce(func.max(AgentEvent.sequence), 0)).where(
            AgentEvent.session_id == main.id))
        for item in events:
            item.sequence += high_water
        db.add_all(messages)
        await db.flush()
        db.add_all(parts + events)
    async with get_db_session() as db:
        checks = await begin_snapshot(db)
        scope = dict(user_id=owner, workspace_id=workspace, main_id=main.id, snapshot_checks=checks)
        # Each independently valid root has its own 200-source budget.
        for message in (left, right, chain):
            await validate_message_sources(db, message, **scope)
        for message in (over, too_deep, cycle_a, cycle_b):
            with pytest.raises(AssistantError) as rejected:
                await validate_message_sources(db, message, **scope)
            assert rejected.value.code == "ASSISTANT_SOURCE_UNVERIFIED"
