"""The same permission call changes only after an exact durable confirmation."""
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from agent_catalog.catalog import prepare_lineup
from agent_catalog.schemas import MemberSpec, TeamPolicy, TeamSpec
from db.base import get_db_session
from db.models.session import Session
from permission import permission
from team import amendments, runtime_binding
from team.errors import TeamError
from team.journal import snapshot, write_transaction
from team.service import start_confirmed_locked
from tests.unit.test_team_catalog import config, new_root
from tests.unit.test_team_compiler import spec


async def test_scope_confirmation_allows_only_the_exact_operation_and_revocation_is_live(config, monkeypatch):
    monkeypatch.setattr(runtime_binding, "get_config", lambda: config)
    monkeypatch.setattr(permission, "_get_user_approved", lambda _user: [])
    root, actor = await new_root()
    definition = spec()
    definition.tool_allowlist = ["read", "write"]
    team = TeamSpec(name="Scoped writer", preset_members=[MemberSpec(alias="writer", inline=definition)],
        policy=TeamPolicy(member_selection="explicit_only", delegable_tools=["read", "write"]))
    lineup = await prepare_lineup(team, actor)
    async with write_transaction() as db:
        result = await start_confirmed_locked(db, root=await db.get(Session, root), question_id="scope-start",
            title=team.name, goal="Write a report", policy=team.policy, grant=lineup.grant,
            coordinator=lineup.coordinator, members=lineup.members)
    run = result["id"]
    member = next(item for item in result["members"] if item["role"] == "member")
    async with get_db_session() as db:
        member_session = await db.get(Session, member["id"])
    authority = await runtime_binding.load_binding(member_session)
    target = "/workspace/reports/acceptance.md"
    rules = [permission.Rule(permission="*", pattern="*", action="ask")]

    async def check(path=target, **kwargs):
        await permission.ask(member["id"], "edit", [path], user_id=actor.owner_user_id,
            config_rules=rules, authority_rulesets=authority.permission_planes, **kwargs)

    with pytest.raises(TeamError) as denied:
        await check()
    assert denied.value.current == {"permission": "edit", "patterns": [target]}
    assert not permission._pending
    from agent.hooks import ToolHooks
    hooks = ToolHooks(session_id=member["id"], user_id=actor.owner_user_id,
        config_rules=rules, authority_rule_planes=authority.permission_planes)
    blocked = await hooks.authorize_tool("write", {"file_path": target, "content": "New file"})
    assert json.loads(blocked.output)["current"] == {"permission": "edit", "patterns": [target]}
    assert blocked.metadata["code"] == "PERMISSION_REQUIRES_USER"

    async def propose(scopes, identifier):
        prepared, references = await amendments.prepare(run, replace(actor, kind="member", member_id=root),
            TeamSpec(name="Approve report path", policy=TeamPolicy(permission_rules=scopes)))
        return SimpleNamespace(id=identifier, user_id=actor.owner_user_id, status="answered", answers=[["应用调整"]],
            continuation={"kind": "team_lineup", "mode": "amend", "lineup": prepared.saved(), **references})

    async def apply(row):
        async with write_transaction() as db:
            return await amendments.apply_answer(db, await db.get(Session, root), row, {})

    row = await propose([{"permission": "edit", "pattern": target}], "scope-approve")
    # Preparing the card has no authority effect, nor does declining it.
    with pytest.raises(TeamError):
        await check()
    row.answers = [["保持原样"]]
    await apply(row)
    with pytest.raises(TeamError):
        await check()
    row.id = "scope-reapproved"
    row.answers = [["应用调整"]]
    receipt = await apply(row)
    await check()
    assert await apply(row) == receipt
    with pytest.raises(TeamError):
        await check("/workspace/reports/other.md")
    with pytest.raises(permission.PermissionDeniedError):
        await check(guard_rules=[permission.Rule(permission="edit", pattern=target, action="deny")])
    state = await snapshot(run, actor)
    assert state["grant"]["version"] == 2
    assert (await runtime_binding.load_binding(member_session)).to_json() == authority.to_json()
    await apply(await propose([], "scope-revoke"))
    with pytest.raises(TeamError):
        await check()
    assert (await snapshot(run, actor))["grant"]["version"] == 3


@pytest.mark.parametrize("name", ["external_directory", "bash", "computer"])
async def test_scope_cannot_authorize_a_tool_outside_the_run(config, name):
    _, actor = await new_root()
    with pytest.raises(TeamError) as error:
        await prepare_lineup(TeamSpec(name="Invalid scope", policy=TeamPolicy(
            delegable_tools=["read"], permission_rules=[{"permission": name, "pattern": "*"} ])), actor)
    assert error.value.code == "INVALID_PERMISSION_GRANT"
