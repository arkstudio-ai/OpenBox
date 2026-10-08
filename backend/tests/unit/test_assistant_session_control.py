"""Execution-page Stop and old mutations, using actual HTTP/SQL Task state."""
import asyncio

import httpx
import pytest
from fastapi import BackgroundTasks, FastAPI, HTTPException
from sqlalchemy import func, select

from agent import inbox, loop, processor
from agent.driver import reserve_run
from api import sessions, ws
from assistant.commands import accept_task_command
from assistant.control import accept_control_command
from assistant.policy import AssistantError
from assistant.scheduling import TaskSchedulingHeld
from assistant.session_control import StopBody, stop_task, target_for_session
from db.base import close_engine, get_db_session, get_engine, init_engine
from db.models.agent_driver import AgentDriverState
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask, TaskResult
from db.models.message import Message
from db.models.question import QuestionCheckpoint
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from session.agent_event_log import verify_agent_event_parity
from session.session import create_session, get_session
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_commands import setup_task
from tests.unit.test_assistant_control import control
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_steering import running, view


def client_for(args):
    app = FastAPI()
    app.include_router(sessions.router, prefix='/api/agent')
    actor = {'user_id': args['user_id'], 'workspace_id': args['workspace_id']}
    app.dependency_overrides[sessions.get_current_user] = lambda: actor
    app.dependency_overrides[sessions.get_workspace] = lambda: {'id': args['workspace_id']}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://session.test')


async def target_body(lease, key='execution-stop'):
    session = await get_session(lease.session_id, user_id=lease.user_id)
    target = await target_for_session(session, lease.user_id)
    return {'task_control': {k: target[k] for k in ('task_id', 'expected_revision', 'expected_run')} | {'idempotency_key': key}}


async def test_http_and_ws_stop_share_one_command_and_settle_original_task(monkeypatch):
    args, created, lease, batch = await running()
    followup = await accept_task_command(**{**args, 'delivery': 'followup', 'expected_run': None})
    body = await target_body(lease)
    _patch_real_loop_runtime(monkeypatch, config=_loop_config(), process_step=processor.process_step)
    async def no_suggestions(*args, **kwargs): return None
    monkeypatch.setattr('agent.suggestions.generate_suggestions', no_suggestions)
    async with client_for(args) as client:
        detail = await client.get(f'/api/agent/session/{lease.session_id}')
        assert detail.status_code == 200
        assert detail.json()['assistant_managed'] is True
        assert 'memory_policy' not in detail.json()
        assert detail.json()['task_control']['expected_run'] == args['expected_run']
        path = f'/api/agent/session/{lease.session_id}/abort'
        first, second, _ = await asyncio.gather(client.post(path, json=body), client.post(path, json=body),
            ws._handle_client_message(lease.user_id, 'user', {'type': 'session.abort',
                'sessionId': lease.session_id, 'taskControl': body['task_control']}))
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        receipt = first.json()['task_control']
        assert receipt['action'] == 'cancel' and receipt['observed_state'] == 'canceling'
        assert lease.abort.is_set()
        assert (await inbox.get_inbox_item(followup['inbox_id'], user_id=lease.user_id)).state == 'canceled'
        await loop.run_loop(lease.session_id, lease.user_id, lease=lease)
        current = await view(args)
        assert current['task']['desired_state'] == current['task']['observed_state'] == 'canceled'
        assert current['latest_result']['outcome'] == 'aborted'
        async with get_db_session() as db:
            result = await db.get(TaskResult, current['latest_result']['result_id'])
            assert result.task_id == created['task_id'] and result.consumed_inbox_ids == [created['inbox_id']]
            users = (await db.scalars(select(Message).where(Message.session_id == lease.session_id, Message.role == 'user'))).all()
            assert [m.id for m in users] == [batch.messages[0].id]
            assert await db.scalar(select(func.count()).select_from(AssistantCommand).where(
                AssistantCommand.target_id == created['task_id'], AssistantCommand.action == 'task_cancel')) == 1
        # Recovery of the response does not manufacture another stop marker or input.
        url = get_engine().url.render_as_string(hide_password=False)
        await close_engine()
        init_engine(url)
        assert (await client.post(path, json=body)).json() == first.json()
    assert (await verify_agent_event_parity(lease.session_id, user_id=lease.user_id)).ok


