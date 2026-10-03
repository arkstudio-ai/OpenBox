"""Real history pagination, domain tools and provider evidence projections."""
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
from tests.unit.test_assistant_reporting import prepare_report
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


async def test_history_rechecks_membership_and_rejects_owned_but_unlinked_sessions():
    ctx, lease, _, accepted, report = await read_turn()
    try:
        unlinked = await create_session(user_id=ctx.user_id, workspace_id=ctx.workspace_id)
        identity = dict(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        with pytest.raises(AssistantError) as unrelated:
            await read_history(**identity, session_id=unlinked.id)
        assert unrelated.value.status == 404
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


async def test_report_history_reads_only_bound_parts_and_records_actual_coverage():
    ctx, lease, message, _, result_id, _ = await prepare_report()
    try:
        async with get_db_session() as db:
            result = await db.get(TaskResult, result_id)
            reference = result.output_refs[-1]
            # Same source message, but not a frozen part in the Result.
            original = await db.get(Part, reference["part_id"])
            extra = Part(id="unbound-" + message.id, user_id=ctx.user_id, session_id=original.session_id,
                message_id=original.message_id, type="text", data={"type": "text", "text": "UNRELATED_SECRET"},
                created_at=original.created_at)
            db.add(extra)
        result, ctx, _ = await call_tool(ctx, "history.read", {"session_id": reference["session_id"],
            "message_ids": [reference["message_id"]]})
        assert not result.metadata.get("error")
        assert "UNRELATED_SECRET" not in result.output
        assert "Browser verification is still untested" in result.output
        async with get_db_session() as db:
            event = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == ctx.session_id,
                AgentEvent.part_id == ctx.part_id, AgentEvent.kind == "assistant.report.sources_read"))
            assert [span["part_id"] for span in event.payload["spans"]] == [reference["part_id"]]
    finally:
        await lease.release(session_status="idle")


async def test_read_bodies_do_not_persist_and_each_provider_projection_checks_original_sources():
    ctx, lease, _, part, result_id, _ = await prepare_report()
    try:
        output, ctx, part = await call_tool(ctx, "results.read", {"result_id": result_id})
        assert "Browser verification is still untested" in output.output
        async with get_db_session() as db:
            saved = await db.get(Part, part.id)
            assert "Browser verification is still untested" not in json.dumps(saved.data)
        surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        projected = await project_main_messages(list(surface.messages), ctx=ctx)
        rendered = _to_llm_messages(projected, user_id=ctx.user_id, assistant_projection_verified=True)
        assert "Browser verification is still untested" in json.dumps(rendered)
        # A synchronous serializer cannot trust a persisted marker by itself.
        assert "Browser verification is still untested" not in json.dumps(_to_llm_messages(list(surface.messages)))
        async with get_db_session() as db:
            row = await db.get(TaskResult, result_id)
            original = await db.get(Part, row.output_refs[-1]["part_id"])
            original.data = {**original.data, "text": "A changed report"}
        with pytest.raises(AssistantError) as revoked:
            await project_main_messages(list(surface.messages), ctx=ctx)
        assert revoked.value.code == "ASSISTANT_RESULT_SOURCE_CHANGED"
    finally:
        await lease.release(session_status="idle")


async def test_derived_answer_cannot_bypass_revoked_original_history():
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
        with pytest.raises(AssistantError) as invalid:
            await read_history(**identity)
        assert invalid.value.status == 410
    finally:
        await lease.release(session_status="idle")


async def test_domain_tools_are_bounded_and_sandbox_free():
    from assistant.reporting import ASSISTANT_TOOLS
    assert set(TOOLS) == ASSISTANT_TOOLS and all(not tool.sandbox_required for tool in TOOLS.values())
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
