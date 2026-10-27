"""``/ws/assistant/voice``: a phone call with the personal assistant (docs/VOICE_CALL_SPEC.md §5).

The handshake order and close codes are the contract the web and mobile
clients follow. The socket is accepted first, so a refusal reaches a browser
as its own close code instead of a failed handshake (1006).
"""
import asyncio
import dataclasses
import json
import time
from decimal import Decimal
from contextlib import aclosing, suppress

import anyio
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from auth.middleware import get_current_user, is_auth_enabled
from billing.media import settle_voice_call, voice_credit_room
from billing.service import BillingError
from auth.socket_access import SocketAccess
from auth.ticket import consume_ticket
from core.log import create_logger
from voice import calls, cards, events, phrases, prompt, summary, tools, upkeep, voices
from voice import config as voice_settings
from voice.assistant_link import AssistantLink, main_session
from voice.bridge import REPLY_GAP_SECONDS, Bridge
from voice.meter import PRICE_DATE, CallMeter, call_prices
from voice.progress import Progress
from voice.reports import ReportWatcher
from voice.updates import CallUpdates
from voice.provider import RealtimeProvider

log = create_logger("api.voice")
router = APIRouter()

provider_factory = RealtimeProvider  # tests replace it with a scripted provider

MAX_FRAME_BYTES = 12800        # 400 ms of 16 kHz PCM16; clients send 3,200-byte frames
CLIENT_SILENCE_SECONDS, HEARTBEAT_SECONDS, LOCK_RENEW_SECONDS = 30, 10, 20
LIMIT_WAIT_SECONDS = 10        # for the limit_reached phrase to be said
# "quota": the call's credits ran out (VOICE_CALL_SPEC §9).
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
        try:
            # Calls are paid in credits like everything else; no time quota of their own.
            room = await voice_credit_room(workspace_id)
        except BillingError:
            return await _close(websocket, 4029)  # out of credits (docs/VOICE_CALL_SPEC.md §5.5)
        await _call(websocket, access, config, workspace_id, main_id, room)
    finally:
        with anyio.CancelScope(shield=True):
            await calls.release_lock(access.user_id)


class VoiceChoice(BaseModel):
    voice: str | None = Field(default=None, min_length=1, max_length=255)
    model: str | None = Field(default=None, min_length=1, max_length=128)


@router.get("/api/assistant/voice/voices")
async def list_voices(current_user: dict = Depends(get_current_user)):
    """The voices a user may pick for calls (Settings → 语音通话) and the one in use."""
    config = voice_settings.voice_config()
    return await voices.user_selection(current_user["user_id"], config)


@router.get("/api/assistant/voice/samples/{voice_id}")
async def voice_sample(voice_id: str, model: str | None = Query(default=None)):
    """A short preview of a listed voice. Public like any static asset: no user data in it."""
    from fastapi.responses import FileResponse
    if model is not None and model not in voices.MODEL_IDS:
        raise HTTPException(422, "Unknown voice model")
    # Old clients omit model; both catalogues remain available for their previews.
    path = next((path for candidate in ([model] if model else voices.VOICE_KEYS)
                 if (path := voices.sample_path(voice_id, candidate)) is not None), None)
    if path is None:
        raise HTTPException(404, "No sample for this voice")
    return FileResponse(path, media_type="audio/mp4", headers={"Cache-Control": "public, max-age=86400"})


@router.put("/api/assistant/voice/voice")
async def choose_voice(body: VoiceChoice, current_user: dict = Depends(get_current_user)):
    """Saved in the user's preferences; the next call speaks with it."""
    try:
        return await voices.save_choice(current_user["user_id"], voice_settings.voice_config(),
                                       model=body.model, voice=body.voice)
    except ValueError:
        raise HTTPException(422, {"code": "VOICE_UNKNOWN", "message": "Pick one of the listed voices"})


