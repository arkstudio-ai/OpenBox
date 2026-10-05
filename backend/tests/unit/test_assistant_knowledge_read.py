"""Published body pages through real SQL, tool, processor and source boundaries."""
from copy import deepcopy
import json
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from assistant import business_context, knowledge
from assistant.commands import command_digest
from assistant.evidence import projection_digest, validate_message_sources
from assistant.policy import AssistantError
from db.base import get_db_session, get_engine
from db.models.agent_inbox import AgentInboxItem
from db.models.memory_v2 import MemorySource
from db.models.memory_wiki import MemoryWikiPage
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from memory import service as memory_service
from memory.redaction import redact_credentials, text_hash
from tests.unit.assistant_source_fixtures import consume_context
from tests.unit.test_assistant_context_sources import finish, next_turn
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_knowledge import page_for, seed
from tests.unit.test_assistant_knowledge_provenance import checkpoint, events, projected_request, revoke, start
from tests.unit.test_assistant_reads import call_tool


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    monkeypatch.setattr("agent.inbox.schedule_inbox_wake", lambda *_: None)


async def set_body(page, body):
    async with get_db_session() as db:
        row = await db.get(MemoryWikiPage, page.id)
        row.body, row.content_hash = body, text_hash(body)


async def reference(identity, page, **scope):
    directory = await knowledge.directory(**identity, **scope)
    return next(item["source_ref"] for item in directory["items"] if item["id"] == page.id)


@pytest.mark.parametrize("body", ["原始正文🙂" * 4500, "\x01" * 17000,
    "lead " + "password=fixture-secret-split-across-boundary " + "ending " * 1800],
    ids=["unicode", "escaped-control-byte-budget", "redact-before-pagination"])
async def test_body_pages_preserve_exact_redacted_bytes_with_no_writes(monkeypatch, body):
    identity, _, _, _ = await seed(monkeypatch)
    page, _ = await page_for(identity, None, "Page")
    await set_body(page, body)
    ref = await reference(identity, page)
    max_chars = 11 if body.startswith("lead ") else 16000
    # Keep the credential boundary proof small while still spanning a page.
    if max_chars == 11:
        body = body[:100]
        await set_body(page, body)
        ref = await reference(identity, page)
    statements, values = [], []
    def record(_conn, _cursor, statement, *_args):
        statements.append(statement)
    event.listen(get_engine().sync_engine, "before_cursor_execute", record)
    try:
        cursor = None
        while True:
            value = await knowledge.read(**identity, source_ref=ref, max_chars=max_chars, cursor=cursor)
            values.append(value)
            assert len(json.dumps(value, ensure_ascii=False).encode()) <= knowledge.MAX_RESPONSE_BYTES
            assert value["chunk_hash"] == text_hash(value["text"])
            assert value["item"]["source_ref"]["content_hash"] == text_hash(body)
            assert value["projection_hash"] == text_hash(redact_credentials(body))
            assert value["untrusted_data"] is True
            assert value["offset"] == sum(len(old["text"]) for old in values[:-1])
            assert value["end_offset"] - value["offset"] == len(value["text"]) <= max_chars
            cursor = value["next_cursor"]
            assert value["truncated"] is (cursor is not None)
            if not cursor:
                break
    finally:
        event.remove(get_engine().sync_engine, "before_cursor_execute", record)
    assert len(values) > 1 and "".join(value["text"] for value in values) == redact_credentials(body)
    assert values[-1]["end_offset"] == values[-1]["total_chars"]
    assert "fixture-secret-split" not in json.dumps(values)
    assert statements and not any(re.match(r"\s*(INSERT|UPDATE|DELETE|CREATE|DROP)\b", sql, re.I) for sql in statements)


