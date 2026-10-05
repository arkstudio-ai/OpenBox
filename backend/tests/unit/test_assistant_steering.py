"""Exact-run task steering across SQL races, HTTP, recovery and settlement."""
import asyncio
from datetime import datetime, timedelta, timezone
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from agent import inbox
from agent.driver import reserve_run
from assistant.commands import accept_task_command
from assistant.policy import AssistantError
from assistant.reads import get_task
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask, TaskResult, TaskSubmission
from models.message import TextPart
from session.agent_event_log import verify_agent_event_parity
from session.session import create_assistant_message, save_part, update_message_info
from tests.unit.test_assistant_api import client_for
from tests.unit.test_assistant_commands import setup_task
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401


async def running():
    owner, _, workspace, main, create = await setup_task()
    receipt = await accept_task_command(**create)
    lease = await reserve_run(receipt['execution_session_id'], owner)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    await lease.set_phase('running')
    args = dict(user_id=owner, workspace_id=workspace, main_id=main.id, task_id=receipt['task_id'],
        expected_revision=2, idempotency_key='steer-first', prompt='Shorten the current report', delivery='steer',
        expected_run={'run_id': lease.run_id, 'generation': lease.generation})
    return args, receipt, lease, batch


async def terminal(lease, parent_id, *, failed=False, settle=True):
    fence = (lease.session_id, lease.run_id, lease.generation)
    answer = await create_assistant_message(lease.session_id, parent_id, agent='build', model_id='test/model',
        user_id=lease.user_id, run_fence=fence)
    await save_part(TextPart(session_id=lease.session_id, message_id=answer.id, text='Result from this run'),
        is_new=True, user_id=lease.user_id, run_fence=fence)
    answer.finish = 'error' if failed else 'stop'
    if failed:
        answer.error = {'name': 'ProviderError', 'message': 'No response'}
    await update_message_info(answer, user_id=lease.user_id, run_fence=fence)
    if settle:
        await inbox.settle_claimed_inbox_items(lease, result_message_id=answer.id, outcome='error' if failed else 'succeeded')
    return answer


async def view(args):
    return await get_task(**{key: args[key] for key in ('user_id', 'workspace_id', 'main_id', 'task_id')})


async def test_two_steers_join_the_same_run_and_produce_one_result_with_all_inputs():
    args, created, lease, batch = await running()
    try:
        first, duplicate = await asyncio.gather(accept_task_command(**args), accept_task_command(**args))
        assert first == duplicate and first['state'] == 'accepted'
        assert (await view(args))['latest_submission']['applied_at'] is None
        second = await accept_task_command(**{**args, 'idempotency_key': 'steer-second', 'expected_revision': 3,
            'prompt': 'Keep the citations'})
        modified = await inbox.claim_inbox_boundary(lease, step=2, include_next_turn=False)
        assert {row.id for row in modified.receipts} == {first['inbox_id'], second['inbox_id']}
        assert all(row.run_id == lease.run_id and row.generation == lease.generation for row in modified.receipts)
        current = await view(args)
        assert current['latest_submission']['disposition'] == 'applied'
        assert current['latest_submission']['expected_run'] == args['expected_run']
        answer = await terminal(lease, modified.messages[-1].id)
        await inbox.settle_claimed_inbox_items(lease, result_message_id=answer.id, outcome='succeeded')
        async with get_db_session() as db:
            results = list((await db.scalars(select(TaskResult).where(TaskResult.task_id == created['task_id']))).all())
            assert len(results) == 1 and results[0].observed_intent_revision == 3
            assert set(results[0].consumed_inbox_ids) == {created['inbox_id'], first['inbox_id'], second['inbox_id']}
        assert (await verify_agent_event_parity(lease.session_id, user_id=lease.user_id)).ok
    finally:
        await lease.release(session_status='idle')
    assert await accept_task_command(**args) == first  # Response lost, now run/revision have changed.
    with pytest.raises(AssistantError) as changed:
        await accept_task_command(**{**args, 'expected_run': {'run_id': 'different', 'generation': 2}})
    assert changed.value.code == 'ASSISTANT_COMMAND_CONFLICT'


