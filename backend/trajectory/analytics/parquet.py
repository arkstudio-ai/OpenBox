"""Parquet files of the analytics tables, built with DuckDB.

Rows are staged as JSON lines in the export's temporary directory and
converted there by an in-memory DuckDB connection limited to MEMORY_LIMIT and
one thread, which spills to the same directory. Before it runs anything else
the connection loses access to everything outside that directory:
extensions can be neither installed nor loaded (so no network file systems),
no other file can be read or written, and the configuration is locked.
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

from trajectory.analytics.schema import COLUMNS, ORDER, TABLES

MEMORY_LIMIT = "256MB"
THREADS = 1
#: Rows per Parquet row group; the writer buffers one group at a time.
ROW_GROUP_SIZE = 50_000
COMPRESSION = "zstd"
SPILL_DIRECTORY = "duckdb-spill"


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class StagedTable:
    """JSON lines of one table, exactly its columns on every line."""

    def __init__(self, directory: Path, table: str):
        self.table = table
        self.columns = frozenset(name for name, _ in COLUMNS[table])
        self.path = directory / f"{table}.jsonl"
        self.rows = 0
        self._file = self.path.open("w", encoding="utf-8")

    def write(self, rows: list[dict]) -> None:
        for row in rows:
            if row.keys() != self.columns:
                raise ValueError(f"{self.table} row does not match its columns: {sorted(row.keys() ^ self.columns)}")
            self._file.write(json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")))
            self._file.write("\n")
            self.rows += 1

    def close(self) -> None:
        self._file.close()


def connect(directory: Path) -> duckdb.DuckDBPyConnection:
    """An in-memory connection with the export's limits, confined to ``directory``."""
    directory = directory.resolve()
    spill = directory / SPILL_DIRECTORY
    spill.mkdir(exist_ok=True)
    connection = duckdb.connect(":memory:", config={
        "memory_limit": MEMORY_LIMIT, "threads": THREADS, "temp_directory": str(spill),
        "autoinstall_known_extensions": False, "autoload_known_extensions": False,
        "allow_community_extensions": False,
    })
    try:
        # allowed_directories can only be set once the database runs. Without
        # external access an explicit INSTALL or LOAD fails as well, which the
        # autoload settings alone do not prevent.
        connection.execute(f"SET allowed_directories = [{_literal(f'{directory}/')}]")
        connection.execute("SET enable_external_access = false")
        connection.execute("SET TimeZone = 'UTC'")
        connection.execute("SET preserve_insertion_order = false")
        connection.execute("SET lock_configuration = true")
    except BaseException:
        connection.close()
        raise
    return connection


def build(directory: Path, staged: dict[str, StagedTable]) -> dict[str, tuple[Path, int]]:
    """Write ``<table>.parquet`` in ``directory`` from each closed staged table; (path, rows) per table."""
    directory = directory.resolve()
    connection = connect(directory)
    try:
        built = {}
        for table in TABLES:
            columns = COLUMNS[table]
            types = ", ".join(f"{_literal(name)}: {_literal(type_)}" for name, type_ in columns)
            names = ", ".join(_identifier(name) for name, _ in columns)
            source = (f"read_json({_literal(str(staged[table].path.resolve()))}, format = 'newline_delimited', "
                      f"columns = {{{types}}})")
            target = directory / f"{table}.parquet"
            (rows,) = connection.execute(
                f"COPY (SELECT {names} FROM {source} ORDER BY {ORDER[table]}) TO {_literal(str(target))} "
                f"(FORMAT parquet, COMPRESSION {COMPRESSION}, ROW_GROUP_SIZE {ROW_GROUP_SIZE})").fetchone()
            if rows != staged[table].rows:
                raise RuntimeError(f"DuckDB wrote {rows} {table} rows of {staged[table].rows} staged")
            built[table] = (target, rows)
        return built
    finally:
        connection.close()
