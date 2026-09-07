"""A failed browser probe must say which layer failed, and failures must be snapshotted."""
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sandbox import browser, diag


@pytest.fixture(autouse=True)
def fresh_ring():
    diag.clear()
    yield
    diag.clear()


def _exec(exit_code=0, stdout="", stderr=""):
    return SimpleNamespace(exit_code=exit_code, stdout=stdout, stderr=stderr)


async def test_probe_distinguishes_transport_connect_http_and_parse():
    transport = SimpleNamespace(execute=AsyncMock(side_effect=OSError("tunnel closed")))
    assert (await browser._probe_url(transport, "http://x")).describe() == "transport: OSError: tunnel closed"

    refused = SimpleNamespace(execute=AsyncMock(return_value=_exec(
        7, "\nOBX_HTTP=000", "curl: (7) Failed to connect to 127.0.0.1 port 9333: Connection refused")))
    probe = await browser._probe_url(refused, "http://x")
    assert probe.stage == "connect" and "Connection refused" in probe.detail

    not_found = SimpleNamespace(execute=AsyncMock(return_value=_exec(0, "no such target\nOBX_HTTP=404")))
    probe = await browser._probe_url(not_found, "http://x")
    assert probe.stage == "http" and probe.status == 404

    garbage = SimpleNamespace(execute=AsyncMock(return_value=_exec(0, "<html>\nOBX_HTTP=200")))
    assert (await browser._probe_url(garbage, "http://x")).stage == "parse"

    ok = SimpleNamespace(execute=AsyncMock(return_value=_exec(0, '{"Browser":"Chrome/1"}\nOBX_HTTP=200')))
    probe = await browser._probe_url(ok, "http://x")
    assert probe.ok and probe.data == {"Browser": "Chrome/1"} and probe.describe() == "ok"
    assert "-w" in ok.execute.await_args.args[0]


async def test_probe_tolerates_a_body_without_the_status_marker():
    """Older stubs and odd curls: a bare JSON body is still a success."""
    client = SimpleNamespace(execute=AsyncMock(return_value=_exec(0, '{"a":1}')))
    assert (await browser._probe_url(client, "http://x")).data == {"a": 1}


async def test_chrome_probe_reason_comes_from_the_renderer_probe_stderr(monkeypatch):
    version = browser.Probe(True, data={"Browser": "Chrome/151"})
    monkeypatch.setattr(browser, "_probe_url", AsyncMock(return_value=version))
    client = SimpleNamespace(execute=AsyncMock(return_value=_exec(
        1, "", "Chrome renderer probe failed: TimeoutError: no page target")))
    data, reason = await browser._probe_chrome_detailed(client)
    assert data is None and reason == "Chrome renderer probe failed: TimeoutError: no page target"
    assert client.execute.await_args.args[0].startswith(": obx-chrome-probe;")


async def test_chrome_probe_reason_names_the_http_layer(monkeypatch):
    monkeypatch.setattr(browser, "_probe_url", AsyncMock(
        return_value=browser.Probe(False, stage="connect", detail="Connection refused")))
    data, reason = await browser._probe_chrome_detailed(SimpleNamespace(execute=AsyncMock()))
    assert data is None and reason == "/json/version connect: Connection refused"


