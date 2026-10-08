"""SQL authority invariants, run against SQLite and opt-in isolated PostgreSQL.

MEMORY_TEST_POSTGRES_URL must point at a disposable, independently provisioned
schema/database. The fixture creates tables, never drops existing data.
"""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import importlib
import os
import asyncio

import pytest
from sqlalchemy import select, update, func

from db.base import Base, close_engine, get_db_session, init_engine
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceMember
from db.models.project import Project
from db.models.session import Session
from db.models.message import Message
from db.models.part import Part
from db.models.memory import UserMemory
from db.models.memory_v2 import MemoryIndexState, MemoryRevision, MemorySource, MemorySourceLink, MemoryOutbox, MemoryTombstone
from memory import service
from memory.context import assemble_user_context
from memory.policy import MemoryAccessDenied, resolve_access_scope


@pytest.fixture(params=['sqlite', 'postgres'])
async def authority_scope(request, tmp_path):
    url = os.environ.get('MEMORY_TEST_POSTGRES_URL') if request.param == 'postgres' else f'sqlite+aiosqlite:///{tmp_path}/memory-authority.db'
    if not url:
        pytest.skip('No isolated PostgreSQL authority database configured')
    await close_engine()
    engine = init_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    token = uuid4().hex[:12]
    owner, other, ws = f'mv2_owner_{token}', f'mv2_other_{token}', f'mv2_ws_{token}'
    p1, p2, foreign = f'mv2_p1_{token}', f'mv2_p2_{token}', f'mv2_foreign_{token}'
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add_all([User(id=uid, username=uid, created_at=now, updated_at=now) for uid in (owner, other)])
        await db.flush()
        db.add(Workspace(id=ws, name=ws, owner_user_id=owner, created_at=now, updated_at=now))
        await db.flush()
        for uid in (owner, other):
            db.add(WorkspaceMember(workspace_id=ws, user_id=uid, role='owner' if uid == owner else 'member', status='active', created_at=now, updated_at=now))
            await db.execute(update(User).where(User.id == uid).values(default_workspace_id=ws))
        db.add_all([Project(id=pid, user_id=uid, workspace_id=ws, name='same name', slug=pid, created_at=now, updated_at=now)
                    for pid, uid in ((p1, owner), (p2, owner), (foreign, other))])
    yield {'user_id': owner, 'workspace_id': ws, 'other': other, 'p1': p1, 'p2': p2, 'foreign': foreign}
    await close_engine()


def identity(scope):
    return {'user_id': scope['user_id'], 'workspace_id': scope['workspace_id']}


async def source_session(scope, body='I prefer concise Chinese replies.'):
    suffix = uuid4().hex[:10]
    sid, mid, pid = f'mv2_s_{suffix}', f'mv2_msg_{suffix}', f'mv2_part_{suffix}'
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(Session(id=sid, user_id=scope['user_id'], workspace_id=scope['workspace_id'], project_id=scope['p1'], created_at=now, updated_at=now))
        await db.flush()
        db.add(Message(id=mid, session_id=sid, user_id=scope['user_id'], role='user', created_at=now))
        await db.flush()
        db.add(Part(id=pid, message_id=mid, session_id=sid, user_id=scope['user_id'], type='text', data={'text': body}, created_at=now))
    return sid, mid, pid


async def test_candidate_owner_cannot_confirm_and_default_context_is_private(authority_scope):
    scope = authority_scope
    candidate = await service.write_memory(**identity(scope), scope='LONG_TERM', type='VOICE',
        value={'summary': 'model claimed confirmed'}, owner='USER_CONFIRMED', confidence=100)
    assert candidate['owner'] == 'SYSTEM_INFERRED'
    assert candidate['confirmation_status'] == 'PENDING'
    assert await service.list_active_memories(**identity(scope)) == []
    assert (await assemble_user_context(**identity(scope)))['context'] == ''
    manual = await service.create_note(**identity(scope), summary='actual personal preference')
    project = await service.create_note(**identity(scope), project_id=scope['p1'], summary='project private fact')
    assert [row['id'] for row in await service.list_active_memories(**identity(scope))] == [manual['id']]
    project_rows = await service.list_active_memories(**identity(scope), project_id=scope['p1'])
    assert {row['id'] for row in project_rows} == {manual['id'], project['id']}
    assert await service.list_active_memories(user_id=scope['other'], workspace_id=scope['workspace_id']) == []
    with pytest.raises(MemoryAccessDenied):
        await service.search_memories(**identity(scope), project_id=scope['foreign'])


