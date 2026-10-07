"""Tool calls inside a call: ``assistant_ask`` turns, the direct reads, and how results are told.

docs/ASSISTANT_VOICE_FIX_PLAN.md §5.1 and §5.5. ``assistant_ask`` gets its one
function_call_output at once (``{"status": "accepted"}``), so the front desk
never sits on a pending call. The result becomes a background note: a
user-role message injected in an idle window and immediately followed by our
delivery request, which asks for the front desk's own words, not a reading.
A reply that started within 1.5 s after the note went in, before ours could
(a VAD reply winning the race), saw the note and counts as the delivery; a
delivery nobody heard has its note deleted, so no later reply can read it,
and is injected again at the next idle window. A result is told once.

A turn that stopped at a confirmation card comes back with the card: its
note lists it under a short handle and the reply reads it and asks. The
user's yes or no is answered with ``cards_answer`` (voice/cards.py), never
handed to the assistant: a new main-session turn would void the card. The
resumed turn is followed like any other and its result told the same way.
"""
import asyncio
import re
from dataclasses import replace

from core.identifier import generate_id
from core.log import create_logger
from voice import cards, events, phrases, tools
from voice.assistant_link import VoiceTurnRef, turn_outcome as _outcome

log = create_logger("voice.turns")

# A reply that promises to act ("这就去删", "我去安排") made without calling any tool: measured
# twice in a row after a declined card. The user's words are then handed over as assistant_ask would.
PROMISE = re.compile(r"我这就去|这就去|马上去|我去(?:办|处理|安排|查|看|建|删|改|弄|做|问)|我来(?:办|处理|安排)"
                     r"|交给(?:个人)?助理|让助理|帮你(?:删|建|改|安排|处理|办)|去(?:办|处理|安排|删|建)了"
                     r"|\bI'?ll (?:go|get|handle|take care|ask|set|create|delete|cancel)|\bon it\b"
                     r"|let me (?:handle|ask|get)", re.IGNORECASE)
COVER_SECONDS = 1.5
DELIVERY_ATTEMPTS = 3
ACCEPTED = {"zh": "结果稍后以后台备注送到", "en": "the result arrives later as a background note"}


