"""Real history pagination, domain tools and read observations in provider requests.

V2 (PERSONAL_ASSISTANT_DESIGN_V2.md 4.2, D1): reads check current ownership;
saved answers and observations are not re-validated when sources change later.
"""
from dataclasses import replace
import json

import pytest
from sqlalchemy import select

from agent import inbox
from agent.driver import reserve_run
from agent.loop import _to_llm_messages
from assistant.history import read_history
from assistant.policy import AssistantError
from assistant.projection import project_main_messages
from core.config import get_config
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.assistant import TaskResult
from db.models.part import Part
from db.models.workspace import WorkspaceMember
from models.message import TextPart, ToolPartData, ToolStatus
from session.agent_event_log import load_canonical_model_surface
from session.session import create_assistant_message, create_session, save_part, update_message_info
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.assistant_helpers import prepare_report
from tests.unit.test_assistant_results import result_ready
from tool.assistant_tools import assistant_tools
from tool.tool import ToolContext
from tests.unit.assistant_source_fixtures import consume_context

TOOLS = {tool.id: tool for tool in assistant_tools}


@pytest.fixture(autouse=True)
def cursor_key(monkeypatch):
    monkeypatch.setattr(get_config(), "jwt_secret", "assistant-cursor-test-only")


async def read_turn():
    owner, workspace, main, accepted, execution_lease, report = await result_ready()
    await execution_lease.release(session_status="idle")
    await inbox.accept_inbox_item(session_id=main.id, user_id=owner, delivery="followup",
        prompt="Read the original report and preserve what remains untested.", origin="human",
        origin_ref={"actor_user_id": owner})
    lease = await reserve_run(main.id, owner)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    message = await create_assistant_message(main.id, batch.messages[0].id, model_id="test/model",
        agent="assistant", user_id=owner, run_fence=(main.id, lease.run_id, lease.generation))
    ctx = ToolContext(user_id=owner, workspace_id=workspace, session_id=main.id, project_id=main.project_id,
        agent_id="assistant", run_id=lease.run_id, run_generation=lease.generation, message_id=message.id)
    return ctx, lease, message, accepted, report


async def call_tool(ctx, operation, arguments):
    part = ToolPartData(tool=operation, canonical_tool_id=operation, call_id="read-" + operation.replace(".", "-"),
        wire_tool_name=operation.replace(".", "_"), provider_binding_digest="d" * 64, provider_dialect="openai",
        stream_seq=0, status=ToolStatus.RUNNING, input=arguments, session_id=ctx.session_id, message_id=ctx.message_id)
    await save_part(part, is_new=True, user_id=ctx.user_id, run_fence=ctx.run_fence)
    ctx = replace(ctx, part_id=part.id)
    result = await TOOLS[operation].execute(arguments, ctx)
    part.status = ToolStatus.ERROR if result.metadata.get("error") else ToolStatus.COMPLETED
    part.output, part.metadata = result.output, result.metadata
    await save_part(part, user_id=ctx.user_id, run_fence=ctx.run_fence)
    return result, ctx, part


@pytest.mark.parametrize("text", ["原始证据🙂" * 3500, "\x01" * 14000], ids=["unicode", "json-escaped-controls"])
async def test_long_unicode_history_paginates_without_breaking_json_and_detects_changed_source(text):
    ctx, lease, _, accepted, report = await read_turn()
    try:
        async with get_db_session() as db:
            part = await db.scalar(select(Part).where(Part.message_id == report.id, Part.type == "text"))
            part.data = {**part.data, "text": text}
            source_id = part.id
        identity = dict(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
            session_id=accepted["execution_session_id"], message_ids=[report.id], max_chars=16000)
        page = await read_history(**identity)
        cursor = page["next_cursor"]
        assert cursor and page["returned_chars"] <= 16000
        assert len(json.dumps(page, ensure_ascii=False).encode()) < 50 * 1024
        chunks = [item["text"] for item in page["items"]]
        while page["next_cursor"]:
            page = await read_history(**identity, cursor=page["next_cursor"])
            assert len(json.dumps(page, ensure_ascii=False).encode()) < 50 * 1024
            chunks += [item["text"] for item in page["items"]]
        assert "".join(chunks) == text
        with pytest.raises(AssistantError) as changed_selection:
            await read_history(**{**identity, "message_ids": None}, cursor=cursor)
        assert changed_selection.value.status == 409
        async with get_db_session() as db:
            part = await db.get(Part, source_id)
            part.data = {**part.data, "text": "changed"}
        with pytest.raises(AssistantError) as changed:
            await read_history(**identity, cursor=cursor)
        assert changed.value.status == 410
    finally:
        await lease.release(session_status="idle")


