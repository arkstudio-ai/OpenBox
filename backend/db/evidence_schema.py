"""Which committed changes make a cached assistant validation verdict stale.

``assistant.evidence_cache`` reuses a verdict only while every fact that its
validation read is unchanged. This registry is the single application-side
description of that coverage; the PostgreSQL migration installs the same
triggers (a test compares them), and SQLite installs them here.

* Cold tables bump one epoch per (owner, table) for any committed change
  (on PostgreSQL, except their listed volatile columns, which no validation
  reads; SQLite cannot filter columns without naming them, so any change).
* Hot tables (messages, parts, agent events, inbox items) are written by
  every run, so they carry a per-row ``evidence_version`` changed by every
  UPDATE instead. Deletes and identity changes (the only columns a
  non-loading join may read) also bump the owner's epoch. Inserts never do:
  a capture reads these rows only by primary key or as a whole bounded set
  (every row matching its literal filters), which a later check reads again.

Keys are ``u|<user>|<table>``, ``w|<workspace>|<table>`` and, when a row's
owner cannot be found, the global ``*||<table>``.
"""
from sqlalchemy import event, text

EPOCHS = "assistant_evidence_epochs"
SEQUENCE = "assistant_evidence_version_seq"

# table -> (scope kind, owner, volatile columns). An owner "parent.column:fk"
# is read from the parent row.
COLD = {
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
# table -> identity columns (the only ones a join may read without loading).
ROWS = {
    "agent_events": ("id", "session_id", "user_id", "kind", "message_id", "run_id", "generation"),
    "agent_inbox_items": ("id", "user_id", "session_id", "message_id"),
    "messages": ("id", "session_id", "user_id", "role", "created_at"),
    "parts": ("id", "message_id", "session_id", "user_id", "type", "created_at"),
}
# A bounded set must name one of these, so a later check reads a few rows.
ANCHORS = {
    "agent_events": ("id", "session_id", "message_id"),
    "agent_inbox_items": ("id", "session_id", "message_id"),
    "messages": ("id", "session_id"),
    "parts": ("id", "message_id", "session_id"),
}
COVERED = frozenset(COLD) | frozenset(ROWS) | {EPOCHS}


def owner_kind(table):
    return COLD[table][0] if table in COLD else "u"


def key(table, owner, kind="u"):
    return f"*||{table}" if owner is None else f"{kind}|{owner}|{table}"


# --------------------------------------------------------------------------
# SQLite: immediate triggers. One writer holds the whole database, so a bump
# inside the writing transaction cannot deadlock and is visible to that same
# transaction's own reads, exactly as its other changes are.
#
# SQLite refuses to drop or rename a column that a trigger names, so these
# triggers name only owner and identity columns. Every update of a cold row
# bumps (no volatile filter): more misses on single-user SQLite, never a
# stale verdict. PostgreSQL filters volatile columns without naming the rest.
# --------------------------------------------------------------------------

def _quote(name):
    return '"' + name.replace('"', '""') + '"'


def _owner_sql(spec, row, present):
    if ":" in spec:
        parent, fk = spec.split(":")
        parent_table, parent_column = parent.split(".")
        if not present(parent_table):
            return "NULL"  # The global key, never a trigger naming a missing table.
        return f"(SELECT {_quote(parent_column)} FROM {_quote(parent_table)} WHERE id = {row}.{_quote(fk)})"
    return f"{row}.{_quote(spec)}"


def _bump_sql(table_expr, kind, owner_sql):
    scope = (f"CASE WHEN ({owner_sql}) IS NULL THEN '*||' || {table_expr} "
             f"ELSE '{kind}|' || ({owner_sql}) || '|' || {table_expr} END")
    return f"INSERT OR REPLACE INTO {EPOCHS} (scope_key, version) VALUES ({scope}, random());"


def _changed(columns):
    return " OR ".join(f"NEW.{_quote(c)} IS NOT OLD.{_quote(c)}" for c in columns) or "0"


def sqlite_statements(columns_of):
    """DDL for every covered table present; ``columns_of(table)`` lists live columns."""
    statements = []
    def present(table):
        return bool(columns_of(table))
    for table, (kind, owner, _volatile) in sorted(COLD.items()):
        if not present(table):
            continue
        name, literal = f"assistant_evidence_{table}", f"'{table}'"
        new = _bump_sql(literal, kind, _owner_sql(owner, "NEW", present))
        old = _bump_sql(literal, kind, _owner_sql(owner, "OLD", present))
        statements += [
            f"CREATE TRIGGER {name}_insert AFTER INSERT ON {table} BEGIN {new} END",
            f"CREATE TRIGGER {name}_update AFTER UPDATE ON {table} BEGIN {old} {new} END",
            f"CREATE TRIGGER {name}_delete AFTER DELETE ON {table} BEGIN {old} END",
        ]
    for table, identity in sorted(ROWS.items()):
        if not present(table):
            continue
        name, literal = f"assistant_evidence_{table}", f"'{table}'"
        new, old = _bump_sql(literal, "u", "NEW.user_id"), _bump_sql(literal, "u", "OLD.user_id")
        statements += [
            f"CREATE TRIGGER {name}_version AFTER UPDATE ON {table} "
            f"WHEN NEW.evidence_version IS OLD.evidence_version "
            f"BEGIN UPDATE {table} SET evidence_version = random() WHERE id = NEW.id; END",
            f"CREATE TRIGGER {name}_identity AFTER UPDATE ON {table} WHEN {_changed(identity)} BEGIN {old} {new} END",
            f"CREATE TRIGGER {name}_delete AFTER DELETE ON {table} BEGIN {old} END",
        ]
    return statements


def install_sqlite(connection):
    """Idempotently (re)install SQLite coverage from the live table columns."""
    if connection.dialect.name != "sqlite":
        return
    def columns_of(table):
        return [row[1] for row in connection.execute(text(f"PRAGMA table_info({_quote(table)})")).fetchall()]
    for table in ROWS:
        existing = columns_of(table)
        if existing and "evidence_version" not in existing:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN evidence_version BIGINT"))
    if not columns_of(EPOCHS):
        connection.execute(text(f"CREATE TABLE {EPOCHS} (scope_key VARCHAR(200) PRIMARY KEY, version BIGINT NOT NULL)"))
    for (name,) in connection.execute(text(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'assistant_evidence_%'")).fetchall():
        connection.execute(text(f"DROP TRIGGER {_quote(name)}"))
    for statement in sqlite_statements(columns_of):
        connection.execute(text(statement))


def register(metadata):
    """Install SQLite coverage whenever a schema is created from metadata."""
    if not event.contains(metadata, "after_create", _after_create):
        event.listen(metadata, "after_create", _after_create)


def _after_create(metadata, connection, **_kw):
    install_sqlite(connection)
