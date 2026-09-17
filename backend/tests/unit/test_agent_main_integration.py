"""The durable kernel composes with main's workspace and question lifecycle."""
import pytest

from agent import driver
from db.models.question import SessionExecution
from db.models.session import Session
from question import runtime
from tests.unit.test_durable_questions import read, state  # noqa: F401


async def test_driver_and_question_runtime_share_run_identity(state):
    lease = await driver.reserve_run("s1", "u1")
    try:
        ticket = await runtime.start_run("s1", "u1", driver_lease=lease)
        assert ticket is not None
        assert ticket.run_id == lease.run_id
        assert (await read(Session, "s1")).workspace_id == "w1"
        assert (await read(SessionExecution, "s1")).run_id == lease.run_id
        await runtime.finish_run(ticket, completed=True)
    finally:
        await lease.release()


async def test_durable_reservation_rejects_another_workspace_member(state):
    with pytest.raises(LookupError):
        await driver.reserve_run("s1", "u2")
    assert await driver.get_driver_state("s1") is None
    assert (await read(Session, "s1")).status == "idle"
