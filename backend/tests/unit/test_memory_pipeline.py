"""Extraction correctness under real Inbox/Event boundaries and SQL leases."""
from datetime import datetime, timedelta, timezone
import asyncio
import os
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, update

from agent import inbox
from agent.driver import reserve_run
from core.config import OpenBoxConfig
from db.base import Base, close_engine, get_db_session, init_engine
from db.models.memory import UserMemory
from db.models.memory_pipeline import MemoryExtractionCursor, MemoryExtractionJob, MemoryTurnCompletion
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceMember
from memory import jobs, service
from memory.extraction import ConfiguredMemoryExtractor, ExtractionProviderError, ExtractionSchemaError, MemoryExtractionWorker, validate_proposals
from models.message import TextPart
from session.agent_event_log import append_surface_remove_locked, prepare_agent_event_write
from session.session import create_assistant_message, save_part, update_message_info


@pytest.fixture(autouse=True)
async def pipeline_database(tmp_path):
    await close_engine()
    database_url = os.environ.get("MEMORY_PIPELINE_TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path / 'pipeline.db'}"
    engine = init_engine(database_url)
    import db.models  # noqa: F401
    if engine.dialect.name == "sqlite":
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    yield
    await close_engine()


async def _seed(monkeypatch):
    suffix = uuid4().hex[:12]
    user_id, workspace_id, project_id, session_id = (f"{kind}-{suffix}" for kind in ("usr", "ws", "prj", "ses"))
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=user_id, username=user_id, created_at=now, updated_at=now))
        await db.flush()
        db.add(Workspace(id=workspace_id, owner_user_id=user_id, name="Pipeline", created_at=now, updated_at=now))
        await db.flush()
        db.add(WorkspaceMember(user_id=user_id, workspace_id=workspace_id, role="owner", status="active", created_at=now, updated_at=now))
        db.add(Project(id=project_id, user_id=user_id, workspace_id=workspace_id, name="Pipeline", created_at=now, updated_at=now))
        await db.flush()
        db.add(Session(id=session_id, user_id=user_id, workspace_id=workspace_id, project_id=project_id,
                       model="test/model", agent="build", status="idle", token_usage={},
                       tool_exposure_state={}, created_at=now, updated_at=now))
        await db.execute(update(User).where(User.id == user_id).values(default_workspace_id=workspace_id))
    config = OpenBoxConfig(model="test/model", memory={"auto_extract": True, "automatic_knowledge": False, "v2_write": True, "allowed_user_ids": [user_id]})
    monkeypatch.setattr("core.config.get_config", lambda: config)
    return user_id, workspace_id, project_id, session_id


async def _finish_turn(seed, *, text="我喜欢简短的中文回复", finish="stop", memory_success=True, release=True):
    user_id, _workspace, _project, session_id = seed
    accepted = await inbox.accept_inbox_item(session_id=session_id, user_id=user_id,
                                             delivery="followup", prompt=text, client_id=uuid4().hex)
    lease = await reserve_run(session_id, user_id)
    fence = (session_id, lease.run_id, lease.generation)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    trigger = batch.receipts[0].message_id
    assistant = await create_assistant_message(session_id, trigger, model_id="test/model", agent="build",
                                               user_id=user_id, run_fence=fence)
    await save_part(TextPart(text="好的，我会按你的要求回复。", session_id=session_id, message_id=assistant.id),
                    is_new=True, user_id=user_id, run_fence=fence)
    assistant.finish = finish
    await update_message_info(assistant, user_id=user_id, run_fence=fence)
    await inbox.settle_claimed_inbox_items(lease, result_message_id=assistant.id,
                                           outcome="succeeded", memory_success=memory_success)
    if release:
        await lease.release(session_status="idle")
    return accepted, lease, assistant


async def _job(seed, ordinal=1):
    async with get_db_session() as db:
        return await db.scalar(select(MemoryExtractionJob).where(
            MemoryExtractionJob.session_id == seed[3], MemoryExtractionJob.ordinal == ordinal))


def _proposal(frozen):
    return {"candidates": [{"type": "PREFERENCE", "summary": "用户喜欢简短的中文回复", "fact_key": "personal.reply_style",
                             "confidence": 60, "source_indexes": [0],
                             "quotes": [{"source_index": 0, "quote": "我喜欢简短的中文回复"}]}]}


