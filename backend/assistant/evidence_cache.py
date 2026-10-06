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
(nothing is cached) for an uncovered table, a hot-table read (messages,
parts, agent events, inbox items) bounded neither by a primary key nor by
literal filters that name a small set, a non-loading join reading a
non-identity column, a loaded row of another owner, a memory time window not
bounded by its loaded rows, a clock read in SQL, or any write. A bounded set
is kept whole (every row matching those literals, with its version), so an
inserted, moved or deleted row shows as well as an updated one.

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
import time
import weakref
from collections import Counter, OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from itertools import product
from datetime import datetime, timezone

from sqlalchemy import String, case, cast, event, func, inspect as sa_inspect, literal, null, select, tuple_, union_all
from sqlalchemy.engine import Engine
from sqlalchemy.sql import operators, visitors
from sqlalchemy.sql.elements import BinaryExpression, BindParameter, BooleanClauseList, ColumnClause, Grouping, Tuple
from sqlalchemy.sql.selectable import Alias, FromGrouping, Join, Select, TableClause

from core.log import create_logger
from db import evidence_schema as coverage
from db.base import Base, get_db_session
from db.models.evidence import AssistantEvidenceEpoch
from db.models.message import Message
from db.models.part import Part

log = create_logger("assistant.evidence_cache")

MAX_ENTRIES = 1024  # A W2-sized closure keeps a few hundred row versions.
# A capture costs about one full validation. One at a time, after a short
# delay, it mostly runs while the requester waits on its provider, not on SQL.
CAPTURE_SLOTS = 1
CAPTURE_DELAY = 1.0
CAPTURE_INLINE = False  # Tests capture before returning, so reuse is deterministic.
CHURN_SECONDS = 30.0  # A closure stale this soon after capture backs off.
stats = Counter()
_cache = OrderedDict()
_inflight = {}
_churn = {}  # key -> (consecutive early stales, monotonic time before which no recapture)
_slots = weakref.WeakKeyDictionary()
_deadlines = contextvars.ContextVar("assistant_evidence_deadlines", default=None)
_ABSENT = object()
_OWN_WRITE = "own write"
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
    _churn.clear()
    stats.clear()


_heights = contextvars.ContextVar("assistant_evidence_heights", default=None)
PROVEN = object()  # A source ref proven by a reused verdict; its row loads on demand.


def note_depth(depth):
    """The deepest graph position a measured validation reached."""
    sink = _heights.get()
    if sink is not None and depth > sink[0]:
        sink[0] = depth


@contextmanager
def measuring_height():
    sink = [0]
    token = _heights.set(sink)
    try:
        yield sink
    finally:
        _heights.reset(token)


async def reuse(db, unit, key, scope, capture):
    """A cached verdict proven current now, else None; the caller validates in place.

    Used where a validation shares its caller's budgets and path (a nested
    answer): the caller applies the verdict's value only when that is exact.
    """
    current = mode()
    from assistant.command_sources import _path
    if current == "off" or _path.get() or not _eligible(scope):
        return None
    full_key = (unit, scope, key)
    closure = _cache.get(full_key)
    if _deadlines.get() is not None:
        recorder = _capturing.get(asyncio.current_task())
        if (closure is None or recorder is None or recorder.refusal is not None
                or scope != (recorder.user_id, recorder.workspace_id) or db.sync_session is not recorder._session):
            return None
        with recorder.paused():
            stale = await _stale(db, closure)
        if stale is not None:
            return None
        recorder.merge(closure)
        stats[f"{unit}.composed"] += 1
        return closure.value
    if closure is None:
        stats[f"{unit}.miss"] += 1
        await _schedule(full_key, scope, capture)
        return None
    stale = None if _proven(db, full_key, closure) else await _stale(db, closure)
    if stale is None:
        _cache.move_to_end(full_key)
        stats[f"{unit}.hit"] += 1
        return closure.value
    if stale != _OWN_WRITE:
        _cache.pop(full_key, None)
        _note_stale(full_key, closure)
        stats[f"{unit}.stale"] += 1
        log.info("Evidence closure stale unit=%s reason=%s", unit, stale)
        await _schedule(full_key, scope, capture)
    return None


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
    sets: tuple         # ((table, columns, values, ((id, evidence_version), ...)), ...)
    derived: tuple      # ((table, via, via_column, columns, values, ((id, evidence_version), ...)), ...)
    deadlines: tuple    # ((moment, clock), ...)
    value: object
    captured_at: float = 0.0


