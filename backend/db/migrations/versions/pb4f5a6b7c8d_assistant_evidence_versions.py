"""Version the facts behind assistant validation verdicts.

A cached verdict is reused only while every fact its validation read is
unchanged (assistant.evidence_cache). Cold tables bump one epoch per (owner,
table) at commit; messages/parts get a per-row version changed by every
UPDATE; agent_events bump one epoch per (owner, kind) for listed kinds.

Bumps are collected per transaction and applied by one deferred trigger in
sorted key order at commit, so the shared epoch rows are never locked while a
transaction still waits for other rows (no new lock-order deadlocks). The
writing transaction's own uncommitted changes are visible to it through the
transaction-local setting ``assistant.evidence_keys``.

Existing rows keep NULL versions: a never-updated row's version is NULL until
its first UPDATE, so no table rewrite or backfill is needed.

Revision ID: pb4f5a6b7c8d
Revises: pb3e4f5a6b7c
"""
from alembic import op
import sqlalchemy as sa

revision = "pb4f5a6b7c8d"
down_revision = "pb3e4f5a6b7c"
branch_labels = None
depends_on = None

# Frozen copy of db.evidence_schema at this revision; a test compares the
# installed triggers with the current registry.
COLD = {
    "agent_inbox_items": ("u", "user_id", ()),
    "assistant_commands": ("u", "actor_user_id", ()),
    "assistant_task_results": ("u", "assistant_tasks.user_id:task_id", ()),
    "assistant_task_submissions": ("u", "assistant_tasks.user_id:task_id", ()),
    "assistant_tasks": ("u", "user_id", ()),
    "cron_jobs": ("u", "user_id", ()),
    "cron_runs": ("u", "user_id", ()),
    "file_assets": ("u", "user_id", ()),
    "memory_document_revisions": ("u", "memory_documents.user_id:document_id", ()),
    "memory_documents": ("u", "user_id", ()),
    "memory_revisions": ("u", "user_id", ()),
    "memory_source_links": ("u", "user_memories.user_id:memory_id", ()),
    "memory_sources": ("u", "user_id", ()),
    "memory_tombstones": ("u", "user_id", ()),
    "memory_wiki_pages": ("u", "user_id", ()),
    "projects": ("u", "user_id", ()),
    "question_checkpoints": ("u", "user_id", ()),
    "session_executions": ("u", "user_id", ("updated_at", "lease_until", "trace_context", "run_progress")),
    "sessions": ("u", "user_id", ("updated_at", "status", "token_usage", "tool_exposure_state",
                                  "additions", "deletions", "files_changed")),
    "user_memories": ("u", "user_id", ("hit_count", "last_hit_at")),
    "users": ("u", "id", ("updated_at", "failed_login_count", "locked_until", "default_workspace_id")),
    "workspace_members": ("u", "user_id", ()),
    "workspaces": ("w", "id", ()),
}
ROWS = {
    "messages": ("id", "session_id", "user_id", "role", "created_at"),
    "parts": ("id", "message_id", "session_id", "user_id", "type", "created_at"),
}
EVENT_KINDS = (
    "assistant.business.read", "assistant.budget.started", "assistant.compaction.committed",
    "assistant.compaction.consumed", "assistant.compaction.requested", "assistant.context.consumed",
    "assistant.continuation.sources_projected", "assistant.control.blocked", "assistant.control.resumed",
    "assistant.decision.proposed", "assistant.decision.recorded", "assistant.isolation.created",
    "assistant.message.committed", "assistant.queue.claimed", "assistant.report.sources_projected",
    "inbox.accepted", "model.requested",
)

