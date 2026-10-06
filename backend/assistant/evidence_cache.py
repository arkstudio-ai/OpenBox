"""Verified closures: reuse a validation verdict while every fact it read is unchanged.

Hold checks, provider checkpoints and source reads re-validate the same
provenance graph again and again, although almost none of it changes between
two checks. This module keeps one in-process verdict per validation unit and
reuses it only after a single read proves that its whole closure is current.

Capture. After a miss, the original validation runs again from ids in a new
REPEATABLE READ READ ONLY snapshot whose first read is every epoch its owner
could depend on (db.evidence_schema). A recorder sees every statement and
every loaded row. It keeps the epochs of the covered tables actually read and
the version of each message/part row read, and it refuses the verdict
(nothing is cached) for an uncovered table, a messages/parts read not bounded
by a primary key or reading a non-identity column without loading its row,
an agent_events read of an unlisted or unknown kind, a loaded row of another
owner, a memory time window not bounded by its loaded rows, a clock read in
SQL, or any write.

Reuse. A later check reads those epochs and row versions in one statement in
the caller's own transaction. A committed change to any covered row of that
owner changes them, and so does an earlier write of the same transaction.
Reaching a recorded deadline (a continuation grant or a memory window) ends
the verdict too. Only successful verdicts are cached; a refusal is always
recomputed. Reuse is limited to a top-level validation: a nested one depends
on its caller's cycle path.

ASSISTANT_EVIDENCE_CACHE=verify also runs the original validation on every
reuse and fails loudly if they differ (the test suite); =off disables reuse.
"""
import asyncio
import contextvars
import os
import re
import weakref
from collections import Counter, OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import String, case, cast, event, func, inspect as sa_inspect, literal, null, select, union_all
from sqlalchemy.engine import Engine
from sqlalchemy.sql import operators, visitors
from sqlalchemy.sql.elements import BinaryExpression, BindParameter, BooleanClauseList, ColumnClause, Grouping, Tuple
from sqlalchemy.sql.selectable import Alias, FromGrouping, Join, Select, TableClause

from core.log import create_logger
from db import evidence_schema as coverage
from db.base import get_db_session
from db.models.evidence import AssistantEvidenceEpoch
from db.models.message import Message
from db.models.part import Part

log = create_logger("assistant.evidence_cache")

MAX_ENTRIES = 1024  # A W2-sized closure keeps a few hundred row versions.
CAPTURE_SLOTS = 2
CAPTURE_INLINE = False  # Tests capture before returning, so reuse is deterministic.
stats = Counter()
_cache = OrderedDict()
_inflight = {}
_slots = weakref.WeakKeyDictionary()
_deadlines = contextvars.ContextVar("assistant_evidence_deadlines", default=None)
_ABSENT = object()
_CLOCK = re.compile(r"\b(now\s*\(|current_timestamp|localtimestamp|current_date|clock_timestamp"
                    r"|statement_timestamp|transaction_timestamp)", re.I)
_TABLES = re.compile(r"\b(?:FROM|JOIN)\s+\"?([A-Za-z_][A-Za-z0-9_]*)\"?", re.I)
# Loaded rows whose own columns bound the verdict in time.
_DEADLINE_COLUMNS = {"user_memories": ("ttl", "valid_from", "valid_to")}
_WINDOW_COLUMNS = {"user_memories": frozenset({"ttl", "valid_from", "valid_to"})}


def mode():
    value = os.environ.get("ASSISTANT_EVIDENCE_CACHE", "on").strip().lower()
    return value if value in {"off", "on", "verify"} else "on"


def _now():
    return datetime.now(timezone.utc)


def clear():
    _cache.clear()
    stats.clear()


def note_deadline(moment, clock=None):
    """A capture's verdict ends when ``clock()`` reaches ``moment``.

    ``clock`` is the one the depending validation itself reads, so the verdict
    ends exactly when that validation would start to refuse.
    """
    sink = _deadlines.get()
    if sink is not None and moment is not None:
        sink.append((_aware(moment), clock or _now))


def _memory_clock():
    from memory import policy
    return policy.datetime.now(timezone.utc)


def _aware(moment):
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class Refused(Exception):
    pass


