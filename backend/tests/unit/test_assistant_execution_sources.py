"""Execution history must retain the sources of its generated instructions."""
import json

import pytest
from sqlalchemy import select

from assistant.evidence import validate_message_sources
from assistant.history import read_history
from assistant.policy import AssistantError
from assistant.public_history import public_messages
from assistant.results import part_hash
from assistant.transactions import begin_snapshot
from db.base import get_db_session
from db.models.part import Part
from db.models.message import Message
from db.models.assistant import AssistantCommand, AssistantTask
from db.models.agent_inbox import AgentInboxItem
from db.models.session import Session
from session.session import create_session, create_user_message, get_messages, get_session
from tests.unit.assistant_source_fixtures import consume_context
from tests.unit.test_assistant_command_sources import delegated_asset_task, revoke_asset, settle_execution
from tests.unit.test_assistant_command_sources import no_task_dispatch  # noqa: F401
from tests.unit.test_assistant_assets import asset_for
from tests.unit.test_assistant_context_sources import finish, next_turn
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_public_history import client_for
from tests.unit.test_assistant_api import client_for as assistant_client
from tests.unit.test_assistant_reads import call_tool, cursor_key  # noqa: F401
from tests.unit.test_assistant_schedule_commands import controlled_wakes  # noqa: F401


async def test_execution_history_cannot_bypass_a_revoked_command_source():
    ctx, lease, _, asset, receipt = await delegated_asset_task()
    try:
        result = await settle_execution(ctx, receipt)
        scope = dict(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
            session_id=receipt['execution_session_id'])
        original = await read_history(**scope, message_ids=[result.result_message_id])
        assert 'Result derived' in json.dumps(original)
        await revoke_asset(asset)
        with pytest.raises(AssistantError):
            await read_history(**scope, message_ids=[result.result_message_id])
        assert (await read_history(**scope))['items'] == []
    finally:
        await lease.release(session_status='idle')


