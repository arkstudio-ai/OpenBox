from types import SimpleNamespace

import pytest

from main import create_app


def _bootstrap_endpoint(app):
    return next(route.endpoint for route in app.routes if route.path == "/api/auth/bootstrap")


@pytest.mark.anyio
async def test_single_user_bootstrap_returns_the_stable_local_owner(monkeypatch):
    monkeypatch.setattr(
        "core.config.get_config",
        lambda: SimpleNamespace(
            jwt_secret="",
            cors_origins=["http://localhost:3000"],
        ),
    )

    app = create_app()
    payload = await _bootstrap_endpoint(app)()

    assert payload == {
        "mode": "single_user",
        "user": {"id": "default", "username": "default", "role": "admin"},
    }
    paths = {route.path for route in app.routes}
    assert {"/api/auth/me", "/api/auth/me/preferences", "/api/auth/ticket"} <= paths
    assert {
        "/api/auth/login",
        "/api/auth/register",
        "/api/auth/refresh",
    }.isdisjoint(paths)


@pytest.mark.anyio
async def test_multi_user_bootstrap_never_returns_a_user(monkeypatch):
    monkeypatch.setattr(
        "core.config.get_config",
        lambda: SimpleNamespace(
            jwt_secret="configured",
            cors_origins=["http://localhost:3000"],
        ),
    )

    payload = await _bootstrap_endpoint(create_app())()

    assert payload == {"mode": "multi_user"}
