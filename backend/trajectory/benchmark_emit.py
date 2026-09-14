"""Local micro-benchmark of the producer hot path; never touches a database.

``emit()`` latency covers the budget lookup, ``prepare_fast``, orjson
serialization and the enqueue. ``emit_bytes()`` latency is the enqueue alone.
The writer thread runs concurrently against a temporary spool; untimed flushes
between rounds keep the queue from overflowing so drops cannot skew samples.
"""
import argparse
from contextlib import contextmanager
import gc
import json
import logging
import math
import os
from pathlib import Path
import platform
import statistics
import tempfile
import time

KIB = 1024
MIB = 1024 * KIB
_ENV = ("TRAJECTORY_SINK", "TRAJECTORY_SPOOL_DIR", "TRAJECTORY_RECORDING_ENABLED", "TRAJECTORY_RECORD_USER_IDS",
        "TRAJECTORY_EMIT_QUEUE_BYTES", "TRAJECTORY_EMIT_MAX_EVENT_BYTES")


def percentile(values, fraction):
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))]


def event_for(target_bytes: int) -> tuple[str, dict]:
    """A realistic event whose serialized size is close to ``target_bytes``."""
    if target_bytes <= 16 * KIB:
        # About 440 bytes go to identity and envelope fields; each repeat is 49 bytes.
        text = "Observed provider text, 数字 and punctuation. " * max(1, (target_bytes - 440) // 49)
        return "request.delta", {"chunk_index": 1, "mode": "delta", "purpose": "chat", "elapsed_ms": 12.5,
                                 "blocks": [{"block_id": "text:0", "type": "text", "delta": text}]}
    messages = [{"role": "user" if index % 2 == 0 else "assistant",
                 "content": [{"type": "text", "text": "Conversation history line with details. " * 24}]}
                for index in range(target_bytes // 1030)]
    return "request.prepared", {"purpose": "chat", "model": "fixture/model", "capture_level": "adapter_input",
                                "input": {"model": "fixture/model", "messages": messages, "stream": True}}


@contextmanager
def benchmark_emitter(spool_dir: Path):
    from trajectory.emitter import get_emitter, reset_emitter_for_tests
    saved = {key: os.environ.get(key) for key in _ENV}
    os.environ.update({"TRAJECTORY_SINK": "spool", "TRAJECTORY_SPOOL_DIR": str(spool_dir),
                       "TRAJECTORY_RECORDING_ENABLED": "true", "TRAJECTORY_EMIT_QUEUE_BYTES": str(256 * MIB),
                       "TRAJECTORY_EMIT_MAX_EVENT_BYTES": str(32 * MIB)})
    os.environ.pop("TRAJECTORY_RECORD_USER_IDS", None)
    reset_emitter_for_tests()
    try:
        yield get_emitter()
    finally:
        reset_emitter_for_tests()
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def measure(target_bytes: int, samples: int, *, warmup: int = 50) -> dict:
    import orjson
    from trajectory.context import TraceContext
    from trajectory.emitter import emit
    from trajectory.types import prepare_fast

    context = TraceContext("bench_user", "bench_root", workspace_id="bench_ws", turn_id="turn_1", run_id="run_1",
                           generation=3, agent_id="agent_main", step_id="step_1", request_id="req_1")
    event_type, data = event_for(target_bytes)
    event_bytes = len(orjson.dumps(prepare_fast(context, {"type": event_type, "data": data})))
    rounds = max(1, min(samples, (128 * MIB) // event_bytes))
    with tempfile.TemporaryDirectory(prefix="openbox-emit-benchmark-") as directory:
        with benchmark_emitter(Path(directory)) as emitter:
            for _ in range(warmup):
                emit(event_type, data, context=context)
            emitter.flush(30)
            payload = orjson.dumps(prepare_fast(context, {"type": event_type, "data": data}))
            emit_samples, enqueue_samples = [], []
            gc.collect()
            clock = time.perf_counter_ns
            while len(emit_samples) < samples:
                for _ in range(min(rounds, samples - len(emit_samples))):
                    started = clock()
                    identifier = emit(event_type, data, context=context)
                    emit_samples.append(clock() - started)
                    if identifier is None:
                        raise RuntimeError("benchmark event was dropped")
                emitter.flush(60)
            while len(enqueue_samples) < samples:
                for _ in range(min(rounds, samples - len(enqueue_samples))):
                    started = clock()
                    accepted = emitter.emit_bytes(payload, user_id="bench_user", session_id="bench_root",
                                                  run_id="run_1", request_id="req_1")
                    enqueue_samples.append(clock() - started)
                    if not accepted:
                        raise RuntimeError("benchmark event was dropped")
                emitter.flush(60)
            stats = emitter.stats()
    micros = lambda values, fraction: percentile(values, fraction) / 1000
    return {"target_bytes": target_bytes, "event_bytes": event_bytes, "event_type": event_type, "samples": samples,
            "emit_p50_us": micros(emit_samples, 0.50), "emit_p99_us": micros(emit_samples, 0.99),
            "emit_mean_us": statistics.fmean(emit_samples) / 1000, "emit_max_us": max(emit_samples) / 1000,
            "emit_bytes_p50_us": micros(enqueue_samples, 0.50), "emit_bytes_p99_us": micros(enqueue_samples, 0.99),
            "dropped_events": stats["dropped_events"], "written_lines": stats["written_lines"]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=20000, help="samples for 2 KiB events")
    parser.add_argument("--large-samples", type=int, default=400, help="samples for 512 KiB events")
    parser.add_argument("--output", help="optional JSON result path")
    args = parser.parse_args(argv)
    logging.disable(logging.WARNING)
    result = {"environment": {"platform": platform.platform(), "processor": platform.machine(),
                              "python": platform.python_version()},
              "measurements": [measure(2 * KIB, args.samples), measure(512 * KIB, args.large_samples)],
              "limitations": ["Single process; the event loop is not running, so scheduling noise from a busy "
                              "server is not represented.", "Spool writes go to a local temporary directory."]}
    text = json.dumps(result, indent=2)
    if args.output:
        Path(args.output).write_text(text)
    print(text)
    return result


if __name__ == "__main__":
    main()
