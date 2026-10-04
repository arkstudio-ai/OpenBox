"""Transport failures and admission ordering around the real worker boundary."""
import io
import struct
import types

import httpx
import pytest

from tests.unit.test_action_server_desktop_lease import server
from tests.unit.test_action_server_resource_control import journal, FENCE, headers  # noqa: F401
import file_worker


async def test_closed_resource_refuses_file_worker_dispatch(journal, monkeypatch, tmp_path):
    journal.bind(FENCE, "bind", journal.status()["journal_id"])
    journal.close(FENCE, "close", journal.status()["journal_id"])
    monkeypatch.setenv("OPENBOX_EXECUTOR_USER", "sandbox")
    async def forbidden(*args, **kwargs):
        pytest.fail("closed resource launched its file worker")
    monkeypatch.setattr(file_worker.asyncio, "create_subprocess_exec", forbidden)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app), base_url="http://fixture") as client:
        response = await client.post("/write_file", headers=headers(), json={"path": str(tmp_path / "must-not-write"), "content": "forbidden"})
        assert response.status_code == 423
    assert journal.status()["blocking_count"] == 0
    assert not (tmp_path / "must-not-write").exists()


async def test_previously_admitted_file_request_rechecks_close_before_spawn(journal, monkeypatch):
    from resource_gate import GateError
    from starlette.datastructures import Headers
    journal.bind(FENCE, "bind", journal.status()["journal_id"])
    operation = journal.admit(Headers(headers()), "POST", "/write_file")
    journal.checkpoint(operation)
    journal.close(FENCE, "close", journal.status()["journal_id"])
    scope = {"type": "http", "method": "POST", "path": "/write_file", "headers": [],
        "openbox.resource_gate": journal, "openbox.resource_operation": operation}
    async def forbidden(*args, **kwargs):
        pytest.fail("stale file request reached its handler or worker")
    monkeypatch.setattr(file_worker.asyncio, "create_subprocess_exec", forbidden)
    middleware = file_worker.FileOperationMiddleware(forbidden, lambda: True, lambda: {})
    with pytest.raises(GateError, match="RESOURCE_CONTROL_HELD"):
        await middleware(scope, forbidden, forbidden)
    journal.finish(operation)
    assert journal.receipt(operation["id"])["state"] == "unknown"


@pytest.mark.parametrize("frame", [struct.pack("!I", 65537), struct.pack("!I", 5) + b"bad", b"\0"])
def test_invalid_or_incomplete_body_frames_fail_closed(monkeypatch, frame):
    monkeypatch.setattr(file_worker.sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(frame)))
    with pytest.raises(file_worker.FileWorkerError):
        file_worker._read_chunk()


async def test_worker_failure_never_falls_through_to_privileged_handler(monkeypatch):
    from execution_identity import IsolationError
    async def broken(*args, **kwargs):
        raise IsolationError("fixture failure")
    async def forbidden(*args, **kwargs):
        pytest.fail("failed file worker fell back to the privileged handler")
    monkeypatch.setattr(file_worker, "run_request", broken)
    middleware = file_worker.FileOperationMiddleware(forbidden, lambda: True, lambda: {})
    sent = []
    async def send(message):
        sent.append(message)
    await middleware({"type": "http", "method": "POST", "path": "/write_file", "headers": []}, forbidden, send)
    assert sent[0]["status"] == 502


def test_slash_redirects_and_control_routes_remain_in_the_supervisor():
    assert file_worker.handles("/read_file")
    assert file_worker.handles("/skills/example/archive")
    for path in ("/read_file/", "/skills/example/", "/resource-control/close", "/execute", "/catalog"):
        assert not file_worker.handles(path)
