"""Existing-session linking uses the real SQL, Inbox and result boundaries."""
import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from agent import inbox
from agent.driver import reserve_run
from assistant.inputs import accept_session_input
from assistant.linking import candidate, link_existing
from assistant.policy import AssistantError
from assistant.results import deliver_task_result
from assistant.service import ensure_main_session
from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask, TaskResult, TaskSubmission
from db.models.question import QuestionCheckpoint
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from models.message import TextPart, ToolPartData
from session.fork import fork_session
from session.session import create_assistant_message, create_session, save_part, update_message_info
from tests.unit.test_assistant_api import client_for
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


async def setup(**options):
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    session = await create_session(user_id=owner, workspace_id=workspace, project_id=main.project_id,
        model="test/model", **{"visibility": "private", "memory_policy": "assistant_isolated", **options})
    scope = {"user_id": owner, "workspace_id": workspace, "main_id": main.id}
    return scope, other, main, session


async def inspected(scope, session, key="link-one"):
    async with get_db_session() as db:
        value = await candidate(db, await db.get(Session, session.id))
    return {**scope, "session_id": session.id, "expected_version": value["version"], "idempotency_key": key}


async def test_concurrent_link_preserves_session_and_creates_no_input_or_run(monkeypatch):
    scope, _, main, session = await setup(title="Original conversation")
    args = await inspected(scope, session)
    monkeypatch.setattr(inbox, "schedule_inbox_wake", lambda *_: pytest.fail("link must not wake"))
    first, second = await asyncio.gather(link_existing(**args), link_existing(**args))
    assert first == second and first["created"] and first["adopted_inputs"] == 0
    again = await link_existing(**{**args, "idempotency_key": "another-device"})
    assert again["task_id"] == first["task_id"] and not again["created"]
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(AssistantTask).where(AssistantTask.user_id == scope["user_id"])) == 1
        assert await db.scalar(select(func.count()).select_from(Session).where(Session.user_id == scope["user_id"])) == 2
        assert await db.scalar(select(AgentInboxItem.id).where(AgentInboxItem.session_id == session.id)) is None
        assert await db.get(AgentDriverState, session.id) is None
        row = await db.get(Session, session.id)
        assert row.parent_id is None and row.title == "Original conversation" and row.id != main.id
    with pytest.raises(AssistantError, match="different input"):
        await link_existing(**{**args, "expected_version": "a" * 64})


@pytest.mark.parametrize("state,code", [
    ("shared", "ASSISTANT_LINK_SHARED"), ("standard", "ASSISTANT_LINK_MEMORY_POLICY"),
    ("unproven", "ASSISTANT_LINK_HISTORY_UNVERIFIED"), ("deleted", "ASSISTANT_SESSION_DELETED"),
    ("other", "ASSISTANT_SESSION_UNAVAILABLE"), ("project", "ASSISTANT_PROJECT_UNAVAILABLE"),
])
async def test_ineligible_history_or_scope_is_not_modified(state, code):
    scope, other, _, session = await setup()
    async with get_db_session() as db:
        row = await db.get(Session, session.id)
        if state == "shared": row.visibility = "workspace"
        if state == "standard": row.memory_policy = "standard"
        if state == "deleted": row.is_deleted = True
        if state == "other": row.user_id = other
        if state == "project":
            from db.models.project import Project
            (await db.get(Project, row.project_id)).user_id = other
        if state == "unproven":
            event = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == session.id,
                AgentEvent.kind == "assistant.isolation.created"))
            event.kind = "legacy.unverified"
    args = await inspected(scope, session)
    with pytest.raises(AssistantError) as rejected:
        await link_existing(**args)
    assert rejected.value.code == code
    async with get_db_session() as db:
        assert await db.scalar(select(AssistantTask.id).where(AssistantTask.execution_session_id == session.id)) is None
        assert await db.scalar(select(AssistantCommand.id).where(AssistantCommand.actor_user_id == scope["user_id"])) is None


async def test_changed_version_blocks_new_link_and_replay_rechecks_membership():
    scope, _, _, session = await setup()
    args = await inspected(scope, session)
    old = await inbox.accept_inbox_item(session_id=session.id, user_id=scope["user_id"], prompt="Original input",
        delivery="followup", client_id="before-link", origin="unknown")
    with pytest.raises(AssistantError) as stale:
        await link_existing(**args)
    assert stale.value.code == "ASSISTANT_LINK_CHANGED"
    args = await inspected(scope, session)
    receipt = await link_existing(**args)
    assert receipt["adopted_inputs"] == 1
    async with get_db_session() as db:
        row = await db.get(AgentInboxItem, old.id)
        assert row.origin == "unknown" and row.prompt == "Original input"
        member = await db.get(WorkspaceMember, (scope["workspace_id"], scope["user_id"]))
        member.status = "removed"
    with pytest.raises(AssistantError) as revoked:
        await link_existing(**args)
    assert revoked.value.status == 403


