"""Durable task holds stop new work without discarding already completed facts."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import func, select

from agent import inbox
from agent.driver import (RecoveredDriver, bind_current_lease, reserve_recovered_run,
                          reserve_run, reset_current_lease)
from assistant.commands import accept_task_command
from assistant.scheduling import TaskSchedulingHeld, task_hold
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantTask, TaskResult, TaskSubmission
from db.models.session import Session
from session.agent_event_log import load_canonical_model_surface, verify_agent_event_parity
from session.session import create_session
from tests.unit.test_assistant_commands import setup_task
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_steering import running, terminal
from tool.tool import ToolContext, ToolResult, define_tool


async def hold(task_id, state='paused'):
    # Only disposable test rows are changed; public controls have a separate
    # Command/revision contract and are not simulated by synthetic user input.
    async with get_db_session() as db:
        task = await db.get(AssistantTask, task_id)
        task.desired_state = state
        task.control_revision += 1


@pytest.mark.parametrize('state', ['paused', 'canceled'])
async def test_hold_preserves_queued_input_and_blocks_driver_and_wake(state):
    owner, _, _, main, command = await setup_task()
    receipt = await accept_task_command(**command)
    await hold(receipt['task_id'], state)
    with pytest.raises(TaskSchedulingHeld):
        await reserve_run(receipt['execution_session_id'], owner)
    assert await inbox.wake_inbox_session(receipt['execution_session_id'], owner) is None
    assert (await inbox.get_inbox_item(receipt['inbox_id'], user_id=owner)).state == 'accepted'
    async with get_db_session() as db:
        assert await db.get(AgentDriverState, receipt['execution_session_id']) is None
        assert (await db.get(TaskSubmission, receipt['submission_id'])).applied_at is None
    # A private main and independent tasks are not held by this task.
    lease = await reserve_run(main.id, owner)
    await lease.release(session_status='idle')


async def test_descendants_inherit_hold_from_persisted_lineage():
    owner, _, workspace, _, command = await setup_task()
    receipt = await accept_task_command(**command)
    child = await create_session(user_id=owner, workspace_id=workspace,
        parent_id=receipt['execution_session_id'])
    cron = await create_session(user_id=owner, workspace_id=workspace, parent_id=child.id, kind='cron')
    await hold(receipt['task_id'])
    for target in (child.id, cron.id):
        assert (await task_hold(target, owner)).task_id == receipt['task_id']
        with pytest.raises(TaskSchedulingHeld):
            await reserve_run(target, owner)
    async with get_db_session() as db:
        (await db.get(Session, child.id)).is_deleted = True
    assert (await task_hold(cron.id, owner)).state == 'unavailable'


async def test_hold_between_reserve_and_claim_keeps_input_unconsumed():
    owner, _, _, _, command = await setup_task()
    receipt = await accept_task_command(**command)
    lease = await reserve_run(receipt['execution_session_id'], owner)
    try:
        await hold(receipt['task_id'])
        assert (await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)).empty
        assert await lease.abort_was_requested()
        await lease.assert_current()  # The owner still has authority to settle.
        assert not lease._lost
    finally:
        await lease.release(session_status='idle')
    assert (await inbox.get_inbox_item(receipt['inbox_id'], user_id=owner)).state == 'accepted'


async def test_held_recovery_allows_settlement_but_not_reserved_prompt_replay():
    args, receipt, lease, batch = await running()
    await hold(receipt['task_id'])
    # The external result already exists when the process loses its lease.
    answer = await terminal(lease, batch.messages[0].id)
    record = RecoveredDriver(lease.session_id, lease.user_id, lease.run_id,
        lease.generation, 'running', batch.messages[0].id)
    await lease.preserve_for_recovery()
    with pytest.raises(TaskSchedulingHeld):
        await reserve_recovered_run(record, initial_phase='reserved')
    maintenance = await reserve_recovered_run(record, initial_phase='finalizing')
    try:
        await inbox.rebind_recovered_claims(record, maintenance)
        await inbox.settle_claimed_inbox_items(maintenance, result_message_id=answer.id, outcome='recovered')
        async with get_db_session() as db:
            results = list((await db.scalars(select(TaskResult).where(TaskResult.task_id == args['task_id']))).all())
            assert len(results) == 1 and results[0].run_id == lease.run_id
            assert (await db.get(AssistantTask, args['task_id'])).desired_state == 'paused'
    finally:
        await maintenance.release(session_status='idle')
    assert (await verify_agent_event_parity(lease.session_id, user_id=lease.user_id)).ok


async def test_held_reserved_wake_is_sent_to_maintenance_without_execution(monkeypatch):
    from agent.recovery import repair_interrupted_session, resume_reserved_prompts
    owner, _, _, _, command = await setup_task()
    receipt = await accept_task_command(**command)
    lease = await reserve_run(receipt['execution_session_id'], owner)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    record = RecoveredDriver(lease.session_id, lease.user_id, lease.run_id,
        lease.generation, 'reserved', batch.messages[0].id)
    await hold(receipt['task_id'])
    await lease.preserve_for_recovery()
    async def forbidden(*a, **kw):
        raise AssertionError('Recovery must never launch this held prompt')
    monkeypatch.setattr('agent.recovery._run_recovered_prompt', forbidden)
    resumed, invalid = await resume_reserved_prompts([record])
    assert not resumed and invalid == [record]
    await repair_interrupted_session(lease.session_id, lease.user_id, recovered=record)
    item = await inbox.get_inbox_item(receipt['inbox_id'], user_id=owner)
    assert item.state == 'settled'
    async with get_db_session() as db:
        assert (await db.get(AgentDriverState, lease.session_id)).phase == 'idle'
        result = await db.scalar(select(TaskResult).where(TaskResult.task_id == receipt['task_id']))
        assert result.run_id == lease.run_id and result.generation == lease.generation
    assert (await verify_agent_event_parity(lease.session_id, user_id=owner)).ok


async def test_prepared_and_direct_tools_recheck_hold_before_body():
    from agent.hooks import ToolHooks
    args, _, lease, _ = await running()
    called = []
    async def body(params, ctx):
        called.append(params)
        return ToolResult(output='completed effect')
    class Params(BaseModel):
        pass
    ctx = ToolContext(session_id=lease.session_id, user_id=lease.user_id, abort=lease.abort,
        run_id=lease.run_id, run_generation=lease.generation, _assert_current=lease.assert_current)
    hooks = ToolHooks(lease.session_id, lease.user_id)
    async def allowed(*a, **kw):
        return None
    hooks.authorize_tool = allowed
    try:
        prepared = await hooks.prepare_execute('test', body, {}, ctx)
        await hold(args['task_id'])
        with pytest.raises(TaskSchedulingHeld):
            await hooks.dispatch_execute(prepared)
        direct = define_tool('test', description='test', parameters=Params, execute=body, sandbox_required=False)
        with pytest.raises(TaskSchedulingHeld):
            await direct.execute({}, ctx)
        assert not called and ctx.abort.is_set()
    finally:
        await lease.release(session_status='idle')


async def test_completed_tool_can_publish_its_receipt_after_hold():
    from agent.hooks import ToolHooks
    args, _, lease, _ = await running()
    async def body(params, ctx):
        return ToolResult(output='The earlier operation succeeded', metadata={'receipt': 'original'})
    async def allowed(*a, **kw):
        return None
    hooks = ToolHooks(lease.session_id, lease.user_id)
    hooks.authorize_tool = allowed
    ctx = ToolContext(session_id=lease.session_id, user_id=lease.user_id,
        _assert_current=lease.assert_current)
    try:
        prepared = await hooks.prepare_execute('test', body, {}, ctx)
        outcome = await hooks.dispatch_execute(prepared)
        await hold(args['task_id'])
        result = await hooks.finalize_execute(prepared, outcome)
        assert result.output == 'The earlier operation succeeded'
        assert result.metadata['receipt'] == 'original' and not result.metadata.get('error')
    finally:
        await lease.release(session_status='idle')


async def test_held_provider_checkpoint_and_stream_start_no_request_or_billing(monkeypatch):
    monkeypatch.setenv('LITELLM_LOCAL_MODEL_COST_MAP', 'true')
    from agent.llm import metered_completion, stream_llm
    from session.agent_event_log import checkpoint_model_request
    args, _, lease, _ = await running()
    fence = (lease.session_id, lease.run_id, lease.generation)
    async def unexpected(*a, **kw):
        raise AssertionError('A held task must not start billing')
    monkeypatch.setattr('billing.service.UsageMeter.start', unexpected)
    try:
        await load_canonical_model_surface(lease.session_id, user_id=lease.user_id, run_fence=fence)
        await hold(args['task_id'])
        with pytest.raises(TaskSchedulingHeld):
            await checkpoint_model_request(lease.session_id, user_id=lease.user_id, run_fence=fence,
                request_id='held-request', model_id='test/model', provider_binding_digest='a'*64,
                tool_schema_digest='b'*64, prompt_shape_digest='c'*64,
                expected_event_sequence=1, expected_event_digest='d'*64)
        ctx = ToolContext(session_id=lease.session_id, user_id=lease.user_id, abort=lease.abort)
        with pytest.raises(TaskSchedulingHeld):
            async for _ in stream_llm(None, [], [], {}, 'test/model', ctx):
                raise AssertionError('No provider output is allowed')
        with pytest.raises(TaskSchedulingHeld):
            await metered_completion(ctx=ctx, billing_kind='title', model='test/model', messages=[])
        async with get_db_session() as db:
            assert not await db.scalar(select(AgentEvent.id).where(AgentEvent.session_id == lease.session_id,
                AgentEvent.kind == 'model.requested'))
    finally:
        await lease.release(session_status='idle')


@pytest.mark.parametrize('mode', ['litellm', 'responses', 'completion'])
async def test_hold_after_meter_admission_still_blocks_provider_transport(monkeypatch, mode):
    monkeypatch.setenv('LITELLM_LOCAL_MODEL_COST_MAP', 'true')
    from agent import llm
    from tests.unit.test_agent_loop_terminal_steps import _loop_config
    args, _, lease, _ = await running()
    config = _loop_config()
    monkeypatch.setattr('core.config.get_config', lambda: config)
    async def admitted(**kwargs):
        await hold(args['task_id'])
        return None
    def forbidden(*a, **kw):
        raise AssertionError('No provider network request may begin after the hold')
    monkeypatch.setattr('billing.service.UsageMeter.start', admitted)
    monkeypatch.setattr('litellm.acompletion', forbidden)
    monkeypatch.setattr(httpx.AsyncClient, 'stream', forbidden)
    ctx = ToolContext(session_id=lease.session_id, user_id=lease.user_id, abort=lease.abort)
    try:
        if mode == 'completion':
            with pytest.raises(TaskSchedulingHeld):
                await llm.metered_completion(ctx=ctx, billing_kind='title', model=config.model, messages=[])
        else:
            model = 'openai/gpt-5.1' if mode == 'responses' else config.model
            events = [event async for event in llm.stream_llm(None, [], [], {}, model, ctx)]
            assert len(events) == 1 and events[0]['type'] == 'error'
            assert isinstance(events[0]['error'], TaskSchedulingHeld)
        assert ctx.abort.is_set()
    finally:
        await lease.release(session_status='idle')


async def test_held_sandbox_rejects_transport_but_allows_lease_cleanup():
    from sandbox.client import SandboxClient
    args, _, lease, _ = await running()
    client = SandboxClient('sandbox.invalid', 8000, 'test')
    requests = []
    def transport(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={})
    token = bind_current_lease(lease)
    try:
        await hold(args['task_id'])
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport), base_url=client.base_url,
                event_hooks={'request': [client._authorize_request]}) as http:
            with pytest.raises(TaskSchedulingHeld):
                await http.post('/execute', json={'command': 'must not execute'})
            await http.post('/desktop/lease/release', json={})
        assert requests == ['/desktop/lease/release']
    finally:
        reset_current_lease(token)
        await lease.release(session_status='idle')


async def test_private_attachments_are_denied_before_a_mid_delivery_hold_can_exist(monkeypatch):
    from core.config import OpenBoxConfig, PrivateRuntimeConfig
    from db.models.file_asset import FileAsset
    from sandbox.manager import SandboxManager
    # Private execution now has a separately authorized supplier. Exercise
    # the real disabled route, not an obsolete get_client sentinel that would
    # also reject legitimate private provisioning before its own scope check.
    config = OpenBoxConfig(private_runtime=PrivateRuntimeConfig(enabled=False))
    monkeypatch.setattr('core.config.get_config', lambda: config)
    monkeypatch.setattr('sandbox.sandbox_manager', SandboxManager())
    owner, _, workspace, _, command = await setup_task()
    asset_ids = [uuid4().hex, uuid4().hex]
    async with get_db_session() as db:
        for index, asset_id in enumerate(asset_ids):
            db.add(FileAsset(id=asset_id, user_id=owner, workspace_id=workspace, name=f'attachment-{index}.txt',
                oss_key=f'qa/{asset_id}', mime='text/plain', size=1, status='ready', source='user',
                transient=False, is_deleted=False, created_at=datetime.now(timezone.utc)))
    receipt = await accept_task_command(**{**command, 'attachments': asset_ids})
    lease, batch = await inbox._reserve_and_claim(receipt['execution_session_id'], owner)
    calls = []
    class NoSharedProvider:
        def __getattr__(self, name):
            calls.append(name)
            raise AssertionError('Private attachment rejection must precede ordinary provider acquisition')
    monkeypatch.setattr('sandbox.provider', NoSharedProvider())
    try:
        result = await inbox.deliver_claimed_attachments(lease, item_ids=[receipt['inbox_id']],
            expected_asset_ids=batch.attachment_ids)
        assert result.terminal_item_ids == (receipt['inbox_id'],) and not result.runnable_item_ids
        assert not calls
        async with get_db_session() as db:
            item = await db.get(AgentInboxItem, receipt['inbox_id'])
            assert item.state == 'settled' and item.delivery_attempts == 1
            assert item.delivery_last_error['code'] == 'private_runtime_unavailable'
            assert item.delivery_last_error['retryable'] is False
            assert (await db.get(AssistantTask, receipt['task_id'])).desired_state == 'running'
    finally:
        await lease.release(session_status='idle')


async def test_held_lease_does_not_acquire_desktop(monkeypatch):
    from sandbox import sandbox_manager
    args, _, lease, _ = await running()
    async def forbidden(*a, **kw):
        raise AssertionError('Held execution cannot acquire a desktop')
    monkeypatch.setattr(sandbox_manager, 'acquire', forbidden)
    token = bind_current_lease(lease)
    try:
        await hold(args['task_id'])
        with pytest.raises(TaskSchedulingHeld):
            await sandbox_manager.get_client(lease.session_id, user_id=lease.user_id)
    finally:
        reset_current_lease(token)
        await lease.release(session_status='idle')


async def test_held_cron_skips_before_summary_or_resources(monkeypatch):
    from cron.executor import execute_cron_job
    owner, _, workspace, _, command = await setup_task()
    receipt = await accept_task_command(**command)
    await hold(receipt['task_id'])
    async def unexpected(*a, **kw):
        raise AssertionError('A held cron must not start summary or resource discovery')
    monkeypatch.setattr('cron.executor._get_session_summary', unexpected)
    monkeypatch.setattr('cron.executor._create_temp_session', unexpected)
    result = await execute_cron_job({'id': 'held-test-cron', 'user_id': owner,
        'workspace_id': workspace, 'session_id': receipt['execution_session_id']})
    assert result == {'status': 'skipped', 'error': 'ASSISTANT_TASK_HELD'}


@pytest.mark.parametrize('boundary', ['before_loop', 'before_provider', 'after_provider'])
async def test_real_loop_hold_closes_claim_without_reissuing_work(monkeypatch, boundary):
    from agent import loop, processor
    from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
    args, receipt, lease, _ = await running()
    calls = []
    async def provider(**kwargs):
        calls.append(kwargs)
        assert len(calls) == 1
        if boundary == 'after_provider':
            yield {'type': 'text_delta', 'text': 'Persisted partial evidence'}
            await hold(args['task_id'])
            yield {'type': 'finish', 'reason': 'tool_calls', 'usage': {}}
        else:
            raise AssertionError('A held request reached the provider')
    _patch_real_loop_runtime(monkeypatch, config=_loop_config(), process_step=processor.process_step)
    monkeypatch.setattr(processor, 'stream_llm', provider)
    if boundary == 'before_loop':
        await hold(args['task_id'])
    elif boundary == 'before_provider':
        from session import agent_event_log
        original = agent_event_log.checkpoint_model_request
        async def checkpoint(*a, **kw):
            await hold(args['task_id'])
            return await original(*a, **kw)
        monkeypatch.setattr(agent_event_log, 'checkpoint_model_request', checkpoint)
    try:
        await loop.run_loop(lease.session_id, lease.user_id, lease=lease)
        assert len(calls) == (1 if boundary == 'after_provider' else 0)
        assert (await inbox.get_inbox_item(receipt['inbox_id'], user_id=lease.user_id)).state == 'settled'
        async with get_db_session() as db:
            state = await db.get(AgentDriverState, lease.session_id)
            assert state.phase == 'idle' and state.generation == lease.generation
            assert (await db.get(AssistantTask, args['task_id'])).desired_state == 'paused'
            assert await db.scalar(select(func.count()).select_from(TaskResult).where(TaskResult.task_id == args['task_id'])) == 1
        parity = await verify_agent_event_parity(lease.session_id, user_id=lease.user_id)
        assert parity.ok, parity.model_dump()
    finally:
        await lease.release(session_status='idle')


async def test_postgres_reservation_waits_for_committed_hold():
    owner, _, _, _, command = await setup_task()
    receipt = await accept_task_command(**command)
    async with get_db_session() as db:
        if db.get_bind().dialect.name != 'postgresql':
            pytest.skip('Independent transaction row-lock race requires PostgreSQL')
        task = await db.scalar(select(AssistantTask).where(AssistantTask.id == receipt['task_id']).with_for_update())
        reservation = asyncio.create_task(reserve_run(receipt['execution_session_id'], owner))
        try:
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(reservation), timeout=.1)
            task.desired_state = 'paused'
            task.control_revision += 1
            await db.commit()
            with pytest.raises(TaskSchedulingHeld):
                await reservation
        finally:
            if not reservation.done():
                reservation.cancel()
                await asyncio.gather(reservation, return_exceptions=True)
    assert (await inbox.get_inbox_item(receipt['inbox_id'], user_id=owner)).state == 'accepted'