@dataclass(frozen=True)
class Closure:
    epochs: tuple       # ((scope key, version or None when absent), ...)
    rows: tuple         # ((table, id, evidence_version), ...)
    part_sets: tuple    # ((message id, ((part id, evidence_version), ...)), ...)
    deadlines: tuple    # ((moment, clock), ...)
    value: object


async def verified(db, unit, key, scope, slow, capture):
    """Return ``(value, hit)`` for one validation unit.

    ``slow()`` runs the original validation in the caller's session.
    ``capture(snapshot)`` runs exactly the same validation from ids in the
    given new read-only session. ``scope`` is ``(user_id, workspace_id)`` and
    ``key`` every other input that decides the verdict.
    """
    current = mode()
    from assistant.command_sources import _path
    # Inside a capture every nested unit runs in full, so the recorder sees
    # each read that the outer verdict depends on.
    if current == "off" or _path.get() or _deadlines.get() is not None or not _eligible(scope):
        return await slow(), False
    full_key = (unit, scope, key)
    closure = _cache.get(full_key)
    if closure is not None and _proven(db, full_key, closure):
        stale = None
    elif closure is not None:
        stale = await _stale(db, closure)
    if closure is not None:
        if stale is None:
            _cache.move_to_end(full_key)
            stats[f"{unit}.hit"] += 1
            if current == "verify":
                await _verify(unit, key, closure, slow)
            return closure.value, True
        _cache.pop(full_key, None)
        stats[f"{unit}.stale"] += 1
        log.info("Evidence closure stale unit=%s reason=%s", unit, stale)
    value = await slow()
    stats[f"{unit}.miss"] += 1
    await _schedule(full_key, scope, capture)
    return value, False


async def _verify(unit, key, closure, slow):
    from assistant.policy import AssistantError
    try:
        value = await slow()
    except AssistantError as error:
        raise AssertionError(f"evidence cache reused {unit} {key!r} but validation refused it: {error.code}") from error
    if value != closure.value:
        raise AssertionError(f"evidence cache reused {unit} {key!r} with a different value")


def _eligible(scope):
    return all(isinstance(item, str) and item and "|" not in item for item in scope)


def _label(item):
    # A key's table/kind only; never its owner id.
    return item.split("|", 2)[-1]


async def _stale(db, closure):
    """None while the closure is current, else a short reason naming no ids."""
    return (await _stale_many(db, {None: closure}))[None]


async def _stale_many(db, closures):
    """Prove many closures in one statement: ``{key: None or a reason}``."""
    reasons = {key: "deadline" for key, closure in closures.items()
               if any(clock() >= moment for moment, clock in closure.deadlines)}
    pending = {key: closure for key, closure in closures.items() if key not in reasons}
    if not pending:
        return reasons
    epoch, none = AssistantEvidenceEpoch, cast(null(), String)
    keys = sorted({item for closure in pending.values() for item, _ in closure.epochs})
    branches = [select(literal("e").label("kind"), epoch.scope_key.label("id"), epoch.version.label("version"),
                       none.label("parent")).where(epoch.scope_key.in_(keys))]
    for table, model in (("messages", Message), ("parts", Part)):
        ids = sorted({ident for closure in pending.values() for kind, ident, _ in closure.rows if kind == table})
        if ids:
            branches.append(select(literal(table), model.id, model.evidence_version, none).where(model.id.in_(ids)))
    message_ids = sorted({message_id for closure in pending.values() for message_id, _ in closure.part_sets})
    if message_ids:
        branches.append(select(literal("s"), Part.id, Part.evidence_version, Part.message_id)
                        .where(Part.message_id.in_(message_ids)))
    if db.get_bind().dialect.name == "postgresql":
        # This transaction's own covered writes bump their epochs only at commit.
        own = func.coalesce(func.current_setting("assistant.evidence_keys", True), "")
        branches.append(select(literal("g"), literal(""), case((own == "", 0), else_=1), none))
    found, part_sets, own_write = {}, {}, False
    for kind, ident, version, parent in (await db.execute(union_all(*branches))).all():
        if kind == "g":
            own_write = bool(version)
        elif kind == "s":
            part_sets.setdefault(parent, set()).add((ident, version))
        else:
            found[(kind, ident)] = version
    for key, closure in pending.items():
        reasons[key] = "own write" if own_write else _changed(closure, found, part_sets)
    return reasons


