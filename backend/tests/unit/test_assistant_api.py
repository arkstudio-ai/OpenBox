"""Real HTTP/SQL acceptance, consistent snapshots, read positions and report retries."""
import asyncio
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import func, select

from agent import inbox
from agent.driver import reserve_run
from api import assistant as api
from assistant.commands import accept_task_command
from assistant.inputs import accept_turn
from assistant.policy import AssistantError
from assistant.results import deliver_task_result
from assistant.service import ensure_main_session
from assistant.snapshot import advance_read_cursor, get_snapshot
from core.config import get_config
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantReadCursor, AssistantTask, TaskResult
from db.models.part import Part
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from models.message import TextPart
from session.session import create_assistant_message, save_part, update_message_info
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_results import result_ready


@pytest.fixture(autouse=True)
def signing_key(monkeypatch):
    monkeypatch.setattr(get_config(), "jwt_secret", "assistant-api-test-only")


def client_for(owner, workspace, monkeypatch):
    app = FastAPI()
    app.include_router(api.router)
    actor = {"user_id": owner, "workspace_id": workspace}
    app.dependency_overrides[api.get_current_user] = lambda: actor
    app.dependency_overrides[api.get_workspace] = lambda: {"id": workspace}
    monkeypatch.setattr(api, "schedule_inbox_wake", lambda *_: None)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://assistant.test")


async def complete_answer(owner, workspace, main, key):
    receipt = await accept_turn(user_id=owner, workspace_id=workspace, main_id=main.id,
                               client_id=key, text=f"Question {key}")
    lease = await reserve_run(main.id, owner)
    try:
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        assert batch.messages[0].client_message_id == key
        fence = (main.id, lease.run_id, lease.generation)
        message = await create_assistant_message(main.id, batch.messages[0].id, model_id="test/model",
            agent="assistant", user_id=owner, run_fence=fence)
        part = TextPart(session_id=main.id, message_id=message.id, text=f"Answer {key}")
        await save_part(part, is_new=True, user_id=owner, run_fence=fence)
        message.finish = "stop"
        await update_message_info(message, user_id=owner, run_fence=fence)
        await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
    finally:
        await lease.release(session_status="idle")
    return receipt, message, part


async def fail_report(owner, main):
    lease = await reserve_run(main.id, owner)
    try:
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        assert len(batch.receipts) == 1 and batch.receipts[0].origin == "task_result"
        fence = (main.id, lease.run_id, lease.generation)
        message = await create_assistant_message(main.id, batch.messages[0].id, model_id="test/model",
            agent="assistant", user_id=owner, run_fence=fence)
        message.finish, message.error = "error", {"name": "ProviderError", "message": "simulated failure"}
        await update_message_info(message, user_id=owner, run_fence=fence)
    finally:
        await lease.release(session_status="idle")


async def test_http_get_is_read_only_ensure_is_unique_and_human_turn_is_idempotent(monkeypatch):
    owner, _, workspace = await accounts()
    async with client_for(owner, workspace, monkeypatch) as client:
        assert (await client.get("/api/assistant")).json()["state"] == "not_created"
        assert (await client.post("/api/assistant/turns", json={"client_id": "one", "text": "Hello",
                                                              "delivery": "followup"})).status_code == 404
        first, second = await asyncio.gather(*(client.post("/api/assistant/ensure", json={"model": "test/model"})
                                              for _ in range(2)))
        assert first.json() == second.json()
        main_id = first.json()["session_id"]
        payload = {"client_id": "phone-turn-1", "text": "Hello", "delivery": "followup"}
        one, two = await asyncio.gather(*(client.post("/api/assistant/turns", json=payload) for _ in range(2)))
        assert one.status_code == two.status_code == 202 and one.json() == two.json()
        assert len(one.json()["inbox_client_id"]) == 64
        assert one.json()["state"] == "accepted" and one.json()["message_id"] is None
        for changed, status in (({"text": "Changed"}, 409), ({"origin": "human"}, 422),
                                ({"delivery": "steer"}, 422), ({"attachment_ids": ["a", "a"]}, 422)):
            assert (await client.post("/api/assistant/turns", json={**payload, **changed})).status_code == status
        async with get_db_session() as db:
            row = await db.get(Session, main_id)
            row.model = "new/default"
        assert (await client.post("/api/assistant/turns", json=payload)).json() == one.json()
        before = (await client.get("/api/assistant")).json()
        assert before["tasks"] == [] and before["unread_count"] == 0
        async with get_db_session() as db:
            assert await db.scalar(select(func.count()).select_from(Session).where(Session.user_id == owner)) == 1
            assert await db.scalar(select(func.count()).select_from(AgentInboxItem).where(AgentInboxItem.user_id == owner)) == 1
            assert await db.get(AgentDriverState, main_id) is None
            item = await db.get(AgentInboxItem, one.json()["inbox_id"])
            assert item.origin == "human" and item.origin_ref["actor_user_id"] == owner


