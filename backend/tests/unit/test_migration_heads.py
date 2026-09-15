"""The alembic graph must have exactly one head and no duplicate revision ids.

The container starts with `alembic upgrade head`; a duplicate id (a hand-picked
hex that already existed) made gw2 refuse to start on 2026-09-10 and forced a
rollback. sqlite unit tests use `create_all`, so only this check catches it.
"""
import re
import warnings
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND = Path(__file__).resolve().parents[2]


def test_single_head_and_unique_revision_ids():
    versions = BACKEND / "db" / "migrations" / "versions"
    ids: dict[str, list[str]] = {}
    for path in versions.glob("*.py"):
        m = re.search(r'^revision(?:\s*:\s*[^=]+)?\s*=\s*["\']([0-9a-f]+)["\']', path.read_text(encoding="utf-8"), re.M)
        assert m, f"{path.name} has no revision"
        ids.setdefault(m.group(1), []).append(path.name)
    dupes = {k: v for k, v in ids.items() if len(v) > 1}
    assert not dupes, f"duplicate revision ids: {dupes}"
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # alembic warns on duplicates; make it fatal here
        heads = ScriptDirectory.from_config(Config(str(BACKEND / "alembic.ini"))).get_heads()
    assert len(heads) == 1, f"expected one alembic head, found {heads}"


def test_head_retires_business_trajectory_tables_and_the_trace_chain_stays_separate():
    """Trajectory tables leave the business chain (SPEC §6.9); the trace database has its own chain."""
    business = ScriptDirectory.from_config(Config(str(BACKEND / "alembic.ini")))
    [head] = business.get_heads()
    lineage = {script.revision for script in business.iterate_revisions(head, "base")}
    # Dropping the old trajectory tables and the metadata sync cursor indexes.
    assert {"d3b5f7a9c1e2", "e5c7a9b1d3f4"} <= lineage
    trace_config = Config(str(BACKEND / "alembic_trajectory.ini"))
    trace_config.set_main_option("script_location", str(BACKEND / "trajectory" / "store" / "migrations"))
    trace = {script.revision for script in ScriptDirectory.from_config(trace_config).walk_revisions()}
    assert trace and not trace & {script.revision for script in business.walk_revisions()}
