"""One call's state machine between the client, the front desk and the personal assistant.

Rules: docs/VOICE_CALL_SPEC.md §4.2, docs/VOICE_CALL_BACKEND.md §8 and the
second round, docs/ASSISTANT_VOICE_FIX_PLAN.md §5. Measured against the
provider: ``response.create`` while a reply is active is refused, and a
second output for one call_id is refused. So every call_id gets exactly one
output (``assistant_ask`` at once; its result comes later as a note, see
voice/turns.py), and a reply of ours (greeting, result, progress, follow-up,
notice) is only requested in an idle window, one at a time, and once the
client has played the reply before it (``reply_gap``).

The provider session itself is kept: after a while without any response a
silent text-only reply keeps it open (voice/upkeep.py), and a session the
provider drops under a live call is replaced like a rotation (``recover``),
the client socket staying open.
"""
import asyncio
import json
import time
from collections import deque
from contextlib import suppress

from core.log import create_logger
from voice import events, phrases
from voice.assistant_link import turn_outcome as _outcome
from voice.meter import CallMeter, OUTPUT_BYTES_PER_SECOND
from voice.prompt import with_sections
from voice.provider import refuses_reply
from voice.transcript import CallTranscript
from voice.turns import ACK_WAIT_SECONDS, TurnsMixin
from voice.upkeep import ContextKeeper

log = create_logger("voice.bridge")

OUTBOX_LIMIT = 1000
# After a valid end of speech the VAD answers by itself; keep out of its way this long.
REPLY_GRACE_SECONDS = 3.0
STOP_WAIT_SECONDS, CLOSE_WAIT_SECONDS = 2.0, 3.0  # final usage after hang-up; turn observers' last step
PLAYBACK_MARGIN_SECONDS = 0.3
# After the greeting has played on the client, the microphone counts again (echo of its last word aside).
GREETING_MARGIN_SECONDS = 0.3
# A reply of ours starts only once the client has played the last one, and this long after: results used to go
# in at response.done, seconds before the client had played the reply, and came out back to back
# (2026-10-09: 72 places with three front-desk lines in a row). None (``reply_gap``): no pacing (tests).
REPLY_GAP_SECONDS = 0.8
# One progress line per request, after ``late_after`` of quiet; the next (another request's) a minute later at
# the earliest. Measured 2026-10-09: three per request, twelve seconds apart, made "稍等…" 53 times in 50 calls.
PROGRESS_GAP_SECONDS, PROGRESS_PER_TURN = 60.0, 1
INSTRUCTIONS_GAP_SECONDS = 2.0  # progress moves fast; the session prompt follows it at most this often
# A response.create neither started nor refused by then is given up: an error naming no client event is no
# longer taken for a refusal, so one that was would otherwise hold every later reply back.
REQUEST_TIMEOUT_SECONDS = 8.0
KEEPALIVE_RETRY_SECONDS = 10.0  # after a silent reply that was refused, before the next one
# A session the provider drops under a live call (2026-10-09: 13 of 62 calls ended that way): a fresh one,
# two attempts about a second apart within 20 s, three times per call at most.
RECOVERY_ATTEMPTS, RECOVERY_PAUSE_SECONDS, RECOVERY_WINDOW_SECONDS, RECOVERIES_PER_CALL = 2, 1.0, 20.0, 3
UNSENT_AUDIO_BYTES = events.INPUT_SAMPLE_RATE * 2 * 2  # the microphone's last two seconds while no session listens


