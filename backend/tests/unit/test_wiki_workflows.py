"""Versioned workflow decisions against real SQL, pages and source authority."""
from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select

from db.base import get_db_session
from db.models.wiki_workflow import WikiArtifact, WikiWorkflowEvent
from memory import service as memory_service
from memory.wiki import organization, profiles, records, workflow_adaptation, workflows
from tests.unit.test_memory_wiki import approve, compile_one, seed, wiki_database  # noqa: F401
from tests.unit.test_wiki_organization import drain
from wiki_compiler.profile_templates import KNOWLEDGE_REVIEW
from wiki_compiler.profiles import ProfileError, validate_profile


async def setup(monkeypatch, *, with_task=False):
    data = await seed(monkeypatch)
    candidate, _ = await compile_one(data)
    page = await approve(data, candidate)
    definition = deepcopy(KNOWLEDGE_REVIEW)
    if not with_task:
        definition["workflows"]["organize-review"]["stages"].pop(0)
    profile = await profiles.save(user_id=data[0], workspace_id=data[1], project_id=data[2],
        definition=definition, expected_revision=0)
    run = await workflows.start(user_id=data[0], workspace_id=data[1], profile_id=profile["id"],
        expected_profile_revision=profile["revision"], workflow_id="organize-review", inputs={"goal": "Review team knowledge"}, request_id=uuid4().hex)
    return data, page, profile, run


async def act(data, run, action, payload=None, request_id=None):
    return await workflows.act(user_id=data[0], workspace_id=data[1], run_id=run["id"], expected_revision=run["revision"],
        request_id=request_id or uuid4().hex, action=action, payload=payload or {})


def page_output(page):
    return {"kind": "page", "entity_type": "topic", "slug": page["slug"], "title": page["title"],
            "fields": {"summary": "Team response language"}, "page_id": page["id"], "page_revision": page["revision"], "expected_revision": 0}


async def first_stage(data, page, run):
    run = await act(data, run, "submit", {"outputs": [page_output(page)]})
    run = await act(data, run, "approve", {"gate": "human:review", "output_hash": run["stages"]["document"]["output_hash"]})
    return await act(data, run, "advance")


@pytest.mark.asyncio
async def test_multi_stage_gate_replacement_recovery_and_idempotence(monkeypatch):
    data, page, profile, run = await setup(monkeypatch)
    run = await act(data, run, "submit", {"outputs": [page_output(page)]})
    with pytest.raises(ProfileError, match="gate_required"):
        await act(data, run, "advance")
    run = await act(data, run, "approve", {"gate": "human:review", "output_hash": run["stages"]["document"]["output_hash"]})
    changed = page_output(page)
    changed["fields"]["summary"] = "A corrected classification"
    run = await act(data, run, "submit", {"outputs": [changed]})
    assert run["stages"]["document"]["approvals"] == {}
    with pytest.raises(ProfileError, match="gate_required"):
        await act(data, run, "advance")
    run = await act(data, run, "approve", {"gate": "human:review", "output_hash": run["stages"]["document"]["output_hash"]})
    original, request_id = run, uuid4().hex
    run = await act(data, run, "advance", request_id=request_id)
    replay = await act(data, original, "advance", request_id=request_id)
    assert replay["revision"] == run["revision"] and run["current_stage"] == "finalize"
    record = (await profiles.records(user_id=data[0], workspace_id=data[1], profile_id=profile["id"]))["records"][0]
    run = await act(data, run, "fail", {"reason": "Waiting for a final decision"})
    run = await act(data, run, "resume")
    run = await act(data, run, "submit", {"outputs": [{"kind": "lifecycle", "record_id": record["id"],
        "expected_revision": record["revision"], "to": "reviewed"}]})
    run = await act(data, run, "approve", {"gate": "human:finalize", "output_hash": run["stages"]["finalize"]["output_hash"]})
    run = await act(data, run, "advance")
    assert run["status"] == "completed"
    stats = await profiles.statistics(user_id=data[0], workspace_id=data[1], profile_id=profile["id"])
    assert stats["entities"]["topic"]["states"] == {"draft": 0, "reviewed": 1, "archived": 0}
    detail = await workflows.detail(user_id=data[0], workspace_id=data[1], run_id=run["id"])
    assert len(detail["events"]) == run["revision"]
    with pytest.raises(ProfileError, match="terminal"):
        await act(data, run, "resume")