async def _call(websocket, access, config, workspace_id, main_id, room):
    """One call. ``room``: the credits it may spend (enforce billing), None for no cap."""
    user_id, lang = access.user_id, await phrases.user_language(access.user_id)
    # Freeze both for the entire call, including session rotation. Settings
    # changed mid-call take effect on the next call.
    choice = await voices.user_selection(user_id, config)
    config = config.model_copy(update={"model": choice["model"], "voice": choice["selected"]})
    prices = call_prices(config.model)
    call_id = await calls.create_call(user_id=user_id, workspace_id=workspace_id, main_session_id=main_id,
                                      client=access.client or "web", model=config.model, voice=config.voice)
    provider = provider_factory(config, debug=config.debug_transcripts)
    context = asyncio.create_task(prompt.front_context(user_id=user_id, workspace_id=workspace_id,
                                                       main_session_id=main_id))
    try:
        await provider.open()  # overlaps the context reads
        facts = await context
        instructions = prompt.front_instructions(facts, lang, prompt.local_now())
        opening_instructions = prompt.front_instructions(facts, lang, prompt.local_now(), opening=True)
        await provider.configure(opening_instructions)
    except BaseException as exc:  # including a cancelled handshake: nothing may stay open or "active"
        context.cancel()
        log.warning("voice call=%s provider unavailable error=%s attempts=%s", call_id, type(exc).__name__,
                    getattr(provider, "attempts", None))
        with anyio.CancelScope(shield=True):
            await provider.close()
            await calls.finish_call(call_id, status="failed", end_reason="error", duration_seconds=0, turns=0,
                                    snapshot=CallMeter(prices.rates, price_date=prices.date).snapshot())
            await _send(websocket, events.error("provider_unavailable", lang))
            await _close(websocket, 1011)
        if not isinstance(exc, Exception):
            raise
        return
    from voice.assistant_link import observation_timeout
    link = AssistantLink(call_id=call_id, user_id=user_id, workspace_id=workspace_id, main_session_id=main_id,
                         lang=lang, turn_timeout=observation_timeout(config.turn_timeout_seconds), model=config.turn_model,
                         variant=config.turn_variant)
    scope = tools.CallScope(user_id=user_id, workspace_id=workspace_id, main_session_id=main_id, call_id=call_id,
                            lang=lang)

    async def opener(text):
        """A fresh provider session for a long call (voice/upkeep.py) or for one the provider dropped
        (``Bridge.recover``); the client never sees it."""
        fresh = provider_factory(config, debug=config.debug_transcripts)
        try:
            await fresh.open()
            await fresh.configure(text)
        except BaseException:
            await fresh.close()
            raise
        return fresh
    from voice.handoff import required as handoff_required
    from voice.live_context import publish as publish_context
    from core.config import get_config
    from voice.memory_context import prepare as prepare_memory, refresh as refresh_memory, prewarm as prewarm_memory
    native_memory = get_config().memory.enabled("semantic_recall", user_id)
    from voice.shared_context import SharedContextWatcher
    from voice.reply_focus import ReplyFocus
    shared_context = SharedContextWatcher(scope)
    bridge = Bridge(provider, link, lang=lang, late_after=config.late_after_seconds,
                    debug_transcripts=config.debug_transcripts, scope=scope, instructions=instructions,
                    opening_instructions=opening_instructions,
                    handoff_checker=handoff_required,
                    progress=Progress(user_id=user_id, main_session_id=main_id, lang=lang), opener=opener,
                    rates=prices.rates, price_date=prices.date,
                    recap=facts.recap, tell_reports=facts.reports,
                    detail=facts.detail, first_call=facts.first_call,
                    keepalive_after=upkeep.keepalive_seconds(config.model, config.keepalive_seconds),
                    reply_gap=REPLY_GAP_SECONDS, context_sink=lambda lines: publish_context(scope, lines),
                    context_reader=shared_context.read,
                    prepare_memory=prepare_memory if native_memory else None,
                    refresh_memory=refresh_memory if native_memory else None,
                    prewarm_memory=prewarm_memory if native_memory else None,
                    focus=ReplyFocus())
    updates = CallUpdates(scope)
    from voice.memory_capture import OccurrenceClock
    occurrence_clock = OccurrenceClock()
    memory_changed = asyncio.Event()

    async def deliver_reports(found):
        await bridge.report_batch(found)

    async def deliver_context(fresh):
        if fresh:
            await bridge.refresh_shared_context(fresh)

    async def profile_changed(event):
        """A rename or a new way of talking, made in Settings or in chat while this call goes on."""
        if (event.get("data") or {}).get("userId") != user_id or bridge.closing:
            return
        if native_memory:
            # One atomic packet replaces profile AND old memory settings. The
            # bus event only wakes the SQL reader, never supplies authority.
            memory_changed.set()
            return
        nonlocal facts
        try:
            # The plans offered when the call began stay offered; asking how to address them goes on until answered.
            fresh = await prompt.user_facts(user_id, workspace_id, asking=facts.ask_address)
            facts = dataclasses.replace(facts, **{**fresh, "followups": facts.followups})
            await bridge.rebase(prompt.front_instructions(facts, lang, prompt.local_now()),
                                tell_reports=facts.reports, detail=facts.detail)
        except Exception as exc:  # the call goes on with the profile it started with
            log.warning("voice call=%s profile not refreshed error=%s", call_id, type(exc).__name__)
    from bus import bus
    from bus.events import ASSISTANT_PROFILE_UPDATED
    unsubscribe_profile = bus.subscribe(ASSISTANT_PROFILE_UPDATED, profile_changed)
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
                payload = json.loads(message.get("text") or "")
                kind = payload.get("type")
            except (ValueError, AttributeError):
                continue
            if kind == "stop":
                stopped = time.monotonic()
                await bridge.stop()
                raise CallEnded("hangup")
            if kind == "ping":
                bridge.emit(events.heartbeat(time.monotonic() - started))
            elif kind == "view":
                from voice.view_context import session_hint
                bridge.scope = dataclasses.replace(bridge.scope,
                    viewed_session_id=session_hint(payload.get("session_id")))
            elif kind == "capabilities" and payload.get("playback_ack") is True:
                bridge.playback_ack = True
            elif kind == "playback.completed" and isinstance(payload.get("response_id"), str):
                await bridge.playback_completed(payload["response_id"])

    async def provider_to_bridge():
        while True:  # a long call moves to a fresh provider session; follow it
            current = bridge.provider
            async with aclosing(current.events()) as stream:
                async for event in stream:
                    if current is not bridge.provider:
                        break  # the replaced session's last words: the new one has the call
                    await _provider_event(bridge, link, event, occurrence_clock=occurrence_clock)
            # The provider closed the session in use (an idle timeout, a server error): a fresh one carries
            # the call on, the client socket staying open; or the call ends as it always did.
            if current is bridge.provider and not await bridge.recover(current):
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
            # Calls have no duration cap; only the available credits can end one here.
            spent = room is not None and Decimal(bridge.meter.snapshot()["total_yuan"]) >= room
            if spent:
                await bridge.begin_limit("credits", now - started)
                with suppress(TimeoutError):
                    await asyncio.wait_for(bridge.limit_done.wait(), LIMIT_WAIT_SECONDS)
                await asyncio.sleep(bridge.playback_left())  # let the client finish saying it
                raise CallEnded("quota")
            if now - beat >= HEARTBEAT_SECONDS:
                beat = now
                bridge.emit(events.heartbeat(now - started))
            if now - renew >= LOCK_RENEW_SECONDS:
                renew = now
                await calls.renew_lock(user_id)
            await bridge.fill_idle()

    async def memory_updates():
        if not native_memory:
            await asyncio.Future()
        failures = 0
        while True:
            try:
                await asyncio.wait_for(memory_changed.wait(), 0.5)
            except TimeoutError:
                pass
            memory_changed.clear()
            try:
                await asyncio.wait_for(bridge.refresh_idle_memory(), 5)
                failures = 0
            except Exception as exc:
                failures += 1
                if failures == 1 or failures % 30 == 0:
                    log.warning("voice memory refresh failed call=%s error=%s", call_id, type(exc).__name__)

    async def raise_on(flag: asyncio.Event, ending: CallEnded):
        await flag.wait()
        raise ending

    async def pending_cards():
        # Independent of the heartbeat/credit timer: a slow question read must not stall a call.
        failures, initialized = 0, False
        while True:
            try:
                await asyncio.wait_for(updates.cards.wait(), cards.POLL_SECONDS)
            except TimeoutError:
                pass
            updates.cards.clear()
            reading = asyncio.create_task(asyncio.wait_for(cards.waiting(bridge.scope), cards.POLL_TIMEOUT_SECONDS))
            try:
                snapshot = await asyncio.shield(reading)
                if not initialized:
                    # Existing decisions stay available through cards_pending;
                    # reconnecting is not a request to read them aloud again.
                    bridge.seed_waiting_cards(*snapshot)
                    initialized = True
                else:
                    await bridge.waiting_cards(*snapshot)
                failures = 0
            except asyncio.CancelledError:
                # Finish the bounded read before call settlement. Cancelling an in-flight SQLite read
                # can leave its transaction holding a lock while hang-up tries to write the bill.
                await asyncio.gather(reading, return_exceptions=True)
                raise
            except Exception as exc:
                failures += 1
                if failures == 1 or failures % 30 == 0:
                    log.warning("voice cards watch failed call=%s error=%s", call_id, type(exc).__name__)

    tasks, ending, answered = [], CallEnded("network", close=None), False
    reports = ReportWatcher(user_id=user_id, main_session_id=main_id, call_id=call_id, workspace_id=workspace_id)
    try:
        bridge.progress.start()
        await reports.start()
        # Answer as soon as the provider is configured. Both audio directions
        # run before requesting the opening, so the very first words can interrupt it.
        started = last_audio = time.monotonic()
        await websocket.send_json(events.ready(call_id, config.model, prices.date or PRICE_DATE))
        answered = True
        bridge.answered()
        tasks = [asyncio.create_task(coroutine) for coroutine in (
            provider_to_bridge(), client_to_bridge(), access.watch(),
            raise_on(bridge.failed, CallEnded("error", close=1011, error="provider_error")),
            bridge_to_client(), timers(), pending_cards(), memory_updates(),
            updates.watch(updates.reports, reports.poll, deliver_reports, name="reports"),
            updates.watch(updates.context, shared_context.poll, deliver_context, name="context"),
            raise_on(bridge.overflow, CallEnded("network", close=1011, error="internal")))]
        await bridge.start()
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        stopped = stopped or time.monotonic()
        ending = _ending(next(task for task in tasks if task in done), call_id)
    except GONE:
        pass
    finally:
        unsubscribe_profile()
        updates.close()
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
            # A call hung up while it was still ringing lasted nothing.
            duration = (stopped or time.monotonic()) - started if answered else 0.0
            await _end(websocket, bridge, provider, ending, call_id=call_id, user_id=user_id, lang=lang,
                       client=access.client or "web", duration=duration, workspace_id=workspace_id,
                       main_id=main_id, model=config.model)
            if bridge.spoken.user_lines():  # what this call was about, for the next greeting (background)
                summary.save_after_call(call_id, bridge.spoken.render(bridge.keeper.covered), bridge.keeper.summary)


