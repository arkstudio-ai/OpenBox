"""Read-only memory diagnostics and separately granted, bounded replays.

GETs never invoke a model. A replay preview freezes its redacted query, current
scope, exact source revisions, provider contract and expiry. Submission is an
atomic one-shot claim with a real attempt ID before any network call.
"""
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, StrictBool
from sqlalchemy import select, update

from auth.middleware import get_current_user
from auth.workspace import get_workspace
from core.config import get_config
from core.identifier import ascending
from db.base import get_db_session
from db.models.memory_runtime import MemoryReplayPreview
from db.models.memory_v2 import MemoryDebugRun
from memory import observability
from memory.policy import MemoryAccessDenied, resolve_access_scope
from memory.redaction import redact_value, text_hash

router = APIRouter(prefix='/api/memory-debug', tags=['memory-debug'], dependencies=[Depends(get_workspace)])


class ReplayPreviewBody(BaseModel):
    steps: list[Literal['route', 'retrieval']] = Field(default_factory=lambda: ['route', 'retrieval'], min_length=1, max_length=2)


class ReplayBody(BaseModel):
    preview_id: str = Field(min_length=1, max_length=64)
    confirm_cost: StrictBool


def _now():
    return datetime.now(timezone.utc)


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _config_hash(config):
    # Only the hash is persisted; credentials and configured URLs are never
    # exposed by the preview. Budget/model changes invalidate existing grants.
    return sha256(json.dumps(config.model_dump(), sort_keys=True, default=str).encode()).hexdigest()


def _require(feature, user):
    config = get_config().memory
    if not config.enabled(feature, user['user_id']):
        raise HTTPException(403, {'code': 'MEMORY_DEBUG_DISABLED', 'message': 'This diagnostic capability is disabled for the current user'})
    return config


async def _scope(user, project_id=None, *, include_all_projects=False):
    try:
        async with get_db_session() as db:
            return await resolve_access_scope(db, user_id=user['user_id'], workspace_id=user.get('workspace_id'),
                                              project_id=project_id, include_all_projects=include_all_projects)
    except MemoryAccessDenied as exc:
        raise HTTPException(404, 'memory diagnostic scope not found') from exc


async def _read(run_id, scope, config):
    try:
        result = await observability.read_debug_run(run_id, scope, config)
    except MemoryAccessDenied as exc:
        raise HTTPException(404, 'memory diagnostic run not found') from exc
    if result is None:
        raise HTTPException(404, 'memory diagnostic run not found')
    return result


@router.get('/runs')
async def list_runs(project_id: str | None = None, session_id: str | None = None, request_id: str | None = None,
    status: str | None = None, since: datetime | None = None, until: datetime | None = None,
    cursor: str | None = None, limit: int = Query(30, ge=1, le=100),
    current_user: dict = Depends(get_current_user)):
    config = _require('debug_view', current_user)
    if any(value is not None and value.tzinfo is None for value in (since, until)):
        raise HTTPException(422, 'Diagnostic dates must include a timezone')
    since = since.astimezone(timezone.utc) if since else None
    until = until.astimezone(timezone.utc) if until else None
    if since and until and since > until:
        raise HTTPException(422, 'Diagnostic dates must form an increasing window')
    scope = await _scope(current_user, project_id, include_all_projects=project_id is None)
    return await observability.list_debug_runs(scope, config, project_id=project_id, session_id=session_id,
        request_id=request_id, status=status, since=since, until=until, limit=limit, cursor=cursor)


@router.get('/runs/{run_id}')
async def read_run(run_id: str, current_user: dict = Depends(get_current_user)):
    config = _require('debug_view', current_user)
    return await _read(run_id, await _scope(current_user), config)


@router.get('/health')
async def health(current_user: dict = Depends(get_current_user)):
    config = _require('debug_view', current_user)
    return await observability.memory_health(await _scope(current_user, include_all_projects=True), config)


