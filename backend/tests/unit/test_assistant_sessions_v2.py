"""V2 P2 session management on real SQL: list, watch, read, archive, rename, confirm shared sends.

docs/PERSONAL_ASSISTANT_DESIGN_V2.md section 6: the assistant works in any top-level
conversation the user owns; text sent into a workspace-visible one needs the user's
confirmation on a card first (D4); a subagent conversation belongs to its parent (D2).
"""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from itertools import count
import json
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from agent import inbox
from agent.driver import reserve_run
from assistant.commands import ToolSource, accept_task_command
from assistant.confirmations import CANCEL, CONFIRM, KIND, input_digest
from assistant.history import read_history
from assistant.linking import archive_task, candidate, link_existing
from assistant.policy import AssistantError
from assistant.reads import list_sessions, watch_list
from assistant.service import ensure_main_session
from assistant.session_tools import rename_session
from assistant.task_context import task_context
from core.config import get_config
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask, TaskResult
from db.models.file_asset import FileAsset
from db.models.part import Part
from db.models.question import QuestionCheckpoint, SessionExecution
from db.models.session import Session
from models.message import TextPart, ToolPartData, ToolStatus
from question import question as q
from session.session import create_assistant_message, create_session, save_part, update_message_info
from tests.unit.test_assistant_api import client_for
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tool.assistant_tools import assistant_tools
from tool.tool import ToolContext

TOOLS = {tool.id: tool for tool in assistant_tools}
CALLS = count(1)
MAIN_MESSAGES = {}


@pytest.fixture(autouse=True)
def quiet_runtime(monkeypatch):
    monkeypatch.setattr(get_config(), "jwt_secret", "assistant-sessions-v2-test-only")
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)
    MAIN_MESSAGES.clear()


async def assistant():
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    return owner, other, workspace, main


async def conversation(owner, workspace, main, title, **options):
    """An ordinary chat the user created; the default audience is the workspace."""
    return await create_session(user_id=owner, workspace_id=workspace, project_id=main.project_id,
                                model="test/model", title=title, **options)


async def watch(owner, workspace, main, session, key="watch"):
    async with get_db_session() as db:
        version = (await candidate(db, await db.get(Session, session.id)))["version"]
    return await link_existing(user_id=owner, workspace_id=workspace, main_id=main.id,
        session_id=session.id, expected_version=version, idempotency_key=key)


async def run_once(session_id, owner, text):
    """One inbox-driven execution turn of a conversation that ends with `text`."""
    lease = await reserve_run(session_id, owner)
    try:
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        fence = (session_id, lease.run_id, lease.generation)
        message = await create_assistant_message(session_id, batch.messages[0].id, model_id="test/model",
            agent="build", user_id=owner, run_fence=fence)
        await save_part(TextPart(session_id=session_id, message_id=message.id, text=text),
                        is_new=True, user_id=owner, run_fence=fence)
        message.finish = "stop"
        await update_message_info(message, user_id=owner, run_fence=fence)
        await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
    finally:
        await lease.release(session_status="idle")
    return message


async def human_turn(session_id, owner, text, reply):
    await inbox.accept_inbox_item(session_id=session_id, user_id=owner, delivery="followup", prompt=text,
        origin="human", origin_ref={"actor_user_id": owner})
    return await run_once(session_id, owner, reply)


async def main_turn(owner, workspace, main, prompt, **item):
    """A claimed human turn of the main session; returns its tool context and human message ID."""
    origin_ref = {"actor_user_id": owner, **item.pop("origin_ref", {})}
    await inbox.accept_inbox_item(session_id=main.id, user_id=owner, delivery="followup", prompt=prompt,
        origin="human", origin_ref=origin_ref, **item)
    lease = await reserve_run(main.id, owner)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    message = await create_assistant_message(main.id, batch.messages[0].id, model_id="test/model",
        agent="assistant", user_id=owner, run_fence=(main.id, lease.run_id, lease.generation))
    ctx = ToolContext(user_id=owner, workspace_id=workspace, session_id=main.id, project_id=main.project_id,
        agent_id="assistant", run_id=lease.run_id, run_generation=lease.generation, message_id=message.id)
    MAIN_MESSAGES[message.id] = message
    return ctx, lease, batch.messages[0].id


async def finish_turn(ctx, lease, text="Done."):
    """End the main turn like the processor: a final answer and a settled input."""
    message = MAIN_MESSAGES.pop(ctx.message_id)
    await save_part(TextPart(session_id=ctx.session_id, message_id=message.id, text=text),
                    is_new=True, user_id=ctx.user_id, run_fence=ctx.run_fence)
    message.finish = "stop"
    await update_message_info(message, user_id=ctx.user_id, run_fence=ctx.run_fence)
    await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")


async def tool_call(ctx, operation, arguments):
    """Persist the provider's call like the processor, then run the real tool.

    A confirmation card suspends the call: the result is {"suspended": request_id}.
    """
    index = next(CALLS)
    part = ToolPartData(tool=operation, canonical_tool_id=operation, call_id=f"call-{index}",
        wire_tool_name=operation.replace(".", "_"), provider_binding_digest="d" * 64, provider_dialect="openai",
        stream_seq=index, status=ToolStatus.RUNNING, input=arguments, session_id=ctx.session_id,
        message_id=ctx.message_id)
    await save_part(part, is_new=True, user_id=ctx.user_id, run_fence=ctx.run_fence)
    try:
        result = await TOOLS[operation].execute(arguments, replace(ctx, part_id=part.id))
    except q.QuestionSuspended as suspended:
        return {"suspended": suspended.request_id}, part
    part.status = ToolStatus.ERROR if result.metadata.get("error") else ToolStatus.COMPLETED
    part.output, part.metadata = result.output, result.metadata
    await save_part(part, user_id=ctx.user_id, run_fence=ctx.run_fence)
    return json.loads(result.output), part


