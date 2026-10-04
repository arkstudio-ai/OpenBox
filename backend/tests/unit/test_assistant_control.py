"""Task controls through real SQL, exact Driver races and the actual loop."""
import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from agent import inbox
from agent.driver import RecoveredDriver, reserve_recovered_run, reserve_run
from assistant.commands import accept_task_command
from assistant.control import accept_control_command, recover_controls
from assistant.policy import AssistantError
from assistant.scheduling import TaskSchedulingHeld
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask, TaskResult, TaskSubmission
from db.models.message import Message
from db.models.workspace import WorkspaceMember
from session.agent_event_log import verify_agent_event_parity
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_steering import running, terminal, view


def control(args, action='pause', **changes):
    return {**{key: args[key] for key in ('user_id', 'workspace_id', 'main_id', 'task_id',
        'expected_revision', 'expected_run')}, 'idempotency_key': action, 'action': action, **changes}


async def paused(monkeypatch, *, unclaimed_steer=False):
    from agent import loop, processor
    from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
    args, created, lease, batch = await running()
    if unclaimed_steer:
        accepted = await accept_task_command(**args)
        args['expected_revision'] = accepted['task_revision']
    _patch_real_loop_runtime(monkeypatch, config=_loop_config(), process_step=processor.process_step)
    async def no_suggestions(*args, **kwargs):
        return None
    monkeypatch.setattr('agent.suggestions.generate_suggestions', no_suggestions)
    receipt = await accept_control_command(**control(args))
    assert receipt['observed_state'] == 'pausing' and lease.abort.is_set()
    await loop.run_loop(lease.session_id, lease.user_id, lease=lease)
    state = await view(args)
    assert state['task']['observed_state'] == 'paused'
    assert state['latest_result']['outcome'] == 'aborted'
    return args, created, lease, batch, receipt, state


async def test_pause_receipt_replays_before_freshness_without_adding_input(monkeypatch):
    args, created, lease, _, receipt, state = await paused(monkeypatch)
    assert state['task']['intent_revision'] == 1
    assert await accept_control_command(**control(args)) == receipt
    with pytest.raises(AssistantError) as conflict:
        await accept_control_command(**control(args, action='cancel', idempotency_key='pause'))
    assert conflict.value.code == 'ASSISTANT_COMMAND_CONFLICT'
    async with get_db_session() as db:
        for model, where in ((TaskSubmission, TaskSubmission.task_id == created['task_id']),
                             (AgentInboxItem, AgentInboxItem.session_id == lease.session_id)):
            assert await db.scalar(select(func.count()).select_from(model).where(where)) == 1
        command = await db.get(AssistantCommand, receipt['command_id'])
        assert command.state == 'applied' and command.receipt == receipt
        member = await db.get(WorkspaceMember, (args['workspace_id'], args['user_id']))
        member.status = 'removed'
    with pytest.raises(AssistantError) as revoked:
        await accept_control_command(**control(args))
    assert revoked.value.status == 403


@pytest.mark.parametrize('changed', ['run', 'generation', 'missing', 'revision'])
async def test_stale_control_cannot_interrupt_observed_or_replacement_run(changed):
    args, _, lease, _ = await running()
    request = control(args)
    if changed == 'revision': request['expected_revision'] = 1
    elif changed == 'missing': request['expected_run'] = None
    else:
        request['expected_run'] = {**request['expected_run'],
            'run_id' if changed == 'run' else 'generation': 'stale' if changed == 'run' else lease.generation + 1}
    try:
        with pytest.raises(AssistantError):
            await accept_control_command(**request)
        assert not lease.abort.is_set()
        async with get_db_session() as db:
            assert (await db.get(AgentDriverState, lease.session_id)).abort_requested_at is None
            assert (await db.get(AssistantTask, args['task_id'])).desired_state == 'running'
    finally:
        await lease.release()