async def test_history_rechecks_membership_and_reads_owned_top_level_sessions_only():
    ctx, lease, _, accepted, report = await read_turn()
    try:
        # V2 (design 6.2): any top-level conversation the user owns is readable, linked or not.
        unlinked = await create_session(user_id=ctx.user_id, workspace_id=ctx.workspace_id)
        child = await create_session(user_id=ctx.user_id, workspace_id=ctx.workspace_id, parent_id=unlinked.id)
        identity = dict(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        assert (await read_history(**identity, session_id=unlinked.id))["items"] == []
        with pytest.raises(AssistantError) as subagent:
            await read_history(**identity, session_id=child.id)
        assert subagent.value.status == 404
        page = await read_history(**identity, session_id=accepted["execution_session_id"],
                                  message_ids=[report.id], max_chars=3)
        async with get_db_session() as db:
            (await db.get(WorkspaceMember, (ctx.workspace_id, ctx.user_id))).status = "removed"
        with pytest.raises(AssistantError) as revoked:
            await read_history(**identity, session_id=accepted["execution_session_id"],
                               message_ids=[report.id], max_chars=3, cursor=page["next_cursor"])
        assert revoked.value.status == 403
    finally:
        await lease.release(session_status="idle")


async def test_report_history_reads_only_the_bound_task_session():
    from assistant.commands import accept_task_command
    ctx, lease, message, _, result_id, _ = await prepare_report()
    try:
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            reference = result.output_refs[-1]
        other = await accept_task_command(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
            project_id=ctx.project_id, idempotency_key="unrelated-task", prompt="UNRELATED_TASK_PROMPT")
        output, ctx, _ = await call_tool(ctx, "history.read", {"session_id": reference["session_id"],
            "message_ids": [reference["message_id"]]})
        assert not output.metadata.get("error"), output.output
        assert "Browser verification is still untested" in output.output
        denied, ctx, _ = await call_tool(ctx, "history.read", {"session_id": other["execution_session_id"]})
        assert denied.metadata["failure_code"] == "ASSISTANT_REPORT_SCOPE"
        assert "UNRELATED_TASK_PROMPT" not in denied.output
        # V2 records no per-read coverage receipts.
        async with get_db_session() as db:
            assert not (await db.scalars(select(AgentEvent.id).where(AgentEvent.session_id == ctx.session_id,
                AgentEvent.kind.in_(("assistant.report.sources_read", "assistant.history.read"))))).all()
    finally:
        await lease.release(session_status="idle")


async def test_read_output_is_stored_and_replayed_only_inside_the_run_that_read_it():
    ctx, lease, _, part, result_id, _ = await prepare_report()
    try:
        output, ctx, part = await call_tool(ctx, "results.read", {"result_id": result_id})
        assert "Browser verification is still untested" in output.output
        # The page's version identifies the observation (the report input itself also carries a summary).
        observed = json.loads(output.output)["source_version"]
        async with get_db_session() as db:
            saved = await db.get(Part, part.id)
            assert observed in json.dumps(saved.data)
            assert "transient_assistant_refs" not in json.dumps(saved.data)
        surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        projected = await project_main_messages(list(surface.messages), ctx=ctx)
        rendered = _to_llm_messages(projected, user_id=ctx.user_id, assistant_projection_verified=True)
        assert observed in json.dumps(rendered)
        # Without the current-run marker a stored observation is only a request to read again.
        unmarked = json.dumps(_to_llm_messages(list(surface.messages)))
        assert observed not in unmarked and "fresh_read_required" in unmarked
        async with get_db_session() as db:
            row = await db.get(TaskResult, result_id)
            original = await db.get(Part, row.output_refs[-1]["part_id"])
            original.data = {**original.data, "text": "A changed report"}
        # D1: a later source change does not fail or rewrite the observation this run already made.
        surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        projected = await project_main_messages(list(surface.messages), ctx=ctx)
        rendered = json.dumps(_to_llm_messages(projected, user_id=ctx.user_id, assistant_projection_verified=True))
        assert observed in rendered and "A changed report" not in rendered
    finally:
        await lease.release(session_status="idle")


async def test_derived_answer_stays_readable_after_its_source_changes():
    ctx, lease, message, accepted, report = await read_turn()
    try:
        output, ctx, _ = await call_tool(ctx, "history.read", {"session_id": accepted["execution_session_id"],
            "message_ids": [report.id]})
        assert not output.metadata.get("error")
        await consume_context(ctx)
        await save_part(TextPart(session_id=ctx.session_id, message_id=message.id,
            text="Derived answer based on the execution report."), is_new=True, user_id=ctx.user_id, run_fence=ctx.run_fence)
        message.finish = "stop"
        await update_message_info(message, user_id=ctx.user_id, run_fence=ctx.run_fence)
        identity = dict(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
            session_id=ctx.session_id, message_ids=[message.id])
        assert "Derived answer" in json.dumps(await read_history(**identity))
        async with get_db_session() as db:
            source = await db.scalar(select(Part).where(Part.message_id == report.id, Part.type == "text"))
            source.data = {**source.data, "text": "changed source"}
        # D1: the saved answer is not re-validated; a new read returns the current source.
        assert "Derived answer" in json.dumps(await read_history(**identity))
        current = await read_history(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
            session_id=accepted["execution_session_id"], message_ids=[report.id])
        assert [item["text"] for item in current["items"]] == ["changed source"]
    finally:
        await lease.release(session_status="idle")


async def test_domain_tools_are_bounded_and_sandbox_free():
    from assistant.continuation import COORDINATION_TOOLS
    from assistant.reporting import ASSISTANT_TOOLS
    assert set(TOOLS) == ASSISTANT_TOOLS | COORDINATION_TOOLS
    assert "tasks.next_step" not in ASSISTANT_TOOLS
    assert all(not tool.sandbox_required for tool in TOOLS.values())
    ctx, lease, _, accepted, _ = await read_turn()
    try:
        for operation, arguments in [("projects.list", {}), ("sessions.list", {}), ("tasks.list", {}),
                                      ("tasks.get", {"task_id": accepted["task_id"]})]:
            result, ctx, _ = await call_tool(ctx, operation, arguments)
            assert not result.metadata.get("error"), result.output
            assert not result.metadata.get("truncated")
            assert json.loads(result.output)
    finally:
        await lease.release(session_status="idle")


async def test_credentials_are_removed_before_paging_while_request_contact_details_remain():
    ctx, lease, _, accepted, report = await read_turn()
    try:
        async with get_db_session() as db:
            source = await db.scalar(select(Part).where(Part.message_id == report.id, Part.type == "text"))
            source.data = {**source.data, "text": "Contact qa@example.com. password=TOP_SECRET_VALUE Next step is untested."}
            db.add(Part(id="reasoning-" + report.id, user_id=ctx.user_id, session_id=source.session_id,
                message_id=report.id, type="reasoning", data={"type": "reasoning", "text": "INTERNAL_REASONING"},
                created_at=source.created_at))
        identity = dict(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
            session_id=accepted["execution_session_id"], message_ids=[report.id], max_chars=4)
        page = await read_history(**identity)
        text = "".join(item["text"] for item in page["items"])
        while page["next_cursor"]:
            page = await read_history(**identity, cursor=page["next_cursor"])
            text += "".join(item["text"] for item in page["items"])
        assert "qa@example.com" in text and "untested" in text
        assert "TOP_SECRET_VALUE" not in text and "INTERNAL_REASONING" not in text
        assert "[redacted]" in text
    finally:
        await lease.release(session_status="idle")