async def inputs(session_id):
    async with get_db_session() as db:
        return list((await db.scalars(select(AgentInboxItem).where(AgentInboxItem.session_id == session_id)
                                      .order_by(AgentInboxItem.created_at, AgentInboxItem.id))).all())


async def card(request_id):
    async with get_db_session() as db:
        return await db.get(QuestionCheckpoint, request_id)


# 1. sessions.list -----------------------------------------------------------

async def test_sessions_list_covers_every_owned_top_level_conversation():
    owner, other, workspace, main = await assistant()
    shared = await conversation(owner, workspace, main, "Shared snake UI")
    private = await conversation(owner, workspace, main, "Private notes", visibility="private")
    isolated = await conversation(owner, workspace, main, "Isolated draft", visibility="private",
                                  memory_policy="assistant_isolated")
    await create_session(user_id=owner, workspace_id=workspace, project_id=main.project_id, model="test/model",
                         title="Subagent of shared", parent_id=shared.id)
    deleted = await conversation(owner, workspace, main, "Deleted chat")
    await conversation(owner, workspace, main, "Nightly schedule", kind="cron")
    await create_session(user_id=other, workspace_id=workspace, model="test/model", title="Member's own chat")
    async with get_db_session() as db:
        (await db.get(Session, deleted.id)).is_deleted = True
    page = await list_sessions(user_id=owner, workspace_id=workspace, main_id=main.id)
    assert {item["id"] for item in page["items"]} == {shared.id, private.id, isolated.id}
    entry = next(item for item in page["items"] if item["id"] == shared.id)
    assert entry["visibility"] == "workspace" and entry["project_id"] == main.project_id
    assert entry["project_name"] and entry["watched"] is False
    assert entry["task_id"] is None and entry["latest_summary"] is None
    assert {item["visibility"] for item in page["items"]} == {"workspace", "private"}


