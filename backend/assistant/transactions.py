"""Borrow an existing read transaction without splitting its SQL snapshot."""
from collections import OrderedDict
from contextlib import asynccontextmanager, contextmanager
from hashlib import sha256
import json

from sqlalchemy import text

from db.base import get_db_session


class SnapshotChecks:
    """Bounded reuse of independent checks inside one explicit SQL snapshot.

    Recursive message/decision graphs must keep their own depth, cycle and
    cardinality accounting. Never cache those graphs or use this for writes
    or provider pre-dispatch freshness checks.
    """
    MAX_ENTRIES = 512

    def __init__(self, db):
        self._db = db
        self._transaction = db.sync_session.get_transaction()
        self._values = OrderedDict()
        # Completed command groups (see command_sources.group_proof) belong to
        # the facts they were checked against, so they live and die with them.
        self._groups = {}
        self._task_facts_walk = None

    @property
    def reuse_task_facts(self):
        return self._task_facts_walk is not None

    def _require_snapshot(self, db):
        # The opt-in fact reader also remembers completed writes/flushes. A
        # clean identity map alone cannot detect an earlier SQLite flush.
        walk = self._task_facts_walk
        if walk is not None and (walk.db is not db or not walk.usable(db)):
            raise RuntimeError("Source checks require their original read-only snapshot")
        if (db is not self._db or self._transaction is None or not self._transaction.is_active
                or db.sync_session.get_transaction() is not self._transaction
                or db.in_nested_transaction() or db.new or db.dirty or db.deleted):
            raise RuntimeError("Source checks require their original read-only snapshot")

    def reusable(self, db):
        """Whether this snapshot can still share facts with ``db`` (no raise)."""
        try:
            self._require_snapshot(db)
        except RuntimeError:
            return False
        return True

    def _remember(self, key, value):
        # This is a retention bound, not a graph/source verification budget.
        # A later hot fact can replace an older one in the same SQL snapshot;
        # an evicted value simply takes the original validation path again.
        if self.MAX_ENTRIES <= 0:
            return
        if key in self._values:
            self._values[key] = value
            self._values.move_to_end(key)
            return
        while len(self._values) >= self.MAX_ENTRIES:
            self._values.popitem(last=False)
        self._values[key] = value

    async def check(self, db, kind, scope, payload, validate, *, fingerprint=None):
        # A REPEATABLE READ snapshot cannot refresh a held row with other
        # bytes, so fingerprints only matter to BoundaryChecks below.
        self._require_snapshot(db)
        key = (kind, *scope, _payload_digest(payload))
        if key in self._values:
            self._values.move_to_end(key)
            return self._values[key]
        value = await validate()
        self._require_snapshot(db)
        self._remember(key, value)
        return value

    async def read_many(self, db, kind, scope, ids, load_missing):
        """Read immutable facts in batches; never cache a reference's verdict.

        Share the bounded check cache. Missing IDs are loaded together, and
        this call keeps all returned facts even if they exceed cache capacity.
        """
        if not self.reuse_task_facts:
            raise RuntimeError("Task facts require an opted-in read-only snapshot")
        self._require_snapshot(db)
        keys = {identity: ("facts", kind, *scope, identity) for identity in ids}
        values = {}
        for identity, key in keys.items():
            if key in self._values:
                values[identity] = self._values[key]
                self._values.move_to_end(key)
        missing = [identity for identity in keys if identity not in values]
        if missing:
            loaded = await load_missing(missing)
            self._require_snapshot(db)
            for identity in missing:
                value = loaded.get(identity)
                values[identity] = value
                self._remember(keys[identity], value)
        return values


def _payload_digest(payload):
    return sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
                             separators=(",", ":")).encode()).hexdigest()


