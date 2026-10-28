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
from voice import events, phrases, reply_focus
from voice.assistant_link import turn_outcome as _outcome
from voice.meter import CallMeter, OUTPUT_BYTES_PER_SECOND
from voice.prompt import with_sections
from voice.provider import ProviderEvent, refuses_reply
from voice.transcript import CallTranscript
from voice.freshness import FreshnessMixin, work_claim
from voice.playback import PlaybackMixin
from voice.turns import ACK_WAIT_SECONDS, REMOVED_DECISION_SECONDS, TurnsMixin
from voice.upkeep import ContextKeeper
from voice.memory_context import DEGRADE_AFTER_SECONDS, MemoryContextMixin, RecentContext, keeps_call_context

log = create_logger("voice.bridge")

OUTBOX_LIMIT = 1000
# After a valid end of speech the VAD answers by itself; keep out of its way this long.
REPLY_GRACE_SECONDS = 3.0
STOP_WAIT_SECONDS, CLOSE_WAIT_SECONDS = 2.0, 3.0  # final usage after hang-up; turn observers' last step
PLAYBACK_MARGIN_SECONDS = 0.3
# A reply of ours starts only once the client has played the last one, and this long after: results used to go
# in at response.done, seconds before the client had played the reply, and came out back to back
# (2026-10-09: 72 places with three front-desk lines in a row). None (``reply_gap``): no pacing (tests).
REPLY_GAP_SECONDS = 0.8
REPORT_COALESCE_SECONDS = 0.4
# One progress line per request, after ``late_after`` of quiet; the next (another request's) a minute later at
# the earliest. Measured 2026-10-09: three per request, twelve seconds apart, made "稍等…" 53 times in 50 calls.
PROGRESS_GAP_SECONDS, PROGRESS_PER_TURN = 60.0, 1
INSTRUCTIONS_GAP_SECONDS = 2.0  # progress moves fast; the session prompt follows it at most this often
# A response.create neither started nor refused by then is given up: an error naming no client event is no
# longer taken for a refusal, so one that was would otherwise hold every later reply back.
REQUEST_TIMEOUT_SECONDS = 8.0
CANCEL_TIMEOUT_SECONDS = 2.0  # response.cancel must release the slot, or the provider session is replaced
KEEPALIVE_RETRY_SECONDS = 10.0  # after a silent reply that was refused, before the next one
# A session the provider drops under a live call (2026-10-09: 13 of 62 calls ended that way): a fresh one,
# two attempts about a second apart within 20 s, three times per call at most.
RECOVERY_ATTEMPTS, RECOVERY_PAUSE_SECONDS, RECOVERY_WINDOW_SECONDS, RECOVERIES_PER_CALL = 2, 1.0, 20.0, 3
UNSENT_AUDIO_BYTES = events.INPUT_SAMPLE_RATE * 2 * 2  # the microphone's last two seconds while no session listens
STREAMED_KINDS = frozenset({"audio", "assistant_transcribing", "user_transcribing"})  # one per delta; never traced


