"""In-process async pub/sub event bus with optional Redis cross-worker broadcasting."""
import asyncio
import json
from typing import Any, Callable
from uuid import uuid4

from core.log import create_logger

log = create_logger("bus")

# Type for event handlers
EventHandler = Callable[[dict[str, Any]], Any]

# Unique identifier for this worker process
worker_id: str = uuid4().hex[:8]

# Global state
_subscribers: dict[str, list[EventHandler]] = {}
_all_subscribers: list[EventHandler] = []

# Redis state (optional)
_redis_client = None  # redis.asyncio.Redis instance
_redis_listener_task: asyncio.Task | None = None
# ``init_redis_bus`` means this process is a distributed deployment. If its
# Redis connection is down, durable outbox delivery is unavailable — silently
# treating zero local subscribers as success would stamp and lose the event.
_redis_required = False

REDIS_BUS_CHANNEL = "bus:events"


def _dispatch_local(event: dict[str, Any]) -> None:
    """Dispatch an event to local (in-process) subscribers only."""
    event_type = event.get("type", "")

    # Notify type-specific subscribers
    for handler in _subscribers.get(event_type, []):
        try:
            result = handler(event)
            if asyncio.iscoroutine(result):
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(result)
                except RuntimeError:
                    pass
        except Exception as exc:
            log.error(
                f"Event handler error for {event_type}: {type(exc).__name__}"
            )

    # Notify all-event subscribers
    for handler in _all_subscribers:
        try:
            result = handler(event)
            if asyncio.iscoroutine(result):
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(result)
                except RuntimeError:
                    log.warning(f"No running loop for async handler, event={event_type}")
        except Exception as exc:
            log.error(f"Global event handler error: {type(exc).__name__}")


def publish(event_type: str, data: dict[str, Any] | None = None) -> asyncio.Task | None:
    """Publish locally and schedule Redis delivery.

    Ordinary UI hints remain fire-and-forget. Durable outbox callers use
    :func:`publish_confirmed` and await the returned Redis operation before
    stamping their database event.
    """
    event = {"type": event_type, "data": data or {}}

    # Always dispatch locally
    n_type = len(_subscribers.get(event_type, []))
    n_all = len(_all_subscribers)
    if n_all == 0 and event_type not in ("server.heartbeat",):
        log.warning(f"publish({event_type}): 0 subscribers! (type={n_type}, all={n_all})")
    _dispatch_local(event)

    # Also broadcast via Redis if available
    if _redis_client is not None:
        envelope = json.dumps({
            "worker_id": worker_id,
            "event": event,
        })
        try:
            loop = asyncio.get_event_loop()
            task = loop.create_task(_redis_publish(envelope))
            task.add_done_callback(_log_redis_publish_failure)
            return task
        except RuntimeError:
            # No event loop running; skip Redis broadcast
            log.debug("No event loop for Redis broadcast, skipping")
    return None


async def _redis_publish(envelope: str) -> None:
    """Publish envelope to Redis bus channel."""
    client = _redis_client
    if client is None:
        raise RuntimeError("Redis bus is not connected")
    await client.publish(REDIS_BUS_CHANNEL, envelope)