async def test_http_task_receipts_replay_before_revision_and_are_owner_scoped(monkeypatch):
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    async with client_for(owner, workspace, monkeypatch) as client:
        payload = {"idempotency_key": "create", "project_id": main.project_id, "title": "Report",
                   "input": {"text": "Create the report", "delivery": "followup"}}
        one, two = await asyncio.gather(*(client.post("/api/assistant/tasks", json=payload) for _ in range(2)))
        assert one.status_code == two.status_code == 202 and one.json() == two.json()
        receipt = one.json()
        command = {"idempotency_key": "followup", "expected_revision": 1, "action": "input",
                   "input": {"text": "Use Chinese", "delivery": "followup"}}
        endpoint = f"/api/assistant/tasks/{receipt['task_id']}/commands"
        followup = await client.post(endpoint, json=command)
        assert followup.status_code == 202 and followup.json()["task_revision"] == 2
        assert (await client.post(endpoint, json=command)).json() == followup.json()
        assert (await client.post(endpoint, json={**command, "idempotency_key": "stale"})).status_code == 409
        read = await client.get(f"/api/assistant/commands/{receipt['command_id']}")
        assert read.json()["receipt"] == receipt
        snapshot = (await client.get("/api/assistant")).json()
        assert snapshot["tasks"][0]["latest_submission"]["applied_at"] is None
        assert snapshot["tasks"][0]["latest_result"] is None
        assert snapshot["tasks"][0]["execution_session"]["id"] == receipt["execution_session_id"]
    await ensure_main_session(user_id=other, workspace_id=workspace)
    async with client_for(other, workspace, monkeypatch) as client:
        assert (await client.get(f"/api/assistant/tasks/{receipt['task_id']}")).status_code == 404
        assert (await client.get(f"/api/assistant/commands/{receipt['command_id']}")).status_code == 404
        assert (await client.get("/api/assistant/tasks")).json()["items"] == []
    async with get_db_session() as db:
        (await db.get(WorkspaceMember, (workspace, owner))).status = "removed"
    async with client_for(owner, workspace, monkeypatch) as client:
        assert (await client.post("/api/assistant/tasks", json=payload)).status_code == 403


async def test_read_position_is_atomic_max_across_devices_and_does_not_wake(monkeypatch):
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    await complete_answer(owner, workspace, main, "first")
    await complete_answer(owner, workspace, main, "second")
    async with client_for(owner, workspace, monkeypatch) as client:
        snapshot = (await client.get("/api/assistant")).json()
        assert snapshot["unread_count"] == 2 and len(snapshot["answers"]) == 2
        newest, older = snapshot["answers"]
        assert newest["sequence"] <= snapshot["high_water_mark"]
        small = (await client.get("/api/assistant?limit=1")).json()
        assert small["unread_count"] == 1 and small["unread_count_is_lower_bound"] is True
        previous = (await client.get("/api/assistant", params={"limit": 1,
            "before_sequence": small["next_before_sequence"]})).json()
        assert previous["answers"][0]["message_id"] == older["message_id"]
        assert previous["next_before_sequence"] is None
        responses = await asyncio.gather(*(client.post("/api/assistant/read-cursor", json={
            "last_seen_sequence": row["sequence"], "display_token": row["display_token"],
        }) for row in (newest, older, older)))
        assert all(response.status_code == 200 for response in responses)
        refreshed = (await client.get("/api/assistant")).json()
        assert refreshed["last_seen_sequence"] == newest["sequence"] and refreshed["unread_count"] == 0
        async with get_db_session() as db:
            assert await db.scalar(select(func.count()).select_from(AssistantReadCursor).where(
                AssistantReadCursor.user_id == owner)) == 1
            assert (await db.get(AgentDriverState, main.id)).generation == 2