@pytest.mark.parametrize('change', ['revision', 'run', 'generation', 'task', 'missing'])
async def test_late_or_unbound_stop_cannot_interrupt_newer_work(change):
    args, _, lease, _ = await running()
    body = await target_body(lease)
    target = body['task_control']
    if change == 'revision': target['expected_revision'] -= 1
    elif change == 'run': target['expected_run']['run_id'] = 'old-run'
    elif change == 'generation': target['expected_run']['generation'] += 1
    elif change == 'task': target['task_id'] = 'another-task'
    else: body = {}
    try:
        async with client_for(args) as client:
            response = await client.post(f'/api/agent/session/{lease.session_id}/abort', json=body)
            assert response.status_code == 409, response.text
        assert not lease.abort.is_set()
        async with get_db_session() as db:
            assert (await db.get(AgentDriverState, lease.session_id)).abort_requested_at is None
            assert (await db.get(AssistantTask, args['task_id'])).desired_state == 'running'
            assert await db.scalar(select(func.count()).select_from(AgentInboxItem).where(
                AgentInboxItem.session_id == lease.session_id)) == 1
    finally:
        await lease.release()


async def test_paused_task_can_be_canceled_through_bound_execution_stop():
    args, _, lease, _ = await running()
    await accept_control_command(**control(args))
    session = await get_session(lease.session_id, user_id=lease.user_id)
    body = StopBody.model_validate(await target_body(lease))
    try:
        receipt = await stop_task(session, lease.user_id, body)
        assert receipt['desired_state'] == 'canceled'
    finally:
        await lease.release()


async def test_queued_stop_prevents_admission_and_preserves_input_receipt():
    owner, _, workspace, _, create = await setup_task()
    created = await accept_task_command(**create)
    session = await get_session(created['execution_session_id'], user_id=owner)
    target = await target_for_session(session, owner)
    assert target['expected_run'] is None
    body = StopBody.model_validate({'task_control': {k: target[k] for k in (
        'task_id', 'expected_revision', 'expected_run')} | {'idempotency_key': 'queued-stop'}})
    first, second = await asyncio.gather(stop_task(session, owner, body), stop_task(session, owner, body))
    assert first == second and first['observed_state'] == 'canceled'
    assert (await inbox.get_inbox_item(created['inbox_id'], user_id=owner)).state == 'canceled'
    with pytest.raises(TaskSchedulingHeld): await reserve_run(session.id, owner)


