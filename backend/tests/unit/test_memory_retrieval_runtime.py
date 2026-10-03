"""Runtime contracts with real SQL and injected, isolated provider/index adapters.

No test contacts a provider or the live vector service. PostgreSQL runs use the
same opt-in isolated database as the authority suite and unique actors/generations.
"""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.orm import with_loader_criteria

from core.config import MemoryConfig, OpenBoxConfig
from core.identifier import ascending
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_v2 import MemoryIndexState, MemoryOutbox, MemorySource, MemoryTombstone
from db.models.workspace import WorkspaceMember
from memory import outbox, reconcile, retrieval, routing, service
from memory.index.base import IndexHit
from memory.index.lexical import bm25, tokenize
from memory.index.qdrant import config_hash
from memory.policy import MemoryAccessDenied, MemoryAccessScope, resolve_access_scope
from memory.providers.common import MemoryProviderError
from memory.providers.jev import validate_response
from memory.redaction import redact_value
from tests.unit.test_memory_authority_v2 import authority_scope, identity, source_session  # noqa: F401


class FakeEmbedding:
    def __init__(self, *, callback=None, error=None):
        self.callback, self.error, self.calls = callback, error, []

    async def embed(self, texts):
        self.calls.append(list(texts))
        if self.callback:
            await self.callback()
        if self.error:
            raise MemoryProviderError(self.error)
        return [[0.1] * 64 for _ in texts], {'input_tokens': 3}


class FakeIndex:
    def __init__(self, config, *, hits=(), callback=None):
        self.fingerprint = config_hash(config)
        self.hits, self.callback = list(hits), callback
        self.points, self.calls = {}, []

    async def search(self, vector, scope, *, kind, limit):
        self.calls.append(('search', kind, scope.user_id, scope.project_id))
        return [hit for hit in self.hits if hit.kind == kind][:limit]

    async def upsert_version(self, document, vectors, chunks):
        self.calls.append(('upsert', document.kind, document.id, document.revision))
        if self.callback:
            await self.callback()
        key = (document.kind, document.id, document.revision)
        ids = [f'{document.id}:{document.revision}:{i}' for i, _ in enumerate(chunks)]
        self.points[key] = ids
        return ids

    async def delete_revision(self, kind, object_id, revision):
        self.calls.append(('delete_revision', kind, object_id, revision))
        self.points.pop((kind, object_id, revision), None)

    async def delete_object_versions(self, kind, object_id, *, keep_revision=None):
        self.calls.append(('delete_object_versions', kind, object_id, keep_revision))
        for key in list(self.points):
            if key[:2] == (kind, object_id) and key[2] != keep_revision:
                self.points.pop(key)

    async def point_ids(self, kind, object_id):
        return [point for key, points in self.points.items() if key[:2] == (kind, object_id) for point in points]


@pytest.fixture
async def runtime_env(authority_scope, monkeypatch):
    scope = authority_scope
    config = MemoryConfig(index_sync=True, retrieval_v2=True, rerank=True, route_jev=True,
        allowed_user_ids=[scope['user_id']], embedding_dimensions=64, index_generation='rt-' + uuid4().hex[:20])
    monkeypatch.setattr('core.config.get_config', lambda: OpenBoxConfig(memory=config))
    async def accept_relevant_test_documents(query, texts, settings):
        return [(position, .9) for position, _ in enumerate(texts)], {'input_tokens': 1}
    monkeypatch.setattr('memory.rerank.rerank', accept_relevant_test_documents)
    # Cleanup intentionally ignores rollout allowlists. Keep this fake-worker
    # harness in its newly created workspace so an isolated shared PG database's
    # other synthetic/browser runs are never claimed or acknowledged here.
    @asynccontextmanager
    async def scoped_worker_session():
        async with get_db_session() as db:
            def fixture_scope(statement):
                statement.statement = statement.statement.options(*[with_loader_criteria(
                    model, model.workspace_id == scope['workspace_id'], include_aliases=True)
                    for model in (MemoryOutbox, MemoryIndexState, MemoryTombstone, UserMemory, MemorySource)])
            event.listen(db.sync_session, 'do_orm_execute', fixture_scope)
            try:
                yield db
            finally:
                event.remove(db.sync_session, 'do_orm_execute', fixture_scope)
    monkeypatch.setattr(outbox, 'get_db_session', scoped_worker_session)
    monkeypatch.setattr(reconcile, 'get_db_session', scoped_worker_session)
    indexes = {}
    def index_factory(settings, generation=None):
        generation = generation or settings.index_generation
        return indexes.setdefault(generation, FakeIndex(settings))
    monkeypatch.setattr(outbox, 'QdrantMemoryIndex', index_factory)
    monkeypatch.setattr(reconcile, 'QdrantMemoryIndex', index_factory)
    return scope, config, indexes, index_factory(config)


def valid_route():
    return {'model':'jev-1.13.0', 'answers': {
        'memory_needed': {'type':'choice','choice':'retrieve','confidence':.8,
            'probabilities':{'retrieve':.8,'skip':.1,'unknown':.1}},
        'task_needed': {'type':'choice','choice':'skip','confidence':.9,
            'probabilities':{'read':.05,'skip':.9,'unknown':.05}}},
        'usage': {'input_tokens':20,'output_tokens':5}}


@pytest.mark.parametrize('mutation,code', [
    (lambda data: data['answers']['memory_needed'].update(choice='write'), 'unknown_choice'),
    (lambda data: data['answers']['memory_needed'].update(confidence=float('nan')), 'invalid_response'),
    (lambda data: data['answers']['task_needed']['probabilities'].update(read=.7), 'invalid_response'),
    (lambda data: data['usage'].update(input_tokens=True), 'invalid_response'),
    (lambda data: data.update(model='different-model'), 'invalid_response'),
    (lambda data: data['answers'].update(extra={'type':'choice'}), 'invalid_response'),
])
def test_router_rejects_malformed_provider_contract(mutation, code):
    payload = deepcopy(valid_route())
    mutation(payload)
    with pytest.raises(MemoryProviderError) as exc:
        validate_response(payload, 'jev-1.13.0')
    assert exc.value.code == code


