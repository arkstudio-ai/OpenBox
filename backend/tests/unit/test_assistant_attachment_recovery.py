"""Original-input attachment failures converge without replay or new input."""
import asyncio
from dataclasses import asdict

import httpx
import pytest
from sqlalchemy import func, select

from agent import inbox, recovery
from agent.driver import RecoveredDriver, get_driver_state, reserve_recovered_run, reserve_run
from assistant.control import accept_control_command, recover_controls, run_resume
from core.identifier import ascending
from db.base import close_engine, get_db_session, get_engine, init_engine
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantTask, TaskResult, TaskSubmission
from db.models.file_asset import FileAsset
from db.models.message import Message
from db.models.session import Session
from models.message import FilePart, MessageInfo, ToolPartData, ToolStatus
from sandbox.assets import AssetDeliveryError
from session.agent_event_log import verify_agent_event_parity
from session.session import create_session, create_user_message, save_part, update_message_info
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_runtime_resource import attachment, runtime, rows  # noqa: F401
from tests.unit.test_assistant_resource_control import resource, close, drain  # noqa: F401
from tests.unit.test_assistant_resource_gateway import gateway  # noqa: F401


async def record_for(lease):
    state = await get_driver_state(lease.session_id)
    return RecoveredDriver(session_id=state.session_id, user_id=state.user_id,
        run_id=state.run_id, generation=state.generation, phase=state.phase,
        trigger_message_id=state.trigger_message_id)


async def failures(lease):
    async with get_db_session() as db:
        return list((await db.scalars(select(AgentEvent).where(
            AgentEvent.session_id == lease.session_id, AgentEvent.kind == "attachment.delivery_failed",
        ).order_by(AgentEvent.sequence))).all())


@pytest.fixture
async def direct(monkeypatch):
    owner, _, workspace = await accounts()
    session = await create_session(user_id=owner, workspace_id=workspace)
    lease = await reserve_run(session.id, owner)
    assets = ["fixture-asset"]
    message_id = ascending("message")
    message = await create_user_message(session.id, "Keep my original constraints", user_id=owner,
        run_fence=(session.id, lease.run_id, lease.generation), bind_trigger=True, message_id=message_id,
        additional_parts=(FilePart(path="/workspace/uploads/fixture.txt", mime_type="text/plain",
            asset_id=assets[0], session_id=session.id, message_id=message_id),))
    async def must_not_run(*_args, **_kwargs):
        pytest.fail("Attachment failure reached the provider loop")
    monkeypatch.setattr("agent.loop.run_loop", must_not_run)
    yield lease, assets, message.id
    await lease.release(session_status="idle")


@pytest.mark.parametrize("code", ["asset_unavailable", "resource_preparation_unavailable"])
async def test_direct_permanent_failure_closes_once_without_creating_inbox(direct, monkeypatch, code):
    lease, assets, parent = direct
    calls = []
    completed_children = []
    original_complete = recovery._complete_recovered_child_outputs
    async def complete_children(current):
        completed_children.append(current.run_id)
        await original_complete(current)
    monkeypatch.setattr(recovery, "_complete_recovered_child_outputs", complete_children)
    async def fail(*_args, **_kwargs):
        calls.append(True)
        raise AssetDeliveryError(expected_asset_ids=assets, missing_asset_ids=assets,
            retryable=False, code=code)
    monkeypatch.setattr("sandbox.assets.deliver_asset_ids", fail)
    await recovery._run_recovered_prompt(lease, assets)
    assert len(calls) == 1 and (await get_driver_state(lease.session_id)).phase == "idle"
    assert completed_children == [lease.run_id]
    event, = await failures(lease)
    assert event.payload["attempt"] == 1 and event.payload["error"]["code"] == code
    async with get_db_session() as db:
        message = await db.get(Message, event.payload["result_message_id"])
        assert message.parent_id == parent and message.finish == "error"
        assert message.error["reason_code"] == code
        assert (await db.get(Session, lease.session_id)).status == "error"
        assert await db.scalar(select(func.count()).select_from(AgentInboxItem).where(
            AgentInboxItem.session_id == lease.session_id)) == 0
        assert await db.scalar(select(func.count()).select_from(Message).where(
            Message.session_id == lease.session_id, Message.role == "user")) == 1
    assert (await verify_agent_event_parity(lease.session_id, user_id=lease.user_id)).ok


