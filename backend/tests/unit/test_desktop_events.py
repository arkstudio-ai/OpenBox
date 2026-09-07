"""The desktop timeline: best-effort rows, spans that record outcomes, bounded detail."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sandbox import events


@pytest.fixture(autouse=True)
async def empty_table():
    await events.purge(older_than_days=-1)
    yield
    await events.purge(older_than_days=-1)


async def test_emit_stores_and_lists_newest_first():
    first = await events.emit("browser.ensure", desktop_id="ecd-1", summary="one")
    second = await events.emit("browser.ensure", desktop_id="ecd-1", status="fail", summary="two")
    assert first and second and first != second
    rows = await events.list_events(desktop_id="ecd-1")
    assert [r["summary"] for r in rows] == ["two", "one"]
    assert rows[0]["status"] == "fail" and rows[0]["container_key"] == "ecd-1"
    assert (await events.get(first))["summary"] == "one"
    assert await events.get("nope") is None
    assert [r["summary"] for r in await events.list_events(status="fail")] == ["two"]
    assert [r["summary"] for r in await events.list_events(kind="lease.acquire")] == []


async def test_desktop_filter_also_matches_rows_keyed_by_container_only():
    await events.emit("browser.diag", container_key="ecd-9", summary="keyed by container")
    assert [r["summary"] for r in await events.list_events(desktop_id="ecd-9")] == ["keyed by container"]


async def test_client_trace_labels_the_row():
    from sandbox.client import SandboxClient

    client = SandboxClient(host="h", port=1, api_key="k", desktop_id="ecd-7")
    async with client.request_context(session_id="sess", tool_call_id="call-1"):
        event_id = await events.emit("browser.ensure", client=client, summary="traced")
    row = await events.get(event_id)
    assert (row["session_id"], row["tool_call_id"], row["desktop_id"]) == ("sess", "call-1", "ecd-7")


async def test_span_records_success_with_duration_and_detail():
    async with events.span("browser.chrome_launch", container_key="s1") as event:
        event.detail["presentation"] = "headed"
        event.summary = "up"
    row = await events.get(event.event_id)
    assert row["status"] == "ok" and row["duration_ms"] >= 0
    assert row["detail"] == {"presentation": "headed"} and row["summary"] == "up"


async def test_span_records_failure_and_reraises():
    error = RuntimeError("Chrome did not open\nlog tail")
    error.diag_id = "dev_x"
    with pytest.raises(RuntimeError):
        async with events.span("browser.ensure", container_key="s1") as event:
            raise error
    row = await events.get(event.event_id)
    assert row["status"] == "fail" and row["diag_id"] == "dev_x"
    assert row["summary"] == "RuntimeError: Chrome did not open"
    assert row["detail"]["error"].startswith("Chrome did not open\nlog tail")


async def test_span_marks_timeouts():
    with pytest.raises(asyncio.TimeoutError):
        async with events.span("channel.verify", desktop_id="ecd-1") as event:
            raise asyncio.TimeoutError()
    assert (await events.get(event.event_id))["status"] == "timeout"


async def test_emit_never_raises_when_the_database_is_down(monkeypatch):
    @asynccontextmanager
    async def broken():
        raise RuntimeError("db down")
        yield

    monkeypatch.setattr(events, "get_db_session", broken)
    assert await events.emit("browser.ensure", summary="x") is None
    async with events.span("browser.ensure") as event:
        pass
    assert event.event_id is None


async def test_detail_is_bounded():
    huge = {"log": "x" * (events.DETAIL_LIMIT + 100), "small": 1}
    event_id = await events.emit("browser.diag", detail=huge)
    detail = (await events.get(event_id))["detail"]
    assert detail["small"] == 1 and detail["_truncated"] is True
    assert "bytes dropped" in detail["log"]


async def test_purge_keeps_recent_rows():
    from db.base import get_db_session
    from db.models.desktop_event import DesktopEvent

    keep = await events.emit("browser.ensure", summary="keep")
    old = await events.emit("browser.ensure", summary="old")
    async with get_db_session() as session:
        row = await session.get(DesktopEvent, old)
        row.ts = datetime.now(timezone.utc) - timedelta(days=events.RETENTION_DAYS + 1)
    assert await events.purge() == 1
    assert [r["id"] for r in await events.list_events()] == [keep]


async def test_slow_or_refused_lease_lands_on_the_timeline(monkeypatch):
    import httpx
    from sandbox import client as client_module

    class FakeHTTP:
        def __init__(self, responses):
            self.responses = responses

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, path, **kwargs):
            if path == "/desktop/lease/release":
                return httpx.Response(200, json={"released": True}, request=httpx.Request("POST", path))
            response = self.responses.pop(0)
            response._request = httpx.Request("POST", path)
            return response

    client = client_module.SandboxClient(host="h", port=1, api_key="k", desktop_id="ecd-3")
    slow = httpx.Response(200, json={"token": "t", "wait_ms": 5000, "ttl_seconds": 60})
    monkeypatch.setattr(client, "_client", lambda **kw: FakeHTTP([slow]))
    async with client.desktop_lease(session_id="s", tool_call_id="c"):
        pass
    rows = await events.list_events(kind="lease.acquire")
    assert len(rows) == 1 and rows[0]["status"] == "info" and "5000ms" in rows[0]["summary"]
    assert rows[0]["desktop_id"] == "ecd-3" and rows[0]["session_id"] == "s"

    busy = httpx.Response(423, json={"detail": {"holder_session": "other"}})
    monkeypatch.setattr(client, "_client", lambda **kw: FakeHTTP([busy]))
    with pytest.raises(httpx.HTTPStatusError):
        async with client.desktop_lease(session_id="s", tool_call_id="c2"):
            pass
    refused = await events.list_events(kind="lease.acquire", status="fail", with_detail=True)
    assert len(refused) == 1 and refused[0]["detail"]["holder"]["holder_session"] == "other"

    fast = httpx.Response(200, json={"token": "t", "wait_ms": 10, "ttl_seconds": 60})
    monkeypatch.setattr(client, "_client", lambda **kw: FakeHTTP([fast]))
    async with client.desktop_lease(session_id="s", tool_call_id="c3"):
        pass
    assert len(await events.list_events(kind="lease.acquire")) == 2


async def test_browser_runtime_repair_is_on_the_timeline():
    from sandbox import browser_runtime as runtime
    import json

    ok = json.dumps({"version": runtime.RUNTIME_VERSION, "ready": True, "changed": True})
    client = SimpleNamespace(execute=AsyncMock(side_effect=[
        SimpleNamespace(exit_code=0, stdout='{"version":"old","ready":true}', stderr=""),
        SimpleNamespace(exit_code=0, stdout="backup=/x\n" + ok, stderr=""),
    ]))
    assert (await runtime.ensure_browser_runtime(client))["changed"] is True
    check = await events.list_events(kind="browser.runtime_check")
    repair = await events.list_events(kind="browser.runtime_repair")
    assert check[0]["status"] == "fail" and "version 'old'" in check[0]["summary"]
    assert repair[0]["status"] == "ok" and repair[0]["summary"].startswith(f"repaired to {runtime.RUNTIME_VERSION}")

    # A passing check leaves no trace: it happens on every turn.
    quiet = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(exit_code=0, stdout=ok, stderr="")))
    await runtime.ensure_browser_runtime(quiet)
    assert len(await events.list_events(kind="browser.runtime_check")) == 1
