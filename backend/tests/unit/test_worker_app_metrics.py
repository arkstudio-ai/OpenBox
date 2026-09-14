"""Worker metrics registry: the exact SPEC §8.13 names, counters, gauges and the /metrics snapshot."""
import threading

from trajectory.ops import cms
from trajectory.worker import metrics as metrics_module
from trajectory.worker.metrics import COUNTERS, GAUGES, Metrics, get_metrics, reset_metrics_for_tests


def test_names_are_exactly_the_spec_and_cover_the_cloudmonitor_push():
    assert COUNTERS == ("ingest_lines", "ingest_events", "duplicates", "idempotency_conflicts", "deleted_drops",
                        "ownership_drops", "gaps_recorded", "producer_loss_events", "quarantined_files", "blob_puts",
                        "blob_put_bytes", "blob_put_failures", "segment_uploads", "segment_failures", "gc_deleted",
                        "gc_failures", "exports_built")
    assert GAUGES == ("spool_bytes", "spool_files", "spool_oldest_age_seconds", "ingest_lag_seconds",
                      "projection_lag_events", "archive_lag_events", "gc_queue_depth", "trace_db_bytes",
                      "hot_events_rows", "trajectories_degraded", "trajectories_blocked", "stale_hot_partitions")
    assert set(cms.COUNTERS) <= set(COUNTERS)
    assert set(cms.GAUGES) <= set(GAUGES)


def test_snapshot_starts_at_zero_and_reflects_updates():
    moment = [50.0]
    registry = Metrics(clock=lambda: moment[0])
    snapshot = registry.snapshot()
    assert snapshot == {"counters": dict.fromkeys(COUNTERS, 0), "gauges": dict.fromkeys(GAUGES, 0),
                        "uptime_seconds": 0.0}
    registry.inc("ingest_lines")
    registry.inc("blob_put_bytes", 1024)
    registry.inc("ingest_lines", 2.5)
    registry.set_gauge("spool_bytes", 4096)
    registry.set_gauge("spool_bytes", 10)
    registry.set_gauge("ingest_lag_seconds", 0.25)
    moment[0] = 62.3456
    snapshot = registry.snapshot()
    assert snapshot["counters"]["ingest_lines"] == 3.5 and snapshot["counters"]["blob_put_bytes"] == 1024
    assert snapshot["gauges"]["spool_bytes"] == 10 and snapshot["gauges"]["ingest_lag_seconds"] == 0.25
    assert snapshot["uptime_seconds"] == 12.346
    snapshot["counters"]["ingest_lines"] = 99
    assert registry.snapshot()["counters"]["ingest_lines"] == 3.5


def test_invalid_values_are_ignored_and_unknown_names_are_kept(monkeypatch):
    warnings = []
    monkeypatch.setattr(metrics_module.log, "warning", lambda *args: warnings.append(args))
    registry = Metrics()
    for value in (-1, float("nan"), float("inf"), True, "3", None):
        registry.inc("duplicates", value)
        registry.set_gauge("gc_queue_depth", value if value != -1 else "x")
    registry.inc(["unhashable"])
    assert registry.snapshot()["counters"]["duplicates"] == 0 and registry.snapshot()["gauges"]["gc_queue_depth"] == 0
    registry.set_gauge("archive_lag_events", -5)
    assert registry.snapshot()["gauges"]["archive_lag_events"] == -5
    registry.inc("ingest_lnes")
    registry.inc("ingest_lnes")
    registry.set_gauge("spool_byts", 1)
    snapshot = registry.snapshot()
    assert snapshot["counters"]["ingest_lnes"] == 2 and snapshot["gauges"]["spool_byts"] == 1
    problems = [args[1] for args in warnings]
    assert problems.count("unknown counter") == 1 and problems.count("unknown gauge") == 1


def test_updates_from_many_threads_are_not_lost():
    registry = Metrics()

    def work():
        for _ in range(2000):
            registry.inc("blob_puts")

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert registry.snapshot()["counters"]["blob_puts"] == 16000


def test_process_registry_is_shared_until_reset():
    reset_metrics_for_tests()
    first = get_metrics()
    first.inc("exports_built")
    assert get_metrics() is first and get_metrics().snapshot()["counters"]["exports_built"] == 1
    reset_metrics_for_tests()
    assert get_metrics() is not first and get_metrics().snapshot()["counters"]["exports_built"] == 0
