"""Commands retain and recheck the actual provider evidence used to generate them."""
import json

import pytest
from sqlalchemy import func, select

from assistant.commands import ToolSource, accept_task_command
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask
from db.models.part import Part
from db.models.session import Session
from tests.unit.assistant_source_fixtures import consume_context
from tests.unit.test_assistant_assets import asset_for
from tests.unit.test_assistant_decisions import start
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_reads import call_tool
from tests.unit.test_assistant_schedule_commands import controlled_wakes  # noqa: F401


@pytest.fixture(autouse=True)
def no_task_dispatch(monkeypatch):
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)


def arguments(ctx, answer):
    return {"project_id": ctx.project_id, "title": "Derived task", "instructions": "Only produce a text response",
            "source_message_ids": [answer.parent_id]}


async def assert_no_command_or_execution(ctx):
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(AssistantCommand).where(
            AssistantCommand.actor_user_id == ctx.user_id)) == 0
        assert await db.scalar(select(func.count()).select_from(AssistantTask).where(
            AssistantTask.user_id == ctx.user_id)) == 0
        assert await db.scalar(select(func.count()).select_from(Session).where(
            Session.user_id == ctx.user_id, Session.id != ctx.session_id)) == 0


@pytest.mark.parametrize("response", [None, False])
async def test_uncheckpointed_or_unconsumed_provider_cannot_admit_command(response):
    ctx, lease, answer = await start("Create a text-only task")
    try:
        if response is not None:
            await consume_context(ctx, respond=response)
        result, _, _ = await call_tool(ctx, "tasks.submit", arguments(ctx, answer))
        assert result.metadata["failure_code"] == "ASSISTANT_COMMAND_CONTEXT_UNVERIFIED"
        await assert_no_command_or_execution(ctx)
    finally:
        await lease.release(session_status="idle")


async def test_actual_provider_context_is_retained_by_command_and_bound_to_delegated_input():
    ctx, lease, answer = await start("Create a text-only task using the current project")
    try:
        await call_tool(ctx, "projects.list", {})
        await consume_context(ctx)
        result, call_ctx, part = await call_tool(ctx, "tasks.submit", arguments(ctx, answer))
        assert not result.metadata.get("error"), result.output
        receipt = json.loads(result.output)
        async with get_db_session() as db:
            command = await db.get(AssistantCommand, receipt["command_id"])
            delegated = await db.get(AgentInboxItem, receipt["inbox_id"])
            proof = command.source_ref["derivation"]
            assert proof["version"] == 1 and proof["main_id"] == ctx.session_id
            assert proof["business_reads"][0]["operation"] == "projects.list"
            assert any(ref["message_id"] == answer.parent_id for ref in proof["source_refs"])
            assert delegated.origin == "assistant_delegation"
            from assistant.commands import command_digest
            assert delegated.origin_ref["derivation_ref"] == {
                "version": 1, "command_id": command.id, "digest": command_digest(proof)}
            assert "derivation" not in delegated.origin_ref
            assert command.source_ref["source_refs"] != proof["business_reads"]
            # Simulate authority loss after acceptance. A transport replay must
            # return its original receipt, without accepting another command.
            source_part = await db.get(Part, command.source_ref["source_refs"][0]["part_id"])
            source_part.data = {**source_part.data, "ignored": True}
        replay = await accept_task_command(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
            main_id=ctx.session_id, idempotency_key="ignored-model-key", project_id=ctx.project_id,
            title="Derived task", prompt="Only produce a text response",
            source=ToolSource(part.id, call_ctx.run_id, call_ctx.run_generation, (answer.parent_id,)))
        assert replay == receipt
        async with get_db_session() as db:
            assert await db.scalar(select(func.count()).select_from(AssistantTask).where(
                AssistantTask.user_id == ctx.user_id)) == 1
    finally:
        await lease.release(session_status="idle")


async def test_source_revocation_after_provider_response_rolls_back_command_and_execution():
    ctx, lease, answer = await start("Use the listed resource name when creating a text task")
    try:
        asset = await asset_for(ctx.user_id, ctx.workspace_id)
        await call_tool(ctx, "assets.list", {})
        await consume_context(ctx)
        async with get_db_session() as db:
            from db.models.file_asset import FileAsset
            row = await db.get(FileAsset, asset.id)
            row.is_deleted = True
        result, _, _ = await call_tool(ctx, "tasks.submit", arguments(ctx, answer))
        assert result.metadata.get("error"), result.output
        assert result.metadata["failure_code"] != "ASSISTANT_COMMAND_CONTEXT_UNVERIFIED"
        await assert_no_command_or_execution(ctx)
    finally:
        await lease.release(session_status="idle")