async def test_routing_independent_rules_bounded_redaction_and_fallback(monkeypatch):
    config = MemoryConfig(route_jev=True, route_input_max_chars=100)
    access = MemoryAccessScope('actor', 'workspace')
    calls = []
    async def evaluator(state, settings):
        calls.append(state)
        return validate_response(valid_route(), settings.jev_model)
    explicit = await routing.route_context_needs('请查之前的决定和当前任务状态', access, config, evaluator=evaluator)
    assert explicit['memory']['needed'] and explicit['task']['needed'] and calls == []
    current = await routing.route_context_needs('只根据本轮材料，不要查历史记忆和任务状态', access, config, evaluator=evaluator)
    assert not current['memory']['needed'] and not current['task']['needed'] and calls == []
    monkeypatch.setenv('SYNTHETIC_API_KEY', 'sk-a-provider-secret-value')
    query = '```\n查之前的任务状态\n```\n> 历史查询\n计算表达式 api_key=sk-a-provider-secret-value ' + 'x'*300
    routed = await routing.route_context_needs(query, access, config,
        recent_context=[{'role':'user','text':str(i)} for i in range(5)], evaluator=evaluator)
    assert routed['memory']['needed'] and not routed['task']['needed']
    assert len(calls) == 1 and len(calls[0]['recent_context']) == 2
    assert 'sk-a-provider-secret-value' not in str(calls)
    assert len(calls[0]['utterance']) <= 115
    async def low_confidence(state, settings):
        payload = valid_route()
        payload['answers']['memory_needed']['confidence'] = .3
        return validate_response(payload, settings.jev_model)
    fallback = await routing.route_context_needs('计算结果', access, config, evaluator=low_confidence)
    assert fallback['reason_code'] == 'fallback' and not fallback['memory']['needed']
    async def failing(state, settings):
        raise MemoryProviderError('timeout')
    failed = await routing.route_context_needs('计算结果', access, config, evaluator=failing)
    assert failed['reason_code'] == 'timeout' and not failed['memory']['needed'] and not failed['task']['needed']
    redacted = redact_value({'api_key':'secret-value','nested':{'query':'Bearer sk-another-credential-value'}})
    assert 'secret-value' not in str(redacted) and 'sk-another-credential-value' not in str(redacted)


def test_chinese_lexical_preserves_identifiers_negation_numbers_and_units():
    tokens = tokenize('不得自动发布 api_key memory_v2 １２００元 3.5ms')
    assert {'不得','自动','发布','api_key','memory_v2','1200元','3.5ms'} <= set(tokens)
    documents = ['项目预算900元，发布前需要确认', '项目预算1200元，不得自动发布，api_key 不能记录', '天气晴朗']
    ranking = bm25('不得自动发布，预算1200元', documents)
    assert ranking[0][0] == 1 and 2 not in [index for index, _ in ranking]


async def test_dense_failure_uses_chinese_lexical_and_foreign_hits_are_rejected(runtime_env):
    scope, config, _, index = runtime_env
    visible = await service.create_note(**identity(scope), summary='项目预算1200元，不得自动发布')
    foreign = await service.create_note(user_id=scope['other'], workspace_id=scope['workspace_id'], summary='外国用户的预算1200元')
    other_project = await service.create_note(**identity(scope), project_id=scope['p2'], summary='另一个项目的预算1200元')
    candidate = await service.write_memory(**identity(scope), scope='LONG_TERM', type='PREFERENCE', owner='SYSTEM_INFERRED',
        value={'summary':'未经确认的预算1200元'})
    fallback = await retrieval.search_memory(query='预算1200元不得发布', **identity(scope), config=config,
        embedding=FakeEmbedding(error='timeout'), index=index)
    assert fallback['items'][0]['id'] == visible['id'] and 'timeout' in fallback['degraded_reasons']
    index.hits = [IndexHit('memory', row['id'], 1, .99) for row in (foreign, other_project, candidate, visible)]
    dense = await retrieval.search_memory(query='预算1200元', **identity(scope), config=config, embedding=FakeEmbedding(), index=index)
    assert {item['id'] for item in dense['candidates'] if item['kind'] == 'memory'} == {visible['id']}
    assert not any(text in str(dense) for text in ['外国用户','另一个项目','未经确认'])


async def test_correction_and_pending_sources_never_reenter_raw_retrieval(runtime_env):
    scope, config, _, _ = runtime_env
    sid, _, _ = await source_session(scope, '预算1200元，使用中文答复')
    proposal = await service.propose_note(**identity(scope), project_id=scope['p1'], session_id=sid, summary='预算1200元')
    async with get_db_session() as db:
        access = await resolve_access_scope(db, **identity(scope), project_id=scope['p1'])
        assert await retrieval.authorized_documents(db, access, config) == []
    await service.confirm_note(**identity(scope), proposal_id=proposal['id'], expected_revision=1)
    await service.edit_note(**identity(scope), memory_id=proposal['id'], expected_revision=2, summary='预算900元')
    async with get_db_session() as db:
        access = await resolve_access_scope(db, **identity(scope), project_id=scope['p1'])
        docs = await retrieval.authorized_documents(db, access, config)
    assert any(doc.kind == 'memory' and doc.text == '预算900元' for doc in docs)
    assert not any('1200' in doc.text for doc in docs)


