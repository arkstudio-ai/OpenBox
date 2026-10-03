"""Consumer automation preserves source authority, bounded cost and user edits."""
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from core import config as runtime_config
from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_v2 import MemorySource, MemorySourceLink
from db.models.memory_wiki import MemoryWikiCandidate, MemoryWikiJob, MemoryWikiPage
from db.models.wiki_platform import WikiMaintenancePolicy, WikiOrganizationRun
from memory import service as memories
from memory.grounding import validate_verdicts
from memory.policy import resolve_access_scope
from memory.providers.common import MemoryProviderError
from memory.wiki import editing, exchange, maintenance, reader, service
from memory.wiki.consumer_refresh import refresh_existing
from memory.wiki.organization_worker import WikiOrganizationWorker
from memory.wiki.worker import MemoryWikiWorker
from tests.unit.test_memory_wiki import FakeModel, compile_one, seed, wiki_database  # noqa: F401
from tests.unit.test_wiki_organization import ConceptModel


class Verifier:
    def __init__(self, supported=True, before=None):
        self.supported, self.before, self.calls = supported, before, 0

    async def verify(self, items, **kwargs):
        self.calls += 1
        if self.before:
            await self.before()
        return [self.supported] * len(items), {"input_tokens": 9, "output_tokens": 4}


async def automatic_page(data, *, model=None, verifier=None):
    uid, wid, pid, note, config = data
    config.automatic_knowledge = True
    job = await service.schedule_compile(user_id=uid, workspace_id=wid, project_id=pid,
        slug="working-agreement", title="工作约定", memory_ids=[note["id"]], config=config)
    worker = MemoryWikiWorker(config, model=model or FakeModel(), verifier=verifier or Verifier())
    assert await worker.run_once()
    async with get_db_session() as db:
        row = await db.get(MemoryWikiJob, job["id"])
        return row, await db.get(MemoryWikiCandidate, row.candidate_id) if row.candidate_id else None


@pytest.mark.asyncio
async def test_automatic_publication_keeps_evidence_and_is_immediately_retrievable(monkeypatch):
    data = await seed(monkeypatch)
    job, candidate = await automatic_page(data)
    assert job.status == "COMPLETED" and candidate.status == "APPROVED"
    assert candidate.approved_by is None and candidate.reason_code == "automatic_grounded"
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
        documents = await service.authorized_wiki_documents(db, scope, data[4])
    assert len(documents) == 1 and "中文" in documents[0].text
    page = await reader.page_detail(user_id=data[0], workspace_id=data[1], page_id=candidate.target_page_id)
    assert page["body_available"] and page["source_details"][0]["body"] == "这个项目使用中文答复。"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["用户不接受中文。", "用户必须每天写报告。", "团队明天一定发布。"])
async def test_matching_quote_does_not_authorize_unsupported_claims(monkeypatch, bad):
    data = await seed(monkeypatch)
    class Unsupported:
        async def generate(self, request):
            source = request.sources[0]
            return {"paragraphs": [{"text": bad, "citations": [{"source_id": source.id, "quote": source.text}]}]}, {}
    job, candidate = await automatic_page(data, model=Unsupported(), verifier=Verifier(False))
    assert job.status == "COMPLETED" and candidate.status == "APPROVED"
    assert bad not in candidate.draft["body"] and "这个项目使用中文答复。" in candidate.draft["body"]
    assert candidate.usage["automatic_grounding"]["mode"] == "admitted_memory_copy"


@pytest.mark.asyncio
async def test_provider_failure_preserves_full_source_including_negation_and_condition(monkeypatch):
    data = list(await seed(monkeypatch))
    text = "我不是产品负责人。只有周末我才参加志愿活动。会议时间尚未确定。"
    data[3] = await memories.edit_note(user_id=data[0], workspace_id=data[1], memory_id=data[3]["id"],
        expected_revision=data[3]["revision"], summary=text)
    class Broken:
        async def generate(self, request):
            raise MemoryProviderError("wiki_provider_http_503")
    _, candidate = await automatic_page(data, model=Broken())
    assert text in candidate.draft["body"] and candidate.usage["automatic_grounding"]["mode"] == "admitted_memory_copy"


