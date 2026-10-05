"""A slow original Wuying status response must fit the whole startup budget."""
import asyncio

from sandbox.browser_resource_client import BrowserResourceClient
from tests.unit.test_assistant_browser_resources import browser_world, ensure  # noqa: F401
from tests.unit.test_private_wuying_runtime import assistant_database, wuying_world as private_world  # noqa: F401


async def test_remote_status_above_two_seconds_completes_without_canceling_original_read(browser_world, monkeypatch):
    original = BrowserResourceClient.status
    reads, cancellations = [], []

    async def delayed(client):
        reads.append(client.base_url)
        try:
            await asyncio.sleep(2.1)
            return await original(client)
        except asyncio.CancelledError:
            cancellations.append(client.base_url)
            raise

    monkeypatch.setattr(BrowserResourceClient, "status", delayed)
    result = await ensure(browser_world)
    assert result["fence"]["epoch"] == 1 and result["remote_available"]
    assert reads == [browser_world.route.base_url] * 2
    assert cancellations == []
    assert browser_world.requests == ["/v1/status", "/v1/status"]
    assert not browser_world.pipe.calls
