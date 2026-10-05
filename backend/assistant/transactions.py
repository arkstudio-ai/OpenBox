"""Borrow an existing read transaction without splitting its SQL snapshot."""
from contextlib import asynccontextmanager
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
        self._values = {}
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

    async def check(self, db, kind, scope, payload, validate):
        self._require_snapshot(db)
        digest = sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
                                   separators=(",", ":")).encode()).hexdigest()
        key = (kind, *scope, digest)
        if key in self._values:
            return self._values[key]
        value = await validate()
        self._require_snapshot(db)
        if len(self._values) < self.MAX_ENTRIES:
            self._values[key] = value
        return value

    async def read_many(self, db, kind, scope, ids, load_missing):
        """Read immutable facts in batches; never cache a reference's verdict.

        Share the check budget. When it is full, missing IDs are still loaded
        together rather than degrading into one query per ID.
        """
        if not self.reuse_task_facts:
            raise RuntimeError("Task facts require an opted-in read-only snapshot")
        self._require_snapshot(db)
        keys = {identity: ("facts", kind, *scope, identity) for identity in ids}
        values = {identity: self._values[key] for identity, key in keys.items() if key in self._values}
        missing = [identity for identity in keys if identity not in values]
        if missing:
            loaded = await load_missing(missing)
            self._require_snapshot(db)
            for identity in missing:
                value = loaded.get(identity)
                values[identity] = value
                if len(self._values) < self.MAX_ENTRIES:
                    self._values[keys[identity]] = value
        return values


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
