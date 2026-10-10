"""Record source scopes in the same transaction as events; backfill old tapes.

The recorder's user/workspace context is inherited by subagents. Missing legacy
workspace fields use the original root recording scope, never today's metadata.
A source moved to a different scope therefore cannot grant access to old bytes.
"""
from collections import defaultdict

from sqlalchemy import BigInteger, bindparam, column, values
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from trajectory.store.models import SessionTrajectory, TrajectorySessionSource
from trajectory.types import CorruptContent


async def index_events(db, events: list[dict], scopes: dict) -> None:
    """No commit. Retain distinct original scopes and advance only a contiguous prefix."""
    sources, sequences = set(), defaultdict(list)
    for event in events:
        trajectory_id, seq = event["trajectory_id"], int(event["seq"])
        root = scopes[trajectory_id]
        context = event.get("context") or {}
        source = event.get("source_session_id")
        user = event.get("user_id")
        workspace = event.get("workspace_id", context.get("workspace_id", root.workspace_id))
        if (not isinstance(source, str) or not 0 < len(source) <= 64 or user != root.user_id
                or not isinstance(workspace, str) or len(workspace) > 64):
            raise CorruptContent("Invalid recorded source scope")
        sources.add((trajectory_id, source, user, workspace))
        sequences[trajectory_id].append(seq)
    insert = sqlite_insert if db.bind.dialect.name == "sqlite" else pg_insert
    names = ("trajectory_id", "session_id", "user_id", "workspace_id")
    rows = [dict(zip(names, binding)) for binding in sorted(sources)]
    for offset in range(0, len(rows), 200):
        await db.execute(insert(TrajectorySessionSource).values(rows[offset:offset + 200]).on_conflict_do_nothing())
    bounds = []
    for trajectory_id, seqs in sequences.items():
        ordered = sorted(seqs)
        if ordered != list(range(ordered[0], ordered[-1] + 1)):
            raise CorruptContent("Recorded source index has a sequence gap")
        bounds.append((trajectory_id, ordered[0] - 1, ordered[-1]))
    table = SessionTrajectory.__table__
    for offset in range(0, len(bounds), 200):
        chunk = bounds[offset:offset + 200]
        if db.bind.dialect.name == "postgresql":
            pending = values(column("id", table.c.id.type), column("base", BigInteger),
                             column("through", BigInteger), name="source_ranges").data(chunk)
            await db.execute(table.update().where(table.c.id == pending.c.id,
                table.c.audience_seq == pending.c.base).values(audience_seq=pending.c.through))
        else:
            await db.execute(table.update().where(table.c.id == bindparam("source_id"),
                table.c.audience_seq == bindparam("source_base")).values(audience_seq=bindparam("source_head")),
                [{"source_id": identity, "source_base": base, "source_head": head} for identity, base, head in chunk])


async def backfill_sources(trajectory_id: str, *, blob_store, batch_events: int) -> int:
    """Read original events (including archived ranges), then atomically certify their prefix."""
    from sqlalchemy import select
    from trajectory.repository import stored_events
    from trajectory.store.database import trace_read_session, trace_session

    async with trace_read_session() as db:
        row = await db.get(SessionTrajectory, trajectory_id)
        if (row is None or row.deleted_at is not None or row.content_expired_at is not None
                or row.audience_seq >= row.committed_seq):
            return 0
        base, head = row.audience_seq, row.committed_seq
        events = await stored_events(db, row, base, head, batch_events, blob_store=blob_store)
    if not events or [int(event["seq"]) for event in events] != list(range(base + 1, base + len(events) + 1)):
        raise CorruptContent("Original events are missing from the recorded source index")
    async with trace_session() as db:
        locked = await db.scalar(select(SessionTrajectory).where(SessionTrajectory.id == trajectory_id,
            SessionTrajectory.deleted_at.is_(None), SessionTrajectory.content_expired_at.is_(None))
            .with_for_update(key_share=True, skip_locked=True).execution_options(populate_existing=True))
        if locked is None or locked.audience_seq != base:
            return 0
        await index_events(db, events, {trajectory_id: locked})
    return len(events)