async def test_sessions_list_pages_with_a_stable_cursor():
    owner, _, workspace, main = await assistant()
    created = [(await conversation(owner, workspace, main, f"Conversation {index}")).id for index in range(5)]
    everything = [item["id"] for item in (await list_sessions(user_id=owner, workspace_id=workspace,
                                                               main_id=main.id))["items"]]
    assert sorted(everything) == sorted(created)
    paged, cursor = [], None
    while True:
        page = await list_sessions(user_id=owner, workspace_id=workspace, main_id=main.id, limit=2, cursor=cursor)
        assert len(page["items"]) <= 2
        paged += [item["id"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert paged == everything


async def test_sessions_list_shows_the_newest_conversation_first():
    owner, _, workspace, main = await assistant()
    titles = []
    for index in range(3):
        titles.append((await conversation(owner, workspace, main, f"Chat {index}")).title)
        await asyncio.sleep(0.01)  # Distinct creation milliseconds.
    first = await list_sessions(user_id=owner, workspace_id=workspace, main_id=main.id, limit=1)
    assert first["items"][0]["title"] == "Chat 2"
    everything = await list_sessions(user_id=owner, workspace_id=workspace, main_id=main.id)
    assert [item["title"] for item in everything["items"]] == ["Chat 2", "Chat 1", "Chat 0"]


async def test_sessions_list_title_query_is_a_literal_case_insensitive_substring():
    owner, _, workspace, main = await assistant()
    for title in ("Snake UI dark mode", "snake ui notes", "100% done", "1000 tasks", "a_b plan", "axb plan",
                  "贪吃蛇界面", "back\\slash"):
        await conversation(owner, workspace, main, title)

    async def titles(query):
        page = await list_sessions(user_id=owner, workspace_id=workspace, main_id=main.id, query=query)
        return sorted(item["title"] for item in page["items"])

    assert await titles("SNAKE UI") == ["Snake UI dark mode", "snake ui notes"]
    assert await titles("100%") == ["100% done"]
    assert await titles("%") == ["100% done"]
    assert await titles("a_b") == ["a_b plan"]
    assert await titles("_") == ["a_b plan"]
    assert await titles("贪吃蛇") == ["贪吃蛇界面"]
    assert await titles("back\\") == ["back\\slash"]
    assert await titles("no such title") == []


async def test_sessions_list_watched_filter_and_latest_summary():
    owner, _, workspace, main = await assistant()
    followed = await conversation(owner, workspace, main, "Followed chat")
    plain = await conversation(owner, workspace, main, "Plain chat")
    unfollowed = await conversation(owner, workspace, main, "Unfollowed chat")
    linked = await watch(owner, workspace, main, followed)
    dropped = await watch(owner, workspace, main, unfollowed, key="watch-dropped")
    await archive_task(user_id=owner, workspace_id=workspace, main_id=main.id, task_id=dropped["task_id"],
        expected_revision=dropped["task_revision"], idempotency_key="archive-dropped")
    await accept_task_command(user_id=owner, workspace_id=workspace, main_id=main.id, task_id=linked["task_id"],
        idempotency_key="followed-input", prompt="Summarize the dark theme work.",
        expected_revision=linked["task_revision"])
    report = "The dark theme is applied to the board, the score and the menus. " * 8
    await run_once(followed.id, owner, report)
    scope = dict(user_id=owner, workspace_id=workspace, main_id=main.id)
    watched = (await list_sessions(**scope, watched=True))["items"]
    assert [item["id"] for item in watched] == [followed.id]
    assert watched[0]["watched"] is True and watched[0]["task_id"] == linked["task_id"]
    summary = watched[0]["latest_summary"]
    assert summary.startswith("The dark theme is applied") and summary.endswith("…") and len(summary) == 301
    others = {item["id"]: item for item in (await list_sessions(**scope, watched=False))["items"]}
    assert set(others) == {plain.id, unfollowed.id}
    # An archived Task keeps its identity but is no longer watched.
    assert others[unfollowed.id]["task_id"] == dropped["task_id"] and others[unfollowed.id]["watched"] is False
    assert others[plain.id]["task_id"] is None


async def test_sessions_list_tool_passes_query_and_watched_with_link_details():
    owner, _, workspace, main = await assistant()
    target = await conversation(owner, workspace, main, "Snake UI v2")
    await conversation(owner, workspace, main, "Unrelated chat")
    ctx, lease, _ = await main_turn(owner, workspace, main, "Find my snake UI chat.")
    try:
        value, _ = await tool_call(ctx, "sessions.list", {"query": "snake", "watched": False})
    finally:
        await lease.release(session_status="idle")
    assert [item["id"] for item in value["items"]] == [target.id]
    assert value["items"][0]["link"]["available"] is True and len(value["items"][0]["link"]["version"]) == 64


# 2. tasks.link_existing -----------------------------------------------------

async def test_link_watches_a_workspace_visible_standard_conversation_and_records_its_results():
    owner, _, workspace, main = await assistant()
    shared = await conversation(owner, workspace, main, "Team snake game")
    await human_turn(shared.id, owner, "Build a snake game.", "A first version of the game is ready.")
    linked = await watch(owner, workspace, main, shared)
    assert linked["created"] and linked["execution_session_id"] == shared.id and linked["adopted_inputs"] == 0
    accepted = await accept_task_command(user_id=owner, workspace_id=workspace, main_id=main.id,
        task_id=linked["task_id"], idempotency_key="dark-theme", prompt="Switch the UI to a dark theme.",
        expected_revision=linked["task_revision"])
    assert accepted["execution_session_id"] == shared.id
    await run_once(shared.id, owner, "The UI now uses a dark theme.")
    async with get_db_session() as db:
        row = await db.get(Session, shared.id)
        assert (row.visibility, row.memory_policy, row.parent_id) == ("workspace", "standard", None)
        task = await db.get(AssistantTask, linked["task_id"])
        result = await db.scalar(select(TaskResult).where(TaskResult.task_id == task.id))
        assert result is not None and task.latest_result_id == result.id
        assert result.outcome == "succeeded" and result.summary == "The UI now uses a dark theme."
        assert result.consumed_inbox_ids == [accepted["inbox_id"]]
        assert (await db.get(AgentInboxItem, accepted["inbox_id"])).state == "settled"
        # The earlier, unwatched turn produced no result.
        assert await db.scalar(select(func.count()).select_from(TaskResult).where(TaskResult.task_id == task.id)) == 1
    context = await watch_list(user_id=owner, workspace_id=workspace)
    item = next(entry for entry in context["items"] if entry["task_id"] == linked["task_id"])
    assert item["latest_result"]["summary"] == "The UI now uses a dark theme."


async def test_link_refuses_a_subagent_conversation():
    owner, _, workspace, main = await assistant()
    parent = await conversation(owner, workspace, main, "Parent chat")
    child = await create_session(user_id=owner, workspace_id=workspace, project_id=main.project_id,
                                 model="test/model", title="Subagent", parent_id=parent.id)
    async with get_db_session() as db:
        inspected = await candidate(db, await db.get(Session, child.id))
    assert inspected["available"] is False and inspected["reason_code"] == "ASSISTANT_LINK_CHILD"
    with pytest.raises(AssistantError) as refused:
        await watch(owner, workspace, main, child)
    assert (refused.value.status, refused.value.code) == (409, "ASSISTANT_LINK_CHILD")
    async with get_db_session() as db:
        assert await db.scalar(select(AssistantTask.id).where(AssistantTask.execution_session_id == child.id)) is None
        assert await db.scalar(select(AssistantCommand.id).where(AssistantCommand.actor_user_id == owner)) is None


# 3. history.read ------------------------------------------------------------

async def test_history_reads_an_unlinked_owned_conversation_and_refuses_others():
    owner, other, workspace, main = await assistant()
    mine = await conversation(owner, workspace, main, "Last week's snake UI")
    await human_turn(mine.id, owner, "Make the snake green.", "The snake is green now.")
    foreign = await create_session(user_id=other, workspace_id=workspace, model="test/model",
                                   title="Member's shared chat")
    await human_turn(foreign.id, other, "PRIVATE_MEMBER_TEXT", "Member reply.")
    child = await create_session(user_id=owner, workspace_id=workspace, project_id=main.project_id,
                                 model="test/model", title="Subagent", parent_id=mine.id)
    deleted = await conversation(owner, workspace, main, "Deleted chat")
    async with get_db_session() as db:
        (await db.get(Session, deleted.id)).is_deleted = True
    scope = dict(user_id=owner, workspace_id=workspace, main_id=main.id)
    page = await read_history(**scope, session_id=mine.id)
    text = json.dumps(page, ensure_ascii=False)
    assert "Make the snake green." in text and "The snake is green now." in text
    for target in (foreign, child, deleted):
        with pytest.raises(AssistantError) as refused:
            await read_history(**scope, session_id=target.id)
        assert (refused.value.status, refused.value.code) == (404, "ASSISTANT_HISTORY_UNAVAILABLE")
    ctx, lease, _ = await main_turn(owner, workspace, main, "What did I ask in last week's snake chat?")
    try:
        value, part = await tool_call(ctx, "history.read", {"session_id": mine.id})
        assert part.status == ToolStatus.COMPLETED and "Make the snake green." in json.dumps(value, ensure_ascii=False)
        refused, part = await tool_call(ctx, "history.read", {"session_id": foreign.id})
        assert part.status == ToolStatus.ERROR and refused["error"] == "ASSISTANT_HISTORY_UNAVAILABLE"
        assert "PRIVATE_MEMBER_TEXT" not in json.dumps(refused, ensure_ascii=False)
    finally:
        await lease.release(session_status="idle")


# 4. tasks.archive -----------------------------------------------------------

async def test_archive_stops_watching_records_no_result_and_followup_watches_again():
    owner, _, workspace, main = await assistant()
    created = await accept_task_command(user_id=owner, workspace_id=workspace, main_id=main.id,
        project_id=main.project_id, idempotency_key="create", prompt="Draft a short text report.", title="Report")
    task_id, execution_id = created["task_id"], created["execution_session_id"]
    scope = dict(user_id=owner, workspace_id=workspace, main_id=main.id, task_id=task_id)
    with pytest.raises(AssistantError) as stale:
        await archive_task(**scope, expected_revision=created["task_revision"] + 1, idempotency_key="stale")
    assert (stale.value.status, stale.value.code) == (409, "ASSISTANT_TASK_REVISION")
    receipt = await archive_task(**scope, expected_revision=created["task_revision"], idempotency_key="archive")
    assert receipt["state"] == "archived" and receipt["task_revision"] == created["task_revision"] + 1
    assert await archive_task(**scope, expected_revision=created["task_revision"], idempotency_key="archive") == receipt
    with pytest.raises(AssistantError) as reused:
        await archive_task(**scope, expected_revision=receipt["task_revision"], idempotency_key="archive")
    assert reused.value.code == "ASSISTANT_COMMAND_CONFLICT"
    async with get_db_session() as db:
        task = await db.get(AssistantTask, task_id)
        assert task.archived_at is not None and task.desired_state == "running"
        assert task_id not in [item["task_id"] for item in (await task_context(db, await db.get(Session, main.id)))["items"]]
        event = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == main.id,
            AgentEvent.kind == "assistant.task.changed").order_by(AgentEvent.sequence.desc()).limit(1))
        assert {key: event.payload.get(key) for key in ("task_id", "command_id", "task_revision")} == {
            "task_id": task_id, "command_id": receipt["command_id"], "task_revision": receipt["task_revision"]}
    assert task_id not in [item["task_id"] for item in (await watch_list(user_id=owner, workspace_id=workspace))["items"]]
    # The already queued input still runs in its conversation, but nothing is recorded or reported.
    await run_once(execution_id, owner, "Report drafted.")
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(TaskResult).where(TaskResult.task_id == task_id)) == 0
        assert (await db.get(AssistantTask, task_id)).latest_result_id is None
        assert (await db.get(AgentInboxItem, created["inbox_id"])).state == "settled"
        revision = (await db.get(AssistantTask, task_id)).control_revision
    again = await accept_task_command(user_id=owner, workspace_id=workspace, main_id=main.id, task_id=task_id,
        idempotency_key="again", prompt="Add one summary line.", expected_revision=revision)
    async with get_db_session() as db:
        assert (await db.get(AssistantTask, task_id)).archived_at is None
    assert task_id in [item["task_id"] for item in (await watch_list(user_id=owner, workspace_id=workspace))["items"]]
    await run_once(execution_id, owner, "Summary line added.")
    async with get_db_session() as db:
        result = await db.scalar(select(TaskResult).where(TaskResult.task_id == task_id))
        assert result.summary == "Summary line added." and result.consumed_inbox_ids == [again["inbox_id"]]


