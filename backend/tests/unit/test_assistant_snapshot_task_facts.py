"""ID facts belong to selected read-only snapshots, never provider authority."""
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.exc import DBAPIError

from assistant.commands import _authority, command_digest
from assistant.policy import AssistantError
from assistant.service import ensure_main_session
from assistant.task_context import _scope, _view, validate_task_snapshots
from assistant.transactions import SnapshotChecks, begin_snapshot, source_snapshot
from db.base import get_db_session, get_engine
from db.models.assistant import AssistantTask, TaskResult
from db.models.project import Project
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from session.session import create_session, get_messages, get_session
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_reads import read_turn


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from tests.offline_wuying import install_wuying_offline_guard
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    install_wuying_offline_guard(monkeypatch)


async def facts_world(count=1):
    """Seed legitimate scalar facts; full provenance uses read_turn below."""
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    now = datetime.now(timezone.utc)
    ids = []
    for index in range(count):
        execution = await create_session(user_id=owner, workspace_id=workspace, project_id=main.project_id,
            visibility="private", memory_policy="assistant_isolated")
        task_id, result_id = uuid4().hex, uuid4().hex
        async with get_db_session() as db:
            db.add(AssistantTask(id=task_id, assistant_session_id=main.id, user_id=owner,
                workspace_id=workspace, project_id=main.project_id, execution_session_id=execution.id,
                title=f"Observed task {index}", control_revision=2, intent_revision=2,
                latest_result_id=result_id, observed_state="completed", created_at=now, updated_at=now))
            await db.flush()
            db.add(TaskResult(id=result_id, task_id=task_id, source_event_key=uuid4().hex,
                run_id=uuid4().hex, generation=1, outcome="succeeded", observed_intent_revision=1,
                available_at=now, created_at=now))
        ids.append(task_id)
    async with get_db_session() as db:
        refs = []
        for task_id in ids:
            task = await db.get(AssistantTask, task_id)
            snapshot = await _view(db, main, task_id)
            refs.append({"task_id": task_id, "scope_digest": _scope(task), "snapshot": snapshot,
                         "snapshot_digest": command_digest(snapshot)})
    return main, refs


def historical(refs):
    values = deepcopy(refs)
    for ref in values:
        ref["snapshot"]["task"].update(control_revision=1, intent_revision=1, observed_state="queued")
        ref["snapshot_digest"] = command_digest(ref["snapshot"])
    return values


@contextmanager
def queries():
    values = []
    def count(_connection, _cursor, statement, parameters, *_rest):
        values.append((statement, parameters))
    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        yield values
    finally:
        event.remove(engine, "before_cursor_execute", count)


@pytest.mark.parametrize("capacity", [512, 1, 0])
async def test_twelve_tasks_batch_missing_ids_and_keep_historical_ref_validation(monkeypatch, record_property, capacity):
    main, refs = await facts_world(13)
    async with source_snapshot() as (db, checks):
        with queries() as baseline:
            assert await validate_task_snapshots(db, main, refs[:12], snapshot_checks=checks) is None
        assert len(baseline) == 24
    monkeypatch.setattr(SnapshotChecks, "MAX_ENTRIES", capacity)
    async with source_snapshot(reuse_task_facts=True) as (db, checks):
        with queries() as initial:
            assert await validate_task_snapshots(db, main, refs[:12], snapshot_checks=checks) is None
        assert len(initial) == 2
        with queries() as second:
            await validate_task_snapshots(db, main, historical(refs[:12]), snapshot_checks=checks)
        assert len(second) == (0 if capacity == 512 else 2)
        # The new ID is interleaved with repeated old IDs. Only misses are
        # queried, without changing reference order or duplicating SQL.
        mixed = [refs[5], refs[12], refs[0], refs[5]]
        with queries() as missing:
            await validate_task_snapshots(db, main, mixed, snapshot_checks=checks)
        assert len(missing) == 2
        if capacity == 512:
            assert refs[12]["task_id"] in missing[0][1] and refs[5]["task_id"] not in missing[0][1]
            assert tuple(missing[1][1]) == (refs[12]["snapshot"]["latest_result"]["result_id"],)
            fact = next(value for key, value in checks._values.items() if key[:2] == ("facts", "task_scope"))
            with pytest.raises(FrozenInstanceError):
                fact.title = "mutated held row"
        assert len(checks._values) <= capacity
    record_property("facts_sql", json.dumps({"baseline": len(baseline), "first": len(initial),
        "other_historical_versions": len(second), "mixed_missing": len(missing), "capacity": capacity}))


