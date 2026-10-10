"""A pending card owns its conversation's next turn (production deadlock, 2026-10-10).

A desktop takeover left a task waiting_input while the assistant kept delegating
follow-ups into it. Each one was claimed by a generation that start_run refused
and stayed claimed forever: the card went stale (its bound generation moved),
"取消任务" got 409 and "暂停" never settled. These run the real Inbox, Driver,
Question and Task transactions on SQL.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

from agent import inbox, loop
from agent.driver import reserve_run
from assistant.commands import accept_task_command
from assistant.control import accept_control_command, recover_controls
from assistant.inputs import accept_turn
from assistant.reads import get_task
from assistant.request_answers import list_waiting
from assistant.requests import list_requests
from assistant.service import ensure_main_session
from assistant.task_context import task_context
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantTask, TaskResult, TaskSubmission
from db.models.message import Message
from db.models.part import Part
from db.models.question import QuestionCheckpoint
from db.models.session import Session
from models.message import TextPart, ToolPartData
from question import question as q, runtime
from question.continuation import apply_answers
from session.agent_event_log import load_canonical_model_surface, verify_agent_event_parity
from session.internal_parts import _lock_fenced, begin_session_write
from session.session import create_assistant_message, save_part, update_message_info
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_sessions_v2 import MAIN_MESSAGES, finish_turn, followup, main_turn, tool_call
from tests.unit.test_assistant_steering import running
from tool.desktop_takeover import ANSWER_DONE, ANSWER_SKIP, DesktopTakeoverArgs, execute as takeover
from tool.question_tool import QuestionArgs, execute as ask_question
from tool.tool import ToolContext
from voice.assistant_link import running_tasks

REAL_WAKE = inbox.schedule_inbox_wake


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from core.config import get_config
    monkeypatch.setattr(get_config(), "jwt_secret", "card-admission-test-only")
    # Tests drive each wake themselves; a background one would race their assertions.
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)
    MAIN_MESSAGES.clear()


def scope(args):
    return {key: args[key] for key in ("user_id", "workspace_id", "main_id")}


async def waiting_task(*, takeover_card=False):
    """A watched task whose run filed a card (a question, or a desktop takeover) and released."""
    args, created, lease, batch = await running()
    owner = lease.user_id
    ticket = await runtime.start_run(lease.session_id, owner, driver_lease=lease)
    token = runtime.current_run.set(ticket)
    try:
        fence = (lease.session_id, lease.run_id, lease.generation)
        message = await create_assistant_message(lease.session_id, batch.messages[0].id, agent="build",
            model_id="test/model", user_id=owner, run_fence=fence)
        part = ToolPartData(session_id=lease.session_id, message_id=message.id,
                            tool="desktop_takeover" if takeover_card else "question", status="running")
        await save_part(part, is_new=True, user_id=owner, run_fence=fence)
        ctx = ToolContext(session_id=lease.session_id, user_id=owner, workspace_id=args["workspace_id"],
                          message_id=message.id, part_id=part.id)
        with pytest.raises(q.QuestionSuspended) as suspended:
            if takeover_card:
                await takeover(DesktopTakeoverArgs(reason="login_required", url="https://login.example.com/shop",
                                                   page="login"), ctx)
            else:
                await ask_question(QuestionArgs(questions=[{"question": "Choose a color?", "custom": False,
                    "options": [{"label": "Blue"}, {"label": "Green"}]}]), ctx)
        message.finish = "waiting_input"
        await update_message_info(message, user_id=owner, run_fence=fence)
        await runtime.finish_run(ticket)
        await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
    finally:
        runtime.current_run.reset(token)
        await lease.release(session_status="waiting_input")
    return args, created["execution_session_id"], suspended.value.request_id


async def generation(session_id):
    async with get_db_session() as db:
        return (await db.get(AgentDriverState, session_id)).generation


async def item(item_id):
    async with get_db_session() as db:
        return await db.get(AgentInboxItem, item_id)


async def card_status(request_id):
    async with get_db_session() as db:
        return (await db.get(QuestionCheckpoint, request_id)).status


async def platform(args, session_id, text):
    """Input the assistant delegated (origin assistant_delegation), queued in the execution session."""
    async with get_db_session() as db:
        intent = (await db.get(AssistantTask, args["task_id"])).intent_revision
    return (await inbox.accept_inbox_item(session_id=session_id, user_id=args["user_id"], delivery="followup",
        prompt=text, origin="assistant_delegation",
        origin_ref={"task_id": args["task_id"], "intent_revision": intent})).id


async def human(args, text):
    """The user's own input to the task (origin human), as the task card or execution page sends it."""
    async with get_db_session() as db:
        revision = (await db.get(AssistantTask, args["task_id"])).control_revision
    return (await accept_task_command(**scope(args), task_id=args["task_id"], idempotency_key=f"human-{revision}",
                                      prompt=text, expected_revision=revision))["inbox_id"]


