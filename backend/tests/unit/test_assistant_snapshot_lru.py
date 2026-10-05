"""A full read-snapshot cache must retain hot facts without retaining authority."""
import asyncio
from contextvars import Context
from copy import deepcopy
from datetime import datetime, timezone
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from assistant.commands import _authority, command_digest
from assistant.evidence import validate_source_ref
from assistant.policy import AssistantError
from assistant.public_history import public_messages
from assistant.results import part_hash
from assistant.service import ensure_main_session
from assistant.task_context import validate_task_snapshots
from assistant.transactions import SnapshotChecks, begin_snapshot, source_snapshot
from core.identifier import ascending
from db.base import get_db_session, get_engine
from db.models.agent_event import AgentEvent
from db.models.message import Message
from db.models.part import Part
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from session.session import get_messages
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_snapshot_checks import stable
from tests.unit.test_assistant_snapshot_task_facts import facts_world, queries


async def source_world(count, answer_ranges=()):
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    now = datetime.now(timezone.utc)
    messages, parts, refs, events, answers = [], [], [], [], []
    for index in range(count):
        message = Message(id=ascending("message"), session_id=main.id, user_id=owner,
            role="user", finish="stop", summary=False, created_at=now)
        part_id = ascending("part")
        part = Part(id=part_id, session_id=main.id, user_id=owner, message_id=message.id,
            type="text", data={"id": part_id, "type": "text", "text": f"Independent original {index}",
                "session_id": main.id, "message_id": message.id, "origin": "human"}, created_at=now)
        messages.append(message)
        parts.append(part)
        refs.append({"session_id": main.id, "message_id": message.id,
                     "part_id": part.id, "content_hash": part_hash(part)})
    for index, (start, end) in enumerate(answer_ranges):
        answer = Message(id=ascending("message"), session_id=main.id, user_id=owner,
            role="assistant", finish="stop", summary=False, created_at=now)
        messages.append(answer)
        answers.append(answer.id)
        part_id = ascending("part")
        parts.append(Part(id=part_id, session_id=main.id, user_id=owner, message_id=answer.id,
            type="text", data={"id": part_id, "type": "text", "text": f"Verified answer {index}",
                "session_id": main.id, "message_id": answer.id}, created_at=now))
        events.append(AgentEvent(id=uuid4().hex, event_key=uuid4().hex, sequence=index + 1,
            session_id=main.id, user_id=owner, message_id=answer.id, kind="assistant.message.committed",
            payload={"provenance_version": 2, "context_verified": True, "source_refs": refs[start:end]},
            created_at=now))
    async with get_db_session() as db:
        high_water = await db.scalar(select(func.coalesce(func.max(AgentEvent.sequence), 0)).where(
            AgentEvent.session_id == main.id))
        for event in events:
            event.sequence += high_water
        db.add_all(messages)
        await db.flush()
        db.add_all(parts + events)
    return main, refs, answers


async def source(db, checks, main, ref):
    return await validate_source_ref(db, ref, user_id=main.user_id,
        workspace_id=main.workspace_id, main_id=main.id,
        validation={"messages": set(), "refs": {}, "snapshot_checks": checks})


