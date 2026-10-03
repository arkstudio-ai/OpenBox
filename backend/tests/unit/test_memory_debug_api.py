"""Diagnostic read/replay isolation and independently granted side effects."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select, update, func

from api import memory_debug, memory_search
from core.config import MemoryConfig, OpenBoxConfig
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_runtime import MemoryReplayPreview
from db.models.memory_v2 import MemoryDebugRun, MemoryOutbox, MemoryRevision
from db.models.workspace import WorkspaceMember
from memory import observability, service
from memory.policy import resolve_access_scope
from tests.unit.test_memory_authority_v2 import authority_scope, identity, source_session  # noqa: F401


@pytest.fixture
async def debug_env(authority_scope, monkeypatch):
    scope = authority_scope
    config = OpenBoxConfig(memory=MemoryConfig(debug_view=True, debug_replay=True, stable_context_max_chars=3000))
    monkeypatch.setattr('core.config.get_config', lambda: config)
    monkeypatch.setattr(memory_debug, 'get_config', lambda: config)
    monkeypatch.setattr(memory_search, 'get_config', lambda: config)
    async with get_db_session() as db:
        access = await resolve_access_scope(db, **identity(scope), project_id=scope['p1'])
    memory = await service.create_note(**identity(scope), project_id=scope['p1'], summary='记忆：我偏好简短中文回答')
    run_id = await observability.create_debug_run('记忆：中文回答 api_key=sk-synthetic-secret-value', access, config.memory)
    await observability.add_debug_step(run_id, 'route', 'SKIPPED', data={'query': 'api_key=sk-synthetic-secret-value'}, reason_code='jev_skip')
    await observability.finish_debug_run(run_id, source_refs=[{'kind':'memory','id':memory['id'],'revision':memory['revision']}])
    yield scope, config, run_id, memory


async def test_gets_are_read_only_redacted_and_cross_user_ids_are_hidden(debug_env, monkeypatch):
    scope, config, run_id, memory = debug_env
    async def forbidden_provider(*args, **kwargs):
        pytest.fail('GET must not invoke a model provider')
    monkeypatch.setattr('memory.index.embedding.BailianEmbedding.embed', forbidden_provider)
    monkeypatch.setattr('memory.orchestrator.route_context_needs', forbidden_provider)
    before = None
    async with get_db_session() as db:
        before = await db.scalar(select(func.count()).select_from(MemoryDebugRun))
    detail = await memory_debug.read_run(run_id, current_user=identity(scope))
    assert detail['run']['body_available']
    assert 'sk-synthetic-secret-value' not in str(detail)
    assert detail['steps'][0]['reason_code'] == 'jev_skip'
    rows = await memory_debug.list_runs(project_id=scope['p1'], limit=30, current_user=identity(scope))
    assert [run['id'] for run in rows['runs']] == [run_id]
    intruder = {'user_id': scope['other'], 'workspace_id':scope['workspace_id']}
    with pytest.raises(HTTPException) as exc:
        await memory_debug.read_run(run_id, current_user=intruder)
    assert exc.value.status_code == 404
    assert (await memory_debug.list_runs(limit=30, current_user=intruder))['runs'] == []
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(MemoryDebugRun)) == before
        assert (await db.get(UserMemory, memory['id'])).revision == 1


async def test_list_time_filters_are_timezone_aware_and_status_aliases_preserve_wire_state(debug_env):
    scope, config, run_id, memory = debug_env
    now = datetime.now(timezone.utc)
    result = await memory_debug.list_runs(project_id=scope['p1'], status='completed',
        since=now-timedelta(minutes=5), until=now+timedelta(minutes=5), limit=30, current_user=identity(scope))
    assert [run['id'] for run in result['runs']] == [run_id]
    assert result['runs'][0]['status'] == 'SUCCEEDED'
    future = await memory_debug.list_runs(project_id=scope['p1'], since=now+timedelta(days=1), limit=30,
        current_user=identity(scope))
    assert future['runs'] == []
    with pytest.raises(HTTPException) as exc:
        await memory_debug.list_runs(since=datetime.now(), limit=30, current_user=identity(scope))
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        await memory_debug.list_runs(since=now, until=now-timedelta(seconds=1), limit=30, current_user=identity(scope))
    assert exc.value.status_code == 422


async def test_list_orders_and_pages_by_time_across_wiki_and_regular_ids(debug_env):
    scope, config, run_id, memory = debug_env
    instant = datetime.now(timezone.utc)
    wiki_id = 'wiki_trace_' + run_id
    latest_id = 'memoryrun_a_' + run_id
    async with get_db_session() as db:
        original = await db.get(MemoryDebugRun, run_id)
        original.created_at = instant - timedelta(minutes=1)
        for item_id, created in ((wiki_id, instant-timedelta(minutes=2)), (latest_id, instant)):
            db.add(MemoryDebugRun(id=item_id, request_id=item_id, attempt_id=item_id,
                user_id=scope['user_id'], workspace_id=scope['workspace_id'], project_id=scope['p1'],
                status='SUCCEEDED', policy_version=config.memory.policy_version,
                input_hash='synthetic', input_snapshot={}, source_refs=[], usage={},
                created_at=created, expires_at=instant+timedelta(days=1)))
    first = await memory_debug.list_runs(project_id=scope['p1'], limit=1, current_user=identity(scope))
    second = await memory_debug.list_runs(project_id=scope['p1'], limit=1, cursor=first['next_cursor'],
        current_user=identity(scope))
    third = await memory_debug.list_runs(project_id=scope['p1'], limit=1, cursor=second['next_cursor'],
        current_user=identity(scope))
    assert [first['runs'][0]['id'], second['runs'][0]['id'], third['runs'][0]['id']] == [latest_id, run_id, wiki_id]
    assert third['next_cursor'] is None
    hidden = await memory_debug.list_runs(limit=1, cursor=latest_id,
        current_user={'user_id':scope['other'], 'workspace_id':scope['workspace_id']})
    assert hidden == {'runs': [], 'next_cursor': None, 'capabilities': observability.capabilities(config.memory, scope['other'])}


async def test_health_queue_and_observation_counters_are_actor_scoped(debug_env, monkeypatch):
    scope, config, run_id, memory = debug_env
    foreign = await service.create_note(user_id=scope['other'], workspace_id=scope['workspace_id'], summary='foreign private note')
    async with get_db_session() as db:
        await db.execute(update(MemoryOutbox).where(MemoryOutbox.object_id == foreign['id']).values(
            status='RUNNING', attempts=8, lease_until=datetime.now(timezone.utc)-timedelta(minutes=1)))
        intruder = await resolve_access_scope(db, user_id=scope['other'], workspace_id=scope['workspace_id'])
    other_run = await observability.create_debug_run('foreign input', intruder, config.memory)
    await observability.add_debug_step(other_run, 'route', 'SUCCEEDED', reason_code='foreign-marker')
    async def index_health(*args):
        return {'status': 'ready', 'points_count': 12345}
    monkeypatch.setattr('memory.index.qdrant.QdrantMemoryIndex.health', index_health)
    health = await memory_debug.health(current_user=identity(scope))
    assert health['outbox']['counts'] == {'PENDING': 1}
    assert health['outbox']['expired_leases'] == health['outbox']['retry_attempts'] == 0
    assert health['outbox']['oldest_pending_age_seconds'] is not None
    assert health['jobs']['oldest_pending_age_seconds'] is None
    assert 'points_count' not in health['index'] and 'foreign-marker' not in str(health)
    assert health['outbox']['observations'] == [{'phase':'route', 'status':'SKIPPED', 'reason_code':'jev_skip', 'count':1}]


async def test_background_retention_erases_snapshot_bodies_without_changing_facts(debug_env):
    from db.models.memory_v2 import MemoryDebugStep
    scope, config, run_id, memory = debug_env
    preview = await memory_debug.replay_preview(run_id, memory_debug.ReplayPreviewBody(), current_user=identity(scope))
    async with get_db_session() as db:
        await db.execute(update(MemoryDebugRun).where(MemoryDebugRun.id == run_id).values(
            expires_at=datetime.now(timezone.utc)-timedelta(minutes=1)))
        await db.execute(update(MemoryReplayPreview).where(MemoryReplayPreview.id == preview['id']).values(
            expires_at=datetime.now(timezone.utc)-timedelta(minutes=1)))
    await observability.purge_expired_debug_snapshots()
    async with get_db_session() as db:
        run = await db.get(MemoryDebugRun, run_id)
        assert run.input_snapshot == {} and run.source_refs == [] and run.status == 'EXPIRED'
        steps = list((await db.scalars(select(MemoryDebugStep).where(MemoryDebugStep.run_id == run_id))).all())
        assert steps and all(step.data == {} and step.status == 'EXPIRED' for step in steps)
        assert (await db.get(MemoryReplayPreview, preview['id'])).grant == {}
        assert (await db.get(UserMemory, memory['id'])).revision == 1


async def test_replay_requires_preview_and_explicit_charge_confirmation_is_one_shot(debug_env, monkeypatch):
    scope, config, run_id, memory = debug_env
    calls = []
    async def fake_context(query, access, config, **kwargs):
        calls.append((query, kwargs))
        async with get_db_session() as db:
            row = await db.get(MemoryDebugRun, kwargs['existing_run_id'])
            assert row.status == 'RUNNING' and row.parent_run_id == run_id
        await observability.finish_debug_run(kwargs['existing_run_id'], 'SUCCEEDED', source_refs=[{'kind':'memory','id':memory['id'],'revision':1}])
        return {'run_id': kwargs['existing_run_id']}
    monkeypatch.setattr('memory.orchestrator.run_memory_context', fake_context)
    preview = await memory_debug.replay_preview(run_id, memory_debug.ReplayPreviewBody(steps=['retrieval']), current_user=identity(scope))
    assert preview['can_submit'] and preview['cost_estimate'] is None
    assert preview['input']['snapshot_only'] and not preview['input']['original_input_reconstructed']
    assert 'sk-synthetic-secret-value' not in str(preview)
    assert calls == []
    with pytest.raises(HTTPException) as exc:
        await memory_debug.replay(memory_debug.ReplayBody(preview_id=preview['id'], confirm_cost=False), current_user=identity(scope))
    assert exc.value.status_code == 422
    accepted = await memory_debug.replay(memory_debug.ReplayBody(preview_id=preview['id'], confirm_cost=True), current_user=identity(scope))
    repeated = await memory_debug.replay(memory_debug.ReplayBody(preview_id=preview['id'], confirm_cost=True), current_user=identity(scope))
    assert repeated['run_id'] == accepted['run_id'] and len(calls) == 1
    async with get_db_session() as db:
        assert (await db.get(UserMemory, memory['id'])).revision == 1
        assert await db.scalar(select(func.count()).select_from(MemoryRevision).where(MemoryRevision.memory_id == memory['id'])) == 1
        assert await db.scalar(select(func.count()).select_from(MemoryOutbox).where(MemoryOutbox.object_id == memory['id'])) == 1


async def test_changed_sources_hide_old_snapshot_and_block_replay(debug_env, monkeypatch):
    scope, config, run_id, memory = debug_env
    preview = await memory_debug.replay_preview(run_id, memory_debug.ReplayPreviewBody(), current_user=identity(scope))
    await service.delete_memory(**identity(scope), memory_id=memory['id'], expected_revision=1)
    async def forbidden(*args, **kwargs):
        pytest.fail('Deleted evidence must never be sent to replay providers')
    monkeypatch.setattr('memory.orchestrator.run_memory_context', forbidden)
    hidden = await memory_debug.read_run(run_id, current_user=identity(scope))
    assert not hidden['run']['body_available']
    assert 'utterance' not in hidden['run']['input_snapshot']
    assert hidden['run']['source_refs'] == []
    with pytest.raises(HTTPException) as exc:
        await memory_debug.replay(memory_debug.ReplayBody(preview_id=preview['id'], confirm_cost=True), current_user=identity(scope))
    assert exc.value.status_code == 409


async def test_preview_expiry_config_change_and_membership_revoke_block_execution(debug_env, monkeypatch):
    scope, config, run_id, memory = debug_env
    async def forbidden(*args, **kwargs):
        pytest.fail('Invalid replay grant must not call a provider')
    monkeypatch.setattr('memory.orchestrator.run_memory_context', forbidden)
    preview = await memory_debug.replay_preview(run_id, memory_debug.ReplayPreviewBody(), current_user=identity(scope))
    async with get_db_session() as db:
        await db.execute(update(MemoryReplayPreview).where(MemoryReplayPreview.id == preview['id']).values(expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)))
    with pytest.raises(HTTPException) as expired:
        await memory_debug.replay(memory_debug.ReplayBody(preview_id=preview['id'], confirm_cost=True), current_user=identity(scope))
    assert expired.value.status_code == 409
    preview = await memory_debug.replay_preview(run_id, memory_debug.ReplayPreviewBody(), current_user=identity(scope))
    config.memory.embedding_model = 'changed-model'
    with pytest.raises(HTTPException) as changed:
        await memory_debug.replay(memory_debug.ReplayBody(preview_id=preview['id'], confirm_cost=True), current_user=identity(scope))
    assert changed.value.status_code == 409
    async with get_db_session() as db:
        await db.execute(update(WorkspaceMember).where(WorkspaceMember.user_id == scope['user_id'], WorkspaceMember.workspace_id == scope['workspace_id']).values(status='removed'))
    with pytest.raises(HTTPException) as revoked:
        await memory_debug.read_run(run_id, current_user=identity(scope))
    assert revoked.value.status_code == 404


async def test_view_permission_does_not_imply_replay_permission(debug_env):
    scope, config, run_id, memory = debug_env
    config.memory.debug_replay = False
    assert (await memory_debug.read_run(run_id, current_user=identity(scope)))['run']['id'] == run_id
    with pytest.raises(HTTPException) as exc:
        await memory_debug.replay_preview(run_id, memory_debug.ReplayPreviewBody(), current_user=identity(scope))
    assert exc.value.status_code == 403


async def test_removed_canonical_input_hides_snapshot_even_without_retrieved_refs(debug_env, monkeypatch):
    scope, config, _run_id, _memory = debug_env
    from db.models.message import Message
    from db.models.part import Part
    from session.agent_event_log import (prepare_agent_event_write, append_message_events_locked,
        append_part_event_locked, append_surface_remove_locked)
    body = '这条原始问句只有当前输入，不查询记忆'
    sid, mid, pid = await source_session(scope, body=body)
    async with get_db_session() as db:
        session = await prepare_agent_event_write(db, session_id=sid, user_id=scope['user_id'], run_fence=None)
        message, part = await db.get(Message,mid), await db.get(Part,pid)
        await append_message_events_locked(db,session,message,operation='created',run_fence=None)
        await append_part_event_locked(db,session,part,message,operation='created',run_fence=None)
        access = await resolve_access_scope(db,**identity(scope),project_id=scope['p1'])
    run_id = await observability.create_debug_run(body,access,config.memory,session_id=sid,turn_id=mid)
    await observability.finish_debug_run(run_id,source_refs=[])
    assert (await memory_debug.read_run(run_id,current_user=identity(scope)))['run']['body_available']
    preview = await memory_debug.replay_preview(run_id,memory_debug.ReplayPreviewBody(),current_user=identity(scope))
    assert preview['can_submit']
    async with get_db_session() as db:
        session = await prepare_agent_event_write(db,session_id=sid,user_id=scope['user_id'],run_fence=None)
        await append_surface_remove_locked(db,session,message_ids=[mid],run_fence=None)
    async def forbidden(*args,**kwargs):
        pytest.fail('Removed canonical input must not be replayed')
    monkeypatch.setattr('memory.orchestrator.run_memory_context',forbidden)
    detail = await memory_debug.read_run(run_id,current_user=identity(scope))
    assert not detail['run']['body_available'] and 'utterance' not in detail['run']['input_snapshot']
    with pytest.raises(HTTPException) as removed:
        await memory_debug.replay(memory_debug.ReplayBody(preview_id=preview['id'],confirm_cost=True),current_user=identity(scope))
    assert removed.value.status_code == 409


async def test_explicit_search_preserves_project_scope_and_real_read_pipeline(debug_env):
    scope, config, run_id, memory = debug_env
    global_memory = await service.create_note(**identity(scope), summary='我偏好简短中文回答')
    private_other = await service.create_note(user_id=scope['other'], workspace_id=scope['workspace_id'], summary='other secret Chinese')
    bundle = await memory_search.search(memory_search.SearchBody(query='中文回答'), current_user=identity(scope))
    assert 'candidates' not in bundle
    assert {item['id'] for item in bundle['items']} & {memory['id'],private_other['id']} == set()
    assert any(item['id'] == global_memory['id'] for item in bundle['items'])
    project_bundle = await memory_search.search(memory_search.SearchBody(query='中文回答',project_id=scope['p1']), current_user=identity(scope))
    assert any(item['id'] == memory['id'] for item in project_bundle['items'])
    with pytest.raises(HTTPException) as exc:
        await memory_search.search(memory_search.SearchBody(query='中文回答',project_id=scope['foreign']), current_user=identity(scope))
    assert exc.value.status_code == 404


async def _run_with_body(scope, config, memory, *, refs):
    async with get_db_session() as db:
        access = await resolve_access_scope(db, **identity(scope), project_id=scope['p1'])
    run_id = await observability.create_debug_run('我偏好什么回答', access, config.memory)
    await observability.add_debug_step(run_id, 'retrieval', 'SUCCEEDED',
        data={'candidates': [{'kind': 'memory', 'id': memory['id'], 'text': memory['summary']}]},
        source_refs=[{'kind': 'memory', 'id': memory['id'], 'revision': memory['revision']}] if refs else None)
    return run_id


@pytest.mark.parametrize('refs', [True, False])
async def test_a_failed_or_unfinished_run_never_shows_bodies_it_cannot_recheck(debug_env, refs):
    scope, config, _run, memory = debug_env
    run_id = await _run_with_body(scope, config, memory, refs=refs)
    await observability.finish_debug_run(run_id, 'FAILED')
    shown = await memory_debug.read_run(run_id, current_user=identity(scope))
    # With its step's sources registered and still valid, a failed run can be inspected.
    assert shown['run']['body_available'] is refs
    await service.delete_memory(**identity(scope), memory_id=memory['id'], expected_revision=1)
    hidden = await memory_debug.read_run(run_id, current_user=identity(scope))
    assert not hidden['run']['body_available']
    assert memory['summary'] not in str(hidden['steps'])


async def test_an_interrupted_run_keeps_its_step_sources_for_rechecking(debug_env):
    scope, config, _run, memory = debug_env
    run_id = await _run_with_body(scope, config, memory, refs=True)  # process died here: still RUNNING
    async with get_db_session() as db:
        assert (await db.get(MemoryDebugRun, run_id)).source_refs == [
            {'kind': 'memory', 'id': memory['id'], 'revision': memory['revision']}]
    await service.delete_memory(**identity(scope), memory_id=memory['id'], expected_revision=1)
    hidden = await memory_debug.read_run(run_id, current_user=identity(scope))
    assert not hidden['run']['body_available'] and memory['summary'] not in str(hidden['steps'])