async def delegate(args, text):
    """The assistant's own tasks.followup into the task from a main turn citing the user; (receipt, turn inbox id)."""
    async with get_db_session() as db:
        main = await db.get(Session, args["main_id"])
        revision = (await db.get(AssistantTask, args["task_id"])).control_revision
    ctx, lease, said = await main_turn(args["user_id"], args["workspace_id"], main, f"让它继续：{text}")
    try:
        value, _ = await tool_call(ctx, "tasks.followup", followup(args["task_id"], revision, text, said))
        await finish_turn(ctx, lease)
    finally:
        await lease.release(session_status="idle")
    async with get_db_session() as db:
        turn = await db.scalar(select(AgentInboxItem.id).where(AgentInboxItem.message_id == said))
    return value, turn


async def run_claimed(lease, batch, text="Done."):
    """The run of one claimed boundary, settled the way the agent loop settles it."""
    ticket = await runtime.start_run(lease.session_id, lease.user_id, driver_lease=lease)
    assert ticket is not None
    try:
        fence = (lease.session_id, lease.run_id, lease.generation)
        message = await create_assistant_message(lease.session_id, batch.messages[-1].id, agent="build",
            model_id="test/model", user_id=lease.user_id, run_fence=fence)
        await save_part(TextPart(session_id=lease.session_id, message_id=message.id, text=text),
                        is_new=True, user_id=lease.user_id, run_fence=fence)
        message.finish = "stop"
        await update_message_info(message, user_id=lease.user_id, run_fence=fence)
        await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
        await runtime.finish_run(ticket, completed=True)
    finally:
        await lease.release(session_status="idle")


async def stranded(monkeypatch, session_id, owner, *, release=True):
    """What the code did before the gate: claim platform input although a card owned the turn."""
    async def never_blocked(*_args, **_kwargs):
        return False
    with monkeypatch.context() as patch:
        patch.setattr("question.runtime.admission_blocked_locked", never_blocked)
        claimed = await inbox._reserve_and_claim(session_id, owner)
    assert claimed is not None
    lease, batch = claimed
    if release:
        # The old loop: start_run refused, the lease was released, the claim stayed.
        assert await runtime.start_run(session_id, owner, driver_lease=lease) is None
        await lease.release()
    return lease, batch


async def expire(item_ids):
    async with get_db_session() as db:
        await db.execute(update(AgentInboxItem).where(AgentInboxItem.id.in_(list(item_ids))).values(
            claim_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))


async def listed(args):
    return [request["id"] for request in (await list_requests(**scope(args)))["items"]]


async def test_platform_followup_waits_unclaimed_while_a_card_is_pending():
    args, session_id, request_id = await waiting_task()
    owner = args["user_id"]
    before = await generation(session_id)
    queued = await platform(args, session_id, "Also add a summary")
    assert not await inbox._has_waking_input(session_id, owner)
    assert await inbox.wake_inbox_session(session_id, owner) is None
    assert await inbox.resume_claimable_inbox_sessions() == []
    assert (await item(queued)).state == "accepted"
    assert await generation(session_id) == before
    assert await listed(args) == [request_id]
    async with get_db_session() as db:
        assert (await db.get(Session, session_id)).status == "waiting_input"


