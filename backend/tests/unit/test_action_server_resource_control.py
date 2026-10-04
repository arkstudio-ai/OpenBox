"""Remote persistent admission against the real Action Server and processes."""
import asyncio
from concurrent.futures import ProcessPoolExecutor
import multiprocessing

import httpx
import pytest
from starlette.requests import Request

from tests.unit.test_action_server_desktop_lease import server
from resource_gate import Fence, GateError, ResourceGate, ResourceMiddleware


FENCE = Fence("a" * 64, 1, "automation", "fixture-workspace")


def headers(step="step-1", *, epoch=1):
    return {"X-API-Key": "fixture-key", "X-OpenBox-Resource": FENCE.resource_id,
        "X-OpenBox-Resource-Epoch": str(epoch), "X-OpenBox-Resource-Owner": "automation",
        "X-OpenBox-Resource-Owner-Id": FENCE.owner_id,
        "X-OpenBox-Resource-Operation": "existing-effect-id", "X-OpenBox-Resource-Step": step}


@pytest.fixture
def journal(tmp_path, monkeypatch):
    gate = ResourceGate(tmp_path / "control.sqlite3")
    monkeypatch.setattr(server, "_resource_gate", gate)
    monkeypatch.setattr(server, "SESSION_API_KEY", "fixture-key")
    return gate


def child_admit(path, step):
    # Spawned processes deliberately reopen the same real file and acquire
    # independent SQLite locks; a module-local asyncio.Lock cannot pass this.
    from starlette.datastructures import Headers
    gate = ResourceGate(path)
    try:
        return gate.admit(Headers(headers(step)), "POST", "/execute")["id"]
    except GateError as exc:
        return exc.code


async def test_control_protocol_auth_no_human_grant_and_closed_bind_retry_stays_closed(journal):
    body = {"resource_id": FENCE.resource_id, "epoch": 1, "owner_kind": "automation",
        "owner_id": FENCE.owner_id, "command_id": "command-bind"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://fixture") as client:
        assert (await client.post("/resource-control/bind", json=body)).status_code == 403
        assert journal.status()["control"] is None
        assert (await client.get("/alive")).json()["capabilities"][-1] == "resource_admission_v1"
        assert (await client.post("/resource-control/bind", json=body, headers=headers())).status_code == 200
        denied = await client.post("/resource-control/bind", json={**body, "owner_kind": "human", "epoch": 2}, headers=headers())
        assert denied.status_code == 409
        closed = await client.post("/resource-control/close", json={**body, "command_id": "command-close"}, headers=headers())
        assert closed.json()["control"]["admission"] == "closed"
        repeated = await client.post("/resource-control/bind", json=body, headers=headers())
        assert repeated.json()["control"]["admission"] == "closed"
        assert repeated.json()["tracked_operations_drained"] is True
        assert repeated.json()["remote_exclusivity_verified"] is False


async def test_actual_subprocess_receipt_persists_without_claiming_descendant_drain(journal, tmp_path):
    journal.bind(FENCE, "bind")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://fixture") as client:
        result = await client.post("/execute", headers=headers(), json={"command": "printf fixture-ok", "workdir": str(tmp_path)})
        assert result.status_code == 200 and result.json()["stdout"] == "fixture-ok"
        assert result.headers["X-OpenBox-Remote-Operation"] == "step-1"
        duplicate = await client.post("/execute", headers=headers(), json={"command": "false", "workdir": str(tmp_path)})
        assert duplicate.status_code == 409
    reopened = ResourceGate(journal.path)
    assert reopened.receipt("step-1")["state"] == "unknown"
    assert reopened.status()["journal_id"] == journal.status()["journal_id"]
    assert reopened.status()["blocking_count"] == 1
    assert reopened.status()["blocking_operations"][0]["effect_id"] == "existing-effect-id"
    assert not reopened.status()["remote_exclusivity_verified"]


@pytest.mark.parametrize("method,path,payload", [
    ("POST", "/execute", {"command": "printf should-not-run"}),
    ("POST", "/execute_stream", {"command": "printf should-not-run"}),
    ("POST", "/write_file", {"path": "/tmp/never-write", "content": "fixture"}),
    ("POST", "/mcp/tools/fixture/tool", {"arguments": {}}),
    ("GET", "/proxy/9222/json/new", None),
    ("POST", "/dev-browser/start", {}),
    ("POST", "/restore", {"bucket": "fixture"}),
])
async def test_closed_gate_rejects_all_routes_before_handler_including_get_proxy(journal, method, path, payload, monkeypatch):
    journal.bind(FENCE, "bind")
    journal.close(FENCE, "close")
    async def forbidden(*args, **kwargs):
        pytest.fail("a closed resource launched a process")
    monkeypatch.setattr(server.asyncio, "create_subprocess_shell", forbidden)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://fixture") as client:
        result = await client.request(method, path, json=payload, headers=headers())
        assert result.status_code == 423
        assert result.json()["detail"] == "RESOURCE_CONTROL_HELD"
    assert journal.status()["blocking_count"] == 0


async def test_bound_gate_rejects_legacy_or_stale_clients_and_malformed_fences(journal):
    journal.bind(FENCE, "bind")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://fixture") as client:
        for given, status in (({"X-API-Key": "fixture-key"}, 423), (headers(epoch=2), 423),
                              ({**headers(), "X-OpenBox-Resource-Epoch": "01"}, 400)):
            result = await client.post("/execute", headers=given, json={"command": "printf blocked"})
            assert result.status_code == status
    assert journal.status()["blocking_count"] == 0


async def test_sse_prepared_before_close_rechecks_before_deferred_spawn(journal, monkeypatch, tmp_path):
    from starlette.datastructures import Headers
    journal.bind(FENCE, "bind")
    operation = journal.admit(Headers(headers()), "POST", "/execute_stream")
    journal.checkpoint(operation)
    scope = {"type": "http", "method": "POST", "path": "/execute_stream", "headers": [],
        "openbox.resource_operation": operation, "openbox.resource_gate": journal}
    class Deferred:
        def __init__(self, generator):
            self.generator = generator
    monkeypatch.setattr(server, "EventSourceResponse", Deferred)
    async def forbidden(*args, **kwargs):
        pytest.fail("the stream started an old process after close")
    monkeypatch.setattr(server.asyncio, "create_subprocess_shell", forbidden)
    response = await server.execute_stream(server.ExecuteRequest(command="printf stale", workdir=str(tmp_path)), Request(scope))
    journal.close(FENCE, "close")
    events = [event async for event in response.generator]
    assert [event["event"] for event in events] == ["error"]
    assert "RESOURCE_CONTROL_HELD" in events[0]["data"]
    journal.finish(operation)
    assert journal.receipt("step-1")["state"] == "unknown"


def test_independent_processes_cannot_admit_the_same_operation_twice(journal):
    journal.bind(FENCE, "bind")
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(child_admit, [str(journal.path)] * 2, ["one-operation"] * 2))
    assert sorted(results) == ["RESOURCE_OPERATION_ALREADY_RECORDED", "one-operation"]
    assert journal.status()["blocking_count"] == 1


