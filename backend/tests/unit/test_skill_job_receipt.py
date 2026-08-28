"""Terminal jobs leave exactly one durable receipt in their session timeline."""
import uuid
from datetime import datetime, timezone

from skill_runtime import outbox, repository as repo
from skill_runtime.types import Succeeded

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731


async def _make_session(user_id):
    from db.base import get_db_session
    from db.models.session import Session as SessionORM

    session_id = "session_" + uuid.uuid4().hex[:10]
    async with get_db_session() as db:
        db.add(SessionORM(
            id=session_id, user_id=user_id, title="t",
            project_id="proj_" + uuid.uuid4().hex[:8],
            created_at=NOW(), updated_at=NOW(),
        ))
    return session_id


async def _finished_job(session_id=None, user_id=None):
    user_id = user_id or ("u_" + uuid.uuid4().hex[:8])
    job, _ = await repo.admit_job(
        user_id=user_id,
        skill_key="builtin:demo-echo",
        operation="echo",
        idempotency_key="k-" + uuid.uuid4().hex[:8],
        input_data={},
        runtime_kind="internal",
        queue_name="q_" + uuid.uuid4().hex[:8],
        session_id=session_id,
    )
    claimed = (await repo.claim_next(queues=(job.queue_name,), worker_id="w1"))[0]
    await repo.settle_invocation(
        job.id, claimed.lease_token, Succeeded(result={"echo": "done"}),
        attempt_id=claimed.attempt_id,
    )
    return job


async def _receipts_for(session_id):
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.message import Message
    from db.models.part import Part

    async with get_db_session() as db:
        messages = (
            await db.execute(
                select(Message).where(
                    Message.session_id == session_id,
                    Message.finish == "skill_job_receipt",
                )
            )
        ).scalars().all()
        parts = (
            await db.execute(
                select(Part).where(Part.session_id == session_id, Part.type == "skill_job")
            )
        ).scalars().all()
    return messages, parts


async def test_terminal_publish_writes_one_receipt(monkeypatch):
    from bus import bus

    published = []
    monkeypatch.setattr(bus, "publish", lambda t, d=None: published.append((t, d)))

    user = "u_" + uuid.uuid4().hex[:8]
    session_id = await _make_session(user)
    job = await _finished_job(session_id=session_id, user_id=user)

    await outbox.publish_pending()
    messages, parts = await _receipts_for(session_id)
    assert len(messages) == 1
    assert messages[0].client_message_id == f"sjr:{job.id}"
    assert len(parts) == 1
    assert parts[0].data["jobId"] == job.id
    assert parts[0].data["status"] == "succeeded"
    assert parts[0].data["summary"] == "echo: done"

    created_events = [d for t, d in published if t == "message.created"]
    assert len(created_events) == 1
    assert created_events[0]["message"]["parts"][0]["type"] == "skill_job"

    # Replays never duplicate the receipt.
    await outbox.publish_pending()
    messages, parts = await _receipts_for(session_id)
    assert len(messages) == 1 and len(parts) == 1


async def test_get_messages_parses_receipted_session(monkeypatch):
    """Regression: the receipt part must be a member of the MessagePart union,
    or every read of a receipted session 500s on validation."""
    from bus import bus

    monkeypatch.setattr(bus, "publish", lambda t, d=None: None)
    user = "u_" + uuid.uuid4().hex[:8]
    session_id = await _make_session(user)
    job = await _finished_job(session_id=session_id, user_id=user)
    await outbox.publish_pending()

    from session.session import get_messages

    messages = await get_messages(session_id, user_id=user)
    receipts = [
        part
        for message in messages
        for part in message.parts
        if getattr(part, "type", None) == "skill_job"
    ]
    assert len(receipts) == 1
    assert receipts[0].jobId == job.id
    assert receipts[0].status == "succeeded"


async def test_sessionless_job_writes_no_receipt(monkeypatch):
    from bus import bus

    monkeypatch.setattr(bus, "publish", lambda t, d=None: None)
    job = await _finished_job(session_id=None)
    await outbox.publish_pending()

    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.part import Part

    async with get_db_session() as db:
        parts = (
            await db.execute(
                select(Part).where(Part.type == "skill_job", Part.user_id == job.user_id)
            )
        ).scalars().all()
    assert parts == []


async def test_bus_outage_delays_the_receipt_but_never_loses_it(monkeypatch):
    """Per-job wire ordering means a bus outage holds the terminal event (and
    with it the receipt) instead of skipping ahead. Nothing is lost: once the
    bus recovers, the events publish in order and exactly one receipt lands."""
    from bus import bus

    user = "u_" + uuid.uuid4().hex[:8]
    session_id = await _make_session(user)
    job = await _finished_job(session_id=session_id, user_id=user)

    async def boom(event_type, data=None, **kwargs):
        raise RuntimeError("redis down")

    monkeypatch.setattr(bus, "publish_confirmed", boom)
    assert await outbox.publish_pending() == 0
    messages, _ = await _receipts_for(session_id)
    assert messages == []

    published = []

    async def ok(event_type, data=None, **kwargs):
        published.append((data or {}).get("seq"))

    monkeypatch.setattr(bus, "publish_confirmed", ok)
    monkeypatch.setattr(bus, "publish", lambda t, d=None: None)
    assert await outbox.publish_pending() >= 3

    messages, parts = await _receipts_for(session_id)
    assert len(messages) == 1 and len(parts) == 1
    assert parts[0].data["jobId"] == job.id
    assert published == sorted(published), "wire events must keep per-job order"

    # A replay publishes nothing new and never duplicates the receipt.
    await outbox.publish_pending()
    messages, parts = await _receipts_for(session_id)
    assert len(messages) == 1 and len(parts) == 1


async def test_receipt_marker_unique_is_db_enforced():
    """Two racing publishers must collide on the partial unique index, not on
    a check-then-insert. The index is scoped to real receipt rows so a user
    message can never squat the marker."""
    import pytest
    from sqlalchemy.exc import IntegrityError as SAIntegrityError

    from db.base import get_db_session
    from db.models.message import Message

    user = "u_" + uuid.uuid4().hex[:8]
    session_id = await _make_session(user)
    marker = "sjr:sjob_race"
    async with get_db_session() as db:
        db.add(Message(
            id="message_race1_" + uuid.uuid4().hex[:8], session_id=session_id,
            user_id=user, role="assistant", client_message_id=marker,
            finish="skill_job_receipt", created_at=NOW(),
        ))
    with pytest.raises(SAIntegrityError):
        async with get_db_session() as db:
            db.add(Message(
                id="message_race2_" + uuid.uuid4().hex[:8], session_id=session_id,
                user_id=user, role="assistant", client_message_id=marker,
                finish="skill_job_receipt", created_at=NOW(),
            ))


async def test_receipt_flag_off(monkeypatch):
    from bus import bus
    from core.config import get_config

    monkeypatch.setattr(bus, "publish", lambda t, d=None: None)
    monkeypatch.setattr(get_config(), "skill_job_chat_receipt", False)

    user = "u_" + uuid.uuid4().hex[:8]
    session_id = await _make_session(user)
    await _finished_job(session_id=session_id, user_id=user)
    await outbox.publish_pending()
    messages, parts = await _receipts_for(session_id)
    assert messages == [] and parts == []