async def test_archived_task_input_from_its_conversation_is_ordinary_input():
    from assistant.inputs import accept_session_input
    owner, _, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "Watched chat")
    linked = await watch(owner, workspace, main, session)
    async with get_db_session() as db:
        row = await db.get(Session, session.id)
    watched = await accept_session_input(row, user_id=owner, text="While watched", client_id="watched-input")
    async with get_db_session() as db:
        assert (await db.get(AgentInboxItem, watched.id)).origin_ref["task_id"] == linked["task_id"]
        revision = (await db.get(AssistantTask, linked["task_id"])).control_revision
    await archive_task(user_id=owner, workspace_id=workspace, main_id=main.id, task_id=linked["task_id"],
        expected_revision=revision, idempotency_key="archive")
    # Unwatched again: the conversation's own input path takes it, not a Task command.
    assert await accept_session_input(row, user_id=owner, text="After archive", client_id="ordinary-input") is None
    async with get_db_session() as db:
        assert (await db.get(AssistantTask, linked["task_id"])).control_revision == revision + 1


async def test_http_archive_is_idempotent_per_key_and_owner_scoped(monkeypatch):
    owner, other, workspace, main = await assistant()
    created = await accept_task_command(user_id=owner, workspace_id=workspace, main_id=main.id,
        project_id=main.project_id, idempotency_key="create", prompt="Draft a short text report.", title="Report")
    url = f"/api/assistant/tasks/{created['task_id']}/archive"
    body = {"idempotency_key": "http-archive", "expected_revision": created["task_revision"]}
    await ensure_main_session(user_id=other, workspace_id=workspace)
    async with client_for(other, workspace, monkeypatch) as client:
        assert (await client.post(url, json=body)).status_code == 404
    async with client_for(owner, workspace, monkeypatch) as client:
        first, second = await asyncio.gather(client.post(url, json=body), client.post(url, json=body))
        assert first.status_code == second.status_code == 200 and first.json() == second.json()
        assert first.json()["state"] == "archived" and first.json()["task_id"] == created["task_id"]
        assert (await client.post(url, json=body)).json() == first.json()
        stale = await client.post(url, json={**body, "idempotency_key": "http-archive-2"})
        assert stale.status_code == 409 and stale.json()["detail"]["code"] == "ASSISTANT_TASK_REVISION"
        reused = await client.post(url, json={**body, "expected_revision": first.json()["task_revision"]})
        assert reused.status_code == 409 and reused.json()["detail"]["code"] == "ASSISTANT_COMMAND_CONFLICT"
        assert (await client.post(url, json={**body, "expected_revision": 0})).status_code == 422
        assert (await client.get("/api/assistant/watch")).json()["items"] == []
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(AssistantCommand).where(
            AssistantCommand.actor_user_id == owner, AssistantCommand.action == "task_archive")) == 1


