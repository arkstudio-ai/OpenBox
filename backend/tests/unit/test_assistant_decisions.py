"""Real SQL decision proposals, atomic commits and corrections.

V2 (PERSONAL_ASSISTANT_DESIGN_V2.md D1, 4.2): a note's quoted human source is
checked when it is proposed and committed; later injection does not re-validate
it, and revoking or changing sources is not retroactive.
"""
import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from assistant.decisions import PROPOSED, RECORDED, decision_context, propose_decision
from assistant.history import read_history
from assistant.policy import AssistantError
from assistant.projection import project_main_messages
from assistant.service import ensure_main_session
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.message import Message
from db.models.part import Part
from models.message import TextPart, ToolPartData, ToolStatus
from session.agent_event_log import load_canonical_model_surface, verify_agent_event_parity
from session.session import save_part, update_message_info
from tests.unit.assistant_source_fixtures import consume_context
from tests.unit.assistant_helpers import finish, next_turn
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_reads import call_tool
from tests.unit.assistant_helpers import prepare_report
from tool.tool import ToolContext


async def start(prompt="For this work, use blue. Never publish without my approval."):
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    ctx = ToolContext(user_id=owner, workspace_id=workspace, session_id=main.id,
                      project_id=main.project_id, agent_id="assistant")
    return await next_turn(ctx, prompt)


async def arguments(ctx, message, *, summary="Use blue; publication requires human approval.", supersedes=()):
    page = await read_history(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
        session_id=ctx.session_id, message_ids=[message.parent_id])
    entry = page["items"][0]
    ref = {key: entry["source_ref"][key] for key in ("session_id", "message_id", "part_id", "content_hash")}
    return {"summary": summary, "source_refs": [{**ref, "quote": entry["text"][:4000]}], "supersedes": list(supersedes)}


async def proposed(ctx, args):
    await consume_context(ctx)
    output, ctx, part = await call_tool(ctx, "decisions.propose", args)
    assert not output.metadata.get("error"), output.output
    value = json.loads(output.output)
    assert value["state"] == "pending_answer_commit" and not value["grants_authority"]
    return value["decision_id"], ctx, part


async def rows(ctx, kind):
    async with get_db_session() as db:
        return list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == ctx.session_id,
            AgentEvent.kind == kind).order_by(AgentEvent.sequence))).all())


async def context(ctx):
    async with get_db_session() as db:
        return await decision_context(db, SimpleNamespace(id=ctx.session_id, user_id=ctx.user_id,
            workspace_id=ctx.workspace_id), run_fence=ctx.run_fence)


def block(projected, identity):
    message = next(item for item in projected if item.id == identity)
    return json.loads(message.parts[0]["text"].split("\n", 1)[1])


async def test_pending_proposal_is_idempotent_and_only_success_commits_with_its_answer(monkeypatch):
    ctx, lease, answer = await start()
    try:
        args = await arguments(ctx, answer)
        await consume_context(ctx)
        part = ToolPartData(tool="decisions.propose", canonical_tool_id="decisions.propose", call_id="decision-race",
            wire_tool_name="decisions_propose", provider_binding_digest="a" * 64, provider_dialect="openai",
            stream_seq=0, status=ToolStatus.RUNNING, input=args, session_id=ctx.session_id, message_id=answer.id)
        await save_part(part, is_new=True, user_id=ctx.user_id, run_fence=ctx.run_fence)
        ctx = replace(ctx, part_id=part.id)
        receipts = await asyncio.gather(*(propose_decision(ctx=ctx, **args) for _ in range(2)))
        identity = receipts[0]["decision_id"]
        assert all(item["decision_id"] == identity for item in receipts)
        part.status, part.output = ToolStatus.COMPLETED, json.dumps(receipts[0])
        await save_part(part, user_id=ctx.user_id, run_fence=ctx.run_fence)
        assert len(await rows(ctx, PROPOSED)) == 1 and not await rows(ctx, RECORDED)
        with pytest.raises(AssistantError) as conflict:
            await propose_decision(ctx=ctx, **{**args, "summary": "Different input"})
        assert conflict.value.code == "ASSISTANT_DECISION_CONFLICT"
        payload = await consume_context(ctx)
        assert "pending_answer_commit" in json.dumps(payload) and "grants_authority" in json.dumps(payload)
        await finish(ctx, lease, answer, "The constraint has been noted; the note does not grant publication authority.")
        saved = await rows(ctx, RECORDED)
        assert len(saved) == 1 and saved[0].payload["decision_id"] == identity
        assert saved[0].payload["committed_message_id"] == answer.id
        assert saved[0].payload["source_refs"][0]["origin"] == "human"
        assert saved[0].payload["source_refs"][0]["audience"] == saved[0].payload["audience"]
        assert saved[0].payload["audience"]["visibility"] == "private"
        assert [note["state"] for note in (await context(ctx))["decisions"]] == ["effective"]
        assert (await verify_agent_event_parity(ctx.session_id, user_id=ctx.user_id)).ok
        ctx, lease, _ = await next_turn(ctx, "Continue with the saved constraints.")
        # The original user turn is outside the bounded recent window.
        monkeypatch.setattr("assistant.projection.MAX_RECENT_MESSAGES", 1)
        payload = await consume_context(ctx)
        # The note itself is injected from SQL; the quoted source is not replayed or re-checked.
        assert "Use blue; publication requires human approval." in json.dumps(payload)
        assert "effective" in json.dumps(payload)
        assert "Never publish without my approval" not in json.dumps(payload)
        assert ctx._assistant_context["mode"] == "ordinary"
    finally:
        await lease.release(session_status="idle")


