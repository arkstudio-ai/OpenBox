"""Model reads expose usable result references without leaking private mail."""
from dataclasses import replace

import pytest

from team import commands
from team.errors import TeamError
from team.journal import command, snapshot
from team.model_view import view
from team.service import finish
from tests.unit.test_team_commands import make_task, setup_team


def test_optional_task_filter_accepts_empty_strict_provider_field():
    from tool.collaboration.team_tools import TeamView
    assert TeamView(task_id="").task_id is None
    assert TeamView(task_id="  ").task_id is None
    assert TeamView(task_id="ttask_actual").task_id == "ttask_actual"


async def read_as(run, server, member, key, **options):
    async def read(writer):
        writer.actor = replace(writer.actor, member_id=member)
        return await view(writer, section=options.get("section", "summary"),
            offset=options.get("offset", 0), limit=options.get("limit", 20), task_id=options.get("task_id"))
    return await command(run, server, key, {}, read)


async def test_model_view_paginates_full_tasks_and_hydrates_only_the_members_mail():
    run, actor, server, root, members = await setup_team()
    first = await make_task(run, server, members[0])
    second = await make_task(run, server, members[1], "second", dependencies=[first["id"]])
    for index, (sender, recipient, body) in enumerate([
        (root, members[0], "Only researcher should read this"),
        (members[0], members[1], "Shared result reference"),
    ]):
        async def send(writer):
            return await commands.queue_message(writer, from_member_id=sender, to_member_id=recipient, body=body)
        await command(run, server, f"mail-{index}", {}, send)
    page = await read_as(run, server, root, "page-1", section="tasks", limit=1)
    assert page["items"][0]["description"] == first["description"]
    assert page["total"] == 2 and page["next_offset"] == 1
    page2 = await read_as(run, server, root, "page-2", section="tasks", offset=page["next_offset"], limit=1)
    assert page2["items"][0]["id"] == second["id"] and page2["next_offset"] is None
    summary = await read_as(run, server, members[1], "summary")
    assert {item["id"] for item in summary["tasks"]} == {first["id"], second["id"]}
    mail = await read_as(run, server, members[1], "member-mail", section="messages")
    assert [item["body"] for item in mail["items"]] == ["Shared result reference"]
    all_mail = await read_as(run, server, root, "coordinator-mail", section="messages")
    assert all_mail["total"] == 2
    with pytest.raises(TeamError, match="does not belong"):
        await read_as(run, server, root, "foreign-task", section="attempts", task_id="other-run-task")
    assert await snapshot(run, actor) == await snapshot(run, actor, rebuild=True)


@pytest.mark.parametrize("failure", ["block", "fail"])
async def test_blocker_report_cannot_replace_the_original_goal_at_finish(failure):
    run, actor, server, root, members = await setup_team()
    original = await make_task(run, server, members[0])
    report = await make_task(run, server, members[1], "report", deliverable=True)
    await command(run, server, "dispatch", {}, commands.dispatch_ready)

    async def update(writer, task_id, action, **fields):
        task = writer.state["tasks"][task_id]
        return await commands.update_task(writer, task_id, task["revision"], action, **fields)

    await command(run, server, "blocked", {}, lambda writer: update(writer, original["id"], failure, summary="Cannot perform the requested work"))
    await command(run, server, "report-done", {}, lambda writer: update(writer, report["id"], "submit", summary="Report explains the blocker"))
    await command(run, server, "accept-report", {}, lambda writer: update(writer, report["id"], "accept"))
    before = await snapshot(run, actor)
    with pytest.raises(TeamError) as error:
        await command(run, server, "premature-finish", {}, lambda writer: finish(writer, "Completed", []))
    assert error.value.code == "DELIVERABLES_INCOMPLETE"
    assert error.value.current[0]["id"] == original["id"]
    assert await snapshot(run, actor) == before
    await command(run, server, "retry", {}, lambda writer: update(writer, original["id"], "retry", reason="The missing permission was approved"))
    await command(run, server, "redispatch", {}, commands.dispatch_ready)
    await command(run, server, "original-done", {}, lambda writer: update(writer, original["id"], "submit", summary="The requested work is now verified"))
    result = await command(run, server, "actual-finish", {}, lambda writer: finish(writer, "Completed", []))
    assert result["state"] == "completing"