async def test_human_input_supersedes_the_card_and_runs_ahead_of_queued_platform_input():
    args, session_id, request_id = await waiting_task()
    owner = args["user_id"]
    queued = await platform(args, session_id, "Platform follow-up")
    person = await human(args, "Use blue, stop asking")
    lease, batch = await inbox._reserve_and_claim(session_id, owner)
    assert [receipt.id for receipt in batch.receipts] == [person]
    assert await card_status(request_id) == "superseded"
    assert (await item(queued)).state == "accepted"
    await run_claimed(lease, batch)
    lease, batch = await inbox._reserve_and_claim(session_id, owner)
    assert [receipt.id for receipt in batch.receipts] == [queued]
    await run_claimed(lease, batch)
    assert (await verify_agent_event_parity(session_id, user_id=owner, require_closed=True)).ok


async def test_answered_card_resumes_first_then_queued_followups_run_in_order():
    args, session_id, request_id = await waiting_task()
    owner = args["user_id"]
    first = await platform(args, session_id, "Follow-up 1")
    second = await platform(args, session_id, "Follow-up 2")
    request = await q.get_request(request_id, owner)
    await q.reply(request_id, [["Blue"]], owner, reply_id="reply-1",
                  expected_request_revision=request.assistant["request_revision"],
                  options_hash=request.assistant["options_hash"], source_ref={"kind": "card"})
    # The saved answer resumes its own turn before any queued input.
    assert not await inbox._has_waking_input(session_id, owner)
    resumed = await apply_answers(session_id, owner)
    assert resumed is not None and not await inbox._has_waking_input(session_id, owner)
    async with get_db_session() as db:
        waiting = await db.get(Message, (await db.get(QuestionCheckpoint, request_id)).message_id)
    lease = await reserve_run(session_id, owner, trigger_message_id=waiting.parent_id)
    ticket = await runtime.start_run(session_id, owner, expected_generation=resumed, driver_lease=lease)
    assert ticket is not None
    try:
        assert (await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=False)).empty
        fence = (session_id, lease.run_id, lease.generation)
        answer = await create_assistant_message(session_id, waiting.parent_id, agent="build",
            model_id="test/model", user_id=owner, run_fence=fence)
        await save_part(TextPart(session_id=session_id, message_id=answer.id, text="Blue it is."),
                        is_new=True, user_id=owner, run_fence=fence)
        answer.finish = "stop"
        await update_message_info(answer, user_id=owner, run_fence=fence)
        await inbox.settle_claimed_inbox_items(lease, result_message_id=answer.id, outcome="succeeded")
        await runtime.finish_run(ticket, completed=True)
    finally:
        await lease.release(session_status="idle")
    order = []
    for _ in range(2):
        lease, batch = await inbox._reserve_and_claim(session_id, owner)
        order.extend(receipt.id for receipt in batch.receipts)
        await run_claimed(lease, batch)
    assert order == [first, second]
    assert await inbox._reserve_and_claim(session_id, owner) is None


async def test_refused_start_settles_its_claim_not_run_and_hides_it_from_the_model(monkeypatch):
    args, session_id, request_id = await waiting_task()
    owner = args["user_id"]
    sent, _turn = await delegate(args, "Also export a CSV")
    lease, batch = await stranded(monkeypatch, session_id, owner, release=False)
    assert [receipt.id for receipt in batch.receipts] == [sent["inbox_id"]]
    assert await loop.run_loop(session_id, owner, lease=lease) is None
    row = await item(sent["inbox_id"])
    assert (row.state, row.outcome, row.error["code"]) == ("settled", "not_run", "TASK_WAITING_INPUT")
    async with get_db_session() as db:
        closing = await db.get(Message, row.result_message_id)
        assert closing.parent_id == row.message_id and closing.finish == "error"
        assert closing.error["code"] == "TASK_WAITING_INPUT"
        driver = await db.get(AgentDriverState, session_id)
        assert driver.phase == "idle" and driver.run_id is None
        assert (await db.get(Session, session_id)).status == "waiting_input"
        submission = await db.scalar(select(TaskSubmission).where(TaskSubmission.inbox_id == row.id))
        assert submission.disposition == "not_applied"
        assert (await db.get(AssistantTask, args["task_id"])).observed_state == "waiting_input"
        # Nothing ran, so nothing is reported as done.
        assert await db.scalar(select(func.count()).select_from(TaskResult).where(
            TaskResult.task_id == args["task_id"])) == 0
        exclusion = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == session_id,
            AgentEvent.kind == "surface.model_exclusion"))
        assert exclusion.payload["message_ids"] == [row.message_id, closing.id]
        assert exclusion.payload["reason"] == "inbox_waiting_input"
    surface = await load_canonical_model_surface(session_id, user_id=owner, repair_tail=False)
    assert not {row.message_id, closing.id} & {message.id for message in surface.messages}
    # The card is still current and answerable.
    assert await listed(args) == [request_id]
    assert (await verify_agent_event_parity(session_id, user_id=owner, require_closed=True)).ok
    view = await get_task(**scope(args), task_id=args["task_id"])
    assert view["latest_submission"]["error"]["code"] == "TASK_WAITING_INPUT"