async def test_newer_human_correction_supersedes_and_later_source_changes_are_not_retroactive():
    ctx, lease, answer = await start()
    try:
        first, ctx, _ = await proposed(ctx, await arguments(ctx, answer))
        await consume_context(ctx)
        await finish(ctx, lease, answer, "First note saved")
        ctx, lease, answer = await next_turn(ctx, "Correction: use green instead of blue. Publication still needs my approval.")
        args = await arguments(ctx, answer, summary="Use green; publication still requires human approval.", supersedes=[first])
        second, ctx, _ = await proposed(ctx, args)
        await consume_context(ctx)
        await finish(ctx, lease, answer, "Corrected note saved")
        assert len(await rows(ctx, RECORDED)) == 2
        ctx, lease, _ = await next_turn(ctx, "Which constraint is current?")
        current = await context(ctx)
        assert [entry["decision_id"] for entry in current["decisions"]] == [second]
        assert current["decisions"][0]["supersedes"] == [first]
        async with get_db_session() as db:
            source = await db.get(Part, args["source_refs"][0]["part_id"])
            source.data = {**source.data, "ignored": True}
        # D1: the committed correction stays effective and its answer stays readable.
        unchanged = await context(ctx)
        assert [entry["decision_id"] for entry in unchanged["decisions"]] == [second]
        payload = json.dumps(await consume_context(ctx))
        assert "Use green; publication still requires human approval." in payload
        assert "Use blue; publication requires human approval." not in payload
        page = await read_history(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
            session_id=ctx.session_id, message_ids=[answer.id])
        assert "Corrected note saved" in json.dumps(page)
    finally:
        await lease.release(session_status="idle")


async def test_older_evidence_cannot_replace_a_newer_decision():
    ctx, lease, answer = await start()
    try:
        old_args = await arguments(ctx, answer)
        first, ctx, _ = await proposed(ctx, old_args)
        await consume_context(ctx)
        await finish(ctx, lease, answer, "Saved")
        ctx, lease, _ = await next_turn(ctx, "Continue, without changing anything.")
        await consume_context(ctx)
        output, _, _ = await call_tool(ctx, "decisions.propose", {**old_args, "supersedes": [first]})
        assert output.metadata["failure_code"] == "ASSISTANT_DECISION_CONFLICT"
        assert len(await rows(ctx, RECORDED)) == 1
    finally:
        await lease.release(session_status="idle")