@pytest.mark.parametrize("claimed", [False, True])
async def test_original_input_is_adopted_once_and_result_returns_from_original_run(claimed):
    scope, _, main, session = await setup()
    owner = scope["user_id"]
    old = await inbox.accept_inbox_item(session_id=session.id, user_id=owner, prompt="Original input",
        delivery="followup", client_id="original-client", origin="unknown", model="test/model")
    lease = await reserve_run(session.id, owner) if claimed else None
    try:
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True) if lease else None
        args = await inspected(scope, session)
        linked = await link_existing(**args)
        assert linked["adopted_inputs"] == 1
        retried = await accept_session_input(session, user_id=owner, text="Original input", client_id="original-client")
        assert retried.id == old.id and retried.origin == "unknown" and not retried.created
        with pytest.raises(AssistantError) as changed:
            await accept_session_input(session, user_id=owner, text="Changed", client_id="original-client")
        assert changed.value.code == "ASSISTANT_INPUT_CONFLICT"
        if lease is None:
            lease = await reserve_run(session.id, owner)
            batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        fence = (session.id, lease.run_id, lease.generation)
        message = await create_assistant_message(session.id, batch.messages[0].id, model_id="test/model",
            agent="build", user_id=owner, run_fence=fence)
        await save_part(TextPart(session_id=session.id, message_id=message.id, text="Original work completed"),
            is_new=True, user_id=owner, run_fence=fence)
        message.finish = "stop"
        await update_message_info(message, user_id=owner, run_fence=fence)
        await inbox.settle_claimed_inbox_items(lease, result_message_id=message.id, outcome="succeeded")
        async with get_db_session() as db:
            result = await db.scalar(select(TaskResult).where(TaskResult.task_id == linked["task_id"]))
            assert result.run_id == lease.run_id and result.observed_intent_revision == 1
            assert result.consumed_inbox_ids == [old.id]
            submission = await db.scalar(select(TaskSubmission).where(TaskSubmission.inbox_id == old.id))
            assert submission.origin == "unknown" and submission.disposition == "applied"
            command = await db.get(AssistantCommand, submission.command_id)
            assert command.source_ref["original_origin_ref"] == {}
            result_id = result.id
        report = await deliver_task_result(result_id)
        assert report and report["result_id"] == result_id
        # A settled network retry must not become the next user turn.
        retried = await accept_session_input(session, user_id=owner, text="Original input", client_id="original-client")
        assert retried.id == old.id and retried.state == "settled"
        next_input = await accept_session_input(session, user_id=owner, text="Continue here", client_id="next-client")
        async with get_db_session() as db:
            row = await db.get(AgentInboxItem, next_input.id)
            assert row.session_id == session.id and row.origin_ref["intent_revision"] == 2
            assert (await db.get(AssistantTask, linked["task_id"])).latest_result_id == result_id
            assert (await db.get(AgentInboxItem, report["inbox_id"])).session_id == main.id
    finally:
        if lease: await lease.release(session_status="idle")


async def test_late_ordinary_acceptance_is_rejected_after_link():
    scope, _, _, session = await setup()
    await link_existing(**await inspected(scope, session))
    with pytest.raises(AssistantError) as raced:
        await inbox.accept_inbox_item(session_id=session.id, user_id=scope["user_id"],
            prompt="Raced ordinary route", delivery="followup", client_id="race", origin="human",
            origin_ref={"actor_user_id": scope["user_id"], "entrypoint": "rest"})
    assert raced.value.code == "ASSISTANT_LINK_CHANGED"
    accepted = await accept_session_input(session, user_id=scope["user_id"], text="Raced ordinary route", client_id="race")
    assert accepted.created is False  # adapter reads the committed receipt


async def test_archived_link_reuses_task_and_does_not_resume_it():
    scope, _, _, session = await setup()
    first = await link_existing(**await inspected(scope, session))
    async with get_db_session() as db:
        task = await db.get(AssistantTask, first["task_id"])
        task.archived_at = datetime.now(timezone.utc)
        task.desired_state, task.observed_state = "paused", "paused"
    opened = await link_existing(**await inspected(scope, session, "reopen"))
    assert not opened["created"] and opened["task_id"] == first["task_id"]
    async with get_db_session() as db:
        task = await db.get(AssistantTask, first["task_id"])
        assert task.archived_at is None and task.desired_state == task.observed_state == "paused"
        assert task.control_revision == 2