def _call_preview(config, steps, user_id):
    calls = []
    if 'route' in steps:
        calls.append({'component': 'route', 'provider': 'Jev', 'model': config.jev_model,
                      'maximum_requests': 1 if config.enabled('route_jev', user_id) else 0})
    if 'retrieval' in steps:
        calls.append({'component': 'query_embedding', 'provider': 'Bailian', 'model': config.embedding_model,
                      'maximum_requests': 1 if config.enabled('retrieval_v2', user_id) else 0})
        calls.append({'component': 'rerank', 'provider': 'Bailian', 'model': config.rerank_model,
                      'maximum_requests': 1 if config.enabled('rerank', user_id) else 0, 'maximum_documents': config.rerank_max_documents})
    return calls


@router.post('/runs/{run_id}/replay/preview')
async def replay_preview(run_id: str, body: ReplayPreviewBody, current_user: dict = Depends(get_current_user)):
    _require('debug_view', current_user)
    config = _require('debug_replay', current_user)
    actor_scope = await _scope(current_user)
    detail = await _read(run_id, actor_scope, config)
    run = detail['run']
    scope = await _scope(current_user, run.get('project_id'))
    snapshot = run.get('input_snapshot') or {}
    utterance = snapshot.get('utterance')
    steps = list(dict.fromkeys(body.steps))
    can_submit = bool(run.get('body_available') and isinstance(utterance, str) and utterance.strip())
    query = utterance[:config.debug_snapshot_max_chars] if isinstance(utterance, str) else ''
    grant = {
        'query': query, 'steps': steps, 'source_refs': run.get('source_refs') or [],
        'config_hash': _config_hash(config), 'acl_epoch': scope.acl_epoch, 'can_submit': can_submit,
        'session_id': run.get('session_id'), 'turn_id': run.get('turn_id'),
        'snapshot_only': True, 'original_input_reconstructed': False,
    }
    preview_id = ascending('memorypreview')
    expires = _now() + timedelta(minutes=10)
    async with get_db_session() as db:
        # Revalidate membership in the write transaction. This cannot authorize
        # a request after the user's membership has changed during page reads.
        current = await resolve_access_scope(db, user_id=scope.user_id, workspace_id=scope.workspace_id, project_id=scope.project_id)
        if current.acl_epoch != scope.acl_epoch:
            raise HTTPException(409, 'Memory scope changed; open a new preview')
        db.add(MemoryReplayPreview(id=preview_id, run_id=run_id, user_id=scope.user_id,
            workspace_id=scope.workspace_id, project_id=scope.project_id, input_hash=text_hash(query),
            grant=grant, created_at=_now(), expires_at=expires))
    return {'id': preview_id, 'preview_id': preview_id, 'can_submit': can_submit,
            'input': {'utterance': query, 'redacted': True, 'snapshot_only': True,
                      'original_input_reconstructed': False, 'truncated': snapshot.get('truncated', False)},
            'scope': {'workspace_id': scope.workspace_id, 'project_id': scope.project_id, 'visibility': 'PERSONAL'},
            'steps': steps, 'calls': _call_preview(config, steps, scope.user_id), 'cost_estimate': None,
            'cost_currency': None, 'may_charge': any(call['maximum_requests'] for call in _call_preview(config, steps, scope.user_id)),
            'expires_at': expires.isoformat(), 'reason_code': None if can_submit else 'source_or_snapshot_unavailable'}