async def verified(db, unit, key, scope, slow, capture):
    """Return ``(value, hit)`` for one validation unit.

    ``slow()`` runs the original validation in the caller's session.
    ``capture(snapshot)`` runs exactly the same validation from ids in the
    given new read-only session. ``scope`` is ``(user_id, workspace_id)`` and
    ``key`` every other input that decides the verdict.
    """
    current = mode()
    from assistant.command_sources import _path
    if current == "off" or _path.get() or not _eligible(scope):
        return await slow(), False
    full_key = (unit, scope, key)
    if _deadlines.get() is not None:
        # Inside a capture a nested unit either runs in full under the
        # recorder, or its own still-current closure becomes part of this one.
        recorder = _capturing.get(asyncio.current_task())
        closure = _cache.get(full_key)
        if (recorder is not None and closure is not None and recorder.refusal is None
                and scope == (recorder.user_id, recorder.workspace_id) and db.sync_session is recorder._session):
            with recorder.paused():
                stale = await _stale(db, closure)
            if stale is None:
                recorder.merge(closure)
                stats[f"{unit}.composed"] += 1
                if current == "verify":
                    await _verify(unit, key, closure, slow)
                return closure.value, True
        return await slow(), False
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
        if stale == _OWN_WRITE:
            # Only this uncommitted transaction differs: validate in full here
            # and keep the closure, still current for every other reader.
            stats[f"{unit}.own_write"] += 1
            return await slow(), False
        _cache.pop(full_key, None)
        _note_stale(full_key, closure)
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
    for table in coverage.ROWS:
        ids = sorted({ident for closure in pending.values() for kind, ident, _ in closure.rows if kind == table})
        if ids:
            model = Base.metadata.tables[table]
            branches.append(select(literal(table), model.c.id, model.c.evidence_version, none)
                            .where(model.c.id.in_(ids)))
    signatures = {}
    for closure in pending.values():
        for table, columns, values, _ in closure.sets:
            signatures.setdefault((table, columns), set()).add(values)
    for (table, columns), values in sorted(signatures.items()):
        branches.append(_set_select(table, columns, values))
    reached = {}
    for closure in pending.values():
        for table, via, via_column, columns, values, _ in closure.derived:
            reached.setdefault((table, via, via_column, columns), set()).add(values)
    for signature, values in sorted(reached.items()):
        branches.append(_derived_select(*signature, values))
    if db.get_bind().dialect.name == "postgresql":
        # This transaction's own covered writes bump their epochs only at commit.
        own = func.coalesce(func.current_setting("assistant.evidence_keys", True), "")
        branches.append(select(literal("g"), literal(""), case((own == "", 0), else_=1), none))
    found, members, own_write = {}, {}, False
    for kind, ident, version, parent in (await db.execute(union_all(*branches))).all():
        if kind == "g":
            own_write = bool(version)
        elif kind.startswith(("s|", "d|")):
            members.setdefault((kind, parent), set()).add((ident, version))
        else:
            found[(kind, ident)] = version
    for key, closure in pending.items():
        reasons[key] = _OWN_WRITE if own_write else _changed(closure, found, members)
    return reasons


_SEPARATOR = "\x1f"


def _set_label(table, columns):
    return "s|" + table + "|" + ",".join(columns)


def _set_parent(values):
    return _SEPARATOR.join(str(value) for value in values)


