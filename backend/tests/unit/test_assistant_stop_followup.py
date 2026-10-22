"""A fresh execution-page input after Stop is a new intent, never a replay."""
import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from agent import inbox, loop, processor
from agent.driver import reserve_run
from assistant.commands import accept_task_command
from assistant.control import accept_control_command, recover_controls
from assistant.policy import AssistantError
from core.identifier import generate_id
from db.base import close_engine, get_db_session, get_engine, init_engine
from db.models.agent_driver import AgentDriverState
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask
from db.models.part import Part
from session.agent_event_log import verify_agent_event_parity
from session.session import create_session
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_control import control
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_session_control import client_for, target_body
from tests.unit.test_assistant_steering import running, terminal, view


def isolated_runtime(monkeypatch):
    _patch_real_loop_runtime(monkeypatch, config=_loop_config(), process_step=processor.process_step)

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr('agent.suggestions.generate_suggestions', noop)
    monkeypatch.setattr('agent.inbox.wake_inbox_session', noop)


async def stopped(monkeypatch):
    isolated_runtime(monkeypatch)
    args, created, lease, batch = await running()
    body = await target_body(lease)
    async with client_for(args) as client:
        response = await client.post(f'/api/agent/session/{lease.session_id}/abort', json=body)
        assert response.status_code == 200, response.text
    await loop.run_loop(lease.session_id, lease.user_id, lease=lease)
    assert (await view(args))['task']['observed_state'] == 'canceled'
    return args, created, lease, batch, body


async def test_stop_refresh_and_send_executes_only_new_input_once(monkeypatch):
    isolated_runtime(monkeypatch)
    args, created, lease, _ = await running()
    queued = await accept_task_command(**{**args, 'delivery': 'followup', 'expected_run': None})
    stop = await target_body(lease)
    path = f'/api/agent/session/{lease.session_id}'
    async with client_for(args) as client:
        old_stop = await client.post(path + '/abort', json=stop)
        assert old_stop.status_code == 200
        await loop.run_loop(lease.session_id, lease.user_id, lease=lease)
        # No local request memory is needed, including for sessions canceled
        # by older versions (the production incident has this exact state).
        url = get_engine().url.render_as_string(hide_password=False)
        await close_engine()
        init_engine(url)
        refreshed = (await client.get(path)).json()
        assert refreshed['status'] == 'idle'
        assert refreshed['task_control']['desired_state'] == 'canceled'
        before = (await view(args))['task']
        payload = {'text': 'hello after stopping', 'client_message_id': 'fresh-human-message'}
        first, duplicate = await asyncio.gather(*[
            client.post(path + '/prompt_async', json=payload) for _ in range(2)])
        assert first.status_code == duplicate.status_code == 200, first.text
        assert first.json() == duplicate.json()
        current = (await view(args))['task']
        assert current['desired_state'] == 'running'
        assert current['intent_revision'] == before['intent_revision'] + 1
        assert current['control_revision'] == before['control_revision'] + 1
        assert (await inbox.get_inbox_item(queued['inbox_id'], user_id=lease.user_id)).state == 'canceled'
        assert (await inbox.get_inbox_item(created['inbox_id'], user_id=lease.user_id)).outcome == 'aborted'
        # Old stop retry returns its old receipt without affecting the new run;
        # the same old target with a new key must fail its revision fence.
        new_lease = await reserve_run(lease.session_id, lease.user_id)
        try:
            assert (await client.post(path + '/abort', json=stop)).json() == old_stop.json()
            stale = {'task_control': {**stop['task_control'], 'idempotency_key': 'late-old-target'}}
            assert (await client.post(path + '/abort', json=stale)).status_code == 409
            assert not new_lease.abort.is_set()
            batch = await inbox.claim_inbox_boundary(new_lease, step=1, include_next_turn=True)
            assert [row.id for row in batch.receipts] == [first.json()['inboxId']]
            await new_lease.set_phase('running')
            await terminal(new_lease, batch.messages[0].id)
        finally:
            await new_lease.release(session_status='idle')
        replay = await client.post(path + '/prompt_async', json=payload)
        assert replay.status_code == 200 and replay.json()['state'] == 'settled'
        assert replay.json()['inboxId'] == first.json()['inboxId']
        conflict = await client.post(path + '/prompt_async', json={**payload, 'text': 'different input'})
        assert conflict.status_code == 409
        # A delayed retry of an already accepted message cannot undo a later Stop.
        again = await target_body(new_lease, 'stop-new-turn')
        assert (await client.post(path + '/abort', json=again)).status_code == 200
        assert (await client.post(path + '/prompt_async', json=payload)).json()['state'] == 'settled'
        assert (await view(args))['task']['desired_state'] == 'canceled'
        async with get_db_session() as db:
            assert await db.scalar(select(func.count()).select_from(AgentInboxItem).where(
                AgentInboxItem.session_id == lease.session_id, AgentInboxItem.prompt == payload['text'])) == 1
            item = await db.get(AgentInboxItem, first.json()['inboxId'])
            assert item.origin == 'human' and item.origin_ref['reopened_from']['desired_state'] == 'canceled'
            assert not await db.scalar(select(AssistantCommand.id).where(
                AssistantCommand.target_id == args['task_id'], AssistantCommand.action == 'task_resume'))
    assert (await verify_agent_event_parity(lease.session_id, user_id=lease.user_id)).ok