async def test_admission_and_close_race_uses_durable_order_not_a_process_counter(journal):
    from starlette.datastructures import Headers
    journal.bind(FENCE, "bind")
    other = ResourceGate(journal.path)
    admitted, closed = await asyncio.gather(
        asyncio.to_thread(journal.admit, Headers(headers()), "POST", "/execute"),
        asyncio.to_thread(other.close, FENCE, "close"), return_exceptions=True)
    assert not isinstance(closed, Exception)
    if isinstance(admitted, GateError):
        assert admitted.status == 423 and journal.status()["blocking_count"] == 0
    else:
        assert journal.status()["blocking_count"] == 1
        with pytest.raises(GateError):
            journal.checkpoint(admitted)
    assert not journal.status()["remote_exclusivity_verified"]


@pytest.mark.parametrize("direction", ["input", "output"])
async def test_websocket_messages_recheck_closed_control_on_both_directions(journal, direction):
    journal.bind(FENCE, "bind")
    sent, performed = [], []
    incoming = [{"type": "websocket.connect"}, {"type": "websocket.receive", "text": "input"}]
    async def receive():
        return incoming.pop(0)
    async def send(message):
        sent.append(message)
    async def endpoint(scope, receive, send):
        await receive()
        await send({"type": "websocket.accept"})
        journal.close(FENCE, "close")
        if direction == "input":
            await receive()
        else:
            await send({"type": "websocket.send", "text": "old command"})
        performed.append(direction)
    scope = {"type": "websocket", "path": "/terminal", "query_string": b"api_key=fixture-key",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers().items()]}
    middleware = ResourceMiddleware(endpoint, lambda: journal, lambda: "fixture-key")
    await middleware(scope, receive, send)
    assert not performed and sent[-1]["type"] == "websocket.close" and sent[-1]["code"] == 4423
    assert journal.receipt("step-1")["state"] == "unknown"


def test_legacy_unbound_work_is_retained_after_bind_and_restart(journal):
    from starlette.datastructures import Headers
    operation = journal.admit(Headers({}), "POST", "/execute")
    journal.checkpoint(operation)
    journal.finish(operation)
    journal.bind(FENCE, "bind")
    reopened = ResourceGate(journal.path)
    status = reopened.close(FENCE, "close")
    assert status["blocking_count"] == 1
    assert status["blocking_operations"][0]["id"] == operation["id"]
    assert status["blocking_operations"][0]["effect_id"] is None
    assert status["control"]["admission"] == "closed"