async def _provider_event(bridge, link, event, *, occurrence_clock=None):
    """Final ASR becomes durable before recall/forwarding can use the words.

    A bounded SQL-only write cannot wait on this event consumer for an ACK.
    Hang-up drains the write already received, then cancels the Bridge work;
    it must not cancel an accepted human utterance with a model response.
    """
    provider, epoch = getattr(bridge, "provider", None), bridge.session
    occurred_at = occurrence_clock.observe(event, epoch) if occurrence_clock is not None else None
    if event.kind == "user_transcript" and event.text.strip():
        from voice.memory_capture import capture_final
        writing = asyncio.create_task(asyncio.wait_for(capture_final(
            bridge.scope, text=event.text, item_id=event.item_id, provider_session=epoch,
            occurred_at=occurred_at), timeout=3))
        try:
            identity = await asyncio.shield(writing)
        except asyncio.CancelledError:
            await asyncio.gather(writing, return_exceptions=True)
            raise
        except Exception as exc:
            log.warning("voice call=%s final ASR unavailable error=%s", bridge.call_id, type(exc).__name__)
            # The utterance is still answered and can still be handed over, but
            # without a durable human source it is never recalled from and
            # nothing may claim it was saved (remember reports unavailable).
            identity = None
            if skip := getattr(bridge, "skip_recall", None):
                skip(event.item_id)
        if bridge.session != epoch or getattr(bridge, "provider", None) is not provider:
            return  # Keep the old human evidence; do not inject it into a replacement provider session.
        if identity:
            link.bind_native_source(event.item_id, event.text, identity)
    await bridge.on_provider_event(event)