FUNCTIONS = (
    """
CREATE FUNCTION assistant_evidence_note(keys text[]) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    pending text := coalesce(current_setting('assistant.evidence_keys', true), '');
    item text;
BEGIN
    FOREACH item IN ARRAY keys LOOP
        IF item IS NOT NULL AND strpos(pending, chr(10) || item || chr(10)) = 0 THEN
            pending := pending || chr(10) || item || chr(10);
        END IF;
    END LOOP;
    PERFORM set_config('assistant.evidence_keys', pending, true);
END
$$""",
    """
CREATE FUNCTION assistant_evidence_key(scope text, owner text, tbl text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN owner IS NULL THEN '*||' || tbl ELSE scope || '|' || owner || '|' || tbl END
$$""",
    """
CREATE FUNCTION assistant_evidence_owner(spec text, row_data jsonb) RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE
    owner text;
BEGIN
    IF strpos(spec, ':') = 0 THEN
        RETURN row_data ->> spec;
    END IF;
    EXECUTE format('SELECT %I::text FROM %I WHERE id = $1',
                   split_part(split_part(spec, ':', 1), '.', 2), split_part(split_part(spec, ':', 1), '.', 1))
        INTO owner USING row_data ->> split_part(spec, ':', 2);
    RETURN owner;
END
$$""",
    # TG_ARGV: scope kind, owner, '-' (ignore these columns) or '+' (only these).
    """
CREATE FUNCTION assistant_evidence_touch() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    columns text[] := TG_ARGV[3:TG_NARGS - 1];
    new_row jsonb;
    old_row jsonb;
    keys text[] := ARRAY[]::text[];
BEGIN
    IF TG_OP <> 'DELETE' THEN
        new_row := to_jsonb(NEW);
    END IF;
    IF TG_OP <> 'INSERT' THEN
        old_row := to_jsonb(OLD);
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF TG_ARGV[2] = '+' THEN
            IF NOT EXISTS (SELECT 1 FROM unnest(columns) AS c WHERE (new_row -> c) IS DISTINCT FROM (old_row -> c)) THEN
                RETURN NULL;
            END IF;
        ELSIF (new_row - columns) = (old_row - columns) THEN
            RETURN NULL;
        END IF;
    END IF;
    IF new_row IS NOT NULL THEN
        keys := keys || assistant_evidence_key(TG_ARGV[0], assistant_evidence_owner(TG_ARGV[1], new_row), TG_TABLE_NAME);
    END IF;
    IF old_row IS NOT NULL THEN
        keys := keys || assistant_evidence_key(TG_ARGV[0], assistant_evidence_owner(TG_ARGV[1], old_row), TG_TABLE_NAME);
    END IF;
    PERFORM assistant_evidence_note(keys);
    RETURN NULL;
END
$$""",
    # TG_ARGV: the event kinds that validation may read.
    """
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
$$""",
    """
CREATE FUNCTION assistant_evidence_row_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.evidence_version := nextval('assistant_evidence_version_seq');
    RETURN NEW;
END
$$""",
    # One sorted pass at commit: concurrent writers lock epoch rows in one
    # order and only after every other lock of their transaction is held.
    """
CREATE FUNCTION assistant_evidence_flush() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    pending text := coalesce(current_setting('assistant.evidence_keys', true), '');
    item text;
BEGIN
    IF pending = '' THEN
        RETURN NULL;
    END IF;
    PERFORM set_config('assistant.evidence_keys', '', true);
    FOR item IN SELECT DISTINCT k FROM unnest(string_to_array(pending, chr(10))) AS k WHERE k <> '' ORDER BY k LOOP
        INSERT INTO assistant_evidence_epochs (scope_key, version)
        VALUES (item, nextval('assistant_evidence_version_seq'))
        ON CONFLICT (scope_key) DO UPDATE SET version = EXCLUDED.version;
    END LOOP;
    RETURN NULL;
END
$$""",
)
FUNCTION_NAMES = ("assistant_evidence_flush()", "assistant_evidence_row_version()", "assistant_evidence_touch_events()",
                  "assistant_evidence_touch()", "assistant_evidence_owner(text, jsonb)",
                  "assistant_evidence_key(text, text, text)", "assistant_evidence_note(text[])")


