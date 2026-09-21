from unittest.mock import AsyncMock

import pytest

from agent_catalog import repository
from agent_catalog.catalog import prepare_lineup
from agent_catalog.schemas import MemberSpec, TeamSpec
from db.models.session import Session
from team import commands, templates
from team.errors import TeamError
from team.journal import command, snapshot, write_transaction
from team.service import start_confirmed_locked
from tests.unit.test_team_catalog import config, new_root  # noqa: F401
from tests.unit.test_team_compiler import spec


async def completed_team():
    from dataclasses import replace
    root_id, actor = await new_root()
    team = TeamSpec(name="Automatic", acceptance_mode="auto", resource_refs=["asset:instructions"],
        goal_input_schema={"type": "object", "properties": {"goal": {"type": "string"}}},
        result_schema={"type": "object", "properties": {"summary": {"type": "string"}}},
        preset_members=[MemberSpec(alias="a", inline=spec().model_copy(update={"name": "Research"}), responsibility="Compare sources"),
        MemberSpec(alias="b", inline=spec().model_copy(update={"name": "Review"}), responsibility="Check the result")])
    lineup = await prepare_lineup(team, actor)
    async with write_transaction() as db:
        root = await db.get(Session, root_id, with_for_update=True)
        started = await start_confirmed_locked(db, root=root, question_id="confirmed", title="Reusable research", goal="Compare the options",
            policy=team.policy, grant=lineup.grant, coordinator=lineup.coordinator, members=lineup.members,
            configuration=team.model_dump(mode="json"))
    async def close(writer):
        commands.run_status(writer, "canceling")
        commands.run_status(writer, "canceled")
        return {}
    await command(started["id"], replace(actor, kind="server"), "cancel", {}, close)
    members = [member["id"] for member in started["members"] if member["role"] == "member"]
    return started["id"], root_id, actor, members, lineup


async def test_reuse_is_atomic_pinned_idempotent_and_survives_source_deletion(config, monkeypatch):
    from sandbox import sandbox_manager
    from session.session import delete_session
    monkeypatch.setattr(sandbox_manager, "release", AsyncMock())
    run_id, root, actor, members, original = await completed_team()
    saved = await templates.save_template(run_id, actor, "save", "Fixed research", members, {})
    assert saved == await templates.save_template(run_id, actor, "save", "Fixed research", members, {})
    template = TeamSpec.model_validate(saved["version"]["spec"])
    assert template.policy.member_selection == "explicit_only" and template.policy.member_creation == "disabled"
    assert all(member.version_policy == "pinned" for member in template.preset_members)
    assert template.acceptance_mode == "auto"
    assert template.resource_refs == original.spec.resource_refs
    assert template.goal_input_schema == original.spec.goal_input_schema
    assert template.result_schema == original.spec.result_schema
    await delete_session(root, actor.owner_user_id, actor.workspace_id)
    restored = await prepare_lineup(template, actor)
    assert [item[1].spec.instruction for item in restored.members] == [item[1].spec.instruction for item in original.members]
    assert [item[1].summary["model"] for item in restored.members] == [item[1].summary["model"] for item in original.members]
    assert (await repository.get("team", saved["id"], actor, active_only=True))["id"] == saved["id"]


async def test_duplicate_member_names_roll_back_all_creates_and_single_save_is_draft(config):
    run_id, _, actor, members, _ = await completed_team()
    with pytest.raises(TeamError) as error:
        await templates.save_template(run_id, actor, "bad", "Duplicate names", members, dict.fromkeys(members, "Same name"))
    assert error.value.code == "DEFINITION_NAME_TAKEN"
    assert (await repository.list_definitions("agent", actor))["items"] == []
    assert (await repository.list_definitions("team", actor))["items"] == []
    saved = await templates.save_member(run_id, members[0], actor, "member", "Reusable researcher")
    assert saved["status"] == "draft"
    assert "Compare sources" in saved["version"]["spec"]["instruction"]
    assert saved["version"]["spec"]["default_model"] == "openai/test"
    assert (await snapshot(run_id, actor))["run"]["state"] == "canceled"
