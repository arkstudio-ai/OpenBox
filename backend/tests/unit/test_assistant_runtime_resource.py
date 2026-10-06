"""Driver startup and claimed attachment IO use durable non-tool origins."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from agent import inbox
from agent.driver import bind_current_lease, reset_current_lease
from assistant.commands import accept_task_command
from assistant.policy import AssistantError
from core.identifier import ascending
from db.base import close_engine, get_db_session, get_engine, init_engine
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantTask
from db.models.external_effect import ExternalEffect
from db.models.file_asset import FileAsset
from db.models.resource_control import ResourceControlLease
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from question.runtime import now
from sandbox.assets import AssetDeliveryError, deliver_asset_ids
from sandbox.manager import SandboxManager
from sandbox.runtime_operation import RuntimePreparationUncertain, run_runtime_operation
from session.agent_event_log import load_canonical_model_surface
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_resource_control import resource, close, drain, ordinary_running  # noqa: F401
from tests.unit.test_assistant_resource_gateway import gateway  # noqa: F401
from tests.unit.test_assistant_resource_commands import remote, accept  # noqa: F401


async def rows(ctx):
    async with get_db_session() as db:
        return list((await db.scalars(select(ExternalEffect).where(
            ExternalEffect.session_id == ctx.session_id, ExternalEffect.adapter == "sandbox_runtime")
            .order_by(ExternalEffect.created_at))).all())


@pytest.fixture
async def runtime(gateway, resource):
    token = bind_current_lease(resource[2])
    try:
        yield gateway[0], gateway[1], gateway[2], SandboxManager()
    finally:
        reset_current_lease(token)


async def test_project_initialization_reuses_exact_success_across_database_reopen(runtime):
    ctx, sent, _, manager = runtime
    await manager._ensure_session_dir(ctx.sandbox, ctx.session_id)
    original, = await rows(ctx)
    assert original.operation == "project_directory" and original.state == "succeeded"
    url = get_engine().url.render_as_string(hide_password=False)
    await close_engine()
    init_engine(url)
    await manager._ensure_session_dir(ctx.sandbox, ctx.session_id)
    assert len(sent) == 1 and (await rows(ctx))[0].id == original.id
    async with get_db_session() as db:
        events = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == ctx.session_id,
            AgentEvent.kind == "resource.runtime_requested"))).all())
    assert len(events) == 1 and events[0].id == original.safe_context["runtime_origin_id"]
    await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)


@pytest.mark.parametrize("change", ["close", "epoch", "journal"])
async def test_later_runtime_stage_cannot_adopt_new_control(runtime, resource, change):
    ctx, sent, _, manager = runtime
    await manager._ensure_session_dir(ctx.sandbox, ctx.session_id)
    if change == "close":
        await close(resource)
    else:
        async with get_db_session() as db:
            row = await db.get(ResourceControlLease, resource[0].resource_id)
            if change == "epoch": row.epoch += 1
            else: row.remote_journal_id = "a" * 32
    async def later():
        await ctx.sandbox.execute("fixture preparation")
        return {"done": True}
    with pytest.raises(AssistantError):
        await run_runtime_operation(ctx.sandbox, session_id=ctx.session_id, user_id=ctx.user_id,
            stage="later_fixture", payload={}, operation=later)
    assert len(sent) == 1 and len(await rows(ctx)) == 1


@pytest.mark.parametrize("failure", ["timeout", "cancel", "exit_code"])
async def test_startup_uncertainty_is_never_swallowed_or_resent(runtime, resource, failure):
    ctx, sent, transport, manager = runtime
    entered = asyncio.Event()
    async def fail(request):
        await transport(request)
        entered.set()
        if failure == "timeout":
            raise httpx.ReadTimeout("fixture response lost", request=request)
        if failure == "cancel":
            await asyncio.Event().wait()
        return httpx.Response(200, json={"exit_code": 1, "stdout": "", "stderr": "fixture failure"})
    ctx.sandbox._transport = httpx.MockTransport(fail)
    pending = asyncio.create_task(manager._ensure_session_dir(ctx.sandbox, ctx.session_id))
    await asyncio.wait_for(entered.wait(), 5)
    if failure == "cancel":
        pending.cancel()
        with pytest.raises(asyncio.CancelledError): await pending
    else:
        with pytest.raises(RuntimePreparationUncertain): await pending
    original, = await rows(ctx)
    assert original.state == "outcome_unknown"
    ctx.sandbox._transport = httpx.MockTransport(transport)
    with pytest.raises(RuntimePreparationUncertain):
        await manager._ensure_session_dir(ctx.sandbox, ctx.session_id)
    assert len(sent) == 1 and (await drain(resource))["blocking_effect_ids"] == [original.id]


@pytest.fixture
async def attachment(runtime, resource, monkeypatch):
    ctx, _, _, manager = runtime
    assets = []
    async with get_db_session() as db:
        session = await db.get(Session, ctx.session_id)
        task = await db.scalar(select(AssistantTask).where(AssistantTask.execution_session_id == ctx.session_id))
        for index in range(2):
            asset = FileAsset(id=ascending("asset"), user_id=ctx.user_id, workspace_id=ctx.workspace_id,
                session_id=session.id, project_id=session.project_id,
                name=f"runtime-fixture-{index}.txt", oss_key=f"fixture/{index}",
                mime="text/plain", size=7, status="ready", source="user", is_deleted=False,
                transient=False, created_at=now())
            db.add(asset)
            assets.append(asset.id)
    if task is not None:
        # A delegated task Session (private visibility, isolated memory) gets
        # its input through the original Task command, as in production.
        accepted = await accept_task_command(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
            main_id=task.assistant_session_id, project_id=task.project_id, task_id=task.id,
            expected_revision=task.control_revision, idempotency_key="runtime-attachments",
            prompt="Read these synthetic attachments", attachments=assets, delivery="steer",
            expected_run={"run_id": ctx.run_id, "generation": ctx.run_generation})
        item_id = accepted["inbox_id"]
    else:
        accepted = await inbox.accept_inbox_item(session_id=ctx.session_id, user_id=ctx.user_id,
            delivery="steer", prompt="Read these synthetic shared attachments", attachments=assets,
            origin="human", origin_ref={"actor_user_id": ctx.user_id,
                "entrypoint": "resource_attachment_fixture"}, client_id="runtime-attachments")
        item_id = accepted.id
    await inbox.claim_inbox_boundary(resource[2], step=2, include_next_turn=False)
    async def client(session_id, *, user_id):
        assert (session_id, user_id) == (ctx.session_id, ctx.user_id)
        await manager._ensure_session_dir(ctx.sandbox, session_id)
        return ctx.sandbox
    monkeypatch.setattr("sandbox.sandbox_manager.get_client", client)
    monkeypatch.setattr("core.oss.get_oss", lambda: SimpleNamespace(
        presign_get=lambda *_args, **_kwargs: "https://fixture.invalid/object?signature=private-fixture"))
    monkeypatch.setattr("sandbox.assets._use_internal_oss", lambda _oss: False)
    return ctx, assets, item_id


async def transfer(attachment):
    ctx, assets, item_id = attachment
    return await deliver_asset_ids(ctx.session_id, ctx.user_id, assets,
        expected_asset_ids=assets, delivery_id=item_id)


async def test_actual_claimed_delivery_has_separate_effect_and_no_restart_overwrite(runtime, attachment, resource):
    ctx, sent, _, _ = runtime
    result = await inbox.deliver_claimed_attachments(resource[2], item_ids=[attachment[2]])
    assert result.runnable_item_ids == (attachment[2],)
    recorded = await rows(ctx)
    assert {r.operation for r in recorded} == {"project_directory", "attachment_cli", "attachment_delivery"}
    assert len(recorded) == 4 and all(r.state == "succeeded" for r in recorded)
    assert len({r.safe_context["runtime_origin_id"] for r in recorded}) == 1
    assert len(sent) == 4  # mkdir, fixed CLI install, two exact attachment downloads.
    assert all("private-fixture" not in json.dumps(r.provider_receipt) for r in recorded)
    url = get_engine().url.render_as_string(hide_password=False)
    await close_engine()
    init_engine(url)
    assert await transfer(attachment) == [f"/workspace/uploads/runtime-fixture-{i}.txt" for i in range(2)]
    assert len(sent) == 4


@pytest.mark.parametrize("change", ["resource", "source", "last_source", "transport"])
async def test_mid_delivery_change_stops_later_files_and_settles_without_automatic_retry(runtime, attachment, resource, change):
    ctx, sent, transport, _ = runtime
    downloads = 0
    async def respond(request):
        nonlocal downloads
        response = await transport(request)
        if " obx-file get " in json.loads(request.content).get("command", ""):
            downloads += 1
            if downloads == (2 if change == "last_source" else 1):
                if change == "resource": await close(resource)
                elif change in {"source", "last_source"}:
                    async with get_db_session() as db:
                        (await db.get(FileAsset, attachment[1][1])).is_deleted = True
                else: raise httpx.ReadTimeout("fixture download response lost", request=request)
        return response
    ctx.sandbox._transport = httpx.MockTransport(respond)
    result = await inbox.deliver_claimed_attachments(resource[2], item_ids=[attachment[2]])
    assert result.terminal_item_ids == (attachment[2],) and not result.runnable_item_ids
    assert downloads == (2 if change == "last_source" else 1) and len(sent) == 2 + downloads
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, attachment[2])
        assert item.delivery_attempts == 1 and item.delivery_last_error["retryable"] is False
        assert item.delivery_last_error["code"] == "resource_preparation_unavailable"
    transfers = [r for r in await rows(ctx) if r.operation == "attachment_delivery"]
    if change == "resource":
        assert len(transfers) == 1 and transfers[0].state == "succeeded"
        assert (await drain(resource))["blocking_effect_ids"] == []
    else:
        assert transfers[-1].state == "outcome_unknown"
        assert (await drain(resource))["blocking_effect_ids"] == [transfers[-1].id]


async def test_changed_or_missing_attachment_origin_never_acquires_a_desktop(runtime, attachment):
    ctx, sent, _, _ = runtime
    with pytest.raises(AssetDeliveryError) as exc:
        await deliver_asset_ids(ctx.session_id, ctx.user_id, attachment[1], delivery_id="unrelated")
    assert exc.value.retryable is False and not sent and not await rows(ctx)


async def test_direct_recovery_keeps_original_inbox_delivery_identity(runtime, attachment, resource):
    ctx, sent, _, _ = runtime
    expected = await transfer(attachment)
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, attachment[2])
        message_id = item.message_id
    await resource[2].bind_trigger_message(message_id)
    # The older direct entry has no explicit Inbox ID but reaches the same
    # accepted input, so it must reuse the original completed transfer.
    assert await deliver_asset_ids(ctx.session_id, ctx.user_id, attachment[1]) == expected
    assert len(sent) == 4 and len(await rows(ctx)) == 4


async def test_child_driver_cannot_inherit_parent_runtime_authority(runtime):
    from agent.effect_ledger import EffectNotDispatchableError
    ctx, sent, _, _ = runtime
    _, child = await ordinary_running()
    try:
        async def command():
            await ctx.sandbox.execute("fixture")
            return {"done": True}
        async def parent():
            token = bind_current_lease(child)
            try:
                with pytest.raises((AssistantError, EffectNotDispatchableError)):
                    await run_runtime_operation(ctx.sandbox, session_id=child.session_id, user_id=child.user_id,
                        stage="child_fixture", payload={}, operation=command)
                assert not sent
            finally:
                reset_current_lease(token)
            return await command()
        assert await run_runtime_operation(ctx.sandbox, session_id=ctx.session_id, user_id=ctx.user_id,
            stage="parent_fixture", payload={}, operation=parent) == {"done": True}
        assert len(sent) == 1
        row, = await rows(ctx)
        assert row.state == "succeeded"
    finally:
        await child.release(session_status="idle")


async def test_real_v2_server_initializes_project_with_pinned_runtime_receipt(runtime, remote, resource, tmp_path, monkeypatch):
    from assistant import resource_commands as commands
    ctx, _, _, manager = runtime
    control = await accept(remote, resource, "bind")
    assert await commands.dispatch(control["command_id"])
    workdir, internal = tmp_path / "project fixture", tmp_path / "internal fixture"
    monkeypatch.setattr("project.workspace.project_directory", lambda _slug: str(workdir))
    monkeypatch.setattr("project.workspace.INTERNAL_ROOT", str(internal))
    monkeypatch.setattr("project.workspace.WORKSPACE_ROOT", str(tmp_path))
    await manager._ensure_session_dir(ctx.sandbox, ctx.session_id)
    assert workdir.is_dir() and internal.is_dir()
    row, = await rows(ctx)
    assert row.state == "succeeded" and row.provider_receipt["result"] == {"directory": str(workdir)}
    assert row.safe_context["resource_journal_id"] == remote[1].status()["journal_id"]
    assert row.provider_receipt["remote_exclusivity_verified"] is False


async def set_task_intent(ctx, state):
    async with get_db_session() as db:
        task = await db.scalar(select(AssistantTask).where(AssistantTask.execution_session_id == ctx.session_id))
        task.desired_state = state


@pytest.mark.parametrize("resource", ["private"], indirect=True)
async def test_delegated_task_preparation_uses_the_shared_runtime_until_its_task_is_paused(runtime, resource):
    from assistant.scheduling import TaskSchedulingHeld
    ctx, sent, _, manager = runtime
    await manager._ensure_session_dir(ctx.sandbox, ctx.session_id)
    original, = await rows(ctx)
    assert original.operation == "project_directory" and original.state == "succeeded" and len(sent) == 1
    await set_task_intent(ctx, "paused")
    with pytest.raises(TaskSchedulingHeld):
        await manager._ensure_session_dir(ctx.sandbox, ctx.session_id)
    assert len(sent) == 1 and len(await rows(ctx)) == 1
    assert not (await drain(resource))["blocking_effect_ids"]


@pytest.mark.parametrize("resource", ["private"], indirect=True)
async def test_delegated_task_attachments_are_delivered_like_ordinary_ones(runtime, attachment, resource):
    ctx, sent, _, _ = runtime
    result = await inbox.deliver_claimed_attachments(resource[2], item_ids=[attachment[2]])
    assert result.runnable_item_ids == (attachment[2],) and not result.terminal_item_ids
    recorded = await rows(ctx)
    assert {r.operation for r in recorded} == {"project_directory", "attachment_cli", "attachment_delivery"}
    assert len(recorded) == 4 and all(r.state == "succeeded" for r in recorded)
    assert len(sent) == 4  # mkdir, fixed CLI install, two exact attachment downloads.
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, attachment[2])
        assert item.delivery_attempts == 0 and item.delivery_last_error is None


async def test_unsent_effect_cannot_adopt_replaced_journal_in_a_new_driver(runtime, resource, monkeypatch):
    from agent.driver import reserve_run
    ctx, sent, _, _ = runtime
    original = ctx.sandbox._authorize_request
    async def revoke_before_send(request):
        async with get_db_session() as db:
            (await db.get(WorkspaceMember, (ctx.workspace_id, ctx.user_id))).status = "removed"
        await original(request)
    async def operation():
        await ctx.sandbox.execute("fixture")
        return {"done": True}
    async def prepare():
        return await run_runtime_operation(ctx.sandbox, session_id=ctx.session_id, user_id=ctx.user_id,
            stage="fixture_continuation", key="original-input", payload={}, operation=operation)
    monkeypatch.setattr(ctx.sandbox, "_authorize_request", revoke_before_send)
    with pytest.raises(AssistantError):
        await prepare()
    previous, = await rows(ctx)
    assert previous.state == "prepared" and previous.claim_token is None and not sent
    async with get_db_session() as db:
        (await db.get(WorkspaceMember, (ctx.workspace_id, ctx.user_id))).status = "active"
    await resource[2].release(session_status="idle")
    async with get_db_session() as db:
        (await db.get(ResourceControlLease, resource[0].resource_id)).remote_journal_id = "a" * 32
    replacement = await reserve_run(ctx.session_id, ctx.user_id)
    token = bind_current_lease(replacement)
    monkeypatch.setattr(ctx.sandbox, "_authorize_request", original)
    try:
        with pytest.raises(AssistantError):
            await prepare()
        current, = await rows(ctx)
        assert not sent and current.id == previous.id
        assert current.safe_context == previous.safe_context and current.run_id == previous.run_id
    finally:
        reset_current_lease(token)
        await replacement.release(session_status="idle")


@pytest.mark.parametrize("resource", ["private"], indirect=True)
async def test_resumed_delegated_task_reuses_its_shared_attachment_delivery(
        runtime, attachment, resource, monkeypatch):
    from agent import loop, processor
    from agent.driver import RecoveredDriver
    from agent.recovery import _trigger_state
    from assistant.control import accept_control_command, recover_controls
    from db.models.agent_driver import AgentDriverState
    from sandbox import sandbox_manager
    from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
    ctx, sent, _, _ = runtime

    def downloads():
        return [request for request in sent
                if " obx-file get " in json.loads(request.content or b"{}").get("command", "")]

    expected = [f"/workspace/uploads/runtime-fixture-{i}.txt" for i in range(2)]
    assert await transfer(attachment) == expected and len(downloads()) == 2
    async with get_db_session() as db:
        task = await db.scalar(select(AssistantTask).where(AssistantTask.execution_session_id == ctx.session_id))
        args = dict(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=task.assistant_session_id,
            task_id=task.id, expected_revision=task.control_revision,
            expected_run={"run_id": ctx.run_id, "generation": ctx.run_generation})
    client = sandbox_manager.get_client
    _patch_real_loop_runtime(monkeypatch, config=_loop_config(), process_step=processor.process_step)
    async def no_suggestions(*_args, **_kwargs):
        return None
    monkeypatch.setattr("agent.suggestions.generate_suggestions", no_suggestions)
    await accept_control_command(**args, action="pause", idempotency_key="runtime-pause")
    await loop.run_loop(ctx.session_id, ctx.user_id, lease=resource[2])
    async with get_db_session() as db:
        task = await db.get(AssistantTask, task.id)
        assert task.observed_state == "paused"
        assert (await db.get(AgentInboxItem, attachment[2])).state == "settled"
        args.update(expected_revision=task.control_revision, expected_run=None)
    await accept_control_command(**args, action="resume", idempotency_key="runtime-resume")
    _, leases = await recover_controls(task_id=task.id, launch=False)
    lease, = leases
    assert lease.generation > resource[2].generation
    monkeypatch.setattr(sandbox_manager, "get_client", client)
    try:
        async with get_db_session() as db:
            driver = await db.get(AgentDriverState, lease.session_id)
            record = RecoveredDriver(session_id=lease.session_id, user_id=lease.user_id,
                run_id=lease.run_id, generation=lease.generation, phase=driver.phase,
                trigger_message_id=driver.trigger_message_id)
        pending, terminal, expected_assets = await _trigger_state(record)
        assert pending and terminal is None and set(expected_assets) == set(attachment[1])
        result = await inbox.deliver_claimed_attachments(lease, expected_asset_ids=expected_assets)
        # The resumed run continues on the shared desktop with the files that
        # already landed there: the original transfers are reused, not resent.
        assert result.should_run_provider and not result.result_message_id
        assert len(downloads()) == 2
    finally:
        await lease.release(session_status="idle")
