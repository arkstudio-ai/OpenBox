"""Personal assistant V2 foundation: drop the validation cache, keep result summaries.

V2 (docs/PERSONAL_ASSISTANT_DESIGN_V2.md) no longer re-validates saved
answers on read, so the verified-closure cache from pb4f5a6b7c8d,
pb5a6b7c8d9e and pb6b7c8d9e0f has no reader. Its epochs, row versions,
triggers, functions and sequence hold only derived counters, never user
content. TaskResult gains a bounded excerpt of the task session's final
reply, used by result reports and the watch list.

Downgrade restores the pb6b7c8d9e0f structure by replaying those three
migrations; every counter starts again from NULL.

Revision ID: pb7c8d9e0f1a
Revises: pb6b7c8d9e0f
"""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from alembic import op
import sqlalchemy as sa

revision = "pb7c8d9e0f1a"
down_revision = "pb6b7c8d9e0f"
branch_labels = None
depends_on = None

CACHE_MIGRATIONS = (
    "pb4f5a6b7c8d_assistant_evidence_versions.py",
    "pb5a6b7c8d9e_assistant_evidence_row_events.py",
    "pb6b7c8d9e0f_assistant_evidence_row_tasks.py",
)


def _drop_cache():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        triggers = bind.execute(sa.text(
            "SELECT c.relname, t.tgname FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
            "WHERE NOT t.tgisinternal AND t.tgname LIKE 'assistant\\_evidence\\_%' ORDER BY 1, 2")).fetchall()
        for table, name in triggers:
            op.execute(f'DROP TRIGGER IF EXISTS "{name}" ON "{table}"')
        functions = bind.execute(sa.text(
            "SELECT p.oid::regprocedure::text FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = current_schema() AND p.proname LIKE 'assistant\\_evidence\\_%' ORDER BY 1")).fetchall()
        for (signature,) in functions:
            op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    else:
        for (name,) in bind.execute(sa.text(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'assistant_evidence_%'")).fetchall():
            op.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    inspector = sa.inspect(bind)
    for table in sorted(inspector.get_table_names()):
        if any(column["name"] == "evidence_version" for column in inspector.get_columns(table)):
            with op.batch_alter_table(table) as batch:
                batch.drop_column("evidence_version")
    if "assistant_evidence_epochs" in inspector.get_table_names():
        op.drop_table("assistant_evidence_epochs")
    if bind.dialect.name == "postgresql":
        op.execute("DROP SEQUENCE IF EXISTS assistant_evidence_version_seq")


def upgrade():
    _drop_cache()
    op.add_column("assistant_task_results", sa.Column("summary", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("assistant_task_results", "summary")
    directory = Path(__file__).resolve().parent
    for filename in CACHE_MIGRATIONS:
        spec = spec_from_file_location(f"_assistant_cache_{filename[:12]}", directory / filename)
        module = module_from_spec(spec)
        spec.loader.exec_module(module)
        module.upgrade()
