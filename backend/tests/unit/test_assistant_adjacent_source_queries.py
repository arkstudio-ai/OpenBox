"""Adjacent source reads stay fresh without changing their transaction or bytes."""
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from assistant.command_sources import validate_task_command_sources
from assistant.policy import AssistantError
from assistant.results import _result_original
from assistant.transactions import begin_snapshot
from db.base import get_db_session, get_engine
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask, TaskResult, TaskSubmission
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from tests.unit.assistant_source_fixtures import consume_context
from tests.unit.test_assistant_command_sources import arguments, no_task_dispatch, settle_execution  # noqa: F401
from tests.unit.test_assistant_context_sources import finish, next_turn
from tests.unit.test_assistant_decisions import start
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_reads import call_tool
from tests.unit.test_assistant_source_join_queries import sql_reads
from tests.unit.test_assistant_source_queries import original_result  # noqa: F401


@pytest.fixture
async def derived_task():
    ctx, lease, answer = await start("Create one local text-only task, without external actions.")
    try:
        await consume_context(ctx)
        output, _, _ = await call_tool(ctx, "tasks.submit", arguments(ctx, answer))
        assert not output.metadata.get("error"), output.output
        receipt = json.loads(output.output)
        await finish(ctx, lease, answer, "Accepted the authorized text-only task.")
        result = await settle_execution(ctx, receipt)
        return ctx, receipt, result.id
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("count", [0, 1, 101, 201])
async def test_result_batches_preserve_duplicate_order_and_hashes_with_one_less_query(original_result, count):
    scope, accepted, result_id = original_result
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        refs = [deepcopy(result.output_refs[i % len(result.output_refs)]) for i in range(count)]
        retained = SimpleNamespace(task_id=result.task_id, output_refs=refs)
        with sql_reads() as queries:
            task, parts = await _result_original(db, retained, **scope)
        assert task.id == accepted["task_id"]
        assert len(queries) == max(1, (count + 99) // 100)
        assert [(ref, part.id) for ref, part in parts] == [(ref, ref["part_id"]) for ref in refs]
        if refs:
            refs[-1]["content_hash"] = "0" * 64
            with pytest.raises(AssistantError) as error:
                await _result_original(db, retained, **scope)
            assert error.value.code == "ASSISTANT_RESULT_SOURCE_CHANGED"


@pytest.mark.parametrize("refs", [None, {"bad": "container"}, [{"part_id": "missing-fields"}]])
@pytest.mark.parametrize("fault,code", [("member", "ASSISTANT_WORKSPACE_FORBIDDEN"), ("main", "ASSISTANT_UNAVAILABLE")])
async def test_malformed_retained_refs_still_check_scope_before_their_shape(original_result, refs, fault, code):
    scope, _, result_id = original_result
    async with get_db_session() as db:
        if fault == "member":
            (await db.get(WorkspaceMember, (scope["workspace_id"], scope["user_id"]))).status = "removed"
        else:
            (await db.get(Session, scope["main_id"])).visibility = "workspace"
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        retained = SimpleNamespace(task_id=result.task_id, output_refs=refs)
        with pytest.raises(AssistantError) as error:
            await _result_original(db, retained, **scope)
        assert error.value.code == code


async def test_command_originals_and_current_authority_share_only_this_call(derived_task):
    _, receipt, _ = derived_task
    async with get_db_session() as db:
        task = await db.get(AssistantTask, receipt["task_id"])
        for _ in range(2):
            with sql_reads() as queries:
                await validate_task_command_sources(db, task)
            # One current command/Inbox/authority row and one original human
            # Part. A second call must make both real SQL reads again.
            assert len(queries) == 2
    async with get_db_session() as db:
        checks = await begin_snapshot(db)
        task = await db.get(AssistantTask, receipt["task_id"])
        with sql_reads() as first:
            await validate_task_command_sources(db, task, snapshot_checks=checks)
        with sql_reads() as second:
            await validate_task_command_sources(db, task, snapshot_checks=checks)
        assert len(first) == 3 and len(second) == 1  # Original read-only snapshot contract.


@pytest.mark.parametrize("omitted", ["canceled", "not_applied", "later", "legacy"])
async def test_empty_derivation_keeps_its_original_return_before_authority(derived_task, omitted):
    _, receipt, _ = derived_task
    before = None
    async with get_db_session() as db:
        task = await db.get(AssistantTask, receipt["task_id"])
        submission = await db.scalar(select(TaskSubmission).where(TaskSubmission.task_id == task.id))
        if omitted in {"canceled", "not_applied"}:
            submission.disposition = omitted
        elif omitted == "legacy":
            command = await db.get(AssistantCommand, submission.command_id)
            command.source_ref = {key: value for key, value in command.source_ref.items() if key != "derivation"}
        else:
            inbox = await db.get(AgentInboxItem, receipt["inbox_id"])
            before = (await db.get(Message, inbox.message_id)).created_at - timedelta(seconds=1)
        (await db.get(WorkspaceMember, (task.workspace_id, task.user_id))).status = "removed"
        (await db.get(Session, task.assistant_session_id)).visibility = "workspace"
    async with get_db_session() as db:
        task = await db.get(AssistantTask, receipt["task_id"])
        with sql_reads() as queries:
            await validate_task_command_sources(db, task, before=before)
        assert len(queries) == 1


@pytest.mark.parametrize("fault,code", [
    ("member", "ASSISTANT_WORKSPACE_FORBIDDEN"),
    ("main", "ASSISTANT_UNAVAILABLE"),
    ("binding", "ASSISTANT_COMMAND_SOURCE_UNVERIFIED"),
])
async def test_command_authority_still_precedes_invalid_derivation_binding(derived_task, fault, code):
    _, receipt, _ = derived_task
    async with get_db_session() as db:
        task = await db.get(AssistantTask, receipt["task_id"])
        (await db.get(AgentInboxItem, receipt["inbox_id"])).origin_ref = {}
        if fault in {"member", "main"}:
            (await db.get(Session, task.assistant_session_id)).memory_policy = "standard"
        if fault == "member":
            (await db.get(WorkspaceMember, (task.workspace_id, task.user_id))).status = "removed"
    async with get_db_session() as db:
        task = await db.get(AssistantTask, receipt["task_id"])
        with sql_reads() as queries:
            with pytest.raises(AssistantError) as error:
                await validate_task_command_sources(db, task)
        assert error.value.code == code and len(queries) == 1


async def test_command_overflow_still_precedes_current_authority_and_binding_refusal(derived_task):
    _, receipt, _ = derived_task
    async with get_db_session() as db:
        task = await db.get(AssistantTask, receipt["task_id"])
        submission = await db.scalar(select(TaskSubmission).where(TaskSubmission.task_id == task.id))
        command = await db.get(AssistantCommand, submission.command_id)
        inbox = await db.get(AgentInboxItem, submission.inbox_id)
        for _ in range(200):
            identity = uuid4().hex
            db.add(AssistantCommand(**{**{c.name: deepcopy(getattr(command, c.name)) for c in command.__table__.columns},
                "id": identity, "idempotency_key": identity}))
            db.add(AgentInboxItem(**{**{c.name: deepcopy(getattr(inbox, c.name)) for c in inbox.__table__.columns},
                "id": identity, "client_id": identity, "message_id": None, "origin_ref": {}}))
            db.add(TaskSubmission(**{**{c.name: deepcopy(getattr(submission, c.name)) for c in submission.__table__.columns},
                "id": identity, "command_id": identity, "inbox_id": identity}))
        (await db.get(WorkspaceMember, (task.workspace_id, task.user_id))).status = "removed"
    async with get_db_session() as db:
        task = await db.get(AssistantTask, receipt["task_id"])
        with sql_reads() as queries:
            with pytest.raises(AssistantError, match="verification budget") as error:
                await validate_task_command_sources(db, task)
        assert error.value.code == "ASSISTANT_COMMAND_SOURCE_UNVERIFIED" and len(queries) == 1


async def test_command_read_preserves_unflushed_main_fields_and_local_policy_denial(derived_task):
    ctx, receipt, _ = derived_task
    async with get_db_session() as db:
        task = await db.get(AssistantTask, receipt["task_id"])
        main = await db.get(Session, ctx.session_id)
        main.title, main.model = "unflushed title", "unflushed/model"
        with db.no_autoflush:
            await validate_task_command_sources(db, task)
            assert main.title == "unflushed title" and main.model == "unflushed/model" and main in db.dirty
            main.memory_policy = "standard"
            with pytest.raises(AssistantError) as error:
                await validate_task_command_sources(db, task)
            assert error.value.code == "ASSISTANT_UNAVAILABLE"
        await db.rollback()


@pytest.mark.parametrize("fault,code", [
    ("member", "ASSISTANT_WORKSPACE_FORBIDDEN"), ("main", "ASSISTANT_UNAVAILABLE"),
    ("local_main", "ASSISTANT_UNAVAILABLE"),
    ("task", "ASSISTANT_TASK_UNAVAILABLE"), ("execution", "ASSISTANT_EXECUTION_UNAVAILABLE"),
    ("project", "ASSISTANT_PROJECT_UNAVAILABLE"),
])
async def test_postgres_revoked_scope_does_not_refresh_a_held_result_body(original_result, fault, code):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Requires independent PostgreSQL connections with held ORM identities")
    scope, _, result_id = original_result
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        task, parts = await _result_original(db, result, **scope)
        held = parts[-1][1]
        original = deepcopy(held.data)
        reader = await db.scalar(select(func.pg_backend_pid()))
        async with get_db_session() as writer:
            assert await writer.scalar(select(func.pg_backend_pid())) != reader
            source = await writer.get(Part, held.id)
            source.data = {**source.data, "text": "Changed bytes after the scope was revoked"}
            if fault == "member":
                (await writer.get(WorkspaceMember, (scope["workspace_id"], scope["user_id"]))).status = "removed"
            elif fault == "main":
                (await writer.get(Session, scope["main_id"])).memory_policy = "standard"
            elif fault == "local_main":
                pass  # The held local policy below denies an otherwise current SQL grant.
            elif fault == "task":
                (await writer.get(AssistantTask, task.id)).assistant_session_id = task.execution_session_id
            elif fault == "execution":
                (await writer.get(Session, task.execution_session_id)).visibility = "workspace"
            else:
                (await writer.get(Project, task.project_id)).is_deleted = True
        if fault == "local_main":
            (await db.get(Session, scope["main_id"])).memory_policy = "standard"
        with pytest.raises(AssistantError) as error:
            await _result_original(db, result, **scope)
        assert error.value.code == code and held.data == original
        await db.rollback()


@pytest.mark.parametrize("fault,code", [
    ("member", "ASSISTANT_WORKSPACE_FORBIDDEN"), ("main", "ASSISTANT_UNAVAILABLE"),
])
async def test_postgres_command_authority_rechecks_after_independent_revoke_with_old_main(derived_task, fault, code):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Requires independent PostgreSQL connections with held ORM identities")
    ctx, receipt, _ = derived_task
    async with get_db_session() as db:
        task = await db.get(AssistantTask, receipt["task_id"])
        held = await db.get(Session, ctx.session_id)
        await validate_task_command_sources(db, task)
        reader = await db.scalar(select(func.pg_backend_pid()))
        async with get_db_session() as writer:
            assert await writer.scalar(select(func.pg_backend_pid())) != reader
            if fault == "member":
                (await writer.get(WorkspaceMember, (ctx.workspace_id, ctx.user_id))).status = "removed"
            else:
                (await writer.get(Session, ctx.session_id)).memory_policy = "standard"
        assert held.memory_policy == "assistant_isolated"
        with pytest.raises(AssistantError) as error:
            await validate_task_command_sources(db, task)
        assert error.value.code == code


@pytest.mark.parametrize("fault", ["result_body", "command_binding"])
async def test_postgres_actual_checkpoint_rejects_commit_after_projection_before_fresh_validation(
    monkeypatch, derived_task, fault,
):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Requires an independently committed source change at the actual checkpoint")
    from assistant import context_sources
    ctx, receipt, result_id = derived_task
    ctx, lease, _ = await next_turn(ctx, "Read the original task result without starting any work.")
    try:
        output, _, _ = await call_tool(ctx, "results.read", {"result_id": result_id})
        assert not output.metadata.get("error"), output.output
        async with get_db_session() as db:
            prior = await db.scalar(select(func.count()).select_from(AgentEvent).where(
                AgentEvent.session_id == ctx.session_id, AgentEvent.kind == "model.requested"))
        real_check = context_sources.checked_context_locked
        changed = []

        async def check(db, main, context, **kwargs):
            if kwargs.get("fresh") and not changed:
                reader = await db.scalar(select(func.pg_backend_pid()))
                async with get_db_session() as writer:
                    assert await writer.scalar(select(func.pg_backend_pid())) != reader
                    if fault == "result_body":
                        result = await writer.get(TaskResult, result_id)
                        part = await writer.get(Part, result.output_refs[-1]["part_id"])
                        part.data = {**part.data, "text": "Changed before fresh checkpoint began"}
                    else:
                        (await writer.get(AgentInboxItem, receipt["inbox_id"])).origin_ref = {}
                changed.append(True)
            return await real_check(db, main, context, **kwargs)

        monkeypatch.setattr(context_sources, "checked_context_locked", check)
        with pytest.raises(AssistantError):
            await consume_context(ctx)
        assert changed == [True]
        async with get_db_session() as db:
            assert await db.scalar(select(func.count()).select_from(AgentEvent).where(
                AgentEvent.session_id == ctx.session_id, AgentEvent.kind == "model.requested")) == prior
    finally:
        await lease.release(session_status="idle")
