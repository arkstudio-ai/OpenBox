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

    def _require_snapshot(self, db):
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
