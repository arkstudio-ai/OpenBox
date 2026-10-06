"""Real SQL/private manager authority; only the existing guest HTTP is fake.

The fixture reports a manufactured guest isolation proof to exercise protocol
validation. It is not a Linux UID/mount or real Wuying acceptance certificate.

This actor runtime is dormant: only the assistant's own main conversation is
private for runtime purposes, so it is the only audience these fixtures can
pin. Sessions the assistant delegates (private visibility, isolated memory)
use the shared workspace runtime and have no private runtime audience.
"""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from assistant.service import ensure_main_session
from core.config import OpenBoxConfig, PrivateRuntimeConfig
from db.base import get_db_session
from db.models.private_runtime import PrivateRuntimeBinding
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from sandbox import private_runtime, private_wuying
from sandbox.manager import SandboxManager
from sandbox.private_runtime import PrivateRuntimeError
from session.session import create_session
from tests.offline_wuying import install_wuying_offline_guard
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


@pytest.fixture
async def wuying_world(monkeypatch):
    install_wuying_offline_guard(monkeypatch)
    owner, peer, workspace = await accounts()
    # The assistant main conversation is the only remaining private audience.
    session = await ensure_main_session(user_id=owner, workspace_id=workspace)
    peer_session = await ensure_main_session(user_id=peer, workspace_id=workspace)
    shared = await create_session(user_id=owner, workspace_id=workspace)
    # Delegated work keeps its private visibility and isolated memory, but
    # runs on the shared workspace runtime.
    delegated = await create_session(user_id=owner, workspace_id=workspace, visibility="private",
                                     memory_policy="assistant_isolated")
    config = OpenBoxConfig(sandbox_provider="wuying", wuying_routing="shared", wuying_mode="shared",
        wuying_endpoint="http://127.0.0.1:19801", wuying_desktop_id="ecd-private-actor-fixture",
        wuying_region_id="cn-test", wuying_api_key="fixture-existing-action-server-key",
        private_runtime=PrivateRuntimeConfig(enabled=True, allowed_user_ids=[owner, peer]))
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr(private_wuying, "get_config", lambda: config)
    world = SimpleNamespace(owner=owner, peer=peer, workspace=workspace, session=session,
        peer_session=peer_session, shared=shared, delegated=delegated, config=config, proofs={}, sent=[],
        after_response=None, remote_handler=None)
    for index, actor in enumerate((owner, peer)):
        scope = private_runtime.PrivateSessionScope(session.id if actor == owner else peer_session.id, actor, workspace)
        identity = private_wuying.scope_id(scope)
        public = {"version": 1, "protocol": private_wuying.PROTOCOL, "id": "actor_" + identity[:24],
            "attempt_id": "attempt_" + identity[:24], "scope_id": identity, "workspace_id": workspace,
            "actor_user_id": actor, "desktop_id": config.wuying_desktop_id, "region_id": config.wuying_region_id,
            "executor_uid": 2200 + index * 2, "browser_uid": 2201 + index * 2,
            "browser_resource_id": private_wuying._digest({"browser": identity}),
            "identity_digest": private_wuying._digest({"guest": identity}), "isolation_mode": "guest_uid_mount"}
        world.proofs[identity] = {**public, "isolation": {"mode": "guest_uid_mount", "verification": "passed",
            "checks": {name: True for name in private_wuying._CHECKS}}}

    async def remote(request):
        assert request.headers["X-API-Key"] == config.wuying_api_key
        scope = request.headers[private_wuying.SCOPE_HEADER]
        proof = deepcopy(world.proofs[scope])
        world.sent.append((request.method, request.url.path, scope))
        if request.url.path == "/private-runtime/resolve":
            result = proof
        elif request.url.path == "/private-runtime/" + proof["id"] + "/identity":
            if request.headers[private_wuying.ATTEMPT_HEADER] != proof["attempt_id"]:
                return httpx.Response(409, json={"detail": "Original guest attempt changed"})
            result = proof
        elif world.remote_handler is not None:
            result = await world.remote_handler(request)
        else:
            raise AssertionError("Unexpected existing-guest request: " + request.url.path)
        if world.after_response:
            await world.after_response()
        return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)

    world.transport = httpx.MockTransport(remote)
    original = httpx.AsyncClient
    monkeypatch.setattr(private_wuying, "httpx", SimpleNamespace(**{
        **vars(httpx), "AsyncClient": lambda **kwargs: original(**{**kwargs, "transport": world.transport})}))
    return world


