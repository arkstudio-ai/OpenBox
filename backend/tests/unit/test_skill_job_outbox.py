"""Outbox: publish-then-stamp, per-job seq order, replays publish nothing new."""
import asyncio
import uuid

from skill_runtime import outbox, repository as repo
from skill_runtime.types import Succeeded


async def _admit(**overrides):
    kwargs = dict(
        user_id="u_" + uuid.uuid4().hex[:8],
        skill_key="builtin:demo",
        operation="run_step",
        idempotency_key="idem_" + uuid.uuid4().hex[:8],
        input_data={},
        runtime_kind="internal",
        queue_name="q_" + uuid.uuid4().hex[:8],
    )
    kwargs.update(overrides)
    job, _ = await repo.admit_job(**kwargs)
    return job


def _capture(monkeypatch):
    captured = []
    from bus import bus

    monkeypatch.setattr(bus, "publish", lambda event_type, data=None: captured.append((event_type, data)))
    return captured


async def test_publish_pending_delivers_in_seq_order_and_stamps(monkeypatch):
    captured = _capture(monkeypatch)
    job = await _admit()
    claimed = (await repo.claim_next(queues=(job.queue_name,), worker_id="w1"))[0]
    await repo.settle_invocation(
        job.id, claimed.lease_token, Succeeded(result={"ok": 1}), attempt_id=claimed.attempt_id
    )

    count = await outbox.publish_pending()
    mine = [d for t, d in captured if d["jobId"] == job.id]
    assert count >= 3
    assert [d["seq"] for d in mine] == [1, 2, 3]
    assert [d["eventType"] for d in mine] == ["job.created", "job.claimed", "job.succeeded"]
    assert all(d["userId"] == job.user_id for d in mine)

    events = await repo.get_events(job.id, job.user_id)
    assert all(e.published_at is not None for e in events)


async def test_replay_publishes_nothing_new(monkeypatch):
    captured = _capture(monkeypatch)
    job = await _admit()
    await outbox.publish_pending()
    before = len([d for _, d in captured if d["jobId"] == job.id])
    assert before == 1

    await outbox.publish_pending()
    after = len([d for _, d in captured if d["jobId"] == job.id])
    assert after == before


async def test_publish_failure_leaves_event_unstamped(monkeypatch):
    from bus import bus

    job = await _admit()

    def boom(event_type, data=None):
        raise RuntimeError("redis down")

    monkeypatch.setattr(bus, "publish", boom)
    assert await outbox.publish_pending() == 0
    events = await repo.get_events(job.id, job.user_id)
    assert events[0].published_at is None

    captured = _capture(monkeypatch)
    assert await outbox.publish_pending() >= 1
    assert [d["jobId"] for _, d in captured].count(job.id) == 1


async def test_publisher_loop_drains_and_stops(monkeypatch):
    captured = _capture(monkeypatch)
    publisher = outbox.OutboxPublisher(interval_seconds=0.05)
    publisher.start()
    try:
        job = await _admit()
        for _ in range(40):
            await asyncio.sleep(0.05)
            if any(d["jobId"] == job.id for _, d in captured):
                break
        assert any(d["jobId"] == job.id for _, d in captured)
    finally:
        await publisher.stop()