def _changed(closure, found, part_sets):
    for item, version in closure.epochs:
        if found.get(("e", item), _ABSENT) != (_ABSENT if version is None else version):
            return f"epoch {_label(item)}"
    if any(part_sets.get(message_id, set()) != set(parts) for message_id, parts in closure.part_sets):
        return "part set"
    for table, ident, version in closure.rows:
        if found.get((table, ident), _ABSENT) != version:
            return f"row {table}"
    return None


async def prove(db, units):
    """Prove every cached closure of ``units`` in one statement.

    ``units`` are ``(unit, scope, key)``. A proven verdict is then reused by
    ``verified`` without its own read until this transaction ends; a page of
    answers costs one read instead of one per answer.
    """
    if mode() == "off":
        return
    from assistant.command_sources import _path
    if _path.get() or _deadlines.get() is not None:
        return
    closures = {}
    for unit, scope, key in units:
        full_key = (unit, scope, key)
        closure = _cache.get(full_key)
        if closure is not None and _eligible(scope):
            closures[full_key] = closure
    if not closures:
        return
    proven = _proofs(db)
    for full_key, reason in (await _stale_many(db, closures)).items():
        if reason is None:
            proven[full_key] = closures[full_key]
        else:
            _cache.pop(full_key, None)
            stats[f"{full_key[0]}.stale"] += 1
            log.info("Evidence closure stale unit=%s reason=%s", full_key[0], reason)


def _proofs(db):
    """Proofs held for this session's current transaction, until it writes."""
    session = db.sync_session
    transaction = session.get_transaction()
    held = session.info.get("assistant_evidence_proofs")
    if held is None or held[0] is not transaction:
        held = session.info["assistant_evidence_proofs"] = (transaction, {})
        if not event.contains(session, "after_flush", _forget_proofs):
            event.listen(session, "after_flush", _forget_proofs)
            event.listen(session, "do_orm_execute", _forget_proofs_on_write)
    return held[1]


def _forget_proofs(session, *_args):
    session.info.pop("assistant_evidence_proofs", None)


def _forget_proofs_on_write(state):
    if not state.is_select:
        state.session.info.pop("assistant_evidence_proofs", None)


def _proven(db, full_key, closure):
    session = db.sync_session
    if session.get_transaction() is None or session.new or session.dirty or session.deleted:
        return False
    return _proofs(db).get(full_key) is closure


async def _schedule(full_key, scope, capture):
    if full_key in _inflight:
        return
    # A capture never inherits its caller's cycle path or shared walk.
    task = asyncio.get_running_loop().create_task(_capture(full_key, scope, capture),
                                                  context=contextvars.Context())
    _inflight[full_key] = task
    task.add_done_callback(lambda _done: _inflight.pop(full_key, None))
    if CAPTURE_INLINE:
        await task


def _capture_slots():
    loop = asyncio.get_running_loop()
    slots = _slots.get(loop)
    if slots is None:
        slots = _slots[loop] = asyncio.Semaphore(CAPTURE_SLOTS)
    return slots


async def _capture(full_key, scope, capture):
    from assistant.policy import AssistantError
    from assistant.transactions import begin_snapshot
    unit, (user_id, workspace_id) = full_key[0], scope
    deadlines = []
    _deadlines.set(deadlines)
    recorder = _Recorder(user_id, workspace_id)
    try:
        async with _capture_slots(), get_db_session() as snapshot:
            await begin_snapshot(snapshot)
            # The first read fixes the snapshot: every later read is from it.
            epochs = await _scope_epochs(snapshot, user_id, workspace_id)
            await recorder.attach(snapshot)
            try:
                value = await capture(snapshot)
            finally:
                recorder.detach()
            part_sets = await _part_sets(snapshot, recorder.part_sets)
    except AssistantError:
        stats[f"{unit}.refused"] += 1
        return
    except Exception:
        stats[f"{unit}.error"] += 1
        log.exception("Evidence capture failed unit=%s", unit)
        return
    if recorder.refusal:
        stats[f"{unit}.uncacheable"] += 1
        log.info("Evidence verdict not cached unit=%s reason=%s", unit, recorder.refusal)
        return
    stats[f"{unit}.statements"] += recorder.statements
    moments = (*deadlines, *recorder.deadlines)
    if any(clock() >= moment for moment, clock in moments):
        return
    closure = Closure(tuple(sorted((item, epochs.get(item)) for item in recorder.keys())),
                      tuple(sorted(recorder.rows_read())), part_sets, moments, value)
    _store(full_key, closure)
    stats[f"{unit}.captured"] += 1
    log.info("Evidence verdict captured unit=%s statements=%d epochs=%d rows=%d part_sets=%d deadlines=%d",
             unit, recorder.statements, len(closure.epochs), len(closure.rows), len(closure.part_sets),
             len(closure.deadlines))