@pytest.mark.asyncio
async def test_completion_is_atomic_idempotent_and_frozen(monkeypatch):
    seed = await _seed(monkeypatch)
    accepted, lease, assistant = await _finish_turn(seed, release=False)
    try:
        await inbox.settle_claimed_inbox_items(lease, result_message_id=assistant.id,
                                               outcome="succeeded", memory_success=True)
        async with get_db_session() as db:
            receipts = list((await db.scalars(select(MemoryTurnCompletion).where(MemoryTurnCompletion.session_id == seed[3]))).all())
            rows = list((await db.scalars(select(MemoryExtractionJob).where(MemoryExtractionJob.session_id == seed[3]))).all())
        assert len(receipts) == len(rows) == 1
        receipt = receipts[0]
        assert receipt.logical_turn_id == (await inbox.get_inbox_item(accepted.id, user_id=seed[0])).turn_id
        assert receipt.source_boundaries[0]["source_revision"] > 1
        assert "body" not in receipt.source_boundaries[0]
        before = receipt.input_hash
    finally:
        await lease.release(session_status="idle")
    await _finish_turn(seed, text="这个项目需要严格测试")
    assert (await _job(seed, 1)).input_hash == before
    lease = await jobs.claim_job("extract-test", allowed_user_ids=[seed[0]])
    frozen = await jobs.read_extraction_input(lease)
    assert [source["body"] for source in frozen.sources] == ["我喜欢简短的中文回复"]


@pytest.mark.asyncio
async def test_schedule_failure_keeps_receipt_and_recovery_repairs(monkeypatch):
    seed = await _seed(monkeypatch)
    original = jobs.enqueue_completion_locked
    async def broken(*_args, **_kwargs):
        raise RuntimeError("simulated schedule crash")
    monkeypatch.setattr(jobs, "enqueue_completion_locked", broken)
    accepted, _, _ = await _finish_turn(seed)
    assert (await inbox.get_inbox_item(accepted.id, user_id=seed[0])).state == "settled"
    async with get_db_session() as db:
        assert await db.scalar(select(func.count(MemoryTurnCompletion.id)).where(MemoryTurnCompletion.session_id == seed[3])) == 1
    assert await _job(seed) is None
    monkeypatch.setattr(jobs, "enqueue_completion_locked", original)
    assert await jobs.recover_extraction_jobs(include_inbox=False) == 1
    assert await jobs.recover_extraction_jobs(include_inbox=False) == 0
    assert (await _job(seed)).state == "PENDING"


@pytest.mark.asyncio
async def test_waiting_is_not_success_and_old_history_is_not_backfilled(monkeypatch):
    seed = await _seed(monkeypatch)
    await _finish_turn(seed, finish="waiting_input")
    assert await _job(seed) is None
    # A pre-rollout successful Inbox record may remain historical evidence;
    # online compensation has no authorization to import it automatically.
    from db.models.memory_pipeline import MemoryPipelineEnrollment
    async with get_db_session() as db:
        enrollment = await db.get(MemoryPipelineEnrollment, (seed[0], seed[1], jobs.PIPELINE_VERSION))
        enrollment.eligible_since = datetime.now(timezone.utc) + timedelta(seconds=20)
    await _finish_turn(seed, memory_success=False)
    assert await jobs.recover_extraction_jobs() == 0
    assert await _job(seed) is None


@pytest.mark.asyncio
async def test_expired_worker_cannot_commit_after_takeover(monkeypatch):
    seed = await _seed(monkeypatch)
    await _finish_turn(seed)
    first = await jobs.claim_job("worker-one", allowed_user_ids=[seed[0]])
    frozen = await jobs.read_extraction_input(first)
    async with get_db_session() as db:
        await db.execute(update(MemoryExtractionJob).where(MemoryExtractionJob.id == first.job_id).values(
            lease_until=datetime.now(timezone.utc) - timedelta(seconds=5)))
    second = await jobs.claim_job("worker-two", allowed_user_ids=[seed[0]])
    assert second.job_id == first.job_id and second.generation > first.generation
    with pytest.raises(jobs.ExtractionLeaseLost):
        await jobs.commit_extraction(first, frozen, validate_proposals(_proposal(frozen), frozen))
    assert await jobs.renew_job(first) is False
    second_input = await jobs.read_extraction_input(second)
    ids = await jobs.commit_extraction(second, second_input, validate_proposals(_proposal(second_input), second_input))
    async with get_db_session() as db:
        memory = await db.get(UserMemory, ids[0])
        assert memory.status == "CANDIDATE" and memory.confirmation_status == "PENDING"
        assert memory.owner == "SYSTEM_INFERRED"


