"""Bounded original history with actor-bound cursors and current source checks."""
from __future__ import annotations

import base64
from hashlib import sha256
import hmac
import json

from sqlalchemy import func, select

from assistant.commands import _authority, command_digest, task_locked
from assistant.policy import AssistantError
from assistant.reporting import EVIDENCE_PROJECTION_VERSION, _read_call, _text, bound_report_locked
from assistant.results import part_hash, validate_source_asset
from core.config import get_config
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.assistant import AssistantTask
from db.models.message import Message
from db.models.part import Part
from memory.redaction import redact_credentials
from session.agent_event_log import append_agent_event_locked, prepare_agent_event_write, strip_memory_text


def _cursor_key() -> bytes:
    key = get_config().jwt_secret
    if not key:
        raise AssistantError(503, "ASSISTANT_CURSOR_UNAVAILABLE", "Cursor signing is not configured")
    return key.encode()


def _encode_cursor(value: dict) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(_cursor_key(), b"assistant-history-v1:" + raw, sha256).digest()
    return base64.urlsafe_b64encode(signature + raw).decode().rstrip("=")


def _decode_cursor(value: str, scope: str) -> dict:
    try:
        if len(value) > 12000:
            raise ValueError()
        data = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        raw = data[32:]
        expected = hmac.new(_cursor_key(), b"assistant-history-v1:" + raw, sha256).digest()
        payload = json.loads(raw)
        if not hmac.compare_digest(data[:32], expected) or payload["scope"] != scope:
            raise ValueError()
        return payload
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise AssistantError(409, "ASSISTANT_HISTORY_CURSOR", "Cursor does not match this history selection") from None


def visible_part_text(part: Part) -> str | None:
    if part.data.get("ignored") or part.type not in {"text", "file", "tool"}:
        return None
    if part.type != "tool":
        return _text(part)
    data = strip_memory_text(part.data)
    text = json.dumps({"tool": data.get("tool"), "status": data.get("status"),
                       "output": data.get("output"), "error": data.get("error")}, ensure_ascii=False)
    return redact_credentials(text)


async def _window(db, *, ids: list[str], session_id: str, user_id: str, main_id: str,
                  workspace_id: str, allowed_parts: set[str] | None, strict: bool):
    # One history read is one boundary: its messages share each source fact.
    from assistant.transactions import within_boundary
    return await within_boundary(db, lambda checks: _window_checked(db, ids=ids, session_id=session_id,
        user_id=user_id, main_id=main_id, workspace_id=workspace_id, allowed_parts=allowed_parts,
        strict=strict, checks=checks), user_id=user_id, workspace_id=workspace_id, main_id=main_id)


