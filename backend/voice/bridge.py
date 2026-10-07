"""One call's state machine between the client, the front desk and the personal assistant.

Rules: docs/VOICE_CALL_SPEC.md §4.2, docs/VOICE_CALL_BACKEND.md §8 and the
second round, docs/ASSISTANT_VOICE_FIX_PLAN.md §5. Measured against the
provider: ``response.create`` while a reply is active is refused, and a
second output for one call_id is refused. So every call_id gets exactly one
output (``assistant_ask`` at once; its result comes later as a note, see
voice/turns.py), and a reply of ours (greeting, result, progress, follow-up,
notice) is only requested in an idle window, one at a time.
"""
import asyncio
import time
from collections import deque
from contextlib import suppress

from core.log import create_logger
from voice import events, phrases
from voice.assistant_link import turn_outcome as _outcome
from voice.meter import CallMeter, OUTPUT_BYTES_PER_SECOND
from voice.prompt import with_sections
from voice.transcript import CallTranscript
from voice.turns import TurnsMixin
from voice.upkeep import ContextKeeper

log = create_logger("voice.bridge")

OUTBOX_LIMIT = 1000
# After a valid end of speech the VAD answers by itself; keep out of its way this long.
REPLY_GRACE_SECONDS = 3.0
STOP_WAIT_SECONDS, CLOSE_WAIT_SECONDS = 2.0, 3.0  # final usage after hang-up; turn observers' last step
PLAYBACK_MARGIN_SECONDS = 0.3
# After the greeting has played on the client, the microphone counts again (echo of its last word aside).
GREETING_MARGIN_SECONDS = 0.3
PROGRESS_GAP_SECONDS, PROGRESS_PER_TURN = 12.0, 3
# The same step again only after this long: measured, three "still creating it" lines in 40 s were noise.
SAME_STEP_GAP_SECONDS = 30.0
INSTRUCTIONS_GAP_SECONDS = 2.0  # progress moves fast; the session prompt follows it at most this often


