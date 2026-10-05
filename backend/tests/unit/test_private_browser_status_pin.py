"""One real finite status per validation, fresh SQL on both sides.

SQL and the browser journal/service/client are real. Guest HTTP and Chromium
are the existing explicit fixture boundaries, not physical Wuying proof.
"""
import json

import pytest

from assistant import browser_resources as service
from assistant.policy import AssistantError
from db.base import get_db_session
from db.models.browser_resource import BrowserResourceBinding
from db.models.private_runtime import PrivateRuntimeBinding
from db.models.resource_control import ResourceControlLease
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from sandbox import browser_resource_client as client_module
from sandbox.private_runtime import (
    PrivateRuntimeError, read_private_browser_pin, revalidate_private_browser_pin,
    resolve_private_runtime, validate_private_runtime,
)
from tests.unit.test_assistant_browser_resources import (
    assistant_database, private_world, browser_world, ensure,  # noqa: F401
)


async def original_binding(w):
    await ensure(w)
    async with get_db_session() as db:
        return await db.get(BrowserResourceBinding, w.resource_id)


async def test_each_browser_validation_reads_one_actual_status_and_no_redundant_identity(browser_world):
    w = browser_world
    binding = await original_binding(w)
    original_guest_requests = list(w.w.sent)
    start = len(w.requests)
    for _ in range(2):
        client, status = await service._client_status_for(binding)
        assert client.identity == binding.identity == status["identity"]
        assert status["browser_live"] is True
    assert w.requests[start:] == ["/v1/status", "/v1/status"]
    assert w.w.sent == original_guest_requests and w.pipe.calls == []
    async with get_db_session() as db:
        (await db.get(WorkspaceMember, (w.w.workspace, w.w.owner))).status = "removed"
    with pytest.raises(AssistantError):
        await service._client_status_for(binding)
    assert w.requests[start:] == ["/v1/status", "/v1/status"]


@pytest.mark.parametrize("change", ["guest", "attempt", "isolation", "membership", "session",
    "revision", "status", "schema", "source", "endpoint", "key", "allowlist"])
async def test_status_await_cannot_release_changed_guest_or_sql_authority(browser_world, monkeypatch, change):
    w = browser_world
    binding = await original_binding(w)
    original_http_client = client_module.httpx.AsyncClient
    guest_requests = list(w.w.sent)
    start = len(w.requests)

    async def changed_after_headers(response):
        assert response.request.url.path.endswith("/v1/status")
        await response.aread()
        payload = response.json()
        if change == "guest": payload["guest_binding"]["identity_digest"] = "f" * 64
        if change == "attempt": payload["guest_binding"]["attempt_id"] = "replacement-attempt"
        if change == "isolation": payload["isolation"]["checks"]["private_pipe"] = False
        response._content = json.dumps(payload).encode()
        if change == "endpoint": w.w.config.wuying_endpoint = "http://127.0.0.1:19802"
        if change == "key": w.w.config.wuying_api_key = "replacement-fixture-key"
        if change == "allowlist": w.w.config.private_runtime.allowed_user_ids = []
        async with get_db_session() as db:
            if change == "membership":
                (await db.get(WorkspaceMember, (w.w.workspace, w.w.owner))).status = "removed"
            if change == "session": (await db.get(Session, w.main.id)).is_deleted = True
            row = await db.get(PrivateRuntimeBinding, binding.private_runtime_id)
            if change == "revision": row.revision += 1
            if change == "status": row.status = "blocked"
            if change == "schema": row.provider_identity = []
            # The complete original row, not only route fields, is frozen.
            if change == "source": row.error_code = "STATUS_AUTHORITY_CHANGED"

    monkeypatch.setattr(client_module.httpx, "AsyncClient", lambda **args: original_http_client(
        **{**args, "event_hooks": {"response": [changed_after_headers]}}))
    with pytest.raises(AssistantError):
        await service._client_status_for(binding)
    assert w.requests[start:] == ["/v1/status"]
    assert w.w.sent == guest_requests and w.pipe.calls == []


async def test_valid_same_guest_status_can_report_dead_browser_without_granting_control(browser_world):
    w = browser_world
    await original_binding(w)
    w.pipe.live = False
    before = (list(w.w.sent), len(w.requests))
    result = await service.read_browser(**w.scope, resource_id=w.resource_id)
    assert result["remote_available"] is False and result["can_takeover"] is False
    assert result["status"] == "hold" and result["admission"] == "closed"
    assert w.w.sent == before[0] and w.requests[before[1]:] == ["/v1/status"]
    async with get_db_session() as db:
        row = await db.get(ResourceControlLease, w.resource_id)
        assert row.status == "hold" and row.admission_state == "closed"


async def test_browser_pin_is_single_use_and_general_sandbox_validation_still_probes_guest(browser_world):
    w = browser_world
    binding = await original_binding(w)
    pin = await read_private_browser_pin(session_id=w.main.id, user_id=w.w.owner, workspace_id=w.w.workspace,
        binding_id=binding.private_runtime_id, revision=binding.runtime_revision)
    guest_requests = list(w.w.sent)
    client = service._client(pin.route, binding.identity)
    status = await client.status()
    service._isolation(status, pin.route)
    await revalidate_private_browser_pin(pin)
    with pytest.raises(PrivateRuntimeError) as reused:
        await revalidate_private_browser_pin(pin)
    assert reused.value.code == "PRIVATE_RUNTIME_PIN_CONSUMED"
    assert w.w.sent == guest_requests
    args = dict(session_id=w.main.id, user_id=w.w.owner, workspace_id=w.w.workspace)
    route = await resolve_private_runtime(**args, kind="sandbox")
    before = len(w.w.sent)
    assert await validate_private_runtime(route, **args, kind="sandbox") == route
    assert len(w.w.sent) == before + 1 and w.w.sent[-1][1].endswith("/identity")
