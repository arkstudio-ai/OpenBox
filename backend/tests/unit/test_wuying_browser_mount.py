"""Original Action Server mount over real HTTP/WS and a persistent journal.

The guest kernel/Chromium boundaries are explicit local fixtures here. Actual
UID, namespace and Chromium validation belongs to the configured Wuying guest;
this suite never starts Docker, a cloud SDK or a user's installed browser.
"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
import hashlib

import httpx
import pytest
import uvicorn
import websockets

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "container"))
import browser_resource as browser
from private_actor import PrivateActorError
from sandbox.browser_resource_client import BrowserResourceClient, BrowserResourceError
from tests.unit.test_action_server_desktop_lease import server
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401


KEY = "loopback-only-fixture-service-key"
SCOPE = "a" * 64


@dataclass(frozen=True)
class Binding:
    id: str
    attempt_id: str
    scope_id: str
    browser_state: Path
    browser_home: Path
    workspace_dir: Path
    browser_resource_id: str = "b" * 64
    workspace_id: str = "fixture-workspace"
    browser_user: str = "fixture-browser"
    executor_user: str = "fixture-executor"

    @property
    def identity_digest(self):
        return hashlib.sha256(str(self.browser_state).encode()).hexdigest()

    def public(self):
        return {"id": self.id, "attempt_id": self.attempt_id, "scope_id": self.scope_id,
                "workspace_id": self.workspace_id, "browser_resource_id": self.browser_resource_id}


class Registry:
    def __init__(self, binding):
        self.binding, self.proofs = binding, 0

    def lookup(self, binding_id, scope_id=None, attempt_id=None):
        if self.binding is None or (binding_id, scope_id, attempt_id) != (
                self.binding.id, self.binding.scope_id, self.binding.attempt_id):
            raise PrivateActorError("Original fixture binding revoked")
        return self.binding

    def proof(self, binding):
        assert self.lookup(binding.id, binding.scope_id, binding.attempt_id) == binding
        self.proofs += 1
        return {"fixture": "OS probe replaced; no guest isolation claim"}


class FixturePipe:
    def __init__(self, binary, profile, **kwargs):
        self.binary, self.profile = binary, profile
        self.guest_binding = kwargs["guest_binding"]
        self.uid, self.gid = kwargs["uid"], kwargs["gid"]
        self.isolation, self.fixture_no_sandbox = "wuying_guest_uid", False
        self.live = False
        self.calls = []
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.slow = False

    async def start(self):
        self.live = True

    async def stop(self):
        self.live = False

    async def execute(self, kind, args):
        self.calls.append((kind, args))
        if self.slow:
            self.entered.set()
            await self.release.wait()
        if kind == "capture":
            return {"png_base64": "fixture-pixels", "url": "http://127.0.0.1/page",
                    "frame_id": "fixture-frame", "loader_id": "fixture-loader", "sha256": "c" * 64,
                    "width": 1024, "height": 768}
        return {"delivered": True}


@asynccontextmanager
async def listening(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    runner = uvicorn.Server(uvicorn.Config(app, lifespan="off", access_log=False, log_level="error", ws="websockets"))
    task = asyncio.create_task(runner.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(5):
            while not runner.started:
                if task.done():
                    await task
                await asyncio.sleep(.01)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        runner.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()


@pytest.fixture
async def mounted(tmp_path, monkeypatch):
    binding = Binding("fixture_binding", "fixture_attempt", SCOPE, tmp_path / "state", tmp_path / "home", tmp_path / "workspace")
    for path in (binding.browser_state, binding.browser_home, binding.workspace_dir):
        path.mkdir()
    registry = Registry(binding)
    monkeypatch.setattr(browser, "BrowserPipe", FixturePipe)
    monkeypatch.setattr(browser.pwd, "getpwnam", lambda name: SimpleNamespace(pw_uid=12345, pw_gid=12345))

    async def local_os_boundary(journal, pipe):
        return {"mode": "wuying_guest_uid", "verification": "passed", "checks": {"explicit_local_fixture": True}}

    monkeypatch.setattr(browser, "verify_isolation", local_os_boundary)
    mount = server._wuying_browsers
    assert not mount._entries
    monkeypatch.setattr(mount, "get_registry", lambda: registry)
    monkeypatch.setattr(server, "SESSION_API_KEY", KEY)
    monkeypatch.setattr(server, "_resource_gate", None)
    monkeypatch.setattr(server, "_actor_resource_gates", {})
    monkeypatch.setattr("private_actor.registry", lambda: registry)
    async with listening(server.app) as url:
        client = BrowserResourceClient(url + f"/private-runtime/{binding.id}/browser", KEY,
            private_scope=SCOPE, private_attempt=binding.attempt_id)
        try:
            yield SimpleNamespace(binding=binding, registry=registry, mount=mount, url=url, client=client)
        finally:
            for entry in mount._entries.values():
                entry[1].pipe.release.set()
            await mount.stop()


async def prepared(fixture):
    result = await fixture.client.prepare()
    fixture.client.identity = result["identity"]
    return result


async def test_status_is_read_only_and_prepare_is_exact_idempotent(mounted):
    with pytest.raises(BrowserResourceError, match="BROWSER_PREPARATION_REQUIRED"):
        await mounted.client.status()
    assert mounted.registry.proofs == 0 and not mounted.mount._entries
    one = await prepared(mounted)
    two = await mounted.client.prepare()
    assert one["identity"] == two["identity"]
    assert one["guest_binding"] == mounted.binding.public()
    assert mounted.registry.proofs == 1
    assert (await mounted.client.status())["identity"] == one["identity"]


async def test_real_http_ws_human_input_giveback_and_old_token_refusal(mounted):
    ready = await prepared(mounted)
    client, auto = mounted.client, ready["control"]["fence"]
    await client.operate(fence=auto, operation_id="original-capture", kind="capture")
    await client.control("close", fence=auto, command_id="close-auto", actor_id="human")
    grant = await client.control("takeover", fence=auto, command_id="grant-human", actor_id="human",
        next_owner_id="human", ttl_seconds=60)
    human, token = grant["control"]["fence"], grant["human_token"]
    async with client.human_socket(fence=human, human_token=token) as ws:
        import json
        await ws.send(json.dumps(client._operation(fence=human, operation_id="human-text", kind="text",
            args={"text": "local protocol proof"}, human_token=token)))
        assert json.loads(await ws.recv())["receipt"]["state"] == "completed"
        await client.control("close", fence=human, command_id="close-human", actor_id="human")
        returned = await client.control("giveback", fence=human, command_id="return", actor_id="human")
    with pytest.raises(BrowserResourceError):
        await client.operate(fence=human, operation_id="late-human", kind="text", args={"text": "late"}, human_token=token)
    with pytest.raises(BrowserResourceError, match="BROWSER_OBSERVATION_REQUIRED"):
        await client.operate(fence=returned["control"]["fence"], operation_id="stale-image", kind="text", args={"text": "old"})
    fresh = await client.operate(fence=returned["control"]["fence"], operation_id="fresh-image", kind="capture")
    assert fresh["receipt"]["result"]["observation"]["eligible"] is True


async def test_wrong_scope_and_replaced_attempt_never_follow_successor(mounted):
    old = await prepared(mounted)
    wrong = BrowserResourceClient(mounted.client.base_url, KEY, private_scope="f" * 64, private_attempt=mounted.binding.attempt_id)
    with pytest.raises(BrowserResourceError, match="BROWSER_GUEST_UNAVAILABLE"):
        await wrong.status()
    mounted.registry.binding = replace(mounted.binding, attempt_id="replacement_attempt")
    with pytest.raises(BrowserResourceError, match="BROWSER_GUEST_UNAVAILABLE"):
        await mounted.client.prepare()
    entry = mounted.mount._entries[mounted.binding.id]
    assert entry[1].journal.identity == old["identity"] and mounted.registry.proofs == 1
    assert entry[1].pipe.calls == []


async def test_binding_revoked_after_queue_admission_prevents_pipe_dispatch(mounted):
    ready = await prepared(mounted)
    supervisor = mounted.mount._entries[mounted.binding.id][1]
    supervisor.pipe.slow = True
    first = asyncio.create_task(mounted.client.operate(fence=ready["control"]["fence"], operation_id="running", kind="capture"))
    await supervisor.pipe.entered.wait()
    second = asyncio.create_task(mounted.client.operate(fence=ready["control"]["fence"], operation_id="queued", kind="capture"))
    async with asyncio.timeout(3):
        while True:
            with supervisor.journal.transaction() as db:
                if db.execute("SELECT 1 FROM operations WHERE id='queued' AND state='admitted'").fetchone():
                    break
            await asyncio.sleep(.01)
    mounted.registry.binding = None
    supervisor.pipe.release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(value, BrowserResourceError) for value in results)
    assert len(supervisor.pipe.calls) == 1
    assert supervisor.journal.status(True)["control"]["admission"] == "closed"


async def test_established_ws_revocation_closes_without_another_input(mounted):
    ready = await prepared(mounted)
    auto = ready["control"]["fence"]
    await mounted.client.control("close", fence=auto, command_id="close-auto", actor_id="human")
    grant = await mounted.client.control("takeover", fence=auto, command_id="grant", actor_id="human", next_owner_id="human", ttl_seconds=60)
    async with mounted.client.human_socket(fence=grant["control"]["fence"], human_token=grant["human_token"]) as ws:
        mounted.registry.binding = None
        with pytest.raises(websockets.exceptions.ConnectionClosed):
            await asyncio.wait_for(ws.recv(), 3)
    assert mounted.mount._entries[mounted.binding.id][1].pipe.calls == []


@pytest.mark.parametrize("suffix", ["/execute", "/terminal", "/proxy/9333", "/v1/evaluate", "/dev-browser/ws"])
async def test_generic_native_or_raw_cdp_routes_do_not_map_into_browser(mounted, suffix):
    await prepared(mounted)
    async with httpx.AsyncClient(trust_env=False) as client:
        response = await client.post(mounted.client.base_url + suffix, headers=mounted.client._headers, json={"command": "unreachable"})
    assert response.status_code == 404
    assert mounted.mount._entries[mounted.binding.id][1].pipe.calls == []


def test_mount_client_requires_original_scope_attempt_and_safe_prefix():
    with pytest.raises(ValueError, match="original private"):
        BrowserResourceClient("http://127.0.0.1:1/private-runtime/fixture_binding/browser", KEY)
    for path in ("/proxy/8000", "/private-runtime/../browser", "/private-runtime/a%2fb/browser"):
        with pytest.raises(ValueError):
            BrowserResourceClient("http://127.0.0.1:1" + path, KEY, private_scope=SCOPE, private_attempt="fixture_attempt")


@pytest.fixture
async def actor_runtime(mounted, monkeypatch):
    """Real SQL Task/Inbox/Driver and original AS, with only guest OS IO replaced."""
    from datetime import datetime, timezone
    from uuid import uuid4
    from agent.driver import bind_current_lease, reset_current_lease
    from db.base import get_db_session
    from db.models.private_runtime import PrivateRuntimeBinding
    from sandbox.private_runtime import PrivateRuntimeRoute
    from sandbox import resource_operation
    from tests.unit.test_assistant_steering import running
    args, created, lease, _ = await running()
    mounted.registry.binding = binding = replace(mounted.binding, workspace_id=args["workspace_id"])
    guest = {**binding.public(), "identity_digest": binding.identity_digest}
    endpoint = {"base_url": mounted.url, "desktop_id": "fixture-existing-ecd", "region_id": "fixture-region",
                "record_id": None, "authority_digest": "d" * 64}
    provider = {"endpoint": endpoint, "guest_binding": guest}
    stamp, binding_id = datetime.now(timezone.utc), "wpr_" + uuid4().hex
    async with get_db_session() as db:
        db.add(PrivateRuntimeBinding(id=binding_id, workspace_id=args["workspace_id"], actor_user_id=args["user_id"],
            provider="private_wuying_v1", kind="sandbox", isolation_mode="guest_uid_mount", status="ready",
            provision_phase="ready", attempt_id=binding.attempt_id, revision=1, container_id=binding_id,
            container_name="fixture-" + binding_id, workspace_volume=None, data_volume=None, volume_identities={},
            image="fixture-ecd", image_id=binding.identity_digest, host_port=1234, route_key=binding_id,
            api_key_ciphertext="unused", api_key_hash=hashlib.sha256(KEY.encode()).hexdigest(),
            physical_digest="e" * 64, provider_identity=provider, created_at=stamp, updated_at=stamp))
    route = PrivateRuntimeRoute(binding_id, "sandbox", "private_wuying_v1", args["workspace_id"], args["user_id"],
        binding.attempt_id, 1, binding_id, "fixture-" + binding_id, "fixture-ecd", binding.identity_digest, stamp,
        "127.0.0.1", 1234, binding_id, KEY, isolation_mode="guest_uid_mount",
        base_url=mounted.url + "/private-runtime/" + binding.id, scope_id=SCOPE,
        desktop_id=endpoint["desktop_id"], region_id=endpoint["region_id"], guest_binding_id=binding.id,
        guest_attempt_id=binding.attempt_id, provider_identity=provider)

    class RuntimeClient:
        private_runtime_route = route
        desktop_id = route.desktop_id
        workspace_id = route.workspace_id
        lost_reply = False
        after_bind = None

        async def request(self, method, path, **kwargs):
            async with httpx.AsyncClient(trust_env=False) as client:
                request = client.build_request(method, route.base_url + path,
                    headers={"X-API-Key": KEY, "X-OpenBox-Private-Scope": SCOPE,
                             "X-OpenBox-Private-Attempt": route.guest_attempt_id}, **kwargs)
                await resource_operation.authorize_request(self, request)
                response = await client.send(request)
                response.raise_for_status()
                await resource_operation.observe_response(self, response)
                return response.json()

        async def resource_status(self):
            return await self.request("GET", "/resource-control/status")

        async def resource_command(self, action, payload):
            result = await self.request("POST", "/resource-control/" + action, json=payload)
            if self.after_bind is not None:
                await self.after_bind()
            if self.lost_reply:
                self.lost_reply = False
                raise httpx.ReadTimeout("Fixture lost original bind reply")
            return result

    async def guest_catalogue(operation, env):
        assert operation == "actor_catalogue"
        return {"generation": "fixture-generation", "skills": [], "mcp_tools": []}

    monkeypatch.setattr(server, "file_json_operation", guest_catalogue)
    monkeypatch.setattr(server, "_exec_env", lambda: {})  # Linux guest identity is the explicit OS boundary.
    token = bind_current_lease(lease)
    try:
        yield SimpleNamespace(client=RuntimeClient(), lease=lease, args=args, route=route, mounted=mounted)
    finally:
        reset_current_lease(token)
        await lease.release(session_status="idle")


async def test_private_runtime_real_sql_and_actor_journal_without_cloud_desktop(actor_runtime):
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.cloud_desktop import CloudDesktop
    from db.models.external_effect import ExternalEffect
    from sandbox.runtime_operation import run_runtime_operation
    fixture = actor_runtime
    with pytest.raises(httpx.HTTPStatusError) as no_fence:
        await fixture.client.request("GET", "/catalog")
    assert no_fence.value.response.status_code == 423

    async def read():
        return await fixture.client.request("GET", "/catalog")
    result = await run_runtime_operation(fixture.client, session_id=fixture.lease.session_id,
        user_id=fixture.lease.user_id, stage="catalogue_read", payload={"fixed": "catalogue"}, operation=read)
    assert result["generation"] == "fixture-generation"
    async with get_db_session() as db:
        assert await db.scalar(select(CloudDesktop.id).where(CloudDesktop.workspace_id == fixture.args["workspace_id"])) is None
        effect = await db.scalar(select(ExternalEffect).where(ExternalEffect.session_id == fixture.lease.session_id))
        assert effect.state == "succeeded" and effect.resource_id is not None
        from db.models.resource_control import ResourceControlLease
        row = await db.get(ResourceControlLease, effect.resource_id)
        assert row.provider == "private_wuying_v1" and row.desktop_record_id is None
        assert row.remote_journal_id == (await fixture.client.resource_status())["journal_id"]


async def test_actor_bind_lost_response_recovers_exact_receipt_after_sql_reopen(actor_runtime):
    from db.base import close_engine, get_engine, init_engine
    from sandbox.runtime_operation import runtime_context
    fixture = actor_runtime
    fixture.client.lost_reply = True
    with pytest.raises(httpx.ReadTimeout):
        await runtime_context(fixture.client, fixture.lease)
    first = await fixture.client.resource_status()
    url = get_engine().url.render_as_string(hide_password=False)
    await close_engine()
    init_engine(url)
    _, fence, journal, _, _ = await runtime_context(fixture.client, fixture.lease)
    assert journal == first["journal_id"] and fence.resource_id == first["control"]["resource_id"]
    gate = next(iter(server._actor_resource_gates.values()))
    with gate.transaction() as db:
        assert db.execute("SELECT count(*) FROM control_commands").fetchone()[0] == 1


@pytest.mark.parametrize("change", ["revision", "revoke"])
async def test_late_bind_response_cannot_publish_replacement_or_revoked_sql_actor(actor_runtime, change):
    from db.base import get_db_session
    from db.models.private_runtime import PrivateRuntimeBinding
    from db.models.resource_control import ResourceControlLease
    from sqlalchemy import select
    from assistant.policy import AssistantError
    from sandbox.runtime_operation import runtime_context
    fixture = actor_runtime
    async def replace_before_sql_adoption():
        async with get_db_session() as db:
            row = await db.get(PrivateRuntimeBinding, fixture.route.binding_id)
            if change == "revision": row.revision += 1
            else: row.status = "blocked"
    fixture.client.after_bind = replace_before_sql_adoption
    with pytest.raises(AssistantError):
        await runtime_context(fixture.client, fixture.lease)
    async with get_db_session() as db:
        row = await db.scalar(select(ResourceControlLease).where(ResourceControlLease.workspace_id == fixture.args["workspace_id"]))
        assert row.remote_journal_id is not None and row.remote_status is None


async def test_actor_gate_rechecks_original_binding_before_late_dispatch(actor_runtime):
    from sandbox.runtime_operation import runtime_context
    from resource_gate import GateError
    from starlette.datastructures import Headers
    fixture = actor_runtime
    _, fence, journal, _, _ = await runtime_context(fixture.client, fixture.lease)
    gate = next(iter(server._actor_resource_gates.values()))
    headers = Headers({"x-openbox-resource": fence.resource_id, "x-openbox-resource-epoch": str(fence.epoch),
        "x-openbox-resource-owner": fence.owner_kind, "x-openbox-resource-owner-id": fence.owner_id,
        "x-openbox-resource-journal": journal, "x-openbox-resource-operation": "fixture_effect",
        "x-openbox-resource-step": "fixture_step"})
    admitted = gate.admit(headers, "POST", "/private-runtime/fixture_binding/execute")
    fixture.mounted.registry.binding = replace(fixture.mounted.registry.binding, attempt_id="replacement_attempt")
    with pytest.raises(PrivateActorError):
        gate.checkpoint(admitted)
    with gate.transaction() as db:
        assert db.execute("SELECT state FROM operations WHERE id='fixture_step'").fetchone()[0] == "admitted"


async def test_two_private_sessions_first_enrollment_share_one_original_resource(actor_runtime):
    from assistant import resource_control as controls
    from db.base import get_db_session
    from db.models.session import Session
    from db.models.resource_control import ResourceControlLease
    from session.internal_parts import begin_session_write
    from session.session import create_session
    from sqlalchemy import select
    fixture = actor_runtime
    other = await create_session(user_id=fixture.lease.user_id, workspace_id=fixture.args["workspace_id"], visibility="private")
    started = asyncio.Event()
    async def enroll(session_id):
        await started.wait()
        async with get_db_session() as db:
            await begin_session_write(db)
            session = await db.get(Session, session_id)
            return controls.fence_for(await controls.enroll_private_runtime_locked(db, session, fixture.route))
    one = asyncio.create_task(enroll(fixture.lease.session_id))
    two = asyncio.create_task(enroll(other.id))
    started.set()
    left, right = await asyncio.wait_for(asyncio.gather(one, two), 10)
    assert left == right
    async with get_db_session() as db:
        rows = list((await db.scalars(select(ResourceControlLease).where(ResourceControlLease.workspace_id == fixture.args["workspace_id"]))).all())
        assert len(rows) == 1 and rows[0].id == left.resource_id
