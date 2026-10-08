"""Internal closed epoch retirement against real loopback HTTP and journals."""
import asyncio
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import asdict
import multiprocessing
import socket

import httpx
import pytest
from starlette.datastructures import Headers
import uvicorn
import websockets

from tests.unit.test_action_server_desktop_lease import server
from resource_gate import Fence, GateError, ResourceGate


OLD = Fence("d" * 64, 1, "automation", "fixture-workspace")
HUMAN = Fence(OLD.resource_id, 2, "human", "fixture-user")
AUTOMATION = Fence(OLD.resource_id, 3, "automation", OLD.owner_id)


@asynccontextmanager
async def listening(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    runtime = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False,
        log_level="error", ws="websockets"))
    task = asyncio.create_task(runtime.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(3):
            while not runtime.started:
                if task.done():
                    await task
                await asyncio.sleep(.01)
        yield sock.getsockname()[1]
    finally:
        runtime.should_exit = True
        await asyncio.wait_for(task, 3)
        sock.close()


@pytest.fixture
def gate(tmp_path, monkeypatch):
    result = ResourceGate(tmp_path / "control.sqlite3")
    monkeypatch.setattr(server, "_resource_gate", result)
    monkeypatch.setattr(server, "SESSION_API_KEY", "fixture-key")
    monkeypatch.setattr(server, "_desktop_lease", None)
    return result


def headers(gate, fence=OLD, step="fixture-step"):
    return {"X-API-Key": "fixture-key", "X-OpenBox-Resource": fence.resource_id,
        "X-OpenBox-Resource-Epoch": str(fence.epoch), "X-OpenBox-Resource-Owner": fence.owner_kind,
        "X-OpenBox-Resource-Owner-Id": fence.owner_id, "X-OpenBox-Resource-Operation": "fixture-effect",
        "X-OpenBox-Resource-Step": step, "X-OpenBox-Resource-Journal": gate.status()["journal_id"]}


def payload(gate, fence=OLD, target=HUMAN, command_id="transition"):
    return {**asdict(fence), "next_epoch": target.epoch, "next_owner_kind": target.owner_kind,
        "next_owner_id": target.owner_id, "command_id": command_id, "journal_id": gate.status()["journal_id"]}


async def test_real_http_auth_closed_roundtrip_and_late_terminal_reject(gate, tmp_path):
    pinned = gate.status()["journal_id"]
    gate.bind(OLD, "bind", pinned)
    async with listening(server.app) as port:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            assert (await client.post("/resource-control/advance_closed", json=payload(gate))).status_code == 403
            premature = await client.post("/resource-control/advance_closed", json=payload(gate), headers=headers(gate))
            assert premature.status_code == 409 and premature.json()["detail"] == "RESOURCE_MUST_BE_CLOSED"
            # This actual handler has a finite, known lease acquisition result.
            acquired = await client.post("/desktop/lease/acquire", headers={**headers(gate), "X-OpenBox-Instance": "fixture"},
                json={"owner": "fixture", "wait_timeout": 0})
            assert acquired.status_code == 200
            async with asyncio.timeout(3):
                while gate.receipt("fixture-step")["state"] in {"admitted", "running"}:
                    await asyncio.sleep(.01)
            assert gate.receipt("fixture-step")["state"] == "completed"
            gate.close(OLD, "close", pinned)
            advanced = await client.post("/resource-control/advance_closed", json=payload(gate), headers=headers(gate))
            assert advanced.status_code == 200
            assert advanced.json()["control"]["epoch"] == 2
            assert advanced.json()["control"]["admission"] == "closed"
            assert advanced.json()["remote_exclusivity_verified"] is False
            late = await client.post("/execute", headers=headers(gate, step="late"),
                json={"command": "printf never", "workdir": str(tmp_path)})
            assert late.status_code == 423
            returned = await client.post("/resource-control/advance_closed",
                json=payload(gate, HUMAN, AUTOMATION, "return"), headers=headers(gate))
            assert returned.status_code == 200 and returned.json()["control"]["epoch"] == 3
            replay = await client.post("/resource-control/advance_closed", json=payload(gate), headers=headers(gate))
            assert replay.json()["command_receipt"] == advanced.json()["command_receipt"]
            assert replay.json()["control"]["epoch"] == 3  # An old receipt cannot rewind control.
            released = await client.post("/desktop/lease/release", headers={"X-API-Key": "fixture-key"},
                json={"token": acquired.json()["token"]})
            assert released.json()["released"] is True
        with pytest.raises(websockets.exceptions.InvalidStatus) as refused:
            async with websockets.connect(f"ws://127.0.0.1:{port}/terminal?api_key=fixture-key",
                    additional_headers=headers(gate, step="late-terminal")):
                pytest.fail("An old terminal handshake crossed the closed new epoch")
        assert refused.value.response.status_code == 403
    reopened = ResourceGate(gate.path)
    assert reopened.status()["control"]["epoch"] == 3
    assert reopened.status()["blocking_count"] == 0
    with pytest.raises(GateError):
        reopened.receipt("late")


async def test_actual_shell_completion_stays_unknown_and_blocks_transition(gate, tmp_path):
    pinned = gate.status()["journal_id"]
    gate.bind(OLD, "bind", pinned)
    async with listening(server.app) as port:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
            executed = await client.post("/execute", headers=headers(gate),
                json={"command": "printf local-only", "workdir": str(tmp_path)})
            assert executed.json()["stdout"] == "local-only"
            # The client can receive the body before middleware's finally has
            # settled its journal. Neither in-flight state permits a handoff.
            assert gate.receipt("fixture-step")["state"] in {"running", "unknown"}
            gate.close(OLD, "close", pinned)
            blocked = await client.post("/resource-control/advance_closed", json=payload(gate), headers=headers(gate))
            assert blocked.status_code == 423 and blocked.json()["detail"] == "RESOURCE_NOT_DRAINED"
    reopened = ResourceGate(gate.path)
    assert reopened.receipt("fixture-step")["state"] == "unknown"
    assert reopened.status()["control"]["epoch"] == 1


@pytest.mark.parametrize("state", ["admitted", "running", "unknown"])
def test_legacy_work_in_any_nonterminal_state_prevents_new_epoch(gate, state):
    operation = gate.admit(Headers({}), "POST", "/execute")
    if state == "running":
        gate.checkpoint(operation)
    elif state == "unknown":
        gate.finish(operation)
    gate.close(OLD, "close", gate.status()["journal_id"])
    with pytest.raises(GateError, match="RESOURCE_NOT_DRAINED"):
        gate.advance_closed(OLD, HUMAN, "transition", gate.status()["journal_id"])
    assert gate.status()["control"]["epoch"] == 1


def _advance_in_process(path, owner):
    gate = ResourceGate(path)
    try:
        return gate.advance_closed(OLD, Fence(OLD.resource_id, 2, "human", owner), owner,
            gate.status()["journal_id"])["command_receipt"]["next_owner_id"]
    except GateError as exc:
        return exc.code


def test_independent_processes_compare_the_original_epoch(gate):
    gate.close(OLD, "close", gate.status()["journal_id"])
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
        outcomes = list(pool.map(_advance_in_process, [str(gate.path)] * 2, ["human-left", "human-right"]))
    assert outcomes.count("RESOURCE_FENCE_CHANGED") == 1
    winner = next(value for value in outcomes if value != "RESOURCE_FENCE_CHANGED")
    assert gate.status()["control"]["owner_id"] == winner
    assert gate.status()["control"]["admission"] == "closed"


def test_replacement_journal_and_reused_command_payload_cannot_advance(gate, tmp_path):
    pinned = gate.status()["journal_id"]
    gate.close(OLD, "close", pinned)
    gate.advance_closed(OLD, HUMAN, "transition", pinned)
    with pytest.raises(GateError, match="RESOURCE_COMMAND_CONFLICT"):
        gate.advance_closed(OLD, Fence(OLD.resource_id, 2, "human", "someone-else"), "transition", pinned)
    replacement = ResourceGate(tmp_path / "replacement.sqlite3")
    with pytest.raises(GateError, match="RESOURCE_JOURNAL_CHANGED"):
        replacement.advance_closed(OLD, HUMAN, "transition", pinned)
    assert replacement.status()["control"] is None