def _set_select(table, columns, values):
    """Every row matching one signature's literal values, with its version."""
    model = Base.metadata.tables[table]
    bound = [model.c[column] for column in columns]
    parent = (cast(bound[0], String) if len(bound) == 1
              else func.concat_ws(_SEPARATOR, *(cast(column, String) for column in bound)))
    match = (bound[0].in_(sorted(value[0] for value in values)) if len(bound) == 1
             else tuple_(*bound).in_(sorted(values, key=repr)))
    return select(literal(_set_label(table, columns)), model.c.id, model.c.evidence_version, parent).where(match)


def _derived_label(table, via, via_column, columns):
    return "d|" + table + "|" + via + "." + via_column + "|" + ",".join(columns)


def _derived_select(table, via, via_column, columns, values):
    """Every row a literally bounded cold partner reaches by key, with its version."""
    model, other = Base.metadata.tables[table], Base.metadata.tables[via]
    bound = [other.c[column] for column in columns]
    parent = (cast(bound[0], String) if len(bound) == 1
              else func.concat_ws(_SEPARATOR, *(cast(column, String) for column in bound)))
    match = (bound[0].in_(sorted(value[0] for value in values)) if len(bound) == 1
             else tuple_(*bound).in_(sorted(values, key=repr)))
    return (select(literal(_derived_label(table, via, via_column, columns)), model.c.id, model.c.evidence_version,
                   parent).select_from(model.join(other, model.c.id == other.c[via_column])).where(match))


def _changed(closure, found, members):
    for item, version in closure.epochs:
        if found.get(("e", item), _ABSENT) != (_ABSENT if version is None else version):
            return f"epoch {_label(item)}"
    for table, columns, values, contents in closure.sets:
        if members.get((_set_label(table, columns), _set_parent(values)), set()) != set(contents):
            return f"set {table}"
    for table, via, via_column, columns, values, contents in closure.derived:
        label = _derived_label(table, via, via_column, columns)
        if members.get((label, _set_parent(values)), set()) != set(contents):
            return f"set {table} via {via}"
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
        elif reason != _OWN_WRITE:
            _cache.pop(full_key, None)
            _note_stale(full_key, closures[full_key])
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


def _note_stale(full_key, closure):
    """A verdict that changes again right after capture is not worth recapturing at once."""
    if time.monotonic() - closure.captured_at < CHURN_SECONDS:
        count = _churn.get(full_key, (0, 0.0))[0] + 1
        _churn[full_key] = (count, time.monotonic() + min(300.0, CHURN_SECONDS * 2 ** (count - 1)))
    else:
        _churn.pop(full_key, None)


async def _schedule(full_key, scope, capture):
    if full_key in _inflight:
        return
    if not CAPTURE_INLINE and time.monotonic() < _churn.get(full_key, (0, 0.0))[1]:
        stats[f"{full_key[0]}.backoff"] += 1
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
    if not CAPTURE_INLINE:
        await asyncio.sleep(CAPTURE_DELAY)
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
            sets = await _set_contents(snapshot, recorder.sets)
            derived = await _derived_contents(snapshot, recorder.derived)
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
    if sets is None or derived is None:
        stats[f"{unit}.uncacheable"] += 1
        log.info("Evidence verdict not cached unit=%s reason=bounded set too large", unit)
        return
    closure = Closure(tuple(sorted((item, epochs.get(item)) for item in recorder.keys())),
                      tuple(sorted(recorder.rows_read())), sets, derived, moments, value, time.monotonic())
    _store(full_key, closure)
    stats[f"{unit}.captured"] += 1
    log.info("Evidence verdict captured unit=%s statements=%d epochs=%d rows=%d sets=%d deadlines=%d",
             unit, recorder.statements, len(closure.epochs), len(closure.rows), len(closure.sets),
             len(closure.deadlines))


MAX_SET_ROWS = 1000


async def _set_contents(db, sets):
    """Each bounded set whole, in the capture's snapshot; None if one is too large."""
    contents = []
    for (table, columns), values in sorted(sets.items()):
        found = {_set_parent(value): set() for value in values}
        for _label_value, ident, version, parent in (await db.execute(_set_select(table, columns, values))).all():
            found.setdefault(parent, set()).add((ident, version))
        if any(len(rows) > MAX_SET_ROWS for rows in found.values()):
            return None
        contents += [(table, columns, value, tuple(sorted(found[_set_parent(value)], key=repr)))
                     for value in sorted(values, key=repr)]
    return tuple(contents)


