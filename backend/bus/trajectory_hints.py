"""The Redis channel of trajectory hints, ``trajectory:hints`` (WAVE3 shared contract 3).

``trajectory.available`` watermarks (SPEC §8.7) reach the admin WebSockets of
the process that committed them through its in-process bus. Other trajectory
worker processes, and backends that embed the worker, receive them on this
channel only, never on the business ``bus:events`` channel: a worker neither
subscribes to nor decodes business events, and business subscribers never see
trajectory hints. A hint is only a watermark that HTTP catch-up recovers, so
delivery is best effort.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from uuid import uuid4

from core.log import create_logger

log = create_logger("bus.trajectory_hints")

CHANNEL = "trajectory:hints"
EVENT_TYPE = "trajectory.available"
#: What a hint may carry (SPEC §8.7 and the deletion flag); other keys are not forwarded.
HINT_KEYS = frozenset({"user_id", "owner_user_id", "session_id", "trajectory_id", "committed_seq", "deleted"})
RECONNECT_MIN_SECONDS = 1.0
RECONNECT_MAX_SECONDS = 30.0
PING_TIMEOUT_SECONDS = 5.0
#: Pause after an empty poll: redis-py waits inside get_message, other clients return at once.
IDLE_SECONDS = 0.01


def _dispatch(data: dict) -> None:
    from bus.bus import _dispatch_local
    _dispatch_local({"type": EVENT_TYPE, "data": data})


def _log_publish_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        log.warning("Trajectory hint publish failed error_type=%s", type(error).__name__)


class HintChannel:
    """Publishes on and listens to ``trajectory:hints`` through one ``redis.asyncio`` client."""

    def __init__(self, client, *, sender_id: str | None = None):
        self.client = client
        self.sender_id = sender_id or uuid4().hex
        self._listener: asyncio.Task | None = None

    def start(self) -> asyncio.Task:
        if self._listener is None or self._listener.done():
            self._listener = asyncio.get_running_loop().create_task(self._listen(), name="trajectory-hints")
        return self._listener

    async def close(self) -> None:
        listener, self._listener = self._listener, None
        if listener is not None:
            listener.cancel()
            await asyncio.gather(listener, return_exceptions=True)
        with suppress(Exception):
            await self.client.aclose()

    def publish(self, data: dict) -> asyncio.Task | None:
        """Send one hint to the other processes; None without a running event loop."""
        envelope = json.dumps({"sender": self.sender_id, "type": EVENT_TYPE, "data": data},
                              separators=(",", ":"), default=str)
        try:
            task = asyncio.get_running_loop().create_task(self.client.publish(CHANNEL, envelope))
        except RuntimeError:
            return None
        task.add_done_callback(_log_publish_failure)
        return task

    def receive(self, raw) -> dict | None:
        """Dispatch the hint of one channel message locally; None for a message that is ignored.

        Only well-formed hints of other processes count: this process already
        dispatched its own when it published them.
        """
        try:
            envelope = json.loads(raw)
        except (TypeError, ValueError):
            log.warning("Invalid trajectory hint: not JSON")
            return None
        if (not isinstance(envelope, dict) or envelope.get("type") != EVENT_TYPE
                or not isinstance(envelope.get("data"), dict)):
            log.warning("Invalid trajectory hint: not a %s envelope", EVENT_TYPE)
            return None
        if envelope.get("sender") == self.sender_id:
            return None
        data = {key: value for key, value in envelope["data"].items() if key in HINT_KEYS}
        _dispatch(data)
        return data

    async def _listen(self) -> None:
        """Listen until closed; a failed Pub/Sub connection ends its subscription, so subscribe again."""
        delay = RECONNECT_MIN_SECONDS
        while True:
            pubsub = None
            try:
                pubsub = self.client.pubsub()
                await pubsub.subscribe(CHANNEL)
                delay = RECONNECT_MIN_SECONDS
                while True:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if message is None:
                        await asyncio.sleep(IDLE_SECONDS)
                    elif message.get("type") == "message":
                        self.receive(message.get("data"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Trajectory hint listener disconnected; retrying error_type=%s", type(exc).__name__)
            finally:
                if pubsub is not None:
                    with suppress(Exception):
                        await pubsub.unsubscribe(CHANNEL)
                        await pubsub.aclose()
            await asyncio.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX_SECONDS)


_channel: HintChannel | None = None


async def init_trajectory_hints(redis_url: str, *, client=None) -> HintChannel | None:
    """Open this process's hint channel once; ``client`` replaces the Redis client of ``redis_url``.

    Redis being unreachable at startup is not fatal: the listener keeps
    reconnecting, and publishing fails quietly meanwhile.
    """
    global _channel
    if _channel is not None:
        return _channel
    if client is None:
        try:
            import redis.asyncio as aioredis
            client = aioredis.from_url(redis_url, decode_responses=True)
        except Exception as exc:
            log.warning("Trajectory hint channel unavailable; hints stay in this process error_type=%s",
                        type(exc).__name__)
            return None
    channel = _channel = HintChannel(client)
    channel.start()
    try:
        async with asyncio.timeout(PING_TIMEOUT_SECONDS):
            await client.ping()
    except Exception as exc:
        log.warning("Redis is unreachable for trajectory hints; reconnecting in the background error_type=%s",
                    type(exc).__name__)
    return channel


async def close_trajectory_hints() -> None:
    global _channel
    channel, _channel = _channel, None
    if channel is not None:
        await channel.close()


def publish_trajectory_hint(data: dict) -> None:
    """Dispatch a hint to this process's subscribers, then to other processes while the channel is open."""
    _dispatch(data)
    channel = _channel
    if channel is not None:
        channel.publish(data)