async def test_new_input_cannot_reopen_before_old_run_finishes(monkeypatch):
    isolated_runtime(monkeypatch)
    args, _, lease, _ = await running()
    path = f'/api/agent/session/{lease.session_id}'
    try:
        async with client_for(args) as client:
            assert (await client.post(path + '/abort', json=await target_body(lease))).status_code == 200
            response = await client.post(path + '/prompt_async', json={
                'text': 'hello', 'client_message_id': 'before-quiescence'})
            assert response.status_code == 409
            assert response.json()['detail']['code'] == 'ASSISTANT_STILL_STOPPING'
        async with get_db_session() as db:
            assert (await db.get(AssistantTask, args['task_id'])).desired_state == 'canceled'
            assert (await db.get(AgentDriverState, lease.session_id)).abort_requested_at is not None
            assert not await db.scalar(select(AgentInboxItem.id).where(AgentInboxItem.prompt == 'hello'))
    finally:
        await loop.run_loop(lease.session_id, lease.user_id, lease=lease)


async def test_idle_root_cannot_reopen_while_descendant_or_its_old_queue_remains(monkeypatch):
    isolated_runtime(monkeypatch)
    args, _, lease, _ = await running()
    child = await create_session(user_id=lease.user_id, workspace_id=args['workspace_id'], parent_id=lease.session_id)
    old_input = await inbox.accept_inbox_item(session_id=child.id, user_id=lease.user_id, delivery='followup',
        prompt='old child work', origin='human', origin_ref={'actor_user_id': lease.user_id})
    child_lease = await reserve_run(child.id, lease.user_id)
    path = f'/api/agent/session/{lease.session_id}'
    payload = {'text': 'hello', 'client_message_id': 'after-child-stop'}
    try:
        async with client_for(args) as client:
            assert (await client.post(path + '/abort', json=await target_body(lease))).status_code == 200
            await loop.run_loop(lease.session_id, lease.user_id, lease=lease)
            assert (await client.get(path)).json()['status'] == 'idle'
            for release_child in (False, True):
                if release_child:
                    await child_lease.release(session_status='idle')
                response = await client.post(path + '/prompt_async', json=payload)
                assert response.status_code == 409
                assert response.json()['detail']['code'] == 'ASSISTANT_STILL_STOPPING'
            await recover_controls(task_id=args['task_id'], launch=False)
            assert (await inbox.get_inbox_item(old_input.id, user_id=lease.user_id)).state == 'canceled'
            assert (await client.post(path + '/prompt_async', json=payload)).status_code == 200
    finally:
        await child_lease.release()
        await lease.release()


