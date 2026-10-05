"""Joined source lookups retain current scope, refusal order and graph bounds."""
from contextlib import contextmanager

import pytest
from sqlalchemy import event, func, select

from assistant.commands import accept_task_command
from assistant.evidence import _message_evidence, _source_original, validate_message_sources
from assistant.execution_sources import _lineage
from assistant.policy import AssistantError
from assistant.results import part_hash
from db.base import get_db_session, get_engine
from db.models.agent_event import AgentEvent
from db.models.assistant import AssistantTask, TaskResult
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from session.session import create_session
from tests.unit.test_assistant_commands import setup_task
from tests.unit.test_assistant_command_sources import no_task_dispatch  # noqa: F401
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_reporting import answer, prepare_report, seen_read
from tests.unit.test_assistant_results import result_ready


@contextmanager
def sql_reads():
    statements = []

    def count(_connection, _cursor, statement, _parameters, _context, _many):
        assert statement.lstrip().upper().startswith("SELECT")
        statements.append(statement)

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", count)


async def task_scope():
    owner, _, workspace, main, args = await setup_task()
    accepted = await accept_task_command(**args)
    return dict(user_id=owner, workspace_id=workspace, main_id=main.id), accepted


async def source_scope():
    owner, workspace, main, accepted, lease, message = await result_ready()
    await lease.release(session_status="idle")
    async with get_db_session() as db:
        part = await db.scalar(select(Part).where(Part.message_id == message.id, Part.type == "text"))
        reference = dict(session_id=part.session_id, message_id=part.message_id,
                         part_id=part.id, content_hash=part_hash(part))
    return dict(user_id=owner, workspace_id=workspace, main_id=main.id), accepted, reference


async def test_current_source_reads_use_fewer_sql_roundtrips_without_reusing_authority():
    scope, accepted, reference = await source_scope()
    # Cold Sessions prevent identity-map hits from masquerading as fewer reads.
    async with get_db_session() as db:
        with sql_reads() as queries:
            tasks = await _lineage(db, accepted["execution_session_id"], **scope)
        assert [task.id for task in tasks] == [accepted["task_id"]]
        assert len(queries) == 3
    async with get_db_session() as db:
        with sql_reads() as queries:
            part, message = await _source_original(db, reference, **scope)
        assert (part.id, message.id) == (reference["part_id"], reference["message_id"])
        assert len(queries) == 2
    async with get_db_session() as db:
        with sql_reads() as queries:
            report, manifest = await _message_evidence(db, "not-an-answer", user_id=scope["user_id"],
                                                        main_id=scope["main_id"])
        assert report is manifest is None
        assert len(queries) == 1


@pytest.mark.parametrize("faults,code", [
    (("execution", "project", "member", "main"), "ASSISTANT_EXECUTION_SOURCE_UNAVAILABLE"),
    (("project", "member", "main"), "ASSISTANT_PROJECT_UNAVAILABLE"),
    (("wrong-main", "member", "main"), "ASSISTANT_EXECUTION_SOURCE_UNAVAILABLE"),
    (("member", "main"), "ASSISTANT_WORKSPACE_FORBIDDEN"),
    (("main",), "ASSISTANT_UNAVAILABLE"),
])
async def test_lineage_refusals_keep_scope_before_authority_priority(faults, code):
    scope, accepted = await task_scope()
    async with get_db_session() as db:
        execution = await db.get(Session, accepted["execution_session_id"])
        if "execution" in faults:
            execution.visibility = "workspace"
        if "project" in faults:
            (await db.get(Project, execution.project_id)).is_deleted = True
        if "member" in faults:
            (await db.get(WorkspaceMember, (scope["workspace_id"], scope["user_id"]))).status = "removed"
        if "main" in faults:
            (await db.get(Session, scope["main_id"])).visibility = "workspace"
    if "wrong-main" in faults:
        scope["main_id"] = accepted["execution_session_id"]
    async with get_db_session() as db:
        with pytest.raises(AssistantError) as error:
            await _lineage(db, accepted["execution_session_id"], **scope)
        assert error.value.code == code