async def test_cross_channel_tie_is_reranked_before_evidence_deduplication(runtime_env, monkeypatch):
    from memory.index.base import DocumentSnapshot
    from memory.redaction import text_hash
    scope, config, _, index = runtime_env
    config.wiki = True
    await service.create_note(**identity(scope), summary='项目的沟通约定：先结论后理由，预算900元')
    wiki_text = '# 沟通约定与演示预算\n先结论后理由，预算上限900元。'
    async def derived_page(db, access, settings, *, only=None):
        base = await retrieval.authorized_documents(db, access, replace_config_without_wiki(settings))
        memory = next(doc for doc in base if doc.kind == 'memory')
        page = DocumentSnapshot('wiki', 'synthetic-wiki', 1, wiki_text, memory.user_id,
            memory.workspace_id, memory.project_id, access.acl_epoch, text_hash(wiki_text), memory.sources)
        return [page] if only is None or ('wiki', page.id) in only else []
    def replace_config_without_wiki(settings):
        return settings.model_copy(update={'wiki': False})
    calls = []
    async def rank(query, texts, settings):
        calls.append(list(texts))
        chosen = texts.index(wiki_text)
        return [(chosen, .99)] + [(i, .2) for i in range(len(texts)) if i != chosen], {'input_tokens': 9}
    async def page_text(db, access):
        return [('synthetic-wiki', wiki_text)]
    monkeypatch.setattr('memory.wiki.service.authorized_wiki_documents', derived_page)
    # The keyword pool reads page text from SQL; feed it the same synthetic page.
    monkeypatch.setattr('memory.retrieval._wiki_pool', page_text)
    monkeypatch.setattr('memory.rerank.rerank', rank)
    result = await retrieval.search_memory(query='沟通约定与演示预算', **identity(scope), config=config,
        embedding=FakeEmbedding(), index=index)
    assert len(calls) == 1 and result['rerank']['called']
    assert result['items'][0]['kind'] == 'wiki'
    assert len(result['items']) == 1


async def test_low_relevance_small_candidate_set_returns_no_evidence(runtime_env, monkeypatch):
    scope, config, _, index = runtime_env
    note = await service.create_note(**identity(scope), summary='共享日历：每周五下午同步项目状态')
    index.hits = [IndexHit('memory', note['id'], 1, .31)]
    calls = []
    async def irrelevant(query, texts, settings):
        calls.append(list(texts))
        return [(position, .1) for position, _ in enumerate(texts)], {}
    monkeypatch.setattr('memory.rerank.rerank', irrelevant)
    result = await retrieval.search_memory(query='容器日志轮转如何配置', **identity(scope), config=config,
        embedding=FakeEmbedding(), index=index)
    assert len(calls) == 1 and 0 < len(result['candidates']) <= 4
    assert result['items'] == [] and result['degraded_reasons'] == []
    assert result['rerank']['filter_applied'] is True
    assert result['rerank']['filtered_count'] == len(result['candidates'])


async def test_relevance_filter_cannot_backfill_from_unscored_candidates(runtime_env, monkeypatch):
    scope, config, _, index = runtime_env
    config.rerank_max_documents = 2
    for body in ('系统对象存储必须加密', '系统每周四发布', '系统指标保留90天'):
        await service.create_note(**identity(scope), summary=body)
    calls = []
    async def only_one_relevant(query, texts, settings):
        calls.append(list(texts))
        return [(0, .9)] + [(position, .1) for position in range(1, len(texts))], {}
    monkeypatch.setattr('memory.rerank.rerank', only_one_relevant)
    result = await retrieval.search_memory(query='系统配置', **identity(scope), config=config,
        embedding=FakeEmbedding(), index=index)
    assert len(calls[0]) == 2 and len(result['candidates']) > 2
    assert len(result['items']) == 1 and result['items'][0]['rerank_score'] == .9
    assert result['rerank']['filtered_count'] == len(result['candidates']) - 1


@pytest.mark.parametrize('threshold, expected', [(.5, False), (.3, True)])
async def test_relevance_cutoff_is_configurable_and_inclusive(runtime_env, monkeypatch, threshold, expected):
    scope, config, _, index = runtime_env
    config.rerank_min_score = threshold
    await service.create_note(**identity(scope), summary='项目文档使用英文')
    async def borderline(query, texts, settings):
        return [(position, .3) for position, _ in enumerate(texts)], {}
    monkeypatch.setattr('memory.rerank.rerank', borderline)
    result = await retrieval.search_memory(query='文档语言', **identity(scope), config=config,
        embedding=FakeEmbedding(), index=index)
    assert bool(result['items']) is expected


async def test_rerank_failure_preserves_explicit_hybrid_fallback(runtime_env, monkeypatch):
    scope, config, _, index = runtime_env
    await service.create_note(**identity(scope), summary='需求说明使用中文')
    async def unavailable(query, texts, settings):
        raise MemoryProviderError('timeout')
    monkeypatch.setattr('memory.rerank.rerank', unavailable)
    result = await retrieval.search_memory(query='需求说明', **identity(scope), config=config,
        embedding=FakeEmbedding(), index=index)
    assert result['items'] and 'rerank_timeout' in result['degraded_reasons']
    assert result['rerank']['filter_applied'] is False


async def test_zero_cutoff_retains_optional_reranking(runtime_env, monkeypatch):
    scope, config, _, index = runtime_env
    config.rerank_min_score = 0
    await service.create_note(**identity(scope), summary='需求说明使用中文')
    async def unnecessary(query, texts, settings):
        pytest.fail('Small unambiguous candidates do not need optional reranking')
    monkeypatch.setattr('memory.rerank.rerank', unnecessary)
    result = await retrieval.search_memory(query='需求说明', **identity(scope), config=config,
        embedding=FakeEmbedding(), index=index)
    assert result['items'] and result['rerank']['called'] is False