async def test_archive_also_withholds_a_result_recorded_before_archiving():
    from assistant.results import deliver_task_result
    owner, _, workspace, main = await assistant()
    created = await accept_task_command(user_id=owner, workspace_id=workspace, main_id=main.id,
        project_id=main.project_id, idempotency_key="create", prompt="Draft a short text report.", title="Report")
    await run_once(created["execution_session_id"], owner, "Report drafted.")
    async with get_db_session() as db:
        task = await db.get(AssistantTask, created["task_id"])
        result_id, revision = task.latest_result_id, task.control_revision
    await archive_task(user_id=owner, workspace_id=workspace, main_id=main.id, task_id=created["task_id"],
        expected_revision=revision, idempotency_key="archive")
    assert await deliver_task_result(result_id) is None
    assert [item for item in await inputs(main.id) if item.origin == "task_result"] == []


# 5. sessions.rename ---------------------------------------------------------

async def test_rename_session_changes_only_an_owned_top_level_title():
    owner, other, workspace, main = await assistant()
    mine = await conversation(owner, workspace, main, "Old title")
    foreign = await create_session(user_id=other, workspace_id=workspace, model="test/model", title="Member title")
    child = await create_session(user_id=owner, workspace_id=workspace, project_id=main.project_id,
                                 model="test/model", title="Child title", parent_id=mine.id)
    scope = dict(user_id=owner, workspace_id=workspace, main_id=main.id)
    receipt = await rename_session(**scope, session_id=mine.id, title="  贪吃蛇 dark UI  ")
    assert receipt == {"session_id": mine.id, "title": "贪吃蛇 dark UI", "state": "renamed"}
    for target in (foreign, child, main):
        with pytest.raises(AssistantError) as refused:
            await rename_session(**scope, session_id=target.id, title="Hijacked")
        assert (refused.value.status, refused.value.code) == (404, "ASSISTANT_SESSION_UNAVAILABLE")
    for title in ("   ", "x" * 129):
        with pytest.raises(ValueError):
            await rename_session(**scope, session_id=mine.id, title=title)
    async with get_db_session() as db:
        titles = {row.id: row.title for row in (await db.scalars(select(Session).where(
            Session.id.in_([mine.id, foreign.id, child.id])))).all()}
    assert titles == {mine.id: "贪吃蛇 dark UI", foreign.id: "Member title", child.id: "Child title"}


