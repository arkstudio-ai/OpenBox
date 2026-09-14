"""File versions observed at successful executor write boundaries."""
from __future__ import annotations

import difflib
import hashlib

from trajectory import current, enabled, record
from trajectory.redaction import sanitize


def captures_files(ctx) -> bool:
    return bool((getattr(ctx, "trace_context", None) or current()) and enabled(ctx.user_id))


async def record_file_change(ctx, path: str, *, operation: str,
                             before: str | None = None, after: str | None = None) -> None:
    if not captures_files(ctx):
        return
    context = getattr(ctx, "trace_context", None) or current()
    before_visible = sanitize(before) if before is not None else None
    after_visible = sanitize(after) if after is not None else None

    def version(value, original, absent=False):
        if value is None:
            return {"availability": "absent" if absent else "not_recorded"}
        return {"availability": "available", "text": value,
                "sha256": hashlib.sha256(value.encode()).hexdigest(),
                "size_bytes": len(value.encode()), "source": "executor_content",
                "hash_scope": "retained_content", "redacted": value != original}
    diff = None
    if before_visible is not None and after_visible is not None:
        diff = "".join(difflib.unified_diff(before_visible.splitlines(keepends=True),
                                           after_visible.splitlines(keepends=True),
                                           fromfile=path, tofile=path))
    await record("artifact.recorded", {
        "artifact_id": f"file:{context.source_session_id}:{path}",
        "artifact_type": "file_diff", "name": path.rsplit("/", 1)[-1], "path": path,
        "operation": operation, "media_type": "text/plain", "source_kind": "file_tool",
        "before": version(before_visible, before),
        "after": version(after_visible, after, absent=operation == "delete"),
        "diff": diff, "availability": "available", "capture_level": "executor_content",
    }, context=context)
