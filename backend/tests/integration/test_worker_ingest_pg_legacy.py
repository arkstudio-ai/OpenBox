"""Legacy converter (SPEC §8.14) with PostgreSQL business and trace databases.

The business database is a sibling disposable database (``<trace test database>_legacy``) that gets the legacy
trajectory migration and the ``legacy_trajectory_*`` rename; the trace database is migrated with the trace chain.
"""
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from trajectory.storage import MemoryBlobStore
from trajectory.store.models import SessionTrajectory, TrajectoryEvent, TrajectoryPayload
from tests.integration.test_worker_ingest_pg import URL, migrated, recreate  # noqa: F401
from tests.unit.test_migrate_legacy import Cli, _all, build_business, query

pytestmark = pytest.mark.skipif(not URL, reason="TRAJECTORY_TRACE_TEST_DATABASE_URL is not set")


@pytest.fixture
async def business_url(migrated):
    parsed = make_url(URL)
    url = parsed.set(database=f"{parsed.database}_legacy").render_as_string(hide_password=False)
    await recreate(url)
    yield url
    admin = create_async_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{parsed.database}_legacy" WITH (FORCE)'))
    finally:
        await admin.dispose()


async def test_converter_round_trip_on_postgresql(business_url, tmp_path, monkeypatch):
    blob_dir = tmp_path / "legacy-blobs"
    blob_dir.mkdir()
    await build_business(business_url, blob_dir)
    monkeypatch.setenv("TRAJECTORY_DATABASE_URL", URL)
    monkeypatch.setenv("TRAJECTORY_LEGACY_DATABASE_URL", business_url)
    store = MemoryBlobStore()
    cli = Cli(store, blob_dir)
    assert await cli() == 0, cli.lines
    assert await cli() == 0 and "skipped trajectory=trj_a" in cli.lines
    assert await cli("--verify") == 0, cli.lines
    trajectories = await query(URL, lambda db: _all(db, SessionTrajectory))
    assert sorted(row.id for row in trajectories) == ["trj_a", "trj_b"]
    events = await query(URL, lambda db: _all(db, TrajectoryEvent, TrajectoryEvent.trajectory_id == "trj_a"))
    assert sorted(row.seq for row in events) == [1, 2, 3, 4, 5]
    payloads = await query(URL, lambda db: _all(db, TrajectoryPayload, TrajectoryPayload.trajectory_id == "trj_a"))
    assert {row.payload_id for row in payloads} >= {"pld_img", "pld_old", "pld_dup", "pld_deleted"}
    assert await cli("--finalize-drop") == 0 and len(cli.lines) == 7
    engine = create_async_engine(business_url)
    try:
        async with engine.connect() as connection:
            remaining = (await connection.execute(text(
                "SELECT count(*) FROM pg_tables WHERE tablename LIKE 'legacy_trajectory_%'"))).scalar()
    finally:
        await engine.dispose()
    assert remaining == 0