async def _resolve(w, **changes):
    return await private_runtime.resolve_private_runtime(session_id=w.session.id, user_id=w.owner,
        workspace_id=w.workspace, **changes)


async def _validate(w, route):
    return await private_runtime.validate_private_runtime(route, session_id=w.session.id,
        user_id=w.owner, workspace_id=w.workspace, kind=route.kind)


async def test_wuying_binding_uses_existing_guest_and_survives_engine_reopen(wuying_world):
    from db.base import close_engine, get_engine, init_engine
    w = wuying_world
    route = await _resolve(w)
    assert route.provider == "private_wuying_v1" and route.desktop_id == w.config.wuying_desktop_id
    assert route.base_url == w.config.wuying_endpoint + "/private-runtime/" + route.guest_binding_id
    assert route.provider_identity["guest_binding"]["scope_id"] == route.scope_id
    assert route.api_key not in repr(route)
    url = get_engine().url.render_as_string(hide_password=False)
    await close_engine()
    init_engine(url)
    assert await _resolve(w, create=False) == await _validate(w, route) == route
    async with get_db_session() as db:
        rows = list((await db.scalars(select(PrivateRuntimeBinding).where(
            PrivateRuntimeBinding.workspace_id == w.workspace))).all())
        assert len(rows) == 1 and rows[0].data_volume is None and rows[0].workspace_volume is None
        assert rows[0].api_key_ciphertext == "" and rows[0].provider == "private_wuying_v1"
    assert {method for method, _, _ in w.sent} == {"GET"}


@pytest.mark.parametrize("mode", ["docker", "kubernetes", "disabled", "unlisted", "caller_adapter", "peer", "ordinary",
                                  "delegated", "retired_browser"])
async def test_private_execution_cannot_select_another_provider_or_audience(wuying_world, mode):
    w = wuying_world
    args = {"session_id": w.session.id, "user_id": w.owner, "workspace_id": w.workspace}
    if mode in {"docker", "kubernetes"}: w.config.sandbox_provider = mode
    if mode == "disabled": w.config.private_runtime.enabled = False
    if mode == "unlisted": w.config.private_runtime.allowed_user_ids = []
    if mode == "caller_adapter": args["docker"] = object()
    if mode == "peer": args["user_id"] = w.peer
    if mode == "ordinary": args["session_id"] = w.shared.id
    if mode == "delegated": args["session_id"] = w.delegated.id
    if mode == "retired_browser": args["kind"] = "browser_profile"
    with pytest.raises(PrivateRuntimeError):
        await private_runtime.resolve_private_runtime(**args)
    assert w.sent == []
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(PrivateRuntimeBinding).where(
            PrivateRuntimeBinding.workspace_id == w.workspace)) == 0


@pytest.mark.parametrize("change", ["proof", "guest_attempt", "guest_uid", "endpoint", "key", "provider", "membership", "response_revoke"])
async def test_original_guest_and_current_scope_are_revalidated(wuying_world, change):
    w = wuying_world
    route = await _resolve(w)
    before = len(w.sent)
    if change == "proof": w.proofs[route.scope_id]["isolation"]["checks"]["private_mount_namespace"] = False
    if change == "guest_attempt": w.proofs[route.scope_id]["attempt_id"] = "replacement_attempt"
    if change == "guest_uid": w.proofs[route.scope_id]["executor_uid"] += 100
    if change == "endpoint": w.config.wuying_endpoint = "http://127.0.0.1:19802"
    if change == "key": w.config.wuying_api_key = "replacement-key"
    if change == "provider": w.config.sandbox_provider = "docker"
    async def revoke():
        async with get_db_session() as db:
            (await db.get(WorkspaceMember, (w.workspace, w.owner))).status = "removed"
    if change == "membership": await revoke()
    if change == "response_revoke": w.after_response = revoke
    with pytest.raises(PrivateRuntimeError):
        await _validate(w, route)
    assert len(w.sent) == before + (change in {"proof", "guest_attempt", "guest_uid", "response_revoke"})


async def test_failed_guest_proof_does_not_pin(wuying_world):
    w = wuying_world
    for proof in w.proofs.values():
        proof["isolation"]["checks"]["legacy_executor_is_unprivileged"] = False
    with pytest.raises(PrivateRuntimeError, match="isolation"):
        await _resolve(w)
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(PrivateRuntimeBinding).where(
            PrivateRuntimeBinding.workspace_id == w.workspace)) == 0
    assert all(method == "GET" for method, _, _ in w.sent)