async def _derived_contents(db, derived):
    """Each derived set whole, in the capture's snapshot; None if one is too large."""
    contents = []
    for signature, values in sorted(derived.items()):
        found = {_set_parent(value): set() for value in values}
        for _label_value, ident, version, parent in (await db.execute(_derived_select(*signature, values))).all():
            found.setdefault(parent, set()).add((ident, version))
        if any(len(rows) > MAX_SET_ROWS for rows in found.values()):
            return None
        contents += [(*signature, value, tuple(sorted(found[_set_parent(value)], key=repr)))
                     for value in sorted(values, key=repr)]
    return tuple(contents)


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
        self.tables, self.rows, self.deadlines, self.sets = set(), {}, [], {}
        self.statements, self.row_epochs, self.derived = 0, set(), {}
        self.merged, self._paused = set(), False
        self.refusal = None
        self._approved = frozenset()
        self._session = self._connection = None
        self._hooks = (("do_orm_execute", self._orm_execute), ("loaded_as_persistent", self._loaded))
        self._cursor_hook = self._cursor

    def refuse(self, reason):
        if self.refusal is None:
            self.refusal = reason

    @contextmanager
    def paused(self):
        """Proving a nested closure is not a read of the validation itself."""
        self._paused = True
        try:
            yield
        finally:
            self._paused = False

    def merge(self, closure):
        """A nested verdict proven current in this snapshot joins this closure."""
        self.merged |= {item for item, _ in closure.epochs}
        for table, ident, version in closure.rows:
            if self.rows.setdefault((table, ident), version) != version:
                self.refuse(f"{table} row changed during capture")
        for table, columns, values, _ in closure.sets:
            self.sets.setdefault((table, columns), set()).add(values)
        for table, via, via_column, columns, values, _ in closure.derived:
            self.derived.setdefault((table, via, via_column, columns), set()).add(values)
        self.deadlines.extend(closure.deadlines)

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
        keys = set(self.merged)
        for table in self.tables:
            if table in coverage.COLD:
                kind = coverage.owner_kind(table)
                keys.add(coverage.key(table, self.user_id if kind == "u" else self.workspace_id, kind))
                keys.add(coverage.key(table, None))
            elif table in self.row_epochs:
                keys |= {coverage.key(table, self.user_id), coverage.key(table, None)}
        return keys

    def rows_read(self):
        return [(table, ident, version) for (table, ident), version in self.rows.items()]

    # Every cursor statement: tables, writes and SQL clock reads.
    def _cursor(self, conn, cursor, statement, parameters, context, executemany):
        if self._paused:
            return
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
            elif name in coverage.ROWS and name not in approved:
                self.refuse(f"unchecked read of {name}: {_snippet(statement)}")
            elif name != coverage.EPOCHS:
                self.tables.add(name)

    # Every ORM statement: bounded reads of the hot tables.
    def _orm_execute(self, state):
        if self._paused:
            return
        self._approved = frozenset()
        if state.is_insert or state.is_update or state.is_delete:
            self.refuse("write")
            return
        try:
            params = state.parameters if isinstance(state.parameters, dict) else {}
            self._approved = frozenset(self._inspect(state.statement, params))
        except Refused as error:
            self.refuse(f"{error}: {_snippet(str(state.statement))}")

    # Every loaded row: owner, row version and time window.
    def _loaded(self, session, instance):
        if self._paused:
            return
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

    def _derived_set(self, name, partner, partner_column, bounds):
        if partner[0] not in coverage.COLD or not bounds:
            return False
        columns = tuple(sorted(bounds))
        keys = list(product(*(bounds[column] for column in columns)))
        if len(keys) > 200:
            return False
        self.derived.setdefault((name, partner[0], partner_column, columns), set()).update(keys)
        return True

    def _bounded_set(self, name, occurrence_key, bounds):
        if not any(column in bounds for column in coverage.ANCHORS[name]):
            raise Refused(f"{name} read without its primary key or a bounded set")
        columns = tuple(sorted(bounds))
        keys = list(product(*(bounds[column] for column in columns)))
        if len(keys) > (1000 if columns == ("id",) else 200):
            raise Refused(f"{name} bounded set has too many keys")
        self.sets.setdefault((name, columns), set()).update(keys)

    def _inspect(self, statement, params):
        approved = set()
        if not isinstance(statement, Select):
            return approved
        loaded = _loaded_occurrences(statement)
        for select_ in [item for item in visitors.iterate(statement) if isinstance(item, Select)]:
            froms = select_.get_final_froms()
            joins = list(_joins(froms))
            where = list(_conjuncts([select_.whereclause]))
            filters = [select_.whereclause, *(join.onclause for join in joins),
                       *select_._order_by_clauses, *select_._group_by_clauses]
            kinds = {}
            for occurrence in _tables(froms):
                name, occurrence_key = _table_name(occurrence), _occurrence(occurrence)
                # Only conditions every returned row of this table satisfies:
                # an outer join's ON clause restricts its nullable side only.
                restricting = list(where)
                for join in joins:
                    if join.full:
                        continue
                    if not join.isouter or occurrence_key in {_occurrence(item) for item in _tables([join.right])}:
                        restricting += list(_conjuncts([join.onclause]))
                is_loaded = select_ is statement and occurrence_key in loaded
                if name in coverage.ROWS:
                    bound_map = _Bounds(restricting, params)
                    bounds = bound_map.of(occurrence_key)
                    used = _columns_of(occurrence_key, filters)
                    if not is_loaded:
                        used |= _columns_of(occurrence_key, select_.selected_columns)
                    reached = _key_partner(occurrence_key, restricting)
                    partner = reached[0] if reached else None
                    if "id" in bounds:
                        # Every row with these keys, whole: presence, absence
                        # and any column change all show on the next check.
                        self._bounded_set(name, occurrence_key, {"id": bounds["id"]})
                        kinds[occurrence_key] = "key"
                    elif partner is not None and (is_loaded or used <= set(coverage.ROWS[name])):
                        kinds[occurrence_key] = ("join", partner)
                        # Rows reached through a covered partner are loaded or
                        # read by identity only. Unless every candidate row is
                        # returned, an identity change can admit an excluded one.
                        if not is_loaded or _excludes(occurrence_key, restricting, select_):
                            # Every candidate a literally bounded cold partner
                            # names is kept whole; else any identity change counts.
                            if not self._derived_set(name, partner, reached[1], bound_map.of(partner)):
                                self.row_epochs.add(name)
                    else:
                        self._bounded_set(name, occurrence_key, bounds)
                        kinds[occurrence_key] = "set"
                    approved.add(name)
                elif name in _WINDOW_COLUMNS and _columns_of(occurrence_key, filters) & _WINDOW_COLUMNS[name]:
                    # Only loaded rows report when their window ends.
                    if not is_loaded or _bound(occurrence_key, restricting, "id") is None:
                        raise Refused(f"{name} time window read without its loaded rows")
            for occurrence_key, kind in kinds.items():
                if isinstance(kind, tuple) and not _anchored(occurrence_key, kinds):
                    raise Refused(f"{occurrence_key[0]} joined by key to an unbounded {kind[1][0]}")
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
    if table in coverage.ROWS:
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