def _ending(task: asyncio.Task, call_id: str) -> CallEnded:
    exc = None if task.cancelled() else task.exception()
    if isinstance(exc, CallEnded):
        return exc
    if isinstance(exc, HTTPException):  # access revoked while talking
        return CallEnded("error", close=4003)
    log.error("voice call=%s failed error=%s", call_id, type(exc).__name__ if exc else "pump returned")
    return CallEnded("error", close=1011, error="internal")


async def _end(websocket, bridge, provider, ending: CallEnded, *, call_id, user_id, lang, client, duration,
               workspace_id, main_id, model):
    """Record and bill the call, then tell a connected client: error (if any), the final cost, ended, close."""
    pending = len(bridge.pending_calls)
    await bridge.close()
    await bridge.provider.close()
    if bridge.provider is not provider:
        await provider.close()
    bridge.meter.finish()
    snapshot = bridge.meter.snapshot()
    try:
        await calls.finish_call(call_id, status=STATUS[ending.reason], end_reason=ending.reason,
                                duration_seconds=duration, turns=len(bridge.refs), snapshot=snapshot)
    except Exception as exc:
        log.error("voice call=%s not recorded error=%s", call_id, type(exc).__name__)
    try:
        # One usage row per call, keyed on it: the provider's usage at the billing catalogue's prices.
        await settle_voice_call(call_id=call_id, workspace_id=workspace_id, user_id=user_id, session_id=main_id,
                                model_id=model, snapshot=snapshot, duration_sec=duration)
    except Exception as exc:  # the call is over either way; an unbilled call is in the log and in voice_calls
        log.error("voice call=%s not billed error=%s", call_id, type(exc).__name__)
    log.info("voice call=%s user=%s client=%s duration=%.1fs turns=%s pending=%s tokens=%s yuan=%s end=%s "
             "close=%s connect=%s recoveries=%s", call_id, user_id, client, duration, len(bridge.refs), pending,
             snapshot["tokens"], snapshot["total_yuan"], ending.reason, ending.close,
             getattr(provider, "attempts", None), bridge.recoveries)
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
