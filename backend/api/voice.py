"""``/ws/assistant/voice``: a phone call with the personal assistant (docs/VOICE_CALL_SPEC.md §5).

The handshake order and close codes are the contract the web and mobile
clients follow. The socket is accepted first, so a refusal reaches a browser
as its own close code instead of a failed handshake (1006).
"""
import asyncio
import json
import time
from contextlib import suppress

import anyio
from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from auth.middleware import is_auth_enabled
from auth.socket_access import SocketAccess
from auth.ticket import consume_ticket
from core.log import create_logger
from voice import calls, events, phrases, prompt
from voice import config as voice_settings
from voice.assistant_link import AssistantLink, main_session
from voice.bridge import Bridge
from voice.meter import PRICE_DATE, CallMeter
from voice.provider import RealtimeProvider

log = create_logger("api.voice")
router = APIRouter()

provider_factory = RealtimeProvider  # tests replace it with a scripted provider

MAX_FRAME_BYTES = 12800        # 400 ms of 16 kHz PCM16; clients send 3,200-byte frames
CLIENT_SILENCE_SECONDS, HEARTBEAT_SECONDS, LOCK_RENEW_SECONDS = 30, 10, 20
LIMIT_WAIT_SECONDS = 10        # for the limit_reached phrase to be said
STATUS = {"hangup": "ended", "network": "ended", "error": "failed", "limit": "limit", "quota": "limit"}
GONE = (WebSocketDisconnect, RuntimeError, OSError)  # what sending to a closed client raises


class CallEnded(Exception):
    """How a pump ends the call: the ended.reason, an error event code and the close code (None: gone)."""

    def __init__(self, reason: str, *, close: int | None = 1000, error: str | None = None):
        super().__init__(reason)
        self.reason, self.close, self.error = reason, close, error


@router.websocket("/ws/assistant/voice")
async def voice_websocket(websocket: WebSocket, ticket: str = Query(default="")):
    await websocket.accept()
    identity = {"user_id": "default", "client": "web"}
    if is_auth_enabled():
        try:
            identity = await consume_ticket(ticket, audience="voice") if ticket else None
        except Exception as exc:
            log.warning("voice ticket check failed error=%s", type(exc).__name__)
            return await _close(websocket, 1011)
        if not identity:
            return await _close(websocket, 4001)
    access = SocketAccess.from_ticket(identity, authenticated=is_auth_enabled())
    try:
        await access.check()
    except HTTPException:
        return await _close(websocket, 4003)
    config = voice_settings.voice_config()
    if not voice_settings.enabled(config):
        return await _close(websocket, 4503)
    workspace_id, main_id = await main_session(access.user_id, access.workspace_id)
    if not workspace_id or not main_id:
        return await _close(websocket, 4404 if workspace_id else 4003)
    if not await calls.acquire_lock(access.user_id):
        return await _close(websocket, 4009)
    try:
        remaining = await calls.remaining_seconds_today(access.user_id, config.daily_seconds)
        if remaining <= 0:
            return await _close(websocket, 4029)
        await _call(websocket, access, config, workspace_id, main_id, remaining)
    finally:
        with anyio.CancelScope(shield=True):
            await calls.release_lock(access.user_id)


