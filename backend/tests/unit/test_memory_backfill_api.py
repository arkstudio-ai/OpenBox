"""Bounded completion preview never reads another actor or creates work."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select, func

from api import memory_backfill
from core.config import OpenBoxConfig, MemoryConfig
from db.base import get_db_session
from db.models.memory_pipeline import MemoryTurnCompletion, MemoryExtractionJob
from tests.unit.test_memory_authority_v2 import authority_scope, identity  # noqa: F401


def test_backfill_requires_explicit_timezone_window_limit_and_scope():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError):
        memory_backfill.BackfillDryRunBody(start_at=now-timedelta(days=1),end_at=now)
    for start,end,limit in ((now,now,50),(now-timedelta(days=32),now,50),(now.replace(tzinfo=None),now,50),(now-timedelta(days=1),now,501)):
        with pytest.raises(ValidationError):
            memory_backfill.BackfillDryRunBody(project_id=None,start_at=start,end_at=end,limit=limit)


async def test_backfill_is_scoped_metadata_only_and_never_enqueues(authority_scope,monkeypatch):
    scope = authority_scope
    config = OpenBoxConfig(memory=MemoryConfig(backfill=True))
    monkeypatch.setattr(memory_backfill,'get_config',lambda:config)
    now = datetime.now(timezone.utc)
    owner_receipt = 'receipt_'+uuid4().hex[:16]
    async with get_db_session() as db:
        for ordinal,(uid,project,rid) in enumerate(((scope['user_id'],scope['p1'],owner_receipt),(scope['other'],scope['foreign'],'foreign_'+uuid4().hex[:16])),1):
            db.add(MemoryTurnCompletion(id=rid,user_id=uid,workspace_id=scope['workspace_id'],project_id=project,
                session_id='bounded_preview_session_'+uuid4().hex[:12],branch_id='root',logical_turn_id=rid,run_id=rid,
                run_generation=1,result_message_id='result_'+rid,ordinal=ordinal,start_sequence=1,end_sequence=2,
                source_boundaries=[],input_hash='0'*64,acl_hash='1'*64,pipeline_version='memory-extraction-v1',created_at=now))
    # The existing receipt helper is injected here to verify authentication
    # arguments independently from completion-source validation covered by P2.
    received = []
    async def fake_receipts(**kwargs):
        received.append(kwargs)
        assert kwargs['user_id'] == scope['user_id'] and kwargs['workspace_id'] == scope['workspace_id']
        assert kwargs['project_id'] == scope['p1'] and kwargs['limit'] == 50
        return [{'completion_id':owner_receipt,'source_count':0,'input_hash':'0'*64}]
    monkeypatch.setattr(memory_backfill,'backfill_dry_run',fake_receipts)
    body = memory_backfill.BackfillDryRunBody(project_id=scope['p1'],start_at=now-timedelta(days=7),end_at=now+timedelta(seconds=1))
    result = await memory_backfill.dry_run(body,current_user=identity(scope))
    assert result['dry_run'] and result['enqueued'] == result['model_calls'] == 0
    assert result['coverage'] == 'durable_successful_completion_receipts_only'
    assert result['completions'][0]['completion_id'] == owner_receipt and len(received) == 1
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(MemoryExtractionJob).where(MemoryExtractionJob.user_id==scope['user_id'])) == 0
    config.memory.backfill = False
    with pytest.raises(HTTPException) as disabled:
        await memory_backfill.dry_run(body,current_user=identity(scope))
    assert disabled.value.status_code == 403