@pytest.mark.parametrize('change', ['run', 'generation', 'finalizing', 'abort', 'expired', 'unclaimed'])
async def test_stale_or_non_executing_target_rejects_before_acceptance(change):
    args, _, lease, _ = await running()
    try:
        if change in {'run', 'generation'}:
            args['expected_run'] = {**args['expected_run'], change if change == 'generation' else 'run_id':
                lease.generation + 1 if change == 'generation' else 'obsolete'}
        else:
            async with get_db_session() as db:
                state = await db.get(AgentDriverState, lease.session_id)
                if change == 'finalizing': state.phase = 'finalizing'
                if change == 'abort': state.abort_requested_at = datetime.now(timezone.utc)
                if change == 'expired': state.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                if change == 'unclaimed': state.trigger_message_id = None
        with pytest.raises(AssistantError) as stale:
            await accept_task_command(**args)
        assert stale.value.code == 'ASSISTANT_RUN_CONFLICT'
        async with get_db_session() as db:
            assert await db.scalar(select(func.count()).select_from(TaskSubmission).where(TaskSubmission.task_id == args['task_id'])) == 1
            assert not await db.scalar(select(AssistantCommand.id).where(AssistantCommand.actor_user_id == args['user_id'],
                AssistantCommand.idempotency_key == args['idempotency_key']))
    finally:
        await lease.release(session_status='idle')


async def test_stopped_before_claim_is_not_applied_and_never_wakes_a_replacement(monkeypatch):
    args, _, lease, batch = await running()
    accepted = await accept_task_command(**args)
    await terminal(lease, batch.messages[0].id)
    assert (await view(args))['task']['control_revision'] == accepted['task_revision'] + 1
    await lease.release(session_status='idle')
    current = await view(args)
    assert current['latest_submission']['disposition'] == 'not_applied'
    assert current['latest_submission']['run_id'] is None and current['latest_submission']['applied_at'] is None
    assert current['latest_submission']['state'] == 'canceled'
    assert current['task']['observed_state'] == 'input_not_applied'
    assert current['latest_result']['observed_intent_revision'] == 1 < current['task']['intent_revision']
    async def no_reservation(*args, **kwargs):
        raise AssertionError('A stale steer must not start another run')
    monkeypatch.setattr('agent.driver.reserve_run', no_reservation)
    assert await inbox.wake_inbox_session(lease.session_id, lease.user_id) is None
    assert await accept_task_command(**args) == accepted
    assert (await verify_agent_event_parity(lease.session_id, user_id=lease.user_id)).ok


async def test_recovery_expires_unclaimed_steer_but_preserves_ordinary_followup(monkeypatch):
    args, _, lease, _ = await running()
    accepted = await accept_task_command(**args)
    async with get_db_session() as db:
        state = await db.get(AgentDriverState, lease.session_id)
        state.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    try:
        async def no_reservation(*args, **kwargs):
            raise AssertionError('Expired target must be reconciled before reservation')
        monkeypatch.setattr('agent.driver.reserve_run', no_reservation)
        assert await inbox.wake_inbox_session(lease.session_id, lease.user_id) is None
        assert (await inbox.get_inbox_item(accepted['inbox_id'], user_id=lease.user_id)).state == 'canceled'
        revision = (await view(args))['task']['control_revision']
        followup = await accept_task_command(**{**args, 'delivery': 'followup', 'expected_run': None,
            'expected_revision': revision, 'idempotency_key': 'explicit-next-turn'})
        assert await inbox._has_waking_input(lease.session_id, lease.user_id)
        assert (await inbox.get_inbox_item(followup['inbox_id'], user_id=lease.user_id)).state == 'accepted'
    finally:
        await lease.release(session_status='idle')


async def test_two_devices_racing_revision_accept_only_one_new_modification():
    args, _, lease, _ = await running()
    try:
        results = await asyncio.gather(accept_task_command(**args), accept_task_command(**{
            **args, 'idempotency_key': 'second-device', 'prompt': 'Expand the report'}), return_exceptions=True)
        assert len([item for item in results if isinstance(item, dict)]) == 1
        assert [item.code for item in results if isinstance(item, AssistantError)] == ['ASSISTANT_REVISION_CONFLICT']
    finally:
        await lease.release(session_status='idle')


async def test_claim_receipt_remains_input_applied_when_provider_fails():
    args, _, lease, batch = await running()
    try:
        await accept_task_command(**args)
        modified = await inbox.claim_inbox_boundary(lease, step=2, include_next_turn=False)
        await terminal(lease, modified.messages[-1].id, failed=True)
        current = await view(args)
        assert current['latest_submission']['disposition'] == 'applied'
        assert current['latest_result']['outcome'] == 'error' and current['latest_result']['delivery_state'] == 'pending'
    finally:
        await lease.release(session_status='idle')