async def test_direct_transient_budget_survives_database_and_driver_recovery(direct, monkeypatch):
    lease, assets, _ = direct
    calls = []
    async def fail(*_args, **_kwargs):
        calls.append(True)
        raise RuntimeError("private fixture url?signature=must-not-be-persisted")
    monkeypatch.setattr("sandbox.assets.deliver_asset_ids", fail)
    for attempt in range(1, inbox.MAX_DURABLE_DELIVERY_ATTEMPTS + 1):
        await recovery._run_recovered_prompt(lease, assets)
        saved = await failures(lease)
        assert len(saved) == attempt and saved[-1].payload["attempt"] == attempt
        assert "must-not-be-persisted" not in str(saved[-1].payload)
        if attempt < inbox.MAX_DURABLE_DELIVERY_ATTEMPTS:
            record = await record_for(lease)
            assert record.phase == "reserved"
            assert (await verify_agent_event_parity(lease.session_id, user_id=lease.user_id)).open_turn_ids
            url = get_engine().url.render_as_string(hide_password=False)
            await close_engine()
            init_engine(url)
            lease = await reserve_recovered_run(record, initial_phase="reserved")
    assert len(calls) == 3 and (await get_driver_state(lease.session_id)).phase == "idle"
    assert len({event.payload["delivery_key"] for event in saved}) == 1
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(Message).where(
            Message.session_id == lease.session_id, Message.role == "assistant")) == 1
    parity = await verify_agent_event_parity(lease.session_id, user_id=lease.user_id)
    assert parity.ok, str(parity.model_dump())


@pytest.mark.parametrize("interruption", ["cancel", "hold"])
async def test_preparation_interruption_does_not_consume_delivery_budget(direct, monkeypatch, interruption):
    from assistant.scheduling import TaskHold, TaskSchedulingHeld
    lease, assets, _ = direct
    async def interrupt(*_args, **_kwargs):
        if interruption == "cancel":
            raise asyncio.CancelledError()
        raise TaskSchedulingHeld(TaskHold(None, "paused", None))
    monkeypatch.setattr("sandbox.assets.deliver_asset_ids", interrupt)
    if interruption == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await recovery._run_recovered_prompt(lease, assets)
    else:
        await recovery._run_recovered_prompt(lease, assets)
    assert not await failures(lease)
    record = await record_for(lease)
    assert record.phase == "reserved"
    cleanup = await reserve_recovered_run(record, initial_phase="finalizing")
    await cleanup.release(session_status="idle")


async def test_concurrent_terminal_delivery_failure_has_one_public_error(direct, monkeypatch):
    lease, assets, _ = direct
    async def fail(*_args, **_kwargs):
        raise AssetDeliveryError(expected_asset_ids=assets, missing_asset_ids=assets, retryable=False)
    monkeypatch.setattr("sandbox.assets.deliver_asset_ids", fail)
    results = await asyncio.gather(*(inbox.deliver_claimed_attachments(lease, expected_asset_ids=assets)
        for _ in range(2)))
    assert results[0].result_message_id == results[1].result_message_id
    assert all(not result.should_run_provider for result in results)
    assert len(await failures(lease)) == 1


async def test_stale_generation_cannot_record_an_attachment_error(direct):
    from agent.driver import LeaseLostError
    lease, assets, _ = direct
    await lease.preserve_for_recovery(session_status="error")
    successor = await reserve_recovered_run(await record_for(lease), initial_phase="reserved")
    try:
        with pytest.raises(LeaseLostError):
            await inbox.deliver_claimed_attachments(lease, expected_asset_ids=assets)
        assert not await failures(successor)
    finally:
        await successor.release(session_status="idle")


async def test_changed_expected_assets_cannot_reset_retry_identity_or_start_transfer(direct, monkeypatch):
    lease, assets, _ = direct
    calls = []
    async def fail(*_args, **_kwargs):
        calls.append(True)
        raise RuntimeError("fixture temporary failure")
    monkeypatch.setattr("sandbox.assets.deliver_asset_ids", fail)
    await recovery._run_recovered_prompt(lease, assets)
    successor = await reserve_recovered_run(await record_for(lease), initial_phase="reserved")
    await recovery._run_recovered_prompt(successor, ["different-fixture"])
    events = await failures(successor)
    assert len(calls) == 1 and len(events) == 2
    assert events[-1].payload["attempt"] == 2
    assert events[-1].payload["delivery_key"] == events[0].payload["delivery_key"]
    assert events[-1].payload["error"]["code"] == "asset_origin_unavailable"
    assert events[-1].payload["result_message_id"]


