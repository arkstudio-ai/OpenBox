"""Backfill ``trajectory_records.turn_id`` and rebuild ``trajectory_turn_summaries`` (migration t0006).

Rows projected before t0006 carry their turn only inside the ``summary`` JSON.
This copies the id into the new column, one trajectory at a time in bounded
chunks, then recomputes every turn of that trajectory with the same function
the projection worker uses. Rerunnable: a trajectory whose rows already have
the column filled is only recomputed, and recomputation is idempotent.

    TRAJECTORY_DATABASE_URL=... python -m trajectory.ops.turns_backfill [--only trj_x ...] [--limit N] [--chunk 500]

Exit status: 0 when every selected trajectory was processed, 1 when any failed
(the others are still processed), 2 on usage errors.
"""
import argparse
import asyncio
import logging
import os
import sys

from sqlalchemy import func, select, update

from trajectory import turns
from trajectory.store.database import close_trace_engine, init_trace_engine, trace_session
from trajectory.store.models import SessionTrajectory, TrajectoryRecord

log = logging.getLogger("trajectory.ops.turns_backfill")


async def backfill_trajectory(trajectory_id: str, *, chunk: int = 500) -> dict:
    """Fill the column for one trajectory and refresh its turns; returns counters."""
    filled = 0
    turn_ids: set[str] = set()
    async with trace_session() as db:
        trajectory = await db.get(SessionTrajectory, trajectory_id)
        if trajectory is None or trajectory.deleted_at is not None or trajectory.content_expired_at is not None:
            return {"trajectory_id": trajectory_id, "skipped": True, "filled": 0, "turns": 0}
        # Rows still without the column: copy the id out of the summary, in bounded pages.
        after = ""
        while True:
            rows = (await db.execute(select(TrajectoryRecord.record_id, TrajectoryRecord.summary).where(
                TrajectoryRecord.trajectory_id == trajectory_id, TrajectoryRecord.turn_id.is_(None),
                TrajectoryRecord.record_id > after).order_by(TrajectoryRecord.record_id).limit(chunk))).all()
            if not rows:
                break
            for record_id, summary in rows:
                turn_id = summary.get("turn_id") if isinstance(summary, dict) else None
                if turn_id:
                    await db.execute(update(TrajectoryRecord).where(TrajectoryRecord.trajectory_id == trajectory_id,
                                                                    TrajectoryRecord.record_id == record_id)
                                     .values(turn_id=str(turn_id)[:128]))
                    turn_ids.add(str(turn_id)[:128])
                    filled += 1
            after = rows[-1][0]
            if len(rows) < chunk:
                break
        # Every turn of the trajectory, not only the ones just filled: a rerun recomputes all of them.
        known = (await db.scalars(select(TrajectoryRecord.turn_id).where(
            TrajectoryRecord.trajectory_id == trajectory_id, TrajectoryRecord.turn_id.is_not(None)).distinct())).all()
        turn_ids.update(known)
        written = await turns.refresh(db, trajectory, turn_ids, applied_seq=trajectory.projected_seq)
    return {"trajectory_id": trajectory_id, "skipped": False, "filled": filled, "turns": written}


async def run(only: list[str], limit: int | None, chunk: int) -> int:
    query = select(SessionTrajectory.id).where(SessionTrajectory.deleted_at.is_(None),
                                               SessionTrajectory.content_expired_at.is_(None)).order_by(SessionTrajectory.id)
    if only:
        query = query.where(SessionTrajectory.id.in_(only))
    if limit:
        query = query.limit(limit)
    async with trace_session() as db:
        ids = list((await db.scalars(query)).all())
        pending = await db.scalar(select(func.count()).select_from(TrajectoryRecord).where(TrajectoryRecord.turn_id.is_(None)))
    log.info("Turn backfill: %d trajectories, %d record rows without turn_id", len(ids), pending or 0)
    failures = 0
    filled = written = 0
    for index, trajectory_id in enumerate(ids, 1):
        try:
            result = await backfill_trajectory(trajectory_id, chunk=chunk)
        except Exception as exc:  # keep going; the summary names the failures
            failures += 1
            log.warning("Turn backfill failed trajectory_id=%s error_type=%s", trajectory_id, type(exc).__name__)
            continue
        filled += result["filled"]
        written += result["turns"]
        if index % 100 == 0:
            log.info("Turn backfill: %d/%d trajectories, %d rows filled, %d turns written", index, len(ids), filled, written)
    log.info("Turn backfill done: %d trajectories, %d rows filled, %d turns written, %d failed",
             len(ids), filled, written, failures)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", nargs="*", default=[], help="trajectory ids to process (default: every live one)")
    parser.add_argument("--limit", type=int, default=None, help="process at most this many trajectories")
    parser.add_argument("--chunk", type=int, default=500, help="record rows per update page")
    args = parser.parse_args(argv)
    url = (os.getenv("TRAJECTORY_DATABASE_URL") or "").strip()
    if not url:
        print("TRAJECTORY_DATABASE_URL is not set", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    async def go() -> int:
        init_trace_engine(url)
        try:
            return await run(args.only, args.limit, max(1, args.chunk))
        finally:
            await close_trace_engine()
    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(main())
