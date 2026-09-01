"""Cron injector consumes only atomically claimed session outbox rows."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import cron.injector as injector
from cron.outbox import stable_delivery_id


async def _pending_session_delivery(*, status: str = "idle"):
    from db.base import get_db_session
    from db.models.cron import CronDeliveryOutbox, CronRun
    from db.models.project import Project
    from db.models.session import Session
    from db.models.user import User

    suffix = uuid.uuid4().hex[:10]
    user_id = f"u_{suffix}"
    project_id = f"proj_{suffix}"
    session_id = f"sess_{suffix}"
    job_id = f"cron_{suffix}"
    run_id = f"cron_run_{suffix}"
    delivery_id = stable_delivery_id(run_id, "session")
    now = datetime.now(timezone.utc)
    payload = {
        "session_id": session_id,
        "job_name": "daily",
        "occurred_at": now.isoformat(),
        "user_message_id": f"message_{uuid.uuid4().hex[:26]}",
        "user_part_id": f"part_{uuid.uuid4().hex[:26]}",
        "assistant_message_id": f"message_{uuid.uuid4().hex[:26]}",
        "assistant_part_id": f"part_{uuid.uuid4().hex[:26]}",
        "user_text": "scheduled report",
        "result_text": "done",
    }
    async with get_db_session() as db:
        db.add(User(
            id=user_id,
            username=user_id,
            created_at=now,
            updated_at=now,
        ))
        db.add(Project(
            id=project_id,
            user_id=user_id,
            name="p",
            slug=project_id,
            created_at=now,
            updated_at=now,
        ))
        db.add(Session(
            id=session_id,
            user_id=user_id,
            project_id=project_id,
            title="main",
            status=status,
            kind="normal",
            token_usage={},
            created_at=now,
            updated_at=now,
        ))
        db.add(CronRun(
            id=run_id,
            job_id=job_id,
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            status="ok",
            summary_text="done",
            injected=False,
            started_at=now,
            ended_at=now,
        ))
        db.add(CronDeliveryOutbox(
            id=delivery_id,
            run_id=run_id,
            job_id=job_id,
            user_id=user_id,
            project_id=project_id,
            session_id=session_id,
            kind="session",
            payload=payload,
            state="pending",
            attempts=0,
            available_at=now - timedelta(seconds=2),
            created_at=now,
            updated_at=now,
        ))
    return user_id, session_id, run_id, delivery_id


async def test_flush_injects_once_and_marks_run():
    from db.base import get_db_session
    from db.models.cron import CronDeliveryOutbox, CronRun
    from db.models.message import Message
    from sqlalchemy import func, select

    user_id, session_id, run_id, delivery_id = await _pending_session_delivery()
    assert await injector.flush_pending_cron_results(session_id, user_id) == 1
    assert await injector.flush_pending_cron_results(session_id, user_id) == 0

    async with get_db_session() as db:
        message_count = (await db.execute(
            select(func.count()).select_from(Message).where(
                Message.session_id == session_id
            )
        )).scalar_one()
        run = (await db.execute(
            select(CronRun).where(CronRun.id == run_id)
        )).scalar_one()
        delivery = (await db.execute(
            select(CronDeliveryOutbox).where(
                CronDeliveryOutbox.id == delivery_id
            )
        )).scalar_one()
    assert message_count == 2
    assert run.injected is True
    assert delivery.state == "delivered"


async def test_busy_session_releases_claim_without_writing_messages():
    from db.base import get_db_session
    from db.models.cron import CronDeliveryOutbox
    from db.models.message import Message
    from sqlalchemy import func, select

    user_id, session_id, _run_id, delivery_id = await _pending_session_delivery(
        status="busy"
    )
    assert await injector.flush_pending_cron_results(session_id, user_id) == 0
    async with get_db_session() as db:
        count = (await db.execute(
            select(func.count()).select_from(Message).where(
                Message.session_id == session_id
            )
        )).scalar_one()
        delivery = (await db.execute(
            select(CronDeliveryOutbox).where(
                CronDeliveryOutbox.id == delivery_id
            )
        )).scalar_one()
    assert count == 0
    assert delivery.state == "pending"


async def test_flush_rejects_a_fence_for_another_session():
    with pytest.raises(ValueError, match="another session"):
        await injector.flush_pending_cron_results(
            "session-a",
            "user-a",
            run_fence=("session-b", "run", 1),
        )


async def test_compatibility_try_has_nothing_to_do_before_settlement():
    job = {
        "session_id": f"sess_{uuid.uuid4().hex[:8]}",
        "user_id": f"u_{uuid.uuid4().hex[:8]}",
    }
    assert await injector.try_inject_result("missing-run", job, "result") is False


async def test_overflow_compaction_and_injection_share_a_reserved_fence(
    monkeypatch,
):
    from cron import outbox
    import agent.driver as driver
    import session.session as session_mod
    from bus import bus

    calls = []

    class Lease:
        run_id = "compaction-run"
        generation = 17

        async def release(self, *, session_status=None):
            calls.append(("release", session_status))
            return True

    async def reserve(session_id, user_id, **kwargs):
        calls.append(("reserve", session_id, user_id, kwargs))
        return Lease()

    async def needs(*args):
        return True

    async def compact(session_id, user_id, *, run_fence):
        calls.append(("compact", run_fence))

    async def inject(session_id, user_id, **kwargs):
        calls.append(("inject", kwargs["run_fence"]))
        return True

    async def publish(*args, **kwargs):
        return None

    monkeypatch.setattr(driver, "reserve_run", reserve)
    monkeypatch.setattr(outbox, "_session_delivery_needs_compaction", needs)
    monkeypatch.setattr(outbox, "_compact_before_session_delivery", compact)
    monkeypatch.setattr(session_mod, "inject_cron_message_pair_once", inject)
    monkeypatch.setattr(bus, "publish_confirmed", publish)

    now = datetime.now(timezone.utc)
    claim = outbox.DeliveryClaim(
        delivery_id="delivery-overflow",
        run_id="run-overflow",
        job_id="job-overflow",
        user_id="user-overflow",
        project_id="project-overflow",
        session_id="session-overflow",
        kind="session",
        payload={
            "session_id": "session-overflow",
            "job_name": "large result",
            "occurred_at": now.isoformat(),
            "user_message_id": "message-user",
            "user_part_id": "part-user",
            "assistant_message_id": "message-assistant",
            "assistant_part_id": "part-assistant",
            "user_text": "task",
            "result_text": "result",
        },
        attempts=1,
        token="claim-token",
        owner_id="delivery-worker",
        lease_expires_at=now + timedelta(minutes=1),
    )
    outcome = await outbox._deliver_session(claim, run_fence=None)
    expected = ("session-overflow", "compaction-run", 17)
    assert outcome.success is True
    assert ("compact", expected) in calls
    assert ("inject", expected) in calls
    assert calls[-1] == ("release", "idle")
