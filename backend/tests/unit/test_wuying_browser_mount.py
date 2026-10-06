"""The original Action Server's private actor prefix over real HTTP and a journal.

The private browser mount this suite used to cover was removed. What remains
is the dormant private actor runtime: only the assistant's own main
conversation is private (sandbox.privacy), and runtime preparation refuses
that conversation, so no Session can prepare this actor runtime any more. The
first case pins that refusal before any guest IO. The retained end-to-end
cases are skipped until the follow-up removes the module; their fixtures stay
importable (test_private_runtime_context_transaction uses them).

The guest kernel boundary is an explicit local fixture. This suite never
starts Docker, a cloud SDK or a user's installed browser.
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

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "container"))
from private_actor import PrivateActorError
from tests.unit.test_action_server_desktop_lease import server
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401


KEY = "loopback-only-fixture-service-key"
SCOPE = "a" * 64
DORMANT = pytest.mark.skip(reason="Dormant private actor runtime: no Session can prepare it under the current "
    "privacy rule (only the assistant main conversation is private, and runtime preparation refuses it). "
    "Retained until the follow-up removes sandbox.private_runtime/private_wuying.")


@dataclass(frozen=True)
class Binding:
    id: str
    attempt_id: str
    scope_id: str
    browser_state: Path
    browser_home: Path
    workspace_dir: Path
    # Inert wuying_actor_uid_mount_v1 identity fields; no browser is served.
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
    monkeypatch.setattr(server, "SESSION_API_KEY", KEY)
    monkeypatch.setattr(server, "_resource_gate", None)
    monkeypatch.setattr(server, "_actor_resource_gates", {})
    monkeypatch.setattr("private_actor.registry", lambda: registry)
    async with listening(server.app) as url:
        yield SimpleNamespace(binding=binding, registry=registry, url=url)


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


async def test_no_session_can_prepare_the_dormant_actor_runtime(actor_runtime):
    """A retained private route is refused before any guest request or lease.

    The delegated task Session runs on the shared runtime now, so the actor
    audience refuses it. The assistant conversation is private, but runtime
    preparation never runs for it.
    """
    from sqlalchemy import func, select
    from agent.driver import bind_current_lease, reserve_run, reset_current_lease
    from assistant.policy import AssistantError
    from db.base import get_db_session
    from db.models.resource_control import ResourceControlLease
    from sandbox.runtime_operation import ensure_private_runtime_control, runtime_context
    fixture = actor_runtime
    for prepare in (runtime_context, ensure_private_runtime_control):
        with pytest.raises(AssistantError) as refused:
            await prepare(fixture.client, fixture.lease)
        assert refused.value.code == "RESOURCE_CONTROL_HELD"
    main = await reserve_run(fixture.args["main_id"], fixture.args["user_id"])
    token = bind_current_lease(main)
    try:
        for prepare in (runtime_context, ensure_private_runtime_control):
            with pytest.raises(AssistantError) as refused:
                await prepare(fixture.client, main)
            assert refused.value.code == "RESOURCE_CONTROL_HELD"
    finally:
        reset_current_lease(token)
        await main.release(session_status="idle")
    assert fixture.mounted.registry.proofs == 0 and server._actor_resource_gates == {}
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(ResourceControlLease).where(
            ResourceControlLease.workspace_id == fixture.args["workspace_id"])) == 0


@DORMANT
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


@DORMANT
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


@DORMANT
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


@DORMANT
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


@DORMANT
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