async def test_sql_source_timestamp_obeys_local_day_in_sqlite_and_postgres(runtime_env):
    scope, config, _, index = runtime_env
    body = '项目预算900元'
    sid, mid, pid = await source_session(scope, body)
    async with get_db_session() as db:
        access = await resolve_access_scope(db, **identity(scope), project_id=scope['p1'])
        row = await service.create_candidate_in_session(db, access=access, type='PROJECT_CONTEXT', summary=body,
            sources=[{'source_kind':'user_statement', 'body':body, 'session_id':sid, 'turn_id':mid,
                'message_id':mid, 'part_id':pid, 'branch_id':'root',
                'occurred_at':datetime(2026,10,1,15,59,tzinfo=timezone.utc)}])
        memory_id = row.id
    await service.confirm_note(**identity(scope), proposal_id=memory_id, expected_revision=1)
    previous_day = await retrieval.search_memory(query='2026-10-01 的预算', **identity(scope),
        project_id=scope['p1'], config=config, index=index, embedding=FakeEmbedding())
    assert previous_day['items'] and previous_day['time_context']['timezone'] == 'Asia/Shanghai'
    next_day = await retrieval.search_memory(query='2026-10-02 的预算', **identity(scope),
        project_id=scope['p1'], config=config, index=index, embedding=FakeEmbedding())
    assert next_day['items'] == []


async def test_authorization_is_rechecked_before_rerank_and_after_it(runtime_env, monkeypatch):
    scope, config, _, index = runtime_env
    doomed = await service.create_note(**identity(scope), summary='请用中文答复，引用会在embedding等待时删除')
    calls = []
    async def delete_while_embedding():
        await service.delete_memory(**identity(scope), memory_id=doomed['id'], expected_revision=1)
    async def no_rerank(query, texts, settings):
        calls.append(texts)
        pytest.fail('A deleted snapshot must never reach rerank')
    monkeypatch.setattr('memory.rerank.rerank', no_rerank)
    missing = await retrieval.search_memory(query='中文答复', **identity(scope), config=config, force_rerank=True,
        embedding=FakeEmbedding(callback=delete_while_embedding), index=index)
    assert missing['items'] == missing['candidates'] == [] and calls == []
    surviving = await service.create_note(**identity(scope), summary='中文答复，rerank期间会遗忘')
    async def delete_while_reranking(query, texts, settings):
        calls.append(list(texts))
        await service.delete_memory(**identity(scope), memory_id=surviving['id'], expected_revision=1)
        return [(position, .9) for position, _ in enumerate(texts)], {'input_tokens':5}
    monkeypatch.setattr('memory.rerank.rerank', delete_while_reranking)
    late = await retrieval.search_memory(query='中文答复', **identity(scope), config=config, force_rerank=True,
        embedding=FakeEmbedding(), index=index)
    assert calls and late['items'] == late['candidates'] == []
    assert 'rerank期间会遗忘' not in str(late)


async def test_membership_revocation_during_embedding_prevents_provider_followup(runtime_env, monkeypatch):
    scope, config, _, index = runtime_env
    await service.create_note(**identity(scope), summary='中文回复')
    async def revoke():
        async with get_db_session() as db:
            await db.execute(update(WorkspaceMember).where(WorkspaceMember.user_id == scope['user_id'],
                WorkspaceMember.workspace_id == scope['workspace_id']).values(status='removed'))
    async def forbidden(*args):
        pytest.fail('Revoked workspace data must not be sent to another provider')
    monkeypatch.setattr('memory.rerank.rerank', forbidden)
    with pytest.raises(MemoryAccessDenied):
        await retrieval.search_memory(query='中文', **identity(scope), config=config, force_rerank=True,
            embedding=FakeEmbedding(callback=revoke), index=index)


async def test_outbox_late_upsert_compensates_only_its_version_and_cleans_derivatives(runtime_env):
    scope, config, _, index = runtime_env
    note = await service.create_note(**identity(scope), summary='A late write cannot restore forgotten memory')
    lease = await outbox.claim_outbox('synthetic-worker', config)
    assert lease.object_id == note['id']
    async def forget_before_index_returns():
        await service.delete_memory(**identity(scope), memory_id=note['id'], expected_revision=1)
        index.points[('memory',note['id'],2)] = ['newer-version-synthetic']
    index.callback = forget_before_index_returns
    assert await outbox.deliver_outbox(lease, config, index=index, embedding=FakeEmbedding())
    assert ('memory',note['id'],1) not in index.points
    assert index.points[('memory',note['id'],2)] == ['newer-version-synthetic']
    assert ('delete_revision','memory',note['id'],1) in index.calls
    assert not any(call[0] == 'delete_object_versions' for call in index.calls)
    index.callback = None
    assert (await service.cleanup_status(**identity(scope), memory_id=note['id']))['status'] == 'stopped_cleanup_pending'
    worker = outbox.MemoryIndexWorker(config, index=index, embedding=FakeEmbedding())
    for _ in range(12):
        if not await worker.run_once():
            break
    else:
        pytest.fail('Bounded derivative cleanup must converge')
    assert (await service.cleanup_status(**identity(scope), memory_id=note['id']))['status'] == 'cleaned'
    assert not await index.point_ids('memory', note['id'])