@pytest.mark.asyncio
async def test_competing_process_claims_have_one_winner(monkeypatch):
    seed = await _seed(monkeypatch)
    await _finish_turn(seed)
    leases = await asyncio.gather(jobs.claim_job("one", allowed_user_ids=[seed[0]]),
                                  jobs.claim_job("two", allowed_user_ids=[seed[0]]))
    winners = [lease for lease in leases if lease is not None]
    assert len(winners) == 1
    assert (await _job(seed)).attempts == 1


@pytest.mark.asyncio
async def test_direct_regeneration_freezes_original_user_on_new_branch(monkeypatch):
    seed = await _seed(monkeypatch)
    accepted, _, original = await _finish_turn(seed)
    trigger = (await inbox.get_inbox_item(accepted.id, user_id=seed[0])).message_id
    async with get_db_session() as db:
        owner = await prepare_agent_event_write(db, session_id=seed[3], user_id=seed[0], run_fence=None)
        await append_surface_remove_locked(db, owner, message_ids=[original.id])
    lease = await reserve_run(seed[3], seed[0], trigger_message_id=trigger)
    fence = (seed[3], lease.run_id, lease.generation)
    try:
        assistant = await create_assistant_message(seed[3], trigger, user_id=seed[0], run_fence=fence)
        await save_part(TextPart(text="重新回答，好的。", session_id=seed[3], message_id=assistant.id),
                        is_new=True, user_id=seed[0], run_fence=fence)
        assistant.finish = "stop"
        await update_message_info(assistant, user_id=seed[0], run_fence=fence)
        assert await inbox.settle_claimed_inbox_items(lease, result_message_id=assistant.id,
                                                       outcome="succeeded", memory_success=True) == ()
    finally:
        await lease.release(session_status="idle")
    async with get_db_session() as db:
        receipts = list((await db.scalars(select(MemoryTurnCompletion).where(MemoryTurnCompletion.session_id == seed[3]))).all())
        assert len(receipts) == 2
        current = next(row for row in receipts if row.result_message_id == assistant.id)
        assert current.branch_id != "root" and current.logical_turn_id == trigger
        assert current.source_boundaries[0]["message_id"] == trigger
    worker = MemoryExtractionWorker(extractor=lambda frozen: _proposal(frozen))
    assert {await worker.run_once(), await worker.run_once()} == {"CANCELLED", "SUCCEEDED"}


@pytest.mark.asyncio
async def test_dead_boundary_cannot_be_skipped_by_later_success(monkeypatch):
    seed = await _seed(monkeypatch)
    await _finish_turn(seed)
    await _finish_turn(seed, text="以后默认每次都运行测试")
    first = await jobs.claim_job("worker", allowed_user_ids=[seed[0]])
    assert await jobs.fail_job(first, "invalid_schema", permanent=True)
    second = await jobs.claim_job("worker", allowed_user_ids=[seed[0]])
    await jobs.commit_extraction(second, await jobs.read_extraction_input(second), [])
    async with get_db_session() as db:
        cursor = await db.get(MemoryExtractionCursor, (seed[3], "root", jobs.PIPELINE_VERSION))
        assert cursor.completed_ordinal == 0
    assert await jobs.replay_job(first.job_id, user_id=seed[0], workspace_id=seed[1])
    replay = await jobs.claim_job("worker", allowed_user_ids=[seed[0]])
    await jobs.commit_extraction(replay, await jobs.read_extraction_input(replay), [])
    async with get_db_session() as db:
        cursor = await db.get(MemoryExtractionCursor, (seed[3], "root", jobs.PIPELINE_VERSION))
        assert cursor.completed_ordinal == 2


