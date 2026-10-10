"""Read transactions for assistant views and tools."""
from contextlib import asynccontextmanager

from sqlalchemy import text

from db.base import get_db_session


@asynccontextmanager
async def read_session(db=None):
    if db is not None:
        yield db
    else:
        async with get_db_session() as connection:
            yield connection


async def begin_snapshot(db):
    """Call before the first query: one consistent, read-only snapshot."""
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
    else:
        # sqlite3 legacy transaction mode otherwise leaves SELECT outside BEGIN.
        await db.execute(text("BEGIN"))


@asynccontextmanager
async def source_snapshot():
    """A new consistent read-only snapshot, as ``(db, None)``."""
    async with get_db_session() as db:
        await begin_snapshot(db)
        yield db, None
