"""One call's state machine between the client, the front desk and the personal assistant.

Rules: docs/VOICE_CALL_SPEC.md §4.2 and docs/VOICE_CALL_BACKEND.md §8. Measured
against the provider: ``response.create`` while a reply is active is refused,
and a second output for one call_id is refused. So every call_id gets exactly
one output, sent as soon as the assistant settles, and a reply (fixed phrase or
result reading) is only requested in an idle window.
"""
import asyncio
import time
from collections import deque
from contextlib import suppress

from core.identifier import generate_id
from core.log import create_logger
from voice import events, phrases
from voice.assistant_link import VoiceTurnRef, turn_outcome as _outcome
from voice.meter import CallMeter, OUTPUT_BYTES_PER_SECOND
from voice.provider import ASSISTANT_ASK

log = create_logger("voice.bridge")

OUTBOX_LIMIT = 1000
# After a valid end of speech the VAD answers by itself; keep out of its way this long.
REPLY_GRACE_SECONDS = 3.0
STOP_WAIT_SECONDS, CLOSE_WAIT_SECONDS = 2.0, 3.0  # final usage after hang-up; turn observers' last step
DELIVERY_ATTEMPTS = 3
PLAYBACK_MARGIN_SECONDS = 0.3


class Bridge:
    def __init__(self, provider, link, *, lang: str = "zh", late_after: float = 20.0,
                 clock=time.monotonic, debug_transcripts: bool = False):
        self.provider, self.link, self.lang = provider, link, lang
        self.late_after, self.clock, self.debug = late_after, clock, debug_transcripts
        self.state = "idle"                     # idle / user_speaking / responding
        self.active_response: str | None = None
        self.interrupted = False                # the active reply's audio is dropped
        self.pending_calls: dict[str, VoiceTurnRef] = {}
        self.deliveries: deque[VoiceTurnRef] = deque()
        self.response_kind: str | None = None   # model / phrase:<key> / delivery:<turn_id>
        self.requested: str | None = None       # response.create sent, response.created not seen
        self.outbox: asyncio.Queue = asyncio.Queue(maxsize=OUTBOX_LIMIT)
        self.overflow, self.failed, self.limit_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.settled = asyncio.Event()          # set: no started reply still owes its usage
        self.settled.set()
        self.meter = CallMeter()
        self.greeting, self.closing, self.limit_reason = True, False, None
        self.awaiting_since: float | None = None
        self.audio_sent, self.audio_bytes, self.first_audio_at = False, 0, None
        self.transcript = ""                    # the latest user utterance, for VoiceTurn.transcript
        self.refs: list[VoiceTurnRef] = []      # every assistant_ask of the call
        self._seen_calls: set[str] = set()
        self._claimed: str | None = None        # the request the active reply was taken to answer
        self._tasks: set[asyncio.Task] = set()
        self._phase = None

    def emit(self, item) -> None:
        """Queue for the client; after hang-up the socket sends only the final events itself."""
        if self.closing:
            return
        try:
            self.outbox.put_nowait(item)
        except asyncio.QueueFull:
            self.overflow.set()  # the client stopped reading; the socket ends the call

    def _update_phase(self) -> None:
        # A turn is working until its result has been read out; only the front desk's own replies "think".
        working = bool(self.pending_calls or self.deliveries)
        late = any(ref.late for ref in self.pending_calls.values())
        responding = self.state == "responding"
        value = ("greeting" if self.greeting else "listening" if self.state == "user_speaking"
                 else "speaking" if responding and self.audio_sent
                 else "thinking" if (responding and self.response_kind == "model") or self._awaiting_reply()
                 else "working" if working else "listening")
        if (value, working, late) != self._phase:
            self._phase = (value, working, late)
            self.emit(events.phase(value, working=working, late=late))

    def _turn(self, ref: VoiceTurnRef, state: str) -> None:
        self.emit(events.turn(ref.id, state, inbox_id=ref.inbox_id, message_id=ref.message_id))

    async def start(self) -> None:
        """Right after ``ready``: the greeting is the first reply and also checks the voice."""
        self._update_phase()
        await self.say_phrase("greeting")

    async def feed_audio(self, pcm: bytes) -> None:
        if not self.closing:
            # After the limit nobody starts a new turn; silence keeps the stream well formed.
            await self.provider.send_audio(bytes(len(pcm)) if self.limit_reason else pcm)

    async def stop(self) -> None:
        """Hang up: cancel the reply and give its final usage up to two seconds to arrive."""
        self.closing = True
        if self.active_response or not self.settled.is_set():
            await self._command("cancel_response")
            with suppress(TimeoutError):
                await asyncio.wait_for(self.settled.wait(), STOP_WAIT_SECONDS)

    async def on_provider_event(self, event) -> None:
        if handler := getattr(self, "_on_" + event.kind, None):
            await handler(event)

    async def _on_user_started(self, _event) -> None:
        self.state, self.interrupted, self.awaiting_since, self.greeting = "user_speaking", True, None, False
        self.transcript = ""  # a new utterance: never pair a call with the previous words
        self.meter.pending_input = True
        self.emit(events.playback_clear())
        self._update_phase()

    async def _on_user_stopped(self, event) -> None:
        self.state = "idle" if self.state == "user_speaking" else self.state
        # A valid end of speech gets the VAD's own reply; a filtered one (turn_invalid) gets nothing.
        self.awaiting_since = None if event.invalid else self.clock()
        self.meter.pending_input = self.meter.pending_input and not event.invalid
        self._update_phase()
        await self.fill_idle()

    async def _on_user_transcript(self, event) -> None:
        self.transcript = event.text  # becomes VoiceTurn.transcript
        await self._on_assistant_transcript(event, "user")

    async def _on_assistant_transcript(self, event, speaker: str = "assistant") -> None:
        if self.debug:  # QA only: transcripts never reach the log otherwise
            log.info("voice transcript %s text=%s", speaker, event.text)

    async def _on_response_started(self, event) -> None:
        self._claimed, self.requested = self.requested, None
        kind = self._claimed or "model"
        self.active_response, self.response_kind, self.awaiting_since = event.response_id, kind, None
        self.audio_sent, self.audio_bytes, self.first_audio_at = False, 0, None
        self.meter.start(event.response_id)
        self.settled.clear()
        if ref := self._delivery(kind):
            ref.delivery = "delivering"
        if self.state == "user_speaking" or (self.limit_reason and kind != "phrase:limit_reached"):
            # Requested in the instant the user began to talk, or after the limit: never play it.
            self.interrupted = True
            await self._command("cancel_response")
        else:
            self.state, self.interrupted = "responding", False
            if kind.startswith("phrase:"):
                self.emit(events.phrase(kind.split(":", 1)[1]))
        self._update_phase()

    async def _on_audio(self, event) -> None:
        # Generated audio may be billed even when the client never plays it.
        self.meter.audio(event.response_id or self.active_response, event.audio, event.event_id)
        if self.closing or self.interrupted or (event.response_id and event.response_id != self.active_response):
            return
        self.emit(event.audio)
        self.audio_bytes += len(event.audio)
        if not self.audio_sent:
            self.audio_sent, self.first_audio_at = True, self.clock()
            self._update_phase()

    async def _on_tool_call(self, event) -> None:
        if not event.call_id or event.call_id in self._seen_calls:
            return
        self._seen_calls.add(event.call_id)
        if event.name != ASSISTANT_ASK:
            await self._command("send_tool_output", event.call_id, {"status": "unknown_tool"})
            return
        ref = VoiceTurnRef(id=generate_id(), provider_call_id=event.call_id, text=event.text,
                           transcript=self.transcript or event.text, requested=self.clock())
        self.transcript = ""
        self.pending_calls[event.call_id] = ref
        self.refs.append(ref)
        task = asyncio.create_task(self._run_turn(ref))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        self._update_phase()

    async def _on_response_done(self, event) -> None:
        self.meter.settle(event.response_id, event.status, event.usage)
        if not self.meter.pending:
            self.settled.set()
        self.emit(events.cost(self.meter.snapshot()))
        if event.response_id and self.active_response and event.response_id != self.active_response:
            return  # an earlier reply, already replaced
        kind, heard = self.response_kind, self.audio_sent
        self.active_response = self.response_kind = self._claimed = None
        self.state = "idle" if self.state == "responding" else self.state  # still user_speaking if interrupted
        if kind == "phrase:greeting":
            self.greeting = False
        elif kind == "phrase:limit_reached":
            self.limit_done.set()
        elif kind and kind.startswith("delivery:"):
            await self._delivered(kind, heard)
        self._update_phase()
        await self.fill_idle()

    async def _on_provider_error(self, event) -> None:
        log.info("voice provider error code=%s reason=%s requested=%s", event.code, event.reason, self.requested)
        if event.reason == "voice_unsupported":
            self.failed.set()  # only the greeting can reveal it; the call ends as provider_error
            return
        if (event.reason == "active_response" and self.requested is None and self._claimed
                and self._claimed == self.response_kind):
            # Our request lost to a VAD reply created in the same instant, which we took for ours.
            self.requested, self.response_kind, self._claimed = self._claimed, "model", None
            if self.limit_reason:
                await self._command("cancel_response")
        if event.reason == "duplicate_output" or self.requested is None:
            return
        kind, self.requested = self.requested, None
        ref = self._delivery(kind)
        if kind == "phrase:greeting":
            self.greeting = False
        elif ref is not None and event.reason == "active_response":
            ref.delivery = "queued"  # retried after the next response.done
        elif ref is not None:  # refused for another reason: say the result is in the text
            self.deliveries.remove(ref)
            ref.delivery, ref.status = "done", "failed" if ref.status == "ok" else ref.status
            self._turn(ref, "failed")
            await self.link.done(ref, _outcome(ref))
            await self.say_phrase("result_in_text")
        self._update_phase()

    async def _run_turn(self, ref: VoiceTurnRef) -> None:
        try:
            await self.link.start(ref)
            self._turn(ref, "accepted")
            result = await self.link.wait(ref, elapsed=self.clock() - ref.requested, on_message=self._on_message)
        except Exception as exc:  # not accepted at all, or the wait broke: say so either way
            log.warning("voice turn failed turn=%s accepted=%s error=%s", ref.id, bool(ref.inbox_id),
                        type(exc).__name__)
            result = {"status": "failed",
                      "speech": phrases.speech_text("failed" if ref.inbox_id else "unavailable", self.lang)}
        if result is not None:
            await self._settle(ref, result)

    def _on_message(self, ref: VoiceTurnRef) -> None:
        if ref.provider_call_id in self.pending_calls:
            self._turn(ref, "working")

    async def _settle(self, ref: VoiceTurnRef, result: dict) -> None:
        self.pending_calls.pop(ref.provider_call_id, None)
        ref.status, ref.speech, ref.settled = result["status"], result["speech"], self.clock()
        if self.closing:
            return
        # Any time, in any state; the provider refuses a second output for this call_id.
        await self._command("send_tool_output", ref.provider_call_id, {"status": ref.status, "speech": ref.speech})
        if ref.status != "ok":
            self._turn(ref, ref.status)
        ref.delivery = "queued"
        self.deliveries.append(ref)
        self._update_phase()
        await self.fill_idle()

    async def _delivered(self, kind: str, heard: bool) -> None:
        ref = self._delivery(kind)
        if ref is None:
            return
        if not heard and ref.attempts < DELIVERY_ATTEMPTS and not self.closing:
            ref.delivery = "queued"  # nobody heard any of it: read it at the next idle window
            return
        self.deliveries.remove(ref)
        ref.delivery, ref.finished = "done", self.clock()
        if ref.status == "ok":
            self._turn(ref, "delivered")
        await self.link.done(ref, _outcome(ref, heard=heard), delivered=heard)

    def _delivery(self, kind: str | None) -> VoiceTurnRef | None:
        turn_id = kind.split(":", 1)[1] if kind and kind.startswith("delivery:") else None
        return next((ref for ref in self.deliveries if ref.id == turn_id), None)

    def _awaiting_reply(self) -> bool:
        return self.awaiting_since is not None and self.clock() - self.awaiting_since < REPLY_GRACE_SECONDS

    def _can_inject(self) -> bool:
        return (self.state == "idle" and self.requested is None and self.active_response is None
                and not self.closing and not self._awaiting_reply())

    async def fill_idle(self) -> None:
        """After every change and every second: at most one reply, results before reassurance."""
        if self.awaiting_since is not None and not self._awaiting_reply():
            self.awaiting_since = None
            self._update_phase()
        if self.limit_reason:
            if not self.limit_done.is_set():
                await self.say_phrase("limit_reached")
            return
        ref = self.deliveries[0] if self.deliveries else None
        if ref is not None and ref.delivery == "queued" and self._can_inject():
            ref.delivery, ref.attempts = "creating", ref.attempts + 1
            self.requested = f"delivery:{ref.id}"
            await self._create(phrases.delivery_instructions(ref.speech, self.lang) if ref.status == "ok"
                               else phrases.notice_instructions(ref.speech, self.lang))
            return
        now = self.clock()
        due = [ref for ref in self.pending_calls.values() if not ref.late and now - ref.requested >= self.late_after]
        if due and self._can_inject():
            for ref in due:  # one phrase covers every turn that became late together
                ref.late = True
                self._turn(ref, "late")
            await self.say_phrase("still_working")
            self._update_phase()

    async def say_phrase(self, key: str) -> bool:
        if not self._can_inject():
            return False
        self.requested = f"phrase:{key}"
        await self._create(phrases.phrase_instructions(key, self.lang))
        return True

    async def begin_limit(self, reason: str, elapsed: float) -> None:
        """Time is up: stop the current reply, then say ``limit_reached`` in the next idle window."""
        if self.limit_reason:
            return
        self.limit_reason = reason
        self.emit(events.limit(reason, elapsed))
        if self.active_response and self.response_kind != "phrase:limit_reached":
            await self._command("cancel_response")
        await self.fill_idle()

    def playback_left(self) -> float:
        """Roughly how long the client still plays the last reply's audio."""
        played = self.clock() - (self.first_audio_at if self.first_audio_at is not None else self.clock())
        return min(max(self.audio_bytes / OUTPUT_BYTES_PER_SECOND + PLAYBACK_MARGIN_SECONDS - played, 0.0), 8.0)

    async def close(self) -> None:
        """After the call: stop observing turns (the assistant keeps working) and record outcomes."""
        self.closing = self.link.closed = True
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=CLOSE_WAIT_SECONDS)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for ref in self.refs:
            if ref.delivery != "done":  # still working (its result goes to the conversation) or never read out
                await self.link.done(ref, _outcome(ref, pending=ref.provider_call_id in self.pending_calls))

    async def _command(self, name: str, *args) -> bool:
        """A failure means the provider is gone; its pump ends the call."""
        try:
            await getattr(self.provider, name)(*args)
            return True
        except Exception as exc:
            log.warning("voice provider %s failed error=%s", name, type(exc).__name__)
            return False

    async def _create(self, instructions: str) -> None:
        if not await self._command("create_response", instructions):
            self.requested = None