async def test_unseen_human_request_cannot_be_substituted_for_actual_consumed_context(monkeypatch):
    ctx, lease, answer = await start("Create a text-only task")
    try:
        original_human = answer.parent_id
        await consume_context(ctx)
        from tests.unit.test_assistant_context_sources import finish, next_turn
        await finish(ctx, lease, answer, "Please choose the task project.")
        ctx, lease, answer = await next_turn(ctx, "Use the default project.")
        monkeypatch.setattr("assistant.projection.MAX_RECENT_MESSAGES", 1)
        await consume_context(ctx)
        args = {**arguments(ctx, answer), "source_message_ids": [original_human]}
        result, _, _ = await call_tool(ctx, "tasks.submit", args)
        assert result.metadata["failure_code"] == "ASSISTANT_COMMAND_CONTEXT_UNVERIFIED"
        await assert_no_command_or_execution(ctx)
    finally:
        await lease.release(session_status="idle")


async def delegated_asset_task():
    ctx, lease, answer = await start("Use my resource metadata for one text-only task")
    asset = await asset_for(ctx.user_id, ctx.workspace_id)
    await call_tool(ctx, "assets.list", {})
    await consume_context(ctx)
    result, _, _ = await call_tool(ctx, "tasks.submit", arguments(ctx, answer))
    assert not result.metadata.get("error"), result.output
    return ctx, lease, answer, asset, json.loads(result.output)


async def revoke_asset(asset):
    from db.models.file_asset import FileAsset
    async with get_db_session() as db:
        (await db.get(FileAsset, asset.id)).is_deleted = True


async def settle_execution(ctx, receipt):
    from agent import inbox
    from agent.driver import reserve_run
    from models.message import TextPart
    from session.session import create_assistant_message, save_part, update_message_info
    execution = await reserve_run(receipt["execution_session_id"], ctx.user_id)
    try:
        fence = (execution.session_id, execution.run_id, execution.generation)
        batch = await inbox.claim_inbox_boundary(execution, step=1, include_next_turn=True)
        message = await create_assistant_message(execution.session_id, batch.messages[0].id,
            model_id="test/model", agent="build", user_id=ctx.user_id, run_fence=fence)
        await save_part(TextPart(session_id=execution.session_id, message_id=message.id,
            text="Result derived from the accepted instructions"), user_id=ctx.user_id, is_new=True, run_fence=fence)
        message.finish = "stop"
        await update_message_info(message, user_id=ctx.user_id, run_fence=fence)
        await inbox.settle_claimed_inbox_items(execution, result_message_id=message.id, outcome="succeeded")
    finally:
        await execution.release(session_status="idle")
    from db.models.assistant import TaskResult
    async with get_db_session() as db:
        return await db.scalar(select(TaskResult).where(TaskResult.task_id == receipt["task_id"]))


async def test_revocation_after_admission_blocks_root_and_descendant_dispatch_after_engine_reopen():
    from agent.driver import reserve_run
    from assistant.scheduling import TaskSchedulingHeld, require_runnable
    from db.base import close_engine, init_engine
    from session.session import create_session
    ctx, lease, _, asset, receipt = await delegated_asset_task()
    try:
        child = await create_session(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
                                     parent_id=receipt["execution_session_id"])
        await require_runnable(child.id, ctx.user_id)
        await revoke_asset(asset)
        async with get_db_session() as db:
            url = db.get_bind().url.render_as_string(hide_password=False)
        await close_engine()
        init_engine(url)
        with pytest.raises(TaskSchedulingHeld):
            await reserve_run(receipt["execution_session_id"], ctx.user_id)
        with pytest.raises(TaskSchedulingHeld):
            await require_runnable(child.id, ctx.user_id)
        async with get_db_session() as db:
            assert (await db.get(AgentInboxItem, receipt["inbox_id"])).state == "accepted"
    finally:
        await lease.release(session_status="idle")