@router.post('/replay')
async def replay(body: ReplayBody, current_user: dict = Depends(get_current_user)):
    _require('debug_view', current_user)
    config = _require('debug_replay', current_user)
    if not body.confirm_cost:
        raise HTTPException(422, 'Explicit confirmation of possible charges is required')
    actor_scope = await _scope(current_user, include_all_projects=True)
    async with get_db_session() as db:
        preview = await db.scalar(select(MemoryReplayPreview).where(MemoryReplayPreview.id == body.preview_id,
            *actor_scope.predicates(MemoryReplayPreview)))
        if preview is None:
            raise HTTPException(404, 'replay preview not found')
        parent_id, project_id, grant = preview.run_id, preview.project_id, dict(preview.grant or {})
    scope = await _scope(current_user, project_id)
    parent = (await _read(parent_id, actor_scope, config))['run']
    if not parent.get('body_available') or parent.get('source_refs', []) != grant.get('source_refs', []):
        raise HTTPException(409, 'Replay sources are no longer available; open a new preview')
    query = grant.get('query', '')
    if not grant.get('can_submit') or not query or grant.get('config_hash') != _config_hash(config) or grant.get('acl_epoch') != scope.acl_epoch:
        raise HTTPException(409, 'Replay grant or provider configuration changed; open a new preview')
    run_id, attempt_id = ascending('memoryrun'), ascending('memoryattempt')
    async with get_db_session() as db:
        current = await resolve_access_scope(db, user_id=scope.user_id, workspace_id=scope.workspace_id, project_id=scope.project_id)
        preview = await db.scalar(select(MemoryReplayPreview).where(MemoryReplayPreview.id == body.preview_id,
            *current.predicates(MemoryReplayPreview)).with_for_update())
        if preview is None:
            raise HTTPException(404, 'replay preview not found')
        if preview.consumed_run_id:
            old = await db.scalar(select(MemoryDebugRun).where(MemoryDebugRun.id == preview.consumed_run_id,
                *current.predicates(MemoryDebugRun)))
            if old:
                return {'run_id': old.id, 'attempt_id': old.attempt_id, 'status': old.status, 'replayed': False}
            raise HTTPException(409, 'Replay was already claimed')
        if _utc(preview.expires_at) <= _now() or current.acl_epoch != grant['acl_epoch']:
            raise HTTPException(409, 'Replay preview expired or scope changed')
        parent_row = await db.scalar(select(MemoryDebugRun).where(MemoryDebugRun.id == parent_id,
            *current.predicates(MemoryDebugRun), MemoryDebugRun.expires_at > _now()))
        if parent_row is None or not await observability.debug_input_is_current(db, parent_row, current):
            raise HTTPException(409, 'Replay input is no longer available')
        from memory.retrieval import authorized_documents
        references = grant.get('source_refs', [])
        keys = {(entry['kind'], entry['id']) for entry in references if 'kind' in entry and 'id' in entry}
        documents = await authorized_documents(db, current, config, only=keys) if keys else []
        by_key = {(document.kind, document.id): document for document in documents}
        if not all((entry.get('kind'), entry.get('id')) in by_key and
                   by_key[(entry['kind'], entry['id'])].revision == entry.get('revision') for entry in references):
            raise HTTPException(409, 'Replay sources changed before submission')
        result = await db.execute(update(MemoryReplayPreview).where(MemoryReplayPreview.id == preview.id,
            MemoryReplayPreview.consumed_run_id.is_(None), MemoryReplayPreview.expires_at > _now()).values(
            consumed_run_id=run_id).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise HTTPException(409, 'Replay was already claimed')
        db.add(MemoryDebugRun(id=run_id, request_id=ascending('memoryrequest'), attempt_id=attempt_id,
            parent_run_id=parent_id, session_id=grant.get('session_id'), turn_id=grant.get('turn_id'),
            user_id=current.user_id, workspace_id=current.workspace_id, project_id=current.project_id,
            status='RUNNING', policy_version=config.policy_version, input_hash=text_hash(query),
            input_snapshot=redact_value({'utterance': query, 'redacted': True, 'snapshot_only': True,
                'original_input_reconstructed': False, 'replay_preview_id': preview.id}, limit=config.debug_snapshot_max_chars),
            source_refs=grant.get('source_refs', []), usage={}, created_at=_now(),
            expires_at=_now() + timedelta(days=config.debug_retention_days)))
    # This operation produces diagnostic reads only. It never confirms facts,
    # enqueues extraction, publishes Wiki or mutates tasks.
    from memory.orchestrator import run_memory_context
    try:
        await run_memory_context(query, scope, config, session_id=grant.get('session_id'), turn_id=grant.get('turn_id'),
            parent_run_id=parent_id, steps=grant['steps'], existing_run_id=run_id)
    except Exception:
        await observability.add_debug_step(run_id, 'replay', 'FAILED', reason_code='replay_failed')
        await observability.finish_debug_run(run_id, 'FAILED', source_refs=grant.get('source_refs', []))
    return {'run_id': run_id, 'attempt_id': attempt_id}
