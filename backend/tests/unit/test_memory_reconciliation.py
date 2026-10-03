"""Natural corrections revise existing authority, preserve other facts and rebuild Wiki."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from core import config as runtime_config
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_v2 import MemorySource, MemoryRevision
from memory import service
from memory.extraction import MemoryExtractionWorker
from memory.policy import resolve_access_scope
from memory.providers.common import MemoryProviderError
from memory.reconciliation import validate_revisions, validate_separate
from memory.retrieval import authorized_documents
from memory.wiki import reader, service as wiki
from memory.wiki.consumer_refresh import refresh_existing
from memory.wiki.worker import MemoryWikiWorker
from tests.unit.test_automatic_knowledge import Verifier
from tests.unit.test_memory_pipeline import _finish_turn, _job, _seed, pipeline_database  # noqa: F401
from tests.unit.test_memory_wiki import FakeModel
from wiki_compiler.hashing import canonical_hash

BEFORE = "周五晚上和家人做饭，周六下午有陶艺课，周日通常看父母。"
AFTER = "周五晚上常规加班，无法家庭聚餐，周六下午有陶艺课，周日通常看父母。"
CHANGE = "以后每周五晚上要加班，没法和家人聚餐了。"
GUITAR = "我每周三晚上要上吉他课。"


class Planner:
    def __init__(self, memory_id, old="周五晚上和家人做饭", new="周五晚上常规加班，无法家庭聚餐", before=None):
        self.id, self.old, self.new, self.before = memory_id, old, new, before

    async def plan(self, **kwargs):
        if self.before:
            await self.before()
        return {"revisions": [{"memory_id": self.id,
            "edits": [{"old": self.old, "new": self.new, "proposal_indexes": [0]}]}]}, {"model_calls": 1}


def proposal(frozen):
    return {"candidates": [{"type": "CONSTRAINT", "summary": "用户以后周五晚上常规加班，无法家庭聚餐。",
        "fact_key": "personal.weekly_schedule", "confidence": 95, "source_indexes": [0],
        "quotes": [{"source_index": 0, "quote": CHANGE}]}]}


async def seed_correction(monkeypatch, text=CHANGE):
    data = await _seed(monkeypatch)
    config = runtime_config.get_config().memory
    config.automatic_knowledge, config.wiki = True, True
    note = await service.create_note(user_id=data[0], workspace_id=data[1], project_id=data[2],
        summary=BEFORE, fact_key="personal.weekly_schedule")
    # The note predates the correcting message, as it would in real use.
    # SQLite's statement clock has one-second resolution, so a note written in
    # the same second as the message would otherwise look newer than it.
    async with get_db_session() as db:
        earlier = datetime.now(timezone.utc) - timedelta(minutes=5)
        row = await db.get(UserMemory, note["id"])
        row.valid_from = row.created_at = row.recorded_at = earlier
    await _finish_turn(data, text=text)
    return data, note, config


@pytest.mark.asyncio
async def test_chat_correction_preserves_other_clauses_and_refreshes_existing_wiki(monkeypatch):
    data, note, config = await seed_correction(monkeypatch)
    queued = await wiki.schedule_compile(user_id=data[0], workspace_id=data[1], project_id=data[2],
        slug="schedule", title="安排", memory_ids=[note["id"]], config=config)
    compiler = MemoryWikiWorker(config, model=FakeModel(), verifier=Verifier())
    await compiler.run_once()
    from db.models.memory_wiki import MemoryWikiJob
    from db.models.wiki_platform import WikiMaintenancePolicy
    async with get_db_session() as db:
        page_job = await db.get(MemoryWikiJob, queued["id"])
        from db.models.memory_wiki import MemoryWikiCandidate
        page_id = (await db.get(MemoryWikiCandidate, page_job.candidate_id)).target_page_id
    assert await MemoryExtractionWorker(extractor=proposal, verifier=Verifier(),
        reconciler=Planner(note["id"])).run_once() == "SUCCEEDED"
    args = {"user_id": data[0], "workspace_id": data[1], "page_id": page_id}
    assert not (await reader.page_detail(**args))["body_available"]
    async with get_db_session() as db:
        row = await db.get(UserMemory, note["id"])
        assert row.value["summary"] == AFTER and row.revision == note["revision"] + 1
        assert row.owner == "SYSTEM_VERIFIED" and row.confirmation_actor_id is None
        history = (await db.scalars(select(MemoryRevision).where(MemoryRevision.memory_id == row.id)
            .order_by(MemoryRevision.revision))).all()
        assert history[0].value["summary"] == BEFORE and history[-1].reason == "automatic_corrected"
        scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
        documents = await authorized_documents(db, scope, config)
        assert any(AFTER == item.text for item in documents)
        assert all("周五晚上和家人做饭" not in item.text for item in documents)
        policy = await db.scalar(select(WikiMaintenancePolicy).where(WikiMaintenancePolicy.user_id == data[0]))
    await refresh_existing(policy.id, config)
    await compiler.run_once()
    page = await reader.page_detail(**args)
    assert page["body_available"] and AFTER in page["body"]
    assert page["source_details"][0]["kind"] == "verified_memory_revision"
    assert page["source_details"][0]["changes"][0]["body"] == CHANGE
    assert page["source_details"][0]["changes"][0]["session_id"] == data[3]
    # Revoking the original correction also revokes its compiled projection.
    async with get_db_session() as db:
        raw = await db.scalar(select(MemorySource).where(MemorySource.user_id == data[0], MemorySource.body == CHANGE))
        raw.status = "UNAVAILABLE"
    assert not (await reader.page_detail(**args))["body_available"]


@pytest.mark.asyncio
async def test_memory_sources_show_the_correcting_words_and_mark_replaced_evidence(monkeypatch):
    data, note, _ = await seed_correction(monkeypatch)
    assert await MemoryExtractionWorker(extractor=proposal, verifier=Verifier(),
        reconciler=Planner(note["id"])).run_once() == "SUCCEEDED"
    sources = await service.get_sources(user_id=data[0], workspace_id=data[1], memory_id=note["id"])
    current = [item for item in sources if not item["superseded"]]
    assert [item["source_kind"] for item in current] == ["verified_memory_revision"]
    assert current[0]["body"] == AFTER
    assert current[0]["changes"] == [{"body": CHANGE, "session_id": data[3]}]
    # The replaced wording stays listed, without its text.
    replaced = [item for item in sources if item["superseded"]]
    assert replaced and all(item["body"] is None and "changes" not in item for item in replaced)
    # Deleting the chat that held the correction would withdraw this memory.
    assert await service.count_learned_from_session(user_id=data[0], workspace_id=data[1], session_id=data[3]) == 1
    assert await service.count_learned_from_session(user_id=data[0], workspace_id=data[1], session_id="other") == 0


def guitar(frozen):
    # Same broad topic key as the weekly-schedule note, but a different fact.
    return {"candidates": [{"type": "USER_PROFILE", "summary": "用户每周三晚上要上吉他课。",
        "fact_key": "personal.weekly_schedule", "confidence": 95, "source_indexes": [0],
        "quotes": [{"source_index": 0, "quote": GUITAR}]}]}


class Separate:
    async def plan(self, **kwargs):
        return {"revisions": [], "separate": [0]}, {"model_calls": 1}


@pytest.mark.asyncio
async def test_new_fact_sharing_a_topic_key_is_kept_beside_the_existing_memory(monkeypatch):
    data, note, _ = await seed_correction(monkeypatch, text=GUITAR)
    assert await MemoryExtractionWorker(extractor=guitar, verifier=Verifier(),
        reconciler=Separate()).run_once() == "SUCCEEDED"
    async with get_db_session() as db:
        rows = (await db.scalars(select(UserMemory).where(UserMemory.user_id == data[0],
            UserMemory.status == "ACTIVE"))).all()
    by_summary = {row.value["summary"]: row for row in rows}
    assert set(by_summary) == {BEFORE, "用户每周三晚上要上吉他课。"}
    assert by_summary[BEFORE].revision == note["revision"]
    assert by_summary["用户每周三晚上要上吉他课。"].fact_key.startswith("personal.weekly_schedule:")


def test_separate_facts_must_be_supported_proposals_that_revise_nothing():
    proposals = [{"summary": "甲"}, {"summary": "乙"}]
    supported = [canonical_hash(proposals[0])]
    revising = [{"proposal_indexes": [0]}]
    for indexes in ([1], [2], ["0"], [True], "0"):
        with pytest.raises(MemoryProviderError):
            validate_separate({"separate": indexes}, [], proposals, supported)
    with pytest.raises(MemoryProviderError):
        validate_separate({"separate": [0]}, revising, proposals, supported)
    assert validate_separate({"revisions": []}, [], proposals, supported) == []
    assert validate_separate({"separate": [0, 0]}, [], proposals, supported) == [0]


@pytest.mark.asyncio
async def test_matching_fact_key_cannot_silently_report_unchanged_old_memory_as_success(monkeypatch):
    data, note, _ = await seed_correction(monkeypatch)
    class NoRevision:
        async def plan(self, **kwargs):
            return {"revisions": []}, {}
    assert await MemoryExtractionWorker(extractor=proposal, verifier=Verifier(),
        reconciler=NoRevision()).run_once() == "RETRY"
    assert (await _job(data)).last_error == "memory_revision_required"
    async with get_db_session() as db:
        assert (await db.get(UserMemory, note["id"])).value["summary"] == BEFORE


@pytest.mark.asyncio
async def test_failed_revision_verification_keeps_original_intact(monkeypatch):
    data, note, _ = await seed_correction(monkeypatch)
    class RejectRevision(Verifier):
        async def verify(self, items, **kwargs):
            return [kwargs["purpose"] != "memory_revision"] * len(items), {}
    assert await MemoryExtractionWorker(extractor=proposal, verifier=RejectRevision(),
        reconciler=Planner(note["id"], old=BEFORE, new="只保留周五加班。")).run_once() == "RETRY"
    async with get_db_session() as db:
        row = await db.get(UserMemory, note["id"])
        assert row.value["summary"] == BEFORE and row.revision == note["revision"]
    assert (await _job(data)).last_error == "memory_revision_not_verified"


@pytest.mark.asyncio
async def test_user_edit_during_model_reconciliation_wins(monkeypatch):
    data, note, _ = await seed_correction(monkeypatch)
    async def change():
        await service.edit_note(user_id=data[0], workspace_id=data[1], memory_id=note["id"],
            expected_revision=note["revision"], summary="最新决定：周五不加班。")
    assert await MemoryExtractionWorker(extractor=proposal, verifier=Verifier(),
        reconciler=Planner(note["id"], before=change)).run_once() == "RETRY"
    assert (await _job(data)).last_error == "memory_base_revision_changed"
    async with get_db_session() as db:
        assert (await db.get(UserMemory, note["id"])).value["summary"] == "最新决定：周五不加班。"


@pytest.mark.asyncio
async def test_older_message_cannot_overwrite_a_later_explicit_edit(monkeypatch):
    data, note, _ = await seed_correction(monkeypatch)
    await service.edit_note(user_id=data[0], workspace_id=data[1], memory_id=note["id"],
        expected_revision=note["revision"], summary="最新决定：周五不加班。")
    class Unexpected:
        async def plan(self, **kwargs):
            raise AssertionError("Older evidence cannot propose edits to newer authority")
    assert await MemoryExtractionWorker(extractor=proposal, verifier=Verifier(), reconciler=Unexpected()).run_once() == "RETRY"
    assert (await _job(data)).last_error == "memory_revision_required"


@pytest.mark.asyncio
async def test_reconciliation_matches_semantics_without_requiring_same_fact_key(monkeypatch):
    data, note, _ = await seed_correction(monkeypatch)
    def different_key(frozen):
        result = proposal(frozen)
        result["candidates"][0]["fact_key"] = "personal.work.friday"
        return result
    assert await MemoryExtractionWorker(extractor=different_key, verifier=Verifier(),
        reconciler=Planner(note["id"])).run_once() == "SUCCEEDED"
    async with get_db_session() as db:
        rows = (await db.scalars(select(UserMemory).where(UserMemory.user_id == data[0]))).all()
        assert len(rows) == 1 and rows[0].value["summary"] == AFTER


@pytest.mark.parametrize("change", ["foreign", "overlap", "unsupported", "ambiguous"])
def test_reconciliation_rejects_invalid_targets_edits_and_unverified_inputs(change):
    existing = {"m1": {"summary": "甲项目中文；乙项目英文。", "revision": 3}}
    proposals = [{"summary": "甲项目改为英文。"}]
    edit = {"old": "甲项目中文", "new": "甲项目英文", "proposal_indexes": [0]}
    row = {"memory_id": "m1", "edits": [edit]}
    if change == "foreign":
        row["memory_id"] = "other"
    if change == "overlap":
        row["edits"].append({"old": "中文", "new": "日文", "proposal_indexes": [0]})
    if change == "unsupported":
        edit["proposal_indexes"] = [1]
    if change == "ambiguous":
        edit["old"] = "不存在的文字"
    with pytest.raises(MemoryProviderError):
        validate_revisions({"revisions": [row]}, existing, proposals, [canonical_hash(proposals[0])])
