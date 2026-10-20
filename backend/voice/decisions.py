"""Speech depends on decisions this assistant turn actually read, not every open card.

Only committed, successful decision-tool receipts supply dependencies. User text,
background context and arbitrary tool output cannot register one. The exact read
version is checked against SQL at settlement and against live updates thereafter.
"""
import json

from sqlalchemy import select

READS = frozenset({"cards.list", "requests.list", "requests.get"})
ANSWERS = frozenset({"cards.answer", "requests.answer", "requests.reply"})


def observed(payloads):
    versions = {}
    for payload in payloads:
        data = (payload.get("part") or {}).get("data") or {}
        identity = ((payload.get("model") or {}).get("tool_identity") or {}).get("canonical_tool_id") or data.get("tool")
        if identity not in READS | ANSWERS or data.get("status") != "completed" or data.get("error"):
            continue
        try:
            value = json.loads(data.get("output") or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict) or value.get("error"):
            continue
        if identity in ANSWERS:
            if value.get("state") in {"answered", "decision_recorded", "applied"}:
                versions.pop(value.get("request_id"), None)
            continue
        items = [value] if identity == "requests.get" else value.get("items") or []
        items = [*items, *(value.get("other_conversations") or [])]
        for item in items:
            if not isinstance(item, dict) or item.get("kind", "question") != "question":
                continue
            key = item.get("request_id") or item.get("id")
            version = item.get("version")
            if item.get("state", "pending") != "pending":
                versions.pop(key, None)
            elif isinstance(key, str) and isinstance(version, str) and key and version:
                versions[key] = version
    return versions


async def for_turn(session_id, user_id, workspace_id, inbox_id):
    from assistant.policy import require_membership
    from assistant.requests import question_version
    from db.base import get_db_session
    from db.models.agent_event import AgentEvent
    from db.models.agent_inbox import AgentInboxItem
    from db.models.question import QuestionCheckpoint, SessionExecution
    from db.models.session import Session
    from question import runtime

    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, inbox_id) if inbox_id else None
        main = await db.get(Session, session_id)
        if (item is None or item.user_id != user_id or item.session_id != session_id or not item.run_id
                or main is None or main.user_id != user_id or main.workspace_id != workspace_id or main.is_deleted):
            return {}, False
        await require_membership(db, user_id, workspace_id)
        payloads = (await db.scalars(select(AgentEvent.payload).where(
            AgentEvent.session_id == session_id, AgentEvent.user_id == user_id,
            AgentEvent.run_id == item.run_id, AgentEvent.generation == item.generation,
            AgentEvent.turn_id == item.turn_id,
            AgentEvent.kind == "tool.result").order_by(AgentEvent.sequence))).all()
        versions = observed(payloads)
        if not versions:
            return {}, False
        rows = (await db.scalars(select(QuestionCheckpoint).join(
            Session, Session.id == QuestionCheckpoint.session_id).join(
            SessionExecution, SessionExecution.session_id == QuestionCheckpoint.session_id).where(
            QuestionCheckpoint.id.in_(versions), QuestionCheckpoint.user_id == user_id,
            Session.user_id == user_id, Session.workspace_id == workspace_id, Session.is_deleted.is_(False),
            QuestionCheckpoint.status == "pending",
            (QuestionCheckpoint.generation == SessionExecution.generation) | QuestionCheckpoint.detached.is_(True)))).all()
        current = {row.id: question_version(row) for row in rows
                   if not row.expires_at or runtime.utc(row.expires_at) > runtime.now()}
        return versions, any(current.get(key) != version for key, version in versions.items())