async def test_kill_switch_still_cleans_shared_suppressed_raw_source(runtime_env):
    scope, config, _, index = runtime_env
    original = '代号海风；偏好中文答复。'
    sid, mid, pid = await source_session(scope, original)
    memories = []
    async with get_db_session() as db:
        access = await resolve_access_scope(db, **identity(scope), project_id=scope['p1'])
        for key, summary in [('codename','代号海风'), ('language','偏好中文答复')]:
            row = await service.create_candidate_in_session(db, access=access, type='PREFERENCE', summary=summary, fact_key=key,
                sources=[{'source_kind':'user_statement','body':original,'session_id':sid,'turn_id':mid,
                    'message_id':mid,'part_id':pid,'branch_id':'root'}])
            memories.append(row.id)
    for memory_id in memories:
        await service.confirm_note(**identity(scope), proposal_id=memory_id, expected_revision=1)
    worker = outbox.MemoryIndexWorker(config, index=index, embedding=FakeEmbedding())
    async def drain():
        for _ in range(30):
            if not await worker.run_once():
                return
        pytest.fail('Bounded outbox processing should converge')
    await drain()
    async with get_db_session() as db:
        raw_id = await db.scalar(select(MemorySource.id).where(MemorySource.session_id == sid))
    assert await index.point_ids('source', raw_id)
    config.index_sync = False
    await service.delete_memory(**identity(scope), memory_id=memories[0], expected_revision=2)
    await drain()
    assert not await index.point_ids('source', raw_id)
    assert await index.point_ids('memory', memories[1])
    assert (await service.cleanup_status(**identity(scope), memory_id=memories[0]))['status'] == 'cleaned'


@pytest.mark.parametrize('operation', ['DELETE', 'REVOKE'])
async def test_authorized_cleanup_survives_allowlist_shrink_and_disabled_rollout(runtime_env, operation):
    scope, config, _, index = runtime_env
    forgotten = await service.create_note(**identity(scope), summary='Authorized cleanup before any initial index')
    unchanged = await service.create_note(**identity(scope), summary='Remain pending while rollout is disabled')
    index.points[('memory',forgotten['id'],1)] = ['synthetic-old-vector']
    await service.delete_memory(**identity(scope), memory_id=forgotten['id'], expected_revision=1)
    async with get_db_session() as db:
        await db.execute(update(MemoryOutbox).where(MemoryOutbox.object_id == forgotten['id'],
            MemoryOutbox.operation == 'DELETE').values(operation=operation))
    config.allowed_user_ids = [scope['other']]
    config.index_sync = False
    embedding = FakeEmbedding()
    worker = outbox.MemoryIndexWorker(config, index=index, embedding=embedding)
    for _ in range(10):
        if not await worker.run_once():
            break
    else:
        pytest.fail('Authorized deletion should converge even without rollout eligibility')
    assert embedding.calls == [] and not await index.point_ids('memory', forgotten['id'])
    assert (await service.cleanup_status(**identity(scope), memory_id=forgotten['id']))['status'] == 'cleaned'
    async with get_db_session() as db:
        pending = await db.scalar(select(MemoryOutbox).where(MemoryOutbox.object_id == unchanged['id']))
        assert pending.status == 'PENDING' and pending.attempts == 0
        source_ops = (await db.scalars(select(MemoryOutbox).where(MemoryOutbox.user_id == scope['user_id'],
            MemoryOutbox.object_kind == 'source'))).all()
        assert source_ops and all(row.operation == 'DELETE' and row.status == 'SUCCEEDED' for row in source_ops)
    # A nonempty but different allowlist also gates an enabled index provider.
    config.index_sync = True
    assert await outbox.claim_outbox('still-excluded', config) is None


async def test_previously_claimed_upsert_cannot_embed_after_allowlist_shrink(runtime_env):
    scope, config, _, index = runtime_env
    await service.create_note(**identity(scope), summary='Do not charge an excluded actor')
    lease = await outbox.claim_outbox('previous-allowlist', config)
    config.allowed_user_ids = [scope['other']]
    embedding = FakeEmbedding()
    assert not await outbox.deliver_outbox(lease, config, index=index, embedding=embedding)
    assert embedding.calls == [] and index.calls == []


@pytest.mark.parametrize('finish', ['complete', 'fail'])
async def test_running_source_write_after_delete_preserves_terminal_state(runtime_env, finish):
    scope, config, _, index = runtime_env
    note = await service.create_note(**identity(scope), summary='Original source write outlives deletion')
    memory_lease = await outbox.claim_outbox('initial-index', config)
    assert await outbox.deliver_outbox(memory_lease, config, index=index, embedding=FakeEmbedding())
    source_lease = await outbox.claim_outbox('slow-source-worker', config)
    assert source_lease.object_kind == 'source' and source_lease.operation == 'UPSERT'
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked_embedding():
        entered.set()
        await release.wait()
    slow_embedding = FakeEmbedding(callback=blocked_embedding, error='timeout' if finish == 'fail' else None)
    async def slow_delivery():
        try:
            return await outbox.deliver_outbox(source_lease, config, index=index, embedding=slow_embedding)
        except MemoryProviderError as exc:
            await outbox.fail_outbox(source_lease, config, exc.code)
            return False
    task = asyncio.create_task(slow_delivery())
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await service.delete_memory(**identity(scope), memory_id=note['id'], expected_revision=1)
        config.allowed_user_ids = [scope['other']]
        config.index_sync = False
        worker = outbox.MemoryIndexWorker(config, index=index, embedding=FakeEmbedding())
        assert await worker.run_once()  # Memory DELETE.
        assert await worker.run_once()  # Source DELETE while old source lease is live.
        async with get_db_session() as db:
            state = await db.scalar(select(MemoryIndexState).where(MemoryIndexState.object_id == source_lease.object_id,
                MemoryIndexState.index_generation == config.index_generation))
            assert state.status == 'DELETED'
        assert (await service.cleanup_status(**identity(scope), memory_id=note['id']))['status'] == 'stopped_cleanup_pending'
        release.set()
        await task
        async with get_db_session() as db:
            state = await db.scalar(select(MemoryIndexState).where(MemoryIndexState.object_id == source_lease.object_id,
                MemoryIndexState.index_generation == config.index_generation))
            assert state.status == 'DELETED' and state.chunk_ids == []
            old = await db.get(MemoryOutbox, source_lease.id)
            assert old.status == ('CANCELLED' if finish == 'fail' else 'SUCCEEDED')
        assert (await service.cleanup_status(**identity(scope), memory_id=note['id']))['status'] == 'cleaned'
        assert not await index.point_ids('source', source_lease.object_id)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def forgotten_with_running_source(runtime_env):
    scope, config, _, index = runtime_env
    config.worker_lease_seconds, config.provider_timeout_seconds = 10, 30
    note = await service.create_note(**identity(scope), summary='Crash recovery keeps cleanup honest')
    memory_lease = await outbox.claim_outbox('initial-memory', config)
    assert await outbox.deliver_outbox(memory_lease, config, index=index, embedding=FakeEmbedding())
    source_lease = await outbox.claim_outbox('crashed-source', config)
    assert source_lease.object_kind == 'source'
    await service.delete_memory(**identity(scope), memory_id=note['id'], expected_revision=1)
    config.allowed_user_ids, config.index_sync = [scope['other']], False
    worker = outbox.MemoryIndexWorker(config, index=index, embedding=FakeEmbedding())
    assert await worker.run_once()  # Parent DELETE.
    assert await worker.run_once()  # Source DELETE, while its old UPSERT is still live.
    expiry = datetime.now(timezone.utc) - timedelta(seconds=1)
    async with get_db_session() as db:
        await db.execute(update(MemoryOutbox).where(MemoryOutbox.id == source_lease.id).values(lease_until=expiry))
    return note, source_lease, expiry


