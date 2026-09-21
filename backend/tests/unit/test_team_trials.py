from dataclasses import replace

import pytest

from agent_catalog import repository, trials
from agent_catalog.compiler import compile_agent
from db.base import get_db_session
from db.models.session import Session
from team.errors import TeamError
from tests.unit.test_team_catalog import config, new_root
from tests.unit.test_team_compiler import spec


@pytest.mark.parametrize("payload", [
    {"agent": "build"}, {"model": "other"}, {"variant": "high"},
    {"video_model": "other"}, {"video_resolution": "1080p"},
    {"team_request": {}}, {"format": {"type": "json_schema"}},
])
def test_trial_rejects_configuration_escape(payload):
    from types import SimpleNamespace
    from team.guards import check_trial_configuration
    session = SimpleNamespace(kind="agent_trial", agent="definition:fixed", model="frozen", variant=None)
    with pytest.raises(TeamError, match=".") as error:
        check_trial_configuration(session, payload)
    assert error.value.code == "TRIAL_CONFIGURATION_FROZEN"
    check_trial_configuration(session, {"text": "Test", "agent": session.agent, "model": session.model})
    check_trial_configuration(SimpleNamespace(kind="normal"), payload)


async def test_trial_uses_immutable_version_and_owner_scope(config, monkeypatch):
    monkeypatch.setattr(trials, "get_config", lambda: config)
    root_id, actor = await new_root()
    async with get_db_session() as db:
        project_id = (await db.get(Session, root_id)).project_id
    original = spec()
    compiled = compile_agent(original, config=config, role="trial")
    definition = await repository.create("agent", actor, "create", original,
        capability_summary={**compiled.summary, "_compiled_trial": compiled.snapshot()})
    result = await trials.create(definition["id"], actor, "start", project_id=project_id)
    assert await trials.create(definition["id"], actor, "start", project_id=project_id) == result
    authority, frozen, _ = await trials.authority_for_version(result["version_id"], actor)
    assert frozen["instruction"] == original.instruction
    assert authority.composition.agent_preset.name == trials.PREFIX + result["version_id"]
    assert not any(name.startswith("team_") for name in authority.tool_ids)
    changed = original.model_copy(update={"instruction": "Changed later"})
    await repository.mutate("agent", definition["id"], actor, "edit", "save_draft", 1, spec=changed)
    _, after, _ = await trials.authority_for_version(result["version_id"], actor)
    assert after == frozen
    with pytest.raises(TeamError) as error:
        await trials.authority_for_version(result["version_id"], replace(actor, workspace_id="other"))
    assert error.value.status == 404


async def test_generated_draft_receipt_does_not_create_definition(config, monkeypatch):
    from agent_catalog import generation
    from agent.structured_output import TOOL_NAME
    config.team_max_proposals_per_session = 10
    monkeypatch.setattr(generation, "get_config", lambda: config)
    _, actor = await new_root()
    calls = []
    async def fake_stream(**kwargs):
        calls.append(kwargs)
        yield {"type": "tool_call", "tool": TOOL_NAME, "args": spec().model_dump(mode="json")}
        yield {"type": "finish", "reason": "stop"}
    monkeypatch.setattr(generation, "stream_llm", fake_stream)
    first = await generation.generate(actor, "generate", "A reviewer")
    assert first == await generation.generate(actor, "generate", "A reviewer")
    assert len(calls) == 1
    assert calls[0]["billing_kind"] == "agent_definition"
    assert (await repository.list_definitions("agent", actor))["items"] == []
    with pytest.raises(TeamError) as error:
        await generation.generate(actor, "generate", "Different request")
    assert error.value.code == "IDEMPOTENCY_CONFLICT"
