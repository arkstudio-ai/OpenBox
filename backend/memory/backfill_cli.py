"""Read-only operator client for the authenticated, bounded dry-run endpoint.

OPENBOX_MEMORY_TOKEN supplies a bearer token; it is never printed. This client
has no direct database access, model calls, historical scan or enqueue mode.
"""
import argparse
import asyncio
import json
import os

import httpx


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--workspace-id", required=True)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--project-id")
    selection.add_argument("--personal", action="store_true")
    parser.add_argument("--start", required=True, help="ISO date/time with timezone")
    parser.add_argument("--end", required=True, help="Exclusive ISO date/time with timezone")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    token = os.environ.get("OPENBOX_MEMORY_TOKEN")
    if not token:
        parser.error("OPENBOX_MEMORY_TOKEN is required")
    from api.memory_backfill import BackfillDryRunBody
    body = BackfillDryRunBody(project_id=args.project_id, start_at=args.start, end_at=args.end, limit=args.limit)
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(args.base_url.rstrip("/") + "/api/memory-backfill/dry-run",
            headers={"Authorization": f"Bearer {token}", "X-Workspace-Id": args.workspace_id},
            json=body.model_dump(mode="json"))
    if response.is_error:
        print(json.dumps({"status": response.status_code, "error": "Authenticated dry-run was refused"}))
        return 1
    print(json.dumps(response.json(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
