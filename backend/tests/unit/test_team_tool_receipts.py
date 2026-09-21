"""Receipt watermarks cannot swallow peer messages on a response-lost retry."""
import json

from team import commands, scheduler
from team.journal import command, snapshot
from tests.unit.test_team_paid_tools import paid
from tool.team_tools import TaskUpdate, Wait, task_update, wait


async def test_replayed_tool_receipt_keeps_original_watermark_and_wait_observes_later_mail(paid, monkeypatch):
    monkeypatch.setattr(scheduler, "schedule", lambda *_: None)
    paid.ctx.part_id = "progress-receipt"
    args = TaskUpdate(task_id=paid.attempt["task_id"], action="progress", summary="Working on the assigned result")
    original = json.loads((await task_update(args, paid.ctx)).output)
    assert original["seq"] == (await snapshot(paid.run, paid.actor))["seq"]

    async def peer_message(writer):
        return await commands.queue_message(writer, to_member_id=paid.ctx.session_id,
            body="A new constraint arrived after that progress receipt")
    await command(paid.run, paid.server, "later-message", {}, peer_message)
    later_seq = (await snapshot(paid.run, paid.actor))["seq"]
    replayed = json.loads((await task_update(args, paid.ctx)).output)
    assert replayed == original
    assert (await snapshot(paid.run, paid.actor))["seq"] == later_seq

    paid.ctx.part_id = "wait-from-receipt"
    response = await wait(Wait(after_seq=replayed["seq"]), paid.ctx)
    observed = json.loads(response.output)
    assert observed["status"] == "changed"
    assert not response.metadata["turn_yield"]
    # An unchanged command has a retained notice/receipt of its own.
    assert observed["seq"] == (await snapshot(paid.run, paid.actor))["seq"]
