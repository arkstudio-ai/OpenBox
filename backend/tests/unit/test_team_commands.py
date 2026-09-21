from dataclasses import replace
import json

import pytest
from sqlalchemy import select

from agent_catalog.compiler import compile_agent
from agent_catalog.schemas import MemberSpec
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from team import commands
from team.errors import TeamError
from team.journal import command, snapshot
from team.service import admit_member
from tests.unit.test_subagent_composition import _config
from tests.unit.test_team_compiler import spec
from tests.unit.test_team_journal import seed_run


async def setup_team(*, scope=None):
    run_id, actor, root = await seed_run(scope=scope)
    server = replace(actor, kind="server")
    compiled = compile_agent(spec(), config=_config("openai/test"))
    members = []
    for alias in ("researcher", "writer"):
        member = MemberSpec(alias=alias, inline=spec())
        async def admit(writer):
            return await admit_member(writer, member, compiled, source="coordinator")
        members.append((await command(run_id, server, f"admit:{alias}", {}, admit))["member"]["id"])
    return run_id, actor, server, root, members


async def make_task(run_id, server, member, key="create", **changes):
    fields = {"title": "Compare sources", "description": "Analyze the supplied sources",
        "expected_output": "Findings", "acceptance_criteria": "Cite evidence", "owner_member_id": member, **changes}
    async def create(writer):
        return await commands.create_task(writer, fields)
    return (await command(run_id, server, key, fields, create))["task"]


async def test_dependency_dispatch_submission_and_deliverable_review_share_durable_inbox():
    run_id, actor, server, root, members = await setup_team()
    first = await make_task(run_id, server, members[0])
    second = await make_task(run_id, server, members[1], "second", dependencies=[first["id"]], deliverable=True)
    dispatched = await command(run_id, server, "dispatch", {}, commands.dispatch_ready)
    assert [d["task_id"] for d in dispatched["dispatched"]] == [first["id"]]
    async with get_db_session() as db:
        rows = (await db.execute(select(AgentInboxItem).where(AgentInboxItem.session_id.in_(members)))).scalars().all()
    assert len(rows) == 1 and rows[0].source_type == "team_task"
    async def submit(writer):
        task = writer.state["tasks"][first["id"]]
        return await commands.update_task(writer, task["id"], task["revision"], "submit", summary="The two sources agree.")
    completed = await command(run_id, server, "submit", {}, submit)
    assert completed["task"]["state"] == "succeeded"
    later = await command(run_id, server, "dispatch-second", {}, commands.dispatch_ready)
    assert [d["task_id"] for d in later["dispatched"]] == [second["id"]]
    async def deliverable(writer):
        task = writer.state["tasks"][second["id"]]
        return await commands.update_task(writer, task["id"], task["revision"], "submit", summary="Final report ready.")
    result = await command(run_id, server, "deliverable", {}, deliverable)
    assert result["task"]["state"] == "review"
    assert await snapshot(run_id, actor, rebuild=True) == await snapshot(run_id, actor)