async def test_orphan_sweeper_closes_stranded_claims_without_a_new_generation(monkeypatch):
    args, session_id, request_id = await waiting_task(takeover_card=True)
    owner = args["user_id"]
    stuck = [await platform(args, session_id, f"Follow-up {number}") for number in (1, 2)]
    for _ in stuck:
        await stranded(monkeypatch, session_id, owner)
    bumped = await generation(session_id)
    # The production symptom: the claims stayed and the card dropped out of 我的任务.
    assert [(await item(item_id)).state for item_id in stuck] == ["claimed", "claimed"]
    assert await listed(args) == []
    assert await inbox.settle_orphaned_claims() == 0  # still within the claim window
    await expire(stuck)
    assert await inbox.settle_orphaned_claims() == 2
    assert await inbox.settle_orphaned_claims() == 0
    assert await generation(session_id) == bumped
    for item_id in stuck:
        row = await item(item_id)
        assert (row.state, row.outcome) == ("settled", "not_run") and row.result_message_id
    assert await listed(args) == [request_id] and await card_status(request_id) == "pending"
    assert (await verify_agent_event_parity(session_id, user_id=owner, require_closed=True)).ok


async def test_card_survives_refused_generations_but_not_consumed_input(monkeypatch):
    args, session_id, request_id = await waiting_task()
    owner = args["user_id"]
    request = await q.get_request(request_id, owner)
    # A legacy caller whose run start_run refused: one generation, nothing consumed.
    assert await loop.run_loop(session_id, owner) is None
    # A stranded claim closed as not_run consumed nothing either.
    queued = await platform(args, session_id, "Follow-up")
    lease, _ = await stranded(monkeypatch, session_id, owner, release=False)
    assert await loop.run_loop(session_id, owner, lease=lease) is None
    assert await generation(session_id) == request.assistant["generation"] + 2
    assert await listed(args) == [request_id]
    assert (await q.get_request(request_id, owner)).assistant == request.assistant
    # Input that a later generation actually consumed moves the conversation on.
    async with get_db_session() as db:
        (await db.get(AgentInboxItem, queued)).outcome = "succeeded"
    assert await listed(args) == []
    with pytest.raises(q.QuestionGone):
        await q.reply(request_id, [["Blue"]], owner, reply_id="late",
                      expected_request_revision=request.assistant["request_revision"],
                      options_hash=request.assistant["options_hash"], source_ref={"kind": "card"})


async def main_card(owner, workspace, main):
    """The main session's own turn stopped at a confirmation card; its run released, waiting."""
    ctx, lease, _said = await main_turn(owner, workspace, main, "把旧项目删掉")
    message = MAIN_MESSAGES.pop(ctx.message_id)
    try:
        part = ToolPartData(session_id=main.id, message_id=message.id, tool="question", status="running")
        await save_part(part, is_new=True, user_id=owner, run_fence=ctx.run_fence)
        with pytest.raises(q.QuestionSuspended) as suspended:
            await q.ask(main.id, [q.Question(question="确认删除旧项目？", custom=False, options=[
                q.QuestionOption(label="确认"), q.QuestionOption(label="取消")])],
                {"messageID": message.id, "callID": part.id}, owner)
        message.finish = "waiting_input"
        await update_message_info(message, user_id=owner, run_fence=ctx.run_fence)
        await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
    finally:
        await lease.release(session_status="waiting_input")
    return suspended.value.request_id