@pytest.mark.asyncio
async def test_correction_during_verification_blocks_late_publication(monkeypatch):
    data = await seed(monkeypatch)
    async def change():
        await memories.edit_note(user_id=data[0], workspace_id=data[1], memory_id=data[3]["id"],
            expected_revision=data[3]["revision"], summary="现在改为英文答复。")
    job, candidate = await automatic_page(data, verifier=Verifier(before=change))
    assert job.status == "CANCELLED" and job.last_error == "wiki_source_changed" and candidate is None


@pytest.mark.asyncio
async def test_default_organization_publishes_with_budget_and_reuses_unchanged_inputs(monkeypatch):
    data = await seed(monkeypatch)
    config = data[4]
    config.automatic_knowledge = True
    config.wiki_min_topic_memories = 1  # Budget accounting for a one-fact topic.
    await maintenance.schedule_due(config)
    async with get_db_session() as db:
        policy = await db.scalar(select(WikiMaintenancePolicy).where(WikiMaintenancePolicy.user_id == data[0]))
        run_id = policy.last_run_id
        assert policy.enabled and policy.automatic and run_id
    organizer = WikiOrganizationWorker(config, model=ConceptModel())
    compiler = MemoryWikiWorker(config, model=FakeModel(), verifier=Verifier())
    for _ in range(20):
        await organizer.run_once()
        await compiler.run_once()
    async with get_db_session() as db:
        run = await db.get(WikiOrganizationRun, run_id)
        policy = await db.get(WikiMaintenancePolicy, policy.id)
        assert run.status == "COMPLETED", run.reason_code
        assert run.model_calls == policy.calls_used == 3  # extract, synthesize, verify
        page = await db.get(MemoryWikiPage, run.result["pages"][0]["page_id"])
        assert page.status == "PUBLISHED"
        policy.next_check_at = service.now() - timedelta(seconds=1)
    await maintenance.schedule_due(config)
    async with get_db_session() as db:
        policy = await db.get(WikiMaintenancePolicy, policy.id)
        assert policy.last_run_id == run_id and policy.calls_used == 3


@pytest.mark.asyncio
async def test_old_draft_is_revalidated_once_without_human_review(monkeypatch):
    data = await seed(monkeypatch)
    old, _ = await compile_one(data)
    data[4].automatic_knowledge = True
    await maintenance.discover_automatic(data[4])
    async with get_db_session() as db:
        policy = await db.scalar(select(WikiMaintenancePolicy).where(WikiMaintenancePolicy.user_id == data[0]))
    await refresh_existing(policy.id, data[4])
    await refresh_existing(policy.id, data[4])
    verifier = Verifier(False)
    worker = MemoryWikiWorker(data[4], model=FakeModel(), verifier=verifier)
    assert await worker.run_once()
    assert not await worker.run_once()
    async with get_db_session() as db:
        assert (await db.get(MemoryWikiCandidate, old.id)).status == "SUPERSEDED"
        page = await db.get(MemoryWikiPage, old.target_page_id)
        assert page.status == "PUBLISHED"
        assert (await db.get(WikiMaintenancePolicy, policy.id)).calls_used == 1  # cached draft + verification
    assert verifier.calls == 1


@pytest.mark.asyncio
async def test_large_topic_splits_automatically_instead_of_requiring_source_selection(monkeypatch):
    data = await seed(monkeypatch)
    for index in range(12):
        await memories.create_note(user_id=data[0], workspace_id=data[1], project_id=data[2],
                                   summary=f"第 {index + 1} 条会议约定需要保留记录。")
    data[4].automatic_knowledge = True
    await maintenance.schedule_due(data[4])
    organizer = WikiOrganizationWorker(data[4], model=ConceptModel())
    compiler = MemoryWikiWorker(data[4], model=FakeModel(), verifier=Verifier())
    for _ in range(45):
        await organizer.run_once()
        await compiler.run_once()
        # Advance the observation poll without making this test depend on wall time.
        async with get_db_session() as db:
            await db.execute(update(WikiOrganizationRun).where(WikiOrganizationRun.user_id == data[0],
                WikiOrganizationRun.status == "PENDING").values(available_at=service.now()))
    async with get_db_session() as db:
        run = await db.scalar(select(WikiOrganizationRun).where(WikiOrganizationRun.user_id == data[0]))
        assert run.status == "COMPLETED", run.reason_code
        assert len(run.result["pages"]) == 2
        assert all(item["status"] == "completed" for item in run.result["pages"])
        pages = (await db.scalars(select(MemoryWikiPage).where(MemoryWikiPage.user_id == data[0]))).all()
        assert len(pages) == 2 and all(page.status == "PUBLISHED" for page in pages)
        assert len({item["id"] for page in pages for item in page.memory_manifest}) == 13


