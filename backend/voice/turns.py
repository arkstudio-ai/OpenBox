"""Native realtime tool calls and event-driven delivery of background results.

Only function calls dispatch work or answer cards. Speech is conversation, never
an executable command. Provider VAD, playback, call IDs and result delivery are
transport state, independent of the user's language or phrasing.
"""
import asyncio
import json
from collections import deque
from dataclasses import replace

from core.identifier import generate_id
from core.log import create_logger
from voice import cards, events, phrases, tools
from voice.assistant_link import VoiceTurnRef, turn_outcome as _outcome
from voice.transcript import ROLES

log = create_logger("voice.turns")
COVER_SECONDS = 1.5
DELIVERY_ATTEMPTS = 3
TOLD_TOGETHER = 3
CONTEXT_LINES, HEARD_CHARS = 20, 480
ACK_WAIT_SECONDS = 3.0
TRANSCRIPT_WAIT_SECONDS = 2.5


class TurnsMixin:
    def _init_turns(self) -> None:
        self.followup_due = False   # tool outputs no reply has answered yet
        self._direct: set[str] = set()  # direct reads still running
        self._unverified_replies: set[str] = set()
        self._tool_receipts: list[dict] = []
        self._decision_failures: dict[tuple[str, int], int] = {}
        self._acks: set[str] = set()    # replies that carried an assistant_ask
        self._replies: dict[str, str] = {}  # reply → words heard, for acknowledgement and reconnect handling
        self.heard_count = 0            # user utterances transcribed so far (CardDesk: spoken after a card?)
        # Real time: it waits for a transcript still on its way, whatever clock the call runs on.
        self.desk = cards.CardDesk(lambda: (self.heard_count, self.utterance))
        self._answering: dict[str, str] = {}  # the model's own reply → the user audio item it answers
        self._last_user_item = ""
        self._tool_sources: dict[str, str] = {}
        self._user_utterances: dict[str, int] = {}  # original audio item → transcript sequence
        self._asked: tuple[int, VoiceTurnRef] | None = None  # a told result asking the user something
        self._passed_on: dict[str, str] = {}  # task id → the user's words of the request it was passed to
        self._notified_questions: set[tuple[str, str]] = set()
        self._initial_cards: dict[str, str] = {}
        self._initial_questions: dict[str, str] = {}
        self._ack_at: float | None = None     # a silent handover is acknowledged then, unless its result came
        self._heard_replies: deque[str] = deque(maxlen=16)  # the latest replies the user heard words of

    async def _on_tool_call(self, event) -> None:
        if not event.call_id or event.call_id in self._seen_calls:
            return
        self._seen_calls.add(event.call_id)
        if event.name in {"cards_answer", "task_question_answer"} and (event.response_id or self.active_response):
            self._unverified_replies.add(event.response_id or self.active_response)
        self._tool_sources[event.call_id] = self._answering.get(event.response_id or self.active_response or "", self._last_user_item)
        if (event.name in (tools.ASSISTANT_ASK, "cards_answer", "task_question_answer")
                and self.response_kind not in (None, "model", "followup")):
            await self._command("send_tool_output", event.call_id, {
                "status": "not_user_request", "this_call_applied": False,
                "note": "当前是系统事件播报，没有新的用户业务指令。"})
            return
        if event.name == tools.ASSISTANT_ASK:
            await self._ask(event)
        elif event.name in tools.DIRECT:
            self._direct.add(event.call_id)
            self._spawn(self._direct_call(event))
        else:
            await self._command("send_tool_output", event.call_id, {"status": "unknown_tool"})

    async def _ask(self, event, *, waited: bool = False) -> None:
        source_item = self._tool_sources.get(event.call_id, "")
        words = next((line.text for line in reversed(self.spoken.lines)
                      if line.role == "user" and line.item_id == source_item), "") if source_item else self.utterance
        if not words:
            if not waited:
                self._spawn(self._wait_transcript(event))
            else:
                await self._command("send_tool_output", event.call_id, {
                    "status": "need_user_input", "this_call_applied": False,
                    "note": "尚未收到这轮用户的原始转录，不能把工具参数当作用户授权。"})
                self.followup_due = True
            return
        try:
            arguments = tools.AskArguments.model_validate_json(event.arguments or "{}")
        except ValueError:
            await self._command("send_tool_output", event.call_id, {"status": "invalid_arguments", "this_call_applied": False})
            self.followup_due = True
            return
        identity = arguments.question_id
        if identity and identity not in self.desk.questions:
            await self._command("send_tool_output", event.call_id, {
                "status": "unknown_question", "hint": "这张任务卡片未读过、已处理或已过期，先用 cards_pending 核对，不要换一张代答"})
            self.followup_due = True
            return
        context = self._call_context(words, question_id=identity, source_item=source_item)
        if context.get("task_questions"):
            identities = [q["request_id"] for q in context["task_questions"]]
            sequence = self._user_utterances.get(source_item, 0) if source_item else self.heard_count
            if not all(sequence > self.desk._question_shown.get(identity, sequence) for identity in identities):
                await self._command("send_tool_output", event.call_id, {
                    "status": "need_user_answer", "this_call_applied": False,
                    "note": "这次调用对应的用户发言早于卡片展示，需等待用户对卡片作答。"})
                self.followup_due = True
                return
        ref = VoiceTurnRef(id=generate_id(), provider_call_id=event.call_id, text=arguments.request,
                           transcript=words, requested=self.clock(), context=context,
                           lane="remember" if arguments.mode == "remember" else "assistant")
        self._asked = None
        await self._command("send_tool_output", event.call_id, {
            "status": "accepted", "request_id": ref.id, "request": arguments.request, "operation_state": "queued",
            "execution_confirmed": False, "execution_result": None,
            "note": "请求已接收，尚未取得执行结果；后台结果会关联此 request_id 返回。"})
        if event.response_id and event.response_id != self.active_response:
            # Handed over after its reply had ended (the answer's words came late): acknowledged only if that
            # reply said nothing and no result comes soon.
            if event.response_id not in self._heard_replies:
                self._ack_at = self.clock() + ACK_WAIT_SECONDS
        else:
            self._acks.add(event.response_id or self.active_response or "")
        if not self.pending_calls and self.progress is not None:
            self.progress.reset()  # a step from an earlier run is not this request's
        self.pending_calls[event.call_id] = ref
        self.refs.append(ref)
        self._spawn(self._run_turn(ref))
        self._update_phase()

    async def _wait_transcript(self, event) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TRANSCRIPT_WAIT_SECONDS
        source = self._tool_sources.get(event.call_id, "")
        while not self.closing and loop.time() < deadline:
            if (any(line.role == "user" and line.item_id == source for line in self.spoken.lines)
                    if source else self.utterance):
                break
            await asyncio.sleep(0.05)
        if not self.closing:
            await self._ask(event, waited=True)
            await self.fill_idle()

    async def _direct_call(self, event) -> None:
        try:
            await self._complete_direct_call(event)
        finally:
            self._direct.discard(event.call_id)
        await self.fill_idle()

    async def _complete_direct_call(self, event) -> None:
        started, session = self.clock(), self.session
        source = self._tool_sources.get(event.call_id, "")
        words = self.utterance
        sequence = self.heard_count
        if event.name in {"cards_answer", "task_question_answer"}:
            deadline = asyncio.get_running_loop().time() + TRANSCRIPT_WAIT_SECONDS
            while (source not in self._user_utterances if source else not self.utterance) and asyncio.get_running_loop().time() < deadline and not self.closing:
                await asyncio.sleep(.05)
            words = next((line.text for line in reversed(self.spoken.lines)
                          if line.role == "user" and line.item_id == source), "") if source else self.utterance
            sequence = self._user_utterances.get(source, 0) if source else self.heard_count
        scope = replace(self.scope, transcript=words, utterance_index=sequence,
                        desk=self.desk) if self.scope else None
        decision = event.name in {"cards_answer", "task_question_answer"}
        key = (event.name, sequence)
        exhausted = decision and self._decision_failures.get(key, 0) >= 2
        if exhausted:
            result = {"status": "retry_exhausted", "this_call_applied": False,
                      "execution_confirmed": False, "retry_allowed": False,
                      "note": "这次回答未提交。停止自动重试，等待用户新的输入，或让用户在原卡片提交。"}
        else:
            result = await tools.run(event.name, scope, event.arguments) if scope else dict(tools.UNAVAILABLE)
            if decision and result.get("status") in {"invalid_arguments", "target_mismatch"}:
                self._decision_failures[key] = self._decision_failures.get(key, 0) + 1
                if self._decision_failures[key] >= 2:
                    result = {**result, "retry_allowed": False,
                              "next": "Explain that the answer was not submitted. Stop tool retries and wait for the user."}
                if len(self._decision_failures) > 32:
                    self._decision_failures.pop(next(iter(self._decision_failures)))
        # What was really read, never its content: "did it check before it answered?" is in the log.
        log.info("voice tool call=%s tool=%s status=%s ms=%d", self.call_id, event.name, result.get("status"),
                 round((self.clock() - started) * 1000))
        if self.closing:
            return
        while self.desk.answers:  # a card confirmed: the turn it resumes is followed like any other
            answer = self.desk.answers.pop(0)
            if not answer["declined"]:  # declined: nothing happens, and the model has said so
                self._follow(event.call_id, answer)
        if session != self.session:
            return  # its call was the dropped session's: the fresh one never saw it (the user is asked again)
        if event.name in {"cards_answer", "task_question_answer", "platforms_status"}:
            self._tool_receipts.append({"tool": event.name, **result})
            self._tool_receipts = self._tool_receipts[-5:]
        if await self._command("send_tool_output", event.call_id, result) and not exhausted:
            self.followup_due = True  # the model answers from the output in the next idle window

    def _call_context(self, words: str, *, question_id: str = "", source_item: str = "") -> dict:
        """What the assistant gets besides the request: the user's own words and the call's last lines."""
        lines = [line for line in self.spoken.lines if line.role in ("user", "assistant")]
        if source_item:
            end = next((index + 1 for index, line in enumerate(lines) if line.item_id == source_item), len(lines))
            lines = lines[:end]
        context = {"heard": _bounded(words, HEARD_CHARS),
                   "call": [f"{ROLES[line.role]}：{line.text}" for line in lines[-CONTEXT_LINES:]]}
        # AssistantLink bounds the whole provenance object, keeping complete lines where they fit.
        if len(lines) > CONTEXT_LINES:
            context["call_truncated"] = True
        if self.keeper.summary:
            context["summary"] = self.keeper.summary
        if self.detail == "detailed":
            context["detail"] = "detailed"  # the user wants fuller answers on the phone (voice/prompt.py)
        if questions := self.desk.question_context(question_id):
            context["task_questions"] = questions
        return context

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
        ref.reason = result.get("reason") or ("ok" if ref.status == "ok" else ref.status)
        ref.running = result.get("running") or None
        if ref.status == "ok" and ref.running and not ref.cards:
            ref.reason = "running"  # passed on: told as such, and the task's report answers the request
            self._passed_on.update({task["task_id"]: ref.transcript or ref.text for task in ref.running})
        if not self.pending_calls and self.progress is not None:
            self.progress.reset()
        if not self._request_pending():
            self._ack_at = None  # its result is in: that says more than an acknowledgement would
        if self.closing:
            return
        if ref.lane == "remember" and not ref.cards:
            await self._kept_quietly(ref)
            return
        if ref.status != "ok":
            self._turn(ref, ref.status)
        ref.delivery = "queued"
        self.deliveries.append(ref)
        self._update_phase()
        await self.fill_idle()

    async def _kept_quietly(self, ref: VoiceTurnRef) -> None:
        """Something said in passing, remembered (lane ``remember``): nobody asked to hear about it, so it is
        closed without a word; only a card it stopped at is read (2026-10-09: these were told unasked)."""
        ref.delivery, ref.finished = "done", self.clock()
        self._turn(ref, "delivered" if ref.status == "ok" else ref.status)
        self._update_phase()
        await asyncio.shield(self.link.done(ref, "delivered" if ref.status == "ok" else _outcome(ref)))

    def _request_pending(self) -> bool:
        """A request of the user's still being done (a quiet "remember this" is none)."""
        return any(ref.lane != "remember" for ref in self.pending_calls.values())

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
                shown = [cards.spoken_card(card, self.desk.show(card), self.lang) for card in ref.cards]
                ref.note = phrases.card_note(ref.transcript or ref.text, ref.speech, shown, self.lang)
            elif ref.questions:
                shown = [self.desk.show_question(question, self.lang) for question in ref.questions]
                ref.note = phrases.question_note(shown, self.lang)
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
        elif ref.questions:
            instructions = phrases.question_instructions(self.lang)
        elif ref.status != "ok" or ref.reason == "running":  # nothing found yet, whatever the reply's wording
            instructions = phrases.notice_instructions(self.lang)
        elif len(group) == 1 and (ref.context or {}).get("task_questions"):
            instructions = phrases.question_followup_instructions(self.lang)
        elif len(group) > 1 or (ref.report is not None and not ref.asked):
            instructions = phrases.together_instructions(
                len(group), sum(item.report is not None and not item.asked for item in group),
                [item.speech for item in group], self.lang)
        else:
            instructions = phrases.delivery_instructions(ref.speech, self.lang)
        await self._create(instructions)

    def _riders(self, ref: VoiceTurnRef) -> list:
        """Plain results queued with this one, told in the same breath ("另外，刚才那个…"); never a card, a
        failure, a question back or a record added to a reply (each has its own words), nor a report blocked by the user’s report preference."""
        if ref.cards or ref.questions or ref.status != "ok" or ref.reason in ("ask", "recall", "running"):
            return []
        return [other for other in self.deliveries
                if other is not ref and other.delivery == "queued" and other.note_item is None
                and not other.note_pending and not other.cards and not other.questions and other.status == "ok"
                and other.reason not in ("ask", "recall", "running")][:TOLD_TOGETHER - 1]

    def _note_body(self, ref: VoiceTurnRef) -> str:
        body = json.loads(phrases.note_body(ref.reason, ref.transcript or ref.text, ref.speech, self.lang,
                          report_title=ref.report, asked=ref.asked,
                          running=[task["title"] for task in ref.running or []]))
        body["request_id"] = ref.id
        if questions := (ref.context or {}).get("task_questions"):
            body["answer_context"] = questions
        return json.dumps(body, ensure_ascii=False)

    async def report(self, title: str, text: str, key: str, task_id: str = "") -> None:
        """A task's result the assistant reported while the call is on (voice/reports.py): told unasked, after
        the answer to any request still being done and with it (voice/bridge.py ``_next_delivery``), or, for a
        request of this call that was passed to the task, as its answer."""
        from voice.speech_text import clean
        speech = clean(text, self.lang)
        if not speech or self.closing:
            return
        asked = self._passed_on.pop(task_id, "")
        if not asked and not self.tell_reports:
            log.info("voice report kept for the conversation call=%s", self.call_id)  # the user turned these off
            return
        ref = VoiceTurnRef(id=generate_id(), provider_call_id=f"report:{key}"[:64], text=title, transcript="",
                           requested=self.clock(), report=title, asked=asked)
        ref.status, ref.speech, ref.reason, ref.settled = "ok", speech, "ok", self.clock()
        ref.delivery = "queued"
        self.deliveries.append(ref)
        self._update_phase()
        await self.fill_idle()

    def seed_waiting_cards(self, main_cards: list[dict], questions: list[dict]) -> None:
        """A connection is not a request to replay pre-existing decisions.

        Do not mark these cards as shown: the model must read them explicitly
        before answering. A new version during the call may be announced.
        """
        self._initial_cards = {c["card_id"]: cards.version(c) for c in main_cards}
        self._initial_questions = {q["id"]: q.get("version", "") for q in questions}

    async def waiting_cards(self, main_cards: list[dict], questions: list[dict]) -> None:
        """Announce new decisions, respecting report preferences and the current conversation."""
        if self.closing:
            return
        self.desk.reconcile(main_cards, questions)
        question_ids = {q["id"] for q in questions}
        card_versions = {c["card_id"]: cards.version(c) for c in main_cards}
        question_versions = {q["id"]: q.get("version", "") for q in questions}
        for notice in list(self.deliveries):
            stale = ((notice.questions and not any(question_versions.get(q["id"]) == q.get("version", "")
                                                   for q in notice.questions))
                     or (notice.reason == "card_notice" and not any(
                         card_versions.get(c["card_id"]) == cards.version(c) for c in notice.cards)))
            if stale and notice.delivery == "queued":
                self.deliveries.remove(notice)
                self._notified_questions.difference_update((q["id"], q.get("version", "")) for q in notice.questions or [])
                if notice.note_item:
                    await self._delete(notice.note_item)
        if self._asked:
            asked = self._asked[1]
            asked_ids = {q["id"] for q in asked.questions or []} | {
                q["request_id"] for q in (asked.context or {}).get("task_questions") or []}
            if asked_ids and not asked_ids & question_ids:
                self._asked = None
        if not self.tell_reports:
            return  # user-requested card reads and the request's own result still work
        queued_cards = {c["card_id"] for ref in self.deliveries for c in ref.cards or []}
        for card in main_cards:
            if self._initial_cards.get(card["card_id"]) == cards.version(card):
                continue
            # A pending voice turn returns its own confirmation through _settle. Let that path carry
            # both the card and its receipt, rather than racing it with a second announcement.
            if (self.pending_calls or self.desk._versions.get(card["card_id"]) == cards.version(card)
                    or card["card_id"] in queued_cards):
                continue
            notice = VoiceTurnRef(id=generate_id(), provider_call_id=f"card:{card['card_id']}"[:64], text="",
                                  transcript="", requested=self.clock(), report="", cards=[card],
                                  status="ok", reason="card_notice", delivery="queued")
            self.deliveries.append(notice)
        for question in questions:
            key = (question["id"], question.get("version", ""))
            if (self._initial_questions.get(question["id"]) == key[1]
                    or key in self._notified_questions
                    or self.desk.questions.get(question["id"], {}).get("version") == key[1]):
                continue
            self._notified_questions.add(key)
            notice = VoiceTurnRef(id=generate_id(), provider_call_id=f"question:{question['id']}"[:64], text="",
                                  transcript="", requested=self.clock(), report="", questions=[question],
                                  status="ok", reason="question_notice", delivery="queued")
            self.deliveries.append(notice)
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

    async def _delivered(self, ref: VoiceTurnRef | None, heard: bool, said: str = "") -> None:
        """Our delivery (or a reply that covered it) is done; ``said``: what it said, checked against the note."""
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
        if heard and any(q.get("assistant_may_answer") for q in ref.questions or []):
            self._asked = (self.heard_count, ref)
        if not ref.recorded:  # a report or an added record: no voice turn of this call to record
            log.info("voice %s told call=%s heard=%s", "report" if ref.report is not None else "recall",
                     self.call_id, heard)
            return
        if ref.status == "ok":
            self._turn(ref, "delivered")
        # A hang-up right after the result cancels the provider pump; the turn's record still lands.
        await asyncio.shield(self.link.done(ref, _outcome(ref, heard=heard), delivered=heard))

    async def _refused(self, ref: VoiceTurnRef, reason: str) -> None:
        """Our delivery request was refused (or never answered): retried after the reply that won, or given up."""
        if ref.delivery == "covered":
            return  # the reply that won saw the note
        riders = ref.riders or []
        if reason == "active_response" or (reason == "unanswered" and ref.attempts < DELIVERY_ATTEMPTS):
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
        if any(item.lane == "assistant" for item in (ref, *riders)):
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