async def test_read_scope_is_explicit_and_cursor_cannot_change_identity_scope_budget_or_version(monkeypatch):
    identity, other, projects, _ = await seed(monkeypatch)
    page, _ = await page_for(identity, projects[0], "Project source")
    personal, _ = await page_for(identity, None, "Personal source")
    foreign, _ = await page_for({**identity, "user_id": other}, projects[2], "FOREIGN_BODY")
    async with get_db_session() as db:
        # The storage project is deliberately the exact selected source project.
        (await db.get(Session, identity["main_id"])).project_id = projects[0]
    ref = await reference(identity, page, include_all_projects=True)
    with pytest.raises(AssistantError):
        await knowledge.read(**identity, source_ref=ref)
    first = await knowledge.read(**identity, source_ref=ref, project_id=projects[0], max_chars=3)
    assert first["text"] == "PRI"
    assert (await knowledge.read(**identity, source_ref=ref, include_all_projects=True))["text"].startswith("PRIVATE_BODY_")
    assert (await knowledge.read(**identity, source_ref=await reference(identity, personal)))["item"]["id"] == personal.id
    base = dict(identity, source_ref=ref, project_id=projects[0], max_chars=3, cursor=first["next_cursor"])
    for changed in ({"max_chars": 4}, {"project_id": None, "include_all_projects": True},
                    {"source_ref": await reference(identity, personal)}, {"project_id": projects[2]},
                    {"source_ref": {**ref, "id": foreign.id}}, {"source_ref": {**ref, "revision": True}},
                    {"source_ref": {**ref, "version": 2}}, {"source_ref": {**ref, "extra": "unbound"}}):
        with pytest.raises(AssistantError):
            await knowledge.read(**{**base, **changed})
    async with get_db_session() as db:
        (await db.get(MemoryWikiPage, page.id)).revision += 1
    with pytest.raises(AssistantError):
        await knowledge.read(**base)


async def test_first_oversized_metadata_cannot_escape_read_response_budget(monkeypatch):
    identity, _, _, _ = await seed(monkeypatch)
    page, _ = await page_for(identity, None, "Page")
    async with get_db_session() as db:
        (await db.get(MemoryWikiPage, page.id)).title = "\x01" * 160
    # Respect PostgreSQL's title width while making the first metadata frame
    # exceed the response's remaining budget, before any body bytes are added.
    monkeypatch.setattr(knowledge, "MAX_RESPONSE_BYTES", 4500)
    # Obtain the current ref without using the separately budgeted directory.
    async with get_db_session() as db:
        scope = await knowledge._access(db, **identity, project_id=None, include_all_projects=False)
        item = await knowledge._current_item(db, scope, identity["main_id"], await db.get(MemoryWikiPage, page.id), {})
    with pytest.raises(AssistantError) as invalid:
        await knowledge.read(**identity, source_ref=item["source_ref"])
    assert invalid.value.code == "ASSISTANT_CONTEXT_BUDGET"


@pytest.mark.parametrize("change", ["page_version", "page_hash", "source_revoked", "session_deleted",
    "source_identity", "project_deleted", "actor_removed", "forget_memory", "forget_source"])
async def test_changed_or_forgotten_original_source_blocks_old_and_continued_body(monkeypatch, change):
    from session.session import create_session
    identity, _, projects, _ = await seed(monkeypatch)
    page, note = await page_for(identity, projects[0], "SOURCE_BOUND_BODY")
    original = await create_session(user_id=identity["user_id"], workspace_id=identity["workspace_id"],
                                    project_id=projects[0])
    async with get_db_session() as db:
        (await db.get(MemorySource, page.source_manifest[0]["id"])).session_id = original.id
    args = dict(identity, source_ref=await reference(identity, page, project_id=projects[0]),
                project_id=projects[0], max_chars=5)
    first = await knowledge.read(**args)
    if change.startswith("forget"):
        result = await memory_service.forget_memory(user_id=identity["user_id"], workspace_id=identity["workspace_id"],
            memory_id=note["id"], expected_revision=note["revision"], mode="sources" if change == "forget_source" else "memory",
            source_ids=[page.source_manifest[0]["id"]] if change == "forget_source" else None)
        assert result["ok"]
    else:
        async with get_db_session() as db:
            if change == "page_version":
                (await db.get(MemoryWikiPage, page.id)).revision += 1
            elif change == "page_hash":
                (await db.get(MemoryWikiPage, page.id)).body = "Changed without updating its hash"
            elif change == "source_revoked":
                (await db.get(MemorySource, page.source_manifest[0]["id"])).status = "REVOKED"
            elif change == "source_identity":
                (await db.get(MemorySource, page.source_manifest[0]["id"])).source_metadata = {"changed": True}
            elif change == "session_deleted":
                (await db.get(Session, original.id)).is_deleted = True
            elif change == "project_deleted":
                (await db.get(Project, projects[0])).is_deleted = True
            else:
                (await db.get(WorkspaceMember, (identity["workspace_id"], identity["user_id"]))).status = "removed"
    for cursor in (None, first["next_cursor"]):
        with pytest.raises(AssistantError):
            await knowledge.read(**args, cursor=cursor)