async def test_cas_history_source_versions_outbox_and_request_replay(authority_scope):
    scope = authority_scope
    row = await service.create_note(**identity(scope), summary='Old answer', request_id='create-one')
    repeated = await service.create_note(**identity(scope), summary='Old answer', request_id='create-one')
    assert repeated['id'] == row['id']
    changed = await service.edit_note(**identity(scope), memory_id=row['id'], summary='New answer', expected_revision=1, request_id='correct-one')
    assert changed['revision'] == 2
    with pytest.raises(service.MemoryConflict):
        await service.edit_note(**identity(scope), memory_id=row['id'], summary='Stale answer', expected_revision=1)
    replay = await service.edit_note(**identity(scope), memory_id=row['id'], summary='New answer', expected_revision=1, request_id='correct-one')
    assert replay['revision'] == 2
    history = await service.get_history(**identity(scope), memory_id=row['id'])
    assert [version['summary'] for version in history] == ['New answer', 'Old answer']
    async with get_db_session() as db:
        links = (await db.scalars(select(MemorySourceLink).where(MemorySourceLink.memory_id == row['id'], MemorySourceLink.revision == 2))).all()
        assert sorted(link.relation for link in links) == ['SUPERSEDED', 'SUPPORTS']
        events = (await db.scalars(select(MemoryOutbox).where(MemoryOutbox.object_id == row['id']))).all()
        assert {event.revision for event in events} == {1, 2}
        assert len(events) == 2
    assert [fact['summary'] for fact in await service.list_active_memories(**identity(scope))] == ['New answer']
    suppressed = await service.write_memory(**identity(scope), scope='LONG_TERM', type='VOICE', value={'summary': 'Old answer'}, owner='SYSTEM_INFERRED')
    assert suppressed['status'] == 'SUPPRESSED'


async def test_forget_suppresses_late_worker_and_redacts_history(authority_scope):
    scope = authority_scope
    row = await service.create_note(**identity(scope), summary='Please forget this fact', fact_key='personal.fact')
    assert await service.delete_memory(**identity(scope), memory_id=row['id'], expected_revision=1, request_id='forget-one')
    assert await service.delete_memory(**identity(scope), memory_id=row['id'], expected_revision=1, request_id='forget-one')
    assert await service.list_active_memories(**identity(scope)) == []
    assert all(not version['body_available'] and not version['value'] for version in await service.get_history(**identity(scope), memory_id=row['id']))
    assert all(source['body'] is None for source in await service.get_sources(**identity(scope), memory_id=row['id']))
    async with get_db_session() as db:
        access = await resolve_access_scope(db, **identity(scope))
        assert await service.create_candidate_in_session(db, access=access, type='PREFERENCE', summary='Reworded forgotten topic', fact_key='personal.fact') is None
        assert await service.create_candidate_in_session(db, access=access, type='PREFERENCE', summary='Please forget this fact') is None
        assert await db.scalar(select(MemoryTombstone.id).where(MemoryTombstone.object_id == row['id']))
    cleanup = await service.cleanup_status(**identity(scope), memory_id=row['id'])
    assert cleanup['stopped'] and cleanup['status'] == 'stopped_cleanup_pending'