@pytest.fixture
async def resumed(runtime, attachment, resource, monkeypatch):
    from agent import loop, processor
    from sandbox import sandbox_manager
    from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
    ctx, assets, original_id = attachment
    # gateway supplies an artificial running tool call for resource-context
    # tests. Complete that fixture before checking canonical pause parity.
    from db.models.part import Part
    async with get_db_session() as db:
        part = ToolPartData.model_validate((await db.get(Part, ctx.part_id)).data)
    part.status, part.output = ToolStatus.COMPLETED, "fixture completed"
    await save_part(part, user_id=ctx.user_id, run_fence=ctx.run_fence)
    await update_message_info(MessageInfo(id=ctx.message_id, session_id=ctx.session_id,
        role="assistant", finish="tool_calls"), user_id=ctx.user_id, run_fence=ctx.run_fence)
    client = sandbox_manager.get_client
    _patch_real_loop_runtime(monkeypatch, config=_loop_config(), process_step=processor.process_step)
    async def no_suggestions(*_args, **_kwargs):
        return None
    monkeypatch.setattr("agent.suggestions.generate_suggestions", no_suggestions)
    async with get_db_session() as db:
        task = await db.scalar(select(AssistantTask).where(AssistantTask.execution_session_id == ctx.session_id))
        args = dict(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=task.assistant_session_id,
            task_id=task.id, expected_revision=task.control_revision,
            expected_run={"run_id": ctx.run_id, "generation": ctx.run_generation})
    await accept_control_command(**args, action="pause", idempotency_key="attachment-failure-pause")
    await loop.run_loop(ctx.session_id, ctx.user_id, lease=resource[2])
    async with get_db_session() as db:
        task = await db.get(AssistantTask, task.id)
        assert task.observed_state == "paused"
        original = await inbox.get_inbox_item(original_id, user_id=ctx.user_id)
        args.update(expected_revision=task.control_revision, expected_run=None)
    await accept_control_command(**args, action="resume", idempotency_key="attachment-failure-resume")
    _, leases = await recover_controls(task_id=task.id, launch=False)
    lease, = leases
    monkeypatch.setattr(sandbox_manager, "get_client", client)
    async def must_not_run(*_args, **_kwargs):
        pytest.fail("A failed original attachment reached the provider loop")
    monkeypatch.setattr(loop, "run_loop", must_not_run)
    yield ctx, assets, lease, task.id, original
    await lease.release(session_status="idle")


@pytest.mark.parametrize("resource", ["private"], indirect=True)
@pytest.mark.parametrize("failure", ["source", "resource", "transport"])
async def test_private_resume_denial_precedes_other_failures_and_retains_original_inputs(
        resumed, runtime, resource, failure):
    from assistant.results import deliver_task_result
    ctx, assets, lease, task_id, original = resumed
    _, sent, transport, _ = runtime
    if failure == "source":
        async with get_db_session() as db:
            (await db.get(FileAsset, assets[0])).is_deleted = True
    elif failure == "resource":
        await close(resource)
    else:
        async def lose_response(request):
            await transport(request)
            raise httpx.ReadTimeout("fixture response lost", request=request)
        ctx.sandbox._transport = httpx.MockTransport(lose_response)
    await run_resume(lease)
    assert (await get_driver_state(ctx.session_id)).phase == "idle"
    event, = await failures(lease)
    assert event.payload["attempt"] == 1 and event.payload["error"]["retryable"] is False
    assert event.payload["error"]["code"] == "private_runtime_unavailable"
    assert asdict(await inbox.get_inbox_item(original.id, user_id=ctx.user_id)) == asdict(original)
    async with get_db_session() as db:
        task = await db.get(AssistantTask, task_id)
        results = list((await db.scalars(select(TaskResult).where(TaskResult.task_id == task_id))).all())
        result = await db.get(TaskResult, task.latest_result_id)
        assert len(results) == 2 and result.outcome == task.observed_state == "error"
        assert result.run_id == lease.run_id and result.generation == lease.generation
        assert original.id in result.consumed_inbox_ids
        assert result.result_message_id == event.payload["result_message_id"]
        assert await db.scalar(select(func.count()).select_from(TaskSubmission).where(
            TaskSubmission.task_id == task_id)) == 2
        assert await db.scalar(select(func.count()).select_from(Message).where(
            Message.session_id == ctx.session_id, Message.role == "user")) == 2
    await deliver_task_result(result.id)
    async with get_db_session() as db:
        result = await db.get(TaskResult, result.id)
        assert result.delivery_state == ("blocked" if failure == "source" else "accepted")
    parity = await verify_agent_event_parity(ctx.session_id, user_id=ctx.user_id)
    assert parity.ok, str(parity.model_dump())
    # No transport failure or completed prefix can exist: private bytes are
    # refused before this shared client is acquired or an effect is prepared.
    assert not sent and not await rows(ctx)
    assert not (await drain(resource))["blocking_effect_ids"]