@pytest.mark.parametrize("change", ["sequence", "token", "answer", "source", "membership", "foreign", "expired"])
async def test_display_receipt_rejects_forgery_and_current_revocation(change, monkeypatch):
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    _, _, answer_part = await complete_answer(owner, workspace, main, "one")
    if change == "expired":
        monkeypatch.setattr("assistant.snapshot.DISPLAY_TTL_SECONDS", -1)
    state = await get_snapshot(user_id=owner, workspace_id=workspace)
    shown = state["answers"][0]
    kwargs = {"user_id": owner, "workspace_id": workspace, "main_id": main.id,
              "last_seen_sequence": shown["sequence"], "display_token": shown["display_token"]}
    if change == "sequence":
        kwargs["last_seen_sequence"] += 1
    elif change == "token":
        kwargs["display_token"] = "unsigned"
    elif change == "foreign":
        kwargs["user_id"] = other
    elif change != "expired":
        async with get_db_session() as db:
            if change == "membership":
                (await db.get(WorkspaceMember, (workspace, owner))).status = "removed"
            else:
                part = await db.get(Part, answer_part.id) if change == "answer" else await db.scalar(select(Part).where(
                    Part.session_id == main.id, Part.type == "text", Part.id != answer_part.id))
                part.data = {**part.data, "text": "Changed after the client received its snapshot"}
    with pytest.raises(AssistantError):
        await advance_read_cursor(**kwargs)
    async with get_db_session() as db:
        assert await db.get(AssistantReadCursor, (main.id, owner)) is None


async def test_manual_retry_is_concurrent_idempotent_and_has_a_new_finite_budget(monkeypatch):
    owner, workspace, main, task, lease, _ = await result_ready()
    await lease.release(session_status="idle")
    async with get_db_session() as db:
        result = await db.scalar(select(TaskResult).where(TaskResult.task_id == task["task_id"]))
        result_id, original_run, original_generation = result.id, result.run_id, result.generation
    for attempt in range(1, 4):
        delivered = await deliver_task_result(result_id)
        assert delivered["report_attempt"] == attempt
        await fail_report(owner, main)
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            assert result.delivery_state == ("blocked" if attempt == 3 else "retry_wait")
            result.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert await deliver_task_result(result_id) is None
    async with client_for(owner, workspace, monkeypatch) as client:
        endpoint = f"/api/assistant/results/{result_id}/retry"
        payload = {"idempotency_key": "manual-retry", "expected_report_attempt": 3}
        first, second = await asyncio.gather(*(client.post(endpoint, json=payload) for _ in range(2)))
        assert first.status_code == second.status_code == 202 and first.json() == second.json()
        assert first.json()["report_attempt"] == 4
        for attempt in range(4, 7):
            if attempt > 4:
                assert (await deliver_task_result(result_id))["report_attempt"] == attempt
            await fail_report(owner, main)
            async with get_db_session() as db:
                result = await db.get(TaskResult, result_id)
                assert result.delivery_state == ("blocked" if attempt == 6 else "retry_wait")
                result.available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        assert (await client.post(endpoint, json=payload)).json() == first.json()
        assert (await client.post(endpoint, json={**payload, "idempotency_key": "stale"})).status_code == 409
        original = (await client.get(f"/api/assistant/results/{result_id}")).json()
        assert original["outcome"] == "succeeded" and original["delivery_state"] == "blocked"
        assert original["run_id"] == original_run and original["generation"] == original_generation
    async with get_db_session() as db:
        assert (await db.get(AgentDriverState, task["execution_session_id"])).generation == original_generation
        assert await db.scalar(select(func.count()).select_from(AssistantCommand).where(
            AssistantCommand.actor_user_id == owner, AssistantCommand.action == "report_retry")) == 1
        rows = list((await db.scalars(select(AgentInboxItem).where(AgentInboxItem.session_id == main.id))).all())
        assert len(rows) == 6 and len({row.client_id for row in rows}) == 6
        assert all(len(row.client_id) == 64 and row.origin_ref["execution_mode"] == "report_only" for row in rows)


async def test_snapshot_and_high_water_share_one_postgres_snapshot(monkeypatch):
    async with get_db_session() as db:
        if db.get_bind().dialect.name != "postgresql":
            pytest.skip("Independent writer consistency requires PostgreSQL MVCC")
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    task = await accept_task_command(user_id=owner, workspace_id=workspace, main_id=main.id,
        idempotency_key="create", prompt="Report", project_id=main.project_id, title="Before snapshot")
    from assistant import snapshot as snapshots
    from session.agent_event_log import append_agent_event_locked
    from session.internal_parts import _lock_fenced, begin_session_write
    real = snapshots.list_tasks

    async def race(**kwargs):
        async with get_db_session() as db:
            await begin_session_write(db)
            row = await _lock_fenced(db, main.id, owner)
            (await db.get(AssistantTask, task["task_id"])).title = "After snapshot"
            await append_agent_event_locked(db, row, kind="assistant.business.read", payload={"test": True})
        return await real(**kwargs)

    monkeypatch.setattr(snapshots, "list_tasks", race)
    state = await get_snapshot(user_id=owner, workspace_id=workspace)
    assert state["tasks"][0]["task"]["title"] == "Before snapshot"
    async with get_db_session() as db:
        assert (await db.get(AssistantTask, task["task_id"])).title == "After snapshot"
        assert await db.scalar(select(func.max(AgentEvent.sequence)).where(
            AgentEvent.session_id == main.id)) > state["high_water_mark"]