async def test_concurrent_controls_change_revision_once():
    args, _, lease, _ = await running()
    try:
        values = await asyncio.gather(accept_control_command(**control(args)),
            accept_control_command(**control(args, 'cancel')), return_exceptions=True)
        assert len([r for r in values if isinstance(r, dict)]) == 1
        assert [r.code for r in values if isinstance(r, AssistantError)] == ['ASSISTANT_REVISION_CONFLICT']
    finally:
        await lease.release()


async def test_expired_running_marker_is_still_pausing():
    args, _, lease, _ = await running()
    await lease.preserve_for_recovery()
    receipt = await accept_control_command(**control(args))
    assert receipt['observed_state'] == 'pausing'
    with pytest.raises(AssistantError) as stopping:
        await accept_control_command(**control(args, 'resume', expected_revision=receipt['task_revision']))
    assert stopping.value.code == 'ASSISTANT_STILL_STOPPING'


async def test_cancel_unclaimed_inputs_preserves_claimed_and_completed_evidence(monkeypatch):
    args, created, lease, batch = await running()
    queued = await accept_task_command(**{**args, 'delivery': 'followup', 'expected_run': None})
    request = control(args, 'cancel', expected_revision=queued['task_revision'])
    receipt = await accept_control_command(**request)
    assert receipt['observed_state'] == 'canceling'
    assert (await inbox.get_inbox_item(queued['inbox_id'], user_id=lease.user_id)).state == 'canceled'
    assert (await inbox.get_inbox_item(created['inbox_id'], user_id=lease.user_id)).state == 'claimed'
    await terminal(lease, batch.messages[0].id)
    await lease.release(session_status='idle')
    result = await view(args)
    assert result['task']['observed_state'] == 'canceled'
    assert result['latest_result']['outcome'] == 'succeeded'
    assert result['latest_submission']['disposition'] == 'canceled'
    assert await accept_control_command(**request) == receipt
    with pytest.raises(TaskSchedulingHeld):
        await reserve_run(lease.session_id, lease.user_id)
    assert (await verify_agent_event_parity(lease.session_id, user_id=lease.user_id)).ok


async def test_resume_outbox_survives_wake_loss_and_continues_original_turn(monkeypatch):
    from agent import loop, processor
    args, created, old_lease, batch, _, state = await paused(monkeypatch)
    request = control(args, 'resume', expected_revision=state['task']['control_revision'], expected_run=None)
    receipt = await accept_control_command(**request)
    assert receipt['observed_state'] == 'resuming'
    with pytest.raises(TaskSchedulingHeld):
        await reserve_run(old_lease.session_id, old_lease.user_id)
    # Acceptance committed without a wake. A fresh scanner claims the same
    # Command once; no transport or process-local signal is needed.
    _, leases = await recover_controls(task_id=created['task_id'], launch=False)
    assert len(leases) == 1
    lease = leases[0]
    assert (await recover_controls(task_id=created['task_id'], launch=False))[1] == []
    calls = []
    async def provider(**kwargs):
        calls.append(kwargs)
        assert any('Platform control' in s for s in kwargs['system'])
        yield {'type': 'text_delta', 'text': 'Original task continued'}
        yield {'type': 'finish', 'reason': 'stop', 'usage': {}}
    monkeypatch.setattr(processor, 'stream_llm', provider)
    try:
        await loop.run_loop(lease.session_id, lease.user_id, lease=lease)
        assert len(calls) == 1
        result = await view(args)
        assert result['task']['intent_revision'] == 1 and result['task']['observed_state'] == 'completed'
        assert result['latest_result']['generation'] > old_lease.generation
        async with get_db_session() as db:
            results = (await db.scalars(select(TaskResult).where(TaskResult.task_id == created['task_id']))).all()
            assert len(results) == 2
            assert all(r.consumed_inbox_ids == [created['inbox_id']] for r in results)
            assert await db.scalar(select(func.count()).select_from(Message).where(
                Message.session_id == lease.session_id, Message.role == 'user')) == 1
            assert await db.scalar(select(func.count()).select_from(TaskSubmission).where(
                TaskSubmission.task_id == created['task_id'])) == 1
        assert await accept_control_command(**request) == receipt
        parity = await verify_agent_event_parity(lease.session_id, user_id=lease.user_id)
        if not parity.projection_matches:
            from db.models.session import Session
            from session.agent_event_log import _surface_snapshot_locked, project_agent_events
            async with get_db_session() as db:
                events = (await db.scalars(select(AgentEvent).where(AgentEvent.session_id == lease.session_id)
                    .order_by(AgentEvent.sequence))).all()
                actual = await _surface_snapshot_locked(db, await db.get(Session, lease.session_id))
                import difflib, json
                assert project_agent_events(events) == actual, '\n'.join(difflib.unified_diff(
                    json.dumps(project_agent_events(events), indent=2).splitlines(), json.dumps(actual, indent=2).splitlines()))
        assert parity.ok, str(parity.model_dump())
    finally:
        await lease.release()