@pytest.mark.parametrize("damage", ["digest", "revision", "result_task", "scope", "budget"])
async def test_warm_id_facts_do_not_certify_other_reference_bytes_or_scope(damage):
    main, refs = await facts_world(2)
    async with source_snapshot(reuse_task_facts=True) as (db, checks):
        await validate_task_snapshots(db, main, refs, snapshot_checks=checks)
        bad = deepcopy(refs[:1])
        if damage == "digest":
            bad[0]["snapshot"]["task"]["title"] = "tampered"
        elif damage == "revision":
            bad[0]["snapshot"]["task"]["control_revision"] = 3
            bad[0]["snapshot_digest"] = command_digest(bad[0]["snapshot"])
        elif damage == "result_task":
            bad[0]["snapshot"]["latest_result"] = deepcopy(refs[1]["snapshot"]["latest_result"])
            bad[0]["snapshot_digest"] = command_digest(bad[0]["snapshot"])
        elif damage == "scope":
            main = SimpleNamespace(id=main.id, user_id="another-actor", workspace_id=main.workspace_id)
        else:
            bad *= 201
        with pytest.raises(AssistantError):
            await validate_task_snapshots(db, main, bad, snapshot_checks=checks)


@pytest.mark.parametrize("unavailable", ["task", "execution", "project"])
async def test_earlier_unavailable_ref_keeps_priority_over_later_bad_digest(unavailable):
    main, refs = await facts_world(2)
    if unavailable == "task":
        refs[0]["task_id"] = "missing-task"
    else:
        async with get_db_session() as db:
            if unavailable == "execution":
                (await db.get(Session, refs[0]["snapshot"]["task"]["execution_session_id"])).visibility = "workspace"
            else:
                (await db.get(Project, main.project_id)).is_deleted = True
    refs[1]["snapshot_digest"] = "0" * 64
    errors = []
    for enabled in (False, True):
        async with source_snapshot(reuse_task_facts=enabled) as (db, checks):
            with pytest.raises(AssistantError) as denied:
                await validate_task_snapshots(db, main, refs, snapshot_checks=checks)
            errors.append((denied.value.status, denied.value.code))
    assert errors[0] == errors[1]
    assert errors[1][1] == {"task": "ASSISTANT_TASK_UNAVAILABLE", "execution": "ASSISTANT_EXECUTION_UNAVAILABLE",
                            "project": "ASSISTANT_PROJECT_UNAVAILABLE"}[unavailable]


@pytest.mark.parametrize("first_error", ["digest", "unavailable"])
async def test_later_unbindable_sql_id_cannot_mask_the_first_refusal(first_error):
    main, refs = await facts_world(2)
    if first_error == "digest":
        refs[0]["snapshot_digest"] = "0" * 64
    else:
        refs[0]["task_id"] = "missing-task"
    refs[1]["snapshot"]["latest_result"]["result_id"] = "unbound\x00result"
    refs[1]["snapshot_digest"] = command_digest(refs[1]["snapshot"])
    errors = []
    for enabled in (False, True):
        async with source_snapshot(reuse_task_facts=enabled) as (db, checks):
            with pytest.raises(AssistantError) as denied:
                await validate_task_snapshots(db, main, refs, snapshot_checks=checks)
            errors.append(denied.value.code)
    assert errors[0] == errors[1]


@pytest.mark.parametrize("mutation", ["dirty", "flush", "bulk", "transaction", "other_db", "nested"])
async def test_opted_in_facts_reject_local_write_and_transaction_lifetime_changes(mutation):
    main, refs = await facts_world()
    async with source_snapshot(reuse_task_facts=True) as (db, checks):
        await validate_task_snapshots(db, main, refs, snapshot_checks=checks)
        task = await db.get(AssistantTask, refs[0]["task_id"])
        if mutation == "other_db":
            async with get_db_session() as other:
                await begin_snapshot(other)
                with pytest.raises(RuntimeError, match="original read-only snapshot"):
                    await validate_task_snapshots(other, main, refs, snapshot_checks=checks)
            return
        if mutation == "nested":
            async with db.begin_nested():
                with pytest.raises(RuntimeError, match="original read-only snapshot"):
                    await validate_task_snapshots(db, main, refs, snapshot_checks=checks)
            return
        if mutation in {"dirty", "flush"}:
            task.title = "local edit"
        if mutation in {"flush", "bulk"}:
            async def write():
                if mutation == "flush":
                    await db.flush()
                else:
                    await db.execute(update(AssistantTask).where(AssistantTask.id == task.id).values(title="bulk edit"))
            if get_engine().dialect.name == "postgresql":
                with pytest.raises(DBAPIError):
                    await write()  # The production RR transaction is READ ONLY.
            else:
                await write()
        if mutation == "transaction":
            await db.rollback()
            await begin_snapshot(db)
        with pytest.raises(RuntimeError, match="original read-only snapshot"):
            await validate_task_snapshots(db, main, refs, snapshot_checks=checks)
        await db.rollback()