async def test_cleanup_waits_provider_grace_then_retires_crashed_source_in_every_generation(runtime_env, monkeypatch):
    scope, config, indexes, index = runtime_env
    note, lease, expiry = await forgotten_with_running_source(runtime_env)
    retired = 'retired-' + uuid4().hex[:20]
    await outbox.ensure_generation(config, retired)
    indexes[retired] = FakeIndex(config)
    for adapter in indexes.values():
        adapter.calls.clear()
    for adapter in (index, indexes[retired]):
        adapter.points[('source',lease.object_id,lease.revision)] = ['synthetic-late-network-write']
    grace = max(config.provider_timeout_seconds, config.worker_lease_seconds)
    clock = [expiry + timedelta(seconds=grace-1)]
    monkeypatch.setattr(reconcile, '_now', lambda: clock[0])
    assert await reconcile.reconcile_pending_deletions(config) == 0
    assert (await service.cleanup_status(**identity(scope), memory_id=note['id']))['status'] == 'stopped_cleanup_pending'
    assert not any(call[:3] == ('delete_object_versions','source',lease.object_id)
                   for adapter in indexes.values() for call in adapter.calls)
    async with get_db_session() as db:
        assert (await db.get(MemoryOutbox, lease.id)).status == 'RUNNING'
    clock[0] = expiry + timedelta(seconds=grace+1)
    assert await reconcile.reconcile_pending_deletions(config) == 1
    for adapter in indexes.values():
        assert not await adapter.point_ids('source', lease.object_id)
        assert ('delete_object_versions','source',lease.object_id,None) in adapter.calls
    async with get_db_session() as db:
        old = await db.get(MemoryOutbox, lease.id)
        assert old.status == 'CANCELLED' and old.lease_until is None
        state = await db.scalar(select(MemoryIndexState).where(MemoryIndexState.object_id == lease.object_id))
        assert state.status == 'DELETED' and state.chunk_ids == []
    assert (await service.cleanup_status(**identity(scope), memory_id=note['id']))['status'] == 'cleaned'


async def test_cleanup_recovers_dead_parent_and_discovers_old_source_state_without_reopening_completed_events(runtime_env, monkeypatch):
    scope, config, indexes, index = runtime_env
    config.worker_lease_seconds, config.provider_timeout_seconds = 10, 30
    note = await service.create_note(**identity(scope), summary='Crash before derivative cleanup was queued')
    lease = await outbox.claim_outbox('crashed-memory', config)
    await service.delete_memory(**identity(scope), memory_id=note['id'], expected_revision=1)
    instant = datetime.now(timezone.utc)
    async with get_db_session() as db:
        await db.execute(update(MemoryOutbox).where(MemoryOutbox.id == lease.id).values(
            lease_until=instant-timedelta(seconds=40)))
        await db.execute(update(MemoryOutbox).where(MemoryOutbox.object_id == note['id'],
            MemoryOutbox.operation == 'DELETE').values(status='DEAD'))
    config.allowed_user_ids, config.index_sync = [scope['other']], False
    monkeypatch.setattr(reconcile, '_now', lambda: instant)
    assert await reconcile.reconcile_pending_deletions(config) == 1
    async with get_db_session() as db:
        assert (await db.get(MemoryOutbox, lease.id)).status == 'CANCELLED'
        source_delete = await db.scalar(select(MemoryOutbox).where(MemoryOutbox.user_id == scope['user_id'],
            MemoryOutbox.object_kind == 'source'))
        assert source_delete.operation == 'DELETE' and source_delete.status == 'PENDING'
        raw_id = source_delete.object_id
    assert (await service.cleanup_status(**identity(scope), memory_id=note['id']))['status'] == 'stopped_cleanup_pending'
    await reconcile.reconcile_pending_deletions(config)
    assert (await service.cleanup_status(**identity(scope), memory_id=note['id']))['status'] == 'cleaned'
    # A retired raw-source state can lag despite its primary tombstone being
    # complete. Discover it through source links, without reopening the already
    # successful current-generation derivative event.
    retired = 'retired-' + uuid4().hex[:20]
    await outbox.ensure_generation(config, retired)
    indexes[retired] = FakeIndex(config)
    indexes[retired].points[('source',raw_id,1)] = ['retired-source-vector']
    async with get_db_session() as db:
        db.add(MemoryIndexState(id=ascending('memory_index'), user_id=scope['user_id'], workspace_id=scope['workspace_id'],
            object_kind='source', object_id=raw_id, index_generation=retired, desired_revision=1, indexed_revision=1,
            chunk_ids=['retired-source-vector'], status='INDEXED', updated_at=instant))
    await reconcile.reconcile_pending_deletions(config)
    async with get_db_session() as db:
        states = (await db.scalars(select(MemoryIndexState).where(MemoryIndexState.object_id == raw_id))).all()
        assert len(states) == 2 and all(state.status == 'DELETED' and state.chunk_ids == [] for state in states)
        assert (await db.get(MemoryOutbox, source_delete.id)).status == 'SUCCEEDED'
    assert not await indexes[retired].point_ids('source', raw_id)
    assert (await service.cleanup_status(**identity(scope), memory_id=note['id']))['status'] == 'cleaned'


