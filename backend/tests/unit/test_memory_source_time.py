"""Occurrence dates stay bound to the original authorized user evidence."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, update

from core import config as config_module
from db.base import get_db_session
from db.models.memory_pipeline import MemoryExtractionJob, MemoryTurnCompletion
from db.models.memory_v2 import MemorySource, MemorySourceLink
from db.models.message import Message
from db.models.part import Part
from memory import jobs, service
from memory.extraction import validate_proposals
from memory.policy import resolve_access_scope
from memory.source_time import canonical_user_occurrence, source_occurred_at
from memory.time_context import resolve_query_time
from models.message import TextPart
from session import session as session_module
from session.agent_event_log import append_surface_remove_locked, prepare_agent_event_write
from tests.unit.test_memory_pipeline import _finish_turn, _job, _proposal, _seed, pipeline_database  # noqa: F401

OCCURRED = datetime(2026, 10, 1, 16, 37, 12, 345678, tzinfo=timezone.utc)


def _event(sequence, kind, payload, *, created_at=OCCURRED + timedelta(days=2), session_id="session"):
    return SimpleNamespace(sequence=sequence, kind=kind, payload=payload, created_at=created_at,
                           session_id=session_id)


def test_original_creation_wins_over_later_message_snapshot_and_storage_clock():
    original = _event(3, "message.created", {"message": {
        "id": "user", "session_id": "session", "role": "user", "created_at": OCCURRED.isoformat()}})
    later = _event(9, "message.updated", {"message": {
        "id": "user", "session_id": "session", "role": "user",
        "created_at": (OCCURRED + timedelta(days=8)).isoformat()}})
    assert canonical_user_occurrence([later, original], "user", end_sequence=9, session_id="session") == OCCURRED
    assert canonical_user_occurrence([original], "user", end_sequence=2, session_id="session") is None
    assert canonical_user_occurrence([original], "user", session_id="other") is None


@pytest.mark.parametrize("created", [None, "", "invalid", "2026-10-02T00:37:00"])
def test_legacy_seed_only_uses_known_original_message_time(created):
    message = {"id": "user", "session_id": "session", "role": "user"}
    if created is not None:
        message["created_at"] = created
    seed = _event(1, "surface.seed", {"surface": {"session_id": "session", "messages": [message]}})
    assert canonical_user_occurrence([seed], "user", session_id="session") is None
    message["created_at"] = "2026-10-02T00:37:12.345678+08:00"
    assert canonical_user_occurrence([seed], "user", session_id="session") == OCCURRED


def test_original_created_event_time_is_fallback_only_for_actual_message_creation():
    message = {"id": "user", "session_id": "session", "role": "user"}
    event = _event(1, "message.created", {"message": message}, created_at=OCCURRED.replace(tzinfo=None))
    assert canonical_user_occurrence([event], "user", session_id="session") == OCCURRED
    message["role"] = "assistant"
    assert canonical_user_occurrence([event], "user", session_id="session") is None
    message["role"], message["created_at"] = "user", None
    assert canonical_user_occurrence([event], "user", session_id="session") is None


async def _dated_turn(monkeypatch):
    seed = await _seed(monkeypatch)
    insert = session_module._insert_user_message_locked

    async def dated_insert(*args, **kwargs):
        kwargs["now"] = OCCURRED
        return await insert(*args, **kwargs)

    monkeypatch.setattr(session_module, "_insert_user_message_locked", dated_insert)
    _, _, assistant = await _finish_turn(seed)
    return seed, assistant


async def _candidate_source(seed, *, legacy_receipt=False):
    if legacy_receipt:
        # Fabricate the previous schema shape only in this isolated synthetic
        # fixture; production compatibility must never rewrite frozen rows.
        async with get_db_session() as db:
            receipt = await db.scalar(select(MemoryTurnCompletion).where(MemoryTurnCompletion.session_id == seed[3]))
            old_boundaries = [{key: value for key, value in boundary.items() if key != "occurred_at"}
                              for boundary in receipt.source_boundaries]
            old_hash = jobs._hash(old_boundaries)
            receipt.source_boundaries, receipt.input_hash = old_boundaries, old_hash
            await db.execute(update(MemoryExtractionJob).where(MemoryExtractionJob.session_id == seed[3]).values(input_hash=old_hash))
    lease = await jobs.claim_job("source-time", allowed_user_ids=[seed[0]])
    frozen = await jobs.read_extraction_input(lease)
    ids = await jobs.commit_extraction(lease, frozen, validate_proposals(_proposal(frozen), frozen))
    async with get_db_session() as db:
        source = await db.scalar(select(MemorySource).join(MemorySourceLink,
            MemorySourceLink.source_id == MemorySource.id).where(MemorySourceLink.memory_id == ids[0]))
    return ids[0], source, frozen


@pytest.mark.asyncio
async def test_new_completion_hashes_original_occurrence_and_stores_typed_utc_source(monkeypatch):
    seed, _ = await _dated_turn(monkeypatch)
    async with get_db_session() as db:
        receipt = await db.scalar(select(MemoryTurnCompletion).where(MemoryTurnCompletion.session_id == seed[3]))
        boundary = receipt.source_boundaries[0]
        assert boundary["occurred_at"] == OCCURRED.isoformat()
        assert receipt.input_hash == jobs._hash(receipt.source_boundaries)
        assert receipt.input_hash != jobs._hash([{key: value for key, value in boundary.items() if key != "occurred_at"}])
        # Mutable SQL snapshots and job/recorded clocks do not redefine the
        # original immutable Message creation event.
        await db.execute(update(Message).where(Message.id == boundary["message_id"]).values(
            created_at=OCCURRED + timedelta(days=20)))
        await db.execute(update(MemoryExtractionJob).where(MemoryExtractionJob.session_id == seed[3]).values(
            created_at=OCCURRED + timedelta(days=30)))
    _, source, frozen = await _candidate_source(seed)
    assert frozen.sources[0]["occurred_at"] == OCCURRED
    assert jobs._aware(source.occurred_at) == OCCURRED
    assert jobs._aware(source.created_at) != OCCURRED
    assert (await _job(seed)).state == "SUCCEEDED"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["read", "commit"])
async def test_forged_frozen_time_rejected_even_if_boundary_hash_is_recomputed(monkeypatch, phase):
    seed, _ = await _dated_turn(monkeypatch)
    lease = await jobs.claim_job("source-time", allowed_user_ids=[seed[0]])
    frozen = await jobs.read_extraction_input(lease)
    async with get_db_session() as db:
        receipt = await db.get(MemoryTurnCompletion, (await _job(seed)).completion_id)
        boundaries = deepcopy(receipt.source_boundaries)
        boundaries[0]["occurred_at"] = (OCCURRED + timedelta(days=1)).isoformat()
        forged_hash = jobs._hash(boundaries)
        receipt.source_boundaries, receipt.input_hash = boundaries, forged_hash
        await db.execute(update(MemoryExtractionJob).where(MemoryExtractionJob.id == lease.job_id).values(input_hash=forged_hash))
    with pytest.raises(jobs.ExtractionSourceInvalid, match="source_occurrence_changed"):
        if phase == "read":
            await jobs.read_extraction_input(lease)
        else:
            await jobs.commit_extraction(lease, frozen, validate_proposals(_proposal(frozen), frozen))
    async with get_db_session() as db:
        assert await db.scalar(select(func.count(MemorySource.id)).where(MemorySource.user_id == seed[0])) == 0


@pytest.mark.asyncio
async def test_legacy_receipt_recovers_same_version_time_without_changing_frozen_hash(monkeypatch):
    seed, _ = await _dated_turn(monkeypatch)
    _, source, frozen = await _candidate_source(seed, legacy_receipt=True)
    assert frozen.sources[0]["occurred_at"] == OCCURRED
    assert jobs._aware(source.occurred_at) == OCCURRED
    async with get_db_session() as db:
        receipt = await db.get(MemoryTurnCompletion, (await _job(seed)).completion_id)
        assert "occurred_at" not in receipt.source_boundaries[0]
        assert receipt.input_hash == frozen.input_hash == jobs._hash(receipt.source_boundaries)


@pytest.mark.asyncio
async def test_legacy_source_recovers_today_at_local_midnight_without_sql_or_job_rewrite(monkeypatch):
    from memory.retrieval import search_memory

    seed, _ = await _dated_turn(monkeypatch)
    memory_id, source, frozen = await _candidate_source(seed, legacy_receipt=True)
    await service.confirm_note(user_id=seed[0], workspace_id=seed[1], proposal_id=memory_id,
                               expected_revision=1)
    async with get_db_session() as db:
        await db.execute(update(MemorySource).where(MemorySource.id == source.id).values(
            occurred_at=None, created_at=OCCURRED + timedelta(days=20)))
    config = config_module.get_config().memory
    config.retrieval_v2 = config.rerank = config.wiki = False

    async def time_context(_db, _user, query, _config):
        return resolve_query_time(query, "Asia/Shanghai", now=OCCURRED + timedelta(minutes=1))

    monkeypatch.setattr("memory.retrieval.query_time_context", time_context)
    today = await search_memory(query="今天中文回复偏好", user_id=seed[0], workspace_id=seed[1],
                                project_id=seed[2], config=config)
    yesterday = await search_memory(query="昨天中文回复偏好", user_id=seed[0], workspace_id=seed[1],
                                    project_id=seed[2], config=config)
    assert today["time_context"]["start_at"] == "2026-10-02T00:00:00+08:00"
    assert len(today["items"]) == 1
    recovered = next(reference for reference in today["items"][0]["sources"] if reference["id"] == source.id)
    assert recovered["occurred_at"] == OCCURRED.isoformat()
    assert yesterday["items"] == []
    async with get_db_session() as db:
        unchanged = await db.get(MemorySource, source.id)
        receipt = await db.get(MemoryTurnCompletion, (await _job(seed)).completion_id)
        assert unchanged.occurred_at is None and jobs._aware(unchanged.created_at) == OCCURRED + timedelta(days=20)
        assert receipt.input_hash == frozen.input_hash == jobs._hash(receipt.source_boundaries)
        assert "occurred_at" not in receipt.source_boundaries[0]
        assert await db.scalar(select(func.count(MemoryExtractionJob.id)).where(MemoryExtractionJob.session_id == seed[3])) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["part_revision", "part_body", "branch", "forgotten", "scope"])
async def test_legacy_fallback_never_dates_unavailable_or_stale_evidence(monkeypatch, change):
    seed, assistant = await _dated_turn(monkeypatch)
    memory_id, source, _ = await _candidate_source(seed)
    async with get_db_session() as db:
        await db.execute(update(MemorySource).where(MemorySource.id == source.id).values(occurred_at=None))
    if change == "part_revision":
        await session_module.save_part(TextPart(id=source.part_id, text="我喜欢简短的中文回复",
            session_id=seed[3], message_id=source.message_id), is_new=False, user_id=seed[0])
    elif change == "part_body":
        async with get_db_session() as db:
            await db.execute(update(Part).where(Part.id == source.part_id).values(data={"text": "已修改原话"}))
    elif change == "branch":
        async with get_db_session() as db:
            owner = await prepare_agent_event_write(db, session_id=seed[3], user_id=seed[0], run_fence=None)
            await append_surface_remove_locked(db, owner, message_ids=[assistant.id])
    elif change == "forgotten":
        await service.forget_memory(user_id=seed[0], workspace_id=seed[1], memory_id=memory_id,
                                    expected_revision=1, mode="sources", source_ids=[source.id])
    if change == "scope":
        other = await _seed(monkeypatch)
    else:
        other = seed
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=other[0], workspace_id=other[1], project_id=other[2])
        current = await db.get(MemorySource, source.id)
        assert await source_occurred_at(db, access=scope, source=current) is None
        assert current.occurred_at is None
        assert await db.get(Message, source.message_id) is not None


@pytest.mark.asyncio
async def test_unknown_manual_time_never_uses_note_or_source_storage_time(monkeypatch):
    seed = await _seed(monkeypatch)
    note = await service.create_note(user_id=seed[0], workspace_id=seed[1], project_id=seed[2],
                                     summary="手工笔记日期未知")
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=seed[0], workspace_id=seed[1], project_id=seed[2])
        source = await db.scalar(select(MemorySource).join(MemorySourceLink,
            MemorySourceLink.source_id == MemorySource.id).where(MemorySourceLink.memory_id == note["id"]))
        assert source.created_at is not None and source.occurred_at is None
        assert await source_occurred_at(db, access=scope, source=source) is None