async def test_full_512_cache_keeps_the_last_history_branch_hot_and_exact(monkeypatch, record_property):
    # Each answer has its own valid 200-source graph. The third branch exceeds
    # the shared 512-entry cache, and the fourth reuses that recent branch.
    main, _, answer_ids = await source_world(600, ((0, 200), (200, 400), (400, 600), (400, 600)))
    loaded = {row.id: row for row in await get_messages(main.id, user_id=main.user_id)}
    messages = [loaded[identity] for identity in answer_ids]
    from assistant import public_history
    original = public_history.validate_message_sources
    per_answer = []
    async def observe(*args, **kwargs):
        with queries() as statements:
            value = await original(*args, **kwargs)
        per_answer.append(len(statements))
        return value
    monkeypatch.setattr(public_history, "validate_message_sources", observe)
    with queries() as optimized_sql:
        optimized = await public_messages(main, messages, actor_user_id=main.user_id)
    optimized_counts = list(per_answer)
    per_answer.clear()
    with monkeypatch.context() as patch:
        patch.setattr(SnapshotChecks, "MAX_ENTRIES", 0)
        with queries() as uncached_sql:
            uncached = await public_messages(main, messages, actor_user_id=main.user_id)
    record_property("history_sql", json.dumps({"optimized": len(optimized_sql), "uncached": len(uncached_sql),
        "per_answer": optimized_counts, "uncached_per_answer": per_answer}))
    assert stable(optimized) == stable(uncached)
    assert [row["source_status"] for row in optimized] == ["available"] * 4
    # Only the fourth answer's own persisted manifest needs to be read.
    assert optimized_counts[-1] == 1, optimized_counts
    assert len(optimized_sql) < len(uncached_sql) - 150


async def test_fact_batches_share_capacity_promote_hits_and_return_evicted_batch_values(monkeypatch, record_property):
    main, refs = await facts_world(5)
    refs = deepcopy(refs)
    for ref in refs:
        # A legitimate earlier observation can predate the latest result.
        ref["snapshot"]["latest_result"] = None
        ref["snapshot_digest"] = command_digest(ref["snapshot"])
    monkeypatch.setattr(SnapshotChecks, "MAX_ENTRIES", 3)
    counts = []
    async with source_snapshot(reuse_task_facts=True) as (db, checks):
        for selected in (refs[:3], refs[:1], refs[3:4], [refs[0], refs[2], refs[3]], refs[1:2]):
            with queries() as statements:
                assert await validate_task_snapshots(db, main, selected, snapshot_checks=checks) is None
            counts.append(len(statements))
            assert len(checks._values) <= 3
        # The requested batch is larger than the entire cache. It still
        # validates every returned immutable fact in the original order.
        with queries() as batch_sql:
            await validate_task_snapshots(db, main, refs, snapshot_checks=checks)
        assert len(batch_sql) == 1
        assert len(checks._values) == 3
        before = len(checks._values)
        await _authority(db, user_id=main.user_id, workspace_id=main.workspace_id,
                         main_id=main.id, snapshot_checks=checks)
        assert len(checks._values) == before  # Check and fact entries share one bound.
    record_property("task_batch_sql", json.dumps(counts))
    assert counts == [1, 0, 1, 0, 1]


async def test_failed_source_reference_never_evicts_or_certifies_a_warm_original(monkeypatch):
    main, refs, _ = await source_world(3)
    monkeypatch.setattr(SnapshotChecks, "MAX_ENTRIES", 2)
    async with source_snapshot(reuse_task_facts=True) as (db, checks):
        for ref in refs[:2]:
            await source(db, checks, main, ref)
        invalid = {**refs[2], "content_hash": "0" * 64}
        for _ in range(2):
            with queries() as failed_sql, pytest.raises(AssistantError) as denied:
                await source(db, checks, main, invalid)
            assert denied.value.code == "ASSISTANT_SOURCE_CHANGED"
            assert len(failed_sql) == 1 and len(checks._values) == 2
        with queries() as warm_sql:
            for ref in refs[:2]:
                assert part_hash(await source(db, checks, main, ref)) == ref["content_hash"]
        assert len(warm_sql) == 0
        with queries() as new_sql:
            assert part_hash(await source(db, checks, main, refs[2])) == refs[2]["content_hash"]
        assert len(new_sql) == 1
        with queries() as hot_sql:
            await source(db, checks, main, refs[2])
        assert len(hot_sql) == 0
        with queries() as evicted_sql:
            await source(db, checks, main, refs[0])
        assert len(evicted_sql) == 1


