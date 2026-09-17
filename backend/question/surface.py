"""Commit durable human-input transitions to the canonical Agent history.

Callers already hold the owning session's row lock. Seeding happens before
the read model changes, and the new state is appended in that same transaction.
User answers are control-plane writes, independent of a finished run's lease.
"""
from db.models.message import Message
from session.agent_event_log import (
    append_message_events_locked,
    append_part_event_locked,
    ensure_surface_seed_locked,
)


async def prepare(db, session):
    await ensure_surface_seed_locked(db, session)


async def part_updated(db, session, part):
    message = await db.get(Message, part.message_id)
    if message is None or message.user_id != session.user_id:
        raise ValueError("Question checkpoint lost its message")
    await db.flush()
    await append_part_event_locked(db, session, part, message,
                                   operation="updated", run_fence=None)


async def message_updated(db, session, message):
    await db.flush()
    await append_message_events_locked(db, session, message,
                                       operation="updated", run_fence=None)
