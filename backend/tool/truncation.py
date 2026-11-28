"""Output truncation for tool results.

Truncates tool output to MAX_LINES/MAX_BYTES, keeping the beginning and the
end, and saves the full output to a file on the backend host for operators.
The model is not told about that file: its file tools run in the sandbox and
cannot reach the backend host. Includes periodic cleanup of old files.
"""
import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path

from core.log import create_logger

log = create_logger("truncation")

MAX_LINES = 2000
MAX_BYTES = 50 * 1024  # 50KB
RETENTION_MS = 7 * 24 * 3600 * 1000  # 7 days in ms
CLEANUP_INTERVAL_S = 3600  # 1 hour

# Storage directory for truncated output files
_data_dir: str | None = None
_cleanup_task: asyncio.Task | None = None


def _get_data_dir() -> str:
    """Get or create the data directory for truncated output."""
    global _data_dir
    if _data_dir is None:
        data_home = os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share"))
        _data_dir = os.path.join(data_home, "openbox", "tool-output")
        os.makedirs(_data_dir, exist_ok=True)
    return _data_dir


@dataclass
class TruncateResult:
    content: str
    truncated: bool = False
    output_path: str | None = None


# Share of the line and byte budget kept from the end of the output when both
# ends are kept. The end usually carries the result (errors, exit status,
# totals); the beginning carries the context that explains it.
TAIL_SHARE = 0.2

# The full output is stored on the backend host, which the model's file tools
# (they run in the sandbox) cannot reach, so the note only suggests things the
# model can do itself.
OMITTED_NOTE = (
    "[... {lines} lines ({bytes} bytes) omitted from the middle of this output. "
    "The omitted part is not available to you; if you need it, run the tool "
    "again with narrower output (filter it, or select a range with "
    "offset/limit, head, tail or sed -n). ...]"
)


def _char_end_within_bytes(text: str, max_bytes: int) -> int:
    """Largest character index whose UTF-8 prefix fits in ``max_bytes``."""
    if max_bytes <= 0:
        return 0
    prefix = text[:max_bytes].encode("utf-8")[:max_bytes]
    return len(prefix.decode("utf-8", errors="ignore"))


def _char_start_within_bytes(text: str, max_bytes: int) -> int:
    """Smallest character index whose UTF-8 suffix fits in ``max_bytes``."""
    if max_bytes <= 0:
        return len(text)
    suffix = text[-max_bytes:].encode("utf-8")
    if len(suffix) > max_bytes:
        suffix = suffix[-max_bytes:]
    return len(text) - len(suffix.decode("utf-8", errors="ignore"))


def _head_end(text: str, max_lines: int, max_bytes: int) -> int:
    """End (exclusive) of the head kept within the line and byte budgets."""
    if max_lines <= 0 or max_bytes <= 0:
        return 0
    line_end = -1
    for _ in range(max_lines):
        line_end = text.find("\n", line_end + 1)
        if line_end == -1:
            line_end = len(text)
            break
    end = min(line_end, _char_end_within_bytes(text, max_bytes))
    if end < line_end:
        # Cut by bytes: end on a line boundary unless no whole line fits.
        boundary = text.rfind("\n", 0, end)
        if boundary > 0:
            end = boundary
    return end


def _tail_start(text: str, max_lines: int, max_bytes: int, floor: int) -> int:
    """Start of the tail kept within the budgets, never before ``floor``."""
    if max_lines <= 0 or max_bytes <= 0:
        return len(text)
    line_start = len(text)
    newline = len(text)
    for _ in range(max_lines):
        newline = text.rfind("\n", floor, newline)
        if newline == -1:
            line_start = floor
            break
        line_start = newline + 1
    start = max(line_start, _char_start_within_bytes(text, max_bytes), floor)
    if start > line_start:
        # Cut by bytes: start on a line boundary unless no whole line fits.
        boundary = text.find("\n", start)
        if boundary != -1 and boundary + 1 < len(text):
            start = boundary + 1
    return start


def _save_full_output(text: str) -> str | None:
    try:
        from core.identifier import ascending
        filepath = os.path.join(_get_data_dir(), ascending("tool"))
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(text)
        return filepath
    except Exception as e:
        log.warning(f"Failed to save truncated output: {e}")
        return None


async def truncate_output(
    text: str,
    max_lines: int = MAX_LINES,
    max_bytes: int = MAX_BYTES,
    direction: str = "both",
) -> TruncateResult:
    """Truncate tool output to stay within limits.

    ``direction`` is ``"both"`` (keep the beginning and the end, the default),
    ``"head"`` or ``"tail"``. The full output is still saved on the backend
    host, but the model is not pointed at it: none of its tools can read
    that location.
    """
    if not text:
        return TruncateResult(content="", truncated=False)

    total_lines = text.count("\n") + 1
    total_bytes = len(text.encode("utf-8"))

    # Check if within limits
    if total_lines <= max_lines and total_bytes <= max_bytes:
        return TruncateResult(content=text, truncated=False)

    if direction == "head":
        tail_lines = tail_bytes = 0
    elif direction == "tail":
        tail_lines, tail_bytes = max_lines, max_bytes
    else:
        tail_lines = int(max_lines * TAIL_SHARE)
        tail_bytes = int(max_bytes * TAIL_SHARE)
    head_end = _head_end(text, max_lines - tail_lines, max_bytes - tail_bytes)
    tail_start = _tail_start(text, tail_lines, tail_bytes, head_end)
    head = text[:head_end]
    tail = text[tail_start:]
    if tail.startswith("\n"):
        tail = tail[1:]

    kept_lines = (head.count("\n") + 1 if head else 0) + (tail.count("\n") + 1 if tail else 0)
    note = OMITTED_NOTE.format(
        lines=max(total_lines - kept_lines, 0),
        bytes=total_bytes - len(head.encode("utf-8")) - len(tail.encode("utf-8")),
    )
    content = "\n\n".join(part for part in (head, note, tail) if part)

    return TruncateResult(
        content=content,
        truncated=True,
        output_path=_save_full_output(text),
    )


async def cleanup_old_outputs() -> int:
    """Delete truncated output files older than RETENTION_MS.

    Returns the number of files deleted.
    """
    data_dir = _get_data_dir()
    deleted = 0
    cutoff_ms = time.time() * 1000 - RETENTION_MS

    try:
        for filename in os.listdir(data_dir):
            filepath = os.path.join(data_dir, filename)
            if not os.path.isfile(filepath):
                continue
            try:
                mtime_ms = os.path.getmtime(filepath) * 1000
                if mtime_ms < cutoff_ms:
                    os.remove(filepath)
                    deleted += 1
            except OSError:
                continue
    except Exception as e:
        log.warning(f"Cleanup error: {e}")

    if deleted > 0:
        log.info(f"Cleaned up {deleted} old truncated output files")

    return deleted


async def _cleanup_loop():
    """Periodic cleanup loop, runs every CLEANUP_INTERVAL_S."""
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_S)
        try:
            await cleanup_old_outputs()
        except Exception as e:
            log.warning(f"Cleanup loop error: {e}")


def start_cleanup_task():
    """Start the periodic cleanup task."""
    global _cleanup_task
    if _cleanup_task is None or _cleanup_task.done():
        _cleanup_task = asyncio.create_task(_cleanup_loop())
        log.info("Started truncation cleanup task")


def stop_cleanup_task():
    """Stop the periodic cleanup task."""
    global _cleanup_task
    if _cleanup_task and not _cleanup_task.done():
        _cleanup_task.cancel()
        _cleanup_task = None