@pytest.mark.asyncio
async def test_profile_drift_blocks_until_exact_preview_adaptation(monkeypatch):
    data, page, profile, run = await setup(monkeypatch)
    definition = deepcopy(profile["definition"])
    definition["title"] = "Updated handbook"
    await profiles.save(user_id=data[0], workspace_id=data[1], project_id=data[2], profile_id=profile["id"],
        expected_revision=profile["revision"], definition=definition)
    with pytest.raises(ProfileError, match="definition_changed"):
        await act(data, run, "submit", {"outputs": [page_output(page)]})
    preview = await workflow_adaptation.preview(user_id=data[0], workspace_id=data[1], run_id=run["id"])
    assert preview["allowed"] and preview["approvals_will_reset"]
    with pytest.raises(ProfileError, match="adaptation_changed"):
        await act(data, run, "adapt", {"preview_hash": "stale"})
    adapted = await act(data, run, "adapt", {"preview_hash": preview["preview_hash"]})
    assert not adapted["definition_changed"]
    await first_stage(data, page, adapted)


@pytest.mark.asyncio
async def test_source_correction_after_approval_blocks_advance_and_hides_output(monkeypatch):
    data, page, profile, run = await setup(monkeypatch)
    run = await act(data, run, "submit", {"outputs": [page_output(page)]})
    run = await act(data, run, "approve", {"gate": "human:review", "output_hash": run["stages"]["document"]["output_hash"]})
    await memory_service.edit_note(user_id=data[0], workspace_id=data[1], memory_id=data[3]["id"],
        expected_revision=data[3]["revision"], summary="A corrected source")
    with pytest.raises(ValueError, match="unavailable"):
        await act(data, run, "advance")
    current = await workflows.detail(user_id=data[0], workspace_id=data[1], run_id=run["id"])
    assert not current["stages"]["document"]["outputs_available"] and current["stages"]["document"]["outputs"] == []
    assert (await profiles.records(user_id=data[0], workspace_id=data[1], profile_id=profile["id"]))["records"] == []


@pytest.mark.asyncio
async def test_stage_write_boundaries_and_atomic_duplicate_targets(monkeypatch):
    data, page, profile, run = await setup(monkeypatch)
    bad = page_output(page)
    bad["entity_type"] = "undeclared"
    with pytest.raises(ProfileError, match="write_denied"):
        await act(data, run, "submit", {"outputs": [bad]})
    bad = page_output(page)
    bad["fields"] = {"state": "reviewed", "summary": "Cannot skip lifecycle"}
    with pytest.raises(ProfileError, match="transition_required"):
        await act(data, run, "submit", {"outputs": [bad]})
    with pytest.raises(ValueError, match="record_changed"):
        await act(data, run, "submit", {"outputs": [page_output(page), page_output(page)]})
    assert (await profiles.records(user_id=data[0], workspace_id=data[1], profile_id=profile["id"]))["records"] == []