async def test_a_saved_main_answer_cannot_launder_execution_history_derivation(monkeypatch):
    ctx, lease, answer, asset, receipt = await delegated_asset_task()
    try:
        result = await settle_execution(ctx, receipt)
        await finish(ctx, lease, answer, 'Task accepted.')
        ctx, lease, answer = await next_turn(ctx, 'Read that execution history and summarize it.')
        monkeypatch.setattr('assistant.projection.MAX_RECENT_MESSAGES', 1)
        output, _, _ = await call_tool(ctx, 'history.read', {'session_id': receipt['execution_session_id'],
            'message_ids': [result.result_message_id]})
        assert not output.metadata.get('error'), output.output
        await consume_context(ctx)
        assert not ctx._assistant_context['business_reads']
        await finish(ctx, lease, answer, 'PRIVATE_EXECUTION_DERIVATION')
        async with get_db_session() as db:
            await validate_message_sources(db, answer, user_id=ctx.user_id,
                workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        await revoke_asset(asset)
        async with get_db_session() as db:
            with pytest.raises(AssistantError):
                await validate_message_sources(db, answer, user_id=ctx.user_id,
                    workspace_id=ctx.workspace_id, main_id=ctx.session_id)
    finally:
        await lease.release(session_status='idle')


async def test_execution_public_history_rehydrates_cached_pages_before_checking_sources():
    ctx, lease, _, asset, receipt = await delegated_asset_task()
    try:
        result = await settle_execution(ctx, receipt)
        execution = await get_session(receipt['execution_session_id'], user_id=ctx.user_id)
        cached = await get_messages(execution.id, user_id=ctx.user_id)
        async with client_for(ctx.user_id, ctx.workspace_id) as client:
            url = f'/api/agent/session/{execution.id}'
            assert 'Result derived' in (await client.get(url + '/history')).text
            await revoke_asset(asset)
            for suffix in ('/history', '/message?offset=0&limit=200'):
                response = await client.get(url + suffix)
                assert response.status_code == 200, response.text
                assert 'Result derived' not in response.text
                rows = response.json()['messages'] if suffix == '/history' else response.json()
                assert all(row['parts'] == [] and row['source_status'] == 'unavailable' for row in rows)
            assert 'Result derived' not in json.dumps(await public_messages(execution, cached,
                actor_user_id=ctx.user_id))
        async with get_db_session() as db:
            original = await db.scalar(select(Part).where(Part.message_id == result.result_message_id,
                Part.type == 'text'))
            assert 'Result derived' in original.data['text']
    finally:
        await lease.release(session_status='idle')


async def test_bounded_revalidation_covers_execution_copies_and_hides_foreign_or_ordinary_sessions(monkeypatch):
    from session.fork import fork_session
    ctx, lease, _, asset, receipt = await delegated_asset_task()
    try:
        await settle_execution(ctx, receipt)
        copy = await fork_session(receipt['execution_session_id'], user_id=ctx.user_id)
        originals = await get_messages(copy.id, user_id=ctx.user_id)
        params = [('session_id', copy.id), *[('message_ids', m.id) for m in originals]]
        async with assistant_client(ctx.user_id, ctx.workspace_id, monkeypatch) as client:
            first = await client.get('/api/assistant/messages', params=params)
            assert first.status_code == 200, first.text
            assert 'Result derived' in first.text
            await revoke_asset(asset)
            hidden = await client.get('/api/assistant/messages', params=params)
            assert hidden.status_code == 200, hidden.text
            assert all(m['source_status'] == 'unavailable' and m['parts'] == []
                for m in hidden.json()['messages'])
            ordinary = await create_session(user_id=ctx.user_id, workspace_id=ctx.workspace_id)
            denied = await client.get('/api/assistant/messages', params={'session_id':ordinary.id,'message_ids':originals[0].id})
            assert denied.status_code == 404
        from tests.unit.test_assistant_foundation import accounts
        other, _, _ = await accounts()
        async with assistant_client(other, ctx.workspace_id, monkeypatch) as client:
            assert (await client.get('/api/assistant/messages', params=params)).status_code in (403, 404)
    finally:
        await lease.release(session_status='idle')


@pytest.mark.parametrize('isolated', [True, False], ids=['protected-descendant', 'ordinary'])
async def test_sync_prompt_rechecks_sources_changed_while_its_response_was_running(monkeypatch, isolated):
    from api import sessions as routes
    from models.message import TextPart
    from session.session import create_assistant_message, save_part, update_message_info
    ctx, lease, _, asset, receipt = await delegated_asset_task()
    try:
        await settle_execution(ctx, receipt)
        target = await create_session(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
            parent_id=receipt['execution_session_id'] if isolated else None, model='test/model')

        async def no_quota(*_, **__):
            pass

        async def complete_then_revoke(session_id, *, user_id, lease):
            request = (await get_messages(session_id, user_id=user_id))[-1]
            fence = (session_id, lease.run_id, lease.generation)
            answer = await create_assistant_message(session_id, request.id,
                model_id='test/model', agent='build', user_id=user_id, run_fence=fence)
            await save_part(TextPart(session_id=session_id, message_id=answer.id,
                text='SYNC_RESPONSE_BODY'), user_id=user_id, is_new=True, run_fence=fence)
            answer.finish = 'stop'
            await update_message_info(answer, user_id=user_id, run_fence=fence)
            await revoke_asset(asset)
            return answer

        monkeypatch.setattr(routes, 'check_concurrent_agents', no_quota)
        monkeypatch.setattr(routes, '_remember_prompt_history', lambda *_: None)
        monkeypatch.setattr('agent.loop.run_loop', complete_then_revoke)
        async with client_for(ctx.user_id, ctx.workspace_id) as client:
            response = await client.post(f'/api/agent/session/{target.id}/message',
                json={'text': 'Continue with a text reply.'})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['role'] == 'assistant'
        if isolated:
            assert body['parts'] == [] and body['source_status'] == 'unavailable'
            assert 'SYNC_RESPONSE_BODY' not in response.text
        else:
            assert 'SYNC_RESPONSE_BODY' in response.text
            assert 'source_status' not in body
        async with get_db_session() as db:
            original = await db.scalar(select(Part).where(Part.message_id == body['id'], Part.type == 'text'))
            assert original.data['text'] == 'SYNC_RESPONSE_BODY'
    finally:
        await lease.release(session_status='idle')


@pytest.mark.parametrize('copy', [False, True], ids=['descendant', 'fork'])
async def test_execution_copies_and_descendants_retain_original_command_sources_after_reopen(copy):
    from db.base import close_engine, init_engine
    from session.fork import fork_session
    ctx, lease, _, asset, receipt = await delegated_asset_task()
    try:
        await settle_execution(ctx, receipt)
        if copy:
            derived = await fork_session(receipt['execution_session_id'], user_id=ctx.user_id)
        else:
            derived = await create_session(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
                parent_id=receipt['execution_session_id'])
            await create_user_message(derived.id, 'PRIVATE_DESCENDANT_INPUT', agent='build', user_id=ctx.user_id)
        cached = await get_messages(derived.id, user_id=ctx.user_id)
        visible = await public_messages(derived, cached, actor_user_id=ctx.user_id)
        assert visible and all(row['source_status'] == 'available' for row in visible)
        await revoke_asset(asset)
        async with get_db_session() as db:
            url = db.get_bind().url.render_as_string(hide_password=False)
        await close_engine()
        init_engine(url)
        hidden = await public_messages(derived, cached, actor_user_id=ctx.user_id)
        assert all(row['parts'] == [] and row['source_status'] == 'unavailable' for row in hidden)
    finally:
        await lease.release(session_status='idle')


async def test_reparenting_cannot_remove_recorded_execution_history_dependencies():
    ctx, lease, _, _, receipt = await delegated_asset_task()
    try:
        child = await create_session(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
            parent_id=receipt['execution_session_id'])
        await create_user_message(child.id, 'PRIVATE_CHILD', agent='build', user_id=ctx.user_id)
        cached = await get_messages(child.id, user_id=ctx.user_id)
        assert (await public_messages(child, cached, actor_user_id=ctx.user_id))[0]['source_status'] == 'available'
        async with get_db_session() as db:
            (await db.get(Session, child.id)).parent_id = None
        assert (await public_messages(child, cached, actor_user_id=ctx.user_id))[0]['parts'] == []
    finally:
        await lease.release(session_status='idle')


async def test_later_unconsumed_followup_does_not_hide_original_execution_history():
    ctx, lease, answer, _, receipt = await delegated_asset_task()
    try:
        result = await settle_execution(ctx, receipt)
        execution = await get_session(receipt['execution_session_id'], user_id=ctx.user_id)
        cached = await get_messages(execution.id, user_id=ctx.user_id)
        later_asset = await asset_for(ctx.user_id, ctx.workspace_id)
        await call_tool(ctx, 'assets.list', {})
        await consume_context(ctx)
        async with get_db_session() as db:
            revision = (await db.get(AssistantTask, receipt['task_id'])).control_revision
        output, _, _ = await call_tool(ctx, 'tasks.followup', {'task_id': receipt['task_id'],
            'text': 'Use the new resource next time', 'expected_revision': revision,
            'source_message_ids': [answer.parent_id]})
        assert not output.metadata.get('error'), output.output
        await revoke_asset(later_asset)
        page = await read_history(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
            main_id=ctx.session_id, session_id=execution.id, message_ids=[result.result_message_id])
        assert 'Result derived' in json.dumps(page)
        assert all(row['source_status'] == 'available' for row in await public_messages(
            execution, cached, actor_user_id=ctx.user_id))
    finally:
        await lease.release(session_status='idle')


async def test_command_execution_source_cycle_is_not_hidden_by_shared_original_checks():
    from assistant.command_sources import command_derivation_ref
    from assistant.execution_sources import validate_execution_message
    ctx, lease, _, _, receipt = await delegated_asset_task()
    try:
        result = await settle_execution(ctx, receipt)
        async with get_db_session() as db:
            command = await db.get(AssistantCommand, receipt['command_id'])
            source = await db.scalar(select(Part).where(Part.message_id == result.result_message_id,
                Part.type == 'text'))
            proof = {**command.source_ref['derivation'], 'source_refs': [{
                'session_id': source.session_id, 'message_id': source.message_id,
                'part_id': source.id, 'content_hash': part_hash(source)}]}
            command.source_ref = {**command.source_ref, 'derivation': proof}
            accepted = await db.get(AgentInboxItem, receipt['inbox_id'])
            accepted.origin_ref = {**accepted.origin_ref, 'derivation_ref': command_derivation_ref(command)}
        async with get_db_session() as db:
            checks = await begin_snapshot(db)
            message = await db.get(Message, result.result_message_id)
            for _ in range(2):
                with pytest.raises(AssistantError) as cycle:
                    await validate_execution_message(db, message, user_id=ctx.user_id,
                        workspace_id=ctx.workspace_id, main_id=ctx.session_id, snapshot_checks=checks)
                assert cycle.value.code == 'ASSISTANT_COMMAND_SOURCE_UNVERIFIED'
    finally:
        await lease.release(session_status='idle')