async def test_reserved_resume_recovery_ignores_the_old_aborted_answer(monkeypatch):
    from agent.recovery import _trigger_state
    args, created, _, _, _, state = await paused(monkeypatch)
    await accept_control_command(**control(args, 'resume', expected_revision=state['task']['control_revision'], expected_run=None))
    _, leases = await recover_controls(task_id=created['task_id'], launch=False)
    lease = leases[0]
    async with get_db_session() as db:
        driver = await db.get(AgentDriverState, lease.session_id)
        record = RecoveredDriver(session_id=lease.session_id, user_id=lease.user_id, run_id=lease.run_id,
            generation=lease.generation, phase='reserved', trigger_message_id=driver.trigger_message_id)
    await lease.preserve_for_recovery()
    assert (await _trigger_state(record))[:2] == (True, None)
    recovered = await reserve_recovered_run(record, initial_phase='reserved')
    try:
        async with get_db_session() as db:
            event = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == lease.session_id,
                AgentEvent.run_id == recovered.run_id, AgentEvent.kind == 'assistant.control.resumed'))
            assert event is not None and event.turn_id == record.trigger_message_id
    finally:
        await recovered.release()


async def test_resume_does_not_apply_expired_steering_or_leave_task_running(monkeypatch):
    from agent import loop, processor
    import json
    args, _, _, _, _, state = await paused(monkeypatch, unclaimed_steer=True)
    assert state['latest_submission']['disposition'] == 'not_applied'
    await accept_control_command(**control(args, 'resume', expected_revision=state['task']['control_revision'], expected_run=None))
    _, leases = await recover_controls(task_id=args['task_id'], launch=False)
    async def provider(**kwargs):
        assert args['prompt'] not in json.dumps(kwargs['messages'])
        yield {'type': 'text_delta', 'text': 'Original input only'}
        yield {'type': 'finish', 'reason': 'stop', 'usage': {}}
    monkeypatch.setattr(processor, 'stream_llm', provider)
    await loop.run_loop(leases[0].session_id, leases[0].user_id, lease=leases[0])
    current = await view(args)
    assert current['task']['observed_state'] == 'input_not_applied'
    assert current['latest_submission']['disposition'] == 'not_applied'
    assert current['latest_result']['outcome'] == 'succeeded'
    assert current['latest_result']['observed_intent_revision'] == 1 < current['task']['intent_revision']


async def test_pause_and_resume_never_rerun_already_completed_task():
    args, _, lease, batch = await running()
    await terminal(lease, batch.messages[0].id)
    await lease.release()
    state = await view(args)
    pause = await accept_control_command(**control(args, expected_revision=state['task']['control_revision'], expected_run=None))
    assert pause['observed_state'] == 'paused'
    resume = await accept_control_command(**control(args, 'resume', expected_revision=pause['task_revision'], expected_run=None))
    assert resume['observed_state'] == 'completed'
    assert (await recover_controls(task_id=args['task_id'], launch=False))[1] == []


