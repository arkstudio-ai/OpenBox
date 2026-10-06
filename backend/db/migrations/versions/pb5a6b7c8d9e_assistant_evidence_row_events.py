"""Track agent events and inbox items per row for assistant validation verdicts.

The previous revision bumped one epoch per (owner, event kind) and per
(owner, inbox table). Every provider call, committed answer and claimed input
therefore invalidated every cached verdict of that owner, although a verdict
reads only the few events and inputs of its own messages and runs.

Both tables now carry a per-row evidence_version (changed by every UPDATE),
like messages and parts. A capture records the rows it loads by key, or a
whole bounded set (every row matching its literal filters), and a later check
reads exactly those again. Deletes and identity changes still bump the
owner's epoch; inserts never do. Existing rows keep NULL versions.

Revision ID: pb5a6b7c8d9e
Revises: pb4f5a6b7c8d
"""
from alembic import op
import sqlalchemy as sa

revision = "pb5a6b7c8d9e"
down_revision = "pb4f5a6b7c8d"
branch_labels = None
depends_on = None

# Frozen copy of db.evidence_schema.ROWS for these two tables at this revision.
ROWS = {
    "agent_events": ("id", "session_id", "user_id", "kind", "message_id", "run_id", "generation"),
    "agent_inbox_items": ("id", "user_id", "session_id", "message_id"),
}
# The previous revision's coverage, restored by downgrade().
PREVIOUS_EVENT_KINDS = (
    "assistant.business.read", "assistant.budget.started", "assistant.compaction.committed",
    "assistant.compaction.consumed", "assistant.compaction.requested", "assistant.context.consumed",
    "assistant.continuation.sources_projected", "assistant.control.blocked", "assistant.control.resumed",
    "assistant.decision.proposed", "assistant.decision.recorded", "assistant.isolation.created",
    "assistant.message.committed", "assistant.queue.claimed", "assistant.report.sources_projected",
    "inbox.accepted", "model.requested",
)
PREVIOUS_EVENTS_FUNCTION = """
CREATE FUNCTION assistant_evidence_touch_events() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    keys text[] := ARRAY[]::text[];
BEGIN
    IF TG_OP <> 'DELETE' THEN
        IF NEW.kind = ANY(TG_ARGV) THEN
            keys := keys || assistant_evidence_key('u', NEW.user_id, 'agent_events|' || NEW.kind);
        END IF;
    END IF;
    IF TG_OP <> 'INSERT' THEN
        IF OLD.kind = ANY(TG_ARGV) THEN
            keys := keys || assistant_evidence_key('u', OLD.user_id, 'agent_events|' || OLD.kind);
        END IF;
    END IF;
    IF cardinality(keys) > 0 THEN
        PERFORM assistant_evidence_note(keys);
    END IF;
    RETURN NULL;
END
$$"""
ROW_TRIGGERS = ("assistant_evidence_version", "assistant_evidence_touch_update", "assistant_evidence_touch_delete",
                "assistant_evidence_flush_update", "assistant_evidence_flush_delete")
EVENT_TRIGGERS = tuple(f"assistant_evidence_{kind}_{op_name}" for kind in ("touch", "flush")
                       for op_name in ("insert", "update", "delete"))


def _literal(value):
    return "'" + value.replace("'", "''") + "'"


def _args(*values):
    return ", ".join(_literal(value) for value in values)


def _row_triggers(table, identity):
    changed = " OR ".join(f'OLD."{c}" IS DISTINCT FROM NEW."{c}"' for c in identity)
    return [
        f"CREATE TRIGGER assistant_evidence_version BEFORE UPDATE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION assistant_evidence_row_version()",
        f"CREATE TRIGGER assistant_evidence_touch_update AFTER UPDATE ON {table} FOR EACH ROW WHEN ({changed}) "
        f"EXECUTE FUNCTION assistant_evidence_touch({_args('u', 'user_id', '+', *identity)})",
        f"CREATE TRIGGER assistant_evidence_touch_delete AFTER DELETE ON {table} "
        f"FOR EACH ROW EXECUTE FUNCTION assistant_evidence_touch({_args('u', 'user_id', '+', *identity)})",
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
    for table in ROWS:
        op.execute(f"ALTER TABLE {table} ALTER COLUMN evidence_version SET DEFAULT nextval('assistant_evidence_version_seq')")
    for name in EVENT_TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON agent_events")
    for name in ("assistant_evidence_touch", "assistant_evidence_flush"):
        op.execute(f"DROP TRIGGER IF EXISTS {name} ON agent_inbox_items")
    op.execute("DROP FUNCTION IF EXISTS assistant_evidence_touch_events()")
    for table, identity in ROWS.items():
        for statement in _row_triggers(table, identity):
            op.execute(statement)


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        for table in ROWS:
            for name in ROW_TRIGGERS:
                op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
            op.execute(f"ALTER TABLE {table} ALTER COLUMN evidence_version DROP DEFAULT")
        op.execute(PREVIOUS_EVENTS_FUNCTION)
        kinds = ", ".join(_literal(kind) for kind in PREVIOUS_EVENT_KINDS)
        for op_name, condition in (("insert", f"NEW.kind IN ({kinds})"),
                                   ("update", f"OLD.kind IN ({kinds}) OR NEW.kind IN ({kinds})"),
                                   ("delete", f"OLD.kind IN ({kinds})")):
            op.execute(f"CREATE TRIGGER assistant_evidence_touch_{op_name} AFTER {op_name.upper()} ON agent_events "
                       f"FOR EACH ROW WHEN ({condition}) "
                       f"EXECUTE FUNCTION assistant_evidence_touch_events({_args(*PREVIOUS_EVENT_KINDS)})")
            op.execute(f"CREATE CONSTRAINT TRIGGER assistant_evidence_flush_{op_name} AFTER {op_name.upper()} "
                       f"ON agent_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN ({condition}) "
                       f"EXECUTE FUNCTION assistant_evidence_flush()")
        op.execute("CREATE TRIGGER assistant_evidence_touch AFTER INSERT OR UPDATE OR DELETE ON agent_inbox_items "
                   f"FOR EACH ROW EXECUTE FUNCTION assistant_evidence_touch({_args('u', 'user_id', '-')})")
        op.execute("CREATE CONSTRAINT TRIGGER assistant_evidence_flush AFTER INSERT OR UPDATE OR DELETE "
                   "ON agent_inbox_items DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
                   "EXECUTE FUNCTION assistant_evidence_flush()")
    for table in ROWS:
        op.drop_column(table, "evidence_version")
