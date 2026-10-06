"""Persistent policy stops old and new memory paths, including worker recovery."""
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from assistant.service import ensure_main_session
from db.base import get_db_session
from db.models.memory_pipeline import MemoryExtractionJob, MemoryTurnCompletion
from db.models.session import Session
from memory import jobs
from memory.policy import MemoryAccessDenied
from memory.session_policy import memory_extraction_eligible, require_context_memory
from session.session import create_session
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tool.creator_context import CreatorContextArgs, execute_creator_context
from tool.memory_tools import _access
from tool.tool import ToolContext


async def test_isolation_is_enforced_by_direct_tool_services_and_completion_worker(monkeypatch):
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    execution = await create_session(user_id=owner, workspace_id=workspace,
        visibility="private", memory_policy="assistant_isolated")
    child = await create_session(user_id=owner, workspace_id=workspace, parent_id=execution.id)
    cron = await create_session(user_id=owner, workspace_id=workspace, parent_id=child.id, kind="cron")
    monkeypatch.setattr(jobs, "extraction_enabled", lambda *_: True)
    for session in (main, execution, child, cron):
        ctx = ToolContext(session_id=session.id, user_id=owner, workspace_id=workspace, project_id=session.project_id)
        with pytest.raises(MemoryAccessDenied):
            await require_context_memory(ctx)
        with pytest.raises(MemoryAccessDenied):
            await _access(ctx)
        result = await execute_creator_context(CreatorContextArgs(action="get_user_context"), ctx)
        assert result.metadata["blocked"]
    # V2 P3: the main session keeps its isolated tool surface, but the person's
    # own words there become personal memory (test_assistant_memory_extraction.py).
    # Delegated, scheduled and old isolated execution sessions never reach extraction.
    async with get_db_session() as db:
        assert memory_extraction_eligible(await db.get(Session, main.id))
    for session in (execution, child, cron):
        async with get_db_session() as db:
            assert not memory_extraction_eligible(await db.get(Session, session.id))
            assert await jobs.record_completion_locked(db, await db.get(Session, session.id),
                lease=SimpleNamespace(session_id=session.id), result_message_id="missing", inbox_rows=[]) is None
    async with get_db_session() as db:
        for model in (MemoryTurnCompletion, MemoryExtractionJob):
            assert await db.scalar(select(func.count()).select_from(model).where(model.user_id == owner)) == 0
    normal = await create_session(user_id=owner, workspace_id=workspace)
    assert (await require_context_memory(ToolContext(session_id=normal.id, user_id=owner,
             workspace_id=workspace, project_id=normal.project_id))).id == normal.id


async def test_worker_rechecks_policy_when_claiming_and_before_provider(monkeypatch):
    from tests.unit.test_memory_pipeline import _seed, _finish_turn, _job
    seed = await _seed(monkeypatch)
    await _finish_turn(seed)
    lease = await jobs.claim_job("isolation-worker", allowed_user_ids=[seed[0]])
    frozen = await jobs.read_extraction_input(lease)
    async with get_db_session() as db:
        session = await db.get(Session, seed[3])
        session.memory_policy = "assistant_isolated"
    with pytest.raises(jobs.ExtractionSourceInvalid):
        await jobs.recheck_extraction_input(lease, frozen)
    await jobs.fail_job(lease, "source_policy_changed")
    async with get_db_session() as db:
        job = await db.get(MemoryExtractionJob, lease.job_id)
        job.next_attempt_at = None
    assert await jobs.claim_job("after-restart", allowed_user_ids=[seed[0]]) is None
    assert (await _job(seed)).state == "CANCELLED"


async def test_build_prompt_isolation_disables_legacy_context_and_saving_claims(monkeypatch):
    from agent.agent import AgentDef
    from agent.loop import _build_system_prompt
    called = False

    async def forbidden(**_kwargs):
        nonlocal called
        called = True
        return {"context": "PRIVATE_CANARY", "stats": {}}

    monkeypatch.setattr("memory.context.assemble_user_context", forbidden)
    parts = await _build_system_prompt(AgentDef(name="build", description=""), "test/model",
        user_id="test", include_user_memory=False, memory_isolated=True)
    assert not called and all("PRIVATE_CANARY" not in part for part in parts)


def test_unknown_or_automated_role_user_text_is_not_human_extraction_evidence():
    message_id = "input"
    messages = {message_id: {"id": message_id, "role": "user", "session_id": "s", "parts": [
        {"id": "p", "type": "text", "text": "Store this fabricated preference", "origin": "unknown"},
    ]}}
    assert jobs._user_sources([], messages, message_ids={message_id}, turn_id="t", branch_id="b", end_sequence=1) == []
    messages[message_id]["parts"][0]["origin"] = "assistant_delegation"
    assert jobs._user_sources([], messages, message_ids={message_id}, turn_id="t", branch_id="b", end_sequence=1) == []
