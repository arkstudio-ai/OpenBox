"""Shared SQL structure keeps fresh parameters, transactions and source cut points."""
import asyncio
from copy import deepcopy

import pytest
from sqlalchemy import event, text

from assistant import command_sources, execution_sources, task_context
from assistant.commands import _authority, accept_task_command, read_task_scopes
from assistant.execution_sources import _lineage
from assistant.policy import AssistantError
from assistant.service import ensure_main_session
from db.base import get_db_session, get_engine
from db.models.assistant import TaskResult
from db.models.message import Message
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from project.workspace import create_project
from tests.unit.test_assistant_commands import setup_task
from tests.unit.test_assistant_command_sources import no_task_dispatch  # noqa: F401
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_source_queries import original_result  # noqa: F401
from tests.unit.test_assistant_task_source_batches import observed_task


async def scopes():
    owner, other, workspace, main, args = await setup_task()
    first = await accept_task_command(**args)
    peer_main = await ensure_main_session(user_id=other, workspace_id=workspace, model="test/model")
    peer_project = await create_project(user_id=other, workspace_id=workspace, name="Peer statement fixture")
    second = await accept_task_command(**{
        **args, "user_id": other, "main_id": peer_main.id, "project_id": peer_project.id,
    })
    third_owner, _, third_workspace, third_main, third_args = await setup_task()
    third = await accept_task_command(**third_args)
    return [
        (dict(user_id=owner, workspace_id=workspace, main_id=main.id), first),
        (dict(user_id=other, workspace_id=workspace, main_id=peer_main.id), second),
        (dict(user_id=third_owner, workspace_id=third_workspace, main_id=third_main.id), third),
    ]


async def inspect_scope(scope, receipt, *, ids):
    async with get_db_session() as db:
        main = await _authority(db, **scope)
        rows = await read_task_scopes(db, **scope, task_ids=ids)
        lineage = await _lineage(db, receipt["execution_session_id"], **scope)
        assert main.id == scope["main_id"]
        assert [task.id for task, _ in rows] == ids
        assert all(task.user_id == scope["user_id"] and task.workspace_id == scope["workspace_id"]
                   and session.id == task.execution_session_id for task, session in rows)
        assert [task.id for task in lineage] == [receipt["task_id"]]
        return main.id