@pytest.mark.parametrize("faults,code", [
    (("link", "execution", "project", "body"), "ASSISTANT_SOURCE_UNAVAILABLE"),
    (("execution", "project", "body"), "ASSISTANT_EXECUTION_UNAVAILABLE"),
    (("project", "body"), "ASSISTANT_PROJECT_UNAVAILABLE"),
    (("body",), "ASSISTANT_SOURCE_CHANGED"),
])
async def test_original_source_refusals_keep_link_execution_project_then_body_priority(faults, code):
    scope, accepted, reference = await source_scope()
    async with get_db_session() as db:
        task = await db.get(AssistantTask, accepted["task_id"])
        if "link" in faults:
            task.assistant_session_id = task.execution_session_id
        if "execution" in faults:
            (await db.get(Session, task.execution_session_id)).visibility = "workspace"
        if "project" in faults:
            (await db.get(Project, task.project_id)).is_deleted = True
        if "body" in faults:
            part = await db.get(Part, reference["part_id"])
            part.data = {**part.data, "text": "changed source"}
    async with get_db_session() as db:
        with pytest.raises(AssistantError) as error:
            await _source_original(db, reference, **scope)
        assert error.value.code == code


async def test_message_evidence_join_keeps_each_missing_side_and_report_refusal_priority():
    ctx, lease, message, part, result_id, _ = await prepare_report()
    try:
        await seen_read(ctx, part, result_id=result_id)
        await answer(ctx, lease, message, part)
        scope = dict(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        async with get_db_session() as db:
            with sql_reads() as queries:
                report, manifest = await _message_evidence(db, message.id, user_id=ctx.user_id,
                                                            main_id=ctx.session_id)
            assert len(queries) == 1
            assert report.id == result_id and manifest.message_id == message.id
            await validate_message_sources(db, message, **scope)

            # These deliberately corrupt only the isolated fixture. The joined
            # read must retain a report when its manifest no longer matches.
            manifest.kind = "fixture.hidden"
            await db.flush()
            found_report, missing = await _message_evidence(db, message.id, user_id=ctx.user_id,
                                                            main_id=ctx.session_id)
            assert found_report.id == result_id and missing is None
            original = await db.get(Part, report.output_refs[-1]["part_id"])
            original.data = {**original.data, "text": "changed report source"}
            await db.flush()
            with pytest.raises(AssistantError) as error:
                await validate_message_sources(db, message, **scope)
            assert error.value.code == "ASSISTANT_RESULT_SOURCE_CHANGED"

            report.processed_message_id = None
            manifest.kind = "assistant.message.committed"
            await db.flush()
            missing, found_manifest = await _message_evidence(db, message.id, user_id=ctx.user_id,
                                                              main_id=ctx.session_id)
            assert missing is None and found_manifest.id == manifest.id
            missing_report, foreign_manifest = await _message_evidence(db, message.id, user_id="another-actor",
                                                                        main_id=ctx.session_id)
            assert missing_report is foreign_manifest is None
    finally:
        await lease.release(session_status="idle")


async def test_lineage_scalar_constructor_read_preserves_unflushed_child_changes():
    scope, accepted = await task_scope()
    child = await create_session(user_id=scope["user_id"], workspace_id=scope["workspace_id"],
                                 parent_id=accepted["execution_session_id"])
    async with get_db_session() as db:
        held = await db.get(Session, child.id)
        held.title = "pending title in the caller transaction"
        with db.no_autoflush:
            with sql_reads() as queries:
                tasks = await _lineage(db, child.id, **scope)
            assert held.title == "pending title in the caller transaction" and held in db.dirty
        assert len(queries) == 4 and [task.id for task in tasks] == [accepted["task_id"]]
    async with get_db_session() as db:
        assert (await db.get(Session, child.id)).title == "pending title in the caller transaction"


@pytest.mark.parametrize("fault,code", [
    ("child-visibility", "ASSISTANT_EXECUTION_SOURCE_UNAVAILABLE"),
    ("child-parent", "ASSISTANT_EXECUTION_SOURCE_UNAVAILABLE"),
    ("birth", "ASSISTANT_EXECUTION_SOURCE_UNAVAILABLE"),
    ("member", "ASSISTANT_WORKSPACE_FORBIDDEN"),
    ("project", "ASSISTANT_PROJECT_UNAVAILABLE"),
    ("task-link", "ASSISTANT_SOURCE_UNAVAILABLE"),
    ("execution", "ASSISTANT_EXECUTION_UNAVAILABLE"),
    ("body", "ASSISTANT_SOURCE_CHANGED"),
])
async def test_postgres_current_joins_reject_independent_revocation_with_held_orm(fault, code):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Requires independent PostgreSQL connections and an old ORM identity map")
    scope, accepted, reference = await source_scope()
    child = await create_session(user_id=scope["user_id"], workspace_id=scope["workspace_id"],
                                 parent_id=accepted["execution_session_id"])
    async with get_db_session() as db:
        birth = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == child.id,
                                                         AgentEvent.kind == "assistant.isolation.created"))
        execution = await db.get(Session, accepted["execution_session_id"])
        targets = {
            "child-visibility": (Session, child.id, "visibility", "workspace"),
            "child-parent": (Session, child.id, "parent_id", None),
            "birth": (AgentEvent, birth.id, "payload", {**birth.payload, "version": 0}),
            "member": (WorkspaceMember, (scope["workspace_id"], scope["user_id"]), "status", "removed"),
            "project": (Project, execution.project_id, "is_deleted", True),
            "task-link": (AssistantTask, accepted["task_id"], "assistant_session_id", execution.id),
            "execution": (Session, execution.id, "visibility", "workspace"),
            "body": (Part, reference["part_id"], "data", {"type": "text", "text": "changed source"}),
        }
        model, key, field, value = targets[fault]
        held = await db.get(model, key)
        old_value = getattr(held, field)
        use_lineage = fault in {"child-visibility", "child-parent", "birth", "member", "project"}
        if use_lineage:
            assert await _lineage(db, child.id, **scope)
        else:
            assert await _source_original(db, reference, **scope)
        reader_pid = await db.scalar(select(func.pg_backend_pid()))
        async with get_db_session() as writer:
            assert await writer.scalar(select(func.pg_backend_pid())) != reader_pid
            setattr(await writer.get(model, key), field, value)
        assert getattr(held, field) == old_value and old_value != value
        with pytest.raises(AssistantError) as error:
            if use_lineage:
                await _lineage(db, child.id, **scope)
            else:
                await _source_original(db, reference, **scope)
        assert error.value.code == code


async def test_lineage_join_does_not_skip_depth_or_cycle_checks():
    scope, accepted = await task_scope()
    current = accepted["execution_session_id"]
    for _ in range(32):
        child = await create_session(user_id=scope["user_id"], workspace_id=scope["workspace_id"], parent_id=current)
        current = child.id
    async with get_db_session() as db:
        with pytest.raises(AssistantError) as too_deep:
            await _lineage(db, current, **scope)
        assert too_deep.value.code == "ASSISTANT_EXECUTION_SOURCE_UNAVAILABLE"
        birth = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == current,
                                                         AgentEvent.kind == "assistant.isolation.created"))
        # A matching constructor/parent still cannot certify a cycle.
        (await db.get(Session, current)).parent_id = current
        birth.payload = {**birth.payload, "parent_id": current}
    async with get_db_session() as db:
        with pytest.raises(AssistantError) as cycle:
            await _lineage(db, current, **scope)
        assert cycle.value.code == "ASSISTANT_EXECUTION_SOURCE_UNAVAILABLE"
