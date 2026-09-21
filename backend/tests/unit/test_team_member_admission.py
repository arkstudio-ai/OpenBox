"""An active team's additions use its grant, independent of deployment defaults."""
from dataclasses import replace
import json

import pytest

from agent_catalog import repository
from agent_catalog.schemas import MemberSpec
from db.base import get_db_session
from db.models.session import Session
from team import runtime_binding, scheduler
from team.journal import command, snapshot
from tests.unit.test_subagent_composition import _config
from tests.unit.test_team_catalog import config
from tests.unit.test_team_compiler import spec
from tests.unit.test_team_journal import seed_run
from tool.team_tools import StartMember, start_member
from tool.tool import ToolContext


@pytest.mark.parametrize("source", ["inline_default", "inline_override", "saved"])
async def test_member_start_does_not_compile_an_unrequested_default_coordinator(config, monkeypatch, source):
    from agent.driver import reserve_run
    from skill import snapshot as skill_snapshot

    # The original bug rejected every addition here, even an explicit allowed
    # model, because a synthetic coordinator used the deployment default.
    selected = _config("openai/deployment-default", "openai/test")
    config.model, config.models = selected.model, selected.models
    config.max_concurrent_agents = 5
    config.team_max_running_members = 3
    config.team_reserved_agent_slots = 2
    monkeypatch.setattr(scheduler, "schedule", lambda *_: None)
    async def no_skills(*_, **__):
        return []
    monkeypatch.setattr(skill_snapshot, "freeze_specs", no_skills)

    run_id, actor, root = await seed_run()
    async def grant(writer):
        writer.append("team.grant", "grant", {"id": run_id, "version": 2,
            "member_selection": "coordinator_select", "member_creation": "run_scoped",
            "allowed_models": ["openai/test"], "delegable_tools": ["read", "grep"], "paid_tools": {}})
        return {"granted": True}
    await command(run_id, replace(actor, kind="server"), "grant", {}, grant)
    definition = spec(default_model="openai/test" if source != "inline_override" else None)
    if source == "saved":
        saved = await repository.create("agent", actor, "definition", definition)
        await repository.mutate("agent", saved["id"], actor, "publish", "publish", 1)
        member = MemberSpec(alias="analyst", agent_ref=saved["id"])
    else:
        member = MemberSpec(alias="analyst", inline=definition,
            model_override="openai/test" if source == "inline_override" else None)

    state = await snapshot(run_id, actor)
    coordinator_before = state["members"][root]
    binding = runtime_binding.RuntimeBinding(run_id, root, actor.owner_user_id, actor.workspace_id,
        state["run"]["project_id"], "coordinator", {}, {})
    token = runtime_binding._current.set(binding)
    lease = await reserve_run(root, actor.owner_user_id)
    try:
        ctx = ToolContext(session_id=root, user_id=actor.owner_user_id, workspace_id=actor.workspace_id,
            project_id=binding.project_id, part_id="add-member", run_id=lease.run_id, run_generation=lease.generation)
        added = await start_member(StartMember(member=member), ctx)
        assert not added.metadata.get("error"), added.output
        admitted = json.loads(added.output)["member"]
        assert admitted["model"] == "openai/test"
        after = await snapshot(run_id, actor)
        assert after["members"][root] == coordinator_before
        assert after["grant"] == state["grant"]
        async with get_db_session() as db:
            child = await db.get(Session, admitted["id"])
            assert child.model == "openai/test" and child.parent_id == root
        # Replaying the same tool call preserves one admission and receipt.
        assert (await start_member(StartMember(member=member), ctx)).output == added.output
        assert (await snapshot(run_id, actor))["seq"] == after["seq"]

        for suffix, definition, error in [
            ("outside-model", spec(default_model="openai/deployment-default"), "MODEL_NOT_ALLOWED"),
            ("outside-tools", spec(default_model="openai/test").model_copy(update={"tool_allowlist": ["bash"]}), "PERMISSION_REQUIRES_USER"),
        ]:
            blocked = await start_member(StartMember(member=MemberSpec(alias=suffix, inline=definition)),
                replace(ctx, part_id=suffix))
            assert blocked.metadata["code"] == error
        disabled = await start_member(StartMember(member=MemberSpec(alias="disabled", inline=spec(), enabled=False)),
            replace(ctx, part_id="disabled"))
        assert disabled.metadata["code"] == "INVALID_MEMBER"
        assert (await snapshot(run_id, actor))["seq"] == after["seq"]
    finally:
        await lease.release()
        runtime_binding._current.reset(token)
