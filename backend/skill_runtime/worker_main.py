"""Standalone worker role (§4.3):

    openbox-web     -> python -m main
    openbox-worker  -> python -m skill_runtime.worker_main --queues default,media-control

Same image and code as the web role; only the process differs. The worker
claims jobs, runs bounded handler invocations, reconciles lost leases and
drains the event outbox. It never serves HTTP.
"""
from __future__ import annotations

import argparse
import asyncio
import signal

from core.log import create_logger

log = create_logger("skill_runtime.worker_main")


async def run(queues: tuple[str, ...] | None = None) -> None:
    from core.config import get_config

    config = get_config()

    from skill_runtime import registry
    from skill_runtime.embedded import ensure_job_engine
    from skill_runtime.outbox import OutboxPublisher
    from skill_runtime.reconciler import Reconciler
    from skill_runtime.worker import SkillJobWorker

    await ensure_job_engine(config)
    if config.jwt_secret:
        from bus.bus import init_redis_bus

        await init_redis_bus(config.redis_url)

    handler_count = registry.load_builtin_handlers()
    log.info(f"Loaded {handler_count} builtin handler(s)")

    worker = SkillJobWorker(
        queues=queues
        or tuple(q.strip() for q in config.skill_worker_queues.split(",") if q.strip()),
        concurrency=config.skill_worker_concurrency,
        lease_seconds=config.skill_worker_lease_seconds,
        per_user_limit=config.skill_worker_per_user_concurrency,
        invocation_timeout=config.skill_worker_invocation_timeout,
    )
    reconciler = Reconciler()
    outbox = OutboxPublisher()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    worker.start()
    reconciler.start()
    outbox.start()
    log.info("Worker role up")
    await stop.wait()

    log.info("Worker role draining…")
    await worker.stop()
    await reconciler.stop()
    await outbox.stop()

    if config.jwt_secret:
        from bus.bus import close_redis_bus

        await close_redis_bus()
    from db.base import close_engine

    await close_engine()
    log.info("Worker role stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenBox skill job worker role")
    parser.add_argument(
        "--queues",
        default=None,
        help="comma-separated queue pools (default: config skill_worker_queues)",
    )
    args = parser.parse_args()
    queues = (
        tuple(q.strip() for q in args.queues.split(",") if q.strip()) if args.queues else None
    )
    asyncio.run(run(queues))


if __name__ == "__main__":
    main()