class Bridge(TurnsMixin):
    def __init__(self, provider, link, *, lang: str = "zh", late_after: float = 12.0, clock=time.monotonic,
                 debug_transcripts: bool = False, scope=None, progress=None, instructions: str = "",
                 opener=None, summarizer=None, wall_clock=None, rates=None):
        self.provider, self.link, self.lang = provider, link, lang
        self.late_after, self.clock, self.debug = late_after, clock, debug_transcripts
        # scope: whose call (direct reads); progress: the main session's live steps; opener: a fresh session.
        self.scope, self.progress, self.opener = scope, progress, opener
        self.call_id = scope.call_id if scope is not None else ""
        self.state = "idle"                     # idle / user_speaking / responding
        self.active_response: str | None = None
        self.interrupted = False                # the active reply's audio is dropped
        self.pending_calls = {}                 # provider call_id → VoiceTurnRef, until its result is in
        self.deliveries = deque()               # results waiting to be told, oldest first
        self.response_kind: str | None = None   # model / phrase:<key> / delivery:<turn_id> / progress / followup
        self.requested: str | None = None       # response.create sent, response.created not seen
        self.next_phrase: str | None = None     # a fixed notice for the next idle window
        self.outbox: asyncio.Queue = asyncio.Queue(maxsize=OUTBOX_LIMIT)
        self.overflow, self.failed, self.limit_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.settled = asyncio.Event()          # set: no started reply still owes its usage
        self.settled.set()
        self.meter = CallMeter(rates)
        self.greeting, self.closing, self.limit_reason = True, False, None
        self.greeted = asyncio.Event()          # set: the greeting was said in full, refused or cut
        self.greeting_bytes = 0                 # its audio, to know when the client has played it
        self.answered_at: float | None = None   # when the client was told the call is answered
        self.rotating = self.swapping = False   # a fresh session is being prepared / swapped in
        self.awaiting_since: float | None = None
        self.audio_sent, self.audio_bytes, self.first_audio_at, self.started_at = False, 0, None, None
        self.seq = self.started_seq = 0         # provider events so far; at the active reply's start
        self.quiet_since, self.last_progress = clock(), None
        self._progress_version = None           # the step's version when the last progress line was asked for
        self.utterance = ""                     # the latest user utterance: VoiceTurn.transcript, fragment guard
        self.refs = []                          # every assistant_ask of the call
        self.spoken = CallTranscript(now=wall_clock)
        self.keeper = ContextKeeper(self.spoken, clock=clock, summarizer=summarizer, protected=self._protected_items)
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
        if not await self.say_phrase("greeting", phrases.greeting_instructions(self.lang)):
            self._greeting_over()

    def answered(self) -> None:
        """The client was just told the call is answered: it plays the greeting made beforehand now."""
        self.answered_at = self.clock()

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
        if not self.closing:
            # After the limit nobody starts a new turn, and the greeting is never cut off by its own echo;
            # silence keeps the stream well formed.
            hold = self.limit_reason or self._holding_input()
            await self.provider.send_audio(bytes(len(pcm)) if hold else pcm)

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

    async def _on_user_started(self, _event) -> None:
        self.state, self.interrupted, self.awaiting_since = "user_speaking", True, None
        if self.greeting:
            self._greeting_over()
        self.utterance = ""  # a new utterance: never pair a call with the previous words
        self.quiet_since = self.clock()
        self.meter.pending_input = True
        self.emit(events.playback_clear())
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
            self.keeper.user_turn()
            self._heard_card_reply(event.text)

    async def _on_assistant_transcript(self, event) -> None:
        self._said("assistant", event.text, event.item_id)
        if event.response_id:
            self._replies[event.response_id] = self._replies.get(event.response_id, "") + event.text

    def _said(self, speaker: str, text: str, item_id: str = "") -> bool:
        added = self.spoken.add(speaker, text, item_id)
        if added and self.debug:  # QA only: transcripts never reach the log otherwise
            log.info("voice transcript call=%s %s text=%s", self.call_id, speaker, " ".join(text.split()))
        return added

    async def _on_item_created(self, event) -> None:
        self.spoken.item_created(event.item_id, event.role, event.item_type)
        self._match_note(event)

    async def _on_item_deleted(self, event) -> None:
        self.spoken.item_deleted(event.item_id)

    async def _on_response_started(self, event) -> None:
        self._claimed, self.requested = self.requested, None
        kind = self._claimed or "model"
        self.active_response, self.response_kind, self.awaiting_since = event.response_id, kind, None
        self.audio_sent, self.audio_bytes, self.first_audio_at = False, 0, None
        self.started_at = self.quiet_since = self.clock()
        self.started_seq = self.seq
        self.meter.start(event.response_id)
        self.settled.clear()
        if ref := self._delivery(kind):
            ref.delivery = "delivering"
        if kind == "model" and not self._direct:
            self.followup_due = False  # the tool outputs are in the conversation: this reply answers them
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
        if self.closing or self.interrupted or (event.response_id and event.response_id != self.active_response):
            return
        self.emit(event.audio)
        self.audio_bytes += len(event.audio)
        if not self.audio_sent:
            self.audio_sent, self.first_audio_at = True, self.clock()
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
        self.quiet_since = self.clock()
        self.keeper.response_done(event.usage)
        if kind == "phrase:greeting":
            self.greeting_bytes = self.audio_bytes if heard else 0
            self._greeting_over()
        elif kind == "phrase:limit_reached":
            self.limit_done.set()
        elif kind and kind.startswith("delivery:"):
            await self._delivered(self._delivery(kind), heard)
        for ref in [ref for ref in self.deliveries if ref.covered_by and ref.covered_by == event.response_id]:
            await self._delivered(ref, heard)
        if event.response_id in self._acks:
            self._acks.discard(event.response_id)
            if not heard and event.status == "completed":
                self.followup_due = True  # it called assistant_ask without a word: acknowledge it now
        if kind == "model" and event.status == "completed":
            await self._keep_promise(event.response_id, heard)
        else:
            self._replies.pop(event.response_id or "", None)
            self._called.discard(event.response_id or "")
        self._update_phase()
        await self.fill_idle()

    async def _on_provider_error(self, event) -> None:
        log.info("voice provider error code=%s reason=%s requested=%s", event.code, event.reason, self.requested)
        if event.reason == "voice_unsupported":
            self.failed.set()  # only the greeting can reveal it; the call ends as provider_error
            self.greeted.set()
            return
        if event.reason in ("duplicate_output", "item"):
            return  # one call's output or one item: nothing to retry
        if (event.reason == "active_response" and self.requested is None and self._claimed
                and self._claimed == self.response_kind):
            # Our request lost to a VAD reply created in the same instant, which we took for ours.
            self.requested, self.response_kind, self._claimed = self._claimed, "model", None
            self._cover(self.active_response)
            if self.limit_reason:
                await self._command("cancel_response")
        if self.requested is None:
            return
        kind, self.requested = self.requested, None
        if kind == "phrase:greeting":
            self._greeting_over()
        elif ref := self._delivery(kind):
            await self._refused(ref, event.reason)
        self._update_phase()  # a refused follow-up or progress reply is not retried: the reply that won answers

    def _awaiting_reply(self) -> bool:
        return self.awaiting_since is not None and self.clock() - self.awaiting_since < REPLY_GRACE_SECONDS

    def _can_inject(self) -> bool:
        return (self.state == "idle" and self.requested is None and self.active_response is None
                and not self.closing and not self.swapping and not self._awaiting_reply())

    async def fill_idle(self) -> None:
        """After every change and every second: at most one reply of ours, answers before reassurance."""
        if self.awaiting_since is not None and not self._awaiting_reply():
            self.awaiting_since = None
            self._update_phase()
        async with self._idle:
            if self.limit_reason:
                if not self.limit_done.is_set():
                    # Out of credits or out of time: the same goodbye reply, its own words.
                    key = "credits_exhausted" if self.limit_reason == "credits" else "limit_reached"
                    await self.say_phrase("limit_reached", phrases.phrase_instructions(key, self.lang))
                return
            if not self._can_inject():
                return
            await self._tidy()
            if not self._can_inject():
                return
            if self.next_phrase:
                key, self.next_phrase = self.next_phrase, None
                await self.say_phrase(key)
            elif self.followup_due and not self._direct:
                self.followup_due, self.requested = False, "followup"
                await self._create(None)  # the model answers the tool outputs it was given
            elif ref := next((ref for ref in self.deliveries if ref.delivery == "queued"), None):
                await self._deliver(ref)
            elif (not await self._progress() and self.opener is not None and not self.rotating
                  and self.keeper.rotation_due()):
                self.rotating = True
                self._spawn(self._rotate())

    async def _tidy(self) -> None:
        """While idle: delete what a summary covers, then bring the session prompt up to date."""
        for item_id in self.keeper.due_deletions():
            await self._delete(item_id)
        await self._sync_instructions()

    async def _delete(self, item_id: str) -> None:
        self.spoken.item_deleted(item_id)
        await self._command("delete_item", item_id)

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

    async def _progress(self) -> bool:
        """Quiet for ``late_after`` with work pending: one sentence on what is really going on."""
        now = self.clock()
        due = [ref for ref in self.pending_calls.values()
               if ref.progress < PROGRESS_PER_TURN and now - ref.requested >= self.late_after]
        version = self.progress.version if self.progress is not None else None
        gap = SAME_STEP_GAP_SECONDS if version == self._progress_version else PROGRESS_GAP_SECONDS
        if (not due or now - self.quiet_since < self.late_after
                or (self.last_progress is not None and now - self.last_progress < gap)):
            return False
        for ref in due:
            ref.progress += 1
            if not ref.late:
                ref.late = True
                self._turn(ref, "late")
        self.last_progress, self._progress_version = now, version
        await self._sync_instructions(force=True)
        self.requested = "progress"
        await self._create(phrases.progress_instructions(self.progress.step if self.progress else "", self.lang))
        self._update_phase()
        return True

    async def _rotate(self) -> None:
        """A fresh provider session with the memo, swapped in while nothing is going on.

        The memo is made while the call goes on; only opening the new session
        (a fraction of a second) holds our replies back, and only if the call
        is quiet at that moment; otherwise it is tried again a little later.
        """
        new, instructions = None, ""
        try:
            await self.keeper.summary_now()
            if self.closing or not self._quiet_for_swap():
                raise RuntimeError("busy")
            self.swapping = True  # from here on nothing new goes into the old session
            self.keeper.carry_last_lines()
            instructions = self._instructions()
            new = await self.opener(instructions)
        except Exception as exc:
            log.info("voice call=%s rotation postponed error=%s", self.call_id, type(exc).__name__)
        if new is None or self.closing or not self._quiet_for_swap():
            if new is not None:
                await new.close()
            self.keeper.carried = "" if self.swapping else self.keeper.carried  # the old session keeps its own
            self.keeper.rotation_failed()
            self.rotating = self.swapping = False
            return
        old, self.provider = self.provider, new
        self.spoken.reset_items()
        for ref in self.deliveries:  # a note in the old session is gone: inject it again when told
            ref.note_item, ref.note_pending, ref.note_at = None, False, None
        self.keeper.rotated()
        self._sent_instructions, self._instructions_at = instructions, self.clock()
        self.rotating = self.swapping = False
        log.info("voice call=%s provider session rotated", self.call_id)
        await old.close()

    def _quiet_for_swap(self) -> bool:
        return (self.state == "idle" and self.active_response is None and self.requested is None
                and not self.followup_due and not self._direct
                and all(ref.delivery == "queued" for ref in self.deliveries))

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
        """A failure means the provider is gone; its pump ends the call."""
        try:
            await getattr(self.provider, name)(*args)
            return True
        except Exception as exc:
            log.warning("voice provider %s failed error=%s", name, type(exc).__name__)
            return False

    async def _create(self, instructions: str | None) -> None:
        if not await self._command("create_response", instructions):
            self.requested = None
