"""Uploaded original text through real ingestion, retrieval and assistant SQL.

Blob storage, embedding/Qdrant HTTP and external model IO are local substitutes.
Parsing, admission, scopes, observations, processor and receipts are production.
V2 (PERSONAL_ASSISTANT_DESIGN_V2.md 8.4, D1): every read checks current document
authority; earlier observations and saved answers are not re-validated.
"""
from copy import deepcopy
import json
import re

import pytest
from sqlalchemy import event, select

from assistant import memory
from assistant.policy import AssistantError
from db.base import get_db_session, get_engine
from db.models.agent_inbox import AgentInboxItem
from db.models.memory_document import MemoryDocument, MemoryDocumentRevision
from db.models.memory_v2 import MemorySource, MemoryTombstone
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from memory import retrieval
from memory.documents import service as documents
from memory.documents.worker import MemoryDocumentWorker
from memory.redaction import redact_credential_ranges, redact_credentials, text_hash
from memory.wiki import editing
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_memory_reads import configure, external_io, no_dispatch, seed, start  # noqa: F401
from tests.unit.test_assistant_reads import call_tool
from tests.unit.test_memory_documents import Blob, ingest


class Store(Blob):
    async def delete(self, key):
        self.files.pop(key, None)

    async def exists(self, key):
        return key in self.files


async def upload(identity, config, body="DOC_CANARY This uploaded rule belongs to the named venue, not the user.", project_id=None):
    data = (identity["user_id"], identity["workspace_id"], project_id, None, config.memory)
    document, store = await ingest(data, body, filename="原始运营手册.txt", store=Store())
    assert document.status == "READY", document.reason_code
    return document, store


def hit(document):
    return {"kind": "source", "id": document.source_ids[0], "revision": document.revision}


async def selected(identity, document, **scope):
    found = await memory.search(**identity, query="DOC_CANARY", **scope)
    return next(item["source_ref"] for item in found["items"] if item["id"] == document.source_ids[0])


async def test_uploaded_hybrid_candidates_preserve_personal_owned_scope_and_ordinary_retrieval(monkeypatch, external_io):
    identity, other, projects, config = await seed(monkeypatch)
    personal, _ = await upload(identity, config, "Personal pottery notes")
    a, _ = await upload(identity, config, "Amber venue rules", projects[0])
    b, _ = await upload(identity, config, "Blue venue rules", projects[1])
    foreign, _ = await upload({**identity, "user_id": other}, config, "FOREIGN_FILE_CANARY", projects[2])
    external_io.hits = [hit(d) for d in (personal, a, b, foreign)]
    async with get_db_session() as db:
        (await db.get(Session, identity["main_id"])).project_id = projects[0]
    async def no_sql_wait(_kind):
        assert get_engine().sync_engine.pool.checkedout() == 0
    external_io.before = no_sql_wait
    plain = await memory.search(**identity, query="semantic no lexical overlap")
    assert {item["id"] for item in plain["items"]} == {personal.source_ids[0]}
    chosen = await memory.search(**identity, query="semantic no lexical overlap", project_id=projects[0])
    assert {item["id"] for item in chosen["items"]} == {personal.source_ids[0], a.source_ids[0]}
    every = await memory.search(**identity, query="semantic no lexical overlap", include_all_projects=True)
    assert {item["id"] for item in every["items"]} == {personal.source_ids[0], a.source_ids[0], b.source_ids[0]}
    assert foreign.id not in json.dumps(every) and "FOREIGN_FILE_CANARY" not in json.dumps(every)
    assert every["search"]["bounded"] and not every["search"]["exhaustive"]
    assert all(item["category"] == "DOCUMENT" and item["sources"][0]["origin"] == "uploaded_file" for item in every["items"])
    ordinary = await retrieval.search_memory(user_id=identity["user_id"], workspace_id=identity["workspace_id"],
        query="pottery", config=config.memory)
    legacy = next(item for item in ordinary["items"] if item["id"] == personal.source_ids[0])
    assert legacy["kind"] == "source" and legacy["confirmation_status"] == "UPLOADED_DOCUMENT"
    with pytest.raises(AssistantError):
        await memory.read(**identity, source_ref=next(i["source_ref"] for i in every["items"] if i["id"] == b.source_ids[0]))