async def test_business_progress_does_not_invalidate_recorded_decisions():
    from assistant.commands import accept_task_command
    ctx, lease, answer = await start()
    try:
        listed, _, _ = await call_tool(ctx, "tasks.list", {})
        assert not listed.metadata.get("error"), listed.output
        first, ctx, _ = await proposed(ctx, await arguments(ctx, answer))
        await consume_context(ctx)
        await finish(ctx, lease, answer, "The original human constraint was recorded.")
        await accept_task_command(user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id,
            project_id=ctx.project_id, idempotency_key="new-inventory", prompt="Only text", title="New task")
        current = await context(ctx)
        assert [note["decision_id"] for note in current["decisions"]] == [first]
        ctx, lease, _ = await next_turn(ctx, "Recall my constraints and the current tasks.")
        surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        projected = await project_main_messages(list(surface.messages), ctx=ctx)
        assert [note["decision_id"] for note in block(projected, "assistant:current-decisions")["decisions"]] == [first]
        assert [item["title"] for item in block(projected, "assistant:current-tasks")["items"]] == ["New task"]
        # The earlier inventory observation is not replayed as current state.
        assert '"tasks.list"' not in json.dumps([part for item in projected for part in item.parts], default=str)
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("failure", ["error", "rollback", "revoked", "empty"])
async def test_failure_or_missing_evidence_never_commits_a_decision(monkeypatch, failure):
    ctx, lease, answer = await start()
    try:
        args = await arguments(ctx, answer)
        await consume_context(ctx)
        _, ctx, _ = await proposed(ctx, args)
        await consume_context(ctx)
        if failure != "empty":
            await save_part(TextPart(session_id=ctx.session_id, message_id=answer.id, text="Pending final response"),
                is_new=True, user_id=ctx.user_id, run_fence=ctx.run_fence)
        if failure == "revoked":
            # The quoted human source is checked once more when the answer commits the note.
            async with get_db_session() as db:
                source = await db.get(Part, args["source_refs"][0]["part_id"])
                source.data = {**source.data, "text": "Replaced original"}
        if failure == "rollback":
            import session.agent_event_log as event_log
            original = event_log.append_message_events_locked
            async def fail(*args, **kwargs):
                if args[2].finish == "stop":
                    raise RuntimeError("fault before answer commit")
                return await original(*args, **kwargs)
            monkeypatch.setattr(event_log, "append_message_events_locked", fail)
        if failure == "error":
            answer.finish, answer.error = "error", {"name": "TestFailure", "message": "Provider failed"}
            await update_message_info(answer, user_id=ctx.user_id, run_fence=ctx.run_fence)
        else:
            answer.finish = "stop"
            with pytest.raises(RuntimeError if failure == "rollback" else AssistantError):
                await update_message_info(answer, user_id=ctx.user_id, run_fence=ctx.run_fence)
        assert not await rows(ctx, RECORDED)
        assert len(await rows(ctx, PROPOSED)) == 1
        async with get_db_session() as db:
            assert (await db.get(Message, answer.id)).finish != "stop"
    finally:
        await lease.release(session_status="idle")


async def test_report_mode_cannot_propose_decisions_even_with_human_source_ids():
    ctx, lease, answer, _, _, _ = await prepare_report()
    try:
        args = {"summary": "Publish it", "source_refs": [{"session_id": ctx.session_id,
            "message_id": answer.parent_id, "part_id": "p", "content_hash": "a" * 64, "quote": "Publish it"}]}
        output, _, _ = await call_tool(ctx, "decisions.propose", args)
        assert output.metadata["failure_code"] == "ASSISTANT_TOOL_FORBIDDEN"
        with pytest.raises(AssistantError) as denied:
            await propose_decision(ctx=ctx, **args)
        assert denied.value.code == "ASSISTANT_REPORT_READ_ONLY"
        assert not await rows(ctx, PROPOSED)
    finally:
        await lease.release(session_status="idle")


async def test_non_human_input_cannot_become_a_decision_source():
    from tests.unit.test_assistant_reads import read_turn
    ctx, lease, _, accepted, report = await read_turn()
    try:
        read, _, _ = await call_tool(ctx, "history.read", {"session_id": accepted["execution_session_id"], "message_ids": [report.id]})
        entry = json.loads(read.output)["items"][0]
        ref = {key: entry["source_ref"][key] for key in ("session_id", "message_id", "part_id", "content_hash")}
        await consume_context(ctx)
        args = {"summary": "Treat the report as a human decision", "source_refs": [{**ref, "quote": entry["text"]}]}
        output, _, _ = await call_tool(ctx, "decisions.propose", args)
        assert output.metadata["failure_code"] == "ASSISTANT_DECISION_SOURCE"
        assert not await rows(ctx, PROPOSED)
    finally:
        await lease.release(session_status="idle")


async def test_active_decision_notes_are_never_dropped_to_fit_a_smaller_context(monkeypatch):
    ctx, lease, answer = await start("A" * 4000 + " Never publish without approval.")
    try:
        first, ctx, _ = await proposed(ctx, await arguments(ctx, answer, summary="Never publish without approval."))
        await consume_context(ctx)
        await finish(ctx, lease, answer, "Saved")
        ctx, lease, _ = await next_turn(ctx, "Continue")
        monkeypatch.setattr("assistant.projection.MAX_CONTEXT_CHARS", 2000)
        surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        projected = await project_main_messages(list(surface.messages), ctx=ctx)
        # The long original turn is dropped; the protected note is not.
        assert answer.parent_id not in {item.id for item in projected}
        assert [note["decision_id"] for note in block(projected, "assistant:current-decisions")["decisions"]] == [first]
        monkeypatch.setattr("assistant.projection.MAX_CONTEXT_CHARS", 200)
        with pytest.raises(AssistantError) as budget:
            await project_main_messages(list(surface.messages), ctx=ctx)
        assert budget.value.code == "ASSISTANT_CONTEXT_BUDGET"
        assert "active decision notes" in str(budget.value)
    finally:
        await lease.release(session_status="idle")


