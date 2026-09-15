"""The ``tool.output`` records of one tool call.

Tools push their whole collected output on every chunk (``ToolContext.update_output``);
bash, past its chat budget, appends chunks instead. A ``ToolOutputStream`` turns both
into records of the new text:

- ``executor_stream`` records hold one delta of the text added since the last record,
  or one replace when the text was rewritten. A call makes at most one per
  ``TOOL_OUTPUT_RECORD_SECONDS``: the first change is recorded at once, later changes
  are coalesced and a timer records them when the interval ends.
- They stop at ``TRAJECTORY_TOOL_OUTPUT_MAX_BYTES`` for the call; the last one says
  ``stream_truncated``.
- ``finish`` records the ``executor_result``, the call's last ``tool.output``: the whole
  output when it fits the same cap, otherwise the first and last half of the cap around
  a marker line, with ``output_bytes``, ``output_sha256`` and ``output_truncated``.

``trajectory.record`` enqueues without awaiting anything (SPEC §5.3): records reach the
spool, and get their seq, in the order they are made.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from typing import Any

import trajectory
from trajectory.config import integer

log = logging.getLogger(__name__)

#: Shortest interval between two ``executor_stream`` records of one call.
TOOL_OUTPUT_RECORD_SECONDS = 1.0
#: Default of ``TRAJECTORY_TOOL_OUTPUT_MAX_BYTES``.
TOOL_OUTPUT_MAX_BYTES = 256 * 1024
#: A final output above this size is encoded and hashed off the event loop.
THREAD_BYTES = 1024 * 1024
OMITTED = "\n[... {size} bytes omitted ...]\n"


def max_output_bytes() -> int:
    """UTF-8 bytes of output one call records: its stream records together, and its final record."""
    return integer("TRAJECTORY_TOOL_OUTPUT_MAX_BYTES", TOOL_OUTPUT_MAX_BYTES)


def _encode(text: str) -> bytes:
    return text.encode("utf-8", "surrogatepass")


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "surrogatepass")


def _size(text: str) -> int:
    return len(text) if text.isascii() else len(_encode(text))


def _boundary(encoded: bytes, index: int, step: int) -> int:
    """``index`` moved by ``step`` to the first byte of a UTF-8 sequence."""
    while 0 < index < len(encoded) and encoded[index] & 0xC0 == 0x80:
        index += step
    return index


def _prefix(text: str, limit: int) -> str:
    """The longest prefix of ``text`` within ``limit`` UTF-8 bytes."""
    text = text[:limit]  # a character takes at least one byte
    if text.isascii():
        return text
    encoded = _encode(text)
    return text if len(encoded) <= limit else _decode(encoded[:_boundary(encoded, limit, -1)])


def _bounded(output: str, limit: int, encoded: bytes | None = None) -> dict:
    encoded = _encode(output) if encoded is None else encoded
    size = len(encoded)
    if size <= limit:
        return {"output": output}
    head = _boundary(encoded, limit // 2, -1)
    tail = _boundary(encoded, size - (limit - limit // 2), 1)
    return {"output": _decode(encoded[:head]) + OMITTED.format(size=tail - head) + _decode(encoded[tail:]),
            "output_bytes": size, "output_sha256": hashlib.sha256(encoded).hexdigest(), "output_truncated": True}


async def final_output(output: Any, limit: int) -> dict:
    """The output fields of a final record: the whole output within ``limit`` bytes, else its head and tail."""
    if not isinstance(output, str) or (len(output) <= limit and output.isascii()):
        return {"output": output}
    if len(output) > THREAD_BYTES:  # at least as many bytes as characters
        return await asyncio.to_thread(_bounded, output, limit)
    encoded = _encode(output)
    if len(encoded) > THREAD_BYTES:
        return await asyncio.to_thread(_bounded, output, limit, encoded)
    return _bounded(output, limit, encoded)


class ToolOutputStream:
    """The ``tool.output`` records of one tool call, made from the call's event loop."""

    def __init__(self, context, *, tool: str | None = None, owner: Any = None,
                 stopped: Callable[[], bool] | None = None):
        self.context = context
        self.tool = tool
        #: The executor the stream was opened for: ``define_tool`` records into the hooks' stream of its own call only.
        self.owner = owner
        self.stream_truncated = False
        self.closed = False
        self.finished = False
        self._stopped = stopped
        self._interval = TOOL_OUTPUT_RECORD_SECONDS
        self._limit = max_output_bytes()
        self._recorded = ""  # the output as the stream records replay it
        self._streamed = 0  # UTF-8 bytes of the stream records
        self._seen = 0  # characters of the whole output as last observed
        self._latest: str | None = None  # a newer whole output, not recorded yet
        self._appended: list[str] = []  # text after it (or after the recorded output), not recorded yet
        self._appended_chars = 0
        self._index = 0
        self._last = float("-inf")  # monotonic time of the last stream record
        self._timer: asyncio.Task | None = None
        self._sleeping = False

    def _open(self) -> bool:
        return (self.context is not None and not self.closed and not self.stream_truncated
                and not (self._stopped is not None and self._stopped()))

    async def update(self, output: str) -> None:
        """The whole output so far, as tools push it on every chunk."""
        if not isinstance(output, str) or not self._open():
            return
        if not self._appended and output == (self._recorded if self._latest is None else self._latest):
            return
        self._latest, self._seen = output, len(output)
        if self._appended:
            self._appended, self._appended_chars = [], 0
        await self._schedule()

    async def append(self, chunk: str, *, output: str | None = None) -> None:
        """``chunk`` added to the end of the output.

        ``output``, the whole output including the chunk, stands in when the chunk
        does not follow the output the stream has seen (nothing was pushed before).
        """
        if not chunk or not self._open():
            return
        if output is not None and len(output) != self._seen + len(chunk):
            self._rebase(output)
        else:
            self._seen += len(chunk)
            # Text past the cap is never recorded, so it is not kept either.
            if self._appended_chars <= self._limit - self._streamed:
                self._appended.append(chunk)
            self._appended_chars += len(chunk)
        await self._schedule()

    def _rebase(self, output: str) -> None:
        """Take ``output`` as the pending text without keeping ``output`` itself: bash extends
        that string in place, and a second reference would make it copy the string per chunk."""
        room = self._limit - self._streamed
        recorded = self._recorded
        self._seen = len(output)
        if output.startswith(recorded):
            self._latest = None
            self._appended = [output[len(recorded):len(recorded) + room + 1]]
            self._appended_chars = len(output) - len(recorded)
        else:
            self._latest, self._appended, self._appended_chars = output[:room + 1], [], 0

    async def _schedule(self) -> None:
        if self._timer is not None:
            return  # the timer records this change with the others
        wait = self._last + self._interval - time.monotonic()
        if wait <= 0:
            await self._flush()
        else:
            self._sleeping = True
            self._timer = asyncio.get_running_loop().create_task(self._flush_later(wait))

    async def _flush_later(self, wait: float) -> None:
        try:
            await asyncio.sleep(wait)
        finally:
            self._sleeping = False
        try:
            await self._flush()
        except Exception as exc:  # recording never fails the call
            log.warning("Tool output was not recorded error_type=%s", type(exc).__name__)
        finally:
            if self._timer is asyncio.current_task():
                self._timer = None
        if not self.closed and (self._latest is not None or self._appended):
            await self._schedule()  # a change made while the record was being made

    async def _flush(self) -> None:
        """Record what is pending: one delta of the added text, or one replace of rewritten text."""
        latest, appended, appended_chars = self._latest, self._appended, self._appended_chars
        if latest is None and not appended:
            return
        self._latest, self._appended, self._appended_chars = None, [], 0
        if self.context is None or self.stream_truncated or (self._stopped is not None and self._stopped()):
            return
        recorded, room = self._recorded, self._limit - self._streamed
        if latest is None or latest.startswith(recorded):
            mode = "delta"
            text = "" if latest is None else latest[len(recorded):len(recorded) + room + 1]
            wanted = (0 if latest is None else len(latest) - len(recorded)) + appended_chars
        else:
            mode, text, wanted = "replace", latest[:room + 1], len(latest) + appended_chars
        for chunk in appended:
            if len(text) > room:
                break
            text += chunk
        kept = _prefix(text, room)
        truncated = len(kept) < wanted
        if mode == "delta" and not kept and not truncated:
            return
        self._last = time.monotonic()
        self._streamed += _size(kept)
        index, self._index = self._index, self._index + 1
        data = {"output": kept, "mode": mode, "stage": "executor_stream", "chunk_index": index}
        if self.tool is not None:
            data["tool"] = self.tool
        if truncated:
            data["stream_truncated"] = True
            self.stream_truncated = True
            self._recorded = ""
        else:
            self._recorded = recorded + kept if mode == "delta" else kept
        await self._record(data)

    async def _settle(self) -> None:
        """Stop the timer: cancelled while it sleeps, awaited while it records."""
        timer, self._timer = self._timer, None
        if timer is None or timer.done():
            return
        if self._sleeping:
            timer.cancel()
        else:
            await asyncio.wait((timer,))

    async def close(self) -> None:
        """End a call that has no final result: pending output is recorded now, and nothing after it."""
        if self.closed:
            return
        self.closed = True
        await self._settle()
        await self._flush()

    async def finish(self, output: Any, **fields: Any) -> None:
        """Record the call's ``executor_result`` as its last ``tool.output``; it supersedes pending output."""
        if self.finished:
            return
        self.finished = self.closed = True
        await self._settle()
        self._latest, self._appended, self._appended_chars = None, [], 0
        if self.context is None:
            return
        data = {**fields, **await final_output(output, self._limit),
                "mode": "replace", "stage": "executor_result", "final": True}
        if self.tool is not None:
            data["tool"] = self.tool
        await self._record(data)

    async def _record(self, data: dict) -> None:
        await trajectory.record("tool.output", data, context=self.context)


def output_stream(ctx, tool: str | None = None) -> ToolOutputStream:
    """The output stream of the call running on ``ctx``; a call run without the hooks or ``define_tool`` gets one."""
    stream = getattr(ctx, "_trajectory_output_stream", None)
    if stream is None or stream.closed:
        stream = ToolOutputStream(getattr(ctx, "trace_context", None) or trajectory.current(), tool=tool)
        ctx._trajectory_output_stream = stream
    return stream