def _log_redis_publish_failure(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    try:
        error = task.exception()
    except asyncio.CancelledError:
        return
    if error is not None:
        log.warning(
            f"Failed to publish to Redis bus: {type(error).__name__}"
        )


async def publish_confirmed(event_type: str, data: dict[str, Any] | None = None) -> None:
    """Return only after the configured Redis publish has completed.

    With no Redis client, synchronous local dispatch is the complete transport
    and returns immediately. The return value of ``publish`` is deliberately
    used here so tests/adapters that replace that public function retain their
    existing interception point.
    """
    if _redis_required and _redis_client is None:
        raise RuntimeError("Redis bus is required but not connected")
    pending = publish(event_type, data)
    if pending is not None:
        await pending


async def _redis_listener() -> None:
    """Listen across transient Redis failures until application shutdown.

    redis-py reconnects an ordinary command on its next use, but a failed
    Pub/Sub connection ends the subscription itself. Letting this task die
    permanently made later outbox publishes succeed while this API replica
    silently stopped forwarding events to its WebSocket clients.
    """
    reconnect_delay = 1.0
    while True:
        pubsub = None
        try:
            client = _redis_client
            if client is None:
                return
            pubsub = client.pubsub()
            await pubsub.subscribe(REDIS_BUS_CHANNEL)
            log.info(f"Redis bus listener started (worker_id={worker_id})")
            reconnect_delay = 1.0
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                if message is not None and message.get("type") == "message":
                    try:
                        envelope = json.loads(message.get("data", ""))
                        if not isinstance(envelope, dict):
                            raise TypeError("Redis envelope must be an object")
                        sender_id = envelope.get("worker_id")
                        event = envelope.get("event")
                        if event is not None and not isinstance(event, dict):
                            raise TypeError("Redis event must be an object")
                        # Only dispatch events from OTHER workers.
                        if sender_id != worker_id and event:
                            _dispatch_local(event)
                    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
                        log.warning(
                            f"Invalid Redis bus message: {type(exc).__name__}"
                        )
                else:
                    await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            log.info("Redis bus listener shutting down")
            raise
        except Exception as exc:
            log.warning(
                "Redis bus listener disconnected; retrying: "
                f"{type(exc).__name__}"
            )
        finally:
            if pubsub is not None:
                try:
                    await pubsub.unsubscribe(REDIS_BUS_CHANNEL)
                    await pubsub.aclose()
                except Exception:
                    pass
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 30.0)


async def init_redis_bus(redis_url: str) -> None:
    """Initialize Redis-based cross-worker event bus.

    Call this during application startup to enable multi-worker event broadcasting.
    If Redis is unavailable, ordinary in-process events continue locally; a
    durable outbox publish fails closed and remains unstamped for retry.
    """
    global _redis_client, _redis_listener_task, _redis_required
    _redis_required = True
    try:
        import redis.asyncio as aioredis
        _redis_client = aioredis.from_url(redis_url, decode_responses=True)
    except Exception as exc:
        log.warning(
            "Failed to configure Redis bus; local hints remain available and "
            "durable outbox delivery is paused: "
            f"{type(exc).__name__}"
        )
        _redis_client = None
        _redis_listener_task = None
        return

    # Keep the reconnect-capable client and listener even when Redis is down
    # during process startup. Durable publishers will fail and retry while the
    # listener independently reconnects; neither path needs a process restart.
    try:
        await _redis_client.ping()
        log.info(f"Redis bus initialized (worker_id={worker_id})")
    except Exception as exc:
        log.warning(
            "Redis bus unavailable at startup; background reconnect is active "
            "and durable outbox delivery remains paused meanwhile: "
            f"{type(exc).__name__}"
        )
    _redis_listener_task = asyncio.create_task(_redis_listener())


async def close_redis_bus() -> None:
    """Shut down the Redis bus listener and close the connection."""
    global _redis_client, _redis_listener_task, _redis_required
    if _redis_listener_task is not None:
        _redis_listener_task.cancel()
        try:
            await _redis_listener_task
        except asyncio.CancelledError:
            pass
        _redis_listener_task = None
    if _redis_client is not None:
        try:
            await _redis_client.aclose()
        except Exception:
            pass
        _redis_client = None
    _redis_required = False
    log.info("Redis bus closed")


def subscribe(event_type: str, handler: EventHandler) -> Callable[[], None]:
    """Subscribe to a specific event type. Returns unsubscribe function."""
    if event_type not in _subscribers:
        _subscribers[event_type] = []
    _subscribers[event_type].append(handler)

    def unsubscribe():
        try:
            _subscribers[event_type].remove(handler)
        except ValueError:
            pass

    return unsubscribe


def subscribe_all(handler: EventHandler) -> Callable[[], None]:
    """Subscribe to all events. Returns unsubscribe function."""
    _all_subscribers.append(handler)

    def unsubscribe():
        try:
            _all_subscribers.remove(handler)
        except ValueError:
            pass

    return unsubscribe


def publish_toast(user_id: str, level: str, message: str) -> None:
    """Publish a toast notification to a user's frontend."""
    from bus.events import TOAST
    publish(TOAST, {"userId": user_id, "level": level, "message": message})


def clear() -> None:
    """Clear all subscribers (for testing)."""
    _subscribers.clear()
    _all_subscribers.clear()
