#!/usr/bin/env python
"""Print the voice dispatch metrics for the last days (docs/VOICE_DISPATCH_PLAN_20261112.md §4.7).

Read-only. Run from ``backend/`` with the server's environment, so
``DATABASE_URL`` resolves (inside the backend container: ``python scripts/voice_metrics.py``):

    uv run python scripts/voice_metrics.py                 # last 7 days
    uv run python scripts/voice_metrics.py --days 1 --json
    uv run python scripts/voice_metrics.py --user 01M1... --days 30

Three of the six numbers need ``voice.line_log`` to have been on for the window.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


async def _run(args) -> int:
    from core.config import get_config
    from db.base import init_engine
    from voice.metrics import measure, render

    config = get_config()
    init_engine(config.database_url, config.db_pool_size, config.db_pool_overflow)

    until = datetime.now(timezone.utc)
    report = await measure(until - timedelta(days=args.days), until, user_id=args.user)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render(report))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=float, default=7.0)
    parser.add_argument("--user", default=None, help="one user's calls only (user id)")
    parser.add_argument("--json", action="store_true")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
