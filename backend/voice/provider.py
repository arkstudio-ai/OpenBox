"""Bailian realtime client for one call: connect, configure, translate, send.

Only this module speaks the provider's protocol. The bridge sees
``ProviderEvent`` values; raw provider error text never leaves this module
except in a QA-only debug log line with the key scrubbed.
"""
import asyncio
import base64
import binascii
import json
import time
import uuid
from dataclasses import dataclass

import websockets

from core.log import create_logger
from voice import config as settings, tools, voices
from voice.models import is_audio

log = create_logger("voice.provider")

ASSISTANT_ASK = tools.ASSISTANT_ASK
TOOLS = tools.schemas()
MAX_MESSAGE_BYTES = 8 * 1024 * 1024
RETRY_PAUSE_SECONDS = 0.5
RACE_HEAD_START_SECONDS = 0.4  # the first route usually opens in 0.15-0.25 s


class ProviderUnavailable(Exception):
    """No configured session could be opened; the call ends as provider_unavailable."""


@dataclass(frozen=True)
class ProviderEvent:
    """One provider event the bridge cares about (docs/VOICE_CALL_BACKEND.md §7)."""
    kind: str
    response_id: str | None = None
    audio: bytes = b""
    event_id: str = ""
    call_id: str = ""
    name: str = ""
    arguments: str = ""
    text: str = ""
    status: str = ""
    usage: dict | None = None
    invalid: bool = False
    code: str = ""
    # provider_error only: active_response / duplicate_output / voice_unsupported / no_input / item / other.
    reason: str = ""
    # Conversation items (item_created / item_deleted / transcripts): the provider's own item id.
    item_id: str = ""
    role: str = ""
    item_type: str = ""


def classify_error(message: str) -> str:
    text = message.lower()
    if "active response" in text:
        return "active_response"
    if "duplicate" in text and "output" in text:
        return "duplicate_output"
    if "voice" in text and "not supported" in text:
        return "voice_unsupported"
    if "without input" in text or "no user message" in text or "no messages" in text:
        return "no_input"
    if ("item" in text and ("not found" in text or "not exist" in text or "invalid" in text)) or (
            "function call id" in text or "call_id" in text):
        # A delete or create of an item, or a tool output for a call the provider dropped ("Unknown function
        # call id", 2026-10-08): never a refused response, though our delivery request may be pending.
        return "item"
    return "other"


def translate(event: dict) -> ProviderEvent | None:
    kind = event.get("type")
    if kind == "input_audio_buffer.speech_started":
        return ProviderEvent("user_started")
    if kind == "input_audio_buffer.speech_stopped":
        return ProviderEvent("user_stopped", invalid=event.get("reason") == "turn_invalid")
    if kind == "conversation.item.input_audio_transcription.completed":
        return ProviderEvent("user_transcript", text=str(event.get("transcript") or ""),
                             item_id=str(event.get("item_id") or ""))
    if kind == "conversation.item.created":
        # Every item: user audio (at speech start), our notes, replies, function calls and outputs.
        # The provider assigns the id; one we send is ignored (measured 2026-10-07).
        item = event.get("item") or {}
        return ProviderEvent("item_created", item_id=str(item.get("id") or ""), role=str(item.get("role") or ""),
                             item_type=str(item.get("type") or ""), text=_item_text(item),
                             call_id=str(item.get("call_id") or ""))
    if kind == "conversation.item.deleted":
        return ProviderEvent("item_deleted", item_id=str(event.get("item_id") or ""))
    if kind == "response.created":
        return ProviderEvent("response_started", response_id=(event.get("response") or {}).get("id"))
    if kind == "response.audio.delta":
        try:
            audio = base64.b64decode(event.get("delta") or "", validate=True)
        except (binascii.Error, ValueError):
            return None
        return ProviderEvent("audio", response_id=event.get("response_id"), audio=audio,
                             event_id=str(event.get("event_id") or ""))
    if kind in ("response.audio_transcript.done", "response.text.done"):
        # Text-only sessions (tests, probes) report the reply as text instead of a transcript.
        return ProviderEvent("assistant_transcript", response_id=event.get("response_id"),
                             text=str(event.get("transcript") or event.get("text") or ""),
                             item_id=str(event.get("item_id") or ""))
    if kind == "response.function_call_arguments.done":
        arguments = str(event.get("arguments") or "")
        return ProviderEvent("tool_call", response_id=event.get("response_id"), call_id=str(event.get("call_id") or ""),
                             name=str(event.get("name") or ""), arguments=arguments, text=_ask_text(arguments))
    if kind == "response.done":
        response = event.get("response") or {}
        return ProviderEvent("response_done", response_id=response.get("id"),
                             status=str(response.get("status") or "completed"), usage=response.get("usage"))
    if kind == "error":
        error = event.get("error") or {}
        return ProviderEvent("provider_error", code=str(error.get("code") or "")[:80],
                             reason=classify_error(str(error.get("message") or "")))
    return None