async def test_chrome_unavailable_carries_the_last_probe_reason_and_log_tail(monkeypatch):
    monkeypatch.setattr(browser, "_probe_chrome_detailed", AsyncMock(
        return_value=(None, "renderer probe exit 1")))
    monkeypatch.setattr(browser, "ensure_x_helper", AsyncMock())
    monkeypatch.setattr(browser, "_fire_and_forget", AsyncMock())
    monkeypatch.setattr(browser.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(browser, "CHROME_READY_BUDGET", 0)
    client = SimpleNamespace(execute=AsyncMock(return_value=_exec(0, "[1234:1234] cannot open display")))
    with pytest.raises(browser.ChromeUnavailable) as error:
        await browser.ensure_chrome(client, "ecd-x")
    text = str(error.value)
    assert "last probe: renderer probe exit 1" in text
    assert "cannot open display" in text


async def test_log_tail_explains_itself_when_unavailable():
    client = SimpleNamespace(execute=AsyncMock(side_effect=OSError("gone")))
    assert "unavailable: OSError: gone" in await browser._log_tail(client, "/tmp/x.log")
    empty = SimpleNamespace(execute=AsyncMock(return_value=_exec(0, "")))
    assert "empty or missing" in await browser._log_tail(empty, "/tmp/x.log")


async def test_status_reports_why_each_side_is_missing(monkeypatch):
    probes = [
        browser.Probe(False, stage="transport", detail="OSError: tunnel closed"),
        browser.Probe(False, stage="connect", detail="refused"),
    ]
    monkeypatch.setattr(browser, "_probe_url", AsyncMock(side_effect=probes))
    status = await browser.browser_status(SimpleNamespace())
    assert status["chrome"] is None and status["relay"] is None
    assert status["problems"] == {"chrome": "transport: OSError: tunnel closed", "relay": "connect: refused"}


async def test_status_has_no_problems_when_healthy(monkeypatch):
    monkeypatch.setattr(browser, "_probe_url", AsyncMock(side_effect=[
        browser.Probe(True, data={"Browser": "HeadlessChrome/1"}),
        browser.Probe(True, data={"mode": "local"}),
    ]))
    status = await browser.browser_status(SimpleNamespace())
    assert status["problems"] == {"chrome": None, "relay": None}
    assert status["presentation"] == "headless"


async def test_fallback_to_local_keeps_the_reason(monkeypatch):
    monkeypatch.setattr(browser, "ensure_relay", AsyncMock(side_effect=[
        browser.RelayUnavailable("relay did not answer on :9222 within 20s (last probe: connect: refused)\nrelay log:\n..."),
        {"mode": "local", "chromeAvailable": True},
    ]))
    monkeypatch.setattr(browser, "ensure_chrome", AsyncMock(return_value={"Browser": "Chrome/1"}))
    state = await browser._ensure_browser_locked(SimpleNamespace(), "ecd-x", "auto")
    assert state["mode"] == "local"
    assert state["fallback_reason"].startswith("relay did not answer on :9222")


async def test_browser_failure_is_snapshotted_and_cited(monkeypatch):
    import core.config as config
    monkeypatch.setattr(config, "get_config", lambda: SimpleNamespace(sandbox_provider="docker"))
    monkeypatch.setattr(browser, "_ensure_browser_locked", AsyncMock(
        side_effect=browser.ChromeUnavailable("Chrome did not open its debug port\nchrome log:\nboom")))
    report = {"diag_version": "x", "summary": {"lights": {"chrome": "down"}, "findings": []}}

    @asynccontextmanager
    async def lease(**kwargs):
        yield

    client = SimpleNamespace(
        desktop_lease=lease,
        execute=AsyncMock(return_value=_exec(0, json.dumps(report))),
    )
    with pytest.raises(browser.ChromeUnavailable) as error:
        await browser.ensure_browser(client, "session-1", "local")
    diag_id = error.value.diag_id
    stored = await diag.get(diag_id)
    assert stored["report"] == {**report, "via": "action_server"}
    assert stored["reason"] == "ChromeUnavailable" and stored["session_id"] == "session-1"
    assert diag.summarize_error(error.value) == f"Chrome did not open its debug port [diag:{diag_id}]"
    # The snapshot was taken through the same client, after the failure.
    assert "obx-diag" in client.execute.await_args.args[0]
    # And the bring-up itself is on the timeline, citing the snapshot.
    from sandbox import events
    ensure = await events.list_events(kind="browser.ensure", session_id="session-1")
    assert len(ensure) == 1 and ensure[0]["status"] == "fail" and ensure[0]["diag_id"] == diag_id
    assert ensure[0]["summary"].startswith("ChromeUnavailable: Chrome did not open")


async def test_runtime_problems_survive_into_the_exception():
    from sandbox import browser_runtime as runtime
    output = json.dumps({"version": runtime.RUNTIME_VERSION, "ready": False,
                         "problems": ["missing command: node", "TasksMax too low"]})
    with pytest.raises(runtime.BrowserRuntimeUnavailable) as error:
        runtime.verified_result(output)
    assert error.value.problems == ["missing command: node", "TasksMax too low"]
    assert "missing command: node; TasksMax too low" in str(error.value)

    with pytest.raises(runtime.BrowserRuntimeUnavailable, match="version 'old'"):
        runtime.verified_result('{"version":"old","ready":true}')
    with pytest.raises(runtime.BrowserRuntimeUnavailable, match="no result"):
        runtime.verified_result("just noise")


async def test_install_failure_keeps_the_installer_output():
    from sandbox import browser_runtime as runtime
    client = SimpleNamespace(execute=AsyncMock(side_effect=[
        _exec(1, "", "verifier crashed"),
        _exec(2, "backup=/opt/openbox/backups/x\n", "npm ERR! code E404\nnpm ERR! 404 Not Found"),
    ]))
    with pytest.raises(runtime.BrowserRuntimeUnavailable) as error:
        await runtime.ensure_browser_runtime(client)
    assert "Installer exited 2: npm ERR! 404 Not Found" in str(error.value)
    assert "E404" in error.value.output


def test_launch_scripts_reclaim_logs_left_by_another_identity():
    """fs.protected_regular=2 lets a stale sandbox-owned /tmp log silently sink a
    root launch: the redirect fails into /dev/null and nothing ever starts."""
    reclaim = 'rm -f "$f"'
    headed = browser._chrome_launch_script()
    assert reclaim in headed and headed.index(browser.CHROME_LOG) < headed.index("pgrep -x gnome-shell")
    assert headed.index(f"for f in {browser.IBUS_LOG}") < headed.index("ibus-daemon --replace")
    assert f'chown "$U" {browser.IBUS_LOG}' in headed
    relay = browser._relay_start_script("local")
    assert reclaim in relay and relay.index(browser.RELAY_LOG) < relay.index("start-relay")
    headless = browser._headless_chrome_launch_script()
    assert reclaim in headless and headless.index(browser.CHROME_LOG) < headless.index("useradd")