async def _part_sets(db, message_ids):
    """Every part of each message read by message: any insert, move or delete shows."""
    if not message_ids:
        return ()
    found = {message_id: set() for message_id in message_ids}
    rows = await db.execute(select(Part.message_id, Part.id, Part.evidence_version)
                            .where(Part.message_id.in_(sorted(message_ids))))
    for message_id, ident, version in rows.all():
        found[message_id].add((ident, version))
    return tuple(sorted((message_id, tuple(sorted(parts, key=repr))) for message_id, parts in found.items()))


def _store(full_key, closure):
    _cache[full_key] = closure
    _cache.move_to_end(full_key)
    while len(_cache) > MAX_ENTRIES:
        _cache.popitem(last=False)


def _possible_keys(user_id, workspace_id):
    keys = []
    for table, (kind, _, _) in coverage.COLD.items():
        keys += [coverage.key(table, user_id if kind == "u" else workspace_id, kind), coverage.key(table, None)]
    for table in coverage.ROWS:
        keys += [coverage.key(table, user_id), coverage.key(table, None)]
    for kind in coverage.EVENT_KINDS:
        keys += [coverage.key(coverage.event_table(kind), user_id), coverage.key(coverage.event_table(kind), None)]
    return keys


async def _scope_epochs(db, user_id, workspace_id):
    epoch = AssistantEvidenceEpoch
    rows = await db.execute(select(epoch.scope_key, epoch.version)
                            .where(epoch.scope_key.in_(_possible_keys(user_id, workspace_id))))
    return dict(rows.all())


# --------------------------------------------------------------------------
# Capture recorder
# --------------------------------------------------------------------------

