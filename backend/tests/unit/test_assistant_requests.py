"""Real checkpoint/Command transactions, restart replay and authority races."""
import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from assistant.policy import AssistantError
from assistant.requests import list_requests
from assistant.retry import read_command
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.assistant import AssistantCommand, AssistantTask
from db.models.part import Part
from db.models.question import QuestionCheckpoint, SessionExecution
from db.models.workspace import WorkspaceMember
from models.message import ToolPartData
from question import question as q, runtime
from question.continuation import apply_answers
from session.agent_event_log import verify_agent_event_parity
from session.session import create_assistant_message, save_part, update_message_info
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_steering import running


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    from core.config import get_config
    monkeypatch.setattr(get_config(), "jwt_secret", "assistant-question-test-only")


async def pending(*, expires_at=None):
    args, created, lease, batch = await running()
    ticket = await runtime.start_run(lease.session_id, lease.user_id, driver_lease=lease)
    token = runtime.current_run.set(ticket)
    try:
        fence = (lease.session_id, lease.run_id, lease.generation)
        message = await create_assistant_message(lease.session_id, batch.messages[0].id, agent="build",
            model_id="test/model", user_id=lease.user_id, run_fence=fence)
        part = ToolPartData(session_id=lease.session_id, message_id=message.id, tool="question", status="running")
        await save_part(part, is_new=True, user_id=lease.user_id, run_fence=fence)
        with pytest.raises(q.QuestionSuspended) as suspended:
            options = {"question": "Choose a color?", "custom": False,
                "options": [{"label": "Blue"}, {"label": "Green"}]}
            if expires_at is not None:
                await q.ask(lease.session_id, [q.Question(**options)],
                    {"messageID": message.id, "callID": part.id}, lease.user_id, expires_at=expires_at)
            else:
                from tool.question_tool import execute, QuestionArgs
                from tool.tool import ToolContext
                await execute(QuestionArgs(questions=[options]), ToolContext(session_id=lease.session_id,
                    user_id=lease.user_id, workspace_id=args["workspace_id"], message_id=message.id, part_id=part.id))
        message.finish = "waiting_input"
        await update_message_info(message, user_id=lease.user_id, run_fence=fence)
        await runtime.finish_run(ticket)
    finally:
        runtime.current_run.reset(token)
        await lease.release(session_status="waiting_input")
    scope = {key: args[key] for key in ("user_id", "workspace_id", "main_id")}
    request = await q.get_request(suspended.value.request_id, lease.user_id)
    binding = {"reply_id": "reply-1", "expected_request_revision": request.assistant["request_revision"],
        "options_hash": request.assistant["options_hash"], "source_ref": {"kind": "card"}}
    return scope, created, request, binding


async def test_pending_reads_do_not_dispatch_and_draft_revision_does_not_change_request():
    scope, created, request, binding = await pending()
    first = await list_requests(**scope)
    assert [r["id"] for r in first["items"]] == [request.id]
    assert first["items"][0]["questions"][0]["custom"] is False
    with pytest.raises(ValueError, match="offered options"):
        await q.reply(request.id, [["Arbitrary option"]], scope["user_id"], **binding)
    saved = await q.save_draft(request.id, [q.DraftAnswer(selected=["Blue"])], 0, scope["user_id"])
    assert saved.draft_revision == 1 and saved.assistant == request.assistant
    assert (await list_requests(**scope))["items"][0]["draft_revision"] == 1
    async with get_db_session() as db:
        driver = await db.get(AgentDriverState, created["execution_session_id"])
        assert driver.generation == request.assistant["generation"] and driver.phase == "idle"
        assert await db.scalar(select(func.count()).select_from(AssistantCommand).where(
            AssistantCommand.actor_user_id == scope["user_id"])) == 1
    receipt = await q.reply(request.id, [["Blue"]], scope["user_id"], **binding)
    assert receipt["state"] == "accepted"


