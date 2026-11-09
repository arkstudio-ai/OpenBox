"""A session with live reveals must not defer its catalogue on the next run."""

from datetime import datetime, timedelta, timezone

from session.internal_parts import has_live_reveals


def _state(expires_at: datetime, agent: str = "build") -> dict:
    return {
        "v": 1,
        "agents": {
            agent: {
                "catalog_generation": "g1",
                "revealed": {
                    "video_generate": {
                        "expires_at": expires_at.isoformat(),
                        "schema_digest": "a" * 64,
                        "origin_seq": 1,
                    }
                },
            }
        },
        "next_origin_seq": 2,
        "provider_fallback": {},
    }


def test_unexpired_reveal_counts_as_live():
    now = datetime(2026, 11, 9, 3, 3, tzinfo=timezone.utc)
    assert has_live_reveals(_state(now + timedelta(minutes=26)), "build", now=now)


def test_expired_or_foreign_agent_reveals_do_not_count():
    now = datetime(2026, 11, 9, 3, 3, tzinfo=timezone.utc)
    assert not has_live_reveals(_state(now - timedelta(seconds=1)), "build", now=now)
    assert not has_live_reveals(_state(now + timedelta(minutes=5), agent="secretary"), "build", now=now)


def test_missing_or_malformed_state_is_not_live():
    assert not has_live_reveals(None, "build")
    assert not has_live_reveals({"agents": {"build": {"revealed": "nope"}}}, "build")
    assert not has_live_reveals({"agents": {"build": {"revealed": {"x": {"expires_at": "garbage"}}}}}, "build")
