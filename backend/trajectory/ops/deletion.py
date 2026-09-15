"""Checks of the session deletion drill (deploy/gw2/scripts/drill-delete-session.sh, SPEC §8.11, §12).

Runs in the worker image with the worker's environment, so it reads the trace
database of TRAJECTORY_DATABASE_URL and lists the blob store the worker's
retention deletes from (``trajectory.storage.get_blob_store()``: OSS on gw2).
It never writes.

    python -m trajectory.ops.deletion precheck --session-id S
    python -m trajectory.ops.deletion verify --session-id S [--timeout 900] [--interval 15]

``precheck`` passes while the session's trajectory is live and has objects
under its prefix, so the drill can prove that they are removed. ``verify``
waits until the trajectory is tombstoned, no garbage-collection entry for its
prefix is pending and no object is left under the prefix. Both print the last
observed state as one JSON line.

Exit status: 0 pass, 1 criterion not met (``verify``: not within --timeout),
2 usage or configuration errors (no TRAJECTORY_DATABASE_URL, no trajectory for
the session, database or store unreadable).
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from trajectory.storage import get_blob_store, trajectory_prefix

TRACE_ENV = "TRAJECTORY_DATABASE_URL"
#: Listing stops after this many keys; the drill only needs "none" or "some".
MAX_LISTED = 1000
SAMPLE_KEYS = 5
_SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class CheckError(Exception):
    """Configuration or access problems: exit status 2."""


def like_prefix(prefix: str) -> str:
    """A LIKE pattern (escape character backslash) matching the keys that start with ``prefix``."""
    return prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def inspect(engine: AsyncEngine, store, session_id: str) -> dict:
    """The session's trajectory state, its pending GC entries and the objects under its prefix."""
    async with engine.connect() as connection:
        row = (await connection.execute(
            text("SELECT id, recording_status, deleted_at FROM session_trajectories WHERE session_id = :session_id"),
            {"session_id": session_id},
        )).mappings().first()
        if row is None:
            raise CheckError(f"session {session_id} has no trajectory in the trace database")
        prefix = trajectory_prefix(row["id"])
        pending = await connection.scalar(
            text("SELECT count(*) FROM trajectory_gc_queue WHERE storage_key LIKE :pattern ESCAPE '\\'"),
            {"pattern": like_prefix(prefix)},
        )
    keys: list[str] = []
    async for key in store.list(prefix):
        keys.append(key)
        if len(keys) > MAX_LISTED:
            break
    return {
        "session_id": session_id,
        "trajectory_id": row["id"],
        "recording_status": row["recording_status"],
        "tombstoned": row["deleted_at"] is not None,
        "prefix": prefix,
        "gc_pending": int(pending or 0),
        "objects": min(len(keys), MAX_LISTED),
        "objects_truncated": len(keys) > MAX_LISTED,
        "sample_keys": keys[:SAMPLE_KEYS],
    }


def deleted(state: dict) -> bool:
    return state["tombstoned"] and state["gc_pending"] == 0 and state["objects"] == 0


async def precheck(engine: AsyncEngine, store, session_id: str, *, out) -> int:
    state = await inspect(engine, store, session_id)
    problem = None
    if state["tombstoned"]:
        problem = "the trajectory is already tombstoned"
    elif state["objects"] == 0:
        problem = ("nothing is stored under the trajectory prefix yet, so its removal cannot be shown; use a "
                   "session with archived events (idle for more than TRAJECTORY_SEGMENT_IDLE_SECONDS) or large content")
    state["passed"] = problem is None
    out(json.dumps(state, ensure_ascii=False))
    if problem:
        print(f"deletion: {problem}", file=sys.stderr)
        return 1
    return 0


async def verify(
    engine: AsyncEngine, store, session_id: str, *, timeout: float, interval: float, out,
    clock=time.monotonic, sleep=asyncio.sleep,
) -> int:
    deadline = clock() + timeout
    state = None
    while True:
        try:
            state = await inspect(engine, store, session_id)
        except CheckError:
            raise
        except Exception as exc:  # a transient database or store error must not end the wait
            print(f"deletion: check failed ({type(exc).__name__}), retrying", file=sys.stderr)
        else:
            if deleted(state):
                break
            print(
                f"deletion: waiting: tombstoned={state['tombstoned']} gc_pending={state['gc_pending']} "
                f"objects={state['objects']}",
                file=sys.stderr,
            )
        if clock() >= deadline:
            break
        await sleep(interval)
    if state is None:
        raise CheckError("the trace database or the blob store could not be read")
    state["passed"] = deleted(state)
    out(json.dumps(state, ensure_ascii=False))
    return 0 if state["passed"] else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m trajectory.ops.deletion", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (("precheck", "the trajectory is live and has stored objects"),
                            ("verify", "wait for the tombstone and the removal of every object")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--session-id", required=True)
        if name == "verify":
            command.add_argument("--timeout", type=float, default=900.0, help="seconds to wait (default 900)")
            command.add_argument("--interval", type=float, default=15.0, help="seconds between checks (default 15)")
    return parser


async def _run(args, url: str, blob_store, out, clock, sleep) -> int:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        store = blob_store if blob_store is not None else get_blob_store()
        if args.command == "precheck":
            return await precheck(engine, store, args.session_id, out=out)
        return await verify(engine, store, args.session_id, timeout=max(args.timeout, 0.0),
                            interval=max(args.interval, 0.1), out=out, clock=clock, sleep=sleep)
    finally:
        await engine.dispose()


def main(
    argv: list[str] | None = None, *, environ=None, blob_store=None, stdout=None,
    clock=time.monotonic, sleep=asyncio.sleep,
) -> int:
    args = _parser().parse_args(argv)
    env = os.environ if environ is None else environ
    stdout = sys.stdout if stdout is None else stdout
    if not _SESSION_ID.match(args.session_id):
        print("deletion: --session-id must be letters, digits, '_' or '-'", file=sys.stderr)
        return 2
    url = env.get(TRACE_ENV)
    if not url:
        print(f"deletion: {TRACE_ENV} is not set", file=sys.stderr)
        return 2

    def out(line: str) -> None:
        stdout.write(line + "\n")
        stdout.flush()

    try:
        return asyncio.run(_run(args, url, blob_store, out, clock, sleep))
    except CheckError as exc:
        print(f"deletion: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Type only: messages of connection errors may carry the database URL.
        print(f"deletion: the check could not run: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
