"""Stop reaches a running tool body, for tools that say it may.

The scheduler drains dispatched bodies by default: an external effect that is
already on its way cannot be undone, and dropping a paid request loses what
was paid for. That default makes a hung read keep a stopped turn "running"
until the provider's own timeout (Gaode 2026-11-13: a vision call held a
stopped turn for 4.5 minutes, and the session refused new messages meanwhile).

A tool whose body only reads, or whose in-flight request is safe to drop,
declares ``cancel_on_stop=True`` (tool/tool.py). Its body is then raced
against the turn's abort signal exactly as it is raced against its deadline,
and the call is closed with one canonical ``tool_stopped`` result. Tools that
keep their own records of a job (video_analyze) handle the signal themselves
instead, so the record ends in a terminal state.
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

#: Returned by :func:`run_until_stopped` in place of the body's result.
STOPPED = object()

STOPPED_MESSAGE = "Stopped by the user before this tool finished; it returned no result."


async def run_until_stopped(body: Callable[[], Awaitable[Any]], abort) -> Any:
    """Await ``body()``; when ``abort`` is set first, cancel it and return :data:`STOPPED`.

    The body's own cleanup (``finally`` blocks, shielded settlement) runs to
    completion before this returns. A cancellation of the caller is not a
    stop: it cancels the body and propagates.
    """
    if abort is None:
        return await body()
    if abort.is_set():
        return STOPPED
    task = asyncio.ensure_future(body())
    watch = asyncio.create_task(abort.wait())
    try:
        await asyncio.wait({task, watch}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            return task.result()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return STOPPED
    except asyncio.CancelledError:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        raise
    finally:
        watch.cancel()