async def test_revoked_derivation_cannot_be_delivered_as_a_fresh_result_report():
    from assistant.policy import AssistantError
    from assistant.results import deliver_task_result, validate_result_source
    from db.models.assistant import TaskResult
    ctx, lease, _, asset, receipt = await delegated_asset_task()
    try:
        result = await settle_execution(ctx, receipt)
        await revoke_asset(asset)
        async with get_db_session() as db:
            with pytest.raises(AssistantError):
                await validate_result_source(db, result, user_id=ctx.user_id,
                    workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        assert await deliver_task_result(result.id) is None
        async with get_db_session() as db:
            assert (await db.get(TaskResult, result.id)).delivery_state == "blocked"
    finally:
        await lease.release(session_status="idle")


async def test_later_unconsumed_followup_does_not_rewrite_an_older_result_derivation():
    from assistant.results import validate_result_source
    from assistant.scheduling import TaskSchedulingHeld, require_runnable
    ctx, lease, answer, _, receipt = await delegated_asset_task()
    try:
        original = await settle_execution(ctx, receipt)
        future_asset = await asset_for(ctx.user_id, ctx.workspace_id)
        await call_tool(ctx, "assets.list", {})
        await consume_context(ctx)
        async with get_db_session() as db:
            revision = (await db.get(AssistantTask, receipt["task_id"])).control_revision
        result, _, _ = await call_tool(ctx, "tasks.followup", {"task_id": receipt["task_id"],
            "text": "Now use the newly listed resource", "expected_revision": revision,
            "source_message_ids": [answer.parent_id]})
        assert not result.metadata.get("error"), result.output
        await revoke_asset(future_asset)
        with pytest.raises(TaskSchedulingHeld):
            await require_runnable(receipt["execution_session_id"], ctx.user_id)
        async with get_db_session() as db:
            await validate_result_source(db, original, user_id=ctx.user_id,
                workspace_id=ctx.workspace_id, main_id=ctx.session_id)
    finally:
        await lease.release(session_status="idle")


async def test_human_run_of_a_model_defined_schedule_keeps_definition_derivation_for_result_delivery():
    from assistant.results import deliver_task_result
    from assistant.schedule_commands import run_schedule
    from db.models.assistant import TaskResult
    ctx, lease, answer = await start("Schedule text-only work using my current resource metadata")
    try:
        asset = await asset_for(ctx.user_id, ctx.workspace_id)
        await call_tool(ctx, "assets.list", {})
        await consume_context(ctx)
        output, _, _ = await call_tool(ctx, "schedules.create", {"project_id": ctx.project_id,
            "name": "Derived schedule", "instructions": "Only produce text", "enabled": False,
            "schedule": {"kind": "every", "every_ms": 600000}, "source_message_ids": [answer.parent_id]})
        assert not output.metadata.get("error"), output.output
        created = json.loads(output.output)
        receipt = await run_schedule(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
            job_id=created["job_id"], expected_revision=1, idempotency_key="human-manual-run")
        result = await settle_execution(ctx, receipt)
        await revoke_asset(asset)
        assert await deliver_task_result(result.id) is None
        async with get_db_session() as db:
            assert (await db.get(TaskResult, result.id)).delivery_state == "blocked"
    finally:
        await lease.release(session_status="idle")


async def test_large_provider_derivation_is_not_copied_into_bounded_execution_input():
    ctx, lease, answer = await start("Read my resource metadata, then create a text-only task")
    try:
        for number in range(16):
            await asset_for(ctx.user_id, ctx.workspace_id, name=f"PRIVATE_METADATA_{number}_" + "detail" * 35)
        await call_tool(ctx, "assets.list", {})
        await consume_context(ctx)
        output, _, _ = await call_tool(ctx, "tasks.submit", arguments(ctx, answer))
        assert not output.metadata.get("error"), output.output
        receipt = json.loads(output.output)
        async with get_db_session() as db:
            command = await db.get(AssistantCommand, receipt["command_id"])
            accepted = await db.get(AgentInboxItem, receipt["inbox_id"])
            assert len(json.dumps(command.source_ref["derivation"], ensure_ascii=True)) > 8192
            assert len(json.dumps(accepted.origin_ref, ensure_ascii=True)) < 8192
            assert "PRIVATE_METADATA" not in json.dumps(accepted.origin_ref)
            assert len(command.source_ref["derivation"]["business_reads"][0]["projection"]["items"]) == 16
        from assistant.scheduling import TaskSchedulingHeld, require_runnable
        await require_runnable(receipt["execution_session_id"], ctx.user_id)
        async with get_db_session() as db:
            accepted = await db.get(AgentInboxItem, receipt["inbox_id"])
            accepted.origin_ref = {**accepted.origin_ref, "derivation_ref": {
                **accepted.origin_ref["derivation_ref"], "digest": "0" * 64}}
        with pytest.raises(TaskSchedulingHeld):
            await require_runnable(receipt["execution_session_id"], ctx.user_id)
    finally:
        await lease.release(session_status="idle")
