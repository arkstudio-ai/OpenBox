"""File versions observed at successful executor write boundaries."""
from __future__ import annotations

import asyncio
import difflib
import hashlib

from trajectory import current, enabled, record

#: A version that may take more UTF-8 bytes than this is hashed and diffed off the event loop.
THREAD_BYTES = 64 * 1024
#: Two versions taking more UTF-8 bytes than this together are recorded without a diff.
DIFF_MAX_BYTES = 4 * 1024 * 1024


def captures_files(ctx) -> bool:
    """Gate for the extra sandbox reads a recorded file version needs."""
    return bool((getattr(ctx, "trace_context", None) or current()) and enabled(ctx.user_id))


def _version(text: str | None, encoded: bytes | None, *, absent: bool = False) -> dict:
    if text is None:
        return {"availability": "absent" if absent else "not_recorded"}
    return {"availability": "available", "text": text, "sha256": hashlib.sha256(encoded).hexdigest(),
            "size_bytes": len(encoded), "source": "executor_content", "hash_scope": "retained_content"}


def _change(path: str, operation: str, before: str | None, after: str | None) -> dict:
    """The versions and diff of one change, each version encoded once; CPU work only, so it can run in a thread."""
    old = before.encode("utf-8", "surrogatepass") if before is not None else None
    new = after.encode("utf-8", "surrogatepass") if after is not None else None
    change = {"before": _version(before, old), "after": _version(after, new, absent=operation == "delete"),
              "diff": None}
    if old is not None and new is not None:
        if len(old) + len(new) > DIFF_MAX_BYTES:
            # difflib is quadratic in bad cases; both versions stay recorded.
            change["diff_skipped"] = "too_large"
        else:
            change["diff"] = "".join(difflib.unified_diff(before.splitlines(keepends=True),
                                                          after.splitlines(keepends=True),
                                                          fromfile=path, tofile=path))
    return change


def _large(text: str | None) -> bool:
    """Whether ``text`` may take more than ``THREAD_BYTES`` in UTF-8, judged without encoding it."""
    return text is not None and len(text) * (1 if text.isascii() else 4) > THREAD_BYTES


async def record_file_change(ctx, path: str, *, operation: str,
                             before: str | None = None, after: str | None = None) -> None:
    """Enqueue the change as the executor saw it; the worker externalizes large text."""
    if not captures_files(ctx):
        return
    context = getattr(ctx, "trace_context", None) or current()
    if _large(before) or _large(after):
        change = await asyncio.to_thread(_change, path, operation, before, after)
    else:
        change = _change(path, operation, before, after)
    await record("artifact.recorded", {
        "artifact_id": f"file:{context.source_session_id}:{path}",
        "artifact_type": "file_diff", "name": path.rsplit("/", 1)[-1], "path": path,
        "operation": operation, "media_type": "text/plain", "source_kind": "file_tool",
        **change, "availability": "available", "capture_level": "executor_content",
    }, context=context)
