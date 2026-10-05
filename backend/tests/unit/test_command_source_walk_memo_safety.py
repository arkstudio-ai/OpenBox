"""Single-walk SQL reuse never substitutes for changed scope or evidence.

Use real accepted Tasks, Inbox settlement, source references and SQL. The
existing graph-bound suite separately covers cycles, depth and cardinality.
"""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update

from assistant.command_sources import _command_walk, validate_task_command_sources
from assistant.evidence import validate_source_ref
from assistant.policy import AssistantError
from assistant.results import part_hash, validate_result_source
from db.base import get_db_session, get_engine
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantTask, TaskResult
from db.models.message import Message
from db.models.part import Part
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from tests.offline_wuying import install_wuying_offline_guard
from tests.unit.test_assistant_command_sources import (
    delegated_asset_task,
    no_task_dispatch,  # noqa: F401
    revoke_asset,
    settle_execution,
)
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_source_queries import original_result  # noqa: F401


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    install_wuying_offline_guard(monkeypatch)


@pytest.mark.parametrize("change", ["membership", "part"])
async def test_next_top_level_validation_rechecks_independent_revocation_with_held_rows(original_result, change):
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Independent READ COMMITTED connections require PostgreSQL")
    scope, _, result_id = original_result
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        member = await db.get(WorkspaceMember, (scope["workspace_id"], scope["user_id"]))
        _, held_parts = await validate_result_source(db, result, **scope)
        source = held_parts[-1][1]
        old_bytes = deepcopy(source.data)
        reader_pid = await db.scalar(select(func.pg_backend_pid()))
        async with get_db_session() as writer:
            assert await writer.scalar(select(func.pg_backend_pid())) != reader_pid
            if change == "membership":
                (await writer.get(WorkspaceMember, (scope["workspace_id"], scope["user_id"]))).status = "removed"
            else:
                row = await writer.get(Part, source.id)
                row.data = {**row.data, "text": "The original evidence was changed independently"}
        # These identities remain held in the same reader Session. A new root
        # must use current SQL instead of retaining the earlier completed walk.
        assert member.status == "active" and source.data == old_bytes
        with pytest.raises(AssistantError) as denied:
            await validate_result_source(db, result, **scope)
        assert denied.value.code == ("ASSISTANT_WORKSPACE_FORBIDDEN" if change == "membership"
                                      else "ASSISTANT_RESULT_SOURCE_CHANGED")


@pytest.mark.parametrize("rebound_first", ["reference", "result"])
async def test_same_walk_preserves_full_reference_and_result_hash_identity(original_result, rebound_first):
    scope, _, result_id = original_result
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        reference = deepcopy(result.output_refs[-1])
        with _command_walk(db):
            await validate_source_ref(db, reference, **scope)
            with pytest.raises(AssistantError) as ref_denied:
                await validate_source_ref(db, {**reference, "content_hash": "0" * 64}, **scope)
            assert ref_denied.value.code == "ASSISTANT_SOURCE_CHANGED"

            await validate_result_source(db, result, **scope)
            altered = SimpleNamespace(id=result.id, task_id=result.task_id, created_at=result.created_at,
                                      output_refs=deepcopy(result.output_refs))
            altered.output_refs[-1]["content_hash"] = "0" * 64
            with pytest.raises(AssistantError) as result_denied:
                await validate_result_source(db, altered, **scope)
            assert result_denied.value.code == "ASSISTANT_RESULT_SOURCE_CHANGED"

            if get_engine().dialect.name == "postgresql":
                # A different, valid version can refresh the same held ORM
                # Part in READ COMMITTED. An older memo key must never return
                # those new bytes as if they still matched its original hash.
                reader_pid = await db.scalar(select(func.pg_backend_pid()))
                async with get_db_session() as writer:
                    assert await writer.scalar(select(func.pg_backend_pid())) != reader_pid
                    current = await writer.get(Part, reference["part_id"])
                    current.data = {**current.data, "text": "A separately committed new source version"}
                    newer = {**reference, "content_hash": part_hash(current)}
                current = await validate_source_ref(db, newer, **scope)
                assert part_hash(current) == newer["content_hash"] != reference["content_hash"]
                outcomes = {}
                validators = {"reference": lambda: validate_source_ref(db, reference, **scope),
                              "result": lambda: validate_result_source(db, result, **scope)}
                # Each cache surface must reject first, before the other one's
                # detection can invalidate the complete walk on its behalf.
                for label in (rebound_first, *(key for key in validators if key != rebound_first)):
                    try:
                        await validators[label]()
                    except AssistantError as denied:
                        outcomes[label] = denied.code
                    else:
                        outcomes[label] = "accepted_changed_bytes"
                assert outcomes == {"reference": "ASSISTANT_SOURCE_CHANGED",
                                    "result": "ASSISTANT_RESULT_SOURCE_CHANGED"}


async def test_same_task_different_input_boundary_cannot_reuse_an_empty_command_list():
    ctx, lease, _, asset, receipt = await delegated_asset_task()
    try:
        await settle_execution(ctx, receipt)
        await revoke_asset(asset)
        async with get_db_session() as db:
            task = await db.get(AssistantTask, receipt["task_id"])
            item = await db.get(AgentInboxItem, receipt["inbox_id"])
            assert item.message_id is not None
            original_input = await db.get(Message, item.message_id)
            boundary = original_input.created_at
            with _command_walk(db):
                # The same Task is harmless before this materialized input,
                # but its actual command depends on the now-revoked asset.
                await validate_task_command_sources(db, task, before=boundary - timedelta(microseconds=1))
                with pytest.raises(AssistantError) as denied:
                    await validate_task_command_sources(db, task, before=boundary)
                assert denied.value.code == "ASSISTANT_ASSET_UNAVAILABLE"
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("mutation", ["dirty", "flush", "bulk", "transaction"])
async def test_scope_changes_disable_original_reuse_during_the_same_walk(original_result, mutation):
    scope, _, result_id = original_result
    async with get_db_session() as db:
        result = await db.get(TaskResult, result_id)
        main = await db.get(Session, scope["main_id"])
        with _command_walk(db):
            await validate_result_source(db, result, **scope)
            if mutation == "bulk":
                await db.execute(update(Session).where(Session.id == main.id)
                    .values(memory_policy="standard").execution_options(synchronize_session=False))
                assert main.memory_policy == "assistant_isolated"
            elif mutation == "transaction":
                await db.commit()
                async with get_db_session() as writer:
                    (await writer.get(Session, main.id)).memory_policy = "standard"
                assert main.memory_policy == "assistant_isolated"
            else:
                main.memory_policy = "standard"
                if mutation == "flush":
                    await db.flush()
            # A pending local denial counts even before flush; the SQL scope
            # check must also reject a committed change behind held ORM rows.
            with db.no_autoflush:
                with pytest.raises(AssistantError) as denied:
                    await validate_result_source(db, result, **scope)
            assert denied.value.code == "ASSISTANT_UNAVAILABLE"