async def test_full_context_redaction_before_original_chunk_and_read_pagination(monkeypatch, external_io):
    identity, _, _, config = await seed(monkeypatch)
    config.memory.source_chunk_chars = 200
    secret = "LongCredentialFragment" * 20
    body = "DOC_CANARY " + "公开文本。" * 24 + "password=" + secret + "\n末尾仅属于上传文档。"
    document, _ = await upload(identity, config, body)
    external_io.hits = [{"kind": "source", "id": source_id, "revision": document.revision} for source_id in document.source_ids]
    found = await memory.search(**identity, query="DOC_CANARY", limit=20)
    assert {item["id"] for item in found["items"]} == set(document.source_ids)
    projections = []
    for item in found["items"]:
        source = item["sources"][0]
        span = source["source_span"]
        assert span["total_chars"] == len(body) and not span["complete"]
        expected = redact_credential_ranges(body, [(span["start"], span["end"])])[0]
        chunks, cursor = [], None
        while True:
            page = await memory.read(**identity, source_ref=item["source_ref"], max_chars=7, cursor=cursor)
            chunks.append(page["text"])
            assert page["chunk_hash"] == text_hash(page["text"]) and page["projection_hash"] == text_hash(expected)
            assert page["item"]["source_ref"]["content_hash"] == text_hash(body[span["start"]:span["end"]])
            assert page["offset"] == sum(len(chunk) for chunk in chunks[:-1])
            assert len(json.dumps(page, ensure_ascii=False).encode()) <= memory.MAX_RESPONSE_BYTES
            cursor = page["next_cursor"]
            assert page["truncated"] is (cursor is not None)
            if cursor is None:
                assert page["end_offset"] == page["total_chars"]
                break
        assert "".join(chunks) == expected
        projections.extend(chunks)
    assert "LongCredentialFragment" not in json.dumps(found) + "".join(projections)
    assert "redacted" in "".join(projections)
    # The same mapper handles parser-section boundaries and runtime secrets;
    # ordinary whole-text projection remains identical to its existing output.
    monkeypatch.setenv("DOCUMENT_TEST_API_KEY", "runtime-token-cross-boundary")
    original = "前 runtime-token-cross-boundary 后 Bearer abcdefghijklmnop 终 secret=anotherlongvalue"
    ranges = [(i, i + 1) for i in range(len(original))]
    pieces = redact_credential_ranges(original, ranges)
    assert all("runtime-token" not in part and "abcdefghijklmnop" not in part and "anotherlongvalue" not in part for part in pieces)
    for token, replacement in (("runtime-token-cross-boundary", "[redacted]"),
                               ("abcdefghijklmnop", "Bearer [redacted]"), ("anotherlongvalue", "secret=[redacted]")):
        start = original.index(token)
        assert pieces[start:start + len(token)] == [replacement] * len(token)
    assert redact_credential_ranges(original, [(0, len(original))]) == [redact_credentials(original)]


@pytest.mark.parametrize("change", ["revoked", "tombstone", "body", "span", "page", "metadata", "document_hash",
                                    "revision_sections", "project", "actor", "scope", "disabled", "delete"])