@pytest.mark.asyncio
async def test_user_correction_wins_over_inflight_inference(monkeypatch):
    seed = await _seed(monkeypatch)
    await _finish_turn(seed)
    lease = await jobs.claim_job("worker", allowed_user_ids=[seed[0]])
    frozen = await jobs.read_extraction_input(lease)
    direct = await service.create_note(user_id=seed[0], workspace_id=seed[1], project_id=seed[2],
                                     summary="用户现在需要详细的英文回复", fact_key="personal.reply_style")
    with pytest.raises(jobs.ExtractionBaseRevisionChanged):
        await jobs.commit_extraction(lease, frozen, validate_proposals(_proposal(frozen), frozen))
    fresh = await jobs.read_extraction_input(lease)
    ids = await jobs.commit_extraction(lease, fresh, validate_proposals(_proposal(fresh), fresh))
    assert ids == [direct["id"]]
    async with get_db_session() as db:
        row = await db.get(UserMemory, direct["id"])
        assert row.status == "ACTIVE" and row.value["summary"] == "用户现在需要详细的英文回复"


@pytest.mark.asyncio
async def test_source_edit_and_abandoned_branch_cancel_old_commit(monkeypatch):
    seed = await _seed(monkeypatch)
    _, _, assistant = await _finish_turn(seed)
    lease = await jobs.claim_job("worker", allowed_user_ids=[seed[0]])
    frozen = await jobs.read_extraction_input(lease)
    async with get_db_session() as db:
        part = await db.get(Part, frozen.sources[0]["part_id"])
        part.data = {**part.data, "text": "这条原文已修改"}
    with pytest.raises(jobs.ExtractionSourceInvalid, match="source_revision_changed"):
        await jobs.commit_extraction(lease, frozen, validate_proposals(_proposal(frozen), frozen))
    async with get_db_session() as db:
        session = await prepare_agent_event_write(db, session_id=seed[3], user_id=seed[0], run_fence=None)
        await append_surface_remove_locked(db, session, message_ids=[assistant.id])
    with pytest.raises(jobs.ExtractionSourceInvalid, match="branch_abandoned"):
        await jobs.read_extraction_input(lease)


@pytest.mark.asyncio
async def test_worker_schema_validation_retry_and_suppression(monkeypatch):
    seed = await _seed(monkeypatch)
    await _finish_turn(seed)
    async def extractor(frozen):
        return _proposal(frozen)
    worker = MemoryExtractionWorker(extractor=extractor)
    assert await worker.run_once() == "SUCCEEDED"
    job = await _job(seed)
    assert len(job.result_memory_ids) == 1
    assert await service.reject_note(user_id=seed[0], workspace_id=seed[1], proposal_id=job.result_memory_ids[0])
    await _finish_turn(seed)
    assert await worker.run_once() == "SUCCEEDED"
    assert (await _job(seed, 2)).result_memory_ids == []
    await _finish_turn(seed)
    async def malicious(frozen):
        proposal = _proposal(frozen)
        proposal["candidates"][0]["user_id"] = "another-user"
        return proposal
    worker.extractor = malicious
    assert await worker.run_once() == "DEAD"
    assert (await _job(seed, 3)).last_error == "invalid_candidate_fields"


@pytest.mark.asyncio
async def test_provider_receives_only_authorized_frozen_source_and_usage(monkeypatch):
    seed = await _seed(monkeypatch)
    await _finish_turn(seed)
    lease = await jobs.claim_job("provider-test", allowed_user_ids=[seed[0]])
    frozen = await jobs.read_extraction_input(lease)
    requests = []
    def handler(request):
        payload = __import__("json").loads(request.content)
        requests.append(payload)
        assert seed[0] not in request.content.decode()
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": __import__("json").dumps(_proposal(frozen))}}],
                                         "usage": {"prompt_tokens": 31, "completion_tokens": 17}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        extractor = ConfiguredMemoryExtractor(model="test/model", base_url="https://model.invalid/v1",
                                               api_key="test-secret", client=client)
        result = await extractor(frozen)
    assert len(requests) == 1
    assert result.usage["input_tokens"] == 31 and result.usage["output_tokens"] == 17
    assert result.proposals[0]["quotes"][0]["quote"] == frozen.sources[0]["body"]
    with pytest.raises(ExtractionSchemaError, match="unsupported_quote"):
        bad = _proposal(frozen)
        bad["candidates"][0]["quotes"][0]["quote"] = "助手猜测用户喜欢英文"
        validate_proposals(bad, frozen)