async def _call(websocket, access, config, workspace_id, main_id, remaining):
    user_id, lang = access.user_id, await phrases.user_language(access.user_id)
    max_seconds = min(config.max_call_seconds, remaining)
    limit = "max_duration" if config.max_call_seconds <= remaining else "daily_quota"
    call_id = await calls.create_call(user_id=user_id, workspace_id=workspace_id, main_session_id=main_id,
                                      client=access.client or "web", model=config.model, voice=config.voice)
    provider = provider_factory(config, debug=config.debug_transcripts)
    context = asyncio.create_task(prompt.front_context(user_id=user_id, workspace_id=workspace_id,
                                                       main_session_id=main_id))
    try:
        await provider.open()  # overlaps the context reads
        profile, recent = await context
        await provider.configure(prompt.front_instructions(profile, recent, lang, prompt.local_now()))
    except BaseException as exc:  # including a cancelled handshake: nothing may stay open or "active"
        context.cancel()
        log.warning("voice call=%s provider unavailable error=%s attempts=%s", call_id, type(exc).__name__,
                    getattr(provider, "attempts", None))
        with anyio.CancelScope(shield=True):
            await provider.close()
            await calls.finish_call(call_id, status="failed", end_reason="error", duration_seconds=0, turns=0,
                                    snapshot=CallMeter().snapshot())
            await _send(websocket, events.error("provider_unavailable", lang))
            await _close(websocket, 1011)
        if not isinstance(exc, Exception):
            raise
        return
    link = AssistantLink(call_id=call_id, user_id=user_id, workspace_id=workspace_id, main_session_id=main_id,
                         lang=lang, turn_timeout=config.turn_timeout_seconds)
    bridge = Bridge(provider, link, lang=lang, late_after=config.late_after_seconds,
                    debug_transcripts=config.debug_transcripts)
    started = last_audio = time.monotonic()
    stopped = None  # when the user hung up; the final-usage wait is not call time

    async def client_to_bridge():
        nonlocal last_audio, stopped
        while True:
            try:
                message = await asyncio.wait_for(
                    websocket.receive(), max(CLIENT_SILENCE_SECONDS - (time.monotonic() - last_audio), 0.1))
            except TimeoutError:
                raise CallEnded("network", close=1011, error="internal") from None
            if message["type"] == "websocket.disconnect":
                raise CallEnded("network", close=None)
            if message.get("bytes") is not None:
                frame = message["bytes"]
                if len(frame) > MAX_FRAME_BYTES or len(frame) % 2:
                    raise CallEnded("error", close=4400, error="bad_frame")
                if frame:
                    last_audio = time.monotonic()
                    await bridge.feed_audio(frame)
                continue
            try:
                kind = json.loads(message.get("text") or "").get("type")
            except (ValueError, AttributeError):
                continue
            if kind == "stop":
                stopped = time.monotonic()
                await bridge.stop()
                raise CallEnded("hangup")
            if kind == "ping":
                bridge.emit(events.heartbeat(time.monotonic() - started))

    async def provider_to_bridge():
        async for event in provider.events():
            await bridge.on_provider_event(event)
        raise CallEnded("error", close=1011, error="provider_error")

    async def bridge_to_client():
        while True:
            item = await bridge.outbox.get()
            try:
                await (websocket.send_bytes(item) if isinstance(item, bytes) else websocket.send_json(item))
            except GONE:
                raise CallEnded("network", close=None) from None

    async def timers():
        beat = renew = started
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            if now - started >= max_seconds:
                await bridge.begin_limit(limit, now - started)
                with suppress(TimeoutError):
                    await asyncio.wait_for(bridge.limit_done.wait(), LIMIT_WAIT_SECONDS)
                await asyncio.sleep(bridge.playback_left())  # let the client finish saying it
                raise CallEnded("limit" if limit == "max_duration" else "quota")
            if now - beat >= HEARTBEAT_SECONDS:
                beat = now
                bridge.emit(events.heartbeat(now - started))
            if now - renew >= LOCK_RENEW_SECONDS:
                renew = now
                await calls.renew_lock(user_id)
            await bridge.fill_idle()

    async def raise_on(flag: asyncio.Event, ending: CallEnded):
        await flag.wait()
        raise ending

    tasks, ending = [], CallEnded("network", close=None)
    try:
        await websocket.send_json(events.ready(call_id, config.model, max_seconds, PRICE_DATE))
        await bridge.start()
        tasks = [asyncio.create_task(coroutine) for coroutine in (
            provider_to_bridge(), client_to_bridge(), bridge_to_client(), timers(), access.watch(),
            raise_on(bridge.failed, CallEnded("error", close=1011, error="provider_error")),
            raise_on(bridge.overflow, CallEnded("network", close=1011, error="internal")))]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        stopped = stopped or time.monotonic()
        ending = _ending(next(task for task in tasks if task in done), call_id)
    except GONE:
        pass
    finally:
        with anyio.CancelScope(shield=True):
            provider_pump, pumps = (tasks[0], tasks[1:]) if tasks else (None, [])
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            if provider_pump is not None and not provider_pump.done() and not bridge.closing:
                await bridge.stop()  # the provider pump still reads the final usage
            if provider_pump is not None:
                provider_pump.cancel()
                await asyncio.gather(provider_pump, return_exceptions=True)
            await _end(websocket, bridge, provider, ending, call_id=call_id, user_id=user_id, lang=lang,
                       client=access.client or "web", duration=(stopped or time.monotonic()) - started)


def _ending(task: asyncio.Task, call_id: str) -> CallEnded:
    exc = None if task.cancelled() else task.exception()
    if isinstance(exc, CallEnded):
        return exc
    if isinstance(exc, HTTPException):  # access revoked while talking
        return CallEnded("error", close=4003)
    log.error("voice call=%s failed error=%s", call_id, type(exc).__name__ if exc else "pump returned")
    return CallEnded("error", close=1011, error="internal")


async def _end(websocket, bridge, provider, ending: CallEnded, *, call_id, user_id, lang, client, duration):
    """Record the call, then tell a connected client: error (if any), the final cost, ended, close."""
    pending = len(bridge.pending_calls)
    await bridge.close()
    await provider.close()
    bridge.meter.finish()
    snapshot = bridge.meter.snapshot()
    try:
        await calls.finish_call(call_id, status=STATUS[ending.reason], end_reason=ending.reason,
                                duration_seconds=duration, turns=len(bridge.refs), snapshot=snapshot)
    except Exception as exc:
        log.error("voice call=%s not recorded error=%s", call_id, type(exc).__name__)
    log.info("voice call=%s user=%s client=%s duration=%.1fs turns=%s pending=%s tokens=%s yuan=%s end=%s "
             "close=%s connect=%s", call_id, user_id, client, duration, len(bridge.refs), pending, snapshot["tokens"],
             snapshot["total_yuan"], ending.reason, ending.close, getattr(provider, "attempts", None))
    if ending.close is None:
        return
    if ending.error:
        await _send(websocket, events.error(ending.error, lang))
    await _send(websocket, events.cost(snapshot))
    await _send(websocket, events.ended(ending.reason, duration_seconds=duration, pending_turns=pending,
                                        cost_snapshot=snapshot))
    await _close(websocket, ending.close)


async def _send(websocket, event: dict) -> None:
    with suppress(Exception):
        await websocket.send_json(event)


async def _close(websocket, code: int) -> None:
    with suppress(Exception):
        await websocket.close(code=code)