@pytest.mark.asyncio
async def test_edit_updates_authority_atomically_and_rebuilds_original_page(monkeypatch):
    data = await seed(monkeypatch)
    _, candidate = await automatic_page(data)
    args = {"user_id": data[0], "workspace_id": data[1], "page_id": candidate.target_page_id}
    edit = await editing.snapshot(**args)
    entries = [{**item, "text": "以后使用英文答复，代码注释也使用英文。"} for item in edit["entries"]]
    saved = await editing.save(**args, expected_revision=edit["revision"], content_hash=edit["content_hash"],
        title="我的语言偏好", entries=entries, request_id=uuid4().hex)
    assert saved["status"] == "updating"
    assert not (await reader.page_detail(**args))["body_available"]
    async with get_db_session() as db:
        row = await db.get(UserMemory, data[3]["id"])
        assert row.value["summary"] == entries[0]["text"] and row.owner == "USER_CONFIRMED"
        scope = await resolve_access_scope(db, user_id=data[0], workspace_id=data[1], project_id=data[2])
        assert await service.authorized_wiki_documents(db, scope, data[4]) == []
        policy = await db.scalar(select(WikiMaintenancePolicy).where(WikiMaintenancePolicy.user_id == data[0]))
    await refresh_existing(policy.id, data[4])
    assert await MemoryWikiWorker(data[4], model=FakeModel(), verifier=Verifier()).run_once()
    page = await reader.page_detail(**args)
    assert page["body_available"] and page["title"] == "我的语言偏好"
    assert "英文" in page["body"] and "使用中文" not in page["body"]
    with pytest.raises(service.WikiStateError, match="wiki_edit_changed"):
        await editing.save(**args, expected_revision=edit["revision"], content_hash=edit["content_hash"],
            title=edit["title"], entries=entries, request_id=uuid4().hex)


@pytest.mark.asyncio
async def test_edit_cannot_attach_unrelated_memory_and_changes_nothing_on_conflict(monkeypatch):
    data = await seed(monkeypatch)
    _, candidate = await automatic_page(data)
    args = {"user_id": data[0], "workspace_id": data[1], "page_id": candidate.target_page_id}
    edit = await editing.snapshot(**args)
    other = await memories.create_note(user_id=data[0], workspace_id=data[1], project_id=data[2], summary="无关信息。")
    with pytest.raises(service.WikiStateError, match="wiki_edit_changed"):
        await editing.save(**args, expected_revision=edit["revision"], content_hash=edit["content_hash"], title="改名",
            entries=[{"id": other["id"], "revision": other["revision"], "text": "试图替换"}], request_id=uuid4().hex)
    assert (await reader.page_detail(**args))["title"] == edit["title"]


@pytest.mark.asyncio
async def test_import_edit_retains_source_history_and_export_metadata(monkeypatch):
    from tests.unit.test_wiki_exchange import bundle
    data = await seed(monkeypatch)
    actor = {"user_id": data[0], "workspace_id": data[1]}
    uploaded = await exchange.preview(**actor, project_id=data[2], data=bundle())
    document = uploaded["documents"][0]
    published = await exchange.decide(**actor, document_id=document["id"], expected_revision=document["revision"],
        content_hash=document["content_hash"], action="approve", acknowledge_warnings=True)
    args = {**actor, "page_id": published["page_id"]}
    original = await reader.page_detail(**args)
    edit = await editing.snapshot(**args)
    await editing.save(**args, expected_revision=edit["revision"], content_hash=edit["content_hash"],
        title="Updated policy", entries=[{**edit["entries"][0], "text": "We release only after tests pass."}], request_id=uuid4().hex)
    current = await reader.page_detail(**args)
    assert current["body_available"] and current["body"] == "We release only after tests pass."
    assert current["exchange_path"] == original["exchange_path"]
    async with get_db_session() as db:
        assert await db.get(MemorySource, original["source_details"][0]["id"])  # original retained