async def test_main_session_report_waits_for_a_pending_confirmation_card():
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    request_id = await main_card(owner, workspace, main)
    before = await generation(main.id)
    async with get_db_session() as db:
        await begin_session_write(db)
        locked = await _lock_fenced(db, main.id, owner)
        report = (await inbox.accept_inbox_item_locked(db, locked, delivery="followup", prompt="Task result",
            client_id="report-1", origin="task_result",
            origin_ref={"result_id": "result-1", "report_attempt": 1, "execution_mode": "report_only"})).id
    # A claimed report would be refused by start_run and then block every later main turn.
    assert not await inbox._has_waking_input(main.id, owner)
    assert await inbox.wake_inbox_session(main.id, owner) is None
    assert (await item(report)).state == "accepted" and await generation(main.id) == before
    person = (await accept_turn(user_id=owner, workspace_id=workspace, main_id=main.id,
                                client_id="human-1", text="先不删了"))["inbox_id"]
    lease, batch = await inbox._reserve_and_claim(main.id, owner)
    try:
        assert [receipt.id for receipt in batch.receipts] == [person]
        assert await card_status(request_id) == "superseded"
        await inbox.settle_claimed_inbox_items(lease, outcome="aborted", result_message_id=None)
    finally:
        await lease.release(session_status="idle")
    lease, batch = await inbox._reserve_and_claim(main.id, owner)
    try:
        assert [receipt.id for receipt in batch.receipts] == [report]
        await inbox.settle_claimed_inbox_items(lease, outcome="aborted", result_message_id=None)
    finally:
        await lease.release(session_status="idle")


async def test_delegated_followup_to_a_waiting_task_says_which_card_blocks_it():
    args, session_id, request_id = await waiting_task(takeover_card=True)
    sent, turn = await delegate(args, "Download the orders once logged in")
    blocked = sent["blocked_by"]
    assert blocked["request_id"] == request_id and blocked["kind"] == "desktop_takeover"
    assert blocked["host"] == "login.example.com" and blocked["link"] == f"/app/s/{session_id}"
    assert blocked["browser"] == "extension" and blocked["done_option"] == ANSWER_DONE
    assert (await item(sent["inbox_id"])).state == "accepted"
    view = await get_task(**scope(args), task_id=args["task_id"])
    assert view["task"]["observed_state"] == "waiting_input"
    assert view["blocked_by"]["request_id"] == request_id and view["blocked_by"]["kind"] == "desktop_takeover"
    async with get_db_session() as db:
        watched = await task_context(db, await db.get(Session, args["main_id"]))
    [entry] = [entry for entry in watched["items"] if entry["task_id"] == args["task_id"]]
    assert entry["blocked_by"]["kind"] == "desktop_takeover" and entry["pending_questions"] == 1
    # The call never tells it as passed on and being worked on.
    assert await running_tasks(args["main_id"], args["user_id"], turn) == []


async def test_cancel_after_stranded_claims_closes_the_card_and_every_claim_without_409(monkeypatch):
    args, session_id, request_id = await waiting_task(takeover_card=True)
    owner = args["user_id"]
    stuck = [await platform(args, session_id, f"Follow-up {number}") for number in (1, 2)]
    for _ in stuck:
        await stranded(monkeypatch, session_id, owner)
    bumped = await generation(session_id)
    async with get_db_session() as db:
        revision = (await db.get(AssistantTask, args["task_id"])).control_revision
    receipt = await accept_control_command(**scope(args), task_id=args["task_id"], action="cancel",
        idempotency_key="cancel-stuck", expected_revision=revision, expected_run=None)
    assert receipt["observed_state"] == "canceled"
    for item_id in stuck:
        row = await item(item_id)
        assert (row.state, row.outcome) == ("settled", "not_run")
    async with get_db_session() as db:
        assert (await db.get(QuestionCheckpoint, request_id)).status == "cancelled"
        assert await db.scalar(select(AgentInboxItem.id).where(AgentInboxItem.session_id == session_id,
            AgentInboxItem.state == "claimed")) is None
        task = await db.get(AssistantTask, args["task_id"])
        assert task.desired_state == task.observed_state == "canceled"
        result = await db.get(TaskResult, task.latest_result_id)
        assert result.outcome == "aborted" and result.generation < bumped
        assert (await db.get(AgentDriverState, session_id)).generation == bumped
    assert (await verify_agent_event_parity(session_id, user_id=owner, require_closed=True)).ok


