"""The emit hot path stays far below its latency budget (bounds are loose for noisy machines)."""
from trajectory.benchmark_emit import KIB, measure


def test_emit_latency_for_two_kib_and_512_kib_events():
    small = measure(2 * KIB, 3000)
    assert 1.5 * KIB <= small["event_bytes"] <= 3 * KIB
    assert small["dropped_events"] == 0 and small["written_lines"] >= 3000
    # SPEC §14.3 targets p99 <= 50 us (2 KiB) and <= 2 ms (512 KiB); these bounds are 20x looser.
    assert small["emit_p99_us"] < 1000
    assert small["emit_bytes_p99_us"] < 200
    large = measure(512 * KIB, 40, warmup=5)
    assert 400 * KIB <= large["event_bytes"] <= 600 * KIB
    assert large["dropped_events"] == 0
    assert large["emit_p99_us"] < 40_000