async def test_body_observation_is_exact_even_if_its_digest_is_recomputed(monkeypatch):
    ctx, lease, _, identity, _, page = await start(monkeypatch)
    try:
        _, original = await business_context.capture(ctx, "knowledge.read",
            {"source_ref": await reference(identity, page), "max_chars": 5})
        for damage in ("text", "projection_hash", "cursor", "scope", "extra_proof"):
            changed = deepcopy(original)
            if damage == "text":
                changed["projection"]["text"] = "FAKED"
                changed["projection"]["chunk_hash"] = text_hash("FAKED")
            elif damage == "projection_hash":
                changed["projection"]["projection_hash"] = "a" * 64
            elif damage == "cursor":
                changed["projection"]["next_cursor"] = {"offset": 5}
            elif damage == "scope":
                changed["arguments"]["include_all_projects"] = True
            else:
                changed["sources"]["resources"][0]["unbound"] = "value"
            changed["digest"] = command_digest(changed["projection"])
            async with get_db_session() as db:
                with pytest.raises(AssistantError):
                    await business_context.validate(db, await db.get(Session, ctx.session_id), changed, fresh=True)
    finally:
        await lease.release(session_status="idle")


async def test_uploaded_document_read_checks_original_sections_even_without_a_wiki_version_change(monkeypatch):
    from db.models.memory_document import MemoryDocumentRevision
    from tests.unit.test_memory_documents import ingest
    identity, _, projects, config = await seed(monkeypatch)
    body = "# Uploaded document\n\nBODY_UPLOAD_CANARY is original reference data."
    document, _ = await ingest((identity["user_id"], identity["workspace_id"], projects[0], None, config.memory),
                              body, filename="body-read-fixture.txt")
    value = await knowledge.directory(**identity, include_all_projects=True)
    assert {item["id"] for item in value["items"]} == set(document.page_ids)
    ref = value["items"][0]["source_ref"]
    args = dict(identity, source_ref=ref, include_all_projects=True)
    assert "BODY_UPLOAD_CANARY" in (await knowledge.read(**args))["text"]
    async with get_db_session() as db:
        revision = await db.scalar(select(MemoryDocumentRevision).where(
            MemoryDocumentRevision.document_id == document.id, MemoryDocumentRevision.revision == document.revision))
        revision.sections = [{"title": "Replacement", "body": "Different original section without changing the Wiki row"}]
    with pytest.raises(AssistantError):
        await knowledge.read(**args)


async def test_reconciled_body_keeps_flattened_leaf_identity_binding(monkeypatch):
    from tests.unit.test_assistant_knowledge import corrected_page
    identity, page, leaf_id = await corrected_page(monkeypatch)
    args = dict(identity, source_ref=await reference(identity, page, include_all_projects=True), include_all_projects=True)
    assert (await knowledge.read(**args))["text"]
    async with get_db_session() as db:
        leaf = await db.get(MemorySource, leaf_id)
        leaf.source_metadata = {**leaf.source_metadata, "authority_binding": "changed-without-revision"}
    with pytest.raises(AssistantError):
        await knowledge.read(**args)