@pytest.mark.parametrize('paused_first', [False, True])
async def test_cancel_suspended_question_closes_original_turn_without_new_run(paused_first):
    from assistant.requests import list_requests
    from tests.unit.test_assistant_requests import pending
    scope, created, question, _ = await pending(settle_inbox=True)
    session = await get_session(question.session_id, user_id=scope['user_id'])
    async def body(key):
        target = await target_for_session(session, scope['user_id'])
        return {'task_control': {k: target[k] for k in ('task_id', 'expected_revision', 'expected_run')}
            | {'idempotency_key': key}}
    if paused_first:
        target = (await body('pause'))['task_control']
        await accept_control_command(**scope, **{k: target[k] for k in (
            'task_id', 'expected_revision', 'expected_run', 'idempotency_key')}, action='pause')
        async with get_db_session() as db:
            assert (await db.get(QuestionCheckpoint, question.id)).status == 'pending'
    request = await body('stop-waiting')
    async with client_for(scope) as client:
        path = f'/api/agent/session/{question.session_id}/abort'
        first = await client.post(path, json=request)
        assert first.status_code == 200, first.text
        assert first.json()['task_control']['observed_state'] == 'canceled'
        url = get_engine().url.render_as_string(hide_password=False)
        await close_engine()
        init_engine(url)
        assert (await client.post(path, json=request)).json() == first.json()
    assert (await list_requests(**scope))['items'] == []
    async with get_db_session() as db:
        task = await db.get(AssistantTask, created['task_id'])
        result = await db.get(TaskResult, task.latest_result_id)
        assert result.outcome == 'aborted' and result.consumed_inbox_ids == [created['inbox_id']]
        assert result.run_id == question.assistant['run_id'] and result.generation == question.assistant['generation']
        assert (await db.get(QuestionCheckpoint, question.id)).status == 'cancelled'
        assert (await db.get(Session, question.session_id)).status == 'idle'
        driver = await db.get(AgentDriverState, question.session_id)
        assert driver.phase == 'idle' and driver.generation == question.assistant['generation']
        assert await db.scalar(select(func.count()).select_from(TaskResult).where(TaskResult.task_id == task.id)) == 1
        assert await db.scalar(select(func.count()).select_from(Message).where(
            Message.session_id == question.session_id, Message.role == 'user')) == 1
    assert (await verify_agent_event_parity(question.session_id, user_id=scope['user_id'])).ok


async def test_suspended_question_stop_rolls_back_control_and_terminal_together(monkeypatch):
    from tests.unit.test_assistant_requests import pending
    scope, created, question, _ = await pending()
    session = await get_session(question.session_id, user_id=scope['user_id'])
    target = await target_for_session(session, scope['user_id'])
    body = StopBody.model_validate({'task_control': {k: target[k] for k in (
        'task_id', 'expected_revision', 'expected_run')} | {'idempotency_key': 'rollback-waiting'}})
    async def failed_settlement(*args, **kwargs): raise RuntimeError('terminal write failed')
    with monkeypatch.context() as patch:
        patch.setattr('assistant.results.record_execution_result_locked', failed_settlement)
        with pytest.raises(RuntimeError, match='terminal write failed'):
            await stop_task(session, scope['user_id'], body)
    async with get_db_session() as db:
        assert (await db.get(AssistantTask, created['task_id'])).desired_state == 'running'
        assert (await db.get(QuestionCheckpoint, question.id)).status == 'pending'
        assert (await db.get(Session, question.session_id)).status == 'waiting_input'
        assert await db.scalar(select(func.count()).select_from(Message).where(
            Message.session_id == question.session_id, Message.role == 'assistant')) == 1
        assert await db.scalar(select(func.count()).select_from(AssistantCommand).where(
            AssistantCommand.target_id == created['task_id'], AssistantCommand.action == 'task_cancel')) == 0
    assert (await stop_task(session, scope['user_id'], body))['observed_state'] == 'canceled'


async def test_recovery_closes_previously_canceled_suspended_task_once():
    from assistant.control import recover_controls
    from tests.unit.test_assistant_requests import pending
    scope, created, question, _ = await pending(settle_inbox=True)
    async with get_db_session() as db:
        task = await db.get(AssistantTask, created['task_id'])
        task.desired_state = task.observed_state = 'canceled'
    await recover_controls(task_id=created['task_id'], launch=False)
    await recover_controls(task_id=created['task_id'], launch=False)
    async with get_db_session() as db:
        assert (await db.get(QuestionCheckpoint, question.id)).status == 'cancelled'
        assert (await db.get(Session, question.session_id)).status == 'idle'
        assert await db.scalar(select(func.count()).select_from(TaskResult).where(
            TaskResult.task_id == created['task_id'])) == 1
    assert (await verify_agent_event_parity(question.session_id, user_id=scope['user_id'])).ok