async def _window_checked(db, *, ids, session_id, user_id, main_id, workspace_id, allowed_parts, strict, checks):
    messages = list((await db.scalars(select(Message).where(Message.id.in_(ids),
        Message.session_id == session_id, Message.user_id == user_id).order_by(Message.id))).all())
    if len(messages) != len(ids):
        raise AssistantError(410, "ASSISTANT_HISTORY_SOURCE_GONE", "A selected message is no longer available")
    from assistant.evidence_cache import prove
    from assistant.verified_units import execution_message_unit, message_unit
    scope = dict(user_id=user_id, workspace_id=workspace_id, main_id=main_id)
    # One read proves every cached source verdict this window will use.
    await prove(db, [message_unit(message.id, **scope) if session_id == main_id
                     else execution_message_unit(message.id, **scope)
                     for message in messages if session_id != main_id or message.role == "assistant"])
    # Each selected message's parts and event span, read once for the window.
    selected = [message.id for message in messages]
    window_parts = list((await db.scalars(select(Part).where(Part.message_id.in_(selected),
        Part.session_id == session_id, Part.user_id == user_id).order_by(Part.created_at, Part.id))).all())
    spans = {row[0]: (row[1], row[2]) for row in (await db.execute(select(AgentEvent.message_id,
        func.min(AgentEvent.sequence), func.max(AgentEvent.sequence)).where(AgentEvent.message_id.in_(selected),
        AgentEvent.session_id == session_id, AgentEvent.user_id == user_id)
        .group_by(AgentEvent.message_id))).all()}
    entries, version = [], []
    for message in messages:
        # A derived report cannot become an alternate way to fetch revoked
        # execution evidence after results.read has correctly rejected it.
        if session_id != main_id or message.role == "assistant":
            from assistant.evidence import validate_message_sources
            try:
                if session_id == main_id:
                    await validate_message_sources(db, message, user_id=user_id, workspace_id=workspace_id,
                                                   main_id=main_id, snapshot_checks=checks)
                else:
                    from assistant.execution_sources import validate_execution_message
                    await validate_execution_message(db, message, user_id=user_id,
                        workspace_id=workspace_id, main_id=main_id, snapshot_checks=checks)
            except AssistantError:
                if strict:
                    raise AssistantError(410, "ASSISTANT_HISTORY_SOURCE_GONE", "The answer's original evidence is unavailable") from None
                version.append({"id": message.id, "status": "source_unavailable"})
                continue
        parts = [part for part in window_parts if part.message_id == message.id]
        span = spans.get(message.id, (None, None))
        version.append({"id": message.id, "finish": message.finish, "error": bool(message.error)})
        for part in parts:
            if allowed_parts is not None and part.id not in allowed_parts:
                continue
            text = visible_part_text(part)
            if text is None:
                continue
            await validate_source_asset(db, part, user_id=user_id, workspace_id=workspace_id)
            ref = {"session_id": session_id, "message_id": message.id, "part_id": part.id,
                   "content_hash": part_hash(part), "origin": part.data.get("origin", "unknown")}
            version.append(ref)
            entries.append({"message_id": message.id, "role": message.role, "type": part.type,
                "origin": ref["origin"], "source_ref": ref, "content_hash": ref["content_hash"],
                "event_span": list(span), "text": text, "total_chars": len(text), "untrusted_data": True})
    return entries, command_digest(version)