async def test_historical_body_pages_survive_transport_expiry_but_not_forgetting(monkeypatch):
    from assistant.public_history import public_messages
    from assistant.history import read_history
    from session.session import get_messages, get_session
    ctx, lease, answer, identity, _, page = await start(monkeypatch)
    try:
        ref = await reference(identity, page)
        first = await knowledge.read(**identity, source_ref=ref, max_chars=5)
        result, _, _ = await call_tool(ctx, "knowledge.read", {"source_ref": ref, "max_chars": 5,
                                                               "cursor": first["next_cursor"]})
        assert not result.metadata.get("error"), result.output
        await consume_context(ctx)
        await finish(ctx, lease, answer, "BODY_DERIVED_FIRST_ANSWER")
        later = knowledge.time.time() + knowledge.CURSOR_TTL + 1
        monkeypatch.setattr(knowledge, "time", SimpleNamespace(time=lambda: later))
        async with get_db_session() as db:
            await validate_message_sources(db, answer, user_id=ctx.user_id,
                workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        with pytest.raises(AssistantError) as expired:
            await knowledge.read(**identity, source_ref=ref, max_chars=5, cursor=first["next_cursor"])
        assert expired.value.code == "ASSISTANT_KNOWLEDGE_CURSOR"
        ctx, lease, derived = await next_turn(ctx)
        assert "BODY_DERIVED_FIRST_ANSWER" in json.dumps(await consume_context(ctx))
        await finish(ctx, lease, derived, "BODY_DERIVED_SECOND_ANSWER")
        main = await get_session(ctx.session_id, user_id=ctx.user_id)
        frozen = await get_messages(ctx.session_id, user_id=ctx.user_id)
        assert "BODY_DERIVED_SECOND_ANSWER" in json.dumps(await public_messages(main, frozen, actor_user_id=ctx.user_id), default=str)
        result = await memory_service.forget_memory(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
            memory_id=page.memory_manifest[0]["id"], mode="sources", source_ids=[page.source_manifest[0]["id"]])
        assert result["ok"]
        rendered = await public_messages(main, frozen, actor_user_id=ctx.user_id)
        assert "BODY_DERIVED_" not in json.dumps(rendered, default=str)
        for message in (answer, derived):
            assert next(item for item in rendered if item["id"] == message.id)["source_status"] == "unavailable"
        with pytest.raises(AssistantError):
            await read_history(**identity, session_id=ctx.session_id, message_ids=[answer.id, derived.id])
        ctx, lease, _ = await next_turn(ctx)
        assert "BODY_DERIVED_" not in json.dumps(await consume_context(ctx))
    finally:
        await lease.release(session_status="idle")


async def test_postgres_actual_checkpoint_cannot_reuse_held_body_authority(monkeypatch):
    from assistant import context_sources
    if get_engine().dialect.name != "postgresql":
        pytest.skip("Independent PostgreSQL writer during actual provider checkpoint")
    ctx, lease, _, identity, _, page = await start(monkeypatch)
    try:
        result, _, _ = await call_tool(ctx, "knowledge.read", {"source_ref": await reference(identity, page)})
        assert not result.metadata.get("error"), result.output
        surface, wire = await projected_request(ctx)
        assert "PRIVATE_BODY_" in json.dumps(wire)
        original = context_sources.checked_context_locked
        held_objects = []
        async def revoke_after_load(db, main, context, **kwargs):
            held = await db.get(MemorySource, page.source_manifest[0]["id"])
            held_objects.append(held)
            assert held.status == "ACTIVE"
            await revoke(page)
            assert held.status == "ACTIVE"
            return await original(db, main, context, **kwargs)
        monkeypatch.setattr(context_sources, "checked_context_locked", revoke_after_load)
        with pytest.raises(AssistantError):
            await checkpoint(ctx, surface)
        assert held_objects and not await events(ctx, "model.requested")
        assert not await events(ctx, "assistant.context.consumed")
    finally:
        await lease.release(session_status="idle")


async def test_body_derived_task_keeps_sources_and_cannot_run_after_revocation(monkeypatch):
    from agent.driver import reserve_run
    from assistant.scheduling import TaskSchedulingHeld, require_runnable
    from db.models.assistant import AssistantCommand
    from session.session import create_session
    ctx, lease, answer, identity, _, page = await start(monkeypatch)
    try:
        result, _, _ = await call_tool(ctx, "knowledge.read", {"source_ref": await reference(identity, page)})
        assert not result.metadata.get("error"), result.output
        await consume_context(ctx)
        result, _, _ = await call_tool(ctx, "tasks.submit", {"project_id": ctx.project_id,
            "title": "Body-derived text task", "instructions": "Draft text from the verified knowledge body; do not publish.",
            "source_message_ids": [answer.parent_id]})
        assert not result.metadata.get("error"), result.output
        receipt = json.loads(result.output)
        async with get_db_session() as db:
            command = await db.get(AssistantCommand, receipt["command_id"])
            observed = command.source_ref["derivation"]["business_reads"]
            assert len(observed) == 1 and observed[0]["operation"] == "knowledge.read"
            assert observed[0]["projection"]["item"]["source_ref"]["id"] == page.id
            assert (await db.get(Session, receipt["execution_session_id"])).memory_policy == "assistant_isolated"
        child = await create_session(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
                                     parent_id=receipt["execution_session_id"])
        await require_runnable(child.id, ctx.user_id)
        await revoke(page)
        with pytest.raises(TaskSchedulingHeld):
            await reserve_run(receipt["execution_session_id"], ctx.user_id)
        with pytest.raises(TaskSchedulingHeld):
            await require_runnable(child.id, ctx.user_id)
        async with get_db_session() as db:
            assert (await db.get(AgentInboxItem, receipt["inbox_id"])).state == "accepted"
    finally:
        await lease.release(session_status="idle")


async def test_body_derived_compaction_uses_real_provider_boundary_and_does_not_outlive_source(monkeypatch):
    from assistant.compaction import COMMITTED
    from tests.unit.test_assistant_compaction import compact
    ctx, lease, answer, identity, _, page = await start(monkeypatch)
    sent = []
    async def provider(**kwargs):
        sent.append(kwargs["messages"])
        yield {"type": "text_delta", "text": "BODY_DERIVED_SUMMARY"}
        yield {"type": "finish", "reason": "stop", "usage": {}}
    monkeypatch.setattr("agent.llm.stream_llm", provider)
    try:
        await call_tool(ctx, "knowledge.read", {"source_ref": await reference(identity, page)})
        await consume_context(ctx)
        await finish(ctx, lease, answer, "BODY_DERIVED_ORIGINAL")
        ctx, lease, answer = await next_turn(ctx, "Summarize the verified body evidence.")
        await compact(ctx)
        assert len(sent) == 1 and "BODY_DERIVED_ORIGINAL" in json.dumps(sent[0])
        saved = (await events(ctx, COMMITTED))[0]
        assert saved.payload["context"]["source_refs"]
        assert "BODY_DERIVED_SUMMARY" in json.dumps(await consume_context(ctx))
        await finish(ctx, lease, answer, "BODY_DERIVED_SUMMARIZED_ANSWER")
        await revoke(page)
        async with get_db_session() as db:
            summary = await db.get(Message, saved.message_id)
            with pytest.raises(AssistantError):
                await validate_message_sources(db, summary, user_id=ctx.user_id,
                    workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        ctx, lease, _ = await next_turn(ctx)
        assert "BODY_DERIVED_" not in json.dumps(await consume_context(ctx))
    finally:
        await lease.release(session_status="idle")


async def test_actual_loop_reads_directory_and_two_body_pages_into_exact_provider_requests(monkeypatch):
    from agent import processor
    from session.agent_event_log import verify_agent_event_parity
    from tests.unit.test_agent_loop_terminal_steps import _assert_balanced_steps
    from tests.unit.test_assistant_single_projection import _accept, _events, _run, _runtime
    state = await _runtime(monkeypatch)
    state.config.memory.wiki = state.config.memory.v2_write = True
    state.config.memory.automatic_knowledge = False
    state.config.memory.allowed_user_ids = [state.owner]
    state.config.jwt_secret = "knowledge-body-cursor-fixture"
    monkeypatch.setattr("assistant.knowledge.get_config", lambda: state.config)
    monkeypatch.setattr("assistant.history.get_config", lambda: state.config)
    identity = {"user_id": state.owner, "workspace_id": state.workspace, "main_id": state.main.id}
    page, _ = await page_for(identity, None, "Current published body")
    body = "BODY_ONLY_CANARY: evidence data. secret=fixture-credential-only\nSecond paragraph remains reference data."
    await set_body(page, body)
    calls, read_args, chunks = [], {}, []
    def output(kwargs, call_id):
        return json.loads(next(message["content"] for message in kwargs["messages"]
            if message["role"] == "tool" and message["tool_call_id"] == call_id))
    async def provider(**kwargs):
        calls.append(kwargs["messages"])
        number = len(calls)
        if number == 1:
            operation, args, call_id = "knowledge.directory", {}, "body-directory"
        elif number == 2:
            item = output(kwargs, "body-directory")["items"][0]
            assert item["id"] == page.id and "BODY_ONLY_CANARY" not in json.dumps(kwargs["messages"])
            read_args.update(source_ref=item["source_ref"], max_chars=60)
            operation, args, call_id = "knowledge.read", read_args, "body-first"
        elif number == 3:
            first = output(kwargs, "body-first")
            chunks.append(first["text"])
            assert first["untrusted_data"] is True and first["truncated"] is True
            operation, args, call_id = "knowledge.read", {**read_args, "cursor": first["next_cursor"]}, "body-second"
        elif number == 4:
            second = output(kwargs, "body-second")
            chunks.append(second["text"])
            assert second["next_cursor"] is None and "".join(chunks) == redact_credentials(body)
        else:
            assert "BODY_DERIVED_REPLY" not in json.dumps(kwargs["messages"])
            assert "BODY_ONLY_CANARY" not in json.dumps(kwargs["messages"])
        assert "fixture-credential-only" not in json.dumps(kwargs["messages"])
        if number <= 3:
            wire = next(name for name, tool in kwargs["tools"].items() if tool.id == operation)
            yield {"type": "tool_call", "tool": wire, "args": deepcopy(args), "call_id": call_id, "invalid": False}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
        else:
            yield {"type": "text_delta", "text": "BODY_DERIVED_REPLY" if number == 4 else "The original source is unavailable."}
            yield {"type": "finish", "reason": "stop", "usage": {}}
    monkeypatch.setattr(processor, "stream_llm", provider)
    accepted = await _accept(state, "Read the current published knowledge document completely; only answer with text.")
    await _run(state)
    assert len(calls) == 4
    requests, consumed = await _events(state, "model.requested"), await _events(state, "assistant.context.consumed")
    assert len(requests) == len(consumed) == 4
    assert [entry.payload["request_sequence"] for entry in consumed] == [entry.sequence for entry in requests]
    for request, payload in zip(requests, calls):
        assert request.payload["assistant_context"]["messages_digest"] == projection_digest(payload)
    observed = requests[-1].payload["assistant_context"]["business_reads"]
    reads = [entry for entry in observed if entry["operation"] == "knowledge.read"]
    assert len(reads) == 2 and all(entry["projection"]["item"]["source_ref"]["id"] == page.id for entry in reads)
    assert "".join(entry["projection"]["text"] for entry in sorted(reads, key=lambda value: value["projection"]["offset"])) == redact_credentials(body)
    async with get_db_session() as db:
        receipt = await db.get(AgentInboxItem, accepted["inbox_id"])
        assert receipt.state == "settled" and receipt.outcome == "succeeded"
        answer = await db.get(Message, receipt.result_message_id)
        await validate_message_sources(db, answer, user_id=state.owner, workspace_id=state.workspace, main_id=state.main.id)
        parts = (await db.scalars(select(Part).where(Part.session_id == state.main.id, Part.type == "tool"))).all()
        assert len(parts) == 3 and all("BODY_ONLY_CANARY" not in json.dumps(part.data) for part in parts)
        assert (await db.get(Session, state.main.id)).memory_policy == "assistant_isolated"
    await _assert_balanced_steps(state.main.id, state.owner, 4)
    assert (await verify_agent_event_parity(state.main.id, user_id=state.owner)).ok
    await revoke(page)
    await _accept(state, "What was verified in the prior reply?")
    await _run(state)
    assert len(calls) == 5


@pytest.mark.parametrize("mode", ["report_only", "coordination"])
async def test_read_does_not_open_restricted_report_or_coordination_modes(monkeypatch, mode):
    if mode == "report_only":
        from tests.unit.test_assistant_reporting import prepare_report
        ctx, lease, *_ = await prepare_report()
    else:
        from tests.unit.test_assistant_continuation import coordinator, ready
        values, task, result_id = await ready(monkeypatch)
        ctx, lease, _ = await coordinator(values, task, result_id, read=False)
    # A structurally valid reference is insufficient to grant a mode access.
    ref = {"version": 1, "kind": "wiki", "id": "unselected", "assistant_session_id": ctx.session_id,
        "user_id": ctx.user_id, "workspace_id": ctx.workspace_id, "project_id": None, "visibility": "PERSONAL",
        "revision": 1, "content_hash": "a" * 64, "metadata_hash": "b" * 64,
        "dependencies_hash": "c" * 64, "acl_epoch": 1}
    try:
        result, _, _ = await call_tool(ctx, "knowledge.read", {"source_ref": ref})
        assert result.metadata["failure_code"] == "ASSISTANT_TOOL_FORBIDDEN"
        assert not await events(ctx, "assistant.business.read")
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("isolated", [False, True])
async def test_body_tool_preserves_normal_chat_and_inherited_memory_policy(monkeypatch, isolated):
    from memory.policy import MemoryAccessDenied
    from memory.session_policy import require_context_memory
    from session.session import create_session
    from tests.unit.test_assistant_reads import TOOLS
    from tool.tool import ToolContext
    identity, _, _, _ = await seed(monkeypatch)
    page, _ = await page_for(identity, None, "Private background")
    ref = await reference(identity, page)
    session = await create_session(user_id=identity["user_id"], workspace_id=identity["workspace_id"],
        visibility="private", memory_policy="assistant_isolated" if isolated else "standard")
    ctx = ToolContext(user_id=session.user_id, workspace_id=session.workspace_id, session_id=session.id,
        project_id=session.project_id, agent_id="assistant")
    result = await TOOLS["knowledge.read"].execute({"source_ref": ref}, ctx)
    assert result.metadata.get("error"), result.output
    if isolated:
        with pytest.raises(MemoryAccessDenied):
            await require_context_memory(ctx)
    else:
        assert (await require_context_memory(ctx)).id == session.id