async def test_rename_tool_requires_its_persisted_call_and_original_human_message():
    owner, _, workspace, main = await assistant()
    mine = await conversation(owner, workspace, main, "Snake chat")
    ctx, lease, human = await main_turn(owner, workspace, main, "Rename my snake chat to Snake v2.")
    scope = dict(user_id=owner, workspace_id=workspace, main_id=main.id, session_id=mine.id, title="Forged")
    try:
        with pytest.raises(AssistantError) as forged:
            await rename_session(**scope, source=ToolSource("missing-part", ctx.run_id, ctx.run_generation, (human,)))
        assert forged.value.code == "ASSISTANT_CALL_UNVERIFIED"
        _, other_tool = await tool_call(ctx, "tasks.list", {})
        with pytest.raises(AssistantError) as wrong_tool:
            await rename_session(**scope, source=ToolSource(other_tool.id, ctx.run_id, ctx.run_generation, (human,)))
        assert wrong_tool.value.code == "ASSISTANT_CALL_UNVERIFIED"
        not_human, part = await tool_call(ctx, "sessions.rename", {"session_id": mine.id, "title": "Not human",
                                                                  "source_message_ids": [ctx.message_id]})
        assert part.status == ToolStatus.ERROR and not_human["error"] == "ASSISTANT_SOURCE_UNVERIFIED"
        renamed, part = await tool_call(ctx, "sessions.rename", {"session_id": mine.id, "title": "Snake v2",
                                                                "source_message_ids": [human]})
        assert part.status == ToolStatus.COMPLETED and renamed == {"session_id": mine.id, "title": "Snake v2",
                                                                    "state": "renamed"}
    finally:
        await lease.release(session_status="idle")
    async with get_db_session() as db:
        assert (await db.get(Session, mine.id)).title == "Snake v2"


# 6. Confirmation before writing into a workspace-visible conversation (D4) ---

async def watched_conversation(owner, workspace, main, *, visibility="workspace", title="Team snake game"):
    session = await conversation(owner, workspace, main, title, visibility=visibility)
    return session, await watch(owner, workspace, main, session)


def followup(task_id, revision, text, human, **extra):
    return {"task_id": task_id, "text": text, "expected_revision": revision, "source_message_ids": [human], **extra}


async def test_followup_into_a_private_conversation_needs_no_card():
    owner, _, workspace, main = await assistant()
    session, linked = await watched_conversation(owner, workspace, main, visibility="private", title="Private chat")
    ctx, lease, human = await main_turn(owner, workspace, main, "Tell my private chat to use a dark theme.")
    try:
        sent, part = await tool_call(ctx, "tasks.followup", followup(linked["task_id"], linked["task_revision"],
                                                                     "Use a dark theme.", human))
    finally:
        await lease.release(session_status="idle")
    assert part.status == ToolStatus.COMPLETED and sent["state"] == "accepted"
    rows = await inputs(session.id)
    assert [(row.prompt, row.origin) for row in rows] == [("Use a dark theme.", "assistant_delegation")]
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(QuestionCheckpoint).where(
            QuestionCheckpoint.session_id == main.id)) == 0


async def test_followup_into_a_workspace_visible_conversation_sends_only_the_confirmed_text_once():
    owner, _, workspace, main = await assistant()
    session, linked = await watched_conversation(owner, workspace, main)
    text = "Please switch the snake UI to a dark theme."
    ctx, lease, human = await main_turn(owner, workspace, main, "Ask the team snake chat to switch to dark.")
    try:
        args = followup(linked["task_id"], linked["task_revision"], text, human)
        asked, part = await tool_call(ctx, "tasks.followup", args)
        request = await card(asked["suspended"])
        assert (request.session_id, request.status, request.part_id) == (main.id, "pending", part.id)
        assert text in request.questions[0]["question"] and "Team snake game" in request.questions[0]["question"]
        assert [option["label"] for option in request.questions[0]["options"]] == [CONFIRM, CANCEL]
        assert request.continuation["kind"] == "question"
        assert request.continuation[KIND] == {"task_id": linked["task_id"], "digest": input_digest(linked["task_id"], text),
                                              "confirm": CONFIRM}
        async with get_db_session() as db:
            assert (await db.get(Part, part.id)).data["status"] == "waiting_input"
            assert (await db.get(AssistantTask, linked["task_id"])).control_revision == linked["task_revision"]
            assert await db.scalar(select(AssistantCommand.id).where(AssistantCommand.actor_user_id == owner,
                AssistantCommand.action == "task_input")) is None
        assert await inputs(session.id) == []
        await q.reply(request.id, [[CONFIRM]], owner)
        sent, part = await tool_call(ctx, "tasks.followup", args)
        assert part.status == ToolStatus.COMPLETED and sent["state"] == "accepted"
        assert [(row.prompt, row.origin) for row in await inputs(session.id)] == [(text, "assistant_delegation")]
        assert (await card(request.id)).continuation[KIND]["consumed"] is True
        again, _ = await tool_call(ctx, "tasks.followup", {**args, "expected_revision": sent["task_revision"]})
        assert "suspended" in again and again["suspended"] != request.id
        assert len(await inputs(session.id)) == 1
    finally:
        await lease.release(session_status="idle")


async def test_an_answered_card_resumes_the_main_session_through_its_inbox(monkeypatch):
    """Every main turn binds one claimed input (assistant.budget): no raw resumed run."""
    from question.continuation import QuestionContinuationWorker
    woken = []
    monkeypatch.setattr(inbox, "schedule_inbox_wake", lambda *args: woken.append(args))
    owner, _, workspace, main = await assistant()
    session, linked = await watched_conversation(owner, workspace, main)
    ctx, lease, human = await main_turn(owner, workspace, main, "Ask the team snake chat to switch to dark.")
    try:
        args = followup(linked["task_id"], linked["task_revision"], "Please switch to a dark theme.", human)
        asked, part = await tool_call(ctx, "tasks.followup", args)
    finally:
        await lease.release(session_status="waiting_input")
    await q.reply(asked["suspended"], [[CONFIRM]], owner)
    async def raw_run(*_args, **_kwargs):
        raise AssertionError("The main session must not resume with a raw run")
    worker = QuestionContinuationWorker()
    monkeypatch.setattr(worker, "_resume", raw_run)
    async with get_db_session() as db:
        generation = await db.scalar(select(QuestionCheckpoint.generation).where(
            QuestionCheckpoint.id == asked["suspended"]))
    await worker._resume_candidate(main.id, owner, generation)
    assert not worker.runs
    async with get_db_session() as db:
        resumed = list((await db.scalars(select(AgentInboxItem).where(AgentInboxItem.session_id == main.id,
            AgentInboxItem.origin == "system_recovery"))).all())
        execution = await db.get(SessionExecution, main.id)
        answered = await db.get(Part, part.id)
    assert [(row.state, row.origin_ref["entrypoint"]) for row in resumed] == [("accepted", "question_answer")]
    assert execution.resume_pending is False and woken == [(main.id, owner)]
    assert answered.data["status"] == "completed" and answered.data["metadata"]["confirmation"] == "confirmed"
    assert "call the same tool again" in answered.data["output"]