@pytest.mark.parametrize("change", ["session_deleted", "branch_removed", "source_modified"])
async def test_backfill_preview_omits_unavailable_frozen_boundaries(monkeypatch, change):
    seed = await _seed(monkeypatch)
    _, _, assistant = await _finish_turn(seed)
    now = datetime.now(timezone.utc)
    arguments = dict(user_id=seed[0], workspace_id=seed[1], project_id=seed[2],
                     start_at=now - timedelta(days=1), end_at=now + timedelta(minutes=1), limit=10)
    preview = await jobs.backfill_dry_run(**arguments)
    assert len(preview) == 1 and "body" not in __import__("json").dumps(preview)
    async with get_db_session() as db:
        count_before = await db.scalar(select(func.count(MemoryExtractionJob.id)).where(MemoryExtractionJob.session_id == seed[3]))
        if change == "session_deleted":
            await db.execute(update(Session).where(Session.id == seed[3]).values(is_deleted=True))
        elif change == "branch_removed":
            owner = await prepare_agent_event_write(db, session_id=seed[3], user_id=seed[0], run_fence=None)
            await append_surface_remove_locked(db, owner, message_ids=[assistant.id])
        else:
            receipt = await db.scalar(select(MemoryTurnCompletion).where(MemoryTurnCompletion.session_id == seed[3]))
            part = await db.get(Part, receipt.source_boundaries[0]["part_id"])
            part.data = {**part.data, "text": "原文已经更正"}
    assert await jobs.backfill_dry_run(**arguments) == []
    async with get_db_session() as db:
        assert await db.scalar(select(func.count(MemoryExtractionJob.id)).where(MemoryExtractionJob.session_id == seed[3])) == count_before


async def test_source_forget_blocks_provider_ingestion_from_retained_chat(monkeypatch):
    from db.models.memory_v2 import MemorySourceLink

    seed = await _seed(monkeypatch)
    await _finish_turn(seed)
    worker = MemoryExtractionWorker(extractor=lambda frozen: _proposal(frozen))
    assert await worker.run_once() == "SUCCEEDED"
    memory_id = (await _job(seed)).result_memory_ids[0]
    confirmed = await service.confirm_note(user_id=seed[0], workspace_id=seed[1], proposal_id=memory_id)
    async with get_db_session() as db:
        source_id = await db.scalar(select(MemorySourceLink.source_id).where(
            MemorySourceLink.memory_id == memory_id, MemorySourceLink.revision == 1))
    assert (await service.forget_memory(user_id=seed[0], workspace_id=seed[1], memory_id=memory_id,
        expected_revision=confirmed["revision"], mode="sources", source_ids=[source_id]))["ok"]
    await _finish_turn(seed)
    calls = []
    async def forbidden_provider(frozen):
        calls.append(frozen)
        return _proposal(frozen)
    worker.extractor = forbidden_provider
    assert await worker.run_once() == "CANCELLED"
    assert calls == [] and (await _job(seed, 2)).last_error == "source_forgotten"
    now = datetime.now(timezone.utc)
    assert await jobs.backfill_dry_run(user_id=seed[0], workspace_id=seed[1], project_id=seed[2],
        start_at=now - timedelta(days=1), end_at=now + timedelta(minutes=1)) == []