async def test_current_document_authority_blocks_the_old_reference_and_cursor(monkeypatch, external_io, change):
    identity, _, projects, config = await seed(monkeypatch)
    document, store = await upload(identity, config, project_id=projects[0])
    ref = await selected(identity, document, project_id=projects[0])
    args = {"source_ref": ref, "project_id": projects[0], "max_chars": 8}
    value = await memory.read(**identity, **args)
    assert value["text"] == "DOC_CANA" and value["next_cursor"]
    if change == "delete":
        removed = await documents.delete(user_id=identity["user_id"], workspace_id=identity["workspace_id"],
                                         document_id=document.id, store=store)
        assert removed["status"] == "deleted" and removed["original_cleanup"] == "done"
    elif change == "disabled":
        config.memory.wiki = False
    else:
        async with get_db_session() as db:
            source = await db.get(MemorySource, ref["id"])
            doc = await db.get(MemoryDocument, document.id)
            if change == "revoked": source.status = "REVOKED"
            elif change == "tombstone":
                db.add(MemoryTombstone(id="forget-" + source.id, user_id=identity["user_id"],
                    workspace_id=identity["workspace_id"], project_id=projects[0], object_kind="source", object_id=source.id,
                    revision=source.source_revision, deleted_at=doc.updated_at))
            elif change == "body":
                source.body = "coherent new text absent from the immutable parsed section"
                source.content_hash = text_hash(source.body)
            elif change == "span": source.source_metadata = {**source.source_metadata, "start": 1}
            elif change == "page": source.source_metadata = {**source.source_metadata, "page_id": "other-page"}
            elif change == "metadata": source.source_metadata = {**source.source_metadata, "changed_origin": True}
            elif change == "document_hash": doc.content_hash = "f" * 64
            elif change == "revision_sections":
                revision = await db.scalar(select(MemoryDocumentRevision).where(MemoryDocumentRevision.document_id == doc.id))
                revision.sections = [{**revision.sections[0], "body": "changed immutable data"}]
            elif change == "project": (await db.get(Project, projects[0])).is_deleted = True
            elif change == "actor":
                member = await db.scalar(select(WorkspaceMember).where(WorkspaceMember.workspace_id == identity["workspace_id"],
                                                                       WorkspaceMember.user_id == identity["user_id"]))
                member.status = "suspended"
            elif change == "scope": source.project_id = projects[1]
    calls = len(external_io.calls)
    # Every later read, first page or continuation, checks current authority.
    for cursor in (None, value["next_cursor"]):
        with pytest.raises(AssistantError):
            await memory.read(**identity, **args, cursor=cursor)
    assert len(external_io.calls) == calls


async def test_document_edit_replaces_chunks_without_reauthorizing_old_refs(monkeypatch, external_io):
    identity, _, projects, config = await seed(monkeypatch)
    document, store = await upload(identity, config, project_id=projects[0])
    ref = await selected(identity, document, project_id=projects[0])
    before = await editing.snapshot(user_id=identity["user_id"], workspace_id=identity["workspace_id"], page_id=document.page_ids[0])
    updated = "DOC_CANARY Corrected venue rule has a different parsed-text length."
    result = await editing.save(user_id=identity["user_id"], workspace_id=identity["workspace_id"], page_id=document.page_ids[0],
        expected_revision=before["revision"], content_hash=before["content_hash"], title="修正运营手册",
        entries=[{**before["entries"][0], "text": updated}], request_id="assistant-document-edit")
    assert result["status"] == "updating"
    with pytest.raises(AssistantError): await memory.read(**identity, source_ref=ref, project_id=projects[0])
    assert await MemoryDocumentWorker(config.memory, store=store).run_once()
    async with get_db_session() as db: document = await db.get(MemoryDocument, document.id)
    newer = await selected(identity, document, project_id=projects[0])
    current = await memory.read(**identity, source_ref=newer, project_id=projects[0])
    assert current["text"] == updated and current["item"]["sources"][0]["document_origin"] == "user_edit"
    assert newer["revision"] == ref["revision"] + 1 and newer["id"] != ref["id"]
    with pytest.raises(AssistantError): await memory.read(**identity, source_ref=ref, project_id=projects[0])


async def test_document_cursor_scope_and_sql_only_continuation(monkeypatch, external_io):
    identity, _, projects, config = await seed(monkeypatch)
    document, _ = await upload(identity, config, project_id=projects[0])
    ref = await selected(identity, document, include_all_projects=True)
    args = {"source_ref": ref, "project_id": projects[0], "max_chars": 8}
    first = await memory.read(**identity, **args)
    second_args = {**args, "cursor": first["next_cursor"]}
    for changed in ({"max_chars": 9}, {"project_id": None, "include_all_projects": True},
                    {"source_id": ref["id"]}, {"source_ref": {**ref, "kind": "memory"}}):
        with pytest.raises(AssistantError): await memory.read(**identity, **{**second_args, **changed})
    statements, calls = [], len(external_io.calls)
    def record(_conn, _cursor, sql, *_args): statements.append(sql)
    event.listen(get_engine().sync_engine, "before_cursor_execute", record)
    try:
        second = await memory.read(**identity, **second_args)
        assert second["offset"] == len(first["text"])
        future = memory.time.time() + memory.CURSOR_TTL + 10
        monkeypatch.setattr(memory.time, "time", lambda: future)
        with pytest.raises(AssistantError): await memory.read(**identity, **second_args)
    finally:
        event.remove(get_engine().sync_engine, "before_cursor_execute", record)
    assert len(external_io.calls) == calls and statements
    assert not any(re.match(r"\s*(INSERT|UPDATE|DELETE|CREATE|DROP)\b", sql, re.I) for sql in statements)