async def test_fork_isolation_retains_source_provenance_and_parent():
    scope, _, _, session = await setup()
    fork = await fork_session(session.id, user_id=scope["user_id"])
    linked = await link_existing(**await inspected(scope, fork))
    assert linked["execution_session_id"] == fork.id
    async with get_db_session() as db:
        assert (await db.get(Session, fork.id)).parent_id is None
        (await db.get(Session, session.id)).memory_policy = "standard"
    second_fork = await fork_session(fork.id, user_id=scope["user_id"])
    with pytest.raises(AssistantError) as unverified:
        await link_existing(**await inspected(scope, second_fork, "second-fork"))
    assert unverified.value.code == "ASSISTANT_LINK_HISTORY_UNVERIFIED"


@pytest.mark.parametrize("waiting", ["question", "tool"])
async def test_pending_legacy_request_must_finish_in_original_session(waiting):
    scope, _, _, session = await setup()
    now = datetime.now(timezone.utc)
    if waiting == "question":
        async with get_db_session() as db:
            db.add(QuestionCheckpoint(id=f"question-{session.id}", session_id=session.id, user_id=scope["user_id"],
                generation=1, status="pending", questions=[], draft=[], continuation={}, applied=False,
                created_at=now, updated_at=now))
    else:
        message = await create_assistant_message(session.id, None, model_id="test/model", agent="build", user_id=scope["user_id"])
        await save_part(ToolPartData(tool="computer", call_id="legacy-tool", status="running", input={},
            message_id=message.id, session_id=session.id), is_new=True, user_id=scope["user_id"])
    with pytest.raises(AssistantError) as blocked:
        await link_existing(**await inspected(scope, session))
    assert blocked.value.code == ("ASSISTANT_LINK_PENDING_REQUEST" if waiting == "question" else "ASSISTANT_LINK_TOOL_ACTIVE")


async def test_http_inventory_link_replay_and_followup_use_original_session(monkeypatch):
    scope, _, _, session = await setup()
    async with client_for(scope["user_id"], scope["workspace_id"], monkeypatch) as client:
        inventory = (await client.get("/api/assistant/sessions")).json()
        entry = next(item for item in inventory["items"] if item["id"] == session.id)
        assert entry["link"]["available"]
        body = {"session_id": session.id, "expected_version": entry["link"]["version"], "idempotency_key": "http-link"}
        monkeypatch.setattr("api.assistant.schedule_inbox_wake", lambda *_: pytest.fail("link must not wake"))
        first, again = await asyncio.gather(*(client.post("/api/assistant/tasks/link", json=body) for _ in range(2)))
        assert first.status_code == 200 and first.json() == again.json()
        assert first.json()["execution_session_id"] == session.id
        assert (await client.get(f"/api/assistant/tasks/{first.json()['task_id']}")).json()["latest_submission"] is None


async def test_birth_proof_does_not_bless_ordinary_memory_pipeline_evidence():
    from db.models.memory_pipeline import MemoryTurnCompletion
    scope, _, _, session = await setup()
    async with get_db_session() as db:
        db.add(MemoryTurnCompletion(id=session.id, user_id=scope["user_id"], workspace_id=scope["workspace_id"],
            session_id=session.id, branch_id="main", logical_turn_id="prior", run_id="old", run_generation=1,
            result_message_id="old-result", ordinal=1, start_sequence=1, end_sequence=1, source_boundaries=[],
            input_hash="a" * 64, acl_hash="b" * 64, pipeline_version="legacy", created_at=datetime.now(timezone.utc)))
    with pytest.raises(AssistantError) as unverified:
        await link_existing(**await inspected(scope, session))
    assert unverified.value.code == "ASSISTANT_LINK_HISTORY_UNVERIFIED"


async def test_legacy_continuation_without_pending_input_waits_in_original_session():
    scope, _, _, session = await setup()
    lease = await reserve_run(session.id, scope["user_id"])
    try:
        with pytest.raises(AssistantError) as blocked:
            await link_existing(**await inspected(scope, session))
        assert blocked.value.code == "ASSISTANT_LINK_CONTINUATION"
    finally:
        await lease.release(session_status="idle")


async def test_original_rest_retry_and_raced_acceptance_preserve_receipt(monkeypatch):
    from api.sessions import PromptBody, _accept_managed_prompt, _accept_prompt
    from fastapi import HTTPException
    scope, _, _, session = await setup()
    owner = scope["user_id"]
    monkeypatch.setattr("api.sessions._resolve_prompt_model", lambda *_: "test/model")
    body = PromptBody(text="Original REST input", client_message_id="rest-before-link")
    assert await _accept_managed_prompt(session, body, owner) is None
    accepted = await _accept_prompt(session, body, owner)
    await link_existing(**await inspected(scope, session))
    async with get_db_session() as db:
        (await db.get(Session, session.id)).model = "changed/default"
    replayed = await _accept_managed_prompt(session, body, owner)
    assert replayed.id == accepted.id
    raced = PromptBody(text="Late route", client_message_id="raced-route")
    with pytest.raises(HTTPException) as conflict:
        await _accept_prompt(session, raced, owner)
    assert conflict.value.status_code == 409 and conflict.value.detail["code"] == "ASSISTANT_LINK_CHANGED"
    assert (await _accept_managed_prompt(session, raced, owner)).id != accepted.id