async def test_v2_write_gate_blocks_provider_and_inflight_candidate_commit(monkeypatch):
    seed = await _seed(monkeypatch)
    from core.config import get_config
    await _finish_turn(seed)
    lease = await jobs.claim_job("gate-check", allowed_user_ids=[seed[0]])
    frozen = await jobs.read_extraction_input(lease)
    get_config().memory.v2_write = False
    with pytest.raises(jobs.ExtractionSourceInvalid, match="extraction_write_disabled"):
        await jobs.read_extraction_input(lease)
    with pytest.raises(jobs.ExtractionSourceInvalid, match="extraction_write_disabled"):
        await jobs.commit_extraction(lease, frozen, validate_proposals(_proposal(frozen), frozen))
    async with get_db_session() as db:
        assert await db.scalar(select(func.count(UserMemory.id)).where(UserMemory.user_id == seed[0])) == 0
    assert await jobs.fail_job(lease, "extraction_write_disabled", cancelled=True)
    await _finish_turn(seed)
    calls = []
    async def forbidden_provider(frozen):
        calls.append(frozen)
        return _proposal(frozen)
    worker = MemoryExtractionWorker(extractor=forbidden_provider)
    assert await worker.run_once() == "CANCELLED" and calls == []


async def _postgres_only():
    async with get_db_session() as db:
        if db.get_bind().dialect.name != "postgresql":
            pytest.skip("This test verifies PostgreSQL actor row-lock ordering")


async def _repeat_after_candidate(seed):
    await _finish_turn(seed)
    first = await jobs.claim_job("first", allowed_user_ids=[seed[0]])
    frozen = await jobs.read_extraction_input(first)
    memory_id = (await jobs.commit_extraction(first, frozen, validate_proposals(_proposal(frozen), frozen)))[0]
    await _finish_turn(seed)
    lease = await jobs.claim_job("repeat", allowed_user_ids=[seed[0]])
    return memory_id, lease, await jobs.read_extraction_input(lease)


async def test_postgres_forget_waits_for_checked_worker_and_removes_final_result(monkeypatch):
    await _postgres_only()
    seed = await _seed(monkeypatch)
    memory_id, lease, frozen = await _repeat_after_candidate(seed)
    checked, proceed = asyncio.Event(), asyncio.Event()
    original = service.is_candidate_suppressed
    async def pause_after_check(*args, **kwargs):
        suppressed = await original(*args, **kwargs)
        checked.set()
        await proceed.wait()
        return suppressed
    monkeypatch.setattr(service, "is_candidate_suppressed", pause_after_check)
    commit = asyncio.create_task(jobs.commit_extraction(lease, frozen, validate_proposals(_proposal(frozen), frozen)))
    forget = None
    try:
        await asyncio.wait_for(checked.wait(), timeout=5)
        forget = asyncio.create_task(service.forget_memory(user_id=seed[0], workspace_id=seed[1],
            memory_id=memory_id, expected_revision=1))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(forget), timeout=0.2)
        proceed.set()
        assert await asyncio.wait_for(commit, timeout=5) == [memory_id]
        assert (await asyncio.wait_for(forget, timeout=5))["ok"]
        async with get_db_session() as db:
            rows = list((await db.scalars(select(UserMemory).where(UserMemory.user_id == seed[0]))).all())
        assert len(rows) == 1 and rows[0].deleted_at is not None
    finally:
        proceed.set()
        await asyncio.gather(commit, *([forget] if forget else []), return_exceptions=True)


async def test_postgres_worker_waits_for_source_forget_and_never_recreates(monkeypatch):
    from db.models.memory_v2 import MemorySourceLink

    await _postgres_only()
    seed = await _seed(monkeypatch)
    memory_id, lease, frozen = await _repeat_after_candidate(seed)
    async with get_db_session() as db:
        source_id = await db.scalar(select(MemorySourceLink.source_id).where(
            MemorySourceLink.memory_id == memory_id, MemorySourceLink.revision == 1))
    tombstone_written, proceed = asyncio.Event(), asyncio.Event()
    original = service.enqueue_memory_outbox
    async def pause_before_commit(*args, **kwargs):
        result = await original(*args, **kwargs)
        tombstone_written.set()
        await proceed.wait()
        return result
    monkeypatch.setattr(service, "enqueue_memory_outbox", pause_before_commit)
    forget = asyncio.create_task(service.forget_memory(user_id=seed[0], workspace_id=seed[1], memory_id=memory_id,
        expected_revision=1, mode="sources", source_ids=[source_id]))
    commit = None
    try:
        await asyncio.wait_for(tombstone_written.wait(), timeout=5)
        commit = asyncio.create_task(jobs.commit_extraction(lease, frozen, validate_proposals(_proposal(frozen), frozen)))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(commit), timeout=0.2)
        proceed.set()
        assert (await asyncio.wait_for(forget, timeout=5))["ok"]
        with pytest.raises(jobs.ExtractionSourceInvalid, match="source_forgotten"):
            await asyncio.wait_for(commit, timeout=5)
        async with get_db_session() as db:
            assert await db.scalar(select(func.count(UserMemory.id)).where(UserMemory.user_id == seed[0])) == 1
        assert await jobs.fail_job(lease, "source_forgotten", cancelled=True)
    finally:
        proceed.set()
        await asyncio.gather(forget, *([commit] if commit else []), return_exceptions=True)