async def test_failed_vector_verification_does_not_retire_expired_write_or_claim_cleanup_success(runtime_env, monkeypatch):
    scope, config, _, index = runtime_env
    note, lease, expiry = await forgotten_with_running_source(runtime_env)
    monkeypatch.setattr(reconcile, '_now', lambda: expiry+timedelta(seconds=100))
    original_point_ids = index.point_ids
    async def unverified(kind, object_id):
        return ['unconfirmed-late-point'] if (kind,object_id) == ('source',lease.object_id) else await original_point_ids(kind,object_id)
    monkeypatch.setattr(index, 'point_ids', unverified)
    assert await reconcile.reconcile_pending_deletions(config) == 0
    async with get_db_session() as db:
        assert (await db.get(MemoryOutbox, lease.id)).status == 'RUNNING'
    assert (await service.cleanup_status(**identity(scope), memory_id=note['id']))['status'] == 'stopped_cleanup_pending'
    monkeypatch.setattr(index, 'point_ids', original_point_ids)
    assert await reconcile.reconcile_pending_deletions(config) == 1
    assert (await service.cleanup_status(**identity(scope), memory_id=note['id']))['status'] == 'cleaned'


@pytest.mark.parametrize('change', ['renewed_lease', 'new_generation'])
async def test_cleanup_revalidates_inflight_lease_and_generation_after_vector_await(runtime_env, monkeypatch, change):
    scope, config, indexes, index = runtime_env
    note, lease, expiry = await forgotten_with_running_source(runtime_env)
    instant = expiry + timedelta(seconds=100)
    monkeypatch.setattr(reconcile, '_now', lambda: instant)
    original_point_ids = index.point_ids
    changed = []
    async def changed_while_waiting(kind, object_id):
        if (kind,object_id) == ('source',lease.object_id) and not changed:
            changed.append(True)
            if change == 'renewed_lease':
                async with get_db_session() as db:
                    await db.execute(update(MemoryOutbox).where(MemoryOutbox.id == lease.id).values(
                        lease_owner='replacement', lease_generation=lease.generation+1,
                        lease_until=instant+timedelta(seconds=10)))
            else:
                generation = 'added-' + uuid4().hex[:20]
                await outbox.ensure_generation(config, generation)
                indexes[generation] = FakeIndex(config)
                indexes[generation].points[('source',lease.object_id,lease.revision)] = ['new-generation-point']
        return await original_point_ids(kind,object_id)
    monkeypatch.setattr(index, 'point_ids', changed_while_waiting)
    assert await reconcile.reconcile_pending_deletions(config) == 0
    assert changed
    async with get_db_session() as db:
        assert (await db.get(MemoryOutbox, lease.id)).status == 'RUNNING'
        if change == 'renewed_lease':
            await db.execute(update(MemoryOutbox).where(MemoryOutbox.id == lease.id).values(
                lease_until=instant-timedelta(seconds=100)))
    assert (await service.cleanup_status(**identity(scope), memory_id=note['id']))['status'] == 'stopped_cleanup_pending'
    assert await reconcile.reconcile_pending_deletions(config) == 1
    assert (await service.cleanup_status(**identity(scope), memory_id=note['id']))['status'] == 'cleaned'
    assert not any(await asyncio.gather(*(adapter.point_ids('source',lease.object_id) for adapter in indexes.values())))


async def test_cleanup_does_not_treat_disabled_rollout_or_old_revision_as_deletion_authority(runtime_env, monkeypatch):
    scope, config, _, index = runtime_env
    note = await service.create_note(**identity(scope), summary='Budget1200')
    old_lease = await outbox.claim_outbox('crashed-prior-revision', config)
    await service.edit_note(**identity(scope), memory_id=note['id'], expected_revision=1, summary='Budget900')
    instant = datetime.now(timezone.utc)
    async with get_db_session() as db:
        await db.execute(update(MemoryOutbox).where(MemoryOutbox.id == old_lease.id).values(
            lease_until=instant-timedelta(seconds=1000)))
    index.points[('memory',note['id'],2)] = ['valid-new-version']
    config.allowed_user_ids, config.index_sync = [scope['other']], False
    monkeypatch.setattr(reconcile, '_now', lambda: instant)
    assert await reconcile.reconcile_pending_deletions(config) == 0
    assert index.points[('memory',note['id'],2)] == ['valid-new-version']
    assert index.calls == []
    async with get_db_session() as db:
        assert (await db.get(MemoryOutbox, old_lease.id)).status == 'RUNNING'


async def test_scoped_reconcile_keeps_verified_terminal_deletion_complete(runtime_env):
    scope, config, _, index = runtime_env
    note = await service.create_note(**identity(scope), summary='Do not reopen completed cleanup every five minutes')
    await service.delete_memory(**identity(scope), memory_id=note['id'], expected_revision=1)
    worker = outbox.MemoryIndexWorker(config, index=index, embedding=FakeEmbedding())
    for _ in range(10):
        if not await worker.run_once():
            break
    else:
        pytest.fail('Bounded cleanup should converge')
    async with get_db_session() as db:
        access = await resolve_access_scope(db, **identity(scope))
        before = set((await db.scalars(select(MemoryOutbox.id).where(MemoryOutbox.workspace_id == scope['workspace_id']))).all())
    for _ in range(2):
        result = await reconcile.reconcile(access, config)
        assert result['repair_events'] == 0 and result['complete']
    async with get_db_session() as db:
        after = set((await db.scalars(select(MemoryOutbox.id).where(MemoryOutbox.workspace_id == scope['workspace_id']))).all())
        assert before == after
    assert (await service.cleanup_status(**identity(scope), memory_id=note['id']))['status'] == 'cleaned'