async def test_http_and_legacy_adapters_require_explicit_revision_and_run(monkeypatch):
    from api import sessions as routes
    args, _, lease, _ = await running()
    actor = {'user_id': args['user_id'], 'workspace_id': args['workspace_id']}
    try:
        async with client_for(args['user_id'], args['workspace_id'], monkeypatch) as client:
            path = f"/api/assistant/tasks/{args['task_id']}/commands"
            payload = {'idempotency_key': 'http-steer', 'action': 'input', 'expected_revision': 2,
                'input': {'text': args['prompt'], 'delivery': 'steer', 'expected_run': args['expected_run']}}
            response = await client.post(path, json=payload)
            assert response.status_code == 202 and response.json()['expected_run'] == args['expected_run']
            assert (await client.post(path, json=payload)).json() == response.json()
            missing = {**payload, 'input': {'text': 'Not bound', 'delivery': 'steer'}}
            assert (await client.post(path, json=missing)).status_code == 400
        with pytest.raises(HTTPException) as missing:
            await routes.send_message_async(lease.session_id, routes.PromptBody(
                text='No inferred target', client_message_id='legacy-no-target', delivery='steer'), actor)
        assert missing.value.status_code == 400
        async def do_not_wake(*args): return None
        monkeypatch.setattr(inbox, 'wake_inbox_session', do_not_wake)
        legacy = routes.PromptBody(text='Legacy exact steer', client_message_id='legacy-steer', delivery='steer',
            expected_task_revision=3, expected_run=args['expected_run'])
        first = await routes.send_message_async(lease.session_id, legacy, actor)
        assert first['state'] == 'accepted' and first['delivery'] == 'steer'
        assert await routes.send_message_async(lease.session_id, legacy, actor) == first
    finally:
        await lease.release(session_status='idle')


async def test_release_and_not_applied_receipt_roll_back_together(monkeypatch):
    from assistant import steering
    args, _, lease, _ = await running()
    accepted = await accept_task_command(**args)
    original = steering.expire_task_steers_locked
    async def crash(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError('Before release commit')
    monkeypatch.setattr(steering, 'expire_task_steers_locked', crash)
    with pytest.raises(RuntimeError, match='Before release commit'):
        await lease.release(session_status='idle')
    async with get_db_session() as db:
        state = await db.get(AgentDriverState, lease.session_id)
        assert state.run_id == lease.run_id and state.phase == 'running'
        assert (await db.get(AgentInboxItem, accepted['inbox_id'])).state == 'accepted'
        assert not await db.scalar(select(AgentEvent.id).where(AgentEvent.session_id == lease.session_id,
            AgentEvent.kind == 'inbox.canceled'))
    monkeypatch.setattr(steering, 'expire_task_steers_locked', original)
    await lease.release(session_status='idle')
    assert (await view(args))['latest_submission']['disposition'] == 'not_applied'


async def test_followup_projection_keeps_old_business_evidence_valid():
    from assistant.evidence import projection_digest, validate_business_reads
    args, _, lease, _ = await running()
    try:
        current = await view(args)
        legacy = {**current, 'latest_submission': {key: value for key, value in current['latest_submission'].items()
            if key not in {'delivery', 'expected_run', 'state', 'error'}}}
        async with get_db_session() as db:
            await validate_business_reads(db, [{'operation': 'tasks.get', 'arguments': {'task_id': args['task_id']},
                'digest': projection_digest(legacy)}], user_id=args['user_id'], workspace_id=args['workspace_id'], main_id=args['main_id'])
    finally:
        await lease.release(session_status='idle')


async def test_real_loop_consumes_busy_steer_on_next_step_without_new_run(monkeypatch):
    from agent import loop, processor
    from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
    args, _, lease, _ = await running()
    calls, accepted = [], []
    async def provider(**kwargs):
        wire = json.dumps(kwargs['messages'])
        calls.append(wire)
        if len(calls) == 1:
            assert args['prompt'] not in wire
            accepted.append(await accept_task_command(**args))
            assert (await view(args))['latest_submission']['applied_at'] is None
        else:
            assert args['prompt'] in wire
            current = await view(args)
            assert current['latest_submission']['run_id'] == lease.run_id
            assert current['latest_submission']['generation'] == lease.generation
        yield {'type': 'text_delta', 'text': 'Initial draft' if len(calls) == 1 else 'Short final report'}
        yield {'type': 'finish', 'reason': 'stop', 'usage': {}}
    _patch_real_loop_runtime(monkeypatch, config=_loop_config(), process_step=processor.process_step)
    monkeypatch.setattr(processor, 'stream_llm', provider)
    try:
        await loop.run_loop(lease.session_id, lease.user_id, lease=lease)
        assert len(calls) == 2
        current = await view(args)
        assert current['latest_submission']['disposition'] == 'applied'
        assert current['latest_result']['outcome'] == 'succeeded'
        assert current['latest_result']['observed_intent_revision'] == 2
        async with get_db_session() as db:
            assert await db.scalar(select(func.count()).select_from(TaskResult).where(TaskResult.task_id == args['task_id'])) == 1
            assert (await db.get(AgentDriverState, lease.session_id)).generation == lease.generation
        parity = await verify_agent_event_parity(lease.session_id, user_id=lease.user_id)
        assert parity.ok, parity.model_dump()
    finally:
        await lease.release(session_status='idle')
