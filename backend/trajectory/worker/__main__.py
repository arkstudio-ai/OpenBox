"""``python -m trajectory.worker``: check the trace schema, then serve the worker app (SPEC §8.0).

The deployment runs ``alembic -c alembic_trajectory.ini upgrade head`` first; a
worker never serves an older schema.
"""
import asyncio
import os
import sys

from dotenv import load_dotenv

from trajectory.config import BACKEND_DIR, integer

#: WebSocket ping interval and timeout (uvicorn ``ws_ping_interval``), below the proxies' idle timeouts.
PING_SECONDS = 20.0


def main() -> int:
    load_dotenv(BACKEND_DIR / ".env")
    import uvicorn
    from trajectory.worker.app import check_schema, create_app

    url = (os.getenv("TRAJECTORY_DATABASE_URL") or "").strip()
    if not url:
        print("trajectory worker: TRAJECTORY_DATABASE_URL is not set", file=sys.stderr)
        return 2
    try:
        current = asyncio.run(check_schema(url))
    except Exception as exc:
        print(f"trajectory worker: cannot check the trace database schema ({type(exc).__name__})", file=sys.stderr)
        return 3
    if not current:
        print("trajectory worker: the trace database is not at the migration head; "
              "run `alembic -c alembic_trajectory.ini upgrade head` first", file=sys.stderr)
        return 3
    uvicorn.run(
        create_app(database_url=url),
        host=(os.getenv("TRAJECTORY_WORKER_HOST") or "").strip() or "0.0.0.0",
        port=integer("TRAJECTORY_WORKER_PORT", 8090),
        ws_ping_interval=PING_SECONDS,
        ws_ping_timeout=PING_SECONDS,
        lifespan="on",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