@pytest.mark.parametrize('metadata', [
    {'outcome_unknown': True}, {'recovery_code': 'tool_outcome_unknown'}, {'execution_outcome': 'unknown'},
])
async def test_unknown_tool_outcome_blocks_resume_without_new_command(monkeypatch, metadata):
    from db.models.part import Part
    from core.identifier import generate_id
    args, _, lease, _, _, state = await paused(monkeypatch)
    async with get_db_session() as db:
        db.add(Part(id=generate_id(), session_id=lease.session_id, user_id=lease.user_id,
            message_id=state['latest_result']['result_message_id'], type='tool',
            data={'type': 'tool', 'status': 'error', 'metadata': metadata}, created_at=datetime.now(timezone.utc)))
    await recover_controls(task_id=args['task_id'], launch=False)
    with pytest.raises(AssistantError) as blocked:
        await accept_control_command(**control(args, 'resume', expected_revision=state['task']['control_revision'], expected_run=None))
    assert blocked.value.code == 'ASSISTANT_EFFECT_UNRESOLVED'
    assert (await view(args))['task']['desired_state'] == 'paused'


async def test_pause_and_abort_request_roll_back_together(monkeypatch):
    import assistant.control as service
    args, _, lease, _ = await running()
    original = service.append_agent_event_locked
    async def fail(*a, **kw):
        await original(*a, **kw)
        raise RuntimeError('before commit')
    monkeypatch.setattr(service, 'append_agent_event_locked', fail)
    try:
        with pytest.raises(RuntimeError, match='before commit'):
            await accept_control_command(**control(args))
        async with get_db_session() as db:
            assert (await db.get(AssistantTask, args['task_id'])).desired_state == 'running'
            assert (await db.get(AgentDriverState, lease.session_id)).abort_requested_at is None
            assert not await db.scalar(select(AssistantCommand.id).where(AssistantCommand.target_id == args['task_id'],
                AssistantCommand.action == 'task_pause'))
        assert not lease.abort.is_set()
    finally:
        monkeypatch.setattr(service, 'append_agent_event_locked', original)
        await lease.release()


async def test_cancel_reconciles_descendant_runs_and_unclaimed_inputs():
    from session.session import create_session
    args, _, lease, batch = await running()
    child = await create_session(user_id=lease.user_id, workspace_id=args['workspace_id'], parent_id=lease.session_id)
    queued = await inbox.accept_inbox_item(session_id=child.id, user_id=lease.user_id, delivery='followup',
        prompt='Unclaimed child input', origin='human', origin_ref={'actor_user_id': lease.user_id})
    child_lease = await reserve_run(child.id, lease.user_id)
    try:
        receipt = await accept_control_command(**control(args, 'cancel'))
        assert child_lease.abort.is_set() and receipt['observed_state'] == 'canceling'
        await terminal(lease, batch.messages[0].id)
        await lease.release()
        assert (await view(args))['task']['observed_state'] == 'canceling'
        await child_lease.release()
        await recover_controls(task_id=args['task_id'], launch=False)
        assert (await inbox.get_inbox_item(queued.id, user_id=lease.user_id)).state == 'canceled'
        assert (await view(args))['task']['observed_state'] == 'canceled'
        with pytest.raises(TaskSchedulingHeld):
            await reserve_run(child.id, lease.user_id)
    finally:
        await lease.release()
        await child_lease.release()


async def test_http_control_receipt_and_conflict_are_actor_bound(monkeypatch):
    from tests.unit.test_assistant_api import client_for
    args, _, lease, _ = await running()
    async def no_wake(**kwargs):
        return 0, []
    monkeypatch.setattr('assistant.control.recover_controls', no_wake)
    body = {'idempotency_key': 'http-pause', 'action': 'pause', 'expected_revision': 2, 'expected_run': args['expected_run']}
    try:
        async with client_for(args['user_id'], args['workspace_id'], monkeypatch) as client:
            path = f"/api/assistant/tasks/{args['task_id']}/commands"
            first = await client.post(path, json=body)
            assert first.status_code == 202 and 'inbox_id' not in first.json()
            assert (await client.post(path, json=body)).json() == first.json()
            invalid = await client.post(path, json={**body, 'input': {'text': 'Not a control', 'delivery': 'followup'}})
            assert invalid.status_code == 422
            stale = await client.post(path, json={**body, 'idempotency_key': 'other-device', 'action': 'cancel'})
            assert stale.status_code == 409
            assert stale.json()['detail']['current_task']['desired_state'] == 'paused'
            command = await client.get('/api/assistant/commands/' + first.json()['command_id'])
            assert command.json()['receipt'] == first.json() and command.json()['state'] == 'accepted'
    finally:
        await lease.release()