class _Recorder:
    def __init__(self, user_id, workspace_id):
        self.user_id, self.workspace_id = user_id, workspace_id
        self.tables, self.kinds, self.rows, self.deadlines = set(), set(), {}, []
        self.part_sets, self.statements = set(), 0
        self.refusal = None
        self._approved = frozenset()
        self._session = self._connection = None
        self._hooks = (("do_orm_execute", self._orm_execute), ("loaded_as_persistent", self._loaded))
        self._cursor_hook = self._cursor

    def refuse(self, reason):
        if self.refusal is None:
            self.refusal = reason

    async def attach(self, db):
        self._db, self._session = db, db.sync_session
        self._connection = (await db.connection()).sync_connection
        for name, hook in self._hooks:
            event.listen(self._session, name, hook)
        event.listen(self._connection, "before_cursor_execute", self._cursor_hook)
        _capturing[asyncio.current_task()] = self

    def detach(self):
        _capturing.pop(asyncio.current_task(), None)
        for name, hook in self._hooks:
            event.remove(self._session, name, hook)
        event.remove(self._connection, "before_cursor_execute", self._cursor_hook)

    def keys(self):
        keys = set()
        for table in self.tables:
            if table in coverage.COLD:
                kind = coverage.owner_kind(table)
                keys.add(coverage.key(table, self.user_id if kind == "u" else self.workspace_id, kind))
                keys.add(coverage.key(table, None))
            elif table in coverage.ROWS:
                keys |= {coverage.key(table, self.user_id), coverage.key(table, None)}
        for kind in self.kinds:
            keys |= {coverage.key(coverage.event_table(kind), self.user_id),
                     coverage.key(coverage.event_table(kind), None)}
        return keys

    def rows_read(self):
        return [(table, ident, version) for (table, ident), version in self.rows.items()]

    # Every cursor statement: tables, writes and SQL clock reads.
    def _cursor(self, conn, cursor, statement, parameters, context, executemany):
        approved, self._approved = self._approved, frozenset()
        self.statements += 1
        head = statement.lstrip().lstrip("(").lstrip()[:6].upper()
        if head not in {"SELECT", "WITH"}:
            self.refuse(f"statement {head or 'unknown'}")
            return
        if _CLOCK.search(statement):
            self.refuse("SQL clock read")
        for name in {match.lower() for match in _TABLES.findall(statement)}:
            if name not in coverage.COVERED:
                self.refuse(f"uncovered table {name}")
            elif (name in coverage.ROWS or name == coverage.EVENTS) and name not in approved:
                self.refuse(f"unchecked read of {name}: {_snippet(statement)}")
            elif name != coverage.EPOCHS:
                self.tables.add(name)

    # Every ORM statement: bounded reads of the hot tables.
    def _orm_execute(self, state):
        self._approved = frozenset()
        if state.is_insert or state.is_update or state.is_delete:
            self.refuse("write")
            return
        try:
            self._approved = frozenset(self._inspect(state.statement))
        except Refused as error:
            self.refuse(f"{error}: {_snippet(str(state.statement))}")

    # Every loaded row: owner, row version and time window.
    def _loaded(self, session, instance):
        state = sa_inspect(instance)
        table, values = state.mapper.local_table.name, state.dict
        if table in coverage.ROWS:
            ident = (table, values.get("id"))
            version = values.get("evidence_version")
            if self.rows.setdefault(ident, version) != version:
                self.refuse(f"{table} row changed during capture")
        owner = _owner_column(table)
        if owner is not None:
            expected = self.workspace_id if coverage.owner_kind(table) == "w" else self.user_id
            if values.get(owner) != expected:
                self.refuse(f"{table} row of another owner")
        for column in _DEADLINE_COLUMNS.get(table, ()):
            moment = values.get(column)
            if isinstance(moment, datetime) and _aware(moment) > _memory_clock():
                self.deadlines.append((_aware(moment), _memory_clock))

    def _inspect(self, statement):
        approved = set()
        if not isinstance(statement, Select):
            return approved
        loaded = _loaded_occurrences(statement)
        for select_ in [item for item in visitors.iterate(statement) if isinstance(item, Select)]:
            froms = select_.get_final_froms()
            joins = list(_joins(froms))
            filters = [select_.whereclause, *(join.onclause for join in joins),
                       *select_._order_by_clauses, *select_._group_by_clauses]
            conjuncts = list(_conjuncts([select_.whereclause, *(join.onclause for join in joins)]))
            for occurrence in _tables(froms):
                name, occurrence_key = _table_name(occurrence), _occurrence(occurrence)
                is_loaded = select_ is statement and occurrence_key in loaded
                if name in coverage.ROWS:
                    if _bound(occurrence_key, conjuncts, "id") is None:
                        # A whole message's parts: its complete part set is
                        # recorded, so an insert, move or delete shows too.
                        messages = (_bound_values(occurrence_key, conjuncts, "message_id")
                                    if name == "parts" else None)
                        if not messages:
                            raise Refused(f"{name} read without its primary key")
                        self.part_sets |= messages
                    identity = set(coverage.ROWS[name])
                    columns = _columns_of(occurrence_key, filters)
                    if not is_loaded:
                        columns |= _columns_of(occurrence_key, select_.selected_columns)
                    if columns - identity:
                        raise Refused(f"{name} read of {sorted(columns - identity)} without its row version")
                    approved.add(name)
                elif name == coverage.EVENTS:
                    kinds = _kinds(occurrence_key, conjuncts)
                    if not kinds or not kinds <= set(coverage.EVENT_KINDS):
                        raise Refused(f"agent_events read of {sorted(kinds) if kinds else 'any'} kind")
                    self.kinds |= kinds
                    approved.add(name)
                elif name in _WINDOW_COLUMNS and _columns_of(occurrence_key, filters) & _WINDOW_COLUMNS[name]:
                    # Only loaded rows report when their window ends.
                    if not is_loaded or _bound(occurrence_key, conjuncts, "id") is None:
                        raise Refused(f"{name} time window read without its loaded rows")
        return approved


_capturing = {}


def capture_session():
    """The capture's own session when called from inside a capture, else None."""
    try:
        recorder = _capturing.get(asyncio.current_task()) if _capturing else None
    except RuntimeError:
        return None
    return recorder._db if recorder is not None else None


@event.listens_for(Engine, "before_cursor_execute")
def _other_connection(conn, cursor, statement, parameters, context, executemany):
    """A validation that opens its own session would read outside the record."""
    if not _capturing:
        return
    try:
        recorder = _capturing.get(asyncio.current_task())
    except RuntimeError:
        return
    if recorder is not None and conn is not recorder._connection:
        recorder.refuse("statement outside the capture session")


