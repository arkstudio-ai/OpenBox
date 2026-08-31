"""Tenant-scope transport contract for hardened shared sandboxes."""

import httpx
import pytest

from sandbox.client import SandboxClient, USER_SCOPE_HEADER, user_scope_for


def test_user_scope_is_stable_opaque_and_validated():
    raw_user = "auth-provider|person@example.test"
    scope = user_scope_for(raw_user)

    assert scope == user_scope_for(raw_user)
    assert scope.startswith("u-")
    assert len(scope) == 22
    assert raw_user not in scope
    with pytest.raises(ValueError, match="Invalid sandbox user scope"):
        SandboxClient("sandbox", 8000, "key", user_scope=raw_user)


@pytest.mark.asyncio
async def test_scope_header_reaches_direct_skill_requests():
    observed = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request.headers.get(USER_SCOPE_HEADER))
        return httpx.Response(200, json={"name": "dev-browser"})

    scope = user_scope_for("user-1")
    client = SandboxClient(
        "sandbox",
        8000,
        "key",
        base_url="http://action.test",
        user_scope=scope,
    )
    transport = httpx.MockTransport(handler)

    def factory(timeout: float = 30.0):
        return httpx.AsyncClient(
            transport=transport,
            base_url=client.base_url,
            headers=client._headers,
            timeout=timeout,
        )

    client._client = factory
    assert await client.get_skill("dev-browser") == {"name": "dev-browser"}
    assert observed == [scope]