async def test_held_legacy_rest_ws_and_planning_do_not_mutate_inputs(monkeypatch):
    from api import sessions, ws
    from fastapi import BackgroundTasks
    from command.command import CommandInfo
    args, _, lease, _ = await running()
    await accept_control_command(**control(args))
    actor = {'user_id': args['user_id'], 'workspace_id': args['workspace_id']}
    async def template(*args): return CommandInfo('test', 'test', 'build', 'test')
    monkeypatch.setattr('command.command.get_command', template)
    async def resolve(*args): return 'No new input'
    monkeypatch.setattr('command.command.execute_command', resolve)
    operations = [
        lambda: sessions.abort_session(lease.session_id, actor),
        lambda: sessions.accept_plan(lease.session_id, actor),
        lambda: sessions.reject_plan(lease.session_id, actor),
        lambda: sessions.summarize_session(lease.session_id, BackgroundTasks(), actor),
        lambda: sessions.execute_command(lease.session_id, sessions.CommandBody(command='test'), actor),
        lambda: ws._handle_client_message(lease.user_id, 'user', {'type': 'session.abort', 'sessionId': lease.session_id}),
    ]
    try:
        for operation in operations:
            with pytest.raises(TaskSchedulingHeld):
                await operation()
        async with get_db_session() as db:
            assert await db.scalar(select(func.count()).select_from(Message).where(Message.session_id == lease.session_id)) == 1
            assert await db.scalar(select(func.count()).select_from(AgentInboxItem).where(AgentInboxItem.session_id == lease.session_id)) == 1
    finally:
        await lease.release()


async def test_resume_claim_and_command_application_roll_back_atomically(monkeypatch):
    import assistant.control as controls
    args, created, _, _, _, state = await paused(monkeypatch)
    receipt = await accept_control_command(**control(args, 'resume', expected_revision=state['task']['control_revision'], expected_run=None))
    original = controls._bind_resume_locked
    async def fail(*a, **kw):
        await original(*a, **kw)
        raise RuntimeError('before driver commit')
    monkeypatch.setattr(controls, '_bind_resume_locked', fail)
    with pytest.raises(RuntimeError, match='before driver commit'):
        await reserve_run(created['execution_session_id'], args['user_id'], assistant_resume_command_id=receipt['command_id'])
    async with get_db_session() as db:
        assert (await db.get(AgentDriverState, created['execution_session_id'])).phase == 'idle'
        assert (await db.get(AssistantCommand, receipt['command_id'])).state == 'accepted'
        assert not await db.scalar(select(AgentEvent.id).where(AgentEvent.session_id == created['execution_session_id'],
            AgentEvent.kind == 'assistant.control.resumed'))
    monkeypatch.setattr(controls, '_bind_resume_locked', original)
    # Two independent workers racing the same accepted outbox own one run.
    values = await asyncio.gather(*[recover_controls(task_id=args['task_id'], launch=False) for _ in range(2)])
    leases = [lease for _, group in values for lease in group]
    assert len(leases) == 1
    await leases[0].release()


async def test_revoked_resume_is_blocked_without_dispatch_and_requires_explicit_retry(monkeypatch):
    args, created, _, _, _, state = await paused(monkeypatch)
    receipt = await accept_control_command(**control(args, 'resume', expected_revision=state['task']['control_revision'], expected_run=None))
    async with get_db_session() as db:
        (await db.get(WorkspaceMember, (args['workspace_id'], args['user_id']))).status = 'removed'
    assert (await recover_controls(task_id=args['task_id'], launch=False))[1] == []
    async with get_db_session() as db:
        command = await db.get(AssistantCommand, receipt['command_id'])
        task = await db.get(AssistantTask, args['task_id'])
        assert command.state == 'blocked' and command.receipt == receipt
        assert task.desired_state == 'paused' and task.observed_state == 'resume_blocked'
        assert (await db.get(AgentDriverState, created['execution_session_id'])).phase == 'idle'
        (await db.get(WorkspaceMember, (args['workspace_id'], args['user_id']))).status = 'active'
    current = await view(args)
    assert current['latest_control']['state'] == 'blocked'
    assert (await recover_controls(task_id=args['task_id'], launch=False))[1] == []


