"""The cloud-desktop view must stream the box the agent runs in.

Production once had WUYING_MODE=per_user with a sandbox provider that serves a
single shared desktop. Both halves worked: the view provisioned a desktop per
user and streamed it, the agent ran its commands — on a different machine. So a
person watched an idle desktop while their command ran somewhere invisible, and
the agent's "done" was true but unverifiable. Nothing failed; the two planes
just never agreed on which desktop they meant.

These pin the agreement itself rather than either half.
"""
import pytest

from api import desktop as desktop_api
from core.config import OpenBoxConfig


def _config(**overrides) -> OpenBoxConfig:
    cfg = OpenBoxConfig()
    for key, value in overrides.items():
        object.__setattr__(cfg, key, value)
    return cfg


class _Provider:
    def __init__(self, routes_per_user: bool):
        self.routes_per_user = routes_per_user


@pytest.fixture(autouse=True)
def _reset_warning():
    # The mismatch logs once per process; tests must not depend on order.
    desktop_api._warned_split = False
    yield
    desktop_api._warned_split = False


def _use(monkeypatch, *, mode: str, routes_per_user: bool, provider: str = "wuying"):
    monkeypatch.setattr(
        desktop_api, "get_config",
        lambda: _config(sandbox_provider=provider, wuying_mode=mode,
                        wuying_desktop_id="ecd-shared"),
    )
    import sandbox
    monkeypatch.setattr(sandbox, "get_provider", lambda: _Provider(routes_per_user))


def test_per_user_view_needs_a_per_user_sandbox(monkeypatch):
    """Asking for per-user desktops is not enough to serve one."""
    _use(monkeypatch, mode="per_user", routes_per_user=False)
    assert desktop_api._per_user() is False


def test_per_user_view_serves_when_the_sandbox_agrees(monkeypatch):
    _use(monkeypatch, mode="per_user", routes_per_user=True)
    assert desktop_api._per_user() is True


def test_shared_mode_stays_shared(monkeypatch):
    _use(monkeypatch, mode="shared", routes_per_user=True)
    assert desktop_api._per_user() is False


def test_non_wuying_provider_is_never_per_user(monkeypatch):
    _use(monkeypatch, mode="per_user", routes_per_user=True, provider="docker")
    assert desktop_api._per_user() is False


def test_unreadable_provider_falls_back_to_shared(monkeypatch):
    """An unconstructible provider must not be read as "per user is fine"."""
    _use(monkeypatch, mode="per_user", routes_per_user=False)
    import sandbox

    def boom():
        raise RuntimeError("no docker socket")

    monkeypatch.setattr(sandbox, "get_provider", boom)
    assert desktop_api._per_user() is False


def test_the_shared_desktop_provider_declares_itself_shared():
    """The flag is the whole contract; a provider that forgets it re-arms the bug."""
    from sandbox.wuying import WuyingProvider

    assert WuyingProvider.routes_per_user is False


def test_providers_default_to_shared_routing():
    from sandbox.provider import SandboxProvider

    assert SandboxProvider.routes_per_user is False