@pytest.mark.parametrize("resource", ["private"], indirect=True)
async def test_terminal_commit_before_release_crash_recovers_without_new_result_or_transfer(
        resumed, monkeypatch):
    ctx, assets, lease, task_id, _ = resumed
    async with get_db_session() as db:
        (await db.get(FileAsset, assets[0])).is_deleted = True
    original_release = recovery._release_recovery_status
    async def crash_before_release(*_args, **_kwargs):
        raise RuntimeError("fixture after terminal commit")
    monkeypatch.setattr(recovery, "_release_recovery_status", crash_before_release)
    await run_resume(lease)
    record = await record_for(lease)
    assert record.phase == "reserved"
    async with get_db_session() as db:
        result_id = (await db.get(AssistantTask, task_id)).latest_result_id
    monkeypatch.setattr(recovery, "_release_recovery_status", original_release)
    resumed_ids, invalid = await recovery.resume_reserved_prompts([record])
    assert not invalid and not resumed_ids
    assert (await get_driver_state(ctx.session_id)).phase == "idle"
    async with get_db_session() as db:
        assert (await db.get(Session, ctx.session_id)).status == "error"
        task = await db.get(AssistantTask, task_id)
        assert task.latest_result_id == result_id
        result = await db.get(TaskResult, result_id)
        assert result.run_id == lease.run_id and result.generation == lease.generation
        assert await db.scalar(select(func.count()).select_from(TaskResult).where(
            TaskResult.task_id == task_id)) == 2
    assert len(await failures(lease)) == 1
    parity = await verify_agent_event_parity(ctx.session_id, user_id=ctx.user_id)
    assert parity.ok, str(parity.model_dump())


@pytest.mark.parametrize("resource", ["private"], indirect=True)
async def test_private_resume_denial_cannot_become_a_transient_desktop_retry(resumed, monkeypatch):
    ctx, assets, lease, task_id, original = resumed
    calls = []
    async def unavailable(*_args, **_kwargs):
        calls.append(True)
        raise RuntimeError("fixture desktop connection temporarily unavailable")
    monkeypatch.setattr("sandbox.sandbox_manager.get_client", unavailable)
    await run_resume(lease)
    event, = await failures(lease)
    assert event.payload["attempt"] == 1
    assert event.payload["error"]["code"] == "private_runtime_unavailable"
    assert event.payload["error"]["retryable"] is False
    assert event.payload["origin"]["resume_command_id"]
    assert not calls
    assert (await get_driver_state(ctx.session_id)).phase == "idle"
    assert asdict(await inbox.get_inbox_item(original.id, user_id=ctx.user_id)) == asdict(original)
    async with get_db_session() as db:
        assert (await db.get(AssistantTask, task_id)).observed_state == "error"
        assert await db.scalar(select(func.count()).select_from(TaskResult).where(
            TaskResult.task_id == task_id)) == 2
    parity = await verify_agent_event_parity(ctx.session_id, user_id=ctx.user_id)
    assert parity.ok, str(parity.model_dump())


@pytest.mark.parametrize("resource", ["private"], indirect=True)
async def test_resume_terminal_message_and_task_result_rollback_together(resumed, monkeypatch):
    from assistant import results
    ctx, assets, lease, task_id, _ = resumed
    async with get_db_session() as db:
        (await db.get(FileAsset, assets[0])).is_deleted = True
    original = results.record_execution_result_locked
    async def fail_transaction(*_args, **_kwargs):
        raise RuntimeError("fixture result transaction failed")
    monkeypatch.setattr(results, "record_execution_result_locked", fail_transaction)
    await run_resume(lease)
    assert not await failures(lease)
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(TaskResult).where(
            TaskResult.task_id == task_id)) == 1
        assert not await db.scalar(select(AgentEvent.id).where(AgentEvent.session_id == ctx.session_id,
            AgentEvent.run_id == lease.run_id, AgentEvent.kind == "turn.finished"))
    monkeypatch.setattr(results, "record_execution_result_locked", original)
    successor = await reserve_recovered_run(await record_for(lease), initial_phase="reserved")
    await run_resume(successor)
    event, = await failures(successor)
    assert event.payload["attempt"] == 1
    async with get_db_session() as db:
        task = await db.get(AssistantTask, task_id)
        result = await db.get(TaskResult, task.latest_result_id)
        assert result.generation == successor.generation and result.result_message_id == event.payload["result_message_id"]
        assert await db.scalar(select(func.count()).select_from(TaskResult).where(
            TaskResult.task_id == task_id)) == 2
    parity = await verify_agent_event_parity(ctx.session_id, user_id=ctx.user_id)
    assert parity.ok, str(parity.model_dump())
