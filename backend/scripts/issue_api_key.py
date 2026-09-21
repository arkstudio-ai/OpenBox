#!/usr/bin/env python
"""Issue (or revoke) an API key for the public /v1 harness API.

The secret is printed once and never stored. Run from ``backend/`` with the
same environment the server uses, so ``DATABASE_URL`` resolves:

    uv run python scripts/issue_api_key.py --user gaode --workspace ws_xxx --name "高德测试"
    uv run python scripts/issue_api_key.py --user gaode --name "高德测试" --expires-days 90 \\
        --scopes sessions:read,sessions:write,files:read,files:write \\
        --policy '{"interaction_timeout_s": 600, "max_concurrent_sessions": 5}'
    uv run python scripts/issue_api_key.py --revoke key_01J...
    uv run python scripts/issue_api_key.py --list --user gaode

``--user`` accepts a user id or username. ``--workspace`` defaults to the
user's default workspace.
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


async def _resolve_user(ident: str) -> dict:
    from db.repository.user_repo import PgUserRepo

    repo = PgUserRepo()
    user = await repo.get(ident) or await repo.get_by_username(ident)
    if user is None:
        raise SystemExit(f"user not found: {ident}")
    return user


async def _issue(args) -> int:
    from auth.api_key import DEFAULT_SCOPES, issue_key

    user = await _resolve_user(args.user)
    workspace_id = args.workspace or str(user.get("default_workspace_id") or "")
    if not workspace_id:
        raise SystemExit("user has no default workspace; pass --workspace")
    scopes = [s.strip() for s in args.scopes.split(",") if s.strip()] if args.scopes else list(DEFAULT_SCOPES)
    policy = json.loads(args.policy) if args.policy else None
    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=args.expires_days)
        if args.expires_days else None
    )
    try:
        row, secret = await issue_key(
            user_id=user["id"], workspace_id=workspace_id, name=args.name,
            scopes=scopes, policy=policy, rate_limit=args.rate_limit, expires_at=expires_at,
        )
    except (LookupError, ValueError) as exc:
        raise SystemExit(str(exc))
    print(json.dumps({
        "id": row.id,
        "user_id": row.user_id,
        "workspace_id": row.workspace_id,
        "name": row.name,
        "scopes": row.scopes,
        "policy": row.policy,
        "rate_limit": row.rate_limit,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        # Shown once. There is no way to read it back.
        "secret": secret,
    }, ensure_ascii=False, indent=2))
    return 0


async def _revoke(args) -> int:
    from auth.api_key import revoke_key

    if await revoke_key(args.revoke):
        print(f"revoked {args.revoke}")
        return 0
    print(f"key not found or already revoked: {args.revoke}", file=sys.stderr)
    return 1


async def _list(args) -> int:
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.api_key import ApiKey

    user = await _resolve_user(args.user)
    async with get_db_session() as db:
        rows = (await db.scalars(
            select(ApiKey).where(ApiKey.user_id == user["id"]).order_by(ApiKey.created_at)
        )).all()
    for row in rows:
        state = "revoked" if row.revoked_at else "active"
        print(f"{row.id}\t{row.key_prefix}…\t{state}\t{row.workspace_id}\t{row.name}\t"
              f"last_used={row.last_used_at.isoformat() if row.last_used_at else '-'}")
    return 0


async def _main(args) -> int:
    from core.config import get_config
    from db.base import close_engine, init_engine

    config = get_config()
    if not config.jwt_secret:
        raise SystemExit("API keys need multi-user mode (JWT_SECRET / DATABASE_URL configured)")
    init_engine(config.database_url, config.db_pool_size, config.db_pool_overflow)
    try:
        if args.revoke:
            return await _revoke(args)
        if args.list:
            return await _list(args)
        return await _issue(args)
    finally:
        await close_engine()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--user", help="user id or username the key acts as")
    parser.add_argument("--workspace", help="workspace id (default: the user's default workspace)")
    parser.add_argument("--name", help="label shown in listings")
    parser.add_argument("--scopes", help="comma-separated; default: all four")
    parser.add_argument("--policy", help="JSON object merged over the default policy")
    parser.add_argument("--rate-limit", help='e.g. "60/minute"; default: config.rate_limit_api')
    parser.add_argument("--expires-days", type=int, help="expire after N days; default: never")
    parser.add_argument("--revoke", metavar="KEY_ID", help="revoke this key instead of issuing")
    parser.add_argument("--list", action="store_true", help="list the user's keys instead of issuing")
    args = parser.parse_args()
    if not args.revoke and not args.user:
        parser.error("--user is required")
    if not args.revoke and not args.list and not args.name:
        parser.error("--name is required when issuing")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
