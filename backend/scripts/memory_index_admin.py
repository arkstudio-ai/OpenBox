"""Bounded operator repair of derived indexes, with explicit scope and cost consent.

Run in the deployment's normal environment. This command never changes the
selected read generation; use MEMORY_INDEX_GENERATION after completing repair
and checking every paginated scope, including tombstone cleanup.
"""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def execute(args):
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    from core.config import get_config
    from db.base import ensure_engine, get_db_session
    from memory.index.qdrant import QdrantMemoryIndex
    from memory.policy import resolve_access_scope
    config = get_config()
    await ensure_engine(config)
    async with get_db_session() as db:
        scope = await resolve_access_scope(db, user_id=args.user_id, workspace_id=args.workspace_id,
                                           project_id=args.project_id)
    index = QdrantMemoryIndex(config.memory, generation=args.generation)
    if args.operation == "health":
        status = await index.health()
        status.pop("points_count", None)
        return status
    if not args.confirm_cost:
        raise ValueError("Repair can enqueue embeddings; --confirm-cost is required")
    if not config.memory.enabled("index_sync", scope.user_id):
        raise ValueError("Index synchronization is disabled for this actor")
    method = index.reconcile if args.operation == "reconcile" else index.rebuild_generation
    return await method(scope, limit=args.limit, cursor=args.cursor)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["health", "reconcile", "rebuild"])
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--project-id")
    parser.add_argument("--generation")
    parser.add_argument("--cursor")
    parser.add_argument("--limit", type=int, choices=range(1, 1001), default=200, metavar="1..1000")
    parser.add_argument("--confirm-cost", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(asyncio.run(execute(args)), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__, "message": "Index operation failed; check scope/configuration and server diagnostics"}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