class Bridge(MemoryContextMixin, FreshnessMixin, PlaybackMixin, TurnsMixin):
    def __init__(self, provider, link, *, lang: str = "zh", late_after: float = 20.0, clock=time.monotonic,
                 debug_transcripts: bool = False, scope=None, progress=None, instructions: str = "",
                 opening_instructions: str | None = None,
                 opener=None, summarizer=None, handoff_checker=None, wall_clock=None, rates=None, price_date=None,
                  recap: bool = True, tell_reports: bool = True, detail: str = "brief", first_call: bool = False,
                 keepalive_after: float | None = None, reply_gap: float | None = None, unified: bool = False,
                 context_sink=None, context_reader=None,
                 prepare_memory=None, refresh_memory=None, prewarm_memory=None, focus=None):
        self.provider, self.link, self.lang = provider, link, lang
        self.unified = unified
        self.shared_context = ""
        self.context_sink = context_sink
        self._context_dirty = False
        self._context_task = None
        self.decision_context = ""
        self._transcript_turns = set()
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
        self._speech_until: float | None = None
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
        self.answered_at: float | None = None   # when the client was told the call is answered
        self.rotating = self.swapping = False   # a fresh session is being prepared / swapped in
        self.recovering = False                 # the provider dropped the session: a fresh one is being opened
        self.recoveries, self.session = 0, 0    # sessions recovered / swapped in (rotation or recovery) so far
        self.unrecoverable = ""                 # an error a fresh session would get too (key, account)
        self._unsent: deque[bytes] = deque()    # microphone audio while no session listens (bounded)
        self._unsent_bytes = 0
        self._rotation: asyncio.Task | None = None
        self._requested_at: float | None = None
        self._cancel_wait = None  # (provider, session generation, response id, deadline)
        self._cancel_wake: asyncio.TimerHandle | None = None
        self._cancel_recovery_provider = None
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
        self.base_instructions = instructions
        self.opening_instructions = opening_instructions
        self._sent_instructions = opening_instructions if opening_instructions is not None else instructions
        self._instructions_at: float | None = None
        self._seen_calls: set[str] = set()
        self._answered_item = ""                # the latest utterance a completed, heard reply answered
        self._carried_note = self._carried_item = ""  # an unanswered utterance carried into a new session
        self._carry_due = False
        self._claimed: str | None = None        # the request the active reply was taken to answer
        self._tasks: set[asyncio.Task] = set()
        self._idle = asyncio.Lock()             # one decision about the next reply at a time
        self._phase = None
        self._init_turns()
        self.focus = focus  # voice/reply_focus.py; none: only exact names place a reply on a decision
        self._init_memory(prepare_memory, refresh_memory, prewarm_memory)
        self._init_freshness(context_reader)
        self._init_playback()
        from voice.handoff import HandoffGuard
        self.handoff = HandoffGuard(self, handoff_checker) if handoff_checker is not None and not unified else None

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
        working = bool(self.pending_calls or any(ref.delivery != "paused" for ref in self.deliveries))
        late = any(ref.late for ref in self.pending_calls.values())
        responding = self.state == "responding"
        value = ("greeting" if self.greeting else "listening" if self.state == "user_speaking"
                 else "speaking" if responding and self.audio_sent and not self.interrupted
                 else "thinking" if ((responding and self.response_kind == "model") or self._awaiting_reply()
                                      or self.followup_due or self._cancel_recovery_provider is not None)
                 else "working" if working else "listening")
        if (value, working, late) != self._phase:
            self._phase = (value, working, late)
            log.debug("voice phase call=%s value=%s state=%s pending=%d", self.call_id, value,
                      self.state, len(self.pending_calls))
            self.emit(events.phase(value, working=working, late=late))

    async def start(self) -> None:
        """An interruptible opening after the client is ready to speak and listen."""
        self._prewarm_memory_connection()
        if self.state == "user_speaking" or self._last_user_item:
            self._greeting_over()
            await self._sync_instructions(force=True)
            return
        self._update_phase()
        if not await self.say_phrase("greeting", phrases.greeting_instructions(
                self.lang, recap=self.recap, first_call=self.first_call, unified=self.unified)):
            self._greeting_over()
            await self._sync_instructions(force=True)

    def answered(self) -> None:
        """The client is ready; microphone input is accepted throughout playback."""
        self.answered_at = self.clock()
        if self.audio_bytes:  # the greeting's audio so far, queued while it rang: it starts playing now
            self._playing_until = self.answered_at + self.audio_bytes / OUTPUT_BYTES_PER_SECOND

    def _greeting_over(self) -> None:
        self.greeting = False
        self.greeted.set()

    async def feed_audio(self, pcm: bytes) -> None:
        """Never raises: while no session listens (a dropped one, a fresh one opening) the latest two seconds
        are kept for the next session, and the provider pump recovers the call or ends it."""
        if self.closing:
            return
        # Only a spent call limit silences input. Acoustic echo cancellation is
        # the capture layer's responsibility; muting playback also mutes real interruptions.
        frame = bytes(len(pcm)) if self.limit_reason else pcm
        if self.recovering or self.provider is self._cancel_recovery_provider:
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
        if event.kind not in STREAMED_KINDS:  # identities and states only, never words
            log.debug("voice provider event call=%s seq=%d kind=%s response=%s item=%s status=%s reason=%s",
                      self.call_id, self.seq, event.kind, event.response_id or "-", event.item_id or "-",
                      event.status or "-", event.reason or "-")
        if await self._memory_event(event):
            return
        if handler := getattr(self, "_on_" + event.kind, None):
            await handler(event)

    async def _on_user_started(self, event) -> None:
        # Newer speech supersedes a carried utterance: its note becomes an
        # earlier line, which a later restricted turn retires like any other.
        self._carried_note, self._carried_item, self._carry_due = "", "", False
        self._answers_again = 0  # the user speaks: a new question, answered at most twice again
        self._changed_work = {}
        self._question_at = self.clock()
        self._memory_new_speech()
        if self.handoff is not None:
            self.handoff.warm()
        already_cut = self.interrupted and self._playing_until is None
        self.state, self.interrupted, self.awaiting_since = "user_speaking", True, None
        if self.greeting:
            self._greeting_over()
        self.utterance = ""  # a new utterance: never pair a call with the previous words
        self._last_user_item = event.item_id or ""
        self.quiet_since = self.clock()
        self.meter.pending_input = True
        self._speech_until = None
        if not already_cut:
            await self._interrupt_playback()
        else:
            await self._sync_instructions(force=True)
        self._update_phase()

    async def _on_user_transcribing(self, event) -> None:
        # Only confirmed user ASR, never ambient audio or an unconfirmed ASR
        # stash. Raw microphone energy also contains speaker echo and noise.
        # A partial stops playback; it cannot create a user item or authorize work.
        if event.item_id == self._last_user_item:
            return  # canonical VAD owns this turn, including ASR arriving after speech_stopped
        playing = self._playing_until is not None and self.clock() < self._playing_until
        if playing and not self.interrupted:
            self._speech_until = self.clock() + REPLY_GRACE_SECONDS
            await self._interrupt_playback(source="partial_transcript")
        elif self._speech_until is not None:
            self._speech_until = self.clock() + REPLY_GRACE_SECONDS

    async def _interrupt_playback(self, *, source: str = "provider_vad") -> None:
        self.interrupted = True
        if source in ("provider_vad", "partial_transcript"):
            self._pause_user_reports()
        if self.greeting:
            self._greeting_over()
        self.emit(events.playback_clear())
        self._update_phase()  # never keep showing "speaking" while awaiting a provider ACK
        log.info("voice interruption call=%s source=%s", self.call_id, source)
        self._playing_until = None  # the client has stopped playing
        await self._cancel_playbacks()
        if self.active_response:
            self._remember_stale_response(self.active_response)
            await self._command("cancel_response")
        # Restore navigation/history before the response to this real utterance.
        # The opening must not read old task summaries when recap is disabled.
        await self._sync_instructions(force=True)
        self._update_phase()

    def _clear_cancel_wait(self) -> None:
        self._cancel_wait = None
        if self._cancel_wake is not None:
            self._cancel_wake.cancel()
            self._cancel_wake = None

    def _watch_cancel(self) -> None:
        identity = (self.provider, self.session, self.active_response)
        if self._cancel_wait is not None and self._cancel_wait[:3] == identity:
            return  # another interruption must not move the original deadline
        self._clear_cancel_wait()
        self._cancel_wait = (*identity, self.clock() + CANCEL_TIMEOUT_SECONDS)
        self._cancel_wake = asyncio.get_running_loop().call_later(
            CANCEL_TIMEOUT_SECONDS, lambda: self._spawn(self.fill_idle()) if not self.closing else None)

    def _check_cancel_wait(self) -> None:
        if self._cancel_wait is None:
            return
        provider, session, response, deadline = self._cancel_wait
        if (self.closing or self.recovering or provider is not self.provider
                or session != self.session or response != self.active_response):
            self._clear_cancel_wait()
        elif self.clock() >= deadline:
            self._clear_cancel_wait()
            self._cancel_recovery_provider = provider
            log.warning("voice cancellation overdue call=%s response=%s", self.call_id, response)
            self._spawn(self._close_cancelled_session(provider))

    async def _close_cancelled_session(self, provider) -> None:
        # Closing ends its event stream; the existing provider pump alone owns
        # recovery. Never forge response.done or create a second active reply.
        try:
            async with asyncio.timeout(CLOSE_WAIT_SECONDS + .1):
                await provider.close()
        except Exception as exc:
            log.warning("voice cancellation recovery failed call=%s error=%s", self.call_id, type(exc).__name__)
            if provider is self.provider and not self.closing:
                self.failed.set()

    async def _on_user_stopped(self, event) -> None:
        if self._memory_pending:
            self._memory_started = self.clock()
        self._speech_until = None
        self._finish_user_interruption(invalid=event.invalid)
        self.state = "idle" if self.state == "user_speaking" else self.state
        # A valid end of speech gets the VAD's own reply; a filtered one (turn_invalid) gets nothing.
        self.awaiting_since = None if event.invalid else self.clock()
        self.quiet_since = self.clock()
        self.meter.pending_input = self.meter.pending_input and not event.invalid
        if event.invalid:
            self._memory_pending = False
            self._memory_expected_autos = deque((generation, seen) for generation, seen in self._memory_expected_autos
                if generation != self._memory_generation)
        elif self.prepare_memory is None:
            # Native memory has its own ASR/preparation/ACK barrier. Reading
            # work here would block the provider event loop before final ASR;
            # the response boundary still verifies task versions before audio.
            await self._read_current_context()
        self._update_phase()
        await self.fill_idle()

    async def _on_user_transcript(self, event) -> None:
        self._finish_user_interruption()
        # Some realtime sessions omit speech_stopped. Final ASR closes only
        # its own utterance; a late transcript cannot end newer live speech.
        if self.state == "user_speaking" and (not self._last_user_item or event.item_id == self._last_user_item):
            self.state, self._speech_until = "idle", None
            self.awaiting_since = self.clock()  # still allow the provider's native VAD reply
        self.utterance = event.text  # becomes VoiceTurn.transcript
        if self._said("user", event.text, event.item_id):
            self.heard_count += 1
            if event.item_id:
                self._user_utterances[event.item_id] = self.heard_count
            self.keeper.user_turn()
            if self.handoff is not None:
                self.handoff.heard(event.item_id)
            if self.unified and event.text.strip():
                await self._forward_transcript(event)
            self._memory_heard(event)

    async def _forward_transcript(self, event) -> None:
        """Every final ASR item enters the same durable inbox, once. No intent router."""
        from assistant.identities import inbox_key
        from voice.assistant_link import VoiceTurnRef
        # Session rotation can reuse provider IDs. A repeated final event in
        # the same session is a retry, never a second business request.
        key = inbox_key("voice-transcript", self.call_id, self.session, event.item_id or self.heard_count)
        if key in self._transcript_turns:
            return
        self._transcript_turns.add(key)
        # Final ASR also ends its own utterance if the VAD stop was lost. A
        # delayed transcript must not end a newer utterance still being spoken.
        if self.state != "user_speaking" or not self._last_user_item or event.item_id == self._last_user_item:
            if self.state == "user_speaking":
                self.state = "idle"
            self.awaiting_since = self._speech_until = None
        context = self._call_context(event.text, source_item=event.item_id)
        context["handover_source"] = "confirmed_transcript"
        ref = VoiceTurnRef(id=key, provider_call_id=key, text=event.text, transcript=event.text,
                           requested=self.clock(), context=context)
        if not self.pending_calls and self.progress is not None:
            self.progress.reset()
        self.pending_calls[key] = ref
        self.refs.append(ref)
        # Persist acceptance before proceeding, so hang-up cannot lose heard
        # words. The expensive assistant run and result observation stay async.
        try:
            await self.link.start(ref)
        except Exception as exc:
            log.warning("voice transcript acceptance failed call=%s error=%s", self.call_id, type(exc).__name__)
            await self._settle(ref, {"status": "failed", "speech": phrases.speech_text("unavailable", self.lang)})
            return
        self._handed_over(event.item_id)
        self._turn(ref, "accepted")
        if self._ack_at is None:
            self._ack_at = self.clock() + ACK_WAIT_SECONDS
        self._spawn(self._observe_transcript(ref))
        self._update_phase()

    async def _observe_transcript(self, ref) -> None:
        try:
            result = await self.link.wait(ref, elapsed=self.clock() - ref.requested, on_message=self._on_message)
        except Exception as exc:
            log.warning("voice transcript observation failed call=%s error=%s", self.call_id, type(exc).__name__)
            result = {"status": "failed", "speech": phrases.speech_text("failed", self.lang)}
        if result is not None:
            await self._settle(ref, result)

    async def _on_assistant_transcript(self, event) -> None:
        if ((event.response_id or self.active_response) in self._keepalives
                or (event.response_id or self.active_response) in self._unverified_replies
                or event.response_id in self._stale_responses):
            return  # a silent reply's "嗯": never part of the call
        await self._track_spoken_cards(event)
        if (event.response_id or self.active_response) in self._stale_responses:
            return  # placing these words may itself have discovered an obsolete claim
        self._said("assistant", event.text, event.item_id)
        if event.response_id:
            self._replies[event.response_id] = self._replies.get(event.response_id, "") + event.text

    async def _on_assistant_transcribing(self, event) -> None:
        await self._track_spoken_cards(event, partial=True)

    def _said(self, speaker: str, text: str, item_id: str = "") -> bool:
        added = self.spoken.add(speaker, text, item_id)
        if added and speaker in {"user", "assistant"} and self.context_sink and not self.closing:
            self._context_dirty = True
            if self._context_task is None or self._context_task.done():
                self._context_task = self._spawn(self._share_context())
        if added and self.debug:  # QA only: transcripts never reach the log otherwise
            log.info("voice transcript call=%s %s text=%s", self.call_id, speaker, " ".join(text.split()))
        return added

    async def _share_context(self):
        # One writer per call, coalescing speech events without blocking audio.
        while self._context_dirty and not self.closing:
            self._context_dirty = False
            try:
                lines = list(self.spoken.context_lines)
                if self.prepare_memory is not None:
                    lines = RecentContext(lines, self._memory_revocation_epoch)
                await self.context_sink(lines)
            except Exception as exc:
                log.warning("voice context publish failed call=%s error=%s", self.call_id, type(exc).__name__)

    async def _on_item_created(self, event) -> None:
        if event.role == "assistant" and self.response_kind == "keepalive":
            self._keepalives.setdefault(self.active_response or "", set()).add(event.item_id)
        if any(event.item_id in items for items in self._keepalives.values()):
            return  # a silent reply's text: deleted once it is done, never summarized
        self.spoken.item_created(event.item_id, event.role, event.item_type)
        if event.role == "user" and event.item_type == "message" and not event.text:
            self._last_user_item = event.item_id  # the user's audio; our notes carry text
        elif self._carried_note and event.role == "user" and event.text == self._carried_note:
            self._carried_item = event.item_id  # stands in for the lost audio item: never retired
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
        if self.handoff is not None and kind.startswith(("handoff_retry:", "handoff_failed:")) and event.response_id:
            self.handoff.responses[event.response_id] = kind
        if kind.startswith("handoff_retry:") and event.response_id:
            # This is an internal tool repair, not another spoken promise.
            # Only actual acceptance/result delivery may announce submission.
            self._unverified_replies.add(event.response_id)
        self.audio_sent, self.audio_bytes, self.first_audio_at = False, 0, None
        self._audio_ack = False
        self.started_at = self.quiet_since = self.clock()
        self.started_seq = self.seq
        self.meter.start(event.response_id)
        self.settled.clear()
        if kind in ("model", "followup"):
            fresh = await self._read_answer_context()
            # The bounded memory prompt excludes the shared navigation dump.
            # Changes to that unused dump cannot invalidate its prepared answer;
            # explicitly read tasks/cards still carry their own version fences.
            shared_changed = self.prepare_memory is None and self._native_context_changed
            stale = (shared_changed or self._native_work_stale
                     or self._work_changed(self._native_work_pending))
            # Native VAD can begin generation before a context update reaches
            # the provider. Cancel that speculative reply before its first PCM,
            # then explicitly request one with the committed snapshot.
            if (kind == "model" and (stale or not fresh)) or (kind == "followup" and (
                    stale or (not fresh and not self._freshness_notice_requested))):
                self._native_context_changed = False
                self._native_work_stale = False
                self._native_work_pending = {}
                self._unverified_replies.add(event.response_id or "")
                self._remember_stale_response(event.response_id)
                self.followup_due = True
                self._state_changed = self._state_changed or stale
                self.interrupted = True
                await self._command("cancel_response")
                return
            # Explicit reads, and the tasks the reply turns out to be about
            # (voice/reply_focus.py), bind a playing reply. An unrelated
            # background task changing must not restart every casual sentence.
            self._native_work_playing = self._native_work_pending
            self._native_work_pending = {}
        else:
            self._native_work_playing = {}  # a delivery or phrase: no earlier reply's reads bind it
        if self.unified and kind == "model":
            # Providers may automatically generate after VAD. Such speculative
            # answers must never be played or treated as shared facts.
            self._unverified_replies.add(event.response_id or "")
            self.interrupted = True
            await self._command("cancel_response")
            self._update_phase()
            return
        if kind in self._withdrawn_deliveries:
            # A browser click can arrive after response.create, before response.created.
            self.interrupted = True
            await self._command("cancel_response")
            self._update_phase()
            return
        self._native_playing = self._native_pending if kind in ("model", "followup") else {}
        # A text-assistant result is spoken too. Its status/card claims must
        # stop when the user changes the related work during playback.
        self._native_delivery = self._delivery(kind)
        self._native_response = event.response_id if kind in ("model", "followup") or self._native_delivery else None
        self._native_sources = self._card_sources.copy() if self._native_response else {}
        self._native_renderings = {identity: value for identity, value in self._renderings.items()
                                   if self._native_versions.get(identity) == value[0]} if self._native_response else {}
        # What this reply could be asking: the decisions pending as it starts,
        # and those answered moments ago (its context may predate the answer).
        self._native_decisions = dict(self._decisions) if self._native_response else {}
        since = min(self.clock(), self._question_at or self.clock()) - REMOVED_DECISION_SECONDS
        self._native_removed = [(identity, version, text) for identity, (version, text, at)
                                in self._removed_decisions.items() if at >= since] if self._native_response else []
        self._native_tasks = {key: (work_claim(row), reply_focus.describe_task(row))
                              for key, row in self._work.items()} if self._native_response else {}
        self._native_named = reply_focus.named(self._reply_words(event.response_id), {
            key: f"{row.get('title') or ''} {row.get('project') or ''}" for key, row in self._work.items()}
            ) if self._native_response and not self._native_delivery and self._focus_tasks(event.response_id) else None
        if self._native_named:
            # It answers words naming this task, whatever meaning makes of it
            # (measured 2026-10-27: Pause left "it is regenerating" playing).
            self._native_work_playing = {**self._native_work_playing,
                                         self._native_named: self._native_tasks[self._native_named][0]}
        self._native_meaning, self._focus_chars, self._focus_final, self._focus_read = {}, 0, False, False
        self._native_text = ""
        if kind in ("model", "followup"):
            self._native_pending = {}
            self._playing_decision = None  # an older delivery does not own this new native reply
        if (self._native_playing and self._cards_epoch > self._native_read_epoch
                and any(self._native_versions.get(k) != v for k, v in self._native_playing.items())):
            # It answers from a card read before the card changed: never heard.
            self._native_playing.clear()
            self._answer_again()
            await self._interrupt_playback(source="decision_changed")
            self._update_phase()
            return
        if ref := self._delivery(kind):
            ref.delivery = "delivering"
        if kind in ("model", "followup") and event.response_id:
            self._answering[event.response_id] = self._last_user_item
            self._ack_at = None  # the model speaks anyway, with every tool output in view
        elif kind.startswith("handoff_retry:") and event.response_id:
            self._answering[event.response_id] = kind.split(":", 1)[1]
        if kind == "model" and not self._direct:
            self.followup_due = False  # the tool outputs are in the conversation: this reply answers them
            self._tool_receipts.clear()
        if (self.state == "user_speaking"
                or (self._speech_until is not None and self.clock() < self._speech_until)
                or (self.limit_reason and kind != "phrase:limit_reached")):
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
        if not self.audio_sent:
            self._audio_ack = self.playback_ack
            if self._audio_ack:
                self.emit({"type": "playback.started", "response_id": self._playback_key(self.active_response)})
        self.emit(event.audio)
        self.audio_bytes += len(event.audio)
        now = self.clock()  # the client plays what it gets in order: after what it still has, or from now
        self._playing_until = max(self._playing_until or now, now) + len(event.audio) / OUTPUT_BYTES_PER_SECOND
        if not self.audio_sent:
            self.audio_sent, self.first_audio_at = True, now
            if (ref := self._delivery(self.response_kind)) and ref.delivery_requested_at is not None:
                log.info("voice delivery audio call=%s turn=%s source=%s provider_to_first_audio_ms=%.1f",
                         self.call_id, ref.id, ref.provider_call_id,
                         max(now - ref.delivery_requested_at, 0) * 1000)
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
        self._clear_cancel_wait()
        kind, heard = self.response_kind, self.audio_sent
        self.active_response = self.response_kind = self._claimed = None
        self.state = "idle" if self.state == "responding" else self.state  # still user_speaking if interrupted
        self.quiet_since = self.clock()
        self.keeper.response_done(event.usage)
        if kind == "phrase:greeting":
            self._greeting_over()
        elif kind == "phrase:limit_reached":
            self.limit_done.set()
        said = self._replies.get(event.response_id or "", "")
        refs = [ref for ref in self.deliveries if ref.covered_by and ref.covered_by == event.response_id]
        if (ref := self._delivery(kind)) and ref not in refs:
            refs.append(ref)
        await self._response_playback_done(event, refs, said)
        if heard and event.response_id:
            self._heard_replies.append(event.response_id)
        if event.response_id in self._acks:
            self._acks.discard(event.response_id)
            if not heard and event.status == "completed":
                # It called assistant_ask without a word: acknowledged unless the result comes first.
                self._ack_at = self.clock() + ACK_WAIT_SECONDS
        self._replies.pop(event.response_id or "", None)
        self._unverified_replies.discard(event.response_id or "")
        if self.handoff is not None:
            self.handoff.response_done(self._answering.get(event.response_id or "", ""), kind,
                                       event.status == "completed")
        if kind in {"model", "followup"} and heard and event.status == "completed" and not self.interrupted:
            self._answered_item = self._answering.get(event.response_id or "", "") or self._answered_item
        if (kind in {"model", "followup"} and heard and event.status == "completed"
                and not self.followup_due and not self.pending_calls
                and self._answering.get(event.response_id or "") == self._last_user_item):
            self.call_language.begin_turn()  # a one-reply override must not leak into later reports
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
        if self.prepare_memory is not None and kind == "model":
            self._retry_memory_answer(event.reason)
        if self.handoff is not None:
            self.handoff.refused(kind)
        if kind == "phrase:greeting":
            self._greeting_over()
        elif kind == "keepalive" and event.reason == "no_input":
            self._keepalive_prompt = True  # Audio: no user message left in the conversation
        elif ref := self._delivery(kind):
            await self._refused(ref, event.reason)
        self._update_phase()  # a refused follow-up or progress reply is not retried: the reply that won answers

    def _awaiting_reply(self) -> bool:
        return self._memory_pending or (self.awaiting_since is not None and self.clock() - self.awaiting_since < REPLY_GRACE_SECONDS)

    def _can_inject(self) -> bool:
        # A removal still in flight holds replies back; a failed one cannot hold
        # the call forever (whoever depended on it has already decided).
        return (self.state == "idle" and self.requested is None and self.active_response is None
                and self.provider is not self._cancel_recovery_provider
                and not any(not value["done"].is_set() for value in self._memory_blocked.values())
                and all(task.done() for task in self._memory_deletions.values())
                and not self.closing and not self.swapping and not self.recovering and not self._awaiting_reply()
                and (self._speech_until is None or self.clock() >= self._speech_until))

    async def fill_idle(self) -> None:
        """After every change and every second: at most one reply of ours, answers before reassurance, each once
        the client has played the last one; with nothing to say for a while, a silent one that keeps the provider
        session open."""
        self._check_cancel_wait()
        if (self._memory_pending and self.state != "user_speaking" and self._memory_started is not None
                and self.clock() - self._memory_started > DEGRADE_AFTER_SECONDS):
            # Answer without new memory evidence rather than lose the user's turn.
            self._memory_started = None
            log.warning("voice memory preparation overdue call=%s", self.call_id)
            self._spawn(self._memory_degrade(self._memory_utterance or self._last_user_item, self.utterance,
                                             self._memory_generation, self.session, "overdue"))
            return
        if self.awaiting_since is not None and not self._awaiting_reply():
            self.awaiting_since = None
            self._update_phase()
        async with self._idle:
            await self._finish_playbacks()
            if (self.requested is not None and self._requested_at is not None
                    and self.clock() - self._requested_at >= REQUEST_TIMEOUT_SECONDS):
                await self._unanswered()
            if self.limit_reason:
                if not self.limit_done.is_set():
                    await self.say_phrase("limit_reached", phrases.phrase_instructions("credits_exhausted", self.lang))
                return
            if not self._can_inject() or self._report_batch:
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
        if self._memory_ready:
            if self._memory_retry_at is not None and self.clock() < self._memory_retry_at:
                return None  # a refused answer waits for the provider's own reply to end
            return self._say_memory_answer
        if self.next_phrase:
            return self._say_next_phrase
        if self._carry_due:
            return self._say_carried
        # A read can finish between response.done and its function output.
        # Do not insert an unrelated task note into that answer's context.
        if self._direct:
            return None
        if self.handoff is not None and (retry := self.handoff.next_reply()) is not None:
            return retry
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
        # Answering again after an outdated reply: from the latest committed
        # state, not the snapshot the stopped reply was checked against.
        fresh = await (self._read_current_context() if self._state_changed else self._read_answer_context())
        if not self._can_inject():
            return
        self._native_context_changed = False
        self._freshness_notice_requested = not fresh
        self.followup_due, self.requested = False, "followup"
        if pending := self._pending_for_utterance():
            # A stale read/decision reply must not repeat "not making it yet"
            # after this same utterance has already requested and handed off
            # production. Announce only its durable receipt while it is pending.
            # This is transport progress, so unrelated task changes cannot
            # regenerate it as another business-state answer.
            self.requested = "progress"
            self._state_changed, self._just_answered = False, []
            self._tool_receipts.clear()
            self._ack_at = None
            await self._create(phrases.receipt_instructions(self.lang) +
                "\nAccepted user request (context only, do not resubmit): " +
                json.dumps(pending.transcript or pending.text, ensure_ascii=False))
            return
        instructions = (phrases.state_changed_instructions(self.lang, self._decided(), self._work_changes())
                        if self._state_changed else phrases.tool_result_instructions(self.lang))
        self._state_changed, self._just_answered = False, []
        if not fresh:
            instructions += "\n当前状态核验失败，只说明暂时无法核实进展；不得复用历史任务状态或要求重复确认。"
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
        self._ack_at, self.requested = None, "progress" if self.unified else "followup"
        # There is no business tool result yet. A generic answer instruction
        # let the native voice model reuse an unrelated old confirmation as
        # progress for this newly handed-off request.
        await self._create(phrases.receipt_instructions(self.lang))

    def _played_out(self) -> bool:
        """The client has played our last reply and ``reply_gap`` has passed since (always, without pacing)."""
        if self._playbacks:
            return False
        return self.reply_gap is None or self._playing_until is None or (
            self.clock() >= self._playing_until + PLAYBACK_MARGIN_SECONDS + self.reply_gap)

    def _wake_when_played(self) -> None:
        """A reply held back for the client: look again as soon as it has played, not at the next tick."""
        if self.closing:
            return
        loop = asyncio.get_running_loop()
        until = max([self._playing_until or self.clock(), *[
            max(pending["until"] + 10, self.clock() + 1) if pending["ack"] else pending["until"]
            for pending in self._playbacks.values()]])
        delay = min(max(until + PLAYBACK_MARGIN_SECONDS + (self.reply_gap or 0) - self.clock(), 0.05), 10.0)
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
        if self.reply_gap is not None:
            ready = []
            for ref in queued:
                left = ref.requested + REPORT_COALESCE_SECONDS - self.clock()
                if ref.report is not None and not ref.asked and not ref.cards and not ref.questions and left > 0:
                    loop = asyncio.get_running_loop()
                    if self._wake is None or self._wake.when() > loop.time() + left:
                        if self._wake is not None:
                            self._wake.cancel()
                        self._wake = loop.call_later(left, self._woken)
                else:
                    ready.append(ref)
            queued = ready
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
        if event.response_id:
            self._silent_responses.append(event.response_id)
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
        if self.active_response in self._silent_responses:
            self._silent_responses.remove(self.active_response)
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
        if self.prepare_memory is not None and self.requested == "model":
            self.requested = None
            self._retry_memory_answer("unanswered")  # never silently lose the user's turn
            return
        kind, self.requested = self.requested, None
        if self.handoff is not None:
            self.handoff.refused(kind)
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

    def _response_policy(self, memory_packet=None) -> str:
        if self.prepare_memory is None:
            return ""
        packet = self.memory_packet if memory_packet is None else memory_packet
        from voice.prompt import response_preferences
        # A task notification may arrive after the greeting but before the
        # first utterance builds a memory packet. Presentation rules still
        # apply; absence of a packet must not restart the greeting.
        settings = (packet or {}).get("settings", {})
        return response_preferences(self.call_language.resolve(settings), settings.get("call_detail", self.detail),
                                    opening=self.greeting)

    def _instructions(self, *, memory_packet=None, include_response_policy=True) -> str:
        if self.prepare_memory is not None:
            # Current profile + bounded evidence only. Existing navigation,
            # summaries and old card notes cannot bypass this turn's policy.
            packet = self.memory_packet if memory_packet is None else memory_packet
            decisions = history = ""
            if self.memory_turn and keeps_call_context(packet):
                # A turn that keeps the call's context also keeps the decisions
                # the call already presented answerable; its reads stay gated.
                decisions = self.decision_context + self._open_decisions()
                # And what the provider no longer holds: a restricted turn retires
                # the earlier lines, a summary or a fresh session drops them. The
                # call's own record comes back once a turn may use it (measured
                # 2026-10-27: "which fruits did we talk about?" right after a
                # restricted turn got "I don't have our previous conversation").
                history = with_sections("", call_so_far=self.keeper.summary,
                                        last_lines=self.spoken.unheld(self.keeper.covered))
            work = self._turn_work if self.memory_turn else ""
            policy = self._response_policy(memory_packet) if include_response_policy else ""
            return self.base_instructions + "\n" + self._memory_text + "\n" + history + decisions + work + policy
        section = self.progress.section(list(self.pending_calls.values())) if self.progress is not None and not self.unified else ""
        base = self.opening_instructions if self.greeting and self.opening_instructions is not None else self.base_instructions
        if self.unified:
            from voice.prompt import UNIFIED_FRONT
            if not base.startswith(UNIFIED_FRONT):
                base = UNIFIED_FRONT + "\n" + base
        if self.shared_context:
            base += "\n" + self.shared_context
        if self._freshness_failed:
            base += "\n当前任务状态核验失败；历史信息不能作为当前进展或待确认事项的证据。明确说暂时无法核实。"
        if self.decision_context:
            base += "\n" + self.decision_context
        return with_sections(base, call_so_far=self.keeper.summary, progress=section,
                             last_lines=self.keeper.carried)

    async def _sync_instructions(self, *, force: bool = False, memory_packet=None):
        if (self.provider is self._cancel_recovery_provider
                or self._cancel_wait is not None and memory_packet is None and self.prepare_memory is not None):
            # The cancelled response cannot speak. Its successor rechecks the
            # committed context at its response boundary; do not hold up the
            # event pump waiting for an ACK from the session being retired.
            return False
        if self.prepare_memory is not None:
            if self._memory_pending and memory_packet is None and self.memory_packet is None:
                # VAD clears the old packet before ASR/recall. Do not block the
                # event handler publishing that incomplete intermediate view.
                # Automatic replies are quarantined; only the fresh packet's
                # confirmed update may release the next audible response.
                return False
            async with self._memory_sync:
                text = self._instructions(memory_packet=memory_packet)
                if text == self._sent_instructions:
                    return True
                if await self._command("update_instructions_confirmed", text):
                    self._sent_instructions, self._instructions_at = text, self.clock()
                    return True
                return False
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
        await self._create(phrases.receipt_instructions(self.lang, delayed=True) if self.unified else
                           phrases.progress_instructions(self.progress.step if self.progress else "", self.lang))
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
        self._clear_cancel_wait()
        self._cancel_recovery_provider = None
        self._memory_reset()
        self.session += 1
        self.spoken.reset_items()
        for ref in self.deliveries:  # a note in the old session is gone: inject it again when told
            ref.note_item, ref.note_pending, ref.note_at = None, False, None
        self._keepalives.clear()
        self._silent_responses.clear()
        self._unverified_replies.clear()
        self._tool_receipts.clear()
        self._keepalive_prompt = False
        self._carried_note = self._carried_item = ""
        self._carry_due = False
        # The old session's replies and what they were about went with it.
        self._native_response, self._native_playing, self._native_meaning = None, {}, {}
        self._native_work_playing, self._native_work_pending = {}, {}
        self.keeper.rotated()
        self._sent_instructions, self._instructions_at = instructions, self.clock()
        return old

    def _quiet_for_swap(self) -> bool:
        return (self.state == "idle" and self.active_response is None and self.requested is None
                and not self.followup_due and not self._direct and self._ack_at is None
                and all(ref.delivery in ("queued", "paused") for ref in self.deliveries))

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
        cancelled = lost is self._cancel_recovery_provider
        if why := self._recovery_blocker(cancelled=cancelled):
            log.info("voice call=%s provider session lost recovered=no why=%s recoveries=%s", self.call_id, why,
                     self.recoveries)
            return False
        self.recovering = True  # from here on nothing goes to the lost session; the microphone is kept
        try:
            await self._stop_rotation()
            self.swapping = True
            if self.provider is not lost:
                return True  # the rotation swapped a fresh session in just before
            unanswered = self._unanswered_utterance()
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
            self.recoveries += not cancelled
            await self._resend_unsent(discard=speaking)
            if unanswered is not None and not speaking:
                # A transcribed request the lost session never answered: answer
                # it in the new one instead of asking the user to say it again.
                await self._carry_utterance(*unanswered)
            elif cut_off and self.next_phrase is None:
                self.next_phrase = "reconnected"
            log.info("voice call=%s provider session recovered attempts=%s recoveries=%s cut_off=%s", self.call_id,
                     attempts, self.recoveries, cut_off)
        finally:
            self.recovering = self.swapping = False
        self._spawn(lost.close())  # already closed by the provider, as a rule; nothing waits for it
        self._update_phase()
        await self.fill_idle()
        return True

    def _recovery_blocker(self, *, cancelled: bool = False) -> str:
        """Why a lost session could not be replaced by a fresh one; empty when it can."""
        return ("not answered" if self.answered_at is None else "hanging up" if self.closing
                else "limit" if self.limit_reason else "refused " + self.unrecoverable if self.unrecoverable
                else "no opener" if self.opener is None
                else "too many" if not cancelled and self.recoveries >= RECOVERIES_PER_CALL
                else "")

    def _unanswered_utterance(self) -> tuple[str, str] | None:
        """(item, words) of the latest transcribed utterance no completed reply has answered."""
        item = self._last_user_item
        if not item or item not in self._user_utterances or item == self._answered_item:
            return None
        words = next((line.text for line in reversed(self.spoken.lines)
                      if line.role == "user" and line.item_id == item), "")
        return (item, words) if words else None

    async def _carry_utterance(self, item: str, words: str) -> None:
        note = phrases.carried_utterance(words, self.lang)
        self._carried_note = note
        if not await self._command("create_note", note):
            self.next_phrase = "reconnected"
            return
        log.info("voice call=%s unanswered utterance carried into the new session", self.call_id)
        if self.prepare_memory is not None:
            # The same evidence/ACK barrier as spoken input; a text item has no
            # VAD reply of its own to wait for.
            self._memory_new_speech()
            self._memory_expected_autos.pop()
            self._memory_auto_seen.set()
            self._memory_utterance = item
            self._memory_started = self.clock()
            event = ProviderEvent("user_transcript", item_id=item, text=words)
            self._memory_task = self._spawn(self._prepare_memory(event, self._memory_generation, self.session))
        else:
            self._carry_due = True

    async def _say_carried(self) -> None:
        self._carry_due = False
        if self._state_changed:
            await self._say_followup()
            return
        self.requested = "model"
        await self._create(None)

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
        # audio_sent belongs to the last reply that started: a request that
        # never started was not heard, whatever the reply before it played.
        response_id = self.active_response
        self._clear_cancel_wait()
        heard = response_id is not None and self.audio_sent and kind != "keepalive"
        speaking = self.state == "user_speaking"
        cut_off = (speaking or self._awaiting_reply() or self.followup_due or bool(self._direct)
                   or (response_id is not None and (heard or kind in ("model", "followup"))))
        said, lost = self._replies.pop(response_id or "", ""), self._delivery(kind)
        covered = [ref for ref in self.deliveries if response_id and ref.covered_by == response_id]
        if heard or self._playbacks or (self._playing_until is not None and self.clock() < self._playing_until):
            # What the lost session sent may still be playing in the browser.
            # It counts as unheard and is told again in the new session, so it
            # stops here rather than be heard twice.
            self.emit(events.playback_clear())
        self.meter.abandon()  # its usage never comes: counted once, from the audio received
        self.settled.set()
        if self.greeting:
            self._greeting_over()
        self.active_response = self.response_kind = self._claimed = self.requested = None
        self.state, self.interrupted, self.awaiting_since, self.followup_due = "idle", False, None, False
        self._ack_at = None  # its handover went with the session's conversation; the result is told as usual
        self._answering.pop(response_id or "", None)
        self._acks.discard(response_id or "")
        await self._cancel_playbacks()
        for ref in self.deliveries:  # the notes went with the session
            ref.note_item, ref.note_pending, ref.note_at = None, False, None
        cut = [lost] if lost is not None and lost.delivery in ("creating", "delivering") else []
        for ref in cut + [ref for ref in covered if ref is not lost]:
            ref.interrupted = heard
            if not heard:
                ref.attempts = max(ref.attempts - 1, 0)  # a lost socket is not a telling (turns._requeue)
            await self._delivered(ref, False, said)
        for ref in self.deliveries:  # whatever was on its way waits for the next quiet moment
            if ref.delivery not in ("queued", "paused"):
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
        self._clear_cancel_wait()
        if self._memory_warm_task is not None:
            self._memory_warm_task.cancel()
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
        if name == "cancel_response" and self.active_response and not self.closing:
            # All tracked cancellations need a deadline, including stale
            # replies caught before first PCM, not only interrupted playback.
            self._watch_cancel()
        try:
            await getattr(self.provider, name)(*args)
            return True
        except Exception as exc:
            log.warning("voice provider %s failed call=%s error=%s", name, self.call_id, type(exc).__name__)
            return False

    async def _create(self, instructions: str | None) -> None:
        # A response-specific prompt must carry the durable role and tool contract too.
        # Providers can treat it as an override, rather than an addition to session.instructions.
        policy = self._response_policy()
        scoped_policy = None
        if instructions and self.base_instructions:
            # The current speaking settings apply to reports/cards too; do
            # not leave a delivery directive after them overriding brevity
            # or copying a salutation from the report's reference text.
            if policy:
                scoped_policy = (instructions + "\nUse the latest background note as factual data, "
                    "not as wording to read verbatim. Omit its greeting or salutation; keep its actual "
                    "task state, decisions and necessary facts.\n" + policy) if not self.greeting else instructions + policy
            instructions = self._instructions(include_response_policy=False) + "\n\n" + instructions + policy
        elif policy and self.base_instructions:
            # Audio follows a short per-response presentation control more
            # reliably than session-only updates after earlier greetings.
            # The provider keeps the full contract for override-style models
            # without duplicating the memory packet into Audio user items.
            instructions = self._instructions()
            scoped_policy = ("Answer the latest real user utterance using the already confirmed session "
                "context. This is a presentation control, not new user facts or execution permission.\n" + policy)
        self._requested_at = self.clock()
        args = (instructions, scoped_policy) if scoped_policy else (instructions,)
        if not await self._command("create_response", *args):
            # The session is gone: its pump recovers the call (asking a cut-off
            # user to say it again) or ends it.
            self.requested = None
