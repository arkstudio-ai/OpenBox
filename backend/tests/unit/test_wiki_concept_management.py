"""Maintenance enrollment and human decisions survive background reconciliation."""
import pytest
from sqlalchemy import select

from db.base import get_db_session
from db.models.wiki_platform import WikiOrganizationRun, WikiRelation
from memory.wiki import concepts, maintenance, organization, service
from tests.unit.test_memory_wiki import seed, wiki_database  # noqa: F401
from tests.unit.test_wiki_organization import ConceptModel, drain, start


class RelatedModel(ConceptModel):
    async def extract(self, request):
        payload, usage = await super().extract(request)
        first = payload["concepts"][0]
        second = {**first, "title": "沟通规范", "aliases": [], "relations": []}
        first["relations"] = [{"type": "part_of", "target": second["title"], "evidence": first["evidence"]}]
        return {"concepts": [first, second]}, usage


@pytest.mark.asyncio
async def test_rejected_relation_stays_rejected_and_edit_merge_survive_reconciliation(monkeypatch):
    data = await seed(monkeypatch)
    run = await start(data, compile_pages=False)
    result, model, _ = await drain(data, run["id"], RelatedModel())
    assert result["status"] == "completed"
    identity = dict(user_id=data[0], workspace_id=data[1], project_id=data[2])
    relation = (await concepts.relations(**identity))["relations"][0]
    await concepts.decide_relation(user_id=data[0], workspace_id=data[1], relation_id=relation["id"],
        expected_revision=relation["revision"], evidence_hash=relation["evidence_hash"], action="reject")
    second = await start(data, compile_pages=False)
    await drain(data, second["id"], model)
    assert (await concepts.relations(**identity))["relations"] == []
    async with get_db_session() as db:
        assert (await db.get(WikiRelation, relation["id"])).status == "REJECTED"
    entries = (await organization.concepts(**identity))["concepts"]
    source, target = entries
    changed = await organization.edit_concept(user_id=data[0], workspace_id=data[1], concept_id=target["id"],
        expected_revision=target["revision"], title="团队写作约定", aliases=target["aliases"],
        category="人工整理", description="人工确认的概念名称。")
    await concepts.merge(user_id=data[0], workspace_id=data[1], source_id=source["id"], target_id=target["id"],
        source_revision=source["revision"], target_revision=changed["revision"])
    third = await start(data, compile_pages=False)
    await drain(data, third["id"], model)
    entries = (await organization.concepts(**identity))["concepts"]
    assert len(entries) == 1 and entries[0]["id"] == target["id"]
    assert entries[0]["title"] == "团队写作约定" and entries[0]["category"] == "人工整理"
    assert model.calls == 1


@pytest.mark.asyncio
async def test_maintenance_is_explicit_idempotent_and_disable_fences_pending_work(monkeypatch):
    data = await seed(monkeypatch)
    args = dict(user_id=data[0], workspace_id=data[1], project_id=data[2])
    assert not (await maintenance.read(**args))["enabled"]
    await maintenance.schedule_due(data[4])
    assert (await organization.runs(**args))["runs"] == []
    policy = await maintenance.configure(**args, expected_revision=0, enabled=True, call_limit=3, compile_pages=False)
    await maintenance._tick(policy["id"], data[4])
    await maintenance._tick(policy["id"], data[4])
    runs = (await organization.runs(**args))["runs"]
    assert len(runs) == 1
    await maintenance.configure(**args, expected_revision=policy["revision"], enabled=False, call_limit=3, compile_pages=False)
    stopped = await organization.runs(user_id=data[0], workspace_id=data[1], run_id=runs[0]["id"])
    assert stopped["status"] == "cancelled"
    with pytest.raises(service.WikiStateError, match="unavailable"):
        await organization.reserve_call(runs[0]["id"])
    await maintenance._tick(policy["id"], data[4])
    assert len((await organization.runs(**args))["runs"]) == 1


@pytest.mark.asyncio
async def test_cancel_paused_organization_cancels_its_page_jobs(monkeypatch):
    data = await seed(monkeypatch)
    run = await start(data, budget=1)
    paused, _, _ = await drain(data, run["id"])
    cancelled = await organization.act_run(user_id=data[0], workspace_id=data[1], run_id=run["id"],
        expected_revision=paused["revision"], action="cancel")
    assert cancelled["status"] == "cancelled"
    page = await service.get_job(user_id=data[0], workspace_id=data[1], job_id=paused["pages"][0]["job_id"])
    assert page["status"] == "cancelled"


@pytest.mark.asyncio
async def test_completed_partial_requires_source_selection_instead_of_broken_resume(monkeypatch):
    data = await seed(monkeypatch)
    run = await start(data, compile_pages=False)
    async with get_db_session() as db:
        row = await db.get(WikiOrganizationRun, run["id"])
        row.status, row.result = "PARTIAL", {"phase": "complete", "pages": [{"status": "needs_selection"}]}
    with pytest.raises(service.WikiStateError, match="selection_required"):
        await organization.act_run(user_id=data[0], workspace_id=data[1], run_id=run["id"],
            expected_revision=run["revision"], action="resume", max_model_calls=30)