def _literal(value):
    return "'" + value.replace("'", "''") + "'"


def _args(*values):
    return ", ".join(_literal(value) for value in values)


def _distinct(columns):
    return " OR ".join(f'OLD."{c}" IS DISTINCT FROM NEW."{c}"' for c in columns)


def trigger_statements():
    statements = []
    for table, (kind, owner, volatile) in sorted(COLD.items()):
        statements += [
            f"CREATE TRIGGER assistant_evidence_touch AFTER INSERT OR UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION assistant_evidence_touch({_args(kind, owner, '-', *volatile)})",
            f"CREATE CONSTRAINT TRIGGER assistant_evidence_flush AFTER INSERT OR UPDATE OR DELETE ON {table} "
            f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION assistant_evidence_flush()",
        ]
    for table, identity in sorted(ROWS.items()):
        changed = _distinct(identity)
        statements += [
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
    kinds = ", ".join(_literal(kind) for kind in EVENT_KINDS)
    for op_name, condition in (("insert", f"NEW.kind IN ({kinds})"),
                               ("update", f"OLD.kind IN ({kinds}) OR NEW.kind IN ({kinds})"),
                               ("delete", f"OLD.kind IN ({kinds})")):
        statements += [
            f"CREATE TRIGGER assistant_evidence_touch_{op_name} AFTER {op_name.upper()} ON agent_events "
            f"FOR EACH ROW WHEN ({condition}) EXECUTE FUNCTION assistant_evidence_touch_events({_args(*EVENT_KINDS)})",
            f"CREATE CONSTRAINT TRIGGER assistant_evidence_flush_{op_name} AFTER {op_name.upper()} ON agent_events "
            f"DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN ({condition}) EXECUTE FUNCTION assistant_evidence_flush()",
        ]
    return statements


def _trigger_names():
    names = [(table, name) for table in COLD for name in ("assistant_evidence_touch", "assistant_evidence_flush")]
    names += [(table, name) for table in ROWS for name in (
        "assistant_evidence_version", "assistant_evidence_touch_update", "assistant_evidence_touch_delete",
        "assistant_evidence_flush_update", "assistant_evidence_flush_delete")]
    names += [("agent_events", f"assistant_evidence_{kind}_{op_name}") for kind in ("touch", "flush")
              for op_name in ("insert", "update", "delete")]
    return names


def upgrade():
    op.create_table(
        "assistant_evidence_epochs",
        sa.Column("scope_key", sa.String(200), primary_key=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
    )
    for table in ROWS:
        # Nullable without a default is a catalog-only change on PostgreSQL.
        op.add_column(table, sa.Column("evidence_version", sa.BigInteger(), nullable=True))
    if op.get_bind().dialect.name != "postgresql":
        return  # SQLite installs its own coverage (db.evidence_schema).
    op.execute("CREATE SEQUENCE assistant_evidence_version_seq")
    for table in ROWS:
        # Affects future inserts only; existing rows keep NULL.
        op.execute(f"ALTER TABLE {table} ALTER COLUMN evidence_version SET DEFAULT nextval('assistant_evidence_version_seq')")
    for statement in FUNCTIONS:
        op.execute(statement)
    for statement in trigger_statements():
        op.execute(statement)


def downgrade():
    if op.get_bind().dialect.name == "postgresql":
        for table, name in _trigger_names():
            op.execute(f"DROP TRIGGER IF EXISTS {name} ON {table}")
        for name in FUNCTION_NAMES:
            op.execute(f"DROP FUNCTION IF EXISTS {name}")
        for table in ROWS:
            op.execute(f"ALTER TABLE {table} ALTER COLUMN evidence_version DROP DEFAULT")
    for table in ROWS:
        op.drop_column(table, "evidence_version")
    op.drop_table("assistant_evidence_epochs")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP SEQUENCE IF EXISTS assistant_evidence_version_seq")