def _excludes(occurrence_key, restricting, select_):
    """Whether this read can leave out a row its key join would reach."""
    for clause in restricting:
        if not _columns_of(occurrence_key, [clause]):
            continue
        if (isinstance(clause, BinaryExpression) and clause.operator is operators.eq
                and (_column_of(clause.left, occurrence_key, "id") or _column_of(clause.right, occurrence_key, "id"))):
            continue  # The key join itself admits every reachable row.
        return True
    # A limit after ordering by this table's own columns can drop a row.
    return select_._limit_clause is not None and bool(_columns_of(occurrence_key, select_._order_by_clauses))


def _anchored(occurrence_key, kinds, seen=()):
    """A key join is bounded only through a literal key, a set or a cold table."""
    kind = kinds.get(occurrence_key)
    if kind in {"key", "set"}:
        return True
    if not isinstance(kind, tuple) or occurrence_key in seen:
        return False
    partner = kind[1]
    if partner not in kinds:
        return partner[0] in coverage.COLD  # Epochs cover every row of a cold table.
    return _anchored(partner, kinds, (*seen, occurrence_key))


def _key_partner(occurrence_key, conjuncts):
    """``(occurrence, column)`` of another table this table's key equals, if any."""
    for clause in conjuncts:
        if not isinstance(clause, BinaryExpression) or clause.operator is not operators.eq:
            continue
        for mine, other in ((clause.left, clause.right), (clause.right, clause.left)):
            if (_column_of(mine, occurrence_key, "id") and _is_column(other)
                    and _occurrence(other.table) != occurrence_key):
                return _occurrence(other.table), other.name
    return None


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