class Bridge(TurnsMixin):
    def __init__(self, provider, link, *, lang: str = "zh", late_after: float = 20.0, clock=time.monotonic,
                 debug_transcripts: bool = False, scope=None, progress=None, instructions: str = "",
                 opener=None, summarizer=None, wall_clock=None, rates=None, price_date=None,
                  recap: bool = True, tell_reports: bool = True, detail: str = "brief", first_call: bool = False,
                 keepalive_after: float | None = None, reply_gap: float | None = None):
        self.provider, self.link, self.lang = provider, link, lang
        # The user's call settings (assistant/profile.py): whether the greeting recaps, whether task results
        # nobody asked about are told, and how much the assistant says in a reply meant for the phone.
        self.recap, self.tell_reports, self.detail = recap, tell_reports, detail
        self.first_call = first_call
        self.late_after, self.clock, self.debug = late_after, clock, debug_transcripts
        # scope: whose call (direct reads); progress: the main session's live steps; opener: a fresh session;
        # keepalive_after: seconds without a response before a silent one (upkeep.keepalive_seconds), None: never;
        # reply_gap: silence after the client has played a reply before one of ours starts, None: no pacing.
        self.scope, self.progress, self.opener, self.reply_gap = scope, progress, opener, reply_gap
        self.call_id = scope.call_id if scope is not None else ""
        self.state = "idle"                     # idle / user_speaking / responding
        self.active_response: str | None = None
        self.interrupted = False                # the active reply's audio is dropped
        self.pending_calls = {}                 # provider call_id → VoiceTurnRef, until its result is in
        self.deliveries = deque()               # results waiting to be told, oldest first
        # model / phrase:<key> / delivery:<turn_id> / progress / followup / aside
        self.response_kind: str | None = None
        self.requested: str | None = None       # response.create sent, response.created not seen
        self.next_phrase: str | None = None     # a fixed notice for the next idle window
        self.outbox: asyncio.Queue = asyncio.Queue(maxsize=OUTBOX_LIMIT)
        self.overflow, self.failed, self.limit_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.settled = asyncio.Event()          # set: no started reply still owes its usage
        self.settled.set()
        self.meter = CallMeter(rates, price_date=price_date)
        self.greeting, self.closing, self.limit_reason = True, False, None
        self.greeted = asyncio.Event()          # set: the greeting was said in full, refused or cut
        self.greeting_bytes = 0                 # its audio, to know when the client has played it
        self.answered_at: float | None = None   # when the client was told the call is answered
        self.rotating = self.swapping = False   # a fresh session is being prepared / swapped in
        self.recovering = False                 # the provider dropped the session: a fresh one is being opened
        self.recoveries, self.session = 0, 0    # sessions recovered / swapped in (rotation or recovery) so far
        self.unrecoverable = ""                 # an error a fresh session would get too (key, account)
        self._unsent: deque[bytes] = deque()    # microphone audio while no session listens (bounded)
        self._unsent_bytes = 0
        self._rotation: asyncio.Task | None = None
        self._requested_at: float | None = None
        self._keepalives: dict[str, set[str]] = {}  # silent replies of ours → their output items
        self._keepalive_at: float | None = None
        self._keepalive_prompt = False          # Audio said the conversation has no user message: prompt first
        self.awaiting_since: float | None = None
        self.audio_sent, self.audio_bytes, self.first_audio_at, self.started_at = False, 0, None, None
        self._playing_until: float | None = None  # when the client will have played all audio sent so far
        self._wake: asyncio.TimerHandle | None = None  # a look again once it has, for a reply held back
        self.seq = self.started_seq = 0         # provider events so far; at the active reply's start
        self.quiet_since, self.last_progress = clock(), None
        self.utterance = ""                     # latest human transcript; never parsed as business instructions
        self.refs = []                          # every assistant_ask of the call
        self.spoken = CallTranscript(now=wall_clock)
        self.keeper = ContextKeeper(self.spoken, clock=clock, summarizer=summarizer, protected=self._protected_items,
                                    keepalive_after=keepalive_after)
        self.base_instructions = self._sent_instructions = instructions
        self._instructions_at: float | None = None
        self._seen_calls: set[str] = set()
        self._claimed: str | None = None        # the request the active reply was taken to answer
        self._tasks: set[asyncio.Task] = set()
        self._idle = asyncio.Lock()             # one decision about the next reply at a time
        self._phase = None
        self._init_turns()

    def emit(self, item) -> None:
        """Queue for the client; after hang-up the socket sends only the final events itself."""
        if self.closing:
            return
        try:
            self.outbox.put_nowait(item)
        except asyncio.QueueFull:
            self.overflow.set()  # the client stopped reading; the socket ends the call

    def _update_phase(self) -> None:
        # A turn is working until its result has been told; only the front desk's own replies "think".
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

    async def start(self) -> None:
        """While the client still rings: a free greeting from the session's facts; it also checks the voice.

        The call is answered (``ready``) once it is made, so it plays in one
        piece instead of starting while the model is still producing it.
        """
        self._update_phase()
        if not await self.say_phrase("greeting", phrases.greeting_instructions(self.lang, recap=self.recap, first_call=self.first_call)):
            self._greeting_over()

    def answered(self) -> None:
        """The client was just told the call is answered: it plays the greeting made beforehand now."""
        self.answered_at = self.clock()
        if self.audio_bytes:  # the greeting's audio so far, queued while it rang: it starts playing now
            self._playing_until = self.answered_at + self.audio_bytes / OUTPUT_BYTES_PER_SECOND

    def _greeting_over(self) -> None:
        self.greeting = False
        self.greeted.set()

    def _holding_input(self) -> bool:
        """While the greeting plays: a speakerphone hears it again, and the VAD would cut it off as a barge-in."""
        if self.answered_at is None:
            return False
        if self.greeting:
            return True
        if not self.greeting_bytes:
            return False  # refused or cut: nothing is playing
        playing = self.greeting_bytes / OUTPUT_BYTES_PER_SECOND + GREETING_MARGIN_SECONDS
        return self.clock() < self.answered_at + playing

    async def feed_audio(self, pcm: bytes) -> None:
        """Never raises: while no session listens (a dropped one, a fresh one opening) the latest two seconds
        are kept for the next session, and the provider pump recovers the call or ends it."""
        if self.closing:
            return
        # After the limit nobody starts a new turn, and the greeting is never cut off by its own echo;
        # silence keeps the stream well formed.
        frame = bytes(len(pcm)) if self.limit_reason or self._holding_input() else pcm
        if self.recovering:
            self._keep_unsent(frame)
            return
        try:
            await self.provider.send_audio(frame)
        except Exception as exc:  # the session is gone; its pump notices
            if not self._unsent:
                log.info("voice call=%s audio not sent error=%s", self.call_id, type(exc).__name__)
            self._keep_unsent(frame)
            return
        if self._unsent and not self.recovering:
            self._drop_unsent()  # the session took audio again: what it missed is stale now

    def _keep_unsent(self, frame: bytes) -> None:
        self._unsent.append(frame)
        self._unsent_bytes += len(frame)
        while self._unsent_bytes > UNSENT_AUDIO_BYTES:
            self._unsent_bytes -= len(self._unsent.popleft())

    def _drop_unsent(self) -> None:
        self._unsent.clear()
        self._unsent_bytes = 0

    async def stop(self) -> None:
        """Hang up: cancel the reply and give its final usage up to two seconds to arrive."""
        self.closing = True
        if self.active_response or not self.settled.is_set():
            await self._command("cancel_response")
            with suppress(TimeoutError):
                await asyncio.wait_for(self.settled.wait(), STOP_WAIT_SECONDS)

    async def on_provider_event(self, event) -> None:
        self.seq += 1
        if handler := getattr(self, "_on_" + event.kind, None):
            await handler(event)

    async def _on_user_started(self, event) -> None:
        self.state, self.interrupted, self.awaiting_since = "user_speaking", True, None
        if self.greeting:
            self._greeting_over()
        self.utterance = ""  # a new utterance: never pair a call with the previous words
        self._last_user_item = event.item_id or ""
        self.quiet_since = self.clock()
        self.meter.pending_input = True
        self.emit(events.playback_clear())
        self._playing_until = None  # the client has stopped playing
        self._update_phase()

    async def _on_user_stopped(self, event) -> None:
        self.state = "idle" if self.state == "user_speaking" else self.state
        # A valid end of speech gets the VAD's own reply; a filtered one (turn_invalid) gets nothing.
        self.awaiting_since = None if event.invalid else self.clock()
        self.quiet_since = self.clock()
        self.meter.pending_input = self.meter.pending_input and not event.invalid
        self._update_phase()
        await self.fill_idle()

    async def _on_user_transcript(self, event) -> None:
        self.utterance = event.text  # becomes VoiceTurn.transcript
        if self._said("user", event.text, event.item_id):
            self.heard_count += 1
            if event.item_id:
                self._user_utterances[event.item_id] = self.heard_count
            self.keeper.user_turn()

    async def _on_assistant_transcript(self, event) -> None:
        if (event.response_id or self.active_response) in self._keepalives or (event.response_id or self.active_response) in self._unverified_replies:
            return  # a silent reply's "嗯": never part of the call
        self._said("assistant", event.text, event.item_id)
        if event.response_id:
            self._replies[event.response_id] = self._replies.get(event.response_id, "") + event.text

    def _said(self, speaker: str, text: str, item_id: str = "") -> bool:
        added = self.spoken.add(speaker, text, item_id)
        if added and self.debug:  # QA only: transcripts never reach the log otherwise
            log.info("voice transcript call=%s %s text=%s", self.call_id, speaker, " ".join(text.split()))
        return added

    async def _on_item_created(self, event) -> None:
        if event.role == "assistant" and self.response_kind == "keepalive":
            self._keepalives.setdefault(self.active_response or "", set()).add(event.item_id)
        if any(event.item_id in items for items in self._keepalives.values()):
            return  # a silent reply's text: deleted once it is done, never summarized
        self.spoken.item_created(event.item_id, event.role, event.item_type)
        if event.role == "user" and event.item_type == "message" and not event.text:
            self._last_user_item = event.item_id  # the user's audio; our notes carry text
        self._match_note(event)

    async def _on_item_deleted(self, event) -> None:
        self.spoken.item_deleted(event.item_id)

    async def _on_output_item(self, event) -> None:
        if (items := self._keepalives.get(event.response_id or "")) is not None and event.item_id:
            items.add(event.item_id)

    async def _on_response_started(self, event) -> None:
        self._claimed, self.requested = self.requested, None
        kind = self._claimed or "model"
        if kind == "keepalive":
            await self._keepalive_started(event)
            return
        self.active_response, self.response_kind, self.awaiting_since = event.response_id, kind, None
        self.audio_sent, self.audio_bytes, self.first_audio_at = False, 0, None
        self.started_at = self.quiet_since = self.clock()
        self.started_seq = self.seq
        self.meter.start(event.response_id)
        self.settled.clear()
        if ref := self._delivery(kind):
            ref.delivery = "delivering"
        if kind in ("model", "followup") and event.response_id:
            self._answering[event.response_id] = self._last_user_item
            self._ack_at = None  # the model speaks anyway, with every tool output in view
        if kind == "model" and not self._direct:
            self.followup_due = False  # the tool outputs are in the conversation: this reply answers them
            self._tool_receipts.clear()
        if self.state == "user_speaking" or (self.limit_reason and kind != "phrase:limit_reached"):
            # Requested in the instant the user began to talk, or after the limit: never play it.
            self.interrupted = True
            await self._command("cancel_response")
        else:
            self.state, self.interrupted = "responding", False
            if kind.startswith("phrase:") or kind == "progress":
                self.emit(events.phrase(kind.split(":", 1)[-1]))
        self._update_phase()

    async def _on_audio(self, event) -> None:
        # Generated audio may be billed even when the client never plays it.
        self.meter.audio(event.response_id or self.active_response, event.audio, event.event_id)
        if self.closing or self.interrupted or (event.response_id and event.response_id != self.active_response) \
                or (event.response_id or self.active_response) in self._keepalives \
                or (event.response_id or self.active_response) in self._unverified_replies:
            return
        self.emit(event.audio)
        self.audio_bytes += len(event.audio)
        now = self.clock()  # the client plays what it gets in order: after what it still has, or from now
        self._playing_until = max(self._playing_until or now, now) + len(event.audio) / OUTPUT_BYTES_PER_SECOND
        if not self.audio_sent:
            self.audio_sent, self.first_audio_at = True, now
            self._update_phase()

    async def _on_response_done(self, event) -> None:
        self.keeper.responded()  # finished, cancelled or silent: the provider's idle clock starts again
        self.meter.settle(event.response_id, event.status, event.usage)
        if not self.meter.pending:
            self.settled.set()
        if (event.response_id or "") in self._keepalives:
            await self._keepalive_done(event)
            return
        self.emit(events.cost(self.meter.snapshot()))
        if event.response_id and self.active_response and event.response_id != self.active_response:
            return  # an earlier reply, already replaced
        kind, heard = self.response_kind, self.audio_sent
        self.active_response = self.response_kind = self._claimed = None
        self.state = "idle" if self.state == "responding" else self.state  # still user_speaking if interrupted
        self.quiet_since = self.clock()
        self.keeper.response_done(event.usage)
        if kind == "phrase:greeting":
            self.greeting_bytes = self.audio_bytes if heard else 0
            self._greeting_over()
        elif kind == "phrase:limit_reached":
            self.limit_done.set()
        said = self._replies.get(event.response_id or "", "")
        if kind and kind.startswith("delivery:"):
            await self._delivered(self._delivery(kind), heard, said)
        for ref in [ref for ref in self.deliveries if ref.covered_by and ref.covered_by == event.response_id]:
            await self._delivered(ref, heard, said)
        if heard and event.response_id:
            self._heard_replies.append(event.response_id)
        if event.response_id in self._acks:
            self._acks.discard(event.response_id)
            if not heard and event.status == "completed":
                # It called assistant_ask without a word: acknowledged unless the result comes first.
                self._ack_at = self.clock() + ACK_WAIT_SECONDS
        self._replies.pop(event.response_id or "", None)
        self._unverified_replies.discard(event.response_id or "")
        self._answering.pop(event.response_id or "", None)
        self._update_phase()
        await self.fill_idle()

    async def _on_provider_error(self, event) -> None:
        log.info("voice provider error call=%s code=%s reason=%s param=%s requested=%s message=%s", self.call_id,
                 event.code or "-", event.reason, event.param or "-", self.requested, event.text or "-")
        if event.reason == "voice_unsupported":
            self.failed.set()  # only the greeting can reveal it; the call ends as provider_error
            self.greeted.set()
            return
        if event.reason == "denied":
            self.unrecoverable = event.code or event.reason  # a fresh session would be refused the same way
        if event.reason == "session_closed":
            await self._command("close")  # its stream ends now; the provider pump recovers the call
            return
        if not refuses_reply(event.reason, event.param):
            # An item (Audio keeps max_history_turns: a tidy-up delete can miss), the session prompt, a tool
            # output: nothing we asked for was refused, and a delivery waiting for its reply keeps waiting.
            return
        if (event.reason == "active_response" and self.requested is None and self._claimed
                and self._claimed == self.response_kind):
            # Our request lost to a VAD reply created in the same instant, which we took for ours.
            if self._claimed == "keepalive":
                self._unsilence()
            self.requested, self.response_kind, self._claimed = self._claimed, "model", None
            self._cover(self.active_response)
            if self.limit_reason:
                await self._command("cancel_response")
        if self.requested is None:
            return
        kind, self.requested = self.requested, None
        if kind == "phrase:greeting":
            self._greeting_over()
        elif kind == "keepalive" and event.reason == "no_input":
            self._keepalive_prompt = True  # Audio: no user message left in the conversation
        elif ref := self._delivery(kind):
            await self._refused(ref, event.reason)
        self._update_phase()  # a refused follow-up or progress reply is not retried: the reply that won answers

    def _awaiting_reply(self) -> bool:
        return self.awaiting_since is not None and self.clock() - self.awaiting_since < REPLY_GRACE_SECONDS

    def _can_inject(self) -> bool:
        return (self.state == "idle" and self.requested is None and self.active_response is None
                and not self.closing and not self.swapping and not self.recovering and not self._awaiting_reply())

    async def fill_idle(self) -> None:
        """After every change and every second: at most one reply of ours, answers before reassurance, each once
        the client has played the last one; with nothing to say for a while, a silent one that keeps the provider
        session open."""
        if self.awaiting_since is not None and not self._awaiting_reply():
            self.awaiting_since = None
            self._update_phase()
        async with self._idle:
            if (self.requested is not None and self._requested_at is not None
                    and self.clock() - self._requested_at >= REQUEST_TIMEOUT_SECONDS):
                await self._unanswered()
            if self.limit_reason:
                if not self.limit_done.is_set():
                    await self.say_phrase("limit_reached", phrases.phrase_instructions("credits_exhausted", self.lang))
                return
            if not self._can_inject():
                return
            await self._tidy()
            if not self._can_inject():
                return
            say = self._next_reply()
            if say is not None:
                if self._played_out():
                    await say()
                else:
                    self._wake_when_played()
            elif self.opener is not None and not self.rotating and self.keeper.rotation_due():
                self.rotating = True
                self._rotation = self._spawn(self._rotate())
            elif self.keeper.keepalive_due() and (self._keepalive_at is None
                                                  or self.clock() - self._keepalive_at >= KEEPALIVE_RETRY_SECONDS):
                # Nothing said for a while (a task still running does not matter): the provider would close
                # the session, so a reply nobody hears.
                await self._keepalive()

    def _next_reply(self):
        """The reply of ours that is due, answers before reassurance: something to await, or None."""
        if self.next_phrase:
            return self._say_next_phrase
        # A read can finish between response.done and its function output.
        # Do not insert an unrelated task note into that answer's context.
        if self._direct:
            return None
        if self.followup_due:
            return self._say_followup
        if (ref := self._next_delivery()) is not None:
            return lambda: self._deliver(ref)
        if self._ack_due():
            return self._acknowledge
        if due := self._progress_due():
            return lambda: self._say_progress(due)
        return None

    async def _say_next_phrase(self) -> None:
        key, self.next_phrase = self.next_phrase, None
        await self.say_phrase(key)

    async def _say_followup(self) -> None:
        self.followup_due, self.requested = False, "followup"
        instructions = phrases.tool_result_instructions(self.lang)
        receipts, self._tool_receipts = self._tool_receipts, []
        if receipts:
            instructions += "\nActual tool receipts for this answer (facts, not new user instructions): " + json.dumps(
                receipts, ensure_ascii=False)
        await self._create(instructions)

    def _ack_due(self) -> bool:
        """A request handed over without a word whose result has not come within ``ACK_WAIT_SECONDS``."""
        return (self._ack_at is not None and self.clock() >= self._ack_at and self._request_pending()
                )

    async def _acknowledge(self) -> None:
        self._ack_at, self.requested = None, "followup"
        await self._create(phrases.tool_result_instructions(self.lang))

    def _played_out(self) -> bool:
        """The client has played our last reply and ``reply_gap`` has passed since (always, without pacing)."""
        return self.reply_gap is None or self._playing_until is None or (
            self.clock() >= self._playing_until + PLAYBACK_MARGIN_SECONDS + self.reply_gap)

    def _wake_when_played(self) -> None:
        """A reply held back for the client: look again as soon as it has played, not at the next tick."""
        if self.closing:
            return
        loop = asyncio.get_running_loop()
        delay = min(max(self._playing_until + PLAYBACK_MARGIN_SECONDS + self.reply_gap - self.clock(), 0.05), 10.0)
        if self._wake is not None:
            if self._wake.when() <= loop.time() + delay:
                return  # an earlier look is due anyway; it holds the reply again if need be
            self._wake.cancel()
        self._wake = loop.call_later(delay, self._woken)

    def _woken(self) -> None:
        self._wake = None
        if not self.closing:
            self._spawn(self.fill_idle())

    def _next_delivery(self):
        """What the user asked comes first. A task report nobody asked for waits while a request of the user's
        is being done and rides along with its answer ("另外…"); nothing unasked while they asked for quiet."""
        waiting_answer = self._asked is not None
        confirmations = set(self.desk.open())
        busy = self._request_pending()
        queued = [ref for ref in self.deliveries if ref.delivery == "queued"
                  and not (ref.questions and (waiting_answer or self.pending_calls or confirmations))
                  and not (ref.reason == "card_notice" and confirmations - {c["card_id"] for c in ref.cards})
                  and not (busy and ref.report is not None and not ref.asked and not ref.cards and not ref.questions)]
        if confirmation := next((ref for ref in queued if ref.cards), None):
            return confirmation
        return next((ref for ref in queued if ref.report is None), queued[0] if queued else None)

    async def _keepalive(self) -> None:
        """A text-only reply nobody hears, so the provider keeps the session (voice/upkeep.py)."""
        # Audio refuses a reply without a user message in the conversation (a fresh session, say).
        empty = self._keepalive_prompt or not any(item.role == "user" for item in self.spoken.items)
        log.debug("voice call=%s keepalive after=%.0fs empty=%s", self.call_id,
                  self.clock() - self.keeper.responded_at, empty)
        self._keepalive_at = self.clock()
        self.requested, self._requested_at = "keepalive", self.clock()
        if not await self._command("keepalive", empty):
            self.requested = None

    async def _keepalive_started(self, event) -> None:
        """Never told: no phase, no transcript, no turn or follow-up logic, nothing for the client."""
        self.active_response, self.response_kind = event.response_id, "keepalive"
        self.started_at, self.started_seq = self.clock(), self.seq  # should it turn out to be a VAD reply
        self.meter.start(event.response_id)  # billed like any reply
        self.settled.clear()
        self._keepalives.setdefault(event.response_id or "", set())
        if self.state == "user_speaking" or self.limit_reason:
            await self._command("cancel_response")

    async def _keepalive_done(self, event) -> None:
        items = self._keepalives.pop(event.response_id or "", set())
        if not event.response_id or event.response_id == self.active_response:
            self.active_response = self.response_kind = self._claimed = None
        for item_id in items:  # out of the conversation: no later reply reads "嗯"
            await self._command("delete_item", item_id)
        await self.fill_idle()

    def _unsilence(self) -> None:
        """What we took for our silent reply was the VAD's answer to the user (our request lost the race)."""
        self._keepalives.pop(self.active_response or "", None)
        self.audio_sent, self.audio_bytes, self.first_audio_at, self.awaiting_since = False, 0, None, None
        self.quiet_since = self.clock()
        if self.active_response:
            self._answering[self.active_response] = self._last_user_item
        if not self._direct:
            self.followup_due = False
        if self.state != "user_speaking":
            self.state, self.interrupted = "responding", False

    async def _unanswered(self) -> None:
        """Neither started nor refused in time: given up (a result is told again later, three times at most)."""
        kind, self.requested = self.requested, None
        log.info("voice call=%s request unanswered kind=%s", self.call_id, kind)
        if kind == "phrase:greeting":
            self._greeting_over()
        elif ref := self._delivery(kind):
            await self._refused(ref, "unanswered")
        self._update_phase()

    async def _tidy(self) -> None:
        """While idle: delete what a summary covers, then bring the session prompt up to date."""
        for item_id in self.keeper.due_deletions():
            await self._delete(item_id)
        await self._sync_instructions()

    async def _delete(self, item_id: str) -> None:
        self.spoken.item_deleted(item_id)
        await self._command("delete_item", item_id)

    async def rebase(self, instructions: str, *, tell_reports: bool | None = None, detail: str | None = None) -> None:
        """The user changed their assistant profile mid-call (a new name, how to address them, how to talk):
        the session prompt is rebuilt from it and sent now, and every fresh session keeps it."""
        if tell_reports is not None:
            self.tell_reports = tell_reports
        if detail is not None:
            self.detail = detail
        if self.closing or not instructions:
            return
        self.base_instructions = instructions
        await self._sync_instructions(force=True)

    def _instructions(self) -> str:
        section = self.progress.section(list(self.pending_calls.values())) if self.progress is not None else ""
        return with_sections(self.base_instructions, call_so_far=self.keeper.summary, progress=section,
                             last_lines=self.keeper.carried)

    async def _sync_instructions(self, *, force: bool = False) -> None:
        if not self.base_instructions:
            return
        text, now = self._instructions(), self.clock()
        if text == self._sent_instructions or (not force and self._instructions_at is not None
                                               and now - self._instructions_at < INSTRUCTIONS_GAP_SECONDS):
            return
        if await self._command("update_instructions", text):
            self._sent_instructions, self._instructions_at = text, now

    def _progress_due(self) -> list:
        """The user's requests to say a progress line about now: quiet for ``late_after``, each still being done
        after that long and not yet given its one line, the last line a minute ago at least; none while the user
        asked for quiet, none for a quiet "remember this"."""
        now = self.clock()
        if (now - self.quiet_since < self.late_after
                or (self.last_progress is not None and now - self.last_progress < PROGRESS_GAP_SECONDS)):
            return []
        return [ref for ref in self.pending_calls.values() if ref.lane != "remember"
                and ref.progress < PROGRESS_PER_TURN and now - ref.requested >= self.late_after]

    async def _say_progress(self, due: list) -> None:
        """One sentence on what is really going on."""
        for ref in due:
            ref.progress += 1
            if not ref.late:
                ref.late = True
                self._turn(ref, "late")
        self.last_progress = self.clock()
        await self._sync_instructions(force=True)
        self.requested = "progress"
        await self._create(phrases.progress_instructions(self.progress.step if self.progress else "", self.lang))
        self._update_phase()

    async def _rotate(self) -> None:
        """A fresh provider session with the memo, swapped in while nothing is going on.

        The memo is made while the call goes on; only opening the new session
        (a fraction of a second) holds our replies back, and only if the call
        is quiet at that moment; otherwise it is tried again a little later.
        """
        new, instructions = None, ""
        try:
            await self.keeper.summary_now()
            if self.closing or self.recovering or not self._quiet_for_swap():
                raise RuntimeError("busy")
            self.swapping = True  # from here on nothing new goes into the old session
            self.keeper.carry_last_lines()
            instructions = self._instructions()
            new = await self.opener(instructions)
        except Exception as exc:  # cancelled instead when the session drops meanwhile (``recover``)
            log.info("voice call=%s rotation postponed error=%s", self.call_id, type(exc).__name__)
        if new is None or self.closing or self.recovering or not self._quiet_for_swap():
            if new is not None:
                await asyncio.shield(new.close())
            self.keeper.carried = "" if self.swapping else self.keeper.carried  # the old session keeps its own
            self.keeper.rotation_failed()
            self.rotating = self.swapping = False
            return
        old = self._swap_in(new, instructions)
        self.rotating = self.swapping = False
        log.info("voice call=%s provider session rotated", self.call_id)
        await asyncio.shield(old.close())

    def _swap_in(self, new, instructions: str):
        """A fresh session takes the call over (rotation or recovery); its conversation starts empty."""
        old, self.provider = self.provider, new
        self.session += 1
        self.spoken.reset_items()
        for ref in self.deliveries:  # a note in the old session is gone: inject it again when told
            ref.note_item, ref.note_pending, ref.note_at = None, False, None
        self._keepalives.clear()
        self._unverified_replies.clear()
        self._tool_receipts.clear()
        self._keepalive_prompt = False
        self.keeper.rotated()
        self._sent_instructions, self._instructions_at = instructions, self.clock()
        return old

    def _quiet_for_swap(self) -> bool:
        return (self.state == "idle" and self.active_response is None and self.requested is None
                and not self.followup_due and not self._direct and self._ack_at is None
                and all(ref.delivery == "queued" for ref in self.deliveries))

    async def recover(self, lost) -> bool:
        """The provider closed ``lost``, the session in use, under the call: carry on in a fresh one.

        The rotation's swap without its memo or quiet moment: what was being
        said is settled as cut off, a result nobody heard is told again, and a
        user who was cut off is asked to say it again; otherwise nothing is
        said. False: the call ends as before (not answered yet, hanging up, at
        its limit, refused for good, three drops already, or no fresh session
        within the attempts).
        """
        log.info("voice call=%s provider stream ended code=%s reason=%s last_error=%s", self.call_id,
                 getattr(lost, "close_code", None), getattr(lost, "close_reason", "") or "-",
                 getattr(lost, "last_error", "") or "-")
        why = ("not answered" if self.answered_at is None else "hanging up" if self.closing
               else "limit" if self.limit_reason else "refused " + self.unrecoverable if self.unrecoverable
               else "no opener" if self.opener is None else "too many" if self.recoveries >= RECOVERIES_PER_CALL
               else "")
        if why:
            log.info("voice call=%s provider session lost recovered=no why=%s recoveries=%s", self.call_id, why,
                     self.recoveries)
            return False
        self.recovering = True  # from here on nothing goes to the lost session; the microphone is kept
        try:
            await self._stop_rotation()
            self.swapping = True
            if self.provider is not lost:
                return True  # the rotation swapped a fresh session in just before
            cut_off, speaking = await self._lose_session()
            self.keeper.carry_last_lines()
            instructions = self._instructions()
            new, attempts = await self._reopen(instructions)
            if new is None or self.closing:
                if new is not None:
                    await asyncio.shield(new.close())
                log.info("voice call=%s provider session lost recovered=no attempts=%s recoveries=%s",
                         self.call_id, attempts, self.recoveries)
                return False
            self._swap_in(new, instructions)
            self.recoveries += 1
            await self._resend_unsent(discard=speaking)
            if cut_off and self.next_phrase is None:
                self.next_phrase = "reconnected"
            log.info("voice call=%s provider session recovered attempts=%s recoveries=%s cut_off=%s", self.call_id,
                     attempts, self.recoveries, cut_off)
        finally:
            self.recovering = self.swapping = False
        self._spawn(lost.close())  # already closed by the provider, as a rule; nothing waits for it
        self._update_phase()
        await self.fill_idle()
        return True

    async def _stop_rotation(self) -> None:
        """A rotation under way gives way (it may not even have started); the next one is due as before."""
        if self._rotation is not None and not self._rotation.done():
            self._rotation.cancel()
            await asyncio.gather(self._rotation, return_exceptions=True)
            self.rotating = False

    async def _lose_session(self) -> tuple[bool, bool]:
        """What the dropped session took with it. Returns (cut off, speaking): the user was cut off (mid-
        sentence, waiting for an answer, hearing a reply), and whether mid-sentence. A reply of ours nobody
        heard yet cut nobody off: it is simply said in the new session."""
        kind = self.response_kind if self.active_response else self.requested
        response_id, heard = self.active_response, self.audio_sent and kind != "keepalive"
        speaking = self.state == "user_speaking"
        cut_off = (speaking or self._awaiting_reply() or self.followup_due or bool(self._direct)
                   or (response_id is not None and (heard or kind in ("model", "followup"))))
        said, lost = self._replies.pop(response_id or "", ""), self._delivery(kind)
        covered = [ref for ref in self.deliveries if response_id and ref.covered_by == response_id]
        self.meter.abandon()  # its usage never comes: counted once, from the audio received
        self.settled.set()
        if self.greeting:
            self._greeting_over()
        self.active_response = self.response_kind = self._claimed = self.requested = None
        self.state, self.interrupted, self.awaiting_since, self.followup_due = "idle", False, None, False
        self._ack_at = None  # its handover went with the session's conversation; the result is told as usual
        self._answering.pop(response_id or "", None)
        self._acks.discard(response_id or "")
        for ref in self.deliveries:  # the notes went with the session
            ref.note_item, ref.note_pending, ref.note_at = None, False, None
        if lost is not None and lost.delivery in ("creating", "delivering"):
            await self._delivered(lost, heard and lost.delivery == "delivering", said)
        for ref in covered:
            await self._delivered(ref, heard, said)
        for ref in self.deliveries:  # whatever was on its way waits for the next quiet moment
            if ref.delivery != "queued":
                ref.delivery, ref.riders, ref.covered_by = "queued", None, None
        self._update_phase()
        return cut_off, speaking

    async def _reopen(self, instructions: str):
        """A fresh session: (it, attempt) or (None, attempts made) within the attempts and the window."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + RECOVERY_WINDOW_SECONDS
        for attempt in range(1, RECOVERY_ATTEMPTS + 1):
            if attempt > 1:
                await asyncio.sleep(RECOVERY_PAUSE_SECONDS)
            left = deadline - loop.time()
            if left <= 0 or self.closing:
                return None, attempt - 1
            try:
                return await asyncio.wait_for(self.opener(instructions), left), attempt
            except Exception as exc:  # a timeout too: the next attempt, if any
                log.info("voice call=%s recovery attempt=%s failed error=%s", self.call_id, attempt,
                         type(exc).__name__)
        return None, RECOVERY_ATTEMPTS

    async def _resend_unsent(self, *, discard: bool) -> None:
        """The microphone's audio while no session listened goes to the new one, unless it is the rest of a
        sentence whose start went with the old one (the user is asked to say it again instead)."""
        if discard:
            self._drop_unsent()
        while self._unsent:  # frames arriving meanwhile are kept behind these, in order
            frame = self._unsent.popleft()
            self._unsent_bytes -= len(frame)
            try:
                await self.provider.send_audio(frame)
            except Exception:  # this one is gone too: its pump says so
                self._drop_unsent()

    async def say_phrase(self, key: str, instructions: str | None = None) -> bool:
        if not self._can_inject():
            return False
        self.requested = f"phrase:{key}"
        await self._create(instructions or phrases.phrase_instructions(key, self.lang))
        return True

    async def begin_limit(self, reason: str, elapsed: float) -> None:
        """Time or credits are up: stop the current reply, then say goodbye in the next idle window."""
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
        if self.progress is not None:
            self.progress.stop()
        if self._wake is not None:
            self._wake.cancel()
            self._wake = None
        self.keeper.cancel()
        if self._tasks:
            await asyncio.wait(list(self._tasks), timeout=CLOSE_WAIT_SECONDS)
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for ref in self.refs:
            if ref.delivery != "done":  # still working (its result goes to the conversation) or never told
                await self.link.done(ref, _outcome(ref, pending=ref.provider_call_id in self.pending_calls))

    async def _command(self, name: str, *args) -> bool:
        """A failure means the provider is gone; its pump recovers the call or ends it."""
        try:
            await getattr(self.provider, name)(*args)
            return True
        except Exception as exc:
            log.warning("voice provider %s failed call=%s error=%s", name, self.call_id, type(exc).__name__)
            return False

    async def _create(self, instructions: str | None) -> None:
        # A response-specific prompt must carry the durable role and tool contract too.
        # Providers can treat it as an override, rather than an addition to session.instructions.
        if instructions and self.base_instructions:
            instructions = self._instructions() + "\n\n" + instructions
        self._requested_at = self.clock()
        if not await self._command("create_response", instructions):
            self.requested = None