def _snippet(statement):
    # Statement text only: values are bound parameters, never part of it.
    return " ".join(statement.split())[:240]


def _owner_column(table):
    if table in coverage.ROWS or table == coverage.EVENTS:
        return "user_id"
    spec = coverage.COLD.get(table)
    if spec is None or ":" in spec[1]:
        return None
    return spec[1]


def _table_name(selectable):
    element = selectable.element if isinstance(selectable, Alias) else selectable
    return getattr(element, "name", None)


def _occurrence(selectable):
    if isinstance(selectable, Alias):
        return (_table_name(selectable), str(selectable.name))
    return (getattr(selectable, "name", None), None)


def _tables(froms):
    for item in froms:
        if isinstance(item, FromGrouping):
            yield from _tables((item.element,))
        elif isinstance(item, Join):
            yield from _tables((item.left, item.right))
        elif isinstance(item, TableClause) or (isinstance(item, Alias) and isinstance(item.element, TableClause)):
            yield item
        # A subquery is inspected as its own Select.


def _joins(froms):
    for item in froms:
        if isinstance(item, FromGrouping):
            yield from _joins((item.element,))
        elif isinstance(item, Join):
            yield item
            yield from _joins((item.left, item.right))


def _conjuncts(clauses):
    for clause in clauses:
        if clause is None:
            continue
        if isinstance(clause, Grouping):
            yield from _conjuncts([clause.element])
        elif isinstance(clause, BooleanClauseList) and clause.operator is operators.and_:
            yield from _conjuncts(clause.clauses)
        else:
            yield clause


def _column_of(element, occurrence_key, name=None):
    return (isinstance(element, ColumnClause) and element.table is not None
            and _occurrence(element.table) == occurrence_key and (name is None or element.name == name))


def _bound(occurrence_key, conjuncts, column):
    for clause in conjuncts:
        if not isinstance(clause, BinaryExpression):
            continue
        if clause.operator is operators.eq and (_column_of(clause.left, occurrence_key, column)
                                                or _column_of(clause.right, occurrence_key, column)):
            return clause
        if clause.operator is operators.in_op:
            elements = clause.left.clauses if isinstance(clause.left, Tuple) else (clause.left,)
            if any(_column_of(element, occurrence_key, column) for element in elements):
                return clause
    return None


def _bound_values(occurrence_key, conjuncts, column):
    """String values a column is bound to by ``=`` or ``IN``, when all are known."""
    for clause in conjuncts:
        if not isinstance(clause, BinaryExpression):
            continue
        if clause.operator is operators.eq:
            column_side, value_side = ((clause.left, clause.right) if _column_of(clause.left, occurrence_key, column)
                                       else (clause.right, clause.left))
        elif clause.operator is operators.in_op:
            column_side, value_side = clause.left, clause.right
        else:
            continue
        if not _column_of(column_side, occurrence_key, column):
            continue
        value_side = value_side.element if isinstance(value_side, Grouping) else value_side
        if not isinstance(value_side, BindParameter):
            continue
        value = value_side.effective_value
        values = [value] if clause.operator is operators.eq else list(value or ())
        if values and all(isinstance(item, str) for item in values):
            return set(values)
    return None


def _kinds(occurrence_key, conjuncts):
    for clause in conjuncts:
        if not isinstance(clause, BinaryExpression) or not _column_of(clause.left, occurrence_key, "kind"):
            continue
        right = clause.right.element if isinstance(clause.right, Grouping) else clause.right
        if not isinstance(right, BindParameter):
            continue
        value = right.effective_value
        if clause.operator is operators.eq and isinstance(value, str):
            return {value}
        if clause.operator is operators.in_op and right.expanding and value:
            return set(value)
    return None


def _columns_of(occurrence_key, clauses):
    names = set()
    for clause in clauses:
        if clause is None:
            continue
        for element in visitors.iterate(clause):
            if _column_of(element, occurrence_key):
                names.add(element.name)
    return names


def _loaded_occurrences(statement):
    loaded = set()
    for description in statement.column_descriptions:
        entity = description.get("entity")
        if entity is not None and description.get("expr") is entity:
            loaded.add(_occurrence(sa_inspect(entity).selectable))
    return loaded
