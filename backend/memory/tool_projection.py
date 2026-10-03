"""Rematerialize temporary memory-tool evidence without changing chat history.

The canonical ToolPart remains the actual original output. Main-model replay
uses freshly authorized SQL versions only within the same logical turn. Later
turns and compaction retain citations and a fresh-read marker, never the body.
"""
from copy import deepcopy
import json

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from db.base import get_db_session
from db.models.agent_driver import AgentDriverState
from db.models.session import Session
from memory.policy import MemoryAccessDenied, resolve_access_scope
from memory.presentation import document_item, model_item
from memory.redaction import redact_text, redact_value

TRANSIENT_TOOL_IDS = frozenset({"memory_search", "memory_read_sources", "current_task_state"})


def _dump(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def _trusted_operation(part: dict) -> str | None:
    identity = part.get("canonical_tool_id")
    if identity is None:
        identity = part.get("tool")
    return identity if isinstance(identity, str) and identity in TRANSIENT_TOOL_IDS else None


def _is_transient(part: dict) -> bool:
    metadata = part.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    return (part.get("type") == "tool" and
            (_trusted_operation(part) is not None or
             isinstance(metadata.get("transient_memory_refs"), dict)))


def _replace_output(part: dict, value: dict, *, verified: bool, citations_only: bool = False) -> dict:
    replacement = deepcopy(part)
    replacement["output"] = _dump(value)
    replacement["error"] = None
    metadata = replacement.get("metadata") or {}
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    # This marker is an ephemeral projection field, absent from the processor
    # persistence allowlist and ignored without the explicit serializer flag.
    metadata["_memory_projection_verified"] = verified
    replacement["metadata"] = metadata
    state = replacement.get("state")
    if citations_only and value.get("operation", part.get("tool")) == "memory_search":
        # A model may have copied a recalled phrase into its next search
        # query. Such arguments are temporary evidence too, not a back door
        # for old body text to enter a later turn or permanent summary.
        replacement["input"] = {"query": "[temporary query omitted; fresh authorized read required]"}
    if isinstance(state, dict):
        state["output"] = replacement["output"]
        state["error"] = None
        prior_metadata = state.get("metadata") or {}
        state["metadata"] = {**(prior_metadata if isinstance(prior_metadata, dict) else {}), "_memory_projection_verified": verified}
        if citations_only and value.get("operation", part.get("tool")) == "memory_search":
            state["input"] = replacement["input"]
    return replacement


def guard_transient_memory_part(part: dict, *, allow_revalidated: bool = False) -> dict:
    """Synchronous serializers fail closed unless their async caller checked SQL."""
    if not _is_transient(part):
        return part
    metadata = part.get("metadata") or {}
    if allow_revalidated and isinstance(metadata, dict) and metadata.get("_memory_projection_verified") is True:
        return part
    return _replace_output(part, {"status": "fresh_read_required", "untrusted_data": True,
                                  "operation": part.get("tool"), "reason_code": "temporary_result_not_revalidated",
                                  "instruction": "Original memory tool text is temporary. Read current authorized evidence if needed."}, verified=False, citations_only=True)


def _part_dict(part):
    if isinstance(part, dict):
        return deepcopy(part)
    result = part.model_dump()
    for hidden in ("canonical_tool_id", "wire_tool_name", "provider_binding_digest", "provider_dialect", "stream_seq"):
        value = getattr(part, hidden, None)
        if value is not None:
            result[hidden] = value
    return result


async def revalidate_memory_tool_messages(messages: list, *, ctx=None, user_id: str | None = None,
                                           workspace_id: str | None = None, project_id: str | None = None,
                                           session_id: str | None = None, logical_turn_id: str | None = None,
                                           for_compaction: bool = False) -> list:
    """Return detached MessageWithParts; never update canonical events/Parts."""
    from core.config import get_config
    from memory.retrieval import authorized_documents, read_task_state
    from memory.service import read_source_in_scope

    detached = deepcopy(messages)
    for message in detached:
        message.parts = [_part_dict(part) for part in message.parts or []]
    targets = [(message, index, part) for message in detached for index, part in enumerate(message.parts)
               if _is_transient(part)]
    if not targets:
        return detached
    if ctx is not None:
        user_id, workspace_id, project_id, session_id = ctx.user_id, ctx.workspace_id, ctx.project_id or None, ctx.session_id
    settings = get_config().memory
    access = None
    try:
        if not user_id or not workspace_id or not session_id:
            raise MemoryAccessDenied("Projection identity is not available")
        async with get_db_session() as db:
            session = await db.scalar(select(Session).where(Session.id == session_id, Session.user_id == user_id,
                Session.workspace_id == workspace_id, Session.is_deleted.is_(False)))
            if session is None or session.project_id != project_id:
                raise MemoryAccessDenied("Projection identity is not available")
            access = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id)
            if ctx is not None and getattr(ctx, "run_id", None):
                driver = await db.get(AgentDriverState, session_id)
                if (driver is None or driver.user_id != user_id or driver.run_id != ctx.run_id
                        or driver.generation != ctx.run_generation or driver.phase == "idle"):
                    raise MemoryAccessDenied("Projection identity is not available")
                logical_turn_id = driver.trigger_message_id
    except (MemoryAccessDenied, SQLAlchemyError):
        access = None

    for message, index, part in targets:
        metadata = part.get("metadata") or {}
        metadata = (metadata.get("transient_memory_refs") or {}) if isinstance(metadata, dict) else {}
        if not isinstance(metadata, dict):
            metadata = {}
        operation = metadata.get("operation") or part.get("tool")
        marker = {"operation": operation, "status": "unavailable", "untrusted_data": True,
                  "references": [], "instruction": "Memory tool text is temporary. Use a fresh read for facts; citations alone do not establish a fact."}
        if access is None or not settings.enabled("retrieval_v2", user_id):
            marker["reason_code"] = "scope_unavailable"
            message.parts[index] = _replace_output(part, marker, verified=True, citations_only=True)
            continue
        refs = metadata.get("references")
        if (metadata.get("version") != 1 or metadata.get("session_id") != session_id
                or operation != _trusted_operation(part) or not isinstance(refs, list) or len(refs) > 12):
            marker["reason_code"] = "unversioned_temporary_result"
            message.parts[index] = _replace_output(part, marker, verified=True, citations_only=True)
            continue
        same_turn = bool(logical_turn_id and metadata.get("logical_turn_id") == logical_turn_id)
        references = []
        items = []
        try:
            async with get_db_session() as db:
                current_scope = await resolve_access_scope(db, user_id=user_id, workspace_id=workspace_id, project_id=project_id)
                if operation == "memory_read_sources":
                    remaining = max(0, min(8000, int(metadata.get("max_chars", 4000))))
                    offset = max(0, min(32000, int(metadata.get("offset", 0))))
                    for ref in refs:
                        if not isinstance(ref, dict) or ref.get("kind") != "source" or type(ref.get("revision")) is not int:
                            continue
                        row = await read_source_in_scope(db, access=current_scope, source_id=str(ref.get("id") or ""),
                                                         source_revision=ref["revision"])
                        if not row.get("available"):
                            continue
                        references.append({"kind": "source", "id": row["id"], "revision": row["source_revision"]})
                        if same_turn and not for_compaction:
                            body = row.get("body") or ""
                            section = body[offset:offset + remaining]
                            remaining -= len(section)
                            text = redact_text(section, len(section))
                            items.append({"source_id": row["id"], "revision": row["source_revision"],
                                          "available": True, "text": text, "offset": offset,
                                          "redacted": text != section, "truncated": offset + len(section) < len(body),
                                          "content_hash": row["content_hash"],
                                          "source_kind": row["source_kind"], "session_id": row["session_id"],
                                          "message_id": row["message_id"], "untrusted_data": True})
                elif operation == "memory_search":
                    identities = {(ref["kind"], ref["id"]) for ref in refs if isinstance(ref, dict)
                                  and ref.get("kind") in {"memory", "source", "wiki"} and isinstance(ref.get("id"), str)}
                    docs = await authorized_documents(db, current_scope, settings, only=identities)
                    expected = {(ref["kind"], ref["id"]): ref.get("revision") for ref in refs
                                if isinstance(ref, dict) and "kind" in ref and "id" in ref}
                    for doc in docs:
                        if expected.get((doc.kind, doc.id)) != doc.revision:
                            continue
                        references.append({"kind": doc.kind, "id": doc.id, "revision": doc.revision})
                        if same_turn and not for_compaction:
                            items.append({**model_item(document_item(doc)),
                                          "text": redact_text(doc.text, min(len(doc.text), 8000)),
                                          "untrusted_data": True})
                else:
                    # Runtime task facts never become a stable compaction fact.
                    marker["status"] = "fresh_task_read_required"
            if operation == "current_task_state" and same_turn and not for_compaction:
                state = await read_task_state(access, session_id=metadata.get("task_session_id"))
                marker.update(redact_value(state, limit=4000))
                marker["status"] = "fresh_business_sql"
            elif same_turn and not for_compaction:
                marker["items"] = items
                marker["status"] = "fresh_evidence" if items else "unavailable"
            else:
                marker["status"] = "fresh_reference_available" if references else marker["status"]
                marker["reason_code"] = "compaction_citations_only" if for_compaction else "previous_turn_citations_only"
            marker["references"] = references
        except (MemoryAccessDenied, SQLAlchemyError, ValueError, KeyError, TypeError):
            marker["reason_code"] = "reference_unavailable"
        message.parts[index] = _replace_output(part, marker, verified=True,
            citations_only=for_compaction or not same_turn or marker["status"] == "unavailable")
    return detached