def _item_text(item: dict) -> str:
    """The text of a typed message item (our notes); audio items carry none yet."""
    return "".join(str(part.get("text") or "") for part in item.get("content") or []
                   if isinstance(part, dict) and part.get("type") in ("input_text", "text"))


def _ask_text(arguments: str) -> str:
    """``assistant_ask``'s ``request``: what the user wants, restated so it reads without the call."""
    try:
        value = json.loads(arguments or "{}")
    except ValueError:
        return ""
    if not isinstance(value, dict):
        return ""
    return str(value.get("request") or value.get("text") or "").strip()


class RealtimeProvider:
    def __init__(self, config, *, debug: bool = False):
        self.config = config
        self.debug = debug
        self.attempts: list[dict] = []
        self._key = settings.api_key(config)
        self._ws = None
        self._send_lock = asyncio.Lock()
        self._prompt_items: set[str] = set()
        self._pending_prompts: set[str] = set()
        self._response_prompts: dict[str, set[str]] = {}

    async def open(self) -> None:
        """Handshake until ``session.created``, racing the routes in ``proxy_plan``.

        Measured on QA (2026-10-07): either route sometimes hangs for the
        whole timeout while the other opens in 0.12-0.26 s. Trying them one
        after another cost callers 5 s; now each round starts the first route
        at once and the next after a short head start, the first session wins
        and the others are closed.
        """
        url = settings.realtime_url(self.config)
        timeout = self.config.connect_timeout_seconds
        plan = settings.proxy_plan(self.config)
        routes = list(dict.fromkeys(plan))
        for number in range(-(-len(plan) // len(routes))):
            if number:
                await asyncio.sleep(RETRY_PAUSE_SECONDS)
            ws = await self._race(url, routes, timeout)
            if ws is not None:
                self._ws = ws
                return
        raise ProviderUnavailable("no realtime session could be opened")

    async def _race(self, url, routes, timeout):
        async def attempt(proxy, delay):
            await asyncio.sleep(delay)
            started, ws = time.monotonic(), None
            try:
                ws = await websockets.connect(url, additional_headers={"Authorization": f"Bearer {self._key}"},
                                              proxy=proxy, open_timeout=timeout, close_timeout=2,
                                              ping_interval=20, max_size=MAX_MESSAGE_BYTES)
                await self._expect(ws, "session.created", timeout)
            except asyncio.CancelledError:
                if ws is not None:
                    await _quiet_close(ws)
                raise
            except Exception as exc:  # each attempt fails alone; the type is enough to diagnose
                self._attempt(proxy, started, f"failed:{type(exc).__name__}")
                if ws is not None:
                    await _quiet_close(ws)
                raise
            self._attempt(proxy, started, "ok")
            return ws

        tasks = [asyncio.create_task(attempt(proxy, index * RACE_HEAD_START_SECONDS))
                 for index, proxy in enumerate(routes)]
        winner = None
        try:
            for finished in asyncio.as_completed(tasks):
                try:
                    winner = await finished
                    break
                except Exception:
                    continue
        finally:  # also when the call is cancelled mid-handshake: nothing may stay open
            for task in tasks:
                task.cancel()
            for result in await asyncio.gather(*tasks, return_exceptions=True):
                if result is not winner and hasattr(result, "close"):
                    await _quiet_close(result)
        return winner

    def _attempt(self, proxy, started, result):
        attempt = {"mode": "env-proxy" if proxy else "direct", "seconds": round(time.monotonic() - started, 3),
                   "result": result}
        self.attempts.append(attempt)
        log.info("voice provider connect attempt=%s mode=%s seconds=%s result=%s",
                 len(self.attempts), attempt["mode"], attempt["seconds"], result)

    async def configure(self, instructions: str) -> None:
        config = self.config
        session = {
            "modalities": ["text", "audio"],
            "voice": voices.resolve(None, config.voice, config.model), "instructions": instructions,
            "input_audio_format": "pcm", "output_audio_format": "pcm",
            "tools": TOOLS,
        }
        if is_audio(config.model):
            session.update(turn_detection={"type": "smart_turn"}, max_history_turns=config.max_history_turns,
                           enable_speech_emotion=config.enable_speech_emotion)
        else:
            session.update(smooth_output=True, turn_detection={"type": "semantic_vad",
                           "threshold": config.vad_threshold, "silence_duration_ms": config.silence_ms})
        await self._send({"type": "session.update", "session": session})
        try:
            await self._expect(self._ws, "session.updated", config.connect_timeout_seconds)
        except Exception as exc:
            raise ProviderUnavailable(f"session.update failed: {type(exc).__name__}") from None

    async def _expect(self, ws, expected: str, timeout: float) -> dict:
        async with asyncio.timeout(timeout):
            async for raw in ws:
                event = json.loads(raw)
                if event.get("type") == "error":
                    self._debug_error(event)
                    raise ProviderUnavailable(str((event.get("error") or {}).get("code") or "error")[:80])
                if event.get("type") == expected:
                    return event
        raise ProviderUnavailable(f"closed before {expected}")

    async def events(self):
        """Translated events until the provider closes the connection."""
        try:
            async for raw in self._ws:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if await self._response_prompt_event(event):
                    continue
                if event.get("type") == "error":
                    self._debug_error(event)
                translated = translate(event)
                if translated is not None:
                    yield translated
        except websockets.exceptions.ConnectionClosed:
            return

    def _debug_error(self, event):
        if self.debug:  # QA only (debug_transcripts); otherwise only the code reaches a log
            message = str((event.get("error") or {}).get("message") or "")[:200]
            log.debug("voice provider error message=%s", message.replace(self._key, "[hidden]") if self._key
                      else message)

    async def send_audio(self, pcm: bytes) -> None:
        await self._send({"type": "input_audio_buffer.append", "audio": base64.b64encode(pcm).decode("ascii")})

    async def send_tool_output(self, call_id: str, payload: dict) -> None:
        await self._send({"type": "conversation.item.create", "item": {
            "type": "function_call_output", "call_id": call_id, "output": json.dumps(payload, ensure_ascii=False)}})

    async def create_note(self, text: str) -> None:
        """A typed user-role message: how results reach the conversation (measured 2026-10-07)."""
        await self._send({"type": "conversation.item.create", "item": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})

    async def delete_item(self, item_id: str) -> None:
        await self._send({"type": "conversation.item.delete", "item_id": item_id})

    async def update_instructions(self, instructions: str) -> None:
        """Only the instructions; voice, VAD and tools stay (measured 2026-10-07). Send it while idle."""
        await self._send({"type": "session.update", "session": {"instructions": instructions}})

    async def create_response(self, instructions: str | None = None) -> None:
        """Audio requires a user item even for a greeting; Omni supports per-response instructions."""
        if instructions and is_audio(self.config.model):
            item_id = "voice_prompt_" + uuid.uuid4().hex
            self._prompt_items.add(item_id)
            self._pending_prompts.add(item_id)
            text = ("（后台播报要求，不是用户发言，也不是要转交的新任务。仅适用于紧接着的一次回复。）\n"
                    + instructions)
            await self._send_many({"type": "conversation.item.create", "item": {
                "id": item_id, "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": text}]}}, {"type": "response.create"})
            return
        event = {"type": "response.create"}
        if instructions:
            event["response"] = {"instructions": instructions}
        await self._send(event)

    async def _response_prompt_event(self, event: dict) -> bool:
        """Retire Audio's one-response prompts on completion, cancellation or a refused request.

        Keep these control items out of the user's transcript and context keeper.
        Audio preserves supplied item IDs (checked live); ordinary notes still pass through.
        """
        kind = event.get("type")
        item_id = (event.get("item") or {}).get("id") if kind == "conversation.item.created" else event.get("item_id")
        if kind in ("conversation.item.created", "conversation.item.deleted") and item_id in self._prompt_items:
            if kind == "conversation.item.deleted":
                self._prompt_items.discard(item_id)
            return True
        if kind == "response.created" and self._pending_prompts:
            self._response_prompts[(event.get("response") or {}).get("id", "")] = self._pending_prompts
            self._pending_prompts = set()
        if kind == "response.done":
            await self._delete_prompts(self._response_prompts.pop((event.get("response") or {}).get("id", ""), set()))
        elif kind == "error" and self._pending_prompts:
            pending, self._pending_prompts = self._pending_prompts, set()
            await self._delete_prompts(pending)
        return False

    async def _delete_prompts(self, items: set[str]) -> None:
        for item_id in items:
            await self.delete_item(item_id)

    async def cancel_response(self) -> None:
        await self._send({"type": "response.cancel"})

    async def close(self) -> None:
        if self._ws is not None:
            await _quiet_close(self._ws)

    async def _send(self, event: dict) -> None:
        await self._send_many(event)

    async def _send_many(self, *events: dict) -> None:
        async with self._send_lock:
            for event in events:
                await self._ws.send(json.dumps(event, ensure_ascii=False))


async def _quiet_close(ws) -> None:
    try:
        async with asyncio.timeout(3):
            await ws.close()
    except Exception:
        pass