async def test_pause_with_a_stranded_claim_settles_once_the_sweeper_closes_it(monkeypatch):
    args, session_id, _request_id = await waiting_task(takeover_card=True)
    owner = args["user_id"]
    stuck = await platform(args, session_id, "Follow-up")
    await stranded(monkeypatch, session_id, owner)
    async with get_db_session() as db:
        revision = (await db.get(AssistantTask, args["task_id"])).control_revision
    receipt = await accept_control_command(**scope(args), task_id=args["task_id"], action="pause",
        idempotency_key="pause-stuck", expected_revision=revision, expected_run=None)
    assert receipt["observed_state"] == "pausing"  # the claim is still open
    await expire([stuck])
    assert await inbox.settle_orphaned_claims() == 1
    await recover_controls(task_id=args["task_id"], launch=False)
    async with get_db_session() as db:
        task = await db.get(AssistantTask, args["task_id"])
        assert task.desired_state == task.observed_state == "paused"


async def test_takeover_is_answered_only_with_its_completion_option_on_the_users_own_words():
    args, session_id, request_id = await waiting_task(takeover_card=True)
    owner, workspace = args["user_id"], args["workspace_id"]
    [waiting] = [entry for entry in await list_waiting(**scope(args)) if entry["id"] == request_id]
    assert waiting["card_kind"] == "desktop_takeover" and waiting["done_option"] == ANSWER_DONE
    assert waiting["assistant_may_answer"] is False and waiting["host"] == "login.example.com"
    async with get_db_session() as db:
        main = await db.get(Session, args["main_id"])
    ctx, lease, asked = await main_turn(owner, workspace, main, "你帮我点一下继续吧")
    try:
        for arguments in ({"answers": [[ANSWER_DONE]], "source_message_ids": [asked]},  # not finished
                          {"answers": [[ANSWER_DONE]]}):                                  # no citation
            refused, _ = await tool_call(ctx, "requests.answer", {"request_id": request_id, **arguments})
            assert refused["error"] == "ASSISTANT_ANSWER_HUMAN_ONLY", refused
        await finish_turn(ctx, lease)
    finally:
        await lease.release(session_status="idle")
    assert await card_status(request_id) == "pending"
    ctx, lease, done = await main_turn(owner, workspace, main, "我登好了，继续吧")
    try:
        other, _ = await tool_call(ctx, "requests.answer", {"request_id": request_id, "answers": [[ANSWER_SKIP]],
                                                            "source_message_ids": [done]})
        assert other["error"] == "ASSISTANT_ANSWER_HUMAN_ONLY"
        value, _ = await tool_call(ctx, "requests.answer", {"request_id": request_id, "answers": [[ANSWER_DONE]],
                                                            "source_message_ids": [done]})
        assert value["state"] == "answered" and value["answers"] == [[ANSWER_DONE]]
        await finish_turn(ctx, lease)
    finally:
        await lease.release(session_status="idle")
    async with get_db_session() as db:
        row = await db.get(QuestionCheckpoint, request_id)
        assert row.status == "answered" and row.answers == [[ANSWER_DONE]]
        assert [ref["message_id"] for ref in row.continuation["answered_by"]["source_refs"]] == [done]
    assert await apply_answers(session_id, owner) is not None
    async with get_db_session() as db:
        part = await db.get(Part, row.part_id)
    # The resumed tool goes on from the page the person changed, re-checking it first.
    assert part.data["title"] == "由个人助理代答" and part.data["metadata"]["answered_by"] == "assistant"
    assert "re-open client.page('login')" in part.data["output"]
    assert part.data["metadata"]["takeover"]["kind"] == "desktop_takeover"


async def test_a_scheduled_wake_never_acts_as_the_run_that_scheduled_it(monkeypatch):
    seen = []

    async def wake(session_id, user_id):
        seen.append((runtime.current_run.get(), runtime.auxiliary_run.get()))
    monkeypatch.setattr(inbox, "wake_inbox_session", wake)
    token = runtime.current_run.set(runtime.RunTicket("session", "user", 1, "run"))
    try:
        REAL_WAKE("session", "user")
    finally:
        runtime.current_run.reset(token)
    for _ in range(100):
        if seen:
            break
        await asyncio.sleep(0.01)
    assert seen == [(None, None)]
