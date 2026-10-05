"""Actual Chromium private-pipe + independent HTTP/WS clients, local pages only.

Run in a named network=none disposable container with the explicit fixture
variables below. A normal pytest run never launches a user's installed browser.
All created fixture profiles/journals are retained; only owned processes stop.
"""
import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse


ROOT = Path(__file__).resolve().parents[3] if len(Path(__file__).resolve().parents) > 3 else Path("/fixture")
MODULES = Path(os.environ.get("BROWSER_RESOURCE_MODULE_DIR", str(ROOT / "container")))
sys.path.insert(0, str(MODULES))
sys.path.insert(0, str(ROOT / "backend/sandbox"))
from browser_resource import BrowserJournal, BrowserSupervisor, create_app  # noqa: E402
from browser_pipe import BrowserPipe  # noqa: E402
from browser_resource_client import BrowserResourceClient, BrowserResourceError  # noqa: E402


pytestmark = pytest.mark.skipif(os.environ.get("BROWSER_RESOURCE_FIXTURE_ISOLATED") != "1",
    reason="Requires a new dedicated network=none local Chromium fixture; never use an existing browser profile")
KEY = "local-fixture-backend-only-key"


@asynccontextmanager
async def listening(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    runtime = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="error", ws="websockets"))
    task = asyncio.create_task(runtime.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not runtime.started:
                if task.done():
                    await task
                await asyncio.sleep(.01)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        runtime.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


@pytest.fixture
async def actual():
    root = Path(os.environ.get("BROWSER_RESOURCE_TEST_ROOT", "/fixture/retained-tests"))
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    state = root / uuid4().hex
    state.mkdir(mode=0o755)
    journal = BrowserJournal(state / "control", uuid4().hex * 2, "fixture-workspace")
    home = state / ("browser-" + journal.identity["profile_id"])
    home.mkdir(mode=0o700)
    uid = int(os.environ.get("BROWSER_RESOURCE_UID", "1000"))
    pipe = BrowserPipe(os.environ.get("BROWSER_RESOURCE_CHROMIUM", "/usr/lib/chromium/chromium"), home / "profile",
        uid=uid, gid=uid, isolation=os.environ.get("BROWSER_RESOURCE_ISOLATION", "chromium_sandbox"),
        fixture_no_sandbox=os.environ.get("BROWSER_RESOURCE_NO_SANDBOX") == "1", timeout=2)
    supervisor = BrowserSupervisor(journal, pipe)
    await supervisor.start()
    application = FastAPI()
    events = []
    slow_started, slow_release = asyncio.Event(), asyncio.Event()

    @application.get("/record")
    async def record(request: Request):
        events.append(dict(request.query_params))
        return {"accepted": True}

    @application.get("/slow")
    async def slow():
        slow_started.set()
        await slow_release.wait()
        return HTMLResponse("<title>slow completed</title>Completed")

    @application.get("/{page}")
    async def page(page: str):
        return HTMLResponse("""<!doctype html><title>Local finite input</title>
<style>body{margin:0;height:2400px}input{position:absolute;left:20px;top:30px;width:240px;height:40px}
button{position:absolute;left:20px;top:110px;width:160px;height:50px}</style>
<input id='field' aria-label='Local field' oninput="fetch('/record?type=text&value='+encodeURIComponent(this.value))">
<button onclick="fetch('/record?type=click')">Record click</button>
<p style='position:absolute;top:220px'>Local browser profile input fixture</p>""")

    try:
        async with listening(application) as page_url, listening(create_app(supervisor, KEY, manage_lifespan=False)) as url:
            client = BrowserResourceClient(url, KEY, journal.identity, timeout=8)
            yield {"journal": journal, "pipe": pipe, "supervisor": supervisor, "client": client,
                "url": url, "page_url": page_url, "events": events, "slow_started": slow_started,
                "slow_release": slow_release, "state": state}
    finally:
        slow_release.set()
        await supervisor.stop()


async def capture(fixture, fence, key="capture", token=None):
    result = await fixture["client"].operate(fence=fence, operation_id=key, kind="capture", human_token=token)
    assert result["receipt"]["state"] == "completed"
    payload = result["receipt"]["result"]
    png = base64.b64decode(payload["png_base64"], validate=True)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert payload["observation"]["sha256"] == hashlib.sha256(png).hexdigest()
    assert (payload["observation"]["width"], payload["observation"]["height"]) == (1024, 768)
    return payload["observation"]


async def navigate(fixture, path="/a"):
    fence = (await fixture["client"].status())["control"]["fence"]
    observation = await capture(fixture, fence, uuid4().hex)
    result = await fixture["client"].operate(fence=fence, operation_id=uuid4().hex, kind="navigate",
        args={"url": fixture["page_url"] + path}, observation_id=observation["observation_id"])
    assert result["receipt"]["state"] == "completed"
    return fence


async def takeover(fixture, fence, ttl=60):
    client = fixture["client"]
    await client.control("close", fence=fence, command_id="close-old", actor_id="fixture-human")
    return await client.control("takeover", fence=fence, command_id="takeover", actor_id="fixture-human",
        next_owner_id="fixture-human", ttl_seconds=ttl)


async def wait_event(fixture, expected):
    async with asyncio.timeout(3):
        while expected not in fixture["events"]:
            await asyncio.sleep(.01)


async def test_actual_mouse_text_ws_giveback_requires_new_observation(actual, record_property):
    client = actual["client"]
    auto = await navigate(actual)
    before = await capture(actual, auto, "before-human")
    granted = await takeover(actual, auto)
    human, token = granted["control"]["fence"], granted["human_token"]
    assert human["epoch"] == auto["epoch"] + 1
    record_property("browser_identity", json.dumps(actual["journal"].identity, sort_keys=True))
    record_property("browser_sandbox", str(granted["browser_sandbox"]))
    record_property("isolation", json.dumps(granted["isolation"], sort_keys=True))
    if actual["pipe"].fixture_no_sandbox:
        assert granted["isolation"]["mode"] == granted["isolation"]["verification"] == "diagnostic"
    else:
        assert granted["isolation"]["mode"] == actual["pipe"].isolation
        assert granted["isolation"]["verification"] == "passed"
        assert all(granted["isolation"]["checks"].values())
        assert granted["browser_sandbox"] is (actual["pipe"].isolation == "chromium_sandbox")
    async with client.human_socket(fence=human, human_token=token) as ws:
        for operation_id, kind, args in [
            ("human-focus", "mouse", {"x": 90, "y": 50, "button": "left"}),
            ("human-text", "text", {"text": "actual human input"}),
            ("human-input-view", "capture", {}),
            ("human-tab", "key", {"key": "Tab"}),
            ("human-click", "mouse", {"x": 90, "y": 130, "button": "left"}),
            ("human-wheel", "wheel", {"x": 300, "y": 300, "delta_x": 0, "delta_y": 100}),
            ("human-capture", "capture", {})]:
            await ws.send(json.dumps(client._operation(fence=human, operation_id=operation_id, kind=kind,
                args=args, human_token=token)))
            response = json.loads(await ws.recv())
            assert response["receipt"]["state"] == "completed"
            if operation_id == "human-input-view":
                screenshot = actual["state"] / "positive-human-input.png"
                screenshot.write_bytes(base64.b64decode(response["receipt"]["result"]["png_base64"], validate=True))
                record_property("positive_screenshot", str(screenshot))
        await wait_event(actual, {"type": "text", "value": "actual human input"})
        await wait_event(actual, {"type": "click"})
        await client.control("close", fence=human, command_id="close-human", actor_id="fixture-human")
        returned = await client.control("giveback", fence=human, command_id="return", actor_id="fixture-human")
        new_auto = returned["control"]["fence"]
        assert new_auto["epoch"] == auto["epoch"] + 2 and returned["fresh_observation_required"]
        await ws.send(json.dumps(client._operation(fence=human, operation_id="late-ws", kind="text",
            args={"text": "must never arrive"}, human_token=token)))
        denied = json.loads(await ws.recv())
        assert denied["error"]["code"] in {"BROWSER_FENCE_CHANGED", "BROWSER_CONTROL_HELD"}
    # The old socket's finally cannot close the successor automation epoch.
    assert (await client.status())["control"]["admission"] == "open"
    with pytest.raises(BrowserResourceError, match="BROWSER_OBSERVATION_REQUIRED"):
        await client.operate(fence=new_auto, operation_id="stale-frame", kind="text", args={"text": "bad"},
            observation_id=before["observation_id"])
    fresh = await capture(actual, new_auto, "after-giveback")
    assert fresh["fence"] == new_auto and fresh["eligible"] and fresh["url"] == actual["page_url"] + "/a"
    result = await client.operate(fence=new_auto, operation_id="automation-again", kind="mouse",
        args={"x": 400, "y": 200, "button": "left"}, observation_id=fresh["observation_id"])
    assert result["receipt"]["state"] == "completed"
    assert all("must never" not in event.get("value", "") for event in actual["events"])


async def test_real_browser_duplicate_input_and_command_replay_do_not_repeat(actual):
    auto = await navigate(actual)
    first = await takeover(actual, auto)
    replay = await actual["client"].control("takeover", fence=auto, command_id="takeover", actor_id="fixture-human",
        next_owner_id="fixture-human", ttl_seconds=60)
    assert replay["command_receipt"] == first["command_receipt"] and replay["human_token"] == first["human_token"]
    fence, token = first["control"]["fence"], first["human_token"]
    params = {"fence": fence, "operation_id": "one-click", "kind": "mouse",
        "args": {"x": 90, "y": 130, "button": "left"}, "human_token": token}
    original = await actual["client"].operate(**params)
    repeated = await actual["client"].operate(**params)
    assert original["receipt"] == repeated["receipt"]
    await wait_event(actual, {"type": "click"})
    assert actual["events"].count({"type": "click"}) == 1
    with pytest.raises(BrowserResourceError, match="BROWSER_OPERATION_CONFLICT"):
        await actual["client"].operate(**{**params, "args": {"x": 100, "y": 130, "button": "left"}})
    assert (await actual["client"].operation_receipt("one-click"))["receipt"] == original["receipt"]


async def test_late_prepared_http_rejected_and_actual_inflight_navigation_blocks_grant(actual):
    auto = (await actual["client"].status())["control"]["fence"]
    observation = await capture(actual, auto)
    running = asyncio.create_task(actual["client"].operate(fence=auto, operation_id="slow-navigation", kind="navigate",
        args={"url": actual["page_url"] + "/slow"}, observation_id=observation["observation_id"]))
    await asyncio.wait_for(actual["slow_started"].wait(), 2)
    await actual["client"].control("close", fence=auto, command_id="close-inflight", actor_id="fixture-human")
    with pytest.raises(BrowserResourceError, match="BROWSER_NOT_DRAINED"):
        await actual["client"].control("takeover", fence=auto, command_id="takeover", actor_id="fixture-human",
            next_owner_id="fixture-human", ttl_seconds=60)
    with pytest.raises(BrowserResourceError, match="BROWSER_CONTROL_HELD"):
        await actual["client"].operate(fence=auto, operation_id="prepared-before-close", kind="text",
            args={"text": "late"}, observation_id=observation["observation_id"])
    actual["slow_release"].set()
    assert (await running)["receipt"]["state"] == "completed"
    grant = await actual["client"].control("takeover", fence=auto, command_id="takeover", actor_id="fixture-human",
        next_owner_id="fixture-human", ttl_seconds=60)
    assert grant["control"]["fence"]["owner_kind"] == "human"


async def test_close_cancels_only_admitted_unsent_frames_behind_real_browser_io(actual):
    client = actual["client"]
    auto = (await client.status())["control"]["fence"]
    grant = await takeover(actual, auto)
    fence, token = grant["control"]["fence"], grant["human_token"]
    running = asyncio.create_task(client.operate(fence=fence, operation_id="human-slow", kind="navigate",
        args={"url": actual["page_url"] + "/slow"}, human_token=token))
    await asyncio.wait_for(actual["slow_started"].wait(), 2)
    queued = asyncio.create_task(client.operate(fence=fence, operation_id="queued-text", kind="text",
        args={"text": "must not reach the pipe"}, human_token=token))
    async with asyncio.timeout(2):
        while {"id": "queued-text", "state": "admitted"} not in (await client.status())["blocking_operations"]:
            await asyncio.sleep(.01)
    await client.control("close", fence=fence, command_id="close-queue", actor_id="fixture-human")
    with pytest.raises(BrowserResourceError, match="BROWSER_NOT_DRAINED"):
        await client.control("giveback", fence=fence, command_id="giveback-queue", actor_id="fixture-human")
    actual["slow_release"].set()
    assert (await running)["receipt"]["state"] == "completed"
    canceled = (await queued)["receipt"]
    assert canceled["state"] == "canceled" and canceled["result"]["reason"] == "not_dispatched"
    result = await client.control("giveback", fence=fence, command_id="giveback-queue", actor_id="fixture-human")
    assert result["control"]["fence"]["epoch"] == 3


async def test_real_timeout_stays_unknown_after_late_response_and_runtime_restart(actual):
    client = actual["client"]
    auto = (await client.status())["control"]["fence"]
    observation = await capture(actual, auto)
    response = await client.operate(fence=auto, operation_id="unknown-navigation", kind="navigate",
        args={"url": actual["page_url"] + "/slow"}, observation_id=observation["observation_id"])
    assert response["receipt"]["state"] == "unknown"
    actual["slow_release"].set()
    await asyncio.sleep(.05)
    assert (await client.operation_receipt("unknown-navigation"))["receipt"] == response["receipt"]
    with pytest.raises(BrowserResourceError, match="BROWSER_NOT_DRAINED"):
        await client.control("takeover", fence=auto, command_id="cannot-grant", actor_id="fixture-human",
            next_owner_id="fixture-human", ttl_seconds=60)
    old = dict(actual["journal"].identity)
    await actual["supervisor"].stop()
    restarted = BrowserJournal(actual["state"] / "control", old["resource_id"], "fixture-workspace")
    try:
        assert restarted.identity["runtime_id"] != old["runtime_id"]
        assert restarted.identity["journal_id"] == old["journal_id"]
        assert restarted.status(False)["control"]["admission"] == "closed"
        assert restarted.receipt("operation", "unknown-navigation") == response["receipt"]
    finally:
        restarted.release_lock()


async def test_expired_human_heartbeat_and_old_token_never_reopen(actual):
    auto = (await actual["client"].status())["control"]["fence"]
    grant = await takeover(actual, auto, ttl=1)
    human, token = grant["control"]["fence"], grant["human_token"]
    async with actual["client"].human_socket(fence=human, human_token=token) as ws:
        refused = json.loads(await asyncio.wait_for(ws.recv(), 3))
        assert refused["error"]["code"] == "BROWSER_CONTROL_HELD"
    assert (await actual["client"].status())["control"]["status"] == "hold"
    with pytest.raises(BrowserResourceError, match="BROWSER_CONTROL_HELD"):
        await actual["client"].control("heartbeat", fence=human, command_id="late-heartbeat", actor_id="fixture-human",
            ttl_seconds=60, human_token=token)
    with pytest.raises(BrowserResourceError):
        await actual["client"].operate(fence=human, operation_id="late-token", kind="capture", human_token=token)
    assert (await actual["client"].status())["control"]["fence"] == human
    returned = await actual["client"].control("giveback", fence=human, command_id="explicit-return", actor_id="fixture-human")
    assert returned["control"]["fence"]["owner_kind"] == "automation" and returned["fresh_observation_required"]


async def test_ws_disconnect_is_durable_hold_and_wrong_token_cannot_close_owner(actual):
    auto = (await actual["client"].status())["control"]["fence"]
    grant = await takeover(actual, auto)
    fence, token = grant["control"]["fence"], grant["human_token"]
    with pytest.raises(BrowserResourceError, match="BROWSER_TOKEN_INVALID"):
        async with actual["client"].human_socket(fence=fence, human_token="incorrect"):
            pytest.fail("Invalid token opened a stream")
    assert (await actual["client"].status())["control"]["admission"] == "open"
    async with actual["client"].human_socket(fence=fence, human_token=token):
        pass
    async with asyncio.timeout(2):
        while (await actual["client"].status())["control"]["status"] != "hold":
            await asyncio.sleep(.01)
    with pytest.raises(BrowserResourceError):
        await actual["client"].operate(fence=fence, operation_id="after-disconnect", kind="capture", human_token=token)


async def test_actual_navigation_back_reload_and_no_generic_routes(actual):
    auto = await navigate(actual, "/a")
    await navigate(actual, "/b")
    for key, kind in [("back", "back"), ("reload", "reload")]:
        shot = await capture(actual, auto, key + "-before")
        result = await actual["client"].operate(fence=auto, operation_id=key, kind=kind,
            observation_id=shot["observation_id"])
        assert result["receipt"]["state"] == "completed"
    assert (await capture(actual, auto, "after-back"))["url"] == actual["page_url"] + "/a"
    async with httpx.AsyncClient(base_url=actual["url"], trust_env=False) as client:
        for path in ["/execute", "/execute_stream", "/terminal", "/dev-browser/ws", "/proxy/9222/json", "/json/version", "/ticket"]:
            response = await client.post(path, headers={"X-API-Key": KEY}, json={"command": "never"})
            assert response.status_code == 404
        assert (await client.get("/v1/status")).status_code == 403
    argv = Path(f"/proc/{actual['pipe'].process.pid}/cmdline").read_bytes().split(b"\0")
    assert b"--remote-debugging-pipe" in argv
    assert not any(arg.startswith(b"--remote-debugging-port") for arg in argv)


async def test_unprivileged_browser_identity_cannot_read_or_modify_control(actual, record_property):
    uid = actual["pipe"].uid
    pid = actual["pipe"].process.pid
    status = Path(f"/proc/{pid}/status").read_text()
    assert f"Uid:\t{uid}\t{uid}\t{uid}\t{uid}" in status and uid != os.getuid()
    parent_pid = os.getpid()
    paths = [str(actual["journal"].path), f"/proc/{parent_pid}/environ",
             f"/proc/{parent_pid}/fd/{actual['pipe']._write.fileno()}"]
    script = """import os,json,ctypes
os.setgroups([]);os.setgid(UID);os.setuid(UID)
ctypes.CDLL(None).prctl(38,1,0,0,0)
results=[]
for path in PATHS:
 try:
  fd=os.open(path,os.O_RDONLY);os.close(fd);results.append('readable')
 except PermissionError:results.append('denied')
try:
 fd=os.open(PATHS[0],os.O_WRONLY);os.close(fd);write='allowed'
except PermissionError:write='denied'
try:os.kill(PARENT,0);kill='allowed'
except PermissionError:kill='denied'
print(json.dumps({'reads':results,'journal_write_permission':write,'signal_permission':kill,'uid':os.getuid()}))
""".replace("UID", str(uid)).replace("PATHS", repr(paths)).replace("PARENT", str(parent_pid))
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", script, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env={"PATH": "/usr/bin:/bin"})
    output, error = await process.communicate()
    assert process.returncode == 0, error.decode()
    proof = json.loads(output)
    assert proof == {"reads": ["denied"] * 3, "journal_write_permission": "denied", "signal_permission": "denied", "uid": uid}
    record_property("unprivileged_control_probe", json.dumps(proof, sort_keys=True))


async def test_real_supervisor_process_restart_preserves_receipt_but_never_restores_grant(actual, record_property):
    state = actual["state"] / "second-supervisor"
    state.mkdir(mode=0o755)
    resource_id = uuid4().hex * 2
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    args = [sys.executable, str(MODULES / "browser_resource.py"), "--state-dir", str(state),
        "--resource-id", resource_id, "--automation-owner", "fixture-workspace", "--chromium",
        os.environ.get("BROWSER_RESOURCE_CHROMIUM", "/usr/lib/chromium/chromium"),
        "--browser-uid", str(actual["pipe"].uid), "--browser-gid", str(actual["pipe"].gid), "--port", str(port)]
    args.extend(["--isolation", actual["pipe"].isolation])
    if actual["pipe"].fixture_no_sandbox:
        args.append("--fixture-no-sandbox")
    log = open(state / "supervisor.log", "ab", buffering=0)

    async def start():
        process = await asyncio.create_subprocess_exec(*args, stdout=log, stderr=log,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "BROWSER_RESOURCE_API_KEY": KEY})
        try:
            async with asyncio.timeout(8):
                while True:
                    if process.returncode is not None:
                        pytest.fail("New fixture supervisor failed: " + (state / "supervisor.log").read_text()[-1000:])
                    try:
                        status = await BrowserResourceClient(url, KEY).status()
                        return process, status
                    except httpx.ConnectError:
                        await asyncio.sleep(.03)
        except BaseException:
            if process.returncode is None:
                process.terminate()
                await process.wait()
            raise

    async def stop(process):
        if process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), 8)

    first = second = None
    try:
        first, initial = await start()
        original = BrowserResourceClient(url, KEY, initial["identity"])
        fence = initial["control"]["fence"]
        await original.control("close", fence=fence, command_id="close-original", actor_id="fixture-human")
        granted = await original.control("takeover", fence=fence, command_id="durable-grant", actor_id="fixture-human",
            next_owner_id="fixture-human", ttl_seconds=60)
        await original.operate(fence=granted["control"]["fence"], operation_id="subprocess-capture", kind="capture",
            human_token=granted["human_token"])
        await stop(first)
        second, recovered = await start()
        assert recovered["identity"]["profile_id"] == initial["identity"]["profile_id"]
        assert recovered["identity"]["journal_id"] == initial["identity"]["journal_id"]
        assert recovered["identity"]["runtime_id"] != initial["identity"]["runtime_id"]
        assert recovered["control"]["admission"] == "closed" and recovered["control"]["status"] == "hold"
        assert (await original.control_receipt("durable-grant"))["command_receipt"] == granted["command_receipt"]
        with pytest.raises(BrowserResourceError, match="BROWSER_IDENTITY_CHANGED"):
            await original.operate(fence=granted["control"]["fence"], operation_id="stale-after-restart", kind="capture",
                human_token=granted["human_token"])
        record_property("supervisor_restart", json.dumps({"first_pid": first.pid, "second_pid": second.pid,
            "old_runtime": initial["identity"]["runtime_id"], "new_runtime": recovered["identity"]["runtime_id"]}))
    finally:
        for process in (first, second):
            if process is not None:
                await stop(process)
        log.close()


async def test_actual_startup_rejects_unprotected_control_storage(actual):
    from browser_isolation import BrowserIsolationError

    state = actual["state"] / "rejected-startup"
    state.mkdir(mode=0o755)
    journal = BrowserJournal(state / "control", uuid4().hex * 2, "fixture-workspace")
    journal.path.chmod(0o644)
    home = state / "browser"
    home.mkdir(mode=0o700)
    pipe = BrowserPipe(actual["pipe"].binary, home / "profile", uid=actual["pipe"].uid,
        gid=actual["pipe"].gid, isolation="container_uid")
    supervisor = BrowserSupervisor(journal, pipe)
    try:
        with pytest.raises(BrowserIsolationError, match="verification failed"):
            await supervisor.start()
        assert not pipe.live and not supervisor.ready
        assert journal.status(False)["control"]["status"] == "hold"
    finally:
        await supervisor.stop()