async def test_card_expiry_and_revoked_sources_cannot_be_confirmed(authority_scope):
    scope = authority_scope
    proposal = await service.propose_note(**identity(scope), summary='expired proposal')
    async with get_db_session() as db:
        await db.execute(update(UserMemory).where(UserMemory.id == proposal['id']).values(ttl=datetime.now(timezone.utc) - timedelta(seconds=1)))
    assert await service.confirm_note(**identity(scope), proposal_id=proposal['id'], expected_revision=1) is None
    sid, mid, pid = await source_session(scope)
    proposal = await service.propose_note(**identity(scope), project_id=scope['p1'], session_id=sid, summary='source-bound proposal')
    async with get_db_session() as db:
        await db.execute(update(Session).where(Session.id == sid).values(is_deleted=True))
    with pytest.raises(service.MemoryConflict):
        await service.confirm_note(**identity(scope), proposal_id=proposal['id'], expected_revision=1)
    async with get_db_session() as db:
        assert (await db.get(UserMemory, proposal['id'])).revision == 1


async def test_source_forget_is_bounded_and_keeps_original_chat(authority_scope):
    scope = authority_scope
    sid, mid, pid = await source_session(scope)
    proposal = await service.propose_note(**identity(scope), project_id=scope['p1'], session_id=sid, summary='concise replies')
    active = await service.confirm_note(**identity(scope), proposal_id=proposal['id'], expected_revision=1)
    sources = await service.get_sources(**identity(scope), memory_id=active['id'])
    chosen = next(source for source in sources if source['source_kind'] == 'user_statement')
    with pytest.raises(MemoryAccessDenied):
        await service.forget_memory(**identity(scope), memory_id=active['id'], expected_revision=2, mode='sources', source_ids=['foreign-source'])
    result = await service.forget_memory(**identity(scope), memory_id=active['id'], expected_revision=2, mode='sources', source_ids=[chosen['id']])
    assert result['source_ids'] == [chosen['id']] and result['original_chat_deleted'] is False
    async with get_db_session() as db:
        assert (await db.get(Part, pid)).data['text'] == 'I prefer concise Chinese replies.'
        assert (await db.get(MemorySource, chosen['id'])).body is None
        assert (await db.get(UserMemory, active['id'])).value == {}
        assert not (await db.scalars(select(MemoryRevision).where(MemoryRevision.memory_id == active['id']))).first().value
    assert await service.list_active_memories(**identity(scope), project_id=scope['p1']) == []


async def test_authority_effects_roll_back_with_job_transaction(authority_scope):
    scope = authority_scope
    with pytest.raises(RuntimeError, match='crash before job completion'):
        async with get_db_session() as db:
            access = await resolve_access_scope(db, **identity(scope))
            await service.create_candidate_in_session(db, access=access, type='PREFERENCE', summary='atomic candidate',
                sources=[{'source_kind': 'user_statement', 'body': 'atomic source'}], idempotency_key='rollback-job')
            raise RuntimeError('crash before job completion')
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(UserMemory).where(UserMemory.user_id == scope['user_id'])) == 0
        assert await db.scalar(select(func.count()).select_from(MemorySource).where(MemorySource.user_id == scope['user_id'])) == 0
        assert await db.scalar(select(func.count()).select_from(MemoryOutbox).where(MemoryOutbox.user_id == scope['user_id'])) == 0


async def test_concurrent_sessions_preserve_cas_and_unique_keyed_creation(authority_scope):
    scope = authority_scope
    row = await service.create_note(**identity(scope), summary='initial')
    async def correct(summary):
        try:
            return await service.edit_note(**identity(scope), memory_id=row['id'], summary=summary, expected_revision=1)
        except service.MemoryConflict:
            return 'conflict'
    results = await asyncio.gather(correct('session A correction'), correct('session B correction'))
    assert results.count('conflict') == 1
    winners = [result for result in results if isinstance(result, dict)]
    assert len(winners) == 1 and winners[0]['revision'] == 2
    async def candidate(summary):
        async with get_db_session() as db:
            access = await resolve_access_scope(db, **identity(scope))
            created = await service.create_candidate_in_session(db, access=access, type='PREFERENCE', summary=summary,
                sources=[{'source_kind':'user_statement','body':'shared evidence'}], fact_key='same.topic')
            return created.id
    created = await asyncio.gather(candidate('first candidate'), candidate('concurrent candidate'))
    assert created[0] == created[1]
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(MemoryRevision).where(MemoryRevision.memory_id == created[0])) == 1
        assert await db.scalar(select(func.count()).select_from(MemoryOutbox).where(MemoryOutbox.object_id == created[0])) == 1