async def test_cached_structure_keeps_alternating_parameters_and_every_sql_read():
    cases = await scopes()
    statements = []

    def count(_connection, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append((statement, deepcopy(parameters)))

    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", count)
    try:
        for index in (0, 1, 2, 0, 2, 1):
            scope, receipt = cases[index]
            await inspect_scope(scope, receipt, ids=[receipt["task_id"], receipt["task_id"]])
        assert len(statements) == 18  # Three SQL reads on every call, including warm expressions.
        for scope, receipt in reversed(cases):
            await inspect_scope(scope, receipt, ids=[])
        assert len(statements) == 24  # Empty tasks retain their original zero-query contract.
    finally:
        event.remove(engine, "before_cursor_execute", count)


async def test_concurrent_calls_do_not_share_actor_workspace_main_or_execution_parameters():
    cases = await scopes()
    result = await asyncio.gather(*(
        inspect_scope(scope, receipt, ids=[receipt["task_id"]])
        for scope, receipt in [*cases, *reversed(cases), *cases]
    ))
    assert result == [scope["main_id"] for scope, _ in [*cases, *reversed(cases), *cases]]


@pytest.mark.parametrize("fault,expected", [
    ("actor", "ASSISTANT_UNAVAILABLE"),
    ("workspace", "ASSISTANT_WORKSPACE_FORBIDDEN"),
    ("main", "ASSISTANT_UNAVAILABLE"),
    ("null_main", "ASSISTANT_UNAVAILABLE"),
    ("null_actor", "ASSISTANT_WORKSPACE_FORBIDDEN"),
    ("null_workspace", "ASSISTANT_WORKSPACE_FORBIDDEN"),
])
async def test_warmed_authority_does_not_reuse_a_previous_identity(fault, expected):
    cases = await scopes()
    scope, receipt = cases[0]
    async with get_db_session() as db:
        await _authority(db, **scope)
        changed = dict(scope)
        key, value = {
            "actor": ("user_id", cases[1][0]["user_id"]),
            "workspace": ("workspace_id", cases[2][0]["workspace_id"]),
            "main": ("main_id", cases[1][0]["main_id"]),
            "null_main": ("main_id", None), "null_actor": ("user_id", None),
            "null_workspace": ("workspace_id", None),
        }[fault]
        changed[key] = value
        with pytest.raises(AssistantError) as denied:
            await _authority(db, **changed)
        assert denied.value.code == expected
        assert (await _authority(db, **scope)).id == scope["main_id"]
        with pytest.raises(AssistantError) as denied:
            await read_task_scopes(db, **cases[1][0], task_ids=[receipt["task_id"]])
        assert denied.value.code == "ASSISTANT_TASK_UNAVAILABLE"
        with pytest.raises(AssistantError) as denied:
            await _lineage(db, receipt["execution_session_id"], **cases[1][0])
        assert denied.value.code == "ASSISTANT_EXECUTION_SOURCE_UNAVAILABLE"


@pytest.mark.parametrize("surface", ["authority", "task_scope", "lineage"])
async def test_postgres_warm_statement_retains_next_read_revocation(surface):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Independent READ COMMITTED writer requires PostgreSQL")
    scope, receipt = (await scopes())[0]

    async def current(db):
        if surface == "authority":
            return await _authority(db, **scope)
        if surface == "task_scope":
            return await read_task_scopes(db, **scope, task_ids=[receipt["task_id"]])
        return await _lineage(db, receipt["execution_session_id"], **scope)

    async with get_db_session() as reader:
        assert await reader.scalar(text("SHOW transaction_isolation")) == "read committed"
        held = await current(reader)
        async with get_db_session() as writer:
            if surface == "authority":
                (await writer.get(WorkspaceMember, (scope["workspace_id"], scope["user_id"]))).status = "removed"
            else:
                (await writer.get(Session, receipt["execution_session_id"])).visibility = "workspace"
        assert held is not None
        with pytest.raises(AssistantError) as denied:
            await current(reader)
        assert denied.value.code == {
            "authority": "ASSISTANT_WORKSPACE_FORBIDDEN", "task_scope": "ASSISTANT_EXECUTION_UNAVAILABLE",
            "lineage": "ASSISTANT_EXECUTION_SOURCE_UNAVAILABLE",
        }[surface]


async def test_postgres_result_rebinding_after_task_scope_still_hits_later_result_query(original_result, monkeypatch):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Independent READ COMMITTED writer requires PostgreSQL")
    scope, _, result_id = original_result
    ref = await observed_task(scope)
    _, successor = (await scopes())[0]
    original_read = task_context.read_task_scopes
    writes = 0

    async def interleaved(db, **kwargs):
        nonlocal writes
        rows = await original_read(db, **kwargs)
        async with get_db_session() as writer:
            (await writer.get(TaskResult, result_id)).task_id = successor["task_id"]
        writes += 1
        return rows

    async with get_db_session() as reader:
        main = await reader.get(Session, scope["main_id"])
        # A valid original ref reaches the intended post-scope sampling point.
        await task_context.validate_task_snapshots(reader, main, [ref])
        monkeypatch.setattr(task_context, "read_task_scopes", interleaved)
        with pytest.raises(AssistantError) as denied:
            await task_context.validate_task_snapshots(reader, main, [ref])
        assert denied.value.code == "ASSISTANT_TASK_SNAPSHOT_CHANGED"
        assert writes == 1


async def test_postgres_result_boundary_is_still_read_after_lineage(original_result, monkeypatch):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Independent READ COMMITTED writer requires PostgreSQL")
    scope, _, result_id = original_result
    original_lineage = execution_sources._lineage
    original_commands = command_sources.validate_task_command_sources
    boundaries = []

    async def interleaved(db, *args, **kwargs):
        tasks = await original_lineage(db, *args, **kwargs)
        async with get_db_session() as writer:
            (await writer.get(TaskResult, result_id)).result_message_id = None
        return tasks

    async def observed_commands(db, task, **kwargs):
        boundaries.append(kwargs.get("before"))
        return await original_commands(db, task, **kwargs)

    async with get_db_session() as reader:
        result = await reader.get(TaskResult, result_id)
        message = await reader.get(Message, result.result_message_id)
        assert result.created_at != message.created_at
        await execution_sources.validate_execution_message(reader, message, **scope)
        monkeypatch.setattr(execution_sources, "_lineage", interleaved)
        monkeypatch.setattr(command_sources, "validate_task_command_sources", observed_commands)
        await execution_sources.validate_execution_message(reader, message, **scope)
        assert boundaries == [message.created_at]
