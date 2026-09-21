"""Isolation for event-journal fixtures which otherwise remain recovery candidates."""
import asyncio

import pytest
from sqlalchemy import delete, select


@pytest.fixture(autouse=True)
async def isolate_team_journals(request, ensure_test_db):
    if not request.node.path.name.startswith("test_team_"):
        yield
        return
    from db.base import get_db_session
    from db.models.team import TeamEvent, TeamRun
    from team import runtime_binding, scheduler
    async with get_db_session() as db:
        existing = set((await db.execute(select(TeamRun.id))).scalars().all())
    token = runtime_binding._current.set(None)
    try:
        yield
    finally:
        tasks = list(scheduler._scheduled.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        runtime_binding._current.reset(token)
        async with get_db_session() as db:
            created = set((await db.execute(select(TeamRun.id))).scalars().all()) - existing
            if created:
                await db.execute(delete(TeamEvent).where(TeamEvent.team_run_id.in_(created)))
                await db.execute(delete(TeamRun).where(TeamRun.id.in_(created)))
