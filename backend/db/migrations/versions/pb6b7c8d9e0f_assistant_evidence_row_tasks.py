"""Track assistant tasks, commands, submissions and results per row for cached verdicts.

These four tables were cold: one epoch per (owner, table). Every new command
(any tasks.followup), submission, result or task state change therefore
invalidated every cached verdict of that owner that read the table, and the
recaptures competed with the running turn. Like agent events and inbox items
(pb5a6b7c8d9e), they now carry a per-row evidence_version changed by every
UPDATE; a capture records the rows it reads by key, or whole bounded or
derived sets, and only deletes and identity changes bump the owner's epoch.
Existing rows keep NULL versions.

Revision ID: pb6b7c8d9e0f
Revises: pb5a6b7c8d9e
"""
from alembic import op
import sqlalchemy as sa

revision = "pb6b7c8d9e0f"
down_revision = "pb5a6b7c8d9e"
branch_labels = None
depends_on = None

# Frozen copy of db.evidence_schema at this revision: identity columns and
# owner (default user_id) of each table moved from cold to per-row coverage.
ROWS = {
    "assistant_commands": ("id", "actor_user_id", "workspace_id", "assistant_session_id"),
    "assistant_task_results": ("id", "task_id", "result_message_id", "processed_message_id"),
    "assistant_task_submissions": ("id", "task_id", "command_id", "inbox_id"),
    "assistant_tasks": ("id", "user_id", "workspace_id", "assistant_session_id", "execution_session_id", "project_id"),
}
OWNERS = {
    "assistant_commands": "actor_user_id",
    "assistant_task_results": "assistant_tasks.user_id:task_id",
    "assistant_task_submissions": "assistant_tasks.user_id:task_id",
    "assistant_tasks": "user_id",
}
ROW_TRIGGERS = ("assistant_evidence_version", "assistant_evidence_touch_update", "assistant_evidence_touch_delete",
                "assistant_evidence_flush_update", "assistant_evidence_flush_delete")


def _literal(value):
    return "'" + value.replace("'", "''") + "'"


def _args(*values):
    return ", ".join(_literal(value) for value in values)


def _row_triggers(table, identity, owner):
    changed = " OR ".join(f'OLD."{c}" IS DISTINCT FROM NEW."{c}"' for c in identity)
    return [
        f"CREATE TRIGGER assistant_evidence_version BEFORE UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION assistant_evidence_row_version()",
        f"CREATE TRIGGER assistant_evidence_touch_update AFTER UPDATE ON {table} FOR EACH ROW WHEN ({changed}) "
        f"EXECUTE FUNCTION assistant_evidence_touch({_args('u', owner, '+', *identity)})",
        f"CREATE TRIGGER assistant_evidence_touch_delete AFTER DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION assistant_evidence_touch({_args('u', owner, '+', *identity)})",
        f"CREATE CONSTRAINT TRIGGER assistant_evidence_flush_update AFTER UPDATE ON {table} "
        f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN ({changed}) EXECUTE FUNCTION assistant_evidence_flush()",
        f"CREATE CONSTRAINT TRIGGER assistant_evidence_flush_delete AFTER DELETE ON {table} "
        f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION assistant_evidence_flush()",
    ]


def upgrade():
    for table in ROWS:
        op.add_column(table, sa.Column("evidence_version", sa.BigInteger(), nullable=True))
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite installs its own coverage (db.evidence_schema).
    for table, identity in ROWS.items():
        op.execute(f"ALTER TABLE {table} ALTER COLUMN evidence_version SET DEFAULT nextval('assistant_evidence_version_seq')")
        for name in ("assistant_evidence_touch", "assistant_evidence_flush"):
            op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
        for statement in _row_triggers(table, identity, OWNERS[table]):
            op.execute(statement)


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        for table in ROWS:
            for name in ROW_TRIGGERS:
                op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
            op.execute(f"ALTER TABLE {table} ALTER COLUMN evidence_version DROP DEFAULT")
            # The previous cold coverage: any non-volatile change bumps the owner's epoch.
            op.execute(f"CREATE TRIGGER assistant_evidence_touch AFTER INSERT OR UPDATE OR DELETE ON {table} "
                       f"FOR EACH ROW EXECUTE FUNCTION assistant_evidence_touch({_args('u', OWNERS[table], '-')})")
            op.execute(f"CREATE CONSTRAINT TRIGGER assistant_evidence_flush AFTER INSERT OR UPDATE OR DELETE ON {table} "
                       f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION assistant_evidence_flush()")
    for table in ROWS:
        op.drop_column(table, "evidence_version")