async def test_control_tool_requires_original_human_and_persisted_exact_call():
    from assistant.commands import ToolSource
    from models.message import ToolPartData, ToolStatus
    from session.session import create_assistant_message, save_part
    args, _, lease, _ = await running()
    main_id, owner = args['main_id'], args['user_id']
    await inbox.accept_inbox_item(session_id=main_id, user_id=owner, delivery='followup', prompt='Pause this task',
        origin='human', origin_ref={'actor_user_id': owner})
    main_lease = await reserve_run(main_id, owner)
    try:
        batch = await inbox.claim_inbox_boundary(main_lease, step=1, include_next_turn=True)
        fence = (main_id, main_lease.run_id, main_lease.generation)
        message = await create_assistant_message(main_id, batch.messages[0].id, agent='assistant',
            model_id='test/model', user_id=owner, run_fence=fence)
        from tests.unit.assistant_source_fixtures import consume_lease_context
        await consume_lease_context(main_lease, message)
        part = ToolPartData(tool='tasks.pause', canonical_tool_id='tasks.pause', call_id='control-call',
            wire_tool_name='tasks_pause', provider_binding_digest='b'*64, provider_dialect='openai', stream_seq=0,
            status=ToolStatus.RUNNING, input={}, session_id=main_id, message_id=message.id)
        await save_part(part, is_new=True, user_id=owner, run_fence=fence)
        source = ToolSource(part.id, main_lease.run_id, main_lease.generation, (batch.messages[0].id,))
        with pytest.raises(AssistantError) as forged:
            await accept_control_command(**control(args), source=ToolSource('missing', source.run_id, source.generation, source.source_message_ids))
        assert forged.value.code == 'ASSISTANT_CALL_UNVERIFIED'
        with pytest.raises(AssistantError) as fabricated_human:
            await accept_control_command(**control(args), source=ToolSource(part.id, source.run_id, source.generation, (message.id,)))
        assert fabricated_human.value.code == 'ASSISTANT_SOURCE_UNVERIFIED'
        receipt = await accept_control_command(**control(args), source=source)
        assert receipt['observed_state'] == 'pausing'
        assert await accept_control_command(**control(args, idempotency_key='model-key-ignored'), source=source) == receipt
    finally:
        await main_lease.release()
        await lease.release()


async def test_report_only_runtime_cannot_offer_any_task_control():
    from assistant.reporting import REPORT_TOOLS
    from assistant.runtime import authorize_assistant_tool
    from tests.unit.test_assistant_results import result_ready
    from assistant.results import deliver_task_result
    from tool.tool import ToolContext
    assert not {'tasks.pause', 'tasks.resume', 'tasks.cancel'} & REPORT_TOOLS
    owner, workspace, main, task, execution_lease, _ = await result_ready()
    await execution_lease.release()
    async with get_db_session() as db:
        result = await db.scalar(select(TaskResult).where(TaskResult.task_id == task['task_id']))
    await deliver_task_result(result.id)
    lease = await reserve_run(main.id, owner)
    try:
        await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        ctx = ToolContext(session_id=main.id, user_id=owner, workspace_id=workspace,
            run_id=lease.run_id, run_generation=lease.generation)
        for operation in ('tasks.pause', 'tasks.resume', 'tasks.cancel'):
            with pytest.raises(AssistantError) as forbidden:
                await authorize_assistant_tool(ctx, operation, {'task_id': task['task_id']})
            assert forbidden.value.code == 'ASSISTANT_TOOL_FORBIDDEN'
    finally:
        await lease.release()