async def test_reply_replays_current_receipt_after_application_and_later_generation():
    scope, created, request, binding = await pending()
    first, duplicate = await asyncio.gather(*(q.reply(request.id, [["Blue"]], scope["user_id"], **binding) for _ in range(2)))
    assert first == duplicate and first["state"] == "accepted"
    assert await q.list_pending(scope["user_id"]) == []
    assert await apply_answers(request.session_id, scope["user_id"]) == request.generation
    assert await apply_answers(request.session_id, scope["user_id"]) == request.generation
    async with get_db_session() as db:
        row = await db.get(QuestionCheckpoint, request.id)
        assert row.applied
        part = await db.get(Part, request.tool["callID"])
        assert part.data["status"] == "completed"
        assert part.data["metadata"]["reply_ref"] == {"command_id": first["command_id"],
            "reply_id": binding["reply_id"], "request_id": request.id, "origin": "human_card"}
        (await db.get(AgentDriverState, request.session_id)).generation += 1
        (await db.get(SessionExecution, request.session_id)).generation += 1
        assert await db.scalar(select(func.count()).select_from(AssistantCommand).where(
            AssistantCommand.target_type == "question", AssistantCommand.target_id == request.id)) == 1
    replay = await q.reply(request.id, [["Blue"]], scope["user_id"], **binding)
    assert replay["command_id"] == first["command_id"] and replay["state"] == "applied"
    assert (await read_command(**scope, command_id=first["command_id"]))["receipt"] == replay
    # This fixture stops before the next provider run closes the logical turn.
    assert (await verify_agent_event_parity(request.session_id, user_id=scope["user_id"], require_closed=False)).ok


async def test_competing_devices_and_changed_reply_body_conflict():
    scope, _, request, binding = await pending()
    outcomes = await asyncio.gather(q.reply(request.id, [["Blue"]], scope["user_id"], **binding),
        q.reject(request.id, scope["user_id"], **{**binding, "reply_id": "device-2"}), return_exceptions=True)
    assert len([r for r in outcomes if isinstance(r, dict)]) == 1
    assert len([r for r in outcomes if isinstance(r, q.QuestionConflict)]) == 1
    winner = next(r for r in outcomes if isinstance(r, dict))
    with pytest.raises(q.QuestionConflict):
        await q.reply(request.id, [["Green"]], scope["user_id"], **{**binding, "reply_id": winner["reply_id"]})


@pytest.mark.parametrize("continuation", ["completed", "unfinished", "unrelated"])
async def test_question_turn_closes_only_when_its_continuation_finishes(continuation):
    from agent.driver import reserve_run
    from db.models.message import Message
    from session.session import create_user_message

    scope, _, request, binding = await pending()
    await q.reply(request.id, [["Blue"]], scope["user_id"], **binding)
    await apply_answers(request.session_id, scope["user_id"])
    before = await verify_agent_event_parity(request.session_id, user_id=scope["user_id"])
    assert before.projection_matches and before.open_turn_ids
    lease = await reserve_run(request.session_id, scope["user_id"])
    fence = (request.session_id, lease.run_id, lease.generation)
    try:
        async with get_db_session() as db:
            parent_id = (await db.get(Message, request.tool["messageID"])).parent_id
        if continuation == "unrelated":
            parent_id = (await create_user_message(request.session_id, "A different task",
                user_id=scope["user_id"], run_fence=fence)).id
        answer = await create_assistant_message(request.session_id, parent_id, agent="build",
            model_id="test/model", user_id=scope["user_id"], run_fence=fence)
        if continuation != "unfinished":
            answer.finish = "stop"
            await update_message_info(answer, user_id=scope["user_id"], run_fence=fence)
        report = await verify_agent_event_parity(request.session_id, user_id=scope["user_id"])
        assert report.projection_matches
        assert report.ok is (continuation == "completed"), report.model_dump()
        if continuation != "completed":
            assert before.open_turn_ids[0] in report.open_turn_ids
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("change", ["options", "expiry", "driver", "turn", "canceled", "hash", "revision"])
async def test_stale_cards_never_save_an_answer(change):
    scope, created, request, binding = await pending()
    async with get_db_session() as db:
        row = await db.get(QuestionCheckpoint, request.id)
        if change == "options": row.questions = [{**row.questions[0], "question": "A different question"}]
        if change == "expiry": row.expires_at = runtime.now() - timedelta(seconds=1)
        if change == "driver": (await db.get(AgentDriverState, request.session_id)).generation += 1
        if change == "turn": (await db.get(SessionExecution, request.session_id)).generation += 1
        if change == "canceled": (await db.get(AssistantTask, created["task_id"])).desired_state = "canceled"
    if change == "hash": binding["options_hash"] = "0" * 64
    if change == "revision": binding["expected_request_revision"] = "0" * 64
    with pytest.raises(q.QuestionGone):
        await q.reply(request.id, [["Blue"]], scope["user_id"], **binding)
    async with get_db_session() as db:
        assert (await db.get(QuestionCheckpoint, request.id)).answers is None
        assert not await db.scalar(select(AssistantCommand.id).where(
            AssistantCommand.target_type == "question", AssistantCommand.target_id == request.id))


