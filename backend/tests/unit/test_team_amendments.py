from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_catalog.catalog import prepare_lineup
from agent_catalog.schemas import MemberSpec, TeamPolicy, TeamSpec
from db.base import get_db_session
from db.models.session import Session
from team import amendments, commands
from team.errors import TeamError
from team.journal import command, snapshot, write_transaction
from team.membership import retire_member
from team.service import start_confirmed_locked
from tests.unit.test_team_catalog import config, new_root  # noqa: F401
from tests.unit.test_team_commands import make_task
from tests.unit.test_team_compiler import spec


async def active_team():
    root, actor = await new_root()
    definition = TeamSpec(name="Fixed team", preset_members=[MemberSpec(alias="writer", agent_ref="builtin:writer")],
        policy=TeamPolicy(member_selection="explicit_only", member_creation="disabled", allowed_agent_ids=["builtin:writer"]))
    lineup = await prepare_lineup(definition, actor)
    async with write_transaction() as db:
        session = await db.get(Session, root, with_for_update=True)
        run = await start_confirmed_locked(db, root=session, question_id="start", title="Fixed team", goal="Review work",
            policy=definition.policy, grant=lineup.grant, coordinator=lineup.coordinator, members=lineup.members)
    return run["id"], root, actor


async def checkpoint(run, root, actor, *, additions=True):
    proposed = TeamSpec(name="Add independent review", preset_members=[MemberSpec(alias="review", inline=spec())] if additions else [],
        policy=TeamPolicy())
    prepared, references = await amendments.prepare(run, replace(actor, kind="member", member_id=root), proposed)
    return SimpleNamespace(id="amend", user_id=actor.owner_user_id, answers=[["应用调整"]], status="answered",
        continuation={"kind": "team_lineup", "mode": "amend", "lineup": prepared.saved(), **references})


async def apply(root, row):
    async with write_transaction() as db:
        return await amendments.apply_answer(db, await db.get(Session, root), row, {})


async def test_confirmed_amendment_adds_exact_member_atomically_and_keeps_fixed_roster(config):
    run, root, actor = await active_team()
    row = await checkpoint(run, root, actor)
    before = await snapshot(run, actor)
    result, events = await apply(root, row)
    after = await snapshot(run, actor)
    assert len(after["members"]) == len(before["members"]) + 1
    assert after["grant"]["version"] == 2
    assert after["grant"]["member_selection"] == "explicit_only"
    assert after["grant"]["member_creation"] == "disabled"
    assert len(events) == 1 and events[0]["data"]["seq"] == after["seq"]
    assert (await apply(root, row))[0] == result
    assert await snapshot(run, actor) == after == await snapshot(run, actor, rebuild=True)


async def test_rejection_and_stale_confirmation_never_change_members_or_grants(config):
    run, root, actor = await active_team()
    row = await checkpoint(run, root, actor)
    before = await snapshot(run, actor)
    row.answers = [["保持原样"]]
    assert (await apply(root, row))[0]["metadata"]["rejected"]
    assert await snapshot(run, actor) == before
    row.answers = [["应用调整"]]
    row.continuation["expected_grant_version"] = 0
    with pytest.raises(TeamError) as error:
        await apply(root, row)
    assert error.value.code == "STALE_REVISION"
    assert await snapshot(run, actor) == before
    other_root, _ = await new_root()
    with pytest.raises(TeamError):
        await apply(other_root, row)


async def test_grant_only_amendment_preserves_members_and_original_run_limits(config):
    run, root, actor = await active_team()
    row = await checkpoint(run, root, actor, additions=False)
    before = await snapshot(run, actor)
    await apply(root, row)
    after = await snapshot(run, actor)
    assert after["members"] == before["members"] and after["policy"] == before["policy"]
    assert after["grant"]["version"] == 2
    with pytest.raises(TeamError) as error:
        await amendments.prepare(run, replace(actor, kind="member", member_id=root),
            TeamSpec(name="Change scheduling", policy=TeamPolicy(max_members=32)))
    assert error.value.code == "INVALID_AMENDMENT"


async def test_answer_batch_keeps_valid_amendment_and_returns_stale_card_to_model(config):
    from question.continuation import _apply
    run, root, actor = await active_team()
    first = await checkpoint(run, root, actor)
    second = await checkpoint(run, root, actor)
    first.questions = second.questions = []
    first.session_id = second.session_id = root
    second.id = "amend-later"
    async with write_transaction() as db:
        session = await db.get(Session, root)
        receipt, events = await _apply(db, session, first)
        failure, failed_events = await _apply(db, session, second)
    assert receipt["title"] == "Team amendment confirmed" and events
    assert failure["metadata"]["code"] == "STALE_REVISION" and not failed_events
    state = await snapshot(run, actor)
    assert state["grant"]["version"] == 2
    assert sum(member["alias"] == "review" for member in state["members"].values()) == 1


async def test_failed_card_rolls_back_its_partial_admission(config, monkeypatch):
    from question.continuation import _apply
    from team import service
    run, root, actor = await active_team()
    row = await checkpoint(run, root, actor)
    row.questions = []
    row.session_id = root
    before = await snapshot(run, actor)
    original = service.admit_member
    async def fail_after_admission(*args, **kwargs):
        await original(*args, **kwargs)
        raise TeamError("AGENT_NOT_ACCESSIBLE", "Definition access was revoked")
    monkeypatch.setattr(service, "admit_member", fail_after_admission)
    async with write_transaction() as db:
        receipt, _ = await _apply(db, await db.get(Session, root), row)
    assert receipt["metadata"]["error"]
    assert await snapshot(run, actor, rebuild=True) == before


async def test_retirement_requires_resolved_work_and_preserves_alias_and_history(config):
    run, root, actor = await active_team()
    server = replace(actor, kind="server")
    member = next(m for m in (await snapshot(run, actor))["members"].values() if m["role"] == "member")
    task = await make_task(run, server, member["id"])
    async def retire(writer):
        return await retire_member(writer, member["id"], "Responsibility complete")
    with pytest.raises(TeamError) as error:
        await command(run, server, "retire", {}, retire)
    assert error.value.code == "MEMBER_BUSY"
    await command(run, server, "cancel-task", {}, lambda writer: commands.update_task(writer, task["id"], 1, "cancel", reason="No longer needed"))
    assert (await command(run, server, "retire", {}, retire))["status"] == "retired"
    state = await snapshot(run, actor)
    assert state["members"][member["id"]]["membership_state"] == "retired"
    assert state["tasks"][task["id"]]["state"] == "canceled"
    with pytest.raises(TeamError) as error:
        await amendments.prepare(run, replace(actor, kind="member", member_id=root),
            TeamSpec(name="Reuse alias", preset_members=[MemberSpec(alias="writer", agent_ref="builtin:writer")]))
    assert error.value.code == "TEAM_MEMBER_ALIAS_TAKEN"
def test_grant_only_fixed_roster_amendment_passes_the_actual_tool_schema():
    from pydantic import ValidationError
    from tool.team_tools import Proposal
    payload = {"title": "Approve one path", "goal": "Keep the existing roster", "team": {
        "name": "Grant change", "preset_members": [], "policy": {"member_selection": "explicit_only",
        "permission_rules": [{"permission": "edit", "pattern": "/workspace/test/answer.txt"}]}}}
    proposal = Proposal.model_validate({"mode": "amend", **payload})
    assert proposal.team.preset_members == []
    assert proposal.team.policy.model_fields_set == {"member_selection", "permission_rules"}
    with pytest.raises(ValidationError, match="empty lineup"):
        Proposal.model_validate({"mode": "create", **payload})
