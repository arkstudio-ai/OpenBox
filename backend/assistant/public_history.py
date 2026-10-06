"""Transcript pages for the assistant and its task sessions.

V2 (docs/PERSONAL_ASSISTANT_DESIGN_V2.md 11.2): the main session and task
sessions page like ordinary chats. Saved messages are returned as stored; a
final main-session answer also carries the timing of the input it answered.
"""
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.orm import aliased

from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem


async def _reply_timings(db, main, ids):
    """Elapsed time belongs to one settled Inbox answer, not a visual turn.

    Main turns claim exactly one input. Match both immutable message/terminal
    boundaries before trusting that input's mutable result reference. Read in
    batches; missing or ambiguous bindings have no inferred duration.
    """
    created, finished = aliased(AgentEvent), aliased(AgentEvent)
    found = {}
    for offset in range(0, len(ids), 200):
        rows = (await db.execute(select(AgentInboxItem.result_message_id,
            AgentInboxItem.accepted_at, AgentInboxItem.settled_at).join(created,
                (created.session_id == AgentInboxItem.session_id)
                & (created.user_id == AgentInboxItem.user_id)
                & (created.message_id == AgentInboxItem.result_message_id)
                & (created.run_id == AgentInboxItem.run_id)
                & (created.generation == AgentInboxItem.generation)
                & (created.turn_id == AgentInboxItem.turn_id)
                & (created.kind == "message.created"),
            ).join(finished,
                (finished.session_id == created.session_id) & (finished.user_id == created.user_id)
                & (finished.message_id == created.message_id) & (finished.run_id == created.run_id)
                & (finished.generation == created.generation) & (finished.turn_id == created.turn_id)
                & (finished.kind == "turn.finished"),
            ).where(AgentInboxItem.session_id == main.id, AgentInboxItem.user_id == main.user_id,
                AgentInboxItem.result_message_id.in_(ids[offset:offset + 200]),
                AgentInboxItem.state == "settled", AgentInboxItem.outcome == "succeeded",
                AgentInboxItem.delivery == "followup", AgentInboxItem.target == "next-turn",
                AgentInboxItem.message_id == AgentInboxItem.turn_id,
                AgentInboxItem.settled_at.is_not(None)))).all()
        for message_id, accepted, settled in rows:
            accepted = accepted.replace(tzinfo=accepted.tzinfo or timezone.utc).astimezone(timezone.utc)
            settled = settled.replace(tzinfo=settled.tzinfo or timezone.utc).astimezone(timezone.utc)
            value = ({"accepted_at": accepted.isoformat(), "settled_at": settled.isoformat()}
                     if settled >= accepted else None)
            found[message_id] = None if message_id in found else value
    return {key: value for key, value in found.items() if value is not None}


async def public_messages(session, messages, *, actor_user_id):
    """Return the page as stored; the caller already authorized the reader."""
    values = [message.model_dump() for message in messages]
    if getattr(session, "kind", None) != "assistant":
        return values
    answers = [message.id for message in messages
               if message.role == "assistant" and getattr(message, "finish", None) == "stop"
               and not getattr(message, "summary", False)]
    if not answers:
        return values
    async with get_db_session() as db:
        timings = await _reply_timings(db, session, answers)
    for value in values:
        if value["id"] in timings:
            value["assistant_timing"] = timings[value["id"]]
    return values