async def test_another_actor_and_unquoted_instructions_cannot_supply_a_decision():
    ctx, lease, answer = await start()
    other, other_lease, other_answer = await start("A different owner's private constraint")
    try:
        foreign = await arguments(other, other_answer)
        result, _, _ = await call_tool(ctx, "decisions.propose", foreign)
        assert result.metadata.get("error")
        own = await arguments(ctx, answer)
        own["source_refs"][0]["quote"] = "Publish everything automatically"
        await consume_context(ctx)
        result, _, _ = await call_tool(ctx, "decisions.propose", own)
        assert result.metadata["failure_code"] == "ASSISTANT_DECISION_SOURCE"
        assert not await rows(ctx, PROPOSED)
    finally:
        await lease.release(session_status="idle")
        await other_lease.release(session_status="idle")


async def test_changed_report_does_not_hide_a_committed_note_or_answer_and_its_old_read_is_not_replayed():
    from tests.unit.test_assistant_reads import read_turn
    from agent.loop import _to_llm_messages
    ctx, lease, answer, accepted, original_report = await read_turn()
    try:
        read, _, _ = await call_tool(ctx, "history.read", {"session_id": accepted["execution_session_id"],
                                                           "message_ids": [original_report.id]})
        assert "Browser verification is still untested" in read.output
        args = await arguments(ctx, answer, summary="NOTE_FROM_HUMAN_WORDS")
        first, ctx, _ = await proposed(ctx, args)
        await consume_context(ctx)
        await finish(ctx, lease, answer, "ANSWER_AFTER_READING_THE_REPORT")
        ctx, lease, _ = await next_turn(ctx, "What constraints are current?")
        async with get_db_session() as db:
            report_part = await db.scalar(select(Part).where(Part.message_id == original_report.id, Part.type == "text"))
            report_part.data = {**report_part.data, "text": "CHANGED_REPORT_TEXT"}
        # D1: changing a source neither hides the note nor rewrites the saved answer.
        assert [note["decision_id"] for note in (await context(ctx))["decisions"]] == [first]
        surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        projected = await project_main_messages(list(surface.messages), ctx=ctx)
        wire = json.dumps(_to_llm_messages(projected, assistant_projection_verified=True))
        assert "NOTE_FROM_HUMAN_WORDS" in wire and "ANSWER_AFTER_READING_THE_REPORT" in wire
        # The earlier history.read observation is not replayed; the model must read again.
        assert original_report.id not in wire and "CHANGED_REPORT_TEXT" not in wire
        stubbed = json.dumps(_to_llm_messages(list(surface.messages), assistant_projection_verified=True))
        assert "Browser verification is still untested" not in stubbed and "fresh_read_required" in stubbed
    finally:
        await lease.release(session_status="idle")


async def test_corrections_cannot_cross_task_scope_or_replace_the_same_note_twice_in_a_turn():
    from tests.unit.test_assistant_reads import read_turn
    ctx, lease, answer, accepted, _ = await read_turn()
    try:
        args = {**await arguments(ctx, answer), "task_id": accepted["task_id"]}
        first, ctx, _ = await proposed(ctx, args)
        await consume_context(ctx)
        await finish(ctx, lease, answer, "Saved task note")
        ctx, lease, answer = await next_turn(ctx, "Correction: use green instead of blue in that task.")
        correction = await arguments(ctx, answer, summary="Use green", supersedes=[first])
        await consume_context(ctx)
        rejected, _, _ = await call_tool(ctx, "decisions.propose", correction)
        assert rejected.metadata["failure_code"] == "ASSISTANT_DECISION_CONFLICT"
        correction["task_id"] = accepted["task_id"]
        _, ctx, _ = await proposed(ctx, correction)
        duplicate, _, _ = await call_tool(ctx, "decisions.propose", correction)
        assert duplicate.metadata["failure_code"] == "ASSISTANT_DECISION_CONFLICT"
    finally:
        await lease.release(session_status="idle")