async def test_a_card_answered_in_a_call_resumes_the_voice_turn_on_its_own_model(monkeypatch):
    """A voice turn runs on the faster voice model; after its card it goes on with it (and so does the next card)."""
    from question.continuation import QuestionContinuationWorker
    monkeypatch.setattr(inbox, "schedule_inbox_wake", lambda *args: None)
    owner, _, workspace, main = await assistant()
    session, linked = await watched_conversation(owner, workspace, main)
    ctx, lease, human = await main_turn(owner, workspace, main, "Ask the team snake chat to switch to dark.",
        model="openai/qwen3.8-flash", variant="low", origin_ref={"entrypoint": "assistant_voice"})
    try:
        args = followup(linked["task_id"], linked["task_revision"], "Please switch to a dark theme.", human)
        asked, _ = await tool_call(ctx, "tasks.followup", args)
    finally:
        await lease.release(session_status="waiting_input")
    await q.reply(asked["suspended"], [[CONFIRM]], owner)
    worker = QuestionContinuationWorker()
    async with get_db_session() as db:
        generation = await db.scalar(select(QuestionCheckpoint.generation).where(
            QuestionCheckpoint.id == asked["suspended"]))
    await worker._resume_candidate(main.id, owner, generation)
    async with get_db_session() as db:
        [resumed] = list((await db.scalars(select(AgentInboxItem).where(AgentInboxItem.session_id == main.id,
            AgentInboxItem.origin == "system_recovery"))).all())
    assert (resumed.model, resumed.variant) == ("openai/qwen3.8-flash", "low")
    assert resumed.origin_ref["entrypoint"] == "question_answer" and resumed.origin_ref["voice"] is True


async def test_a_card_answered_in_a_typed_turn_resumes_on_the_session_model(monkeypatch):
    from question.continuation import QuestionContinuationWorker
    monkeypatch.setattr(inbox, "schedule_inbox_wake", lambda *args: None)
    owner, _, workspace, main = await assistant()
    session, linked = await watched_conversation(owner, workspace, main)
    ctx, lease, human = await main_turn(owner, workspace, main, "Ask the team snake chat to switch to dark.",
        model="openai/qwen3.8-flash", variant="low")
    try:
        args = followup(linked["task_id"], linked["task_revision"], "Please switch to a dark theme.", human)
        asked, _ = await tool_call(ctx, "tasks.followup", args)
    finally:
        await lease.release(session_status="waiting_input")
    await q.reply(asked["suspended"], [[CONFIRM]], owner)
    async with get_db_session() as db:
        generation = await db.scalar(select(QuestionCheckpoint.generation).where(
            QuestionCheckpoint.id == asked["suspended"]))
        saved = await db.get(Session, main.id)
    await QuestionContinuationWorker()._resume_candidate(main.id, owner, generation)
    async with get_db_session() as db:
        [resumed] = list((await db.scalars(select(AgentInboxItem).where(AgentInboxItem.session_id == main.id,
            AgentInboxItem.origin == "system_recovery"))).all())
    assert (resumed.model, resumed.variant) == (saved.model, saved.variant)
    assert "voice" not in resumed.origin_ref


async def test_cancel_never_sends_and_a_confirmation_covers_only_its_exact_text():
    owner, _, workspace, main = await assistant()
    session, linked = await watched_conversation(owner, workspace, main)
    ctx, lease, human = await main_turn(owner, workspace, main, "Send two notes to the team snake chat.")
    try:
        first = followup(linked["task_id"], linked["task_revision"], "First note for the team.", human)
        asked, _ = await tool_call(ctx, "tasks.followup", first)
        await q.reply(asked["suspended"], [[CANCEL]], owner)
        retried, _ = await tool_call(ctx, "tasks.followup", first)
        assert "suspended" in retried and retried["suspended"] != asked["suspended"]
        assert await inputs(session.id) == []
        await q.reply(retried["suspended"], [[CONFIRM]], owner)
        changed = {**first, "text": "A different note for the team."}
        other, _ = await tool_call(ctx, "tasks.followup", changed)
        assert "suspended" in other and await inputs(session.id) == []
        sent, _ = await tool_call(ctx, "tasks.followup", first)
        assert sent["state"] == "accepted"
        assert [row.prompt for row in await inputs(session.id)] == ["First note for the team."]
        rejected = await card(other["suspended"])
        assert rejected.status == "pending" and rejected.continuation[KIND]["digest"] == input_digest(
            linked["task_id"], "A different note for the team.")
    finally:
        await lease.release(session_status="idle")