async def read_history(*, user_id: str, workspace_id: str, main_id: str, session_id: str,
                       message_ids: list[str] | None = None, cursor: str | None = None,
                       limit: int = 20, max_chars: int = 8000, ctx=None, record: bool = True) -> dict:
    if (type(limit) is not int or not 1 <= limit <= 50 or type(max_chars) is not int
            or not 1 <= max_chars <= 16000 or (message_ids is not None and not 1 <= len(message_ids) <= 20)):
        raise ValueError("Invalid history selection or character budget")
    selected_ids = sorted(set(message_ids or []))
    async with get_db_session() as db:
        if ctx is not None:
            if (ctx.session_id, ctx.user_id, ctx.workspace_id) != (main_id, user_id, workspace_id):
                raise AssistantError(403, "ASSISTANT_HISTORY_SCOPE", "Read context does not match its actor")
            main = await prepare_agent_event_write(db, session_id=main_id, user_id=user_id, run_fence=ctx.run_fence)
        else:
            main = None
        main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        if session_id != main.id:
            task_id = await db.scalar(select(AssistantTask.id).where(AssistantTask.assistant_session_id == main_id,
                AssistantTask.execution_session_id == session_id, AssistantTask.user_id == user_id,
                AssistantTask.workspace_id == workspace_id))
            if task_id is None:
                raise AssistantError(404, "ASSISTANT_HISTORY_UNAVAILABLE", "History is not linked to this assistant")
            await task_locked(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id, task_id=task_id)
        report = (await bound_report_locked(db, main, run_id=ctx.run_id, generation=ctx.run_generation)
                  if ctx is not None else None)
        from assistant.continuation import bound_coordination_locked, binding_ref
        coordination = (await bound_coordination_locked(db, main, run_id=ctx.run_id, generation=ctx.run_generation)
                        if ctx is not None and report is None else None)
        allowed_parts = None
        if report is not None or coordination is not None:
            refs = ([ref for ref, _ in report.parts] if report is not None else
                    [*coordination.result.output_refs, *coordination.human_refs])
            refs = [ref for ref in refs if ref["session_id"] == session_id]
            allowed_parts = {ref["part_id"] for ref in refs}
            allowed_messages = {ref["message_id"] for ref in refs}
            if not allowed_parts or (selected_ids and not set(selected_ids).issubset(allowed_messages)):
                raise AssistantError(403, "ASSISTANT_REPORT_SCOPE", "History is outside this result's exact sources")
            selected_ids = selected_ids or sorted(allowed_messages)
        scope = command_digest({"actor": user_id, "workspace": workspace_id, "main": main_id,
            "session": session_id, "selector": selected_ids, "limit": limit,
            "report": [report.result.id, report.result.report_attempt] if report else None,
            **({"coordination": binding_ref(coordination)} if coordination else {})})
        base = select(Message.id).where(Message.session_id == session_id, Message.user_id == user_id)
        if ctx is not None:
            base = base.where(Message.id != ctx.message_id)
        if selected_ids:
            base = base.where(Message.id.in_(selected_ids))
            if await db.scalar(select(func.count()).select_from(base.subquery())) != len(selected_ids):
                raise AssistantError(410, "ASSISTANT_HISTORY_SOURCE_GONE", "A selected message is unavailable")
        state = _decode_cursor(cursor, scope) if cursor else None
        if state:
            entries, version = await _window(db, ids=state["ids"], session_id=session_id, user_id=user_id,
                main_id=main_id, workspace_id=workspace_id, allowed_parts=allowed_parts, strict=bool(message_ids))
            if version != state["version"]:
                raise AssistantError(410, "ASSISTANT_HISTORY_SOURCE_CHANGED", "History changed during pagination")
        else:
            ceiling = await db.scalar(base.order_by(Message.id.desc()).limit(1))
            state = {"scope": scope, "ceiling": ceiling, "after": "", "index": 0, "offset": 0}
        if not cursor or (state["index"] >= len(entries) and state.get("more")):
            candidates = list((await db.scalars(base.where(Message.id > state["after"],
                Message.id <= (state["ceiling"] or "")).order_by(Message.id).limit(limit + 1))).all())
            ids = candidates[:limit]
            if not cursor and selected_ids and len(ids) < min(limit, len(selected_ids)):
                raise AssistantError(410, "ASSISTANT_HISTORY_SOURCE_GONE", "A selected message is unavailable")
            entries, version = await _window(db, ids=ids, session_id=session_id, user_id=user_id,
                main_id=main_id, workspace_id=workspace_id, allowed_parts=allowed_parts, strict=bool(message_ids))
            state.update(ids=ids, version=version, index=0, offset=0, more=len(candidates) > limit,
                         after=ids[-1] if ids else state["after"])
        # Leave room for IDs, source refs and the signed cursor under the
        # generic tool byte limit, including four-byte Unicode text.
        remaining, items, spans = min(max_chars, 5000), [], []
        while state["index"] < len(entries) and remaining and len(items) < 10:
            entry = entries[state["index"]]
            start = state["offset"]
            text = entry["text"][start:start + remaining]
            items.append({**entry, "text": text, "offset": start})
            spans.append({"part_id": entry["source_ref"]["part_id"], "content_hash": entry["content_hash"],
                          "start": start, "end": start + len(text), "total": entry["total_chars"]})
            remaining -= len(text)
            state["offset"] += len(text)
            if state["offset"] >= entry["total_chars"]:
                state["index"] += 1
                state["offset"] = 0
        more = state["index"] < len(entries) or state["more"]
        next_cursor = _encode_cursor(state) if more else None
        if ctx is not None and record:
            await _read_call(db, main, ctx, "history.read")
            receipt = {"source_refs": [item["source_ref"] for item in items], "spans": spans}
            await append_agent_event_locked(db, main, kind="assistant.history.read", payload=receipt,
                run_fence=ctx.run_fence, message_id=ctx.message_id, part_id=ctx.part_id,
                idempotency_key="history-read:" + command_digest({"part": ctx.part_id, "cursor": cursor, "scope": scope,
                                                                "budget": max_chars}))
            if report:
                await append_agent_event_locked(db, main, kind="assistant.report.sources_read", payload={
                    **receipt, "result_id": report.result.id, "report_attempt": report.result.report_attempt,
                    "inbox_id": report.inbox.id, "source_version": command_digest({"refs": report.result.output_refs,
                                                                                  "projection": EVIDENCE_PROJECTION_VERSION}),
                }, run_fence=ctx.run_fence, message_id=ctx.message_id, part_id=ctx.part_id,
                    idempotency_key="report-history:" + command_digest({"part": ctx.part_id, "cursor": cursor,
                                                                       "scope": scope, "budget": max_chars}))
        return {"session_id": session_id, "items": items, "next_cursor": next_cursor, "truncated": more,
                "returned_chars": sum(len(item["text"]) for item in items), "untrusted_data": True}