async def test_concurrent_manual_command_is_idempotent_and_rejects_topic_overwrite(authority_scope):
    scope = authority_scope
    async def create():
        return await service.create_note(**identity(scope), summary='Explicitly keep this preference', request_id='manual-once', fact_key='reply.preference')
    first, second = await asyncio.gather(create(), create())
    assert first['id'] == second['id']
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(MemoryRevision).where(MemoryRevision.memory_id == first['id'])) == 1
        assert await db.scalar(select(func.count()).select_from(MemoryOutbox).where(MemoryOutbox.object_id == first['id'])) == 1
    with pytest.raises(service.MemoryConflict):
        await service.create_note(**identity(scope), summary='Overwriting without revision is forbidden', fact_key='reply.preference')
    assert (await service.list_active_memories(**identity(scope)))[0]['summary'] == 'Explicitly keep this preference'


async def test_source_forget_serializes_with_candidate_admission(authority_scope, monkeypatch):
    scope = authority_scope
    note = await service.create_note(**identity(scope), summary='Frozen original source with two facts')
    source = (await service.get_sources(**identity(scope), memory_id=note['id']))[0]
    entered, release, forget_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_store, original_lock = service._store_source, service.lock_memory_authority
    async def pause_after_source_validation(db, access, data):
        result = await original_store(db, access, data)
        entered.set()
        await release.wait()
        return result
    async def observe_lock(db, *, user_id):
        if asyncio.current_task().get_name() == 'source-forget-race':
            forget_entered.set()
        return await original_lock(db, user_id=user_id)
    monkeypatch.setattr(service, '_store_source', pause_after_source_validation)
    monkeypatch.setattr(service, 'lock_memory_authority', observe_lock)
    async def candidate():
        async with get_db_session() as db:
            access = await resolve_access_scope(db, **identity(scope))
            row = await service.create_candidate_in_session(db, access=access, type='PREFERENCE', summary='Second extracted fact',
                sources=[{'id':source['id'],'source_kind':'manual','body':source['body']}], fact_key='second.topic')
            return row.id
    admission = asyncio.create_task(candidate(), name='candidate-race')
    forgetting = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        forgetting = asyncio.create_task(service.forget_memory(**identity(scope), memory_id=note['id'], mode='sources',
            source_ids=[source['id']], expected_revision=1), name='source-forget-race')
        await asyncio.wait_for(forget_entered.wait(), timeout=5)
        assert not forgetting.done()
        release.set()
        candidate_id, forgotten = await asyncio.gather(admission, forgetting)
        assert set(forgotten['memory_ids']) == {note['id'], candidate_id}
        async with get_db_session() as db:
            assert (await db.get(UserMemory, candidate_id)).deleted_at is not None
            assert (await db.get(MemorySource, source['id'])).status == 'DELETED'
            assert await db.scalar(select(func.count()).select_from(UserMemory).where(UserMemory.user_id == scope['user_id'],
                UserMemory.status.in_(['ACTIVE','CANDIDATE']))) == 0
    finally:
        release.set()
        await asyncio.gather(admission, *([forgetting] if forgetting else []), return_exceptions=True)


