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

Who handles what (docs/VOICE_CALL_BACKEND.md §20). The front desk answers at
once, from its prompt, the call and its quick reads; meanwhile each
utterance's transcript gets the decision model's verdict and the assistant's
own recall (voice/router.py ``Judge``). Once the reply is done it is checked:
a request it neither handed over nor asked about is handed over; what the
user's records say that it missed or got wrong is added. A
request handed over is planned first (voice/handover.py): a brief that reads
without the call, or, for a quick read, the answer itself.
"""
import asyncio
import re
from dataclasses import dataclass, replace

from core.identifier import generate_id
from core.log import create_logger
from voice import cards, events, phrases, router, tools
from voice.assistant_link import VoiceTurnRef, turn_outcome as _outcome
from voice.transcript import ROLES

log = create_logger("voice.turns")

# A reply that promises to act ("这就去删", "我去安排") made without calling any tool: measured
# twice in a row after a declined card. The user's words are then handed over as assistant_ask would.
PROMISE = re.compile(r"我这就去|这就去|马上去|我去(?:办|处理|安排|查|看|建|删|改|弄|做|问)|我来(?:办|处理|安排)"
                     r"|交给(?:个人)?助理|让助理|帮你(?:删|建|改|安排|处理|办)|去(?:办|处理|安排|删|建)了"
                     r"|\bI'?ll (?:go|get|handle|take care|ask|set|create|delete|cancel)|\bon it\b"
                     r"|let me (?:handle|ask|get)", re.IGNORECASE)
COVER_SECONDS = 1.5
DELIVERY_ATTEMPTS = 3
TOLD_TOGETHER = 3  # results told in one reply at most
ACCEPTED = {"zh": "结果稍后以后台备注送到", "en": "the result arrives later as a background note"}
# The call as the assistant gets it with a request (assistant_link.start): it never heard the call.
CONTEXT_LINES, LINE_CHARS, HEARD_CHARS = 6, 60, 120
# A reply ends before its utterance's transcript now and then: wait this long for the words and the verdict.
HEARD_WAIT_SECONDS = 2.5
HEARD_KEPT = 8
# A reply that says it is done when nothing was handed over ("已经帮你建好了"): corrected once handed over.
_NOT_LATER = r"(?![，,\s]*(?:我|就|再)?(?:跟|告诉|通知|叫|和|给)你)"  # "办好了跟你说" is a promise, not a claim
CLAIMS_DONE = re.compile(r"(?:(?:已经|都)(?:帮你|给你)?(?:建|删|改|发|弄|办|做|安排|处理|设|订|记)(?:好|完|掉|上|出去|下)?了"
                         r"|搞定了|(?:办|弄|建|改|设|记|做)好了|删(?:好|掉)了|发出去了|安排(?:上|好)了)" + _NOT_LATER
                         + r"|\b(?:I'?ve|I have) (?:created|deleted|sent|set up|scheduled|made|done)\b|\ball done\b",
                         re.IGNORECASE)


@dataclass
class Heard:
    """One transcribed utterance: its words, the decision model's verdict and the recall started with it."""
    text: str
    number: int                          # heard_count once it was transcribed
    verdict: asyncio.Future              # router.Route | None
    recall: asyncio.Future | None = None  # list[dict]


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
        self._heard: dict[str, Heard] = {}   # user audio item → its words, verdict and recall
        self._answering: dict[str, str] = {}  # the model's own reply → the user audio item it answers
        self._last_user_item = ""
        self.next_aside: str | None = None    # one sentence the model words itself, in the next idle window

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
        ref = VoiceTurnRef(id=generate_id(), provider_call_id=event.call_id, text=event.text or words,
                           transcript=words, requested=self.clock(), context=self._call_context(words))
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

    def _route_utterance(self, item_id: str, text: str) -> None:
        """A transcript is in: the decision model's verdict and the assistant's recall start at once."""
        if self.judge is None or not item_id or self.scope is None or item_id in self._heard:
            return
        loop = asyncio.get_running_loop()
        if tools.is_fragment(text):  # nothing to route: the promise alone decides, as without a judge
            verdict = loop.create_future()
            verdict.set_result(None)
            self._heard[item_id] = Heard(text, self.heard_count, verdict)
        else:
            recent = [(line.role, line.text) for line in self.spoken.lines
                      if line.role in ("user", "assistant") and line.item_id != item_id]
            self._heard[item_id] = Heard(text, self.heard_count, self._spawn(self.judge.route(text, recent[-4:])),
                                         self._spawn(self.judge.recall(self.scope, text)))
        for stale in list(self._heard)[:-HEARD_KEPT]:
            self._heard.pop(stale, None)

    async def _after_reply(self, response_id: str | None, heard: bool) -> None:
        """The model's own reply to the user is done: was anything left undone or unsaid?"""
        said = self._replies.pop(response_id or "", "")
        called = (response_id or "") in self._called
        self._called.discard(response_id or "")
        item = self._answering.pop(response_id or "", "")
        if called or not heard or self.closing:
            return
        if self.judge is None:
            await self._keep_promise(said, self.utterance, response_id)
        else:
            self._spawn(self._check_reply(response_id, said, item))

    async def _check_reply(self, response_id: str | None, said: str, item: str) -> None:
        """The reply against the verdict: hand over what it left undone, add what the records say."""
        heard = await self._heard_for(item)
        verdict = await heard.verdict if heard is not None else None
        if self.closing:
            return
        words = heard.text if heard is not None else self.utterance
        if verdict is None:  # no verdict: the promise alone decides, as without a judge
            await self._keep_promise(said, words, response_id)
            return
        promise = bool(PROMISE.search(said))
        if verdict.choice == "assistant":
            if promise:
                if self._may_hand_over(words):
                    await self._hand_over(words, response_id, announce=bool(CLAIMS_DONE.search(said)))
                return
            # Not even promised: did the reply leave it undone, or ask back, explain, or answer it?
            check = await self.judge.followthrough(words, said)
            if (check is not None and check.choice == "undone" and check.confidence >= router.UNDONE_CONFIDENCE
                    and not self.closing and self._may_hand_over(words)):
                await self._hand_over(words, response_id, announce=True)
            return
        if verdict.choice not in ("read", "chat"):
            return
        found = await heard.recall if heard.recall is not None else []
        if found:
            check = await self.judge.complement(words, said, [entry["text"] for entry in found])
            if (check is not None and check.choice == "add" and check.confidence >= router.ADD_CONFIDENCE
                    and not self.closing):
                await self._tell_recall(heard, found)
                return
        if promise and verdict.choice == "read" and self._may_hand_over(words):
            await self._hand_over(words, response_id)

    async def _heard_for(self, item: str) -> Heard | None:
        """The utterance a reply answers, once its transcript is in (it can trail the reply)."""
        if not item:
            return None
        waited = 0.0
        while item not in self._heard and waited < HEARD_WAIT_SECONDS and not self.closing:
            await asyncio.sleep(0.05)
            waited += 0.05
        return self._heard.get(item)

    async def _keep_promise(self, said: str, words: str, response_id: str | None) -> None:
        """A reply that said it would act but called nothing: hand the user's words over as assistant_ask would."""
        if PROMISE.search(said) and self._may_hand_over(words) and tools.asks_for_work(words):
            await self._hand_over(words, response_id, announce=bool(CLAIMS_DONE.search(said)))

    def _may_hand_over(self, words: str) -> bool:
        return bool(words and not tools.is_fragment(words) and not (self.desk.fresh() and cards.answers_card(words))
                    and all(tools.plain(ref.transcript) != tools.plain(words) for ref in self.pending_calls.values()))

    async def _hand_over(self, words: str, response_id: str | None, *, announce: bool = False) -> None:
        """Hand the user's words over as assistant_ask would; ``announce``: the reply never said so, or said
        it was already done."""
        log.info("voice handed over call=%s announce=%s", self.call_id, announce)
        ref = VoiceTurnRef(id=generate_id(), provider_call_id=f"promise:{response_id}"[:64], text=words,
                           transcript=words, requested=self.clock(), context=self._call_context(words))
        if self.utterance == words:
            self.utterance = ""
        if not self.pending_calls and self.progress is not None:
            self.progress.reset()
        self.pending_calls[ref.provider_call_id] = ref
        self.refs.append(ref)
        self._spawn(self._run_turn(ref))
        self._update_phase()
        if announce:
            self.next_aside = phrases.handed_over_instructions(words, self.lang)
            await self.fill_idle()

    async def _tell_recall(self, heard: Heard, found: list[dict]) -> None:
        """What the user's records say about a question the reply missed: told in the next idle window."""
        log.info("voice recall added call=%s items=%s", self.call_id, len(found))
        ref = VoiceTurnRef(id=generate_id(), provider_call_id=f"recall:{generate_id()}"[:64], text=heard.text,
                           transcript=heard.text, requested=self.clock(), recall=heard.number)
        ref.status, ref.speech, ref.reason, ref.settled = "ok", "；".join(entry["text"] for entry in found), "recall", \
            self.clock()
        ref.delivery = "queued"
        self.deliveries.append(ref)
        self._update_phase()
        await self.fill_idle()

    def _call_context(self, words: str) -> dict:
        """What the assistant gets besides the request: the user's own words and the call's last lines."""
        lines = [line for line in self.spoken.lines if line.role in ("user", "assistant")][-CONTEXT_LINES:]
        return {"heard": _bounded(words, HEARD_CHARS),
                "call": [f"{ROLES[line.role]}：{_bounded(line.text, LINE_CHARS)}" for line in lines]}

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
        settled_here = await self._plan(ref)
        if settled_here is not None:  # answered from the quick reads, or a question back: no assistant turn
            await self._settle(ref, settled_here)
            return
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

    async def _plan(self, ref: VoiceTurnRef) -> dict | None:
        """The request planned with the call in view (voice/handover.py): briefed for the assistant (None),
        or settled here, as ``link.wait`` would settle it.

        A question may be answered from the recall and the watch list, unless
        the decision model is sure the request is work. Hung up meanwhile, the
        request goes on as it was: its result still reaches the conversation.
        """
        if self.planner is None or self.scope is None or self.closing:
            return None
        reads = None
        if self.judge is not None:
            recent = [(line.role, line.text) for line in self.spoken.lines if line.role in ("user", "assistant")]
            speculative = self._spawn(self.judge.reads(self.scope, ref.text))
            verdict = await self._unless_closing(self.judge.route(ref.text, recent[-4:]))
            if verdict is not None and verdict.choice == "assistant" and verdict.confidence >= router.WORK_CONFIDENCE:
                speculative.cancel()
            else:
                reads = await self._unless_closing(speculative)
        if self.closing:
            return None
        plan = await self._unless_closing(self.planner(
            request=ref.text, words=ref.transcript, lines=list(self.spoken.lines), summary=self.keeper.summary,
            known=self.known, reads=reads, call_id=self.call_id))
        if self.debug and plan is not None:  # QA only: what the assistant (or the user) will get, in full
            log.info("voice plan call=%s kind=%s text=%s", self.call_id, plan.kind, plan.text)
        if plan is None or plan.kind == "brief":
            ref.text = plan.text if plan is not None else ref.text
            return None
        ref.lane = "local" if plan.kind == "answer" else "ask"
        await self.link.answered(ref)
        self._turn(ref, "accepted")
        return {"status": "ok", "speech": plan.text, "reason": ref.lane}

    async def _unless_closing(self, awaitable):
        """Its result, or None once the call is hanging up (the awaitable is cancelled)."""
        task = asyncio.ensure_future(awaitable)
        while not task.done():
            if self.closing:
                task.cancel()
                return None
            await asyncio.wait({task}, timeout=0.2)
        return None if task.cancelled() else task.result()

    def _on_message(self, ref: VoiceTurnRef) -> None:
        if ref.provider_call_id in self.pending_calls:
            self._turn(ref, "working")

    async def _settle(self, ref: VoiceTurnRef, result: dict) -> None:
        self.pending_calls.pop(ref.provider_call_id, None)
        ref.status, ref.speech, ref.settled = result["status"], result["speech"], self.clock()
        ref.cards = [card for card in result.get("cards") or [] if card["card_id"] not in self.desk.answered]
        ref.reason = result.get("reason") or ("ok" if ref.status == "ok" else ref.status)
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
        """The note goes in, then our reply is asked for at once: nothing else can read the note first.

        Results waiting together (a report nobody asked for, the user's own
        turn) ride along in the same note and reply, told one after another.
        """
        ref.attempts += 1
        if ref.note_item is None and not ref.note_pending:
            ref.riders = self._riders(ref)
            for rider in ref.riders:
                rider.delivery = "riding"
            if ref.cards:  # handles are issued as the card's content goes to the model
                shown = [cards.spoken_card(card, self.desk.show(card["card_id"]), self.lang) for card in ref.cards]
                ref.note = phrases.card_note(ref.transcript or ref.text, ref.speech, shown, self.lang)
            else:
                ref.note = phrases.joined_note([self._note_body(item) for item in (ref, *ref.riders)], self.lang)
            ref.note_pending, ref.note_at = True, None
            await self._command("create_note", ref.note)
            if ref.attempts == 1:
                self._said("note", ref.note)
        ref.delivery = "creating"
        self.requested = f"delivery:{ref.id}"
        group = [ref, *(ref.riders or [])]
        if ref.cards:
            instructions = phrases.card_instructions(self.lang)
        elif ref.status != "ok":
            instructions = phrases.notice_instructions(self.lang)
        elif ref.reason == "ask":
            instructions = phrases.ask_instructions(self.lang)
        elif ref.reason == "recall":
            instructions = phrases.recall_instructions(self.lang)
        elif len(group) > 1 or ref.report is not None:
            instructions = phrases.together_instructions(len(group), any(item.report is not None for item in group),
                                                         [item.speech for item in group], self.lang)
        else:
            instructions = phrases.delivery_instructions(ref.speech, self.lang)
        await self._create(instructions)

    def _riders(self, ref: VoiceTurnRef) -> list:
        """Plain results queued with this one, told in the same breath; never a card, a failure, a question
        back or a record added to a reply (each has its own words)."""
        if ref.cards or ref.status != "ok" or ref.reason in ("ask", "recall"):
            return []
        return [other for other in self.deliveries
                if other is not ref and other.delivery == "queued" and other.note_item is None
                and not other.note_pending and not other.cards and other.status == "ok"
                and other.reason not in ("ask", "recall")][:TOLD_TOGETHER - 1]

    def _drop_stale_recalls(self) -> None:
        """A record nobody heard yet is dropped once the user has said something new: the moment passed."""
        for ref in [ref for ref in self.deliveries if ref.recall is not None and ref.delivery == "queued"
                    and ref.recall != self.heard_count]:
            self.deliveries.remove(ref)
            if ref.note_item is not None:
                self._spawn(self._delete(ref.note_item))
            log.info("voice recall dropped call=%s", self.call_id)

    def _note_body(self, ref: VoiceTurnRef) -> str:
        return phrases.note_body(ref.reason, ref.transcript or ref.text, ref.speech, self.lang,
                                 report_title=ref.report)

    async def report(self, title: str, text: str, key: str) -> None:
        """A task's result the assistant reported while the call is on (voice/reports.py): told unasked."""
        from voice.speech_text import clean
        speech = clean(text, self.lang)
        if not speech or self.closing:
            return
        ref = VoiceTurnRef(id=generate_id(), provider_call_id=f"report:{key}"[:64], text=title, transcript="",
                           requested=self.clock(), report=title)
        ref.status, ref.speech, ref.reason, ref.settled = "ok", speech, "ok", self.clock()
        ref.delivery = "queued"
        self.deliveries.append(ref)
        self._update_phase()
        await self.fill_idle()

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
        riders, ref.riders = ref.riders or [], None
        if not heard and ref.attempts < DELIVERY_ATTEMPTS and not self.closing:
            # Nobody heard it: take the note out so no other reply reads it, and tell it again later.
            ref.delivery = "queued"
            for rider in riders:
                rider.delivery = "queued"
            if ref.note_item is not None:
                await self._delete(ref.note_item)
                ref.note_item, ref.note_at = None, None
            return
        self.keeper.note_delivered(ref.note_item)
        for item in (ref, *riders):
            await self._told(item, heard)

    async def _told(self, ref: VoiceTurnRef, heard: bool) -> None:
        self.deliveries.remove(ref)
        ref.delivery, ref.finished = "done", self.clock()
        if not ref.recorded:  # a report or an added record: no voice turn of this call to record
            log.info("voice %s told call=%s heard=%s", "report" if ref.report is not None else "recall",
                     self.call_id, heard)
            return
        if ref.status == "ok":
            self._turn(ref, "delivered")
        # A hang-up right after the result cancels the provider pump; the turn's record still lands.
        await asyncio.shield(self.link.done(ref, _outcome(ref, heard=heard), delivered=heard))

    async def _refused(self, ref: VoiceTurnRef, reason: str) -> None:
        """Our delivery request was refused: retried after the reply that won, or given up."""
        if ref.delivery == "covered":
            return  # the reply that won saw the note
        riders = ref.riders or []
        if reason == "active_response":
            ref.delivery = "queued"
            for rider in riders:
                rider.delivery = "queued"
            ref.riders = None
            return
        ref.riders = None
        for item in (ref, *riders):  # refused for another reason: say the result is in the text
            self.deliveries.remove(item)
            item.delivery, item.status = "done", "failed" if item.status == "ok" else item.status
            if item.recorded:
                self._turn(item, "failed")
                await asyncio.shield(self.link.done(item, _outcome(item)))
        if any(item.lane == "assistant" and item.recall is None for item in (ref, *riders)):
            self.next_phrase = "result_in_text"  # an answer made here is in no conversation: nothing to point at

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


def _bounded(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"
