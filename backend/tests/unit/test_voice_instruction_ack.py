"""Session updates need the exact receipt, independent of the bridge pump."""
import asyncio
import json

import pytest

from tests.support.voice_fakes import drain
from voice.config import VoiceConfig
from voice.provider import RealtimeProvider


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        return json.dumps(await self.incoming.get())

    async def send(self, raw):
        await self.sent.put(json.loads(raw))

    async def close(self):
        pass


async def receipt(socket, instructions):
    await socket.incoming.put({"type": "session.updated", "session": {"instructions": instructions}})
    await drain()


async def test_receipt_reader_does_not_depend_on_bridge_consuming_events():
    provider = RealtimeProvider(VoiceConfig())
    provider._ws = socket = Socket()
    update = asyncio.create_task(provider.update_instructions_confirmed("current private evidence\n"))
    try:
        assert (await socket.sent.get())["type"] == "session.update"
        # Neither a different policy nor whitespace-normalized private bytes
        # certify the submitted prompt. No bridge event consumer is running.
        await receipt(socket, "previous private evidence\n")
        await receipt(socket, "current private evidence")
        assert not update.done()
        await receipt(socket, "current private evidence\n")
        await update
    finally:
        await provider.close()


async def test_late_receipt_cannot_confirm_the_next_different_update():
    provider = RealtimeProvider(VoiceConfig())
    provider._ws = socket = Socket()
    try:
        with pytest.raises(TimeoutError):
            await provider.update_instructions_confirmed("old policy", timeout=.01)
        await socket.sent.get()
        next_update = asyncio.create_task(provider.update_instructions_confirmed("new policy"))
        await socket.sent.get()
        await receipt(socket, "old policy")
        assert not next_update.done()
        await receipt(socket, "new policy")
        await next_update
    finally:
        await provider.close()