async def test_expired_lease_cannot_write_or_ack_and_new_owner_can_recover(runtime_env):
    scope, config, _, index = runtime_env
    note = await service.create_note(**identity(scope), summary='Fence me before embedding returns')
    lease = await outbox.claim_outbox('old-worker', config)
    async def expire():
        async with get_db_session() as db:
            await db.execute(update(MemoryOutbox).where(MemoryOutbox.id == lease.id).values(
                lease_until=datetime.now(timezone.utc)-timedelta(seconds=1)))
    assert not await outbox.deliver_outbox(lease, config, index=index, embedding=FakeEmbedding(callback=expire))
    assert not index.calls
    async with get_db_session() as db:
        assert (await db.get(MemoryOutbox, lease.id)).status == 'RUNNING'
    newer = await outbox.claim_outbox('new-worker', config)
    assert newer.id == lease.id and newer.generation == lease.generation+1
    assert not await outbox.deliver_outbox(lease, config, index=index, embedding=FakeEmbedding())
    assert await outbox.deliver_outbox(newer, config, index=index, embedding=FakeEmbedding())
    async with get_db_session() as db:
        assert (await db.get(MemoryOutbox, lease.id)).status == 'SUCCEEDED'
        state = await db.scalar(select(MemoryIndexState).where(MemoryIndexState.object_id == note['id']))
        assert state.status == 'INDEXED' and state.indexed_revision == 1


async def test_lease_loss_after_upsert_cannot_prune_other_versions(runtime_env):
    scope, config, _, index = runtime_env
    note = await service.create_note(**identity(scope), summary='Only the live owner can prune versions')
    lease = await outbox.claim_outbox('old-worker', config)
    async def replace_owner():
        async with get_db_session() as db:
            await db.execute(update(MemoryOutbox).where(MemoryOutbox.id == lease.id).values(
                lease_owner='new-worker', lease_generation=lease.generation+1))
        index.points[('memory', note['id'], 2)] = ['newer-version']
    index.callback = replace_owner
    assert not await outbox.deliver_outbox(lease, config, index=index, embedding=FakeEmbedding())
    assert not any(call[0] == 'delete_object_versions' for call in index.calls)
    assert index.points[('memory',note['id'],2)] == ['newer-version']
    async with get_db_session() as db:
        row = await db.get(MemoryOutbox, lease.id)
        assert row.status == 'RUNNING' and row.lease_owner == 'new-worker'


async def test_reconcile_repairs_are_idempotent_scoped_and_generation_config_is_immutable(runtime_env):
    scope, config, _, index = runtime_env
    note = await service.create_note(**identity(scope), project_id=scope['p1'], summary='First project source')
    foreign_project = await service.create_note(**identity(scope), project_id=scope['p2'], summary='Second project source')
    async with get_db_session() as db:
        access = await resolve_access_scope(db, **identity(scope), project_id=scope['p1'])
    first = await reconcile.reconcile(access, config, limit=50)
    second = await reconcile.reconcile(access, config, limit=50)
    assert first['repair_events'] > 0 and second['repair_events'] == 0
    async with get_db_session() as db:
        events = (await db.scalars(select(MemoryOutbox).where(MemoryOutbox.user_id == scope['user_id'],
            MemoryOutbox.event_id.startswith('reconcile:')))).all()
        assert events and all(event.project_id == scope['p1'] for event in events)
        assert foreign_project['id'] not in {event.object_id for event in events}
        assert all(len(event.event_id) <= 128 for event in events)
    with pytest.raises(MemoryProviderError) as exc:
        await outbox.ensure_generation(config.model_copy(update={'embedding_dimensions':128}))
    assert exc.value.code == 'index_generation_configuration_changed'


async def test_worker_heartbeat_preserves_only_current_fence(runtime_env, monkeypatch):
    scope, config, _, _ = runtime_env
    await service.create_note(**identity(scope), summary='Keep a long embedding lease alive')
    lease = await outbox.claim_outbox('heartbeat-owner', config)
    stop = asyncio.Event()
    wait_calls = []
    original_wait_for = asyncio.wait_for
    async def immediate_tick(awaitable, timeout):
        wait_calls.append(timeout)
        if len(wait_calls) == 1:
            awaitable.close()
            raise asyncio.TimeoutError
        stop.set()
        return await original_wait_for(awaitable, timeout=1)
    monkeypatch.setattr(outbox.asyncio, 'wait_for', immediate_tick)
    before = datetime.now(timezone.utc)
    await outbox.MemoryIndexWorker(config)._heartbeat(lease, stop)
    async with get_db_session() as db:
        row = await db.get(MemoryOutbox, lease.id)
        until = row.lease_until.replace(tzinfo=timezone.utc) if row.lease_until.tzinfo is None else row.lease_until
        assert until >= before + timedelta(seconds=config.worker_lease_seconds-1)
        row.lease_owner, row.lease_generation = 'replacement', lease.generation+1
        saved_until = row.lease_until
    stop.clear()
    wait_calls.clear()
    await outbox.MemoryIndexWorker(config)._heartbeat(lease, stop)
    async with get_db_session() as db:
        row = await db.get(MemoryOutbox, lease.id)
        assert row.lease_owner == 'replacement' and row.lease_until == saved_until
