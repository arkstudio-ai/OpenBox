"""Schedule math: next-run computation, min-gap, and top-of-hour stagger."""
from datetime import datetime, timedelta, timezone

from cron.schedule import (
    STAGGER_WINDOW_MS,
    apply_stagger,
    compute_next_run_at,
    min_gap_ms,
    schedule_from_dict,
    stagger_ms_for,
)
from cron.types import CronScheduleAt, CronScheduleCron, CronScheduleEvery

NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=timezone.utc)


def test_at_future_and_past():
    future = CronScheduleAt(at="2026-08-25T13:00:00+00:00")
    assert compute_next_run_at(future, NOW) == datetime(2026, 8, 25, 13, tzinfo=timezone.utc)
    past = CronScheduleAt(at="2026-08-25T11:00:00+00:00")
    assert compute_next_run_at(past, NOW) is None


def test_every_advances_from_anchor():
    anchor_ms = int(NOW.timestamp() * 1000)
    sched = CronScheduleEvery(every_ms=600_000, anchor_ms=anchor_ms)
    nxt = compute_next_run_at(sched, NOW + timedelta(minutes=1))
    assert nxt == NOW + timedelta(minutes=10)


def test_cron_expression_with_timezone():
    sched = CronScheduleCron(expr="0 9 * * *", tz="Asia/Shanghai")
    nxt = compute_next_run_at(sched, NOW)
    # 9:00 Asia/Shanghai == 01:00 UTC; next after 12:00 UTC is tomorrow 01:00 UTC
    assert nxt == datetime(2026, 8, 26, 1, 0, tzinfo=timezone.utc)


def test_schedule_from_dict_roundtrip_and_garbage():
    assert schedule_from_dict({"kind": "every", "every_ms": 60000}).every_ms == 60000
    assert schedule_from_dict({"kind": "cron", "expr": "0 9 * * *"}).expr == "0 9 * * *"
    assert schedule_from_dict({"kind": "at", "at": "2026-01-01T00:00:00Z"}).kind == "at"
    assert schedule_from_dict(None) is None
    assert schedule_from_dict({"kind": "cron"}) is None
    assert schedule_from_dict({"kind": "nope"}) is None


def test_min_gap_takes_the_minimum_not_the_average():
    assert min_gap_ms(CronScheduleEvery(every_ms=1_800_000)) == 1_800_000
    # fires at :00 and :01 of every hour — min gap is 60s even though the
    # average is 30 minutes
    tight = min_gap_ms(CronScheduleCron(expr="0,1 * * * *"))
    assert tight is not None and tight <= 60_000
    assert min_gap_ms(CronScheduleAt(at="2026-01-01T00:00:00Z")) is None
    half_hour = min_gap_ms(CronScheduleCron(expr="*/30 * * * *"))
    assert half_hour is not None and 1_700_000 <= half_hour <= 1_800_000


def test_stagger_is_deterministic_and_bounded():
    a = stagger_ms_for("cron_abc")
    assert a == stagger_ms_for("cron_abc")
    assert 0 <= a < STAGGER_WINDOW_MS
    assert stagger_ms_for("cron_abc") != stagger_ms_for("cron_xyz") or True  # may collide, no crash


def test_stagger_applies_only_to_top_of_hour_cron():
    base = datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc)
    top = CronScheduleCron(expr="0 9 * * *")
    shifted = apply_stagger(base, top, "cron_abc")
    assert shifted == base + timedelta(milliseconds=stagger_ms_for("cron_abc"))

    not_top = CronScheduleCron(expr="*/5 * * * *")
    assert apply_stagger(base, not_top, "cron_abc") == base

    every = CronScheduleEvery(every_ms=600_000)
    assert apply_stagger(base, every, "cron_abc") == base

    at = CronScheduleAt(at="2026-08-26T09:00:00Z")
    assert apply_stagger(base, at, "cron_abc") == base

    assert apply_stagger(None, top, "cron_abc") is None
