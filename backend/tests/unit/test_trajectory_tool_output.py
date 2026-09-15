"""One call's tool output records: throttled stream records, the byte cap and the bounded final result."""
import asyncio
import hashlib

import pytest

from trajectory import tool_output
from trajectory.context import TraceContext
from trajectory.tool_output import OMITTED, ToolOutputStream, final_output


@pytest.fixture
def recorded(monkeypatch):
    import trajectory
    outputs = []

    async def record(kind, data, *, context=None, **kwargs):
        assert kind == "tool.output" and context is not None
        outputs.append(data)
    monkeypatch.setattr(trajectory, "record", record)
    return outputs


def open_stream(monkeypatch, *, interval: float, limit: int | None = None, **kwargs) -> ToolOutputStream:
    monkeypatch.setattr(tool_output, "TOOL_OUTPUT_RECORD_SECONDS", interval)
    if limit is not None:
        monkeypatch.setenv("TRAJECTORY_TOOL_OUTPUT_MAX_BYTES", str(limit))
    return ToolOutputStream(TraceContext(user_id="owner", session_id="session", call_id="call"),
                            tool="example", **kwargs)


def changes(outputs):
    return [(item["mode"], item["output"]) for item in outputs if item["stage"] == "executor_stream"]


@pytest.mark.asyncio
async def test_changes_within_the_interval_become_one_delta_or_one_replace(monkeypatch, recorded):
    stream = open_stream(monkeypatch, interval=0.05)
    await stream.update("abc")
    await stream.update("abc\n[Waiting... no output for 60s]\n")
    await stream.update("abcdef")
    await asyncio.sleep(0.08)
    await stream.update("xyz")
    await stream.update("xyz!")
    await asyncio.sleep(0.08)
    # The idle notice was never recorded: the coalesced change extends "abc".
    assert changes(recorded) == [("delta", "abc"), ("delta", "def"), ("replace", "xyz!")]
    assert [item["chunk_index"] for item in recorded] == [0, 1, 2]


@pytest.mark.asyncio
async def test_appended_chunks_are_deltas_and_a_chunk_that_does_not_follow_takes_the_whole_output(monkeypatch,
                                                                                                recorded):
    stream = open_stream(monkeypatch, interval=0)
    await stream.append("ab", output="ab")
    await stream.update("abc\n[Waiting...]\n")
    await stream.append("def", output="abcdef")  # the notice is gone from the tool's output
    await stream.append("ghi", output="abcdefghi")
    assert changes(recorded) == [("delta", "ab"), ("delta", "c\n[Waiting...]\n"), ("replace", "abcdef"),
                                 ("delta", "ghi")]


@pytest.mark.asyncio
async def test_streamed_records_stop_at_the_cap_and_the_last_one_says_so(monkeypatch, recorded):
    stream = open_stream(monkeypatch, interval=0, limit=11)
    for output in ("été", "été été", "été été é", "été été é!"):
        await stream.update(output)
    assert [(item["output"], item.get("stream_truncated")) for item in recorded] == [
        ("été", None), (" été", None), ("", True)]
    assert stream.stream_truncated

    recorded.clear()
    cut = open_stream(monkeypatch, interval=0, limit=7)
    for output in ("été", "été été", "été été été"):
        await cut.update(output)
    # A character is never split: the record keeps the bytes before it.
    assert [(item["output"], item.get("stream_truncated")) for item in recorded] == [("été", None), (" ", True)]


@pytest.mark.asyncio
async def test_close_records_what_is_pending_at_once_and_nothing_after_it(monkeypatch, recorded):
    stream = open_stream(monkeypatch, interval=10)
    await stream.update("a")
    await stream.update("ab")
    await stream.close()
    await stream.update("abc")
    await stream.append("d")
    assert changes(recorded) == [("delta", "a"), ("delta", "b")]


@pytest.mark.asyncio
async def test_a_stopped_call_drops_its_pending_output(monkeypatch, recorded):
    stopped = False
    stream = open_stream(monkeypatch, interval=0.02, stopped=lambda: stopped)
    await stream.update("a")
    await stream.update("ab")
    stopped = True
    await asyncio.sleep(0.05)
    await stream.close()
    assert changes(recorded) == [("delta", "a")]


@pytest.mark.asyncio
async def test_finish_cancels_the_pending_flush_and_records_the_final_output_last(monkeypatch, recorded):
    stream = open_stream(monkeypatch, interval=0.02)
    await stream.update("a")
    await stream.update("ab")
    await stream.finish("abc", title="done", duration_ms=1.0)
    await asyncio.sleep(0.05)
    assert recorded == [
        {"tool": "example", "output": "a", "mode": "delta", "stage": "executor_stream", "chunk_index": 0},
        {"tool": "example", "title": "done", "duration_ms": 1.0, "output": "abc", "mode": "replace",
         "stage": "executor_result", "final": True}]


@pytest.mark.asyncio
async def test_a_final_output_over_the_cap_keeps_its_head_and_tail_at_character_boundaries():
    full = "é" * 100  # 200 bytes
    bounded = await final_output(full, 51)
    # The 25 bytes of the head hold 12 characters, the 26 of the tail 13.
    assert bounded == {"output": "é" * 12 + OMITTED.format(size=150) + "é" * 13, "output_bytes": 200,
                       "output_sha256": hashlib.sha256(full.encode()).hexdigest(), "output_truncated": True}
    assert await final_output("fits", 51) == {"output": "fits"}
    assert await final_output({"structured": True}, 51) == {"output": {"structured": True}}


@pytest.mark.asyncio
async def test_a_final_output_over_1_mib_is_bounded_off_the_event_loop(monkeypatch):
    offloaded = []
    to_thread = asyncio.to_thread

    async def spy(function, *args):
        offloaded.append(function.__name__)
        return await to_thread(function, *args)
    monkeypatch.setattr(tool_output.asyncio, "to_thread", spy)
    big = "a" * (1024 * 1024 + 1)
    bounded = await final_output(big, 1024)
    assert offloaded == ["_bounded"] and bounded["output_bytes"] == len(big)
    await final_output("a" * 4096, 1024)
    assert offloaded == ["_bounded"]