class BoundaryChecks(SnapshotChecks):
    """Read each independent fact once per boundary, in the caller's transaction.

    One hold check or provider/context checkpoint owns this object for one
    call. The transaction may be READ COMMITTED: a fact is read when it is
    first needed instead of again on every graph edge that reaches it, as a
    single uninterrupted read of that fact would be. Nothing survives the
    call, a flush or other write, a nested/changed transaction, or another
    boundary, and callers read current authority again after their graph.

    Recursive message/decision/command graphs keep their own path, depth and
    cardinality accounting. Held ORM rows are fingerprinted because a later
    READ COMMITTED read may refresh them in place; a changed row stops reuse.
    """

    def __init__(self, db, walk):
        super().__init__(db)
        self._task_facts_walk = walk
        self._stopped = False

    def _require_snapshot(self, db):
        raise RuntimeError("Boundary checks are not a read-only snapshot")

    def usable(self, db):
        if self._stopped:
            return False
        walk = self._task_facts_walk
        if (db is not self._db or self._transaction is None or not self._transaction.is_active
                or db.sync_session.get_transaction() is not self._transaction
                or db.in_nested_transaction() or db.new or db.dirty or db.deleted
                or walk.db is not db or not walk.usable(db)):
            self.stop()
            return False
        return True

    def reusable(self, db):
        return self.usable(db)

    def stop(self):
        self._stopped = True
        self._values.clear()
        self._groups.clear()

    async def check(self, db, kind, scope, payload, validate, *, fingerprint=None):
        if not self.usable(db):
            return await validate()
        key = (kind, *scope, _payload_digest(payload))
        if key in self._values:
            value, frozen = self._values[key]
            if fingerprint is None or fingerprint(value) == frozen:
                self._values.move_to_end(key)
                return value
            # Another read refreshed this held identity with new bytes. Never
            # return them under the verdict that checked the earlier bytes.
            self.stop()
            return await validate()
        value = await validate()
        if self.usable(db):
            self._remember(key, (value, fingerprint(value) if fingerprint is not None else None))
        return value

    async def read_many(self, db, kind, scope, ids, load_missing):
        unique = list(dict.fromkeys(ids))
        if not self.usable(db):
            loaded = await load_missing(unique) if unique else {}
            return {identity: loaded.get(identity) for identity in unique}
        keys = {identity: ("facts", kind, *scope, identity) for identity in unique}
        values = {}
        for identity, key in keys.items():
            if key in self._values:
                values[identity] = self._values[key][0]
                self._values.move_to_end(key)
        missing = [identity for identity in unique if identity not in values]
        if missing:
            loaded = await load_missing(missing)
            keep = self.usable(db)
            for identity in missing:
                value = loaded.get(identity)
                values[identity] = value
                if keep:
                    self._remember(keys[identity], (value, None))
        return values


@contextmanager
def boundary_checks(db):
    """Yield one boundary-owned fact scope, or None when reuse is unsafe.

    None keeps the original per-edge reads: no transaction has started, a
    savepoint is open, unflushed changes are pending, or a validation on
    another Session already owns the current command walk.
    """
    from assistant.command_sources import _command_walk, _walk
    existing = _walk.get()
    if ((existing is not None and existing.db is not db) or db.sync_session.get_transaction() is None
            or db.in_nested_transaction() or db.new or db.dirty or db.deleted):
        yield None
        return
    with _command_walk(db) as walk:
        if not walk.usable(db):
            yield None
            return
        checks = BoundaryChecks(db, walk)
        try:
            yield checks
        finally:
            checks.stop()


async def within_boundary(db, validate, *, user_id, workspace_id, main_id):
    """Run one top-level validation as its own boundary, then read authority.

    ``validate(checks)`` receives BoundaryChecks, or None (original per-edge
    reads) when the session cannot safely share facts. Authority is read
    again, uncached, after a shared graph, as BoundaryChecks requires.
    """
    with boundary_checks(db) as checks:
        value = await validate(checks)
        if checks is not None:
            from assistant.commands import _authority
            await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        return value


@asynccontextmanager
async def read_session(db=None):
    if db is not None:
        yield db
    else:
        async with get_db_session() as connection:
            yield connection


async def begin_snapshot(db):
    """Call before the first query. Main and execution writers use different locks."""
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
    else:
        # sqlite3 legacy transaction mode otherwise leaves SELECT outside BEGIN.
        await db.execute(text("BEGIN"))
    return SnapshotChecks(db)


@asynccontextmanager
async def source_snapshot(*, reuse_task_facts=False):
    """Share bounded command proofs across one read-only projection.

    Each message keeps its own source/decision graph limits. Command proofs
    retain their descendants and path heights, and nothing crosses this SQL
    snapshot, a write boundary, or a provider freshness checkpoint.
    """
    from assistant.command_sources import _command_walk
    async with get_db_session() as db:
        checks = await begin_snapshot(db)
        with _command_walk(db) as walk:
            if reuse_task_facts and checks is not None:
                checks._task_facts_walk = walk
            yield db, checks
