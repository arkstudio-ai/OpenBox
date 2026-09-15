"""Shared fixtures for producer tests: a real spool emitter and the lines it wrote."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import event

import db.base as database
from trajectory import spool
from trajectory.emitter import get_emitter, reset_emitter_for_tests


class Spool:
    """Reads what the process emitter wrote, the way the worker will."""

    def __init__(self, root: Path):
        self.root = root

    def lines(self) -> list[dict]:
        emitter = get_emitter()
        if emitter is not None:
            assert emitter.flush(5)
        records = []
        producers = self.root / spool.PRODUCERS_DIR
        if not producers.exists():
            return records
        for producer in sorted(producers.iterdir()):
            for name in sorted(producer.glob(f"*{spool.CLOSED_SUFFIX}")):
                records.extend(spool.decode_line(line) for line in name.read_bytes().splitlines())
        return records

    def events(self, *types: str) -> list[dict]:
        return [line["event"] for line in self.lines()
                if line["k"] == spool.KIND_EVENT and (not types or line["event"]["type"] in types)]

    def controls(self, *types: str) -> list[dict]:
        return [line["control"] for line in self.lines()
                if line["k"] == spool.KIND_CONTROL and line["control"]["type"] != "producer.goodbye"
                and (not types or line["control"]["type"] in types)]


@pytest.fixture
def recording_spool(tmp_path, monkeypatch):
    """Recording on for every user, through a fresh emitter writing to a private spool."""
    from trajectory import producers
    producers.reset_for_tests()
    reset_emitter_for_tests()
    monkeypatch.setenv("TRAJECTORY_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setenv("TRAJECTORY_RECORDING_ENABLED", "true")
    monkeypatch.delenv("TRAJECTORY_RECORD_USER_IDS", raising=False)
    yield Spool(tmp_path / "spool")
    reset_emitter_for_tests()


@pytest.fixture
def business_statements():
    """SQL statements sent to the business engine (request after the engine's fixture)."""
    seen: list[str] = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)
    engine = database._engine.sync_engine
    event.listen(engine, "before_cursor_execute", listener)
    yield seen
    event.remove(engine, "before_cursor_execute", listener)