@pytest.mark.parametrize("revocation", ["membership", "execution", "result"])
async def test_independent_writer_is_seen_by_next_snapshot_without_changing_inflight_rr(revocation):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Independent committed writer needs PostgreSQL RR")
    main, refs = await facts_world()
    async with source_snapshot(reuse_task_facts=True) as (db, checks):
        await _authority(db, user_id=main.user_id, workspace_id=main.workspace_id, main_id=main.id,
                         snapshot_checks=checks)
        await validate_task_snapshots(db, main, refs, snapshot_checks=checks)
        async with get_db_session() as writer:
            if revocation == "membership":
                (await writer.get(WorkspaceMember, (main.workspace_id, main.user_id))).status = "removed"
            elif revocation == "execution":
                (await writer.get(Session, refs[0]["snapshot"]["task"]["execution_session_id"])).is_deleted = True
            else:
                (await writer.get(TaskResult, refs[0]["snapshot"]["latest_result"]["result_id"])).outcome = "failed"
        # Reuse and a real, uncached SELECT agree on the original RR view.
        await _authority(db, user_id=main.user_id, workspace_id=main.workspace_id, main_id=main.id)
        await validate_task_snapshots(db, main, historical(refs), snapshot_checks=checks)
        await validate_task_snapshots(db, main, refs)
    async with source_snapshot(reuse_task_facts=True) as (db, checks):
        with pytest.raises(AssistantError):
            await _authority(db, user_id=main.user_id, workspace_id=main.workspace_id, main_id=main.id,
                             snapshot_checks=checks)
            await validate_task_snapshots(db, main, refs, snapshot_checks=checks)


async def test_fresh_task_checks_ignore_opt_in_and_keep_sql_and_output(record_property):
    main, refs = await facts_world()
    reads = []
    for enabled in (False, True):
        async with source_snapshot(reuse_task_facts=enabled) as (db, checks):
            await validate_task_snapshots(db, main, historical(refs), snapshot_checks=checks)
            with queries() as statements:
                assert await validate_task_snapshots(db, main, refs, fresh=True, snapshot_checks=checks) is None
            reads.append(statements)
    assert reads[0] == reads[1]
    record_property("fresh_sql_unchanged", len(reads[0]))