async def test_legacy_session_prompt_uses_task_command_and_replays_after_revision_changes(monkeypatch):
    from api import sessions as routes
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    task = await accept_task_command(user_id=owner, workspace_id=workspace, main_id=main.id,
        idempotency_key="create", prompt="Report", project_id=main.project_id)
    async def no_wake(*_):
        return None
    monkeypatch.setattr(inbox, "wake_inbox_session", no_wake)
    actor = {"user_id": owner, "workspace_id": workspace}
    # The legacy contract omits delivery and would otherwise preempt the run.
    body = routes.PromptBody(text="Continue in Chinese", client_message_id="legacy-followup")
    first = await routes.send_message_async(task["execution_session_id"], body, actor)
    repeated = await routes.send_message_async(task["execution_session_id"], body, actor)
    assert first == repeated
    async with get_db_session() as db:
        row = await db.get(AssistantTask, task["task_id"])
        assert row.intent_revision == 2 and row.control_revision == 2
        item = await db.get(AgentInboxItem, first["inboxId"])
        assert item.delivery == "followup" and item.origin == "human"
        assert item.origin_ref["task_id"] == row.id and item.origin_ref["intent_revision"] == 2
        assert item.origin_ref["client_message_id"] == "legacy-followup"
        assert len(item.client_id) == 64
        assert await db.get(AgentDriverState, row.execution_session_id) is None
    from fastapi import HTTPException
    for changed in ({"text": "Different"}, {"delivery": "steer"}, {"client_message_id": None}):
        with pytest.raises(HTTPException):
            await routes.send_message_async(task["execution_session_id"],
                routes.PromptBody(**{**body.model_dump(), **changed}), actor)
    async with get_db_session() as db:
        (await db.get(AssistantTask, task["task_id"])).desired_state = "paused"
    with pytest.raises(HTTPException) as paused:
        await routes.send_message_async(task["execution_session_id"],
            routes.PromptBody(text="New input", client_message_id="new-key"), actor)
    assert paused.value.detail["code"] == "ASSISTANT_TASK_NOT_RUNNING"


async def test_task_command_hash_covers_exact_input_including_whitespace(monkeypatch):
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    async with client_for(owner, workspace, monkeypatch) as client:
        payload = {"idempotency_key": "exact-body", "project_id": main.project_id,
                   "input": {"text": " Keep this whitespace ", "delivery": "followup"}}
        accepted = await client.post("/api/assistant/tasks", json=payload)
        assert accepted.status_code == 202
        async with get_db_session() as db:
            assert (await db.get(AgentInboxItem, accepted.json()["inbox_id"])).prompt == " Keep this whitespace "
        payload["input"]["text"] = "Keep this whitespace"
        assert (await client.post("/api/assistant/tasks", json=payload)).status_code == 409


async def test_http_variant_null_clears_and_does_not_alias_omission(monkeypatch):
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model", variant="high")
    async with client_for(owner, workspace, monkeypatch) as client:
        body = {"client_id": "clear", "text": "Hello", "delivery": "followup", "variant": None}
        accepted = await client.post("/api/assistant/turns", json=body)
        assert accepted.status_code == 202
        async with get_db_session() as db:
            item = await db.get(AgentInboxItem, accepted.json()["inbox_id"])
            assert item.variant is None
        assert (await client.post("/api/assistant/turns", json={key: value for key, value in body.items()
                                                               if key != "variant"})).status_code == 409
    lease = await reserve_run(main.id, owner)
    try:
        await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        async with get_db_session() as db:
            assert (await db.get(Session, main.id)).variant is None
    finally:
        await lease.release(session_status="idle")


