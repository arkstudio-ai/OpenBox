"""A pre-provisioned sandbox must survive being recycled.

The WUYING desktop is not OpenBox's container: it was provisioned out of
band and outlives the process. Recycling a sandbox that failed a liveness
check used to clear it from the provider's registry, and nothing outside
the provider's __init__ ever puts it back — so one bad health check turned
every later request into `KeyError: 'wuying-desktop'` until the server was
restarted.
"""
import pytest

from sandbox.client import USER_SCOPE_HEADER, user_scope_for
from sandbox.wuying import CONTAINER_ID, WuyingProvider


@pytest.fixture
def provider(monkeypatch):
    return WuyingProvider()


def evict(p: WuyingProvider) -> None:
    """What _cleanup_stale_sandbox used to do to this provider."""
    p._containers.pop(CONTAINER_ID, None)
    p._api_keys.pop(CONTAINER_ID, None)


def test_the_desktop_is_there_to_begin_with(provider):
    assert provider.get_user_container("someone").id == CONTAINER_ID


def test_the_desktop_comes_back_after_being_evicted(provider):
    evict(provider)
    assert provider.get_user_container("someone").id == CONTAINER_ID


async def test_every_accessor_survives_an_eviction(provider):
    evict(provider)
    assert (await provider.get_container(CONTAINER_ID)).id == CONTAINER_ID
    assert (await provider.ensure_user_container("someone")).id == CONTAINER_ID
    assert (await provider.create_container("x")).id == CONTAINER_ID


def test_the_restored_entry_keeps_its_credentials(provider):
    before = provider.get_user_container("someone")
    evict(provider)
    after = provider.get_user_container("someone")
    assert (after.api_key, after.host, after.port) == (before.api_key, before.host, before.port)


def test_it_is_not_evictable_in_the_first_place():
    # The provider heals itself, but the caller should not be doing this at
    # all — a container it cannot recreate is not its to forget.
    assert WuyingProvider.owns_containers is False


def test_a_provider_that_makes_its_own_containers_still_forgets_them():
    from sandbox.provider import SandboxProvider

    # Future per-user WUYING provisioners may own their desktops. The generic
    # contract remains owning by default; this shared pre-provisioned desktop
    # explicitly opts out above.
    assert SandboxProvider.owns_containers is True


async def test_forwarded_requests_carry_the_callers_opaque_scope(provider, monkeypatch):
    """The proxy path must scope every request to its caller.

    ``forward_to_container`` is how the file APIs and the readiness probes
    reach the desktop, and the deploy now writes
    ``OPENBOX_REQUIRE_USER_SCOPE=1`` into the action server's systemd unit —
    so a request that loses this header stops being a silent tenancy leak and
    starts being a 400 on every proxied call. Neither failure mode should wait
    for a desktop to notice it.
    """
    observed = {}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def request(self, method, url, headers, **_kwargs):
            observed.update({"method": method, "url": url, "headers": headers})
            return object()

    monkeypatch.setattr("sandbox.wuying.httpx.AsyncClient", lambda **_kwargs: Client())
    await provider.forward_to_container(
        CONTAINER_ID,
        "GET",
        "/skills/dev-browser",
        user_id="user-1",
    )

    assert observed["headers"][USER_SCOPE_HEADER] == user_scope_for("user-1")
    # Opaque by construction: the raw id must never travel in the header.
    assert "user-1" not in observed["headers"][USER_SCOPE_HEADER]