async def test_forgetting_one_shared_fact_suppresses_raw_body_keeps_other_fact(authority_scope):
    scope = authority_scope
    original = '我的保密代号是海风；回复语言使用中文。'
    sid, mid, pid = await source_session(scope, original)
    ids = []
    async with get_db_session() as db:
        access = await resolve_access_scope(db, **identity(scope), project_id=scope['p1'])
        for key, summary in [('secret.codename', '我的保密代号是海风'), ('reply.language', '回复语言使用中文')]:
            row = await service.create_candidate_in_session(db, access=access, type='PREFERENCE', summary=summary, fact_key=key,
                sources=[{'body': original, 'source_kind': 'user_statement', 'session_id':sid, 'message_id':mid,
                          'turn_id':mid, 'part_id':pid, 'branch_id':'root'}])
            ids.append(row.id)
    for memory_id in ids:
        await service.confirm_note(**identity(scope), proposal_id=memory_id, expected_revision=1)
    await service.delete_memory(**identity(scope), memory_id=ids[0], expected_revision=2)
    async with get_db_session() as db:
        access = await resolve_access_scope(db, **identity(scope), project_id=scope['p1'])
        source = await db.scalar(select(MemorySource).where(MemorySource.session_id == sid))
        assert await service.source_is_available(db, access, source)
        assert not await service.source_body_is_available(db, access, source)
        assert (await service.read_source_in_scope(db, access=access, source_id=source.id)) == {'available':False, 'reason_code':'unavailable'}
        assert await service.memory_sources_available(db, access, await db.get(UserMemory, ids[1]))
        assert (await db.get(Part, pid)).data['text'] == original
        from core.config import MemoryConfig
        from memory.retrieval import authorized_documents
        documents = await authorized_documents(db, access, MemoryConfig())
        retained = next(doc for doc in documents if doc.kind == 'memory' and doc.id == ids[1])
        assert any(ref['id'] == source.id for ref in retained.sources)
        assert not any(doc.kind == 'source' and doc.id == source.id for doc in documents)
        assert not any('海风' in doc.text for doc in documents)
    sources = await service.get_sources(**identity(scope), memory_id=ids[1])
    raw = next(item for item in sources if item['source_kind'] == 'user_statement')
    assert raw['body'] is None and not raw['body_available']
    assert next(item for item in sources if item['source_kind'] == 'user_confirmation')['body'] == '回复语言使用中文'
    assert [row['summary'] for row in await service.list_active_memories(**identity(scope), project_id=scope['p1'])] == ['回复语言使用中文']


async def test_cleanup_requires_exclusive_source_and_wiki_derivatives(authority_scope):
    from db.models.memory_wiki import MemoryWikiPage, MemoryWikiDependency
    scope, now = authority_scope, datetime.now(timezone.utc)
    note = await service.create_note(**identity(scope), summary='Clean the derivative copies too')
    page_id = 'cleanup_page_' + uuid4().hex[:12]
    async with get_db_session() as db:
        source_id = await db.scalar(select(MemorySourceLink.source_id).where(MemorySourceLink.memory_id == note['id']))
        db.add(MemoryWikiPage(id=page_id, target_identity=uuid4().hex, user_id=scope['user_id'], workspace_id=scope['workspace_id'],
            project_id=None, slug='cleanup', title='Cleanup', revision=1, body='Derivative copy', content_hash='a'*64,
            status='PUBLISHED', source_manifest=[], memory_manifest=[{'id':note['id'],'revision':1}], paragraphs=[],
            acl_epoch=1, policy_version='test', model='synthetic', input_hash='b'*64, candidate_id='synthetic', created_at=now, updated_at=now))
        db.add(MemoryWikiDependency(id=uuid4().hex, page_id=page_id, page_revision=1, object_kind='memory', object_id=note['id'],
            object_revision=1, content_hash='c'*64))
        for kind, object_id in [('source',source_id), ('wiki',page_id)]:
            db.add(MemoryIndexState(id=uuid4().hex, object_kind=kind, object_id=object_id, user_id=scope['user_id'],
                workspace_id=scope['workspace_id'], project_id=None, index_generation='memory-v1',
                desired_revision=1, indexed_revision=1, chunk_ids=['synthetic-copy'], status='INDEXED', updated_at=now))
    await service.delete_memory(**identity(scope), memory_id=note['id'], expected_revision=1)
    async with get_db_session() as db:
        tombstone = await db.scalar(select(MemoryTombstone).where(MemoryTombstone.object_id == note['id']))
        tombstone.purge_status = 'SUCCEEDED'
        await db.execute(update(MemoryOutbox).where(MemoryOutbox.object_id == note['id']).values(status='SUCCEEDED'))
        await db.execute(update(MemoryIndexState).where(MemoryIndexState.object_id == note['id']).values(status='DELETED'))
    partial = await service.cleanup_status(**identity(scope), memory_id=note['id'])
    assert partial['status'] == 'stopped_cleanup_pending' and partial['object_purge_status'] == 'SUCCEEDED'
    assert partial['purge_status'] == 'PENDING'
    async with get_db_session() as db:
        await db.execute(update(MemoryOutbox).where(MemoryOutbox.object_id.in_([source_id,page_id])).values(status='SUCCEEDED'))
        await db.execute(update(MemoryIndexState).where(MemoryIndexState.object_id.in_([source_id,page_id])).values(status='DELETED'))
    assert (await service.cleanup_status(**identity(scope), memory_id=note['id']))['status'] == 'cleaned'