async def test_document_revoked_during_embedding_is_not_returned(monkeypatch, external_io):
    identity, _, _, config = await seed(monkeypatch)
    document, _ = await upload(identity, config)
    external_io.hits = [hit(document)]
    async def revoke(_kind):
        async with get_db_session() as db:
            (await db.get(MemorySource, document.source_ids[0])).status = "REVOKED"
    external_io.before = revoke
    found = await memory.search(**identity, query="DOC_CANARY")
    assert found["items"] == [] and "DOC_CANARY" not in json.dumps(found)


async def test_document_derived_task_records_no_derivation_and_deletion_is_not_retroactive(monkeypatch, external_io):
    from agent.driver import reserve_run
    from assistant.scheduling import require_runnable
    from db.models.assistant import AssistantCommand
    from session.session import create_session
    from tests.unit.assistant_source_fixtures import consume_context
    identity, _, _, config = await seed(monkeypatch)
    document, store = await upload(identity, config)
    ctx, lease, answer = await start(identity)
    try:
        result, _, _ = await call_tool(ctx, "memory.search", {"query": "DOC_CANARY"})
        assert not result.metadata.get("error"), result.output
        ref = json.loads(result.output)["items"][0]["source_ref"]
        result, _, _ = await call_tool(ctx, "memory.read", {"source_ref": ref})
        assert not result.metadata.get("error"), result.output
        await consume_context(ctx)
        result, _, _ = await call_tool(ctx, "tasks.submit", {"project_id": ctx.project_id,
            "title": "Document-derived draft", "instructions": "Draft a private summary with file attribution.",
            "source_message_ids": [answer.parent_id]})
        assert not result.metadata.get("error"), result.output
        receipt = json.loads(result.output)
        async with get_db_session() as db:
            command = await db.get(AssistantCommand, receipt["command_id"])
            # V2 records the triggering human message, not a derivation proof.
            assert "derivation" not in command.source_ref and "DOC_CANARY" not in json.dumps(command.source_ref)
            execution = await db.get(Session, receipt["execution_session_id"])
            assert execution.memory_policy == "assistant_isolated" and execution.parent_id is None
        child = await create_session(user_id=ctx.user_id, workspace_id=ctx.workspace_id, parent_id=execution.id)
        await require_runnable(child.id, ctx.user_id)
        assert (await documents.delete(user_id=ctx.user_id, workspace_id=ctx.workspace_id, document_id=document.id, store=store))["ok"]
        # D1: deleting the upload affects later reads, not accepted work.
        with pytest.raises(AssistantError): await memory.read(**identity, source_ref=ref)
        await require_runnable(child.id, ctx.user_id)
        claimed = await reserve_run(execution.id, ctx.user_id)
        await claimed.release(session_status="idle")
        async with get_db_session() as db:
            assert (await db.get(AgentInboxItem, receipt["inbox_id"])).state == "accepted"
    finally:
        await lease.release(session_status="idle")


