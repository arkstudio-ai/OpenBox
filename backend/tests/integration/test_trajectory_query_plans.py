"""Index access at 100,000 sessions on the pipeline's disposable PostgreSQL database."""
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from tests.integration.test_trajectory_pipeline_e2e import trace_url  # noqa: F401
from trajectory.repository import list_sessions
from trajectory.store.database import close_trace_engine, init_trace_engine, trace_session
from trajectory.worker import projection
from trajectory.worker.settings import WorkerSettings


def _nodes(plan):
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


async def test_idle_poll_and_cursor_pages_use_indexes_at_scale(trace_url, monkeypatch, record_property):
    if not trace_url.startswith("postgresql"):
        pytest.skip("PostgreSQL query plans")
    engine = init_trace_engine(trace_url)
    plans = []

    class Observed:
        def __init__(self, db):
            self.db = db

        async def explain(self, query):
            sql = str(query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
            result = await self.db.scalar(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql))
            plans.append(result[0])

        async def scalars(self, query):
            await self.explain(query)
            return await self.db.scalars(query)

        async def execute(self, query):
            await self.explain(query)
            return await self.db.execute(query)

    @asynccontextmanager
    async def observed_session():
        async with trace_session() as db:
            yield Observed(db)

    try:
        async with engine.begin() as db:
            await db.execute(text("SET LOCAL statement_timeout = '30s'"))
            await db.execute(text("""
                INSERT INTO session_trajectories
                    (id, user_id, session_id, workspace_id, started_at, updated_at, last_activity_at,
                     committed_seq, projected_seq, archived_seq, checkpoint_seq)
                SELECT 'trj_' || lpad(n::text, 6, '0'), 'u', 's_' || lpad(n::text, 6, '0'), 'w',
                       now(), now(), now(), 10, 10, 10, 0
                FROM generate_series(1, 100000) n
            """))
            await db.execute(text("""
                INSERT INTO trajectory_meta_sessions (id, user_id, workspace_id, updated_at, synced_at, projected_activity_at)
                SELECT session_id, user_id, workspace_id, updated_at, updated_at, updated_at
                FROM session_trajectories
            """))
            await db.execute(text("""
                INSERT INTO trajectory_session_summaries
                    (trajectory_id, user_id, session_id, workspace_id, last_activity_at,
                     running_status, recording_status, applied_seq, statistics)
                SELECT id, user_id, session_id, workspace_id, updated_at, 'idle', 'recording', 10, '{}'::jsonb
                FROM session_trajectories
            """))
            await db.execute(text("ANALYZE"))
        monkeypatch.setattr(projection, "trace_session", observed_session)
        settings = replace(WorkerSettings.from_env(), checkpoint_interval=1000)
        worker = projection.ProjectionService(settings, blob_store=None, metrics=None)
        assert await worker._candidates() == []
        nodes = list(_nodes(plans[-1]["Plan"]))
        indexes = {node.get("Index Name") for node in nodes}
        assert {"ix_trajectories_projection_pending", "ix_trajectories_checkpoint_backlog"} <= indexes
        assert sum(node.get("Rows Removed by Filter", 0) for node in nodes) == 0
        record_property("idle_poll_ms", plans[-1]["Execution Time"])

        async with observed_session() as db:
            first = await list_sessions(db, limit=50)
            second = await list_sessions(db, limit=50, cursor=first["next_cursor"])
        assert len(first["items"]) == len(second["items"]) == 50
        assert not {row["session_id"] for row in first["items"]} & {row["session_id"] for row in second["items"]}
        for page, plan in enumerate(plans[-2:]):
            nodes = list(_nodes(plan["Plan"]))
            assert "ix_trajectory_meta_sessions_activity" in {node.get("Index Name") for node in nodes}
            assert not any(node["Node Type"] == "Sort" and node["Actual Rows"] > 51 for node in nodes)
            record_property(f"page_{page}_ms", plan["Execution Time"])
    finally:
        await close_trace_engine()