async def test_new_explicit_manual_statement_has_independent_source_after_fact_forget(authority_scope):
    scope = authority_scope
    first = await service.create_note(**identity(scope), summary='Explicitly remember this again')
    old_source = (await service.get_sources(**identity(scope), memory_id=first['id']))[0]
    await service.delete_memory(**identity(scope), memory_id=first['id'], expected_revision=1)
    second = await service.create_note(**identity(scope), summary='Explicitly remember this again')
    new_source = (await service.get_sources(**identity(scope), memory_id=second['id']))[0]
    assert new_source['id'] != old_source['id'] and new_source['body_available']
    assert new_source['body'] == 'Explicitly remember this again'
    assert [row['id'] for row in await service.list_active_memories(**identity(scope))] == [second['id']]


def test_sqlite_legacy_migration_marks_candidates_and_protects_downgrade():
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    migration = importlib.import_module('db.migrations.versions.m1a2b3c4d5e6_memory_authority')
    legacy_columns = [column._copy() for column in UserMemory.__table__.columns if column.name not in {
        'revision','visibility','confirmation_status','confirmation_actor_id','fact_key','fact_identity','content_hash',
        'occurred_at','recorded_at','valid_from','valid_to','deleted_at','supersedes_id','policy_version','acl_epoch'}]
    meta = sa.MetaData()
    # Resolve only the original table's foreign keys; memory tables reference it.
    sa.Table('users', meta, sa.Column('id', sa.String(64), primary_key=True))
    sa.Table('workspaces', meta, sa.Column('id', sa.String(64), primary_key=True))
    legacy = sa.Table('user_memories', meta, *legacy_columns)
    engine = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as connection:
        meta.create_all(connection)
        now = datetime.now(timezone.utc)
        for status, owner in [('CANDIDATE', 'USER_CONFIRMED'), ('ACTIVE', 'USER_CONFIRMED'), ('DEPRECATED', 'SYSTEM_INFERRED')]:
            connection.execute(legacy.insert().values(id=status, user_id='u', workspace_id='w', scope='LONG_TERM', type='VOICE',
                value={'summary': status}, evidence={}, owner=owner, status=status, created_at=now, updated_at=now))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            rows = connection.execute(sa.text('SELECT id, confirmation_status, content_hash, deleted_at FROM user_memories')).mappings().all()
            assert {row['id']: row['confirmation_status'] for row in rows} == {'CANDIDATE':'LEGACY_CANDIDATE','ACTIVE':'CONFIRMED','DEPRECATED':'PENDING'}
            assert all(row['content_hash'] for row in rows)
            assert connection.execute(sa.text('SELECT count(*) FROM memory_tombstones')).scalar() == 1
            assert connection.execute(sa.text('SELECT count(*) FROM memory_revisions')).scalar() == 3
            migration.downgrade()
            assert 'revision' not in {column['name'] for column in sa.inspect(connection).get_columns('user_memories')}
            migration.upgrade()
            connection.execute(sa.text("UPDATE memory_revisions SET reason='user_corrected' WHERE memory_id='ACTIVE'"))
            with pytest.raises(RuntimeError, match='retain schema'):
                migration.downgrade()