@pytest.mark.asyncio
@pytest.mark.parametrize("supported", [True, False])
async def test_conversation_admission_is_automatic_only_for_verified_statements(monkeypatch, supported):
    from tests.unit.test_memory_pipeline import _seed, _finish_turn, _proposal
    from memory.extraction import MemoryExtractionWorker
    data = await _seed(monkeypatch)
    runtime_config.get_config().memory.automatic_knowledge = True
    await _finish_turn(data)
    worker = MemoryExtractionWorker(extractor=_proposal, verifier=Verifier(supported))
    assert await worker.run_once() == "SUCCEEDED"
    async with get_db_session() as db:
        row = await db.scalar(select(UserMemory).where(UserMemory.user_id == data[0]))
        assert row.status == ("ACTIVE" if supported else "CANDIDATE")
        assert row.owner == ("SYSTEM_VERIFIED" if supported else "SYSTEM_INFERRED")
        assert row.confirmation_actor_id is None
        sources = (await db.scalars(select(MemorySource).join(MemorySourceLink, MemorySourceLink.source_id == MemorySource.id)
            .where(MemorySourceLink.memory_id == row.id, MemorySourceLink.revision == row.revision))).all()
        assert sources and all(item.source_kind == "user_statement" for item in sources)


@pytest.mark.parametrize("value", [{"verdicts": [{"index": 0, "supported": "true"}]},
    {"verdicts": [{"index": 1, "supported": True}]}, {"verdicts": []},
    {"verdicts": [{"index": 0, "supported": True, "approved": True}]}])
def test_grounding_contract_fails_closed(value):
    with pytest.raises(MemoryProviderError):
        validate_verdicts(value, 1)


@pytest.mark.asyncio
async def test_old_tool_candidate_cannot_preempt_verified_extraction(monkeypatch):
    from tests.unit.test_memory_pipeline import _seed, _finish_turn, _proposal
    from memory.extraction import MemoryExtractionWorker
    data = await _seed(monkeypatch)
    config = runtime_config.get_config().memory
    config.automatic_knowledge = True
    await _finish_turn(data)
    old = await memories.write_memory(user_id=data[0], workspace_id=data[1], project_id=data[2],
        scope="LONG_TERM", type="PREFERENCE", value={"summary": "用户喜欢简短的中文回复"}, owner="SYSTEM_INFERRED",
        evidence={"session_id": data[3]})
    def extract(frozen):
        assert not any(item["status"] == "CANDIDATE" for item in frozen.existing_memories)
        return _proposal(frozen)
    assert await MemoryExtractionWorker(extractor=extract, verifier=Verifier()).run_once() == "SUCCEEDED"
    async with get_db_session() as db:
        row = await db.get(UserMemory, old["id"])
        assert row.status == "ACTIVE" and row.owner == "SYSTEM_VERIFIED" and row.confirmation_actor_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["write_memory", "propose_memory"])
async def test_legacy_tool_uses_background_processing_without_confirmation(monkeypatch, action):
    from tests.unit.test_memory_pipeline import _seed
    from tool.creator_context import CreatorContextArgs, execute_creator_context
    from tool.tool import ToolContext
    data = await _seed(monkeypatch)
    runtime_config.get_config().memory.automatic_knowledge = True
    async def forbidden(*args, **kwargs):
        raise AssertionError("Consumer conversation must not open a memory confirmation card")
    monkeypatch.setattr("tool.creator_context.question_mod.ask", forbidden)
    args = CreatorContextArgs(action=action, scope="LONG_TERM", type="PREFERENCE", owner="USER_CONFIRMED",
        value={"summary": "用户喜欢中文"}, summary="用户喜欢中文")
    result = await execute_creator_context(args, ToolContext(user_id=data[0], workspace_id=data[1],
        project_id=data[2], session_id=data[3], message_id="m1", part_id="p1"))
    assert result.metadata == {"status": "automatic_pending", "confirmation_required": False}
    async with get_db_session() as db:
        assert not await db.scalar(select(UserMemory.id).where(UserMemory.user_id == data[0]))
