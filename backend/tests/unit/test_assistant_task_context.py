"""Current task facts remain fresh without granting authority to old snapshots."""
from copy import deepcopy
from datetime import datetime, timezone
import json

import pytest
from sqlalchemy import select

from assistant.commands import accept_task_command
from assistant.context_sources import checked_context_locked
from assistant.evidence import validate_message_sources
from assistant.policy import AssistantError
from assistant.task_context import task_context, validate_task_snapshots
from db.base import get_db_session
from db.models.assistant import AssistantTask, TaskResult
from db.models.project import Project
from db.models.session import Session
from tests.unit.assistant_source_fixtures import consume_context
from tests.unit.test_assistant_context_sources import finish
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_reads import read_turn
from tests.unit.test_assistant_reporting import prepare_report


async def test_each_provider_step_reloads_progress_without_invalidating_historical_observations():
    ctx, lease, answer, accepted, _ = await read_turn()
    try:
        await consume_context(ctx)
        prior = deepcopy(ctx._assistant_context)
        revision = prior['task_snapshots'][0]['snapshot']['task']['control_revision']
        assert prior['task_snapshots'][0]['snapshot']['task']['observed_state'] == 'completed'
        await accept_task_command(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
            idempotency_key='snapshot-followup', task_id=accepted['task_id'], expected_revision=revision,
            prompt='Check the result again')
        await consume_context(ctx)
        current = ctx._assistant_context['task_snapshots'][0]['snapshot']
        assert current['task']['control_revision'] == revision + 1 and current['task']['observed_state'] == 'queued'
        assert current['latest_result']['observed_intent_revision'] == 1
        async with get_db_session() as db:
            main = await db.get(Session, ctx.session_id)
            await checked_context_locked(db, main, prior)
            with pytest.raises(AssistantError) as stale:
                await checked_context_locked(db, main, prior, fresh=True)
            assert stale.value.code == 'ASSISTANT_TASK_SNAPSHOT_CHANGED'
        await finish(ctx, lease, answer, 'A follow-up was accepted. The earlier result belongs to the earlier intent.')
        async with get_db_session() as db:
            await validate_message_sources(db, answer, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
    finally:
        await lease.release(session_status='idle')


async def test_task_context_is_bounded_prioritizes_waiting_and_filters_before_ranking(monkeypatch):
    ctx, lease, _, accepted, _ = await read_turn()
    try:
        receipts = [await accept_task_command(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
            main_id=ctx.session_id, project_id=ctx.project_id, idempotency_key=f'task-{i}',
            prompt=f'Test task {i}', title=f'VISIBLE-{i}') for i in range(4)]
        async with get_db_session() as db:
            waiting = await db.get(AssistantTask, accepted['task_id'])
            waiting.observed_state = 'waiting_input'
            archived = await db.get(AssistantTask, receipts[0]['task_id'])
            archived.archived_at = datetime.now(timezone.utc)
            hidden = await db.get(Session, receipts[1]['execution_session_id'])
            hidden.visibility = 'workspace'
        monkeypatch.setattr('assistant.task_context.MAX_CONTEXT_TASKS', 2)
        async with get_db_session() as db:
            value, refs = await task_context(db, await db.get(Session, ctx.session_id))
        assert len(value['items']) == 2 and len(refs) == 2 and value['has_more']
        assert value['items'][0]['task']['id'] == accepted['task_id']
        assert not value['complete_inventory']
        assert 'VISIBLE-0' not in json.dumps(value) and 'VISIBLE-1' not in json.dumps(value)
    finally:
        await lease.release(session_status='idle')


@pytest.mark.parametrize('change', ['private_scope', 'project', 'title', 'result'])
async def test_saved_answer_cannot_outlive_task_scope_or_immutable_source_changes(change):
    ctx, lease, answer, accepted, _ = await read_turn()
    try:
        await consume_context(ctx)
        await finish(ctx, lease, answer, 'Current SQL task facts were observed.')
        async with get_db_session() as db:
            task = await db.get(AssistantTask, accepted['task_id'])
            if change == 'private_scope':
                (await db.get(Session, task.execution_session_id)).visibility = 'workspace'
            elif change == 'project':
                (await db.get(Project, task.project_id)).is_deleted = True
            elif change == 'title':
                task.title = 'Replaced task title'
            else:
                (await db.get(TaskResult, task.latest_result_id)).outcome = 'failed'
        async with get_db_session() as db:
            with pytest.raises(AssistantError):
                await validate_message_sources(db, answer, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
    finally:
        await lease.release(session_status='idle')


async def test_report_only_never_receives_ambient_task_snapshots():
    ctx, lease, _, _, _, _ = await prepare_report()
    try:
        payload = await consume_context(ctx)
        assert ctx._assistant_context['task_snapshots'] == []
        assert 'Current authorized SQL task facts' not in json.dumps(payload)
        async with get_db_session() as db:
            main = await db.get(Session, ctx.session_id)
            _, snapshots = await task_context(db, main)
            with pytest.raises(AssistantError) as denied:
                await checked_context_locked(db, main, {**ctx._assistant_context, 'task_snapshots': snapshots})
            assert denied.value.code == 'ASSISTANT_REPORT_SCOPE'
    finally:
        await lease.release(session_status='idle')


async def test_task_snapshot_mutation_and_source_budget_are_rejected():
    ctx, lease, _, _, _ = await read_turn()
    try:
        await consume_context(ctx)
        refs = deepcopy(ctx._assistant_context['task_snapshots'])
        refs[0]['snapshot']['task']['observed_state'] = 'forged'
        async with get_db_session() as db:
            main = await db.get(Session, ctx.session_id)
            for invalid in (refs, [ctx._assistant_context['task_snapshots'][0]] * 201):
                with pytest.raises(AssistantError) as denied:
                    await validate_task_snapshots(db, main, invalid)
                assert denied.value.code == 'ASSISTANT_TASK_SNAPSHOT_UNVERIFIED'
    finally:
        await lease.release(session_status='idle')


async def test_task_progress_race_rebuilds_before_the_first_provider_request():
    from agent.loop import _prepare_checkpointed_provider_attempt, _to_llm_messages
    from assistant.context_sources import record_provider_context
    from assistant.evidence import projection_digest
    from assistant.projection import project_main_messages
    from session.agent_event_log import checkpoint_model_request, load_canonical_model_surface
    ctx, lease, answer, accepted, _ = await read_turn()
    attempts = 0
    async def load():
        return await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
    async def build(surface):
        projected = await project_main_messages(list(surface.messages), ctx=ctx)
        wire = _to_llm_messages(projected, user_id=ctx.user_id, assistant_projection_verified=True)
        ctx._assistant_context['messages_digest'] = projection_digest(wire)
        return wire
    async def checkpoint(surface, tool_digest, prompt_digest):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            async with get_db_session() as db:
                (await db.get(AssistantTask, accepted['task_id'])).observed_state = 'waiting_input'
        return await checkpoint_model_request(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence,
            request_id='task-progress-race', model_id='test/model', provider_binding_digest='a' * 64,
            tool_schema_digest=tool_digest, prompt_shape_digest=prompt_digest,
            expected_event_sequence=surface.event_sequence, expected_event_digest=surface.event_digest,
            message_id=ctx.message_id, assistant_context=ctx._assistant_context)
    try:
        prepared = await _prepare_checkpointed_provider_attempt(load_surface=load, build_messages=build,
            checkpoint=checkpoint, system=['Test request'], tools={}, model_id='test/model',
            provider_binding_digest='a' * 64, payload_dialect='openai', tool_choice=None, user_variant=None)
        assert attempts == 2
        assert ctx._assistant_context['task_snapshots'][0]['snapshot']['task']['observed_state'] == 'waiting_input'
        await record_provider_context(ctx, prepared.llm_messages)
        await finish(ctx, lease, answer, 'The current SQL task state is waiting for input.')
        async with get_db_session() as db:
            await validate_message_sources(db, answer, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
    finally:
        await lease.release(session_status='idle')