class TurnsMixin:
    def _init_turns(self) -> None:
        self.followup_due = False   # tool outputs no reply has answered yet
        self._direct: set[str] = set()  # direct reads still running
        self._acks: set[str] = set()    # replies that carried an assistant_ask
        self._called: set[str] = set()  # replies that made a tool call
        self._replies: dict[str, str] = {}  # reply → what it said (promise check)
        self.heard_count = 0            # user utterances transcribed so far (CardDesk: spoken after a card?)
        # Real time: it waits for a transcript still on its way, whatever clock the call runs on.
        self.desk = cards.CardDesk(lambda: (self.heard_count, self.utterance))

    async def _on_tool_call(self, event) -> None:
        if not event.call_id or event.call_id in self._seen_calls:
            return
        self._seen_calls.add(event.call_id)
        self._called.add(event.response_id or self.active_response or "")
        if event.name == tools.ASSISTANT_ASK:
            await self._ask(event)
        elif event.name in tools.DIRECT:
            self._direct.add(event.call_id)
            self._spawn(self._direct_call(event))
        else:
            await self._command("send_tool_output", event.call_id, {"status": "unknown_tool"})

    async def _ask(self, event) -> None:
        words = self.utterance or event.text
        if await self._card_reply(event, words):
            return
        if tools.is_fragment(words):
            # Half a sentence ("嗯", "就是", "帮我"): ask what the user wants instead of handing it over.
            log.info("voice fragment call=%s chars=%s", self.call_id, len(words))
            if await self._command("send_tool_output", event.call_id, dict(tools.NEED_MORE)):
                self.followup_due = True
            return
        plain = tools.plain(words)
        kept = next((ref for ref in self.pending_calls.values()
                     if ref.provider_call_id.startswith("promise:") and tools.plain(ref.transcript) == plain), None)
        if kept is not None:  # the promise keeper already handed these words over: one turn, not two
            await self._command("send_tool_output", event.call_id,
                                {"status": "accepted", "note": ACCEPTED.get(self.lang, ACCEPTED["zh"])})
            return
        ref = VoiceTurnRef(id=generate_id(), provider_call_id=event.call_id, text=event.text, transcript=words,
                           requested=self.clock())
        self.utterance = ""
        await self._command("send_tool_output", event.call_id,
                            {"status": "accepted", "note": ACCEPTED.get(self.lang, ACCEPTED["zh"])})
        self._acks.add(event.response_id or self.active_response or "")
        if not self.pending_calls and self.progress is not None:
            self.progress.reset()  # a step from an earlier run is not this request's
        self.pending_calls[event.call_id] = ref
        self.refs.append(ref)
        self._spawn(self._run_turn(ref))
        self._update_phase()

    async def _card_reply(self, event, words: str) -> bool:
        """A bare yes or no while a card read in this call still waits: it answers the card.

        Handed to the assistant it would start a new main-session turn, which
        voids the card; the model is told to answer it with cards_answer.
        """
        if not self.desk.fresh() or not cards.answers_card(words):
            return False
        waiting = {card["card_id"]: card for card in await self.link.cards()}
        card_id = next((card_id for card_id in reversed(self.desk.fresh()) if card_id in waiting), None)
        if card_id is None:
            return False
        log.info("voice card reply redirected call=%s", self.call_id)
        if await self._command("send_tool_output", event.call_id, {
                "status": "answer_card", "card": self.desk.handle(card_id), "options": waiting[card_id]["options"],
                "hint": "用户是在回答这张卡片：明确同意就用 cards_answer 选确认的选项，拒绝就选取消；不要交给 assistant_ask"}):
            self.followup_due = True
        return True

    async def _direct_call(self, event) -> None:
        try:
            scope = replace(self.scope, transcript=self.utterance, desk=self.desk) if self.scope else None
            result = await tools.run(event.name, scope, event.arguments) if scope else dict(tools.UNAVAILABLE)
        finally:
            self._direct.discard(event.call_id)
        if self.closing:
            return
        while self.desk.answers:  # a card confirmed: the turn it resumes is followed like any other
            answer = self.desk.answers.pop(0)
            if answer["choice"] not in cards.DECLINE:  # declined: nothing happens, and the model has said so
                self._follow(event.call_id, answer)
        if await self._command("send_tool_output", event.call_id, result):
            self.followup_due = True  # the model answers from the output in the next idle window
        await self.fill_idle()

    async def _keep_promise(self, response_id: str | None, heard: bool) -> None:
        """A reply that said it would act but called nothing: hand the user's words over as assistant_ask would."""
        said = self._replies.pop(response_id or "", "")
        called = (response_id or "") in self._called
        self._called.discard(response_id or "")
        words = self.utterance
        if (called or not heard or not PROMISE.search(said) or not words or tools.is_fragment(words)
                or not tools.asks_for_work(words) or (self.desk.fresh() and cards.answers_card(words))
                or any(ref.transcript == words for ref in self.pending_calls.values())):
            return
        log.info("voice promise kept call=%s", self.call_id)
        ref = VoiceTurnRef(id=generate_id(), provider_call_id=f"promise:{response_id}"[:64], text=words,
                           transcript=words, requested=self.clock())
        self.utterance = ""
        if not self.pending_calls and self.progress is not None:
            self.progress.reset()
        self.pending_calls[ref.provider_call_id] = ref
        self.refs.append(ref)
        self._spawn(self._run_turn(ref))
        self._update_phase()

    def _heard_card_reply(self, words: str) -> None:
        """A clear no as the first words after a card was read declines it (voice/cards.py decline)."""
        just_read = [card_id for card_id in self.desk.open() if self.desk.since_read(card_id) == 1]
        if just_read and self.scope is not None and cards.refuses(words):
            scope = replace(self.scope, desk=self.desk)
            for card_id in just_read:
                self._spawn(self._decline(scope, card_id))

    async def _decline(self, scope, card_id: str) -> None:
        try:
            await cards.decline(scope, card_id)
        except Exception as exc:  # the card stays on screen; the user can still answer it there
            log.warning("voice card decline failed call=%s error=%s", self.call_id, type(exc).__name__)

    def _follow(self, call_id: str, answer: dict) -> None:
        ref = VoiceTurnRef(id=generate_id(), provider_call_id=f"{call_id}:card"[:64], text=answer["what"],
                           transcript=answer["words"], requested=self.clock())
        if not self.pending_calls and self.progress is not None:
            self.progress.reset()
        self.pending_calls[ref.provider_call_id] = ref
        self.refs.append(ref)
        self._spawn(self._run_follow(ref, answer["after"]))
        self._update_phase()

    async def _run_follow(self, ref: VoiceTurnRef, after: str) -> None:
        try:
            self._turn(ref, "accepted")
            result = await self.link.follow(ref, after=after, on_message=self._on_message)
        except Exception as exc:  # the resumed turn goes on in the conversation; say so
            log.warning("voice card follow failed turn=%s error=%s", ref.id, type(exc).__name__)
            result = {"status": "failed", "speech": phrases.speech_text("failed", self.lang), "reason": "failed"}
        if result is not None:
            await self._settle(ref, result)

    async def _run_turn(self, ref: VoiceTurnRef) -> None:
        try:
            await self.link.start(ref)
            self._turn(ref, "accepted")
            result = await self.link.wait(ref, elapsed=self.clock() - ref.requested, on_message=self._on_message)
        except Exception as exc:  # not accepted at all, or the wait broke: say so either way
            log.warning("voice turn failed turn=%s accepted=%s error=%s", ref.id, bool(ref.inbox_id),
                        type(exc).__name__)
            reason = "failed" if ref.inbox_id else "unavailable"
            result = {"status": "failed", "speech": phrases.speech_text(reason, self.lang), "reason": reason}
        if result is not None:
            await self._settle(ref, result)

    def _on_message(self, ref: VoiceTurnRef) -> None:
        if ref.provider_call_id in self.pending_calls:
            self._turn(ref, "working")

    async def _settle(self, ref: VoiceTurnRef, result: dict) -> None:
        self.pending_calls.pop(ref.provider_call_id, None)
        ref.status, ref.speech, ref.settled = result["status"], result["speech"], self.clock()
        ref.cards = [card for card in result.get("cards") or [] if card["card_id"] not in self.desk.answered]
        ref.reason = "ok" if ref.status == "ok" else result.get("reason") or ref.status
        if not self.pending_calls and self.progress is not None:
            self.progress.reset()
        if self.closing:
            return
        if ref.status != "ok":
            self._turn(ref, ref.status)
        ref.delivery = "queued"
        self.deliveries.append(ref)
        self._update_phase()
        await self.fill_idle()

    async def _deliver(self, ref: VoiceTurnRef) -> None:
        """The note goes in, then our reply is asked for at once: nothing else can read the note first."""
        ref.attempts += 1
        if ref.note_item is None and not ref.note_pending:
            if ref.cards:  # handles are issued as the card's content goes to the model
                shown = [cards.spoken_card(card, self.desk.show(card["card_id"]), self.lang) for card in ref.cards]
                ref.note = phrases.card_note(ref.transcript or ref.text, ref.speech, shown, self.lang)
            else:
                ref.note = phrases.note_text(ref.reason, ref.transcript or ref.text, ref.speech, self.lang)
            ref.note_pending, ref.note_at = True, None
            await self._command("create_note", ref.note)
            if ref.attempts == 1:
                self._said("note", ref.note)
        ref.delivery = "creating"
        self.requested = f"delivery:{ref.id}"
        await self._create(phrases.card_instructions(self.lang) if ref.cards
                           else phrases.delivery_instructions(ref.speech, self.lang) if ref.status == "ok"
                           else phrases.notice_instructions(self.lang))

    def _match_note(self, event) -> None:
        """The provider names the note's item itself (one we choose is ignored): match it by its text."""
        if event.role != "user" or event.item_type != "message" or not event.text:
            return
        ref = next((ref for ref in self.deliveries if ref.note_pending and ref.note == event.text), None)
        if ref is not None:
            ref.note_pending, ref.note_item, ref.note_at, ref.note_seq = False, event.item_id, self.clock(), self.seq

    def _cover(self, response_id: str | None) -> None:
        """A VAD reply that beat our delivery request: did the provider have the note when it started?

        The provider reports in order, so the note's confirmation arriving
        before the reply's start means the reply was created with the note in
        the conversation; the 1.5 s bound keeps an old note from counting.
        """
        for ref in self.deliveries:
            if (ref.delivery in ("creating", "delivering") and ref.note_at is not None
                    and ref.note_seq < self.started_seq and self.started_at - ref.note_at <= COVER_SECONDS):
                ref.delivery, ref.covered_by = "covered", response_id
                log.info("voice delivery covered turn=%s call=%s", ref.id, self.call_id)

    async def _delivered(self, ref: VoiceTurnRef | None, heard: bool) -> None:
        if ref is None:
            return
        ref.covered_by = None
        if not heard and ref.attempts < DELIVERY_ATTEMPTS and not self.closing:
            # Nobody heard it: take the note out so no other reply reads it, and tell it again later.
            ref.delivery = "queued"
            if ref.note_item is not None:
                await self._delete(ref.note_item)
                ref.note_item, ref.note_at = None, None
            return
        self.deliveries.remove(ref)
        ref.delivery, ref.finished = "done", self.clock()
        self.keeper.note_delivered(ref.note_item)
        if ref.status == "ok":
            self._turn(ref, "delivered")
        await self.link.done(ref, _outcome(ref, heard=heard), delivered=heard)

    async def _refused(self, ref: VoiceTurnRef, reason: str) -> None:
        """Our delivery request was refused: retried after the reply that won, or given up."""
        if ref.delivery == "covered":
            return  # the reply that won saw the note
        if reason == "active_response":
            ref.delivery = "queued"
            return
        self.deliveries.remove(ref)  # refused for another reason: say the result is in the text
        ref.delivery, ref.status = "done", "failed" if ref.status == "ok" else ref.status
        self._turn(ref, "failed")
        await self.link.done(ref, _outcome(ref))
        self.next_phrase = "result_in_text"

    def _delivery(self, kind: str | None) -> VoiceTurnRef | None:
        turn_id = kind.split(":", 1)[1] if kind and kind.startswith("delivery:") else None
        return next((ref for ref in self.deliveries if ref.id == turn_id), None)

    def _protected_items(self) -> set[str]:
        """Notes not told yet stay in the conversation whatever a summary covers."""
        return {ref.note_item for ref in self.deliveries if ref.note_item}

    def _turn(self, ref: VoiceTurnRef, state: str) -> None:
        self.emit(events.turn(ref.id, state, inbox_id=ref.inbox_id, message_id=ref.message_id))

    def _spawn(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task
