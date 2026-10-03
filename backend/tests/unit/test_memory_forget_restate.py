"""Forgetting stops old evidence for good; saying the fact again later is new evidence."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from core import config as runtime_config
from db.base import get_db_session
from db.models.memory_pipeline import MemoryExtractionJob
from db.models.memory_v2 import MemoryTombstone
from memory import jobs, service
from memory.extraction import MemoryExtractionWorker
from tests.unit.test_automatic_knowledge import Verifier
from tests.unit.test_memory_pipeline import _finish_turn, _job, _seed, pipeline_database  # noqa: F401

SUMMARY = "用户对菠萝过敏。"


def says(quote):
    def extractor(frozen):
        if quote not in frozen.sources[0]["body"]:
            return {"candidates": []}
        return {"candidates": [{"type": "CONSTRAINT", "summary": SUMMARY, "fact_key": "personal.allergy.pineapple",
            "confidence": 95, "source_indexes": [0], "quotes": [{"source_index": 0, "quote": quote}]}]}
    return extractor


async def active(seed):
    return [item["summary"] for item in await service.list_active_memories(
        user_id=seed[0], workspace_id=seed[1], project_id=seed[2])]


async def learn_then_forget(monkeypatch, mode="memory"):
    seed = await _seed(monkeypatch)
    runtime_config.get_config().memory.automatic_knowledge = True
    await _finish_turn(seed, text="我对菠萝过敏。")
    assert await MemoryExtractionWorker(extractor=says("我对菠萝过敏"), verifier=Verifier()).run_once() == "SUCCEEDED"
    [memory] = await service.list_active_memories(user_id=seed[0], workspace_id=seed[1], project_id=seed[2])
    sources = await service.get_sources(user_id=seed[0], workspace_id=seed[1], memory_id=memory["id"])
    assert await service.forget_memory(user_id=seed[0], workspace_id=seed[1], memory_id=memory["id"],
        expected_revision=memory["revision"], request_id="forget", mode=mode,
        source_ids=[item["id"] for item in sources] if mode == "sources" else None)
    # The forget happened a while before anything said next.
    async with get_db_session() as db:
        await db.execute(update(MemoryTombstone).where(MemoryTombstone.user_id == seed[0]).values(
            deleted_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
    assert await active(seed) == []
    return seed


@pytest.mark.asyncio
@pytest.mark.parametrize("said", ["其实我对菠萝过敏，还是记住吧。", "我吃菠萝会过敏，我对菠萝过敏，记一下。"])
async def test_saying_a_forgotten_fact_again_later_remembers_it(monkeypatch, said):
    seed = await learn_then_forget(monkeypatch)
    await _finish_turn(seed, text=said)
    assert await MemoryExtractionWorker(extractor=says("我对菠萝过敏"), verifier=Verifier()).run_once() == "SUCCEEDED"
    assert await active(seed) == [SUMMARY]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["memory", "sources"])
async def test_what_was_said_before_forgetting_never_brings_it_back(monkeypatch, mode):
    seed = await learn_then_forget(monkeypatch, mode)
    # A late retry of the original turn, as if its first attempt had failed.
    job = await _job(seed)
    async with get_db_session() as db:
        await db.execute(update(MemoryExtractionJob).where(MemoryExtractionJob.id == job.id).values(state="RETRY"))
    assert await jobs.replay_job(job.id, user_id=seed[0], workspace_id=seed[1])
    await MemoryExtractionWorker(extractor=says("我对菠萝过敏"), verifier=Verifier()).run_once()
    assert await active(seed) == []
