"""The watch list is read fresh for each request; old snapshots are history.

V2 (PERSONAL_ASSISTANT_DESIGN_V2.md 3.2, 4.2, D1): every ordinary main request
gets an "assistant:current-tasks" block read from SQL. Earlier answers are not
re-validated when task facts change, and report turns receive no ambient list.
"""
from datetime import datetime, timezone
import json

import pytest

from assistant.commands import accept_task_command
from assistant.history import read_history
from assistant.projection import project_main_messages
from assistant.task_context import task_context
from db.base import get_db_session
from db.models.assistant import AssistantTask, TaskResult
from db.models.project import Project
from db.models.session import Session
from session.agent_event_log import load_canonical_model_surface
from tests.unit.assistant_source_fixtures import consume_context
from tests.unit.assistant_helpers import finish, next_turn
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_reads import read_turn
from tests.unit.assistant_helpers import prepare_report

WATCH_LIST = "assistant:current-tasks"


async def projected(ctx):
    surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
    return await project_main_messages(list(surface.messages), ctx=ctx)


def watch_list(messages):
    message = next(item for item in messages if item.id == WATCH_LIST)
    return json.loads(message.parts[0]["text"].split("\n", 1)[1])


async def test_each_provider_request_reads_current_progress_without_invalidating_history():
    ctx, lease, answer, accepted, _ = await read_turn()
    try:
        before = watch_list(await projected(ctx))
        [item] = before["items"]
        assert item["task_id"] == accepted["task_id"] and item["observed_state"] == "completed"
        assert item["latest_result"]["outcome"] == "succeeded"
        assert "Browser verification is still untested" in item["latest_result"]["summary"]
        assert before["untrusted_data"] and not before["grants_authority"]
        await accept_task_command(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
            idempotency_key="snapshot-followup", task_id=accepted["task_id"], expected_revision=item["revision"],
            prompt="Check the result again")
        current = watch_list(await projected(ctx))["items"][0]
        assert current["revision"] == item["revision"] + 1 and current["observed_state"] == "queued"
        assert current["latest_result"]["result_id"] == item["latest_result"]["result_id"]
        await consume_context(ctx)
        await finish(ctx, lease, answer, "A follow-up was accepted. The earlier result belongs to the earlier intent.")
        page = await read_history(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
            session_id=ctx.session_id, message_ids=[answer.id])
        assert "belongs to the earlier intent" in json.dumps(page)
    finally:
        await lease.release(session_status="idle")


async def test_task_context_is_bounded_prioritizes_waiting_and_filters_before_ranking(monkeypatch):
    ctx, lease, _, accepted, _ = await read_turn()
    try:
        receipts = [await accept_task_command(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
            main_id=ctx.session_id, project_id=ctx.project_id, idempotency_key=f"task-{i}",
            prompt=f"Test task {i}", title=f"VISIBLE-{i}") for i in range(4)]
        async with get_db_session() as db:
            waiting = await db.get(AssistantTask, accepted["task_id"])
            waiting.observed_state = "waiting_input"
            archived = await db.get(AssistantTask, receipts[0]["task_id"])
            archived.archived_at = datetime.now(timezone.utc)
            deleted = await db.get(Session, receipts[1]["execution_session_id"])
            deleted.is_deleted = True
        monkeypatch.setattr("assistant.task_context.MAX_CONTEXT_TASKS", 2)
        async with get_db_session() as db:
            value = await task_context(db, await db.get(Session, ctx.session_id))
        assert len(value["items"]) == 2 and value["has_more"]
        assert value["items"][0]["task_id"] == accepted["task_id"]
        assert "VISIBLE-0" not in json.dumps(value) and "VISIBLE-1" not in json.dumps(value)
        monkeypatch.setattr("assistant.task_context.MAX_CONTEXT_TASKS", 12)
        async with get_db_session() as db:
            value = await task_context(db, await db.get(Session, ctx.session_id))
        # Archived (unwatched) tasks and deleted sessions are filtered before the limit applies.
        assert {item["title"] for item in value["items"][1:]} == {"VISIBLE-2", "VISIBLE-3"}
        assert not value["has_more"]
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("change", ["project", "title", "result"])
async def test_saved_answer_stays_while_the_next_watch_list_shows_current_facts(change):
    ctx, lease, answer, accepted, _ = await read_turn()
    try:
        await consume_context(ctx)
        await finish(ctx, lease, answer, "Current SQL task facts were observed.")
        async with get_db_session() as db:
            task = await db.get(AssistantTask, accepted["task_id"])
            if change == "project":
                (await db.get(Project, task.project_id)).is_deleted = True
            elif change == "title":
                task.title = "Replaced task title"
            else:
                (await db.get(TaskResult, task.latest_result_id)).outcome = "failed"
        # D1: the earlier answer is not re-validated or rewritten.
        page = await read_history(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
            session_id=ctx.session_id, message_ids=[answer.id])
        assert "Current SQL task facts were observed." in json.dumps(page)
        ctx, lease, answer = await next_turn(ctx, "What is the current task state?")
        items = watch_list(await projected(ctx))["items"]
        if change == "project":
            assert items == []
        elif change == "title":
            assert [item["title"] for item in items] == ["Replaced task title"]
        else:
            assert [item["latest_result"]["outcome"] for item in items] == ["failed"]
    finally:
        await lease.release(session_status="idle")


async def test_report_only_never_receives_the_ambient_watch_list():
    ctx, lease, _, _, _, _ = await prepare_report()
    try:
        messages = await projected(ctx)
        assert ctx._assistant_context["mode"] == "report_only"
        assert WATCH_LIST not in {item.id for item in messages}
        assert "assistant:current-decisions" not in {item.id for item in messages}
        payload = await consume_context(ctx)
        assert "Current watch list" not in json.dumps(payload)
        async with get_db_session() as db:
            # The same SQL facts are still available to an ordinary request.
            assert (await task_context(db, await db.get(Session, ctx.session_id)))["items"]
    finally:
        await lease.release(session_status="idle")


async def test_watch_list_is_redacted_and_bounded():
    ctx, lease, _, accepted, _ = await read_turn()
    try:
        async with get_db_session() as db:
            task = await db.get(AssistantTask, accepted["task_id"])
            task.title = "Deploy with password=TOP_SECRET_TITLE"
            result = await db.get(TaskResult, task.latest_result_id)
            result.summary = "S" * 2000
        value = watch_list(await projected(ctx))
        [item] = value["items"]
        assert "TOP_SECRET_TITLE" not in json.dumps(value)
        assert len(item["latest_result"]["summary"]) == 601 and item["latest_result"]["summary"].endswith("…")
        assert item["session_id"] == accepted["execution_session_id"] and item["pending_questions"] == 0
    finally:
        await lease.release(session_status="idle")