@pytest.mark.asyncio
async def test_durable_organization_link_requires_completion_and_cost_confirmation(monkeypatch):
    data, page, profile, run = await setup(monkeypatch, with_task=True)
    preview = await organization.preview(user_id=data[0], workspace_id=data[1], project_id=data[2])
    payload = {"input_hash": preview["input_hash"], "max_model_calls": 10, "confirm_cost": False}
    with pytest.raises(ProfileError, match="cost_confirmation_required"):
        await act(data, run, "prepare", payload)
    payload["confirm_cost"] = True
    run = await act(data, run, "prepare", payload)
    task = run["stages"]["organize"]["task"]
    with pytest.raises(ProfileError, match="task_incomplete"):
        await act(data, run, "advance")
    await drain(data, task["id"])
    restarted = await workflows.detail(user_id=data[0], workspace_id=data[1], run_id=run["id"])
    assert restarted["stages"]["organize"]["task"]["status"] == "completed"
    advanced = await act(data, restarted, "advance")
    assert advanced["current_stage"] == "document"


@pytest.mark.asyncio
async def test_foreign_actor_cannot_use_run_or_publish_typed_records(monkeypatch):
    data, page, profile, run = await setup(monkeypatch)
    other = await seed(monkeypatch)
    assert await workflows.detail(user_id=other[0], workspace_id=other[1], run_id=run["id"]) is None
    assert await profiles.records(user_id=other[0], workspace_id=other[1], profile_id=profile["id"]) is None
    assert await act(other, run, "cancel") is None


@pytest.mark.asyncio
async def test_relation_artifact_and_lifecycle_constraints_use_current_records(monkeypatch):
    data, page, profile, run = await setup(monkeypatch)
    run = await first_stage(data, page, run)
    one = (await profiles.records(user_id=data[0], workspace_id=data[1], profile_id=profile["id"]))["records"][0]
    args = dict(user_id=data[0], workspace_id=data[1], profile_id=profile["id"], profile_revision=profile["revision"])
    output = page_output(page)
    output["slug"] = "second-topic"
    two = await records.mutate(**args, kind=output.pop("kind"), payload=output)
    relation = await records.mutate(**args, kind="relation", payload={"type": "supports", "from_id": one["id"],
        "from_revision": one["revision"], "to_id": two["id"], "to_revision": two["revision"], "expected_revision": 0})
    assert relation["revision"] == 1
    artifact = {"kind": "artifact", "type": "review-note", "name": "Review findings", "media_type": "text/markdown",
                "body": "# Review\n\nChecked the original source.", "records": [{"id": one["id"], "revision": one["revision"] + 1}]}
    lifecycle = {"kind": "lifecycle", "record_id": one["id"], "expected_revision": one["revision"], "to": "reviewed"}
    run = await act(data, run, "submit", {"outputs": [lifecycle, artifact]})
    # Preflight validates the new revision but leaves the published state alone.
    current = (await profiles.records(user_id=data[0], workspace_id=data[1], profile_id=profile["id"]))["records"]
    assert next(row for row in current if row["id"] == one["id"])["fields"]["state"] == "draft"
    run = await act(data, run, "approve", {"gate": "human:finalize", "output_hash": run["stages"]["finalize"]["output_hash"]})
    run = await act(data, run, "advance")
    async with get_db_session() as db:
        stored = await db.scalar(select(WikiArtifact).where(WikiArtifact.run_id == run["id"]))
        assert stored.body == artifact["body"] and stored.record_manifest[0]["id"] == one["id"]
        assert stored.record_manifest[0]["revision"] == one["revision"] + 1


@pytest.mark.parametrize("mutate", [
    lambda p: p.update({"shell": "echo not executable"}),
    lambda p: p["workflows"]["organize-review"]["stages"][0].update({"action": "shell"}),
    lambda p: p["workflows"]["organize-review"]["stages"][0].update({"gate": "agent:foreign-approval", "gates": ["agent:foreign-approval"]}),
    lambda p: p["relations"]["supports"].update({"to": ["unknown"]}),
    lambda p: p["entities"]["topic"]["lifecycle"].update({"initial": "unknown"}),
])
def test_unsupported_profile_behavior_is_rejected(mutate):
    profile = deepcopy(KNOWLEDGE_REVIEW)
    mutate(profile)
    with pytest.raises(ProfileError):
        validate_profile(profile)
