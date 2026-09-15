"""Ingest failure handling (SPEC §8.2, §8.3): quarantine moves that fail, transient errors, failure counts, retries."""
import logging
import time

import pytest

from trajectory.worker import spool_reader
from tests.unit.test_worker_ingest import event, events_of, harness, settings, trace_db  # noqa: F401


class _Messages(logging.Handler):
    def __init__(self, text: str):
        super().__init__(logging.WARNING)
        self.text, self.messages = text, []

    def emit(self, record):
        if self.text in record.getMessage():
            self.messages.append(record.getMessage())


@pytest.fixture
def warnings_with():
    """``warnings_with(text)``: the list of ingest warnings containing ``text`` from then on."""
    logger = logging.getLogger("openbox.trajectory.worker.ingest")
    handlers = []

    def attach(text: str) -> list[str]:
        handler = _Messages(text)
        logger.addHandler(handler)
        handlers.append(handler)
        return handler.messages

    yield attach
    for handler in handlers:
        logger.removeHandler(handler)


def _release(service):
    """Let every file and batch that backs off retry on the next pass."""
    for failure in service._failures.values():
        failure.next_at = 0.0
    for held in service._backoff.values():
        held.next_at = 0.0


def _wait(entry) -> float:
    return entry.next_at - time.monotonic()


async def test_a_file_that_cannot_be_moved_to_quarantine_blocks_its_producer_and_backs_off(harness, monkeypatch,
                                                                                         warnings_with):
    moves = []
    move = spool_reader.quarantine_file

    def failing_move(spool_dir, item, **kwargs):
        moves.append(item.name)
        return None

    monkeypatch.setattr(spool_reader, "quarantine_file", failing_move)
    warnings = warnings_with("Could not quarantine spool file")
    writer = harness.writer
    bad = writer.file([writer.line("event", event(event_id="ok")), b"garbage\n"])
    later = writer.events(event(event_id="later"))
    key = (writer.producer_id, bad.name)
    result = await harness.run()
    assert (result["events"], result["quarantined_files"]) == (1, 0) and bad.exists() and later.exists()
    failure = harness.service._failures[key]
    assert (failure.attempts, failure.failures) == (1, 0) and 0 < _wait(failure) <= 1
    # While it backs off, neither the file nor the producer's later file is read.
    result = await harness.run()
    assert result["deferred_batches"] == 1 and moves == [bad.name] and later.exists()
    _release(harness.service)
    await harness.run()
    failure = harness.service._failures[key]
    assert len(moves) == 2 and failure.attempts == 2 and 1 < _wait(failure) <= 2 and later.exists()
    assert len(warnings) == 2
    monkeypatch.setattr(spool_reader, "quarantine_file", move)
    _release(harness.service)
    result = await harness.run()
    assert result["quarantined_files"] == 1 and not bad.exists() and not later.exists()
    assert harness.service._failures == {} and len(warnings) == 2
    _, stored = await events_of("ses_1")
    assert stored[1].event_id == "ok" and stored[-1].event_id == "later"