async def test_dag_edit_and_failed_submission_are_atomic():
    run_id, actor, server, root, members = await setup_team()
    first = await make_task(run_id, server, members[0], output_schema={"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]})
    second = await make_task(run_id, server, members[1], "second", dependencies=[first["id"]])
    async def cycle(writer):
        return await commands.update_task(writer, first["id"], 1, "edit", changes={"dependencies": [second["id"]]})
    with pytest.raises(TeamError) as exc:
        await command(run_id, server, "cycle", {}, cycle)
    assert exc.value.code == "DEPENDENCY_CYCLE"
    await command(run_id, server, "dispatch", {}, commands.dispatch_ready)
    before = await snapshot(run_id, actor)
    async def bad_result(writer):
        return await commands.update_task(writer, first["id"], 2, "submit", summary="Done", output={"count": "incorrect"})
    with pytest.raises(TeamError) as exc:
        await command(run_id, server, "bad-result", {}, bad_result)
    assert exc.value.code == "INVALID_TASK_RESULT"
    assert await snapshot(run_id, actor) == before


async def test_progress_does_not_deliver_or_consume_pending_mailbox_and_duplicate_command_does_not_reapply():
    run_id, actor, server, root, members = await setup_team()
    async def progress(writer):
        return await commands.queue_message(writer, from_member_id=members[0], to_member_id=root, body="Reading sources", kind="progress")
    result = await command(run_id, server, "progress", {}, progress)
    before = await snapshot(run_id, actor)
    assert result["message"]["state"] == "recorded"
    assert await command(run_id, server, "progress", {}, progress) == result
    assert await snapshot(run_id, actor) == before
    async with get_db_session() as db:
        rows = (await db.execute(select(AgentInboxItem).where(AgentInboxItem.session_id == root))).scalars().all()
    assert not rows


async def test_user_cannot_impersonate_a_coordinator_to_create_work():
    run_id, actor, server, root, members = await setup_team()
    with pytest.raises(TeamError) as exc:
        await make_task(run_id, actor, members[0])
    assert exc.value.code == "AUTHORITY_REVOKED"


async def test_peer_mail_as_first_input_includes_recipient_identity_without_user_authority():
    run, actor, server, root, members = await setup_team()
    async def deliver(writer):
        await commands.queue_message(writer, from_member_id=members[0], to_member_id=members[1], body="Check this supplied fact.")
        message = writer.events[-1].payload["data"]
        inbox_id = await commands.deliver_message(writer, message)
        return {"inbox_id": inbox_id}
    result = await command(run, server, "mail-first", {}, deliver)
    async with get_db_session() as db:
        row = await db.get(AgentInboxItem, result["inbox_id"])
        assert row.source_type == "team_message"
        body = json.loads(row.prompt.split("\n", 2)[2])
    assert body["identity"] == {"team_run_id": run, "member_id": members[1], "alias": "writer",
        "responsibility": "", "coordinator_id": root}
    assert body["output_dir"] == f".openbox/teams/{run}/{members[1]}"
    assert body["message"] == "Check this supplied fact."
    assert not (await snapshot(run, actor))["tasks"]


@pytest.mark.parametrize("reason", [None, "", "  \n"])
async def test_retry_missing_reason_is_an_argument_error_and_preserves_blocked_result(reason):
    run, actor, server, _, members = await setup_team()
    task = await make_task(run, server, members[0], deliverable=True)
    await command(run, server, "dispatch", {}, commands.dispatch_ready)
    await command(run, server, "block", {}, lambda writer: commands.update_task(
        writer, task["id"], 2, "block", summary="Required source is missing", output={"partial": "retained"}))
    before = await snapshot(run, actor)
    with pytest.raises(TeamError) as exc:
        await command(run, server, "bad-retry", {}, lambda writer: commands.update_task(
            writer, task["id"], 3, "retry", summary="Corrected input is available", reason=reason))
    assert exc.value.code == "INVALID_TASK" and exc.value.status == 422
    assert "reason field" in str(exc.value) and "summary" in str(exc.value)
    assert exc.value.current["state"] == "blocked"
    assert await snapshot(run, actor) == before
    retried = await command(run, server, "fixed-retry", {}, lambda writer: commands.update_task(
        writer, task["id"], 3, "retry", reason="Corrected input is available"))
    assert retried["task"]["state"] == "pending"
    assert (await snapshot(run, actor))["attempts"] == before["attempts"]


async def test_retry_review_result_returns_correct_action_and_does_not_redispatch():
    run, actor, server, _, members = await setup_team()
    task = await make_task(run, server, members[0], deliverable=True)
    await command(run, server, "dispatch", {}, commands.dispatch_ready)
    await command(run, server, "submit", {}, lambda writer: commands.update_task(
        writer, task["id"], 2, "submit", summary="Finished result"))
    before = await snapshot(run, actor)
    with pytest.raises(TeamError) as exc:
        await command(run, server, "wrong-action", {}, lambda writer: commands.update_task(
            writer, task["id"], 3, "retry", reason="Please correct the conclusion"))
    assert exc.value.code == "INVALID_TASK_TRANSITION"
    assert "current state is 'review'" in str(exc.value) and "accept/rework" in str(exc.value)
    assert await snapshot(run, actor) == before


@pytest.mark.parametrize("schema,output,expected", [
    ({"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]}, '{"count": 3}', {"count": 3}),
    ({"type": "object", "properties": {"values": {"type": "array", "items": {"type": "integer"}}, "literal": {"type": "string"}}},
        json.dumps({"values": [1, 2], "literal": '{"x":true}'}), {"values": [1, 2], "literal": '{"x":true}'}),
    (None, '{"literal": true}', '{"literal": true}'),
])
async def test_provider_json_text_is_decoded_only_if_required_and_valid(schema, output, expected):
    run, actor, server, _, members = await setup_team()
    task = await make_task(run, server, members[0], deliverable=True, output_schema=schema)
    await command(run, server, "dispatch", {}, commands.dispatch_ready)
    await command(run, server, "submit", {}, lambda writer: commands.update_task(
        writer, task["id"], 2, "submit", summary="Validated result", output=output))
    state = await snapshot(run, actor)
    assert next(iter(state["attempts"].values()))["output"] == expected
    assert state["tasks"][task["id"]]["state"] == "review"
    assert state == await snapshot(run, actor, rebuild=True)


@pytest.mark.parametrize("output", ['{"count":"wrong"}', '{invalid', '3'])
async def test_json_text_does_not_bypass_result_schema(output):
    run, actor, server, _, members = await setup_team()
    task = await make_task(run, server, members[0], output_schema={"type": "object",
        "properties": {"count": {"type": "integer"}}, "required": ["count"]})
    await command(run, server, "dispatch", {}, commands.dispatch_ready)
    before = await snapshot(run, actor)
    with pytest.raises(TeamError) as exc:
        await command(run, server, "submit", {}, lambda writer: commands.update_task(
            writer, task["id"], 2, "submit", summary="Invalid result", output=output))
    assert exc.value.code == "INVALID_TASK_RESULT"
    assert await snapshot(run, actor) == before
