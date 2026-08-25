"""Computer performance evidence survives tool-part persistence filtering."""

from agent.processor import persisted_tool_metadata


def test_computer_timings_batch_and_lease_are_persisted():
    observed = persisted_tool_metadata({
        "timings": {"execute_ms": 150, "oss_ms": 1200, "total_ms": 2200},
        "batch_size": 3,
        "lease": {"wait_ms": 42, "ttl_seconds": 180},
        "geometry": {"native": [3840, 2160]},
        "private_internal": "drop-me",
    })

    assert observed == {
        "timings": {"execute_ms": 150, "oss_ms": 1200, "total_ms": 2200},
        "batch_size": 3,
        "lease": {"wait_ms": 42, "ttl_seconds": 180},
    }