@pytest.mark.parametrize("mutation", ["dirty", "flush", "bulk", "transaction", "foreign_db"])
async def test_lru_hit_cannot_cross_a_local_write_or_transaction_change(monkeypatch, mutation):
    main, refs, _ = await source_world(3)
    monkeypatch.setattr(SnapshotChecks, "MAX_ENTRIES", 2)
    async with source_snapshot(reuse_task_facts=True) as (db, checks):
        for ref in refs:
            await source(db, checks, main, ref)
        held = await db.get(Session, main.id)
        if mutation == "foreign_db":
            async with get_db_session() as other:
                await begin_snapshot(other)
                with pytest.raises(RuntimeError, match="original read-only snapshot"):
                    await source(other, checks, main, refs[2])
            return
        if mutation in {"dirty", "flush"}:
            held.title = "Uncommitted caller change"
        if mutation in {"flush", "bulk"}:
            async def write():
                if mutation == "flush":
                    await db.flush()
                else:
                    await db.execute(update(Session).where(Session.id == main.id).values(title="Bulk caller change"))
            if get_engine().dialect.name == "postgresql":
                with pytest.raises(DBAPIError):
                    await write()
            else:
                await write()
        if mutation == "transaction":
            await db.rollback()
            await begin_snapshot(db)
        with pytest.raises(RuntimeError, match="original read-only snapshot"):
            await source(db, checks, main, refs[2])
        await db.rollback()


async def test_successful_source_sql_is_not_admitted_after_caller_becomes_dirty(monkeypatch):
    main, refs, _ = await source_world(3)
    monkeypatch.setattr(SnapshotChecks, "MAX_ENTRIES", 2)
    from assistant import evidence
    original = evidence._source_original
    async with source_snapshot(reuse_task_facts=True) as (db, checks):
        for ref in refs[:2]:
            await source(db, checks, main, ref)
        held = await db.get(Session, main.id)
        async def changed_after_read(*args, **kwargs):
            value = await original(*args, **kwargs)
            held.title = "Caller changed during awaited source read"
            return value
        monkeypatch.setattr(evidence, "_source_original", changed_after_read)
        before = tuple(checks._values)
        with pytest.raises(RuntimeError, match="original read-only snapshot"):
            await source(db, checks, main, refs[2])
        assert tuple(checks._values) == before
        await db.rollback()


@pytest.mark.parametrize("revocation", ["membership", "source"])
async def test_evicted_fact_reloads_in_same_rr_but_next_snapshot_observes_revoke(monkeypatch, revocation):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("An independent committed writer needs PostgreSQL RR")
    main, refs, _ = await source_world(3)
    monkeypatch.setattr(SnapshotChecks, "MAX_ENTRIES", 2)
    async with source_snapshot(reuse_task_facts=True) as (db, checks):
        await _authority(db, user_id=main.user_id, workspace_id=main.workspace_id,
                         main_id=main.id, snapshot_checks=checks)
        for ref in refs:
            await source(db, checks, main, ref)
        async def revoke():
            async with get_db_session() as writer:
                if revocation == "membership":
                    (await writer.get(WorkspaceMember, (main.workspace_id, main.user_id))).status = "removed"
                else:
                    (await writer.get(Part, refs[0]["part_id"])).data = {"text": "Replaced original"}
        await Context().run(asyncio.create_task, revoke())
        with queries() as actual_reads:
            await _authority(db, user_id=main.user_id, workspace_id=main.workspace_id,
                             main_id=main.id, snapshot_checks=checks)
            assert part_hash(await source(db, checks, main, refs[0])) == refs[0]["content_hash"]
        assert len(actual_reads) == 2  # Both were evicted; these are real RR reads.
    async with source_snapshot(reuse_task_facts=True) as (db, checks):
        with pytest.raises(AssistantError):
            await _authority(db, user_id=main.user_id, workspace_id=main.workspace_id,
                             main_id=main.id, snapshot_checks=checks)
            await source(db, checks, main, refs[0])
