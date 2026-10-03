"""Ephemeral, reauthorized assistant evidence and a bounded main context."""
from copy import deepcopy
import json

from sqlalchemy import select

from assistant.commands import _authority, command_digest
from assistant.evidence import validate_message_sources, validate_source_ref
from assistant.policy import AssistantError
from assistant.reporting import EVIDENCE_PROJECTION_VERSION
from assistant.results import validate_result_source, validate_source_asset
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.assistant import TaskResult
from db.models.message import Message
from db.models.part import Part
from memory.redaction import redact_credentials
from memory.tool_projection import _part_dict

READ_TOOL_IDS = frozenset({"projects.list", "sessions.list", "tasks.get", "tasks.list", "results.read", "history.read"})
MAX_CONTEXT_CHARS = 72000
MAX_RECENT_MESSAGES = 40


def _operation(part):
    identity = part.get("canonical_tool_id") or part.get("tool")
    return identity if part.get("type") == "tool" and identity in READ_TOOL_IDS else None


def _replace(part, value, *, verified=False):
    part = deepcopy(part)
    part["output"] = json.dumps(value, ensure_ascii=False, default=str)
    part["error"] = None
    part["metadata"] = {**(part.get("metadata") or {}), "_assistant_projection_verified": verified}
    if isinstance(part.get("state"), dict):
        part["state"] = {**part["state"], "output": part["output"], "error": None}
    return part


def guard_assistant_read(part: dict, *, allow_revalidated=False) -> dict:
    if not _operation(part):
        return part
    if allow_revalidated and (part.get("metadata") or {}).get("_assistant_projection_verified") is True:
        return part
    code = (part.get("metadata") or {}).get("failure_code")
    if isinstance(code, str) and code.startswith("ASSISTANT_") and len(code) <= 80:
        return _replace(part, {"status": "unavailable", "error_code": code, "operation": _operation(part)})
    return _replace(part, {"status": "fresh_read_required", "operation": _operation(part),
        "instruction": "Read the current authorized evidence. An old reference does not establish a current fact."})


def strip_assistant_read_text(data: dict) -> dict:
    if not _operation(data) or not (data.get("output") or (data.get("state") or {}).get("output")):
        return data
    safe = _replace(data, {"status": "stored_without_text", "operation": _operation(data),
        "instruction": "Original evidence is re-read under current permissions before each assistant step."})
    safe["metadata"].pop("_assistant_projection_verified", None)
    return safe


async def _fresh_read(part, *, ctx, for_compaction):
    operation = _operation(part)
    if operation is None:
        return part
    descriptor = (part.get("metadata") or {}).get("transient_assistant_refs") or {}
    if (for_compaction or descriptor.get("version") != 1 or descriptor.get("operation") != operation
            or descriptor.get("session_id") != ctx.session_id or descriptor.get("run_id") != ctx.run_id
            or descriptor.get("generation") != ctx.run_generation):
        return guard_assistant_read(part)
    try:
        from assistant.history import visible_part_text
        from assistant.runtime import authorize_assistant_tool
        from tool.assistant_tools import read_operation
        await authorize_assistant_tool(ctx, operation, descriptor["arguments"])
        if operation not in {"history.read", "results.read"}:
            from assistant.evidence import projection_digest
            value = await read_operation(operation, descriptor["arguments"], ctx, record=False)
            if projection_digest(value) != descriptor.get("digest"):
                return guard_assistant_read(part)
            return _replace(part, value, verified=True)
        value = deepcopy(descriptor["projection"])
        async with get_db_session() as db:
            await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
            if operation == "results.read":
                result = await db.get(TaskResult, value["result_id"])
                if result is None:
                    raise AssistantError(410, "ASSISTANT_SOURCE_UNAVAILABLE", "Result is unavailable")
                await validate_result_source(db, result, user_id=ctx.user_id,
                    workspace_id=ctx.workspace_id, main_id=ctx.session_id)
                if command_digest({"refs": result.output_refs, "projection": EVIDENCE_PROJECTION_VERSION}) != value["source_version"]:
                    raise AssistantError(410, "ASSISTANT_SOURCE_CHANGED", "Result changed")
            for entry in value.get("items", []) + value.get("sources", []):
                ref = entry.get("source_ref", entry)
                source = await validate_source_ref(db, ref, user_id=ctx.user_id,
                    workspace_id=ctx.workspace_id, main_id=ctx.session_id)
                text = visible_part_text(source)
                if text is None or len(text) != entry["total_chars"]:
                    raise AssistantError(410, "ASSISTANT_SOURCE_CHANGED", "Evidence projection changed")
                count = entry.pop("read_chars")
                entry["text"] = text[entry["offset"]:entry["offset"] + count]
        return _replace(part, value, verified=True)
    except (AssistantError, KeyError, ValueError, TypeError):
        return guard_assistant_read(part)