@pytest.mark.parametrize('suspended', [False, True])
async def test_stop_before_claim_or_at_question_allows_only_fresh_input(monkeypatch, suspended):
    isolated_runtime(monkeypatch)
    if suspended:
        from tests.unit.test_assistant_requests import pending
        scope, created, question, _ = await pending()
    else:
        from tests.unit.test_assistant_commands import setup_task
        owner, _, workspace, main, create = await setup_task()
        scope = dict(user_id=owner, workspace_id=workspace, main_id=main.id)
        created = await accept_task_command(**create)
    from assistant.session_control import target_for_session
    from session.session import get_session
    session = await get_session(created['execution_session_id'], user_id=scope['user_id'])
    target = await target_for_session(session, scope['user_id'])
    body = {'task_control': {k: target[k] for k in ('task_id', 'expected_revision', 'expected_run')}
            | {'idempotency_key': 'stop-before-followup'}}
    path = f'/api/agent/session/{session.id}'
    async with client_for(scope) as client:
        assert (await client.post(path + '/abort', json=body)).status_code == 200
        response = await client.post(path + '/prompt_async', json={'text': 'new direction', 'client_message_id': 'new-direction'})
        assert response.status_code == 200, response.text
    if suspended:
        from db.models.question import QuestionCheckpoint, SessionExecution
        async with get_db_session() as db:
            assert (await db.get(QuestionCheckpoint, question.id)).status == 'cancelled'
            assert not (await db.get(SessionExecution, session.id)).resume_pending
    new_lease = await reserve_run(session.id, scope['user_id'])
    try:
        batch = await inbox.claim_inbox_boundary(new_lease, step=1, include_next_turn=True)
        assert [row.id for row in batch.receipts] == [response.json()['inboxId']]
    finally:
        await new_lease.release()


async def test_new_input_cannot_bypass_unknown_external_outcomes(monkeypatch):
    args, _, lease, _, _ = await stopped(monkeypatch)
    current = await view(args)
    async with get_db_session() as db:
        db.add(Part(id=generate_id(), session_id=lease.session_id, user_id=lease.user_id,
            message_id=current['latest_result']['result_message_id'], type='tool',
            data={'type': 'tool', 'status': 'error', 'metadata': {'outcome_unknown': True}},
            created_at=datetime.now(timezone.utc)))
    async with client_for(args) as client:
        response = await client.post(f'/api/agent/session/{lease.session_id}/prompt_async', json={
            'text': 'hello', 'client_message_id': 'unverified-effects'})
        assert response.status_code == 409
        assert response.json()['detail']['code'] == 'ASSISTANT_EFFECT_UNRESOLVED'
    assert (await view(args))['task']['desired_state'] == 'canceled'


async def test_reopen_and_input_admission_roll_back_together(monkeypatch):
    args, _, lease, _, _ = await stopped(monkeypatch)
    before = (await view(args))['task']

    async def failed(*args, **kwargs):
        raise RuntimeError('inbox admission failed')

    monkeypatch.setattr('assistant.commands.accept_inbox_item_locked', failed)
    async with client_for(args) as client:
        with pytest.raises(RuntimeError, match='inbox admission failed'):
            await client.post(f'/api/agent/session/{lease.session_id}/prompt_async', json={
                'text': 'hello', 'client_message_id': 'atomic-reopen'})
    current = (await view(args))['task']
    assert current['desired_state'] == 'canceled'
    assert current['control_revision'] == before['control_revision']
    assert current['intent_revision'] == before['intent_revision']


async def test_non_session_commands_and_steer_cannot_reopen_canceled_task(monkeypatch):
    args, _, _, _, _ = await stopped(monkeypatch)
    revision = (await view(args))['task']['control_revision']
    for changes in ({'delivery': 'followup', 'expected_run': None}, {'client_message_id': 'stale-steer'}):
        with pytest.raises(AssistantError) as rejected:
            await accept_task_command(**{**args, **changes, 'expected_revision': revision})
        assert rejected.value.code == 'ASSISTANT_TASK_NOT_RUNNING'
    assert (await view(args))['task']['desired_state'] == 'canceled'


async def test_new_message_does_not_implicitly_resume_paused_task(monkeypatch):
    isolated_runtime(monkeypatch)
    args, _, lease, _ = await running()
    await accept_control_command(**control(args))
    await loop.run_loop(lease.session_id, lease.user_id, lease=lease)
    async with client_for(args) as client:
        response = await client.post(f'/api/agent/session/{lease.session_id}/prompt_async', json={
            'text': 'hello', 'client_message_id': 'paused-input'})
        assert response.status_code == 409
        assert response.json()['detail']['code'] == 'ASSISTANT_TASK_NOT_RUNNING'
    assert (await view(args))['task']['desired_state'] == 'paused'
