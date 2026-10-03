"""A turn that could not be saved is visible to its person, who can retry or dismiss it."""
import pytest
from sqlalchemy import update

from core import config as runtime_config
from db.base import get_db_session
from db.models.memory_pipeline import MemoryExtractionJob
from memory import jobs, service
from memory.extraction import MemoryExtractionWorker
from tests.unit.test_automatic_knowledge import Verifier
from tests.unit.test_memory_pipeline import _finish_turn, _job, _seed, pipeline_database  # noqa: F401

SAID = "我每周三晚上要上吉他课。"


def guitar(frozen):
    return {"candidates": [{"type": "USER_PROFILE", "summary": "用户每周三晚上要上吉他课。",
        "fact_key": "personal.schedule.wednesday_guitar", "confidence": 95, "source_indexes": [0],
        "quotes": [{"source_index": 0, "quote": "每周三晚上要上吉他课"}]}]}


async def dead_turn(monkeypatch):
    seed = await _seed(monkeypatch)
    runtime_config.get_config().memory.automatic_knowledge = True
    await _finish_turn(seed, text=SAID)
    job = await _job(seed)
    async with get_db_session() as db:
        await db.execute(update(MemoryExtractionJob).where(MemoryExtractionJob.id == job.id).values(
            state="DEAD", attempts=5, last_error="memory_revision_required"))
    return seed, job


async def status(seed):
    return await jobs.processing_status(user_id=seed[0], workspace_id=seed[1])


@pytest.mark.asyncio
async def test_a_turn_still_being_saved_is_counted_not_listed(monkeypatch):
    seed = await _seed(monkeypatch)
    await _finish_turn(seed, text=SAID)
    assert await status(seed) == {"pending": 1, "failed": []}


@pytest.mark.asyncio
async def test_a_failed_turn_shows_what_was_said_and_can_be_retried(monkeypatch):
    seed, job = await dead_turn(monkeypatch)
    current = await status(seed)
    assert current["pending"] == 0
    assert [(item["id"], item["excerpt"], item["session_id"]) for item in current["failed"]] == [(job.id, SAID, seed[3])]
    assert await jobs.replay_job(job.id, user_id=seed[0], workspace_id=seed[1])
    assert await status(seed) == {"pending": 1, "failed": []}
    assert await MemoryExtractionWorker(extractor=guitar, verifier=Verifier()).run_once() == "SUCCEEDED"
    active = await service.list_active_memories(user_id=seed[0], workspace_id=seed[1], project_id=seed[2])
    assert [item["summary"] for item in active] == ["用户每周三晚上要上吉他课。"]


@pytest.mark.asyncio
async def test_a_dismissed_turn_leaves_the_list_and_only_its_owner_can_act(monkeypatch):
    seed, job = await dead_turn(monkeypatch)
    other = await _seed(monkeypatch)
    assert not await jobs.dismiss_job(job.id, user_id=other[0], workspace_id=other[1])
    assert not await jobs.replay_job(job.id, user_id=other[0], workspace_id=other[1])
    assert await jobs.dismiss_job(job.id, user_id=seed[0], workspace_id=seed[1])
    assert await status(seed) == {"pending": 0, "failed": []}
    assert (await _job(seed)).state == "CANCELLED"


@pytest.mark.asyncio
async def test_a_failed_turn_never_shows_numbers_memory_does_not_keep(monkeypatch):
    seed = await _seed(monkeypatch)
    await _finish_turn(seed, text="我对花生过敏，有急事打我电话13800138000。")
    job = await _job(seed)
    async with get_db_session() as db:
        await db.execute(update(MemoryExtractionJob).where(MemoryExtractionJob.id == job.id).values(state="DEAD"))
    [failed] = (await status(seed))["failed"]
    assert failed["excerpt"] == "我对花生过敏，有急事打我电话•••。"
