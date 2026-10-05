"""Fresh source reads keep their scope while avoiding per-part round trips."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event, select

from assistant.commands import task_locked
from assistant.evidence import validate_source_ref
from assistant.policy import AssistantError
from assistant.results import part_hash, validate_result_source
from assistant.service import ensure_main_session
from db.base import get_db_session, get_engine
from db.models.assistant import AssistantTask, TaskResult
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_results import result_ready


@pytest.fixture
async def original_result():
    owner, workspace, main, accepted, lease, _ = await result_ready()
    await lease.release(session_status="idle")
    async with get_db_session() as db:
        result_id = await db.scalar(select(TaskResult.id).where(TaskResult.task_id == accepted["task_id"]))
    return dict(user_id=owner, workspace_id=workspace, main_id=main.id), accepted, result_id


async def test_large_result_preserves_order_and_duplicates_without_one_query_per_part(original_result):
    scope, _, result_id = original_result
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        original = await db.get(Part, result.output_refs[-1]["part_id"])
        refs = []
        for i in range(220):
            part = Part(id=uuid4().hex, session_id=original.session_id, message_id=original.message_id,
                user_id=original.user_id, type="text", data={"text": f"Evidence {i}"}, created_at=datetime.now(timezone.utc))
            db.add(part)
            refs.append({"kind": "report", "session_id": part.session_id, "message_id": part.message_id,
                "part_id": part.id, "content_hash": part_hash(part)})
        result.output_refs = [*result.output_refs, *reversed(refs), refs[0]]
    statements = []
    def counted(*args):
        statements.append(args[2])
    engine = get_engine().sync_engine
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        event.listen(engine, "before_cursor_execute", counted)
        try:
            task, parts = await validate_result_source(db, result, **scope)
        finally:
            event.remove(engine, "before_cursor_execute", counted)
        assert task.id == result.task_id
        assert [(ref, part.id) for ref, part in parts] == [(ref, ref["part_id"]) for ref in result.output_refs]
        assert len(statements) < 20, len(statements)
        # A duplicate ID with different evidence bytes must not collapse into
        # the first successful hash check when assembling the batched result.
        damaged = SimpleNamespace(id=result.id, task_id=result.task_id, created_at=result.created_at,
                                  output_refs=deepcopy(result.output_refs))
        damaged.output_refs[-1]["content_hash"] = "0" * 64
        with pytest.raises(AssistantError) as denied:
            await validate_result_source(db, damaged, **scope)
        assert denied.value.code == "ASSISTANT_RESULT_SOURCE_CHANGED"


@pytest.mark.parametrize("change", ["hash", "message", "missing", "main_report", "asset"])
async def test_result_part_read_checks_every_reference_and_asset(original_result, change):
    scope, _, result_id = original_result
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        refs = deepcopy(result.output_refs)
        if change == "hash":
            refs[-1]["content_hash"] = "0" * 64
        elif change == "message":
            refs[-1]["message_id"] = "another-message"
        elif change == "missing":
            refs[-1]["part_id"] = "missing-part"
        elif change == "main_report":
            refs[-1]["session_id"] = scope["main_id"]
        else:
            part = await db.get(Part, refs[-1]["part_id"])
            part.type, part.data = "file", {"asset_id": "missing-asset"}
            refs[-1]["content_hash"] = part_hash(part)
        result.output_refs = refs
    async with get_db_session() as db:
        with pytest.raises(AssistantError) as denied:
            await validate_result_source(db, await db.get(TaskResult, result_id), **scope)
        assert denied.value.code == ("ASSISTANT_ASSET_UNAVAILABLE" if change == "asset" else "ASSISTANT_RESULT_SOURCE_CHANGED")


@pytest.mark.parametrize("change", ["actor", "workspace", "main", "task", "execution_owner", "execution_workspace",
    "execution_project", "execution_visibility", "execution_memory", "execution_kind", "execution_deleted",
    "project_owner", "project_workspace", "project_deleted"])
async def test_joined_task_read_retains_owner_workspace_project_and_execution_boundaries(original_result, change):
    scope, accepted, _ = original_result
    other, _, other_workspace = await accounts()
    other_project_id = None
    if change == "execution_project":
        other_main = await ensure_main_session(user_id=other, workspace_id=other_workspace)
        other_project_id = other_main.project_id
    args = {**scope, "task_id": accepted["task_id"]}
    expected = "ASSISTANT_TASK_UNAVAILABLE"
    if change in {"actor", "workspace", "main", "task"}:
        key, value = {"actor": ("user_id", other), "workspace": ("workspace_id", other_workspace),
                      "main": ("main_id", "another-main"), "task": ("task_id", "another-task")}[change]
        args[key] = value
    else:
        async with get_db_session() as db:
            task = await db.get(AssistantTask, accepted["task_id"])
            if change.startswith("execution_"):
                target = await db.get(Session, task.execution_session_id)
                field, value = {"execution_owner": ("user_id", other), "execution_workspace": ("workspace_id", other_workspace),
                    "execution_project": ("project_id", other_project_id), "execution_visibility": ("visibility", "workspace"),
                    "execution_memory": ("memory_policy", "standard"), "execution_kind": ("kind", "cron"),
                    "execution_deleted": ("is_deleted", True)}[change]
                expected = "ASSISTANT_EXECUTION_UNAVAILABLE"
            else:
                target = await db.get(Project, task.project_id)
                field, value = {"project_owner": ("user_id", other), "project_workspace": ("workspace_id", other_workspace),
                                "project_deleted": ("is_deleted", True)}[change]
                expected = "ASSISTANT_PROJECT_UNAVAILABLE"
            setattr(target, field, value)
    async with get_db_session() as db:
        with pytest.raises(AssistantError) as denied:
            await task_locked(db, **args)
        assert denied.value.code == expected


@pytest.mark.parametrize("surface", ["result", "reference"])
async def test_fresh_read_does_not_reuse_held_orm_bytes_after_another_transaction_changes_the_source(original_result, surface):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Independent READ COMMITTED connections require PostgreSQL")
    scope, _, result_id = original_result
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        ref = result.output_refs[-1]
        if surface == "result":
            _, held = await validate_result_source(db, result, **scope)
        else:
            held = await validate_source_ref(db, ref, **scope)
        assert held  # Keep the ORM rows alive across both reads.
        async with get_db_session() as writer:
            source = await writer.get(Part, ref["part_id"])
            source.data = {**source.data, "text": "Changed original in another transaction"}
        with pytest.raises(AssistantError) as denied:
            if surface == "result":
                await validate_result_source(db, result, **scope)
            else:
                await validate_source_ref(db, ref, **scope)
        assert denied.value.code == ("ASSISTANT_RESULT_SOURCE_CHANGED" if surface == "result"
                                     else "ASSISTANT_SOURCE_CHANGED")
