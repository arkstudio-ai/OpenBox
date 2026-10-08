"""A forget, a paused chat or lost access while one model call is pending stops the next call."""
import pytest

from db.base import get_db_session
from db.models.memory_wiki import MemoryWikiJob
from memory import service
from memory.extraction import MemoryExtractionWorker
from memory.settings import update_settings
from memory.wiki import service as wiki
from memory.wiki.worker import MemoryWikiWorker
from tests.unit.test_automatic_knowledge import Verifier
from tests.unit.test_memory_pipeline import _job, pipeline_database  # noqa: F401
from tests.unit.test_memory_reconciliation import Planner, proposal, seed_correction
from tests.unit.test_memory_wiki import FakeModel, seed, wiki_database  # noqa: F401


class CountingPlanner(Planner):
    calls = 0

    async def plan(self, **kwargs):
        self.calls += 1
        return await super().plan(**kwargs)


async def pause_chat(data):
    await update_settings(data[0], session_id=data[3], session_paused=True)


@pytest.mark.asyncio
async def test_pausing_the_chat_during_extraction_sends_nothing_to_verification(monkeypatch):
    data, note, _ = await seed_correction(monkeypatch)
    verifier = Verifier()

    async def extractor(frozen):
        await pause_chat(data)
        return proposal(frozen)

    worker = MemoryExtractionWorker(extractor=extractor, verifier=verifier, reconciler=CountingPlanner(note["id"]))
    assert await worker.run_once() == "CANCELLED"
    assert verifier.calls == 0
    assert (await _job(data)).last_error == "memory_paused"


@pytest.mark.asyncio
async def test_forgetting_a_memory_during_verification_keeps_it_from_the_reconciler(monkeypatch):
    data, note, _ = await seed_correction(monkeypatch)

    async def forget():
        assert (await service.forget_memory(user_id=data[0], workspace_id=data[1], memory_id=note["id"],
                                            expected_revision=note["revision"]))["ok"]

    planner = CountingPlanner(note["id"])
    worker = MemoryExtractionWorker(extractor=proposal, verifier=Verifier(before=forget), reconciler=planner)
    # The retry re-reads current memories, so the forgotten one is never compared again.
    assert await worker.run_once() == "RETRY"
    assert planner.calls == 0


@pytest.mark.parametrize("stage", ["extraction", "grounding", "planning"])
@pytest.mark.parametrize("invalidation", ["source_deleted", "expired"])
async def test_deleted_old_source_chat_without_memory_revision_change_stops_next_call(monkeypatch, stage, invalidation):
    from db.models.memory import UserMemory
    from db.models.memory_v2 import MemorySource, MemorySourceLink
    from sqlalchemy import select
    from session.session import delete_session
    from tests.unit.test_memory_authority_v2 import source_session
    from tests.unit.test_memory_reconciliation import BEFORE
    data, note, _ = await seed_correction(monkeypatch)
    sid, mid, pid = await source_session({"user_id": data[0], "workspace_id": data[1], "p1": data[2]}, BEFORE)
    async with get_db_session() as db:
        source = await db.scalar(select(MemorySource).join(MemorySourceLink,
            MemorySourceLink.source_id == MemorySource.id).where(MemorySourceLink.memory_id == note["id"]))
        source.session_id, source.message_id, source.part_id = sid, mid, pid

    async def remove_source():
        if invalidation == "source_deleted":
            assert await delete_session(sid, user_id=data[0], workspace_id=data[1])
        else:
            from datetime import datetime, timedelta, timezone
            async with get_db_session() as db:
                (await db.get(UserMemory, note["id"])).ttl = datetime.now(timezone.utc) - timedelta(seconds=1)
        async with get_db_session() as db:
            assert (await db.get(UserMemory, note["id"])).revision == note["revision"]

    async def extract(frozen):
        assert note["id"] in {m["id"] for m in frozen.existing_memories}
        if stage == "extraction":
            await remove_source()
        return proposal(frozen)

    verifier = Verifier(before=remove_source if stage == "grounding" else None)
    planner = CountingPlanner(note["id"], before=remove_source if stage == "planning" else None)
    worker = MemoryExtractionWorker(extractor=extract, verifier=verifier, reconciler=planner)
    assert await worker.run_once() == "RETRY"
    assert (verifier.calls, planner.calls) == {"extraction": (0, 0), "grounding": (1, 0), "planning": (1, 1)}[stage]
    assert (await _job(data)).last_error == "memory_base_revision_changed"


@pytest.mark.asyncio
async def test_pausing_the_chat_while_planning_skips_the_revision_check(monkeypatch):
    data, note, _ = await seed_correction(monkeypatch)
    verifier = Verifier()
    planner = CountingPlanner(note["id"], before=lambda: pause_chat(data))
    worker = MemoryExtractionWorker(extractor=proposal, verifier=verifier, reconciler=planner)
    assert await worker.run_once() == "CANCELLED"
    assert planner.calls == 1 and verifier.calls == 1  # the grounding check only


@pytest.mark.asyncio
async def test_an_unchanged_turn_still_completes_every_call(monkeypatch):
    _, note, _ = await seed_correction(monkeypatch)
    verifier, planner = Verifier(), CountingPlanner(note["id"])
    assert await MemoryExtractionWorker(extractor=proposal, verifier=verifier, reconciler=planner).run_once() == "SUCCEEDED"
    assert planner.calls == 1 and verifier.calls == 2


class ForgettingModel(FakeModel):
    def __init__(self, data):
        super().__init__()
        self.data = data

    async def generate(self, request):
        uid, wid, _pid, note, _config = self.data
        assert (await service.forget_memory(user_id=uid, workspace_id=wid, memory_id=note["id"],
                                            expected_revision=note["revision"]))["ok"]
        return await super().generate(request)


@pytest.mark.asyncio
async def test_forgetting_a_memory_while_a_page_compiles_sends_nothing_to_verification(monkeypatch):
    data = await seed(monkeypatch)
    uid, wid, pid, note, config = data
    config.automatic_knowledge = True
    job = await wiki.schedule_compile(user_id=uid, workspace_id=wid, project_id=pid, slug="agreement",
                                      title="约定", memory_ids=[note["id"]], config=config)
    verifier = Verifier()
    assert await MemoryWikiWorker(config, model=ForgettingModel(data), verifier=verifier).run_once()
    async with get_db_session() as db:
        row = await db.get(MemoryWikiJob, job["id"])
    assert verifier.calls == 0
    assert row.status == "CANCELLED" and row.candidate_id is None