async def test_provider_projection_does_not_opt_in_while_history_and_monitor_do(monkeypatch, record_property):
    from agent.loop import _to_llm_messages
    from assistant import projection, public_history, scheduling
    from assistant.evidence import projection_digest
    from tests.unit.assistant_source_fixtures import consume_context
    from tests.unit.test_assistant_context_sources import finish

    ctx, lease, answer, accepted, _ = await read_turn()
    try:
        await consume_context(ctx)
        await finish(ctx, lease, answer, "Task result was observed.")
        session = await get_session(ctx.session_id, user_id=ctx.user_id)
        messages = await get_messages(ctx.session_id, user_id=ctx.user_id)
        observed = []
        @asynccontextmanager
        async def watched(**kwargs):
            observed.append(kwargs.get("reuse_task_facts", False))
            async with source_snapshot(**kwargs) as pair:
                yield pair
        monkeypatch.setattr(projection, "source_snapshot", watched)
        with queries() as first_sql:
            first = await projection.project_main_messages(messages, ctx=ctx)
        assert observed and not any(observed)
        # Removing the new fact loader entirely has no effect on provider
        # projection or its SQL. This is not an external provider call.
        async def forbidden(*args, **kwargs):
            raise AssertionError("Provider projection must not use task ID facts")
        with monkeypatch.context() as patch:
            patch.setattr(SnapshotChecks, "read_many", forbidden)
            with queries() as second_sql:
                second = await projection.project_main_messages(messages, ctx=ctx)
        assert projection_digest(_to_llm_messages(first, assistant_projection_verified=True)) == projection_digest(
            _to_llm_messages(second, assistant_projection_verified=True))
        assert first_sql == second_sql
        record_property("provider_sql_unchanged", len(first_sql))
        monkeypatch.setattr(public_history, "source_snapshot", watched)
        monkeypatch.setattr("assistant.transactions.source_snapshot", watched)
        observed.clear()
        assert any(row["id"] == answer.id and row["source_status"] == "available" for row in
                   await public_history.public_messages(session, messages, actor_user_id=ctx.user_id))
        assert await scheduling.observe_task_hold(accepted["execution_session_id"], ctx.user_id) is None
        assert observed == [True, True]
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("surface", ["full", "unread"])
async def test_snapshot_views_reuse_task_facts_with_identical_output_and_no_all_read_work(monkeypatch, record_property, surface):
    # Counts snapshot sharing alone; reused verdicts are tested separately.
    monkeypatch.setenv("ASSISTANT_EVIDENCE_CACHE", "off")
    from assistant import events, snapshot
    from core.config import get_config
    from tests.unit.assistant_source_fixtures import consume_context
    from tests.unit.test_assistant_context_sources import finish, next_turn

    monkeypatch.setattr(get_config(), "jwt_secret", "snapshot-task-facts-signing-test-only")
    ctx, lease, answer, accepted, _ = await read_turn()
    try:
        # Three actual checkpoint/response/commit turns observe distinct
        # progress snapshots of the same original Task/Result identities.
        for index, state in enumerate(("completed", "waiting_input", "queued")):
            async with get_db_session() as db:
                (await db.get(AssistantTask, accepted["task_id"])).observed_state = state
            await consume_context(ctx)
            await finish(ctx, lease, answer, f"The observed task progress is {state}.")
            if index < 2:
                ctx, lease, answer = await next_turn(ctx)

        # Compare complete signed display payloads with the real signer. Only
        # its varying expires timestamp is fixed; no authorization is stubbed.
        sign = snapshot._sign_display
        monkeypatch.setattr(snapshot, "_sign_display", lambda payload: sign({**payload, "expires": 4_000_000_000}))
        cursor_time = events.time.time()
        monkeypatch.setattr(events, "time", SimpleNamespace(time=lambda: cursor_time))
        operation = snapshot.get_snapshot if surface == "full" else snapshot.get_unread
        scope = {"user_id": ctx.user_id, "workspace_id": ctx.workspace_id}
        with queries() as optimized_sql:
            optimized = await operation(**scope)
        assert optimized["unread_count"] == 3

        @asynccontextmanager
        async def previous_snapshot(**kwargs):
            assert kwargs == {"reuse_task_facts": True}
            async with source_snapshot() as pair:
                yield pair
        with monkeypatch.context() as patch:
            patch.setattr(snapshot, "source_snapshot", previous_snapshot)
            with queries() as baseline_sql:
                baseline = await operation(**scope)
        # The full view now publishes the source transaction's check time;
        # separate snapshots may differ only in this observation timestamp.
        if surface == "full":
            assert optimized["source_checked_at"] and baseline["source_checked_at"]
        assert {key: value for key, value in optimized.items() if key != "source_checked_at"} == {
            key: value for key, value in baseline.items() if key != "source_checked_at"}

        def task_reads(statements):
            return sum("from assistant_tasks " in (sql := " ".join(statement.lower().split()))
                       or "from assistant_task_results " in sql for statement, _ in statements)
        assert task_reads(optimized_sql) < task_reads(baseline_sql)
        assert len(optimized_sql) < len(baseline_sql)
        assert not any(statement.lstrip().lower().startswith(("insert", "update", "delete"))
                       for statement, _ in optimized_sql + baseline_sql)
        record_property(surface + "_snapshot_sql", json.dumps({"baseline": len(baseline_sql),
            "optimized": len(optimized_sql), "task_result_baseline": task_reads(baseline_sql),
            "task_result_optimized": task_reads(optimized_sql)}))

        # The unmodified write boundary revalidates the actual signed answer.
        # Once all answers are read, neither mode should enter a source DAG.
        full = optimized if surface == "full" else await snapshot.get_snapshot(**scope)
        latest = full["answers"][0]
        await snapshot.advance_read_cursor(**scope, main_id=ctx.session_id,
            last_seen_sequence=latest["sequence"], display_token=latest["display_token"])
        async def forbidden(*_args, **_kwargs):
            raise AssertionError("An all-read badge must not inspect task facts or answer sources")
        with monkeypatch.context() as patch:
            patch.setattr(snapshot, "_answer_digest", forbidden)
            patch.setattr(SnapshotChecks, "read_many", forbidden)
            with queries() as all_read_sql:
                all_read = await snapshot.get_unread(**scope)
            patch.setattr(snapshot, "source_snapshot", previous_snapshot)
            with queries() as old_all_read_sql:
                old_all_read = await snapshot.get_unread(**scope)
        assert all_read == old_all_read == {"unread_count": 0, "unread_count_is_lower_bound": False}
        assert all_read_sql == old_all_read_sql
        assert task_reads(all_read_sql) == 0
        record_property(surface + "_all_read_fact_queries", 0)
    finally:
        await lease.release(session_status="idle")
