"""Current authority joins match sequential checks without reusing a grant."""
import pytest
from sqlalchemy import func, select

from assistant.commands import _authority, task_locked
from assistant.evidence import _source_original
from assistant.execution_sources import _lineage
from assistant.policy import AssistantError
from assistant.results import _result_original
from assistant.service import ensure_main_session
from assistant.source_scope import read_authorized_task
from db.base import get_db_session, get_engine
from db.models.assistant import AssistantTask, TaskResult
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
from db.models.workspace import Workspace, WorkspaceMember
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_command_sources import no_task_dispatch  # noqa: F401
from tests.unit.test_assistant_source_queries import original_result  # noqa: F401
from tests.unit.test_assistant_source_join_queries import source_scope, sql_reads, task_scope


async def test_result_original_keeps_the_full_ordered_body_after_one_scope_query(original_result):
    scope, accepted, result_id = original_result
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        with sql_reads() as queries:
            task, parts = await _result_original(db, result, **scope)
        assert task.id == accepted["task_id"] and len(queries) == 1
        assert [(ref, part.id) for ref, part in parts] == [(ref, ref["part_id"]) for ref in result.output_refs]


@pytest.mark.parametrize("faults", [
    ("workspace", "main_deleted", "task_missing"),
    ("member", "main_deleted", "task_missing"),
    ("main_missing", "task_missing"),
    ("task_invalid",),
    ("main_owner",), ("main_workspace",), ("main_kind",),
    ("main_visibility",), ("main_memory",), ("main_deleted",),
    ("task_missing", "execution_visibility", "project_deleted"),
    ("task_owner",), ("task_workspace",), ("task_main",),
    ("execution_owner",), ("execution_workspace",), ("execution_project",),
    ("execution_memory",), ("execution_kind",), ("execution_deleted",),
    ("execution_visibility", "project_deleted"),
    ("project_owner",), ("project_workspace",), ("project_deleted",),
])
async def test_combined_scope_matches_original_sequential_refusal_priority(faults):
    scope, accepted = await task_scope()
    other, _, other_workspace = await accounts()
    other_main = await ensure_main_session(user_id=other, workspace_id=other_workspace)
    args = {**scope, "task_id": accepted["task_id"]}
    async with get_db_session() as db:
        main = await db.get(Session, scope["main_id"])
        task = await db.get(AssistantTask, accepted["task_id"])
        execution = await db.get(Session, task.execution_session_id)
        project = await db.get(Project, task.project_id)
        for fault in faults:
            if fault == "workspace":
                (await db.get(Workspace, scope["workspace_id"])).is_deleted = True
            elif fault == "member":
                (await db.get(WorkspaceMember, (scope["workspace_id"], scope["user_id"]))).status = "removed"
            elif fault == "main_missing":
                args["main_id"] = "not-an-assistant"
            elif fault == "task_missing":
                args["task_id"] = "not-a-task"
            elif fault == "task_invalid":
                args["task_id"] = 17
            else:
                target_name, field = fault.split("_", 1)
                target = {"main": main, "task": task, "execution": execution, "project": project}[target_name]
                field, value = {
                    "owner": ("user_id", other), "workspace": ("workspace_id", other_workspace),
                    "kind": ("kind", "normal" if target_name == "main" else "cron"),
                    "visibility": ("visibility", "workspace"), "memory": ("memory_policy", "standard"),
                    "deleted": ("is_deleted", True), "main": ("assistant_session_id", other_main.id),
                    "project": ("project_id", other_main.project_id),
                }[field]
                setattr(target, field, value)
    async with get_db_session() as db:
        with pytest.raises(AssistantError) as before:
            await _authority(db, user_id=args["user_id"], workspace_id=args["workspace_id"], main_id=args["main_id"])
            await task_locked(db, **args)
    async with get_db_session() as db:
        with sql_reads() as queries:
            with pytest.raises(AssistantError) as after:
                await read_authorized_task(db, **args)
        assert len(queries) == 1
    assert (after.value.status, after.value.code, str(after.value)) == (
        before.value.status, before.value.code, str(before.value))