async def test_pre_inbox_history_cannot_be_resent_or_preempt_a_linked_run():
    from api.sessions import _reserve_prompt_run
    from fastapi import HTTPException
    from session.session import create_user_message
    scope, _, _, session = await setup()
    owner = scope["user_id"]
    await create_user_message(session.id, "Raw legacy input", agent="build", user_id=owner,
        client_message_id="raw-before-link", origin="human", origin_ref={"actor_user_id": owner})
    await link_existing(**await inspected(scope, session))
    with pytest.raises(AssistantError) as duplicate:
        await accept_session_input(session, user_id=owner, text="Raw legacy input", client_id="raw-before-link")
    assert duplicate.value.code == "ASSISTANT_PRELINK_INPUT"
    lease = await reserve_run(session.id, owner)
    try:
        with pytest.raises(HTTPException) as raced:
            await _reserve_prompt_run(session.id, owner, require_unlinked=True)
        assert raced.value.detail["code"] == "ASSISTANT_LINK_CHANGED"
        async with get_db_session() as db:
            driver = await db.get(AgentDriverState, session.id)
            assert driver.run_id == lease.run_id and driver.abort_requested_at is None
            assert await db.scalar(select(AgentInboxItem.id).where(AgentInboxItem.session_id == session.id)) is None
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("origin", ["human", "task_result"])
async def test_only_persisted_human_tool_authority_can_link(origin):
    from assistant.commands import ToolSource, tool_command_key
    from assistant.reporting import ASSISTANT_TOOLS, REPORT_TOOLS
    from tool.assistant_tools import LinkArgs, assistant_tools
    from tool.tool import ToolContext
    import json
    scope, _, main, session = await setup()
    owner = scope["user_id"]
    assert "tasks.link_existing" in ASSISTANT_TOOLS and "tasks.link_existing" not in REPORT_TOOLS
    ref = {"actor_user_id": owner} if origin == "human" else {
        "result_id": "untrusted-report", "report_attempt": 1, "execution_mode": "report_only"}
    await inbox.accept_inbox_item(session_id=main.id, user_id=owner, prompt="Continue that original conversation",
        delivery="followup", origin=origin, origin_ref=ref)
    lease = await reserve_run(main.id, owner)
    try:
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        fence = (main.id, lease.run_id, lease.generation)
        message = await create_assistant_message(main.id, batch.messages[0].id, agent="assistant", model_id="test/model",
            user_id=owner, run_fence=fence)
        if origin == "human":
            from tests.unit.assistant_source_fixtures import consume_lease_context
            await consume_lease_context(lease, message)
        part = ToolPartData(tool="tasks.link_existing", canonical_tool_id="tasks.link_existing", call_id="link-call",
            wire_tool_name="tasks_link_existing", provider_binding_digest="a" * 64, provider_dialect="openai", stream_seq=0,
            status="running", input={}, session_id=main.id, message_id=message.id)
        await save_part(part, is_new=True, user_id=owner, run_fence=fence)
        source = ToolSource(part.id, lease.run_id, lease.generation, (batch.messages[0].id,))
        args = await inspected(scope, session)
        if origin == "task_result":
            with pytest.raises(AssistantError) as rejected:
                await link_existing(**args, source=source)
            assert rejected.value.code == "ASSISTANT_REPORT_READ_ONLY"
        else:
            ctx = ToolContext(session_id=main.id, user_id=owner, workspace_id=scope["workspace_id"],
                message_id=message.id, part_id=part.id, agent_id="assistant", run_id=lease.run_id, run_generation=lease.generation)
            tool = next(tool for tool in assistant_tools if tool.id == "tasks.link_existing")
            result = await tool.execute(LinkArgs(session_id=session.id, expected_version=args["expected_version"],
                source_message_ids=list(source.source_message_ids)), ctx)
            receipt = json.loads(result.output)
            assert receipt["execution_session_id"] == session.id and receipt["state"] == "linked"
            async with get_db_session() as db:
                command = await db.get(AssistantCommand, receipt["command_id"])
                assert command.idempotency_key == tool_command_key(main.id, part.id)
            assert await link_existing(**args, source=source) == receipt
        with pytest.raises(AssistantError) as forged:
            await link_existing(**args, source=ToolSource("forged", lease.run_id, lease.generation, source.source_message_ids))
        assert forged.value.code == "ASSISTANT_CALL_UNVERIFIED"
    finally:
        await lease.release(session_status="idle")