class _Bounds:
    """Literal str/int values per column under ``=``/``IN``, following ``a = b``.

    A join equality makes both columns share one value set, so a set bounded
    through a joined row's literal key is bounded too. Execution parameters
    supply the values of named binds.
    """

    def __init__(self, conjuncts, params):
        self._parent, self._values, self._columns = {}, {}, set()
        for clause in conjuncts:
            if not isinstance(clause, BinaryExpression) or clause.operator not in (operators.eq, operators.in_op):
                continue
            left, right = clause.left, clause.right
            if clause.operator is operators.eq and _is_column(left) and _is_column(right):
                self._union(_column_key(left), _column_key(right))
                continue
            if clause.operator is operators.in_op and isinstance(left, Tuple):
                # (a, b) IN ((1, 2), ...): each column keeps its own values,
                # a superset of the pairs, which only widens what is kept.
                rows = _literal_values(right, params, many=True, tuples=True)
                for index, column in enumerate(left.clauses):
                    if rows and _is_column(column):
                        key = _column_key(column)
                        self._columns.add(key)
                        self._values.setdefault(key, sorted({row[index] for row in rows}, key=repr))
                continue
            sides = (((left, right), (right, left)) if clause.operator is operators.eq else ((left, right),))
            for column_side, value_side in sides:
                if not _is_column(column_side):
                    continue
                values = _literal_values(value_side, params, many=clause.operator is operators.in_op)
                if values:
                    key = _column_key(column_side)
                    self._columns.add(key)
                    self._values.setdefault(key, values)

    def _find(self, key):
        self._columns.add(key)
        while self._parent.get(key, key) != key:
            key = self._parent[key]
        return key

    def _union(self, first, second):
        first, second = self._find(first), self._find(second)
        if first != second:
            self._parent[first] = second

    def of(self, occurrence_key):
        roots = {}
        for key, values in self._values.items():
            roots.setdefault(self._find(key), values)
        return {name: roots[self._find((occurrence, name))] for occurrence, name in list(self._columns)
                if occurrence == occurrence_key and self._find((occurrence, name)) in roots}


def _is_column(element):
    return isinstance(element, ColumnClause) and element.table is not None


def _column_key(column):
    return (_occurrence(column.table), column.name)


def _literal(item):
    return isinstance(item, (str, int)) and not isinstance(item, bool)


def _literal_values(value_side, params, *, many, tuples=False):
    value_side = value_side.element if isinstance(value_side, Grouping) else value_side
    if not isinstance(value_side, BindParameter):
        return None
    value = value_side.effective_value
    if value is None and value_side.key in params:
        value = params[value_side.key]
    values = list(value or ()) if many else [value]
    if tuples:
        rows = [tuple(row) for row in values if isinstance(row, (tuple, list))]
        return rows if rows and len(rows) == len(values) and all(all(map(_literal, row)) for row in rows) else None
    if values and all(map(_literal, values)):
        return sorted(set(values), key=repr)
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