async def test_actual_loop_reads_document_pages_and_keeps_the_derived_answer_after_delete(monkeypatch, external_io, record_property):
    from agent import processor
    from assistant.public_history import public_messages
    from session.agent_event_log import verify_agent_event_parity
    from session.session import get_messages
    from tests.unit.test_agent_loop_terminal_steps import _assert_balanced_steps
    from tests.unit.assistant_helpers import _accept, _events, _run, _runtime
    state = await _runtime(monkeypatch)
    configure(monkeypatch, state.config)
    state.config.memory.wiki = True
    state.config.memory.allowed_user_ids = [state.owner]
    identity = {"user_id": state.owner, "workspace_id": state.workspace, "main_id": state.main.id}
    body = "DOC_CANARY This venue closes on Mondays. An uploaded file is third-party reference material."
    document, store = await upload(identity, state.config, body)
    external_io.hits = [hit(document)]
    calls, args, chunks = [], {}, []
    def output(kwargs, call_id):
        return json.loads(next(message["content"] for message in kwargs["messages"]
            if message["role"] == "tool" and message["tool_call_id"] == call_id))
    async def provider(**kwargs):
        calls.append(kwargs["messages"])
        number = len(calls)
        if number == 1:
            operation, arguments, call_id = "memory.search", {"query": "semantic unmatched"}, "document-search"
        elif number == 2:
            item = output(kwargs, "document-search")["items"][0]
            assert item["kind"] == "source" and item["sources"][0]["document_id"] == document.id
            args.update(source_ref=item["source_ref"], max_chars=50)
            operation, arguments, call_id = "memory.read", args, "document-first"
        elif number == 3:
            first = output(kwargs, "document-first")
            chunks.append(first["text"])
            assert first["item"]["sources"][0]["origin"] == "uploaded_file" and first["untrusted_data"] is True
            operation, arguments, call_id = "memory.read", {**args, "cursor": first["next_cursor"]}, "document-second"
        elif number == 4:
            second = output(kwargs, "document-second")
            chunks.append(second["text"])
            assert second["next_cursor"] is None and "".join(chunks) == body
        else:
            # D1: the saved reply stays; the deleted upload's text is not replayed.
            assert "DOC_CANARY" not in json.dumps(kwargs["messages"])
            assert "DOCUMENT_DERIVED_REPLY" in json.dumps(kwargs["messages"])
        if number <= 3:
            wire = next(name for name, tool in kwargs["tools"].items() if tool.id == operation)
            yield {"type": "tool_call", "tool": wire, "args": deepcopy(arguments), "call_id": call_id, "invalid": False}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
        else:
            yield {"type": "text_delta", "text": "DOCUMENT_DERIVED_REPLY" if number == 4 else "The uploaded source is unavailable."}
            yield {"type": "finish", "reason": "stop", "usage": {}}
    monkeypatch.setattr(processor, "stream_llm", provider)
    accepted = await _accept(state, "Find my uploaded venue document and read its text completely.")
    await _run(state)
    requested = await _events(state, "model.requested")
    assert len(calls) == len(requested) == 4
    # V2 checkpoints record the turn mode only; nothing is captured for re-validation.
    assert [entry.payload["assistant_context"] for entry in requested] == [{"version": 2, "mode": "ordinary"}] * 4
    assert not await _events(state, "assistant.context.consumed")
    async with get_db_session() as db:
        receipt = await db.get(AgentInboxItem, accepted["inbox_id"])
        assert receipt.state == "settled" and receipt.outcome == "succeeded"
        answer = await db.get(Message, receipt.result_message_id)
        assert answer.finish == "stop" and not answer.error
        parts = list((await db.scalars(select(Part).where(Part.session_id == state.main.id, Part.type == "tool"))).all())
        assert len(parts) == 3 and all("transient_assistant_refs" not in json.dumps(part.data) for part in parts)
        record_property("document_loop_receipt", json.dumps({"document": document.id, "revision": document.revision,
            "source": document.source_ids[0], "inbox": receipt.id, "input": receipt.message_id,
            "result": receipt.result_message_id, "attempts": receipt.delivery_attempts,
            "run": receipt.run_id, "generation": receipt.generation, "requested": 4,
            "tool_parts": 3, "read_pages": 2, "chars": len(body)}))
    await _assert_balanced_steps(state.main.id, state.owner, 4)
    assert (await verify_agent_event_parity(state.main.id, user_id=state.owner)).ok
    assert (await documents.delete(user_id=state.owner, workspace_id=state.workspace, document_id=document.id, store=store))["ok"]
    messages = await get_messages(state.main.id, user_id=state.owner)
    public = await public_messages(state.main, messages, actor_user_id=state.owner)
    assert "DOCUMENT_DERIVED_REPLY" in json.dumps(public) and "source_status" not in json.dumps(public)
    network_calls = len(external_io.calls)
    await _accept(state, "What can still be verified from the prior reply?")
    await _run(state)
    assert len(calls) == 5 and len(external_io.calls) == network_calls