async def test_browser_turn_checks_entry_and_preserves_video_options_on_transport_replay(monkeypatch):
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model", variant="high")
    async with get_db_session() as db:
        row = await db.get(Session, main.id)
        row.video_model, row.video_resolution = "video/original", "720p"
    async with client_for(owner, workspace, monkeypatch) as client:
        body = {"client_id": "browser-request", "assistant_session_id": main.id, "text": "Prepare a clip",
                "delivery": "followup", "video_model": "video/chosen", "video_resolution": "1080p"}
        wrong = await client.post("/api/assistant/turns", json={**body, "assistant_session_id": "other-main"})
        assert wrong.status_code == 409 and wrong.json()["detail"]["code"] == "ASSISTANT_ENTRY_CHANGED"
        accepted = await client.post("/api/assistant/turns", json=body)
        assert accepted.status_code == 202
        default_body = {"client_id": "inherit-video", "text": "Use current choices", "delivery": "followup"}
        inherited = await client.post("/api/assistant/turns", json=default_body)
        async with get_db_session() as db:
            item = await db.get(AgentInboxItem, accepted.json()["inbox_id"])
            assert (item.video_model, item.video_resolution) == ("video/chosen", "1080p")
            default = await db.get(AgentInboxItem, inherited.json()["inbox_id"])
            assert (default.video_model, default.video_resolution) == ("video/original", "720p")
            row = await db.get(Session, main.id)
            row.video_model, row.video_resolution = "video/new-default", "2160p"
        assert (await client.post("/api/assistant/turns", json=body)).json() == accepted.json()
        assert (await client.post("/api/assistant/turns", json=default_body)).json() == inherited.json()
        assert (await client.post("/api/assistant/turns", json={**body, "video_resolution": "720p"})).status_code == 409
        async with get_db_session() as db:
            assert await db.scalar(select(func.count()).select_from(AgentInboxItem).where(AgentInboxItem.user_id == owner)) == 2


async def test_execution_inherits_main_choices_and_followup_preserves_explicit_video_options(monkeypatch):
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model", variant="high")
    async with get_db_session() as db:
        row = await db.get(Session, main.id)
        row.video_model, row.video_resolution = "video/original", "720p"
    async with client_for(owner, workspace, monkeypatch) as client:
        created = await client.post("/api/assistant/tasks", json={"idempotency_key": "task-video",
            "project_id": main.project_id, "input": {"text": "Create a clip", "delivery": "followup"}})
        assert created.status_code == 202
        async with get_db_session() as db:
            execution = await db.get(Session, created.json()["execution_session_id"])
            item = await db.get(AgentInboxItem, created.json()["inbox_id"])
            assert execution.variant == item.variant == "high"
            assert execution.video_model == item.video_model == "video/original"
            assert execution.video_resolution == item.video_resolution == "720p"
        body = {"idempotency_key": "followup-video", "action": "input", "expected_revision": 1,
                "input": {"text": "Use HD", "delivery": "followup", "variant": None,
                          "video_model": "video/chosen", "video_resolution": "1080p"}}
        url = f"/api/assistant/tasks/{created.json()['task_id']}/commands"
        followup = await client.post(url, json=body)
        assert followup.status_code == 202
        assert (await client.post(url, json=body)).json() == followup.json()
        async with get_db_session() as db:
            item = await db.get(AgentInboxItem, followup.json()["inbox_id"])
            assert (item.video_model, item.video_resolution, item.variant) == ("video/chosen", "1080p", None)
        assert (await client.post(url, json={**body, "input": {**body["input"], "video_resolution": "720p"}})).status_code == 409


async def test_legacy_rewrites_cannot_remove_or_fork_assistant_task_evidence(monkeypatch):
    from api import sessions as routes
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    _, answer, _ = await complete_answer(owner, workspace, main, "immutable-answer")
    task = await accept_task_command(user_id=owner, workspace_id=workspace, main_id=main.id,
        idempotency_key="immutable-task", project_id=main.project_id, prompt="Original work")
    actor = {"user_id": owner, "workspace_id": workspace}
    app = FastAPI()
    app.include_router(routes.router, prefix="/api/agent")
    app.dependency_overrides[routes.get_current_user] = lambda: actor
    app.dependency_overrides[routes.get_workspace] = lambda: {"id": workspace}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://assistant.test") as client:
        for session_id in (main.id, task["execution_session_id"]):
            base = f"/api/agent/session/{session_id}"
            for method, suffix, body in (("DELETE", f"/message/{answer.id}", None),
                ("POST", f"/regenerate/{answer.id}", {}), ("POST", "/fork", {"message_id": answer.id}),
                ("POST", f"/revert/{answer.id}", None), ("POST", "/unrevert", None)):
                response = await client.request(method, base + suffix, json=body)
                assert response.status_code == 409, response.text
                assert response.json()["detail"]["code"] == "ASSISTANT_HISTORY_IMMUTABLE"
    async with get_db_session() as db:
        from db.models.message import Message
        assert await db.get(Message, answer.id) is not None
        assert (await db.get(AgentDriverState, main.id)).generation == 1
        assert await db.get(AgentDriverState, task["execution_session_id"]) is None