async def test_suspended_stop_cannot_assign_old_question_to_a_new_generation():
    from tests.unit.test_assistant_requests import pending
    scope, created, question, _ = await pending(settle_inbox=True)
    async with get_db_session() as db:
        (await db.get(AgentDriverState, question.session_id)).generation += 1
    session = await get_session(question.session_id, user_id=scope['user_id'])
    target = await target_for_session(session, scope['user_id'])
    body = StopBody.model_validate({'task_control': {k: target[k] for k in (
        'task_id', 'expected_revision', 'expected_run')} | {'idempotency_key': 'stale-question'}})
    with pytest.raises(AssistantError): await stop_task(session, scope['user_id'], body)
    async with get_db_session() as db:
        assert (await db.get(AssistantTask, created['task_id'])).desired_state == 'running'
        assert (await db.get(QuestionCheckpoint, question.id)).status == 'pending'
        assert (await db.get(AgentDriverState, question.session_id)).generation == question.assistant['generation'] + 1


async def test_child_and_revoked_authority_cannot_target_parent_control():
    args, _, lease, _ = await running()
    body = StopBody.model_validate(await target_body(lease))
    try:
        child = await create_session(user_id=lease.user_id, workspace_id=args['workspace_id'], parent_id=lease.session_id)
        assert await target_for_session(child, lease.user_id) is None
        async with client_for(args) as client:
            child_detail = await client.get(f'/api/agent/session/{child.id}')
            assert child_detail.status_code == 200
            assert child_detail.json()['assistant_managed'] is True
            assert child_detail.json()['task_control'] is None
            ordinary = await create_session(user_id=lease.user_id, workspace_id=args['workspace_id'])
            ordinary_detail = await client.get(f'/api/agent/session/{ordinary.id}')
            assert ordinary_detail.status_code == 200
            assert ordinary_detail.json()['assistant_managed'] is False
            assert 'task_control' not in ordinary_detail.json()
        with pytest.raises(AssistantError): await stop_task(child, lease.user_id, body)
        assert not lease.abort.is_set()
        session = await get_session(lease.session_id, user_id=lease.user_id)
        async with get_db_session() as db:
            (await db.get(WorkspaceMember, (args['workspace_id'], lease.user_id))).status = 'removed'
        with pytest.raises(AssistantError): await stop_task(session, lease.user_id, body)
        with pytest.raises(AssistantError): await target_for_session(session, lease.user_id)
        assert not lease.abort.is_set()
    finally:
        await lease.release()


@pytest.mark.parametrize('operation', ['command', 'summarize', 'todo_add', 'todo_remove', 'plan_read'])
async def test_legacy_mutations_stop_before_rewriting_managed_task_evidence(operation, monkeypatch):
    args, _, lease, _ = await running()
    actor = {'user_id': lease.user_id, 'workspace_id': args['workspace_id']}
    async def no_side_effect(*args, **kwargs): raise AssertionError('Legacy side effect must not run')
    for path in ['command.command.get_command', 'sandbox.sandbox_manager.get_client',
                 'session.todo.add_todo_item', 'session.todo.remove_todo_item', 'agent.compaction.create_compaction']:
        monkeypatch.setattr(path, no_side_effect)
    actions = {
        'command': lambda: sessions.execute_command(lease.session_id, sessions.CommandBody(command='old'), actor),
        'summarize': lambda: sessions.summarize_session(lease.session_id, BackgroundTasks(), actor),
        'todo_add': lambda: sessions.add_todo_item(lease.session_id, sessions.TodoAddBody(subject='new'), actor),
        'todo_remove': lambda: sessions.remove_todo_item(lease.session_id, 'old', actor),
        'plan_read': lambda: sessions.get_plan(lease.session_id, actor),
    }
    try:
        with pytest.raises(HTTPException) as blocked: await actions[operation]()
        assert blocked.value.status_code == 409
        assert not lease.abort.is_set()
    finally:
        await lease.release()