async def test_card_shows_the_complete_text_that_will_be_sent():
    owner, _, workspace, main = await assistant()
    session, linked = await watched_conversation(owner, workspace, main)
    text = "Visible start. " + "x" * 6000 + " HIDDEN_TAIL"
    ctx, lease, human = await main_turn(owner, workspace, main, "Send the long note to the team chat.")
    try:
        asked, _ = await tool_call(ctx, "tasks.followup", followup(linked["task_id"], linked["task_revision"],
                                                                   text, human))
        assert "HIDDEN_TAIL" in (await card(asked["suspended"])).questions[0]["question"]
    finally:
        await lease.release(session_status="idle")


async def test_a_failed_send_does_not_consume_the_confirmation():
    owner, _, workspace, main = await assistant()
    session, linked = await watched_conversation(owner, workspace, main)
    ctx, lease, human = await main_turn(owner, workspace, main, "Send a note to the team snake chat.")
    try:
        args = followup(linked["task_id"], linked["task_revision"], "A note for the team.", human)
        asked, _ = await tool_call(ctx, "tasks.followup", args)
        await q.reply(asked["suspended"], [[CONFIRM]], owner)
        failed, _ = await tool_call(ctx, "tasks.followup", {**args, "expected_revision": linked["task_revision"] + 5})
        assert failed["error"] == "ASSISTANT_REVISION_CONFLICT"
        sent, _ = await tool_call(ctx, "tasks.followup", args)
        assert sent.get("state") == "accepted"
    finally:
        await lease.release(session_status="idle")


async def test_attach_card_names_the_files_that_will_be_shared():
    owner, _, workspace, main = await assistant()
    session, linked = await watched_conversation(owner, workspace, main)
    asset_id = "pa-asset-" + uuid4().hex
    async with get_db_session() as db:
        db.add(FileAsset(id=asset_id, user_id=owner, workspace_id=workspace, session_id=main.id,
            project_id=main.project_id, name="salary-review.xlsx", oss_key="test/salary-review", mime="text/plain",
            size=10, status="ready", created_at=datetime.now(timezone.utc)))
    ctx, lease, human = await main_turn(owner, workspace, main, "Attach my spreadsheet to the team chat.")
    try:
        asked, _ = await tool_call(ctx, "assets.attach", followup(linked["task_id"], linked["task_revision"],
            "Here is the file.", human, attachment_ids=[asset_id]))
        question = (await card(asked["suspended"])).questions[0]["question"]
        assert "salary-review.xlsx" in question
    finally:
        await lease.release(session_status="idle")
    assert await inputs(session.id) == []


async def test_automatic_continuation_is_refused_for_a_workspace_visible_conversation():
    """Automatic steps cannot be confirmed one by one, so shared conversations get none (V2 D4)."""
    owner, _, workspace, main = await assistant()
    session, linked = await watched_conversation(owner, workspace, main)
    quote = "Keep improving the team snake game until it is done, with at most one more step."
    ctx, lease, human = await main_turn(owner, workspace, main, quote)
    try:
        args = followup(linked["task_id"], linked["task_revision"], "Add a pause button.", human,
                        continuation={"authorization_quote": quote, "max_followups": 1})
        asked, _ = await tool_call(ctx, "tasks.followup", args)
        await q.reply(asked["suspended"], [[CONFIRM]], owner)
        before = len(await inputs(session.id))
        refused, _ = await tool_call(ctx, "tasks.followup", args)
        assert refused["error"] == "ASSISTANT_CONTINUATION_SHARED"
        assert len(await inputs(session.id)) == before
        # The refused send left the same text's confirmation usable without a grant.
        plain = followup(linked["task_id"], linked["task_revision"], "Add a pause button.", human)
        assert (await tool_call(ctx, "tasks.followup", plain))[0]["state"] == "accepted"
    finally:
        await lease.release(session_status="idle")


# 7. GET /api/assistant/watch --------------------------------------------------

async def test_watch_endpoint_is_empty_before_the_assistant_and_lists_summaries_after(monkeypatch):
    owner, _, workspace = await accounts()
    async with client_for(owner, workspace, monkeypatch) as client:
        before = await client.get("/api/assistant/watch")
        assert before.status_code == 200 and before.json() == {"items": [], "has_more": False}
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(Session).where(
            Session.user_id == owner, Session.kind == "assistant")) == 0
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    session = await conversation(owner, workspace, main, "Watched snake chat")
    linked = await watch(owner, workspace, main, session)
    await accept_task_command(user_id=owner, workspace_id=workspace, main_id=main.id, task_id=linked["task_id"],
        idempotency_key="watch-input", prompt="Report the score.", expected_revision=linked["task_revision"])
    await run_once(session.id, owner, "The high score is 42.")
    async with client_for(owner, workspace, monkeypatch) as client:
        after = await client.get("/api/assistant/watch")
    assert after.status_code == 200 and after.json()["has_more"] is False
    [item] = after.json()["items"]
    assert (item["task_id"], item["session_id"]) == (linked["task_id"], session.id)
    assert item["latest_result"]["summary"] == "The high score is 42." and item["latest_result"]["outcome"] == "succeeded"
    assert item["project"]["id"] == main.project_id
