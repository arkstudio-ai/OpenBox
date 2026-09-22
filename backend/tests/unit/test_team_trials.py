from dataclasses import replace
import asyncio
from unittest.mock import AsyncMock

import pytest

from agent_catalog import repository, trials
from agent_catalog.compiler import compile_agent
from db.base import get_db_session
from db.models.session import Session
from team.errors import TeamError
from tests.unit.test_team_catalog import config, new_root
from tests.unit.test_team_compiler import spec


@pytest.fixture
def trial_binding(config, monkeypatch):
    from permission import permission
    from team import runtime_binding
    config.permission = {}
    compiled = compile_agent(spec(), config=config, role="trial")
    binding = runtime_binding.RuntimeBinding(None, "trial-core", "user", "workspace", "project", "trial",
        compiled.spec.model_dump(mode="json"), {})
    monkeypatch.setattr(runtime_binding, "get_config", lambda: config)
    monkeypatch.setattr(permission, "_get_user_approved", lambda _user: [])
    token = runtime_binding._current.set(binding)
    try:
        yield compiled
    finally:
        runtime_binding._current.reset(token)


@pytest.mark.parametrize("tool,args", [
    ("bash", {"command": "pwd", "description": "Check workspace"}),
    ("write", {"file_path": "/workspace/check.txt", "content": "ok"}),
    ("edit", {"file_path": "/workspace/check.txt", "old_string": "ok", "new_string": "done"}),
    ("multiedit", {"file_path": "/workspace/check.txt", "edits": [{"old_string": "ok", "new_string": "done"}]}),
    ("apply_patch", {"patch": "*** Begin Patch\n*** Add File: /workspace/check.txt\n+ok\n*** End Patch"}),
])
async def test_trial_core_operations_do_not_stall_on_generated_permission_asks(config, trial_binding, tool, args):
    from agent.hooks import ToolHooks
    from agent.loop import _get_permission_rules
    from permission import permission
    frozen = trial_binding.authority.to_json()
    hooks = ToolHooks(session_id="trial-core", user_id="user", config_rules=_get_permission_rules(config),
        agent_rules=list(trial_binding.authority.composition.agent_preset.permission),
        authority_rule_planes=trial_binding.authority.permission_planes)
    result = await asyncio.wait_for(hooks.authorize_tool(tool, args), timeout=1)
    assert result is None
    assert not permission.list_pending(user_id="user")
    assert trial_binding.authority.to_json() == frozen


@pytest.mark.parametrize("permission_name,pattern,policy", [
    ("bash", "cat /workspace/.env", {}),
    ("bash", "cat /root/.ssh/id_rsa", {}),
    ("bash", "pwd", {"bash": "ask"}),
    ("edit", "/workspace/check.txt", {"edit": "ask"}),
    ("computer", "*", {}),
])
async def test_trial_core_defaults_preserve_confirmation_boundaries(config, trial_binding, permission_name, pattern, policy):
    from team.runtime_binding import preapproved
    config.permission = policy
    assert await preapproved(permission_name, [pattern]) is None


async def test_trial_core_defaults_cannot_override_a_frozen_deny(config, trial_binding):
    from permission import permission
    with pytest.raises(permission.PermissionDeniedError):
        await permission.ask("trial-core", "bash", ["pwd"], user_id="user",
            config_rules=[permission.Rule(permission="bash", pattern="*", action="deny")],
            authority_rulesets=trial_binding.authority.permission_planes)


async def test_trial_defaults_do_not_expand_tools_or_team_member_grants(config, trial_binding, monkeypatch):
    from team import runtime_binding
    binding = runtime_binding.current_binding()
    runtime_binding._current.set(replace(binding, spec={"tool_allowlist": ["read"]}))
    assert await runtime_binding.preapproved("bash", ["pwd"]) is None
    runtime_binding._current.set(replace(binding, role="member", run_id="run"))
    monkeypatch.setattr(runtime_binding, "current_grant", AsyncMock(return_value={"permission_rules": []}))
    assert await runtime_binding.preapproved("bash", ["pwd"]) is False


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