async def test_combined_scope_preserves_unflushed_main_fields_and_handles_expired_policy():
    scope, accepted = await task_scope()
    async with get_db_session() as db:
        main = await db.get(Session, scope["main_id"])
        main.title, main.model = "pending title", "pending/model"
        with db.no_autoflush:
            await read_authorized_task(db, **scope, task_id=accepted["task_id"])
            assert await _lineage(db, accepted["execution_session_id"], **scope)
            assert main.title == "pending title" and main.model == "pending/model" and main in db.dirty
        await db.flush()
        db.expire(main, ["memory_policy"])
        with sql_reads() as queries:
            await read_authorized_task(db, **scope, task_id=accepted["task_id"])
        assert len(queries) == 1  # No implicit lazy load from an expired ORM policy.
    async with get_db_session() as db:
        main = await db.get(Session, scope["main_id"])
        assert (main.title, main.model) == ("pending title", "pending/model")


async def test_unflushed_main_policy_can_only_add_the_original_authority_refusal():
    scope, accepted = await task_scope()
    async with get_db_session() as db:
        main = await db.get(Session, scope["main_id"])
        main.memory_policy = "standard"
        with db.no_autoflush:
            for check, args in (
                (_authority, scope),
                (read_authorized_task, {**scope, "task_id": accepted["task_id"]}),
                (_lineage, {**scope, "session_id": accepted["execution_session_id"]}),
            ):
                with pytest.raises(AssistantError) as error:
                    await check(db, **args)
                assert error.value.code == "ASSISTANT_UNAVAILABLE"
        await db.rollback()


@pytest.mark.parametrize("fault,code", [
    ("workspace", "ASSISTANT_WORKSPACE_FORBIDDEN"),
    ("member", "ASSISTANT_WORKSPACE_FORBIDDEN"),
    ("main", "ASSISTANT_UNAVAILABLE"),
    ("task", "ASSISTANT_TASK_UNAVAILABLE"),
    ("task_project", "ASSISTANT_EXECUTION_UNAVAILABLE"),
    ("execution", "ASSISTANT_EXECUTION_UNAVAILABLE"),
    ("project", "ASSISTANT_PROJECT_UNAVAILABLE"),
])
async def test_postgres_combined_scope_rechecks_held_orm_after_independent_revocation(fault, code):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Requires independent PostgreSQL connections with held ORM identities")
    scope, accepted = await task_scope()
    other, _, other_workspace = await accounts()
    other_main = await ensure_main_session(user_id=other, workspace_id=other_workspace)
    async with get_db_session() as db:
        main = await db.get(Session, scope["main_id"])
        task, execution = await read_authorized_task(db, **scope, task_id=accepted["task_id"])
        targets = {
            "workspace": (Workspace, scope["workspace_id"], "is_deleted", True),
            "member": (WorkspaceMember, (scope["workspace_id"], scope["user_id"]), "status", "removed"),
            "main": (Session, main.id, "memory_policy", "standard"),
            "task": (AssistantTask, task.id, "assistant_session_id", execution.id),
            "task_project": (AssistantTask, task.id, "project_id", other_main.project_id),
            "execution": (Session, execution.id, "visibility", "workspace"),
            "project": (Project, task.project_id, "is_deleted", True),
        }
        model, key, field, value = targets[fault]
        held = await db.get(model, key)
        original = getattr(held, field)
        reader_pid = await db.scalar(select(func.pg_backend_pid()))
        async with get_db_session() as writer:
            assert await writer.scalar(select(func.pg_backend_pid())) != reader_pid
            setattr(await writer.get(model, key), field, value)
        assert getattr(held, field) == original and original != value
        with sql_reads() as queries:
            with pytest.raises(AssistantError) as error:
                await read_authorized_task(db, **scope, task_id=accepted["task_id"])
        assert len(queries) == 1 and error.value.code == code


async def test_postgres_join_does_not_load_changed_body_after_its_link_is_revoked():
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Requires independent PostgreSQL transactions")
    scope, accepted, reference = await source_scope()
    async with get_db_session() as db:
        held = await db.get(Part, reference["part_id"])
        original = dict(held.data)
        reader_pid = await db.scalar(select(func.pg_backend_pid()))
        async with get_db_session() as writer:
            assert await writer.scalar(select(func.pg_backend_pid())) != reader_pid
            task = await writer.get(AssistantTask, accepted["task_id"])
            task.assistant_session_id = task.execution_session_id
            changed = await writer.get(Part, held.id)
            changed.data = {**changed.data, "text": "changed after the source link was revoked"}
        with pytest.raises(AssistantError) as error:
            await _source_original(db, reference, **scope)
        assert error.value.code == "ASSISTANT_SOURCE_UNAVAILABLE"
        assert held.data == original  # Failed scope joins never populate this ORM body.