async def project_main_messages(messages: list, *, ctx, for_compaction=False) -> list:
    """Never replay a summary instead of checking its original references.

    Older turns are bounded; current tool reads preserve their JSON cursor
    contract. Dropped history remains accessible through history.read.
    """
    async with get_db_session() as db:
        await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        current_ids = set((await db.scalars(select(AgentEvent.message_id).where(
            AgentEvent.session_id == ctx.session_id, AgentEvent.user_id == ctx.user_id,
            AgentEvent.run_id == ctx.run_id, AgentEvent.generation == ctx.run_generation,
            AgentEvent.message_id.is_not(None)).distinct())).all())
        protected = {message.id for message in messages if message.role == "user" and message.id in current_ids}
        recent = {message.id for message in messages[-MAX_RECENT_MESSAGES:]} | protected
        detached = deepcopy([message for message in messages if message.id in recent])
        current_input_budget = min(16000, 24000 // max(1, len(protected)))
        user_part_query = select(Part).where(Part.session_id == ctx.session_id, Part.user_id == ctx.user_id,
            Part.message_id.in_([message.id for message in detached if message.role == "user"]))
        user_parts = list((await db.scalars(user_part_query)).all())
        user_part_by_id = {part.id: part for part in user_parts}
        for message in detached:
            message.parts = [_part_dict(part) for part in message.parts or []]
            if message.summary:
                message.parts = []  # Unversioned compaction is never authority.
                continue
            if message.role == "assistant" and message.id not in current_ids:
                row = await db.get(Message, message.id)
                try:
                    if row is None:
                        raise AssistantError(410, "ASSISTANT_SOURCE_UNAVAILABLE", "Message is unavailable")
                    await validate_message_sources(db, row, user_id=ctx.user_id,
                        workspace_id=ctx.workspace_id, main_id=ctx.session_id)
                except AssistantError:
                    message.parts = [{"type": "text", "text": "[Earlier answer omitted: its original evidence is unavailable or changed. Read current sources.]"}]
            for index, part in enumerate(message.parts):
                if message.role == "user" and part.get("type") in {"text", "file"}:
                    source = user_part_by_id.get(part.get("id"))
                    try:
                        if (source is None or source.data.get("ignored")
                                or (source.type == "text" and source.data.get("text") != part.get("text"))):
                            raise AssistantError(410, "ASSISTANT_SOURCE_CHANGED", "Original input is unavailable")
                        await validate_source_asset(db, source, user_id=ctx.user_id, workspace_id=ctx.workspace_id)
                    except AssistantError:
                        message.parts[index] = {"type": "text", "text": "[Original input or attachment is unavailable.]",
                                                "origin": "system_recovery", "synthetic": True}
                        continue
                if part.get("type") == "reasoning":
                    message.parts[index] = {"type": "text", "text": ""}
                elif part.get("type") == "text":
                    text = str(part.get("text") or "")
                    text_limit = current_input_budget if message.id in protected else 16000
                    part["text"] = redact_credentials(text)[:text_limit]
                    if len(text) > text_limit:
                        part["text"] += f"\n[Truncated. Read original message {message.id} using history.read.]"
                    if message.role == "user" and part.get("origin") == "human":
                        part["text"] = f"[Original human message_id={message.id}]\n" + part["text"]
    # Rematerialization can open its own read transactions, so do it after
    # releasing the outer Session read rather than nesting admission locks.
    for message in detached:
        message.parts = [await _fresh_read(part, ctx=ctx, for_compaction=for_compaction) for part in message.parts]
    sizes = {message.id: sum(len(json.dumps(part, ensure_ascii=False, default=str)) for part in message.parts)
             for message in detached}
    kept = set(protected)
    remaining = MAX_CONTEXT_CHARS - sum(sizes.get(message_id, 0) for message_id in protected)
    for message in reversed(detached):
        size = sizes[message.id]
        if message.id not in protected and size <= remaining:
            kept.add(message.id)
            remaining -= size
    return [message for message in detached if message.id in kept]
