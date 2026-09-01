"""Compatibility gateway for durable Cron session deliveries.

The source of truth is ``cron_delivery_outbox``.  The Agent loop still calls
``flush_pending_cron_results`` before releasing a busy main session; that call
now atomically claims outbox work instead of scanning CronRun rows.
"""
from __future__ import annotations

from core.log import create_logger


log = create_logger("cron.injector")


async def try_inject_result(run_id: str, job: dict, result_text: str) -> bool:
    """Wake/attempt an already-committed session outbox delivery.

    Kept for internal compatibility; executors must never call it before their
    exact claim settlement creates the durable row.
    """
    session_id = job.get("session_id")
    if not session_id:
        return False
    from cron.outbox import drain_deliveries, notify_outbox_workers

    notify_outbox_workers()
    attempted = await drain_deliveries(
        owner_id=f"cron-inject-{run_id}"[:160],
        kinds=("session",),
        session_id=session_id,
        user_id=job.get("user_id"),
        limit=1,
    )
    return attempted == 1


async def flush_pending_cron_results(
    session_id: str,
    user_id: str,
    *,
    run_fence: tuple[str, str, int] | None = None,
) -> int:
    """Claim and deliver committed results for one main session.

    ``run_fence`` lets the currently owning Agent flush immediately while the
    session is still BUSY.  Without it, the atomic session gateway accepts only
    an idle session.
    """
    if run_fence is not None and run_fence[0] != session_id:
        raise ValueError("Cron flush run fence targets another session")

    from cron.outbox import drain_deliveries

    return await drain_deliveries(
        owner_id=f"cron-flush-{session_id}"[:160],
        kinds=("session",),
        session_id=session_id,
        user_id=user_id,
        run_fence=run_fence,
        limit=100,
    )