@pytest.mark.parametrize("responses,terminal,complete", [
    (False, "stop", True), (False, "length", False), (False, None, False),
    (True, "completed", True), (True, "incomplete", False), (True, None, False),
])
async def test_extraction_requires_provider_completion_and_missing_usage_stays_unknown(monkeypatch, responses, terminal, complete):
    seed = await _seed(monkeypatch)
    from core.config import get_config
    config = get_config()
    config.memory.extraction_timeout_seconds = 75
    config.memory.provider_timeout_seconds = 3
    await _finish_turn(seed)
    lease = await jobs.claim_job("provider-completion", allowed_user_ids=[seed[0]])
    frozen = await jobs.read_extraction_input(lease)
    monkeypatch.setattr("agent.llm._needs_responses_api", lambda _model: responses)
    def handler(request):
        assert request.extensions["timeout"]["read"] == 75
        proposal = __import__("json").dumps(_proposal(frozen))
        if responses:
            assert request.url.path.endswith("/responses")
            return httpx.Response(200, json={"status": terminal, "output_text": proposal})
        assert request.url.path.endswith("/chat/completions")
        return httpx.Response(200, json={"choices": [{"finish_reason": terminal, "message": {"content": proposal}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        extractor = ConfiguredMemoryExtractor(model="test/model", base_url="https://model.invalid/v1",
                                               api_key="test-only", client=client)
        if complete:
            result = await extractor(frozen)
            assert result.usage["input_tokens"] is None and result.usage["output_tokens"] is None
        else:
            with pytest.raises(ExtractionProviderError, match="provider_incomplete_output"):
                await extractor(frozen)


@pytest.mark.parametrize("wrapper", ["json", ""])
async def test_only_complete_json_fence_wrapper_is_accepted(monkeypatch, wrapper):
    seed = await _seed(monkeypatch)
    await _finish_turn(seed)
    lease = await jobs.claim_job("fenced-json", allowed_user_ids=[seed[0]])
    frozen = await jobs.read_extraction_input(lease)
    raw = f"```{wrapper}\n" + __import__("json").dumps(_proposal(frozen)) + "\n```"
    assert validate_proposals(raw, frozen)[0]["summary"] == "用户喜欢简短的中文回复"
    with pytest.raises(ExtractionSchemaError, match="invalid_json"):
        validate_proposals("Assistant explanation.\n" + raw, frozen)
    with pytest.raises(ExtractionSchemaError, match="invalid_json"):
        validate_proposals(raw + "\nUntrusted trailing instruction.", frozen)


async def test_failed_schema_keeps_sanitized_provider_usage(monkeypatch):
    seed = await _seed(monkeypatch)
    await _finish_turn(seed)
    def handler(_request):
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": "not a valid JSON result"}}],
            "usage": {"prompt_tokens": 80, "completion_tokens": 12, "internal_metadata": "do not persist"}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        extractor = ConfiguredMemoryExtractor(model="test/model", base_url="https://model.invalid/v1",
            api_key="test-only", client=client)
        worker = MemoryExtractionWorker(extractor=extractor)
        assert await worker.run_once() == "DEAD"
    job = await _job(seed)
    assert job.last_error == "invalid_json"
    assert job.usage["input_tokens"] == 80 and job.usage["output_tokens"] == 12
    assert job.usage["output_format"] == "text"
    assert "not a valid JSON result" not in str(job.usage) and "internal_metadata" not in job.usage