@pytest.mark.parametrize("when", ["before_reply", "before_apply"])
async def test_removed_membership_cannot_read_replay_or_apply(when):
    scope, _, request, binding = await pending()
    if when == "before_apply":
        receipt = await q.reply(request.id, [["Blue"]], scope["user_id"], **binding)
    async with get_db_session() as db:
        (await db.get(WorkspaceMember, (scope["workspace_id"], scope["user_id"]))).status = "removed"
    assert await q.list_pending(scope["user_id"]) == []
    with pytest.raises(AssistantError):
        await q.get_request(request.id, scope["user_id"])
    with pytest.raises(AssistantError):
        await q.reply(request.id, [["Blue"]], scope["user_id"], **binding)
    if when == "before_apply":
        assert await apply_answers(request.session_id, scope["user_id"]) is None
        async with get_db_session() as db:
            assert (await db.get(AssistantCommand, receipt["command_id"])).state == "failed"
            assert (await db.get(Part, request.tool["callID"])).data["status"] == "error"


async def test_old_route_cannot_omit_versions_and_model_text_is_not_human_authority(monkeypatch):
    from api.questions import router
    from tests.unit.test_assistant_api import client_for
    scope, _, request, binding = await pending()
    async with client_for(scope["user_id"], scope["workspace_id"], monkeypatch) as client:
        client._transport.app.include_router(router, prefix="/api/agent")
        endpoint = f"/api/agent/question/{request.id}"
        assert (await client.post(endpoint, json={"answers": [["Blue"]]})).status_code == 409
        assert (await client.post(endpoint + "/reject")).status_code == 409
        assert (await client.post(endpoint, json={**binding, "answers": [["Blue"]],
            "source_ref": {"kind": "task_result"}})).status_code == 403
        accepted = await client.post(endpoint, json={**binding, "answers": [["Blue"]]})
        assert accepted.status_code == 200 and accepted.json()["state"] == "accepted"
        assert (await client.post(endpoint, json={**binding, "answers": [["Blue"]]})).json() == accepted.json()
        assert (await client.get("/api/assistant/requests")).json()["receipts"][0]["command_id"] == accepted.json()["command_id"]


async def test_pause_between_acceptance_and_application_keeps_answer_unapplied():
    scope, created, request, binding = await pending()
    receipt = await q.reply(request.id, [["Blue"]], scope["user_id"], **binding)
    async with get_db_session() as db:
        (await db.get(AssistantTask, created["task_id"])).desired_state = "paused"
    assert await apply_answers(request.session_id, scope["user_id"]) is None
    async with get_db_session() as db:
        assert not (await db.get(QuestionCheckpoint, request.id)).applied
        assert (await db.get(AssistantCommand, receipt["command_id"])).state == "accepted"
        assert (await db.get(SessionExecution, request.session_id)).resume_pending


async def test_replacement_marks_saved_reply_failed_and_retry_returns_that_receipt():
    scope, _, request, binding = await pending()
    await q.reply(request.id, [["Blue"]], scope["user_id"], **binding)
    async with runtime.transaction(request.session_id, scope["user_id"], fence=False) as (db, _, execution):
        await runtime.invalidate_locked(db, execution)
    receipt = await q.reply(request.id, [["Blue"]], scope["user_id"], **binding)
    assert receipt["state"] == "failed" and receipt["error_code"] == "QUESTION_GONE"
    assert await apply_answers(request.session_id, scope["user_id"]) is None


async def test_known_expired_question_is_gone_not_unknown():
    scope, _, request, binding = await pending(expires_at=runtime.now() - timedelta(seconds=1))
    with pytest.raises(q.QuestionGone) as gone:
        await q.reply(request.id, [["Blue"]], scope["user_id"], **binding)
    assert gone.value.status == "expired"
    assert (await list_requests(**scope))["items"] == []


async def test_request_events_recover_without_transport_and_contain_only_references():
    from assistant.events import event_cursor, project_task_events, read_events
    scope, created, request, binding = await pending()
    assert await project_task_events(created["task_id"]) > 0
    assert await project_task_events(created["task_id"]) == 0
    await q.reject(request.id, scope["user_id"], **binding)
    await apply_answers(request.session_id, scope["user_id"])
    assert await project_task_events(created["task_id"]) > 0
    page = await read_events(user_id=scope["user_id"], workspace_id=scope["workspace_id"],
        after=event_cursor(**scope, sequence=0), limit=200)
    changed = [e for e in page["events"] if e["kind"] == "assistant.request.changed"]
    assert len(changed) == 3
    assert all(e["request_id"] == request.id and e["request_kind"] == "question" for e in changed)
    assert "Blue" not in str(changed) and "questions" not in str(changed)
