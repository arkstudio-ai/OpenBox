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
from voice import cards, events, phrases, spoken_cards, tools
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
    async def _recover_handoff(self, source: str, words: str) -> None:
        from assistant.identities import inbox_key
        async with self._business_lock:
            guard = self.handoff
            if (guard is None or not guard.current(source) or source in guard.attempted
                    or source in self._spoken_actions):
                return
            guard.attempted.add(source)
            context = self._call_context(words, source_item=source)
            context.update(spoken_input_id=source, handover_source="missed_handoff")
            key = inbox_key("voice-handoff", self.call_id, self.session, source)
            ref = VoiceTurnRef(id=key, provider_call_id=key, text=words, transcript=words,
                               requested=self.clock(), context=context)
            try:
                await self.link.start(ref)
            except Exception as exc:
                log.warning("voice recovery acceptance failed call=%s error=%s", self.call_id, type(exc).__name__)
                guard.pending = (source, "failed")
                return
            guard.refs[source] = ref
            self._remember_spoken_action(source, {"status": "accepted", "request_id": ref.id,
                "operation_state": "received", "execution_confirmed": False,
                "note": "用户原话已经接收，请勿重复提交；任务是否创建以后台结果为准。"})
            self.pending_calls[key] = ref
            self.refs.append(ref)
            self._turn(ref, "accepted")
            self._spawn(self._observe_transcript(ref))
            self._update_phase()

    def _init_turns(self) -> None:
        self.followup_due = False   # tool outputs no reply has answered yet
        self._direct: set[str] = set()  # direct reads still running
        self._unverified_replies: set[str] = set()
        self._tool_receipts: list[dict] = []
        self._decision_failures: dict[tuple[str, int], int] = {}
        self._business_lock = asyncio.Lock()
        self._spoken_actions: dict[str, dict] = {}
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
        self._decision_deliveries: dict[str, VoiceTurnRef] = {}
        self._withdrawn_deliveries: set[str] = set()
        self._withdrawn_notes: set[str] = set()
        self._playing_decision: str | None = None
        self._waiting_versions: dict[str, str] = {}
        self._native_versions: dict[str, str] = {}  # direct-read cards use their spoken content version
        self._native_pending: dict[str, str] = {}
        self._native_playing: dict[str, str] = {}
        self._cards_epoch = 0
        self._native_read_epoch = 0
        self._card_sources = {}
        self._native_sources = {}
        self._native_text = ""
        self._native_response = None
        self._ack_at: float | None = None     # a silent handover is acknowledged then, unless its result came
        self._heard_replies: deque[str] = deque(maxlen=16)  # the latest replies the user heard words of
        self._silent_responses: deque[str] = deque(maxlen=32)  # keepalive tools may arrive after response.done
        self._delivery_sources: dict[str, VoiceTurnRef] = {}
        self._report_batch = 0

    async def _on_tool_call(self, event) -> None:
        if not event.call_id or event.call_id in self._seen_calls:
            return
        self._seen_calls.add(event.call_id)
        if event.response_id and event.response_id in self._stale_responses:
            await self._command("send_tool_output", event.call_id, {
                "status": "stale_response", "this_call_applied": False,
                "note": "This response was superseded before speech. Read current state in the new response."})
            return
        if (self.response_kind == "keepalive"
                or event.response_id and event.response_id in self._silent_responses):
            # A silent transport response must not read cards and trigger an
            # audible followup, or reuse the last human utterance for work.
            await self._command("send_tool_output", event.call_id, {
                "status": "not_user_request", "this_call_applied": False})
            return
        if self.unified:
            await self._command("send_tool_output", event.call_id, {
                "status": "handled_by_main_assistant", "execution_confirmed": False,
                "note": "原始转录已由服务端交给统一主助手；语音层不得另行执行或重复转交。"})
            return
        if event.name in {"cards_answer", "task_question_answer"} and (event.response_id or self.active_response):
            self._unverified_replies.add(event.response_id or self.active_response)
        self._tool_sources[event.call_id] = self._answering.get(event.response_id or self.active_response or "", self._last_user_item)
        response_kind = (self.handoff.responses.get(event.response_id or self.active_response, self.response_kind)
                         if self.handoff is not None else self.response_kind)
        handoff_retry = bool(response_kind and response_kind.startswith("handoff_retry:"))
        if (event.name in (tools.ASSISTANT_ASK, "cards_answer", "task_question_answer")
                and response_kind not in (None, "model", "followup") and not handoff_retry):
            await self._command("send_tool_output", event.call_id, {
                "status": "not_user_request", "this_call_applied": False,
                "note": "当前是系统事件播报，没有新的用户业务指令。"})
            return
        if handoff_retry and (self.handoff is None or not self.handoff.current(self._tool_sources[event.call_id])):
            await self._command("send_tool_output", event.call_id, {
                "status": "stale_request", "this_call_applied": False,
                "note": "用户已继续发言，不能重放上一条交办；以用户当前完整意思为准。"})
            return
        if self.handoff is not None and event.name in ("cards_answer", "task_question_answer"):
            self.handoff.attempted.add(self._tool_sources[event.call_id])
        if event.name == tools.ASSISTANT_ASK:
            await self._ask(event)
        elif event.name in tools.DIRECT:
            self._direct.add(event.call_id)
            self._spawn(self._direct_call(event))
        else:
            await self._command("send_tool_output", event.call_id, {"status": "unknown_tool"})

    async def _ask(self, event, *, waited: bool = False) -> None:
        source_item = self._tool_sources.get(event.call_id, "")
        kind = (self.handoff.responses.get(event.response_id or self.active_response, self.response_kind)
                if self.handoff is not None else self.response_kind)
        if (self.handoff is not None and kind
                and kind.startswith("handoff_retry:") and source_item in self.handoff.refs):
            existing = self.handoff.refs[source_item]
            await self._command("send_tool_output", event.call_id, {
                "status": "accepted" if existing.inbox_id else "received", "operation_state": "received", "request_id": existing.id,
                "execution_confirmed": False, "execution_result": None,
                "note": "这条要求正在核对转交结果，不重复提交；处理进度以实际结果为准。"})
            return
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
        if arguments.mode == "revise" and not identity:
            await self._command("send_tool_output", event.call_id, {
                "status": "revision_target_required", "this_call_applied": False,
                "note": "先用 cards_pending 找到用户正在修改的任务卡，再带 question_id 转交修改；不要先提交旧卡答案。"})
            self.followup_due = True
            return
        if identity and identity not in self.desk.questions:
            await self._command("send_tool_output", event.call_id, {
                "status": "unknown_question", "hint": "这张任务卡片未读过、已处理或已过期，先用 cards_pending 核对，不要换一张代答"})
            self.followup_due = True
            return
        context = self._call_context(words, question_id=identity, source_item=source_item)
        if arguments.mode == "revise":
            shown = self.desk.questions[identity]
            context["revision_request"] = {"request_id": identity, "version": shown["version"],
                                           "session_id": shown["session_id"]}
        if context.get("task_questions") and arguments.mode != "revise":
            identities = [q["request_id"] for q in context["task_questions"]]
            sequence = self._user_utterances.get(source_item, 0) if source_item else self.heard_count
            if not all(sequence > self.desk._question_shown.get(identity, sequence) for identity in identities):
                await self._command("send_tool_output", event.call_id, {
                    "status": "need_user_answer", "this_call_applied": False,
                    "note": "这次调用对应的用户发言早于卡片展示，需等待用户对卡片作答。"})
                self.followup_due = True
                return
        sequence = self._user_utterances.get(source_item, 0) if source_item else self.heard_count
        spoken_key = source_item or (f"utterance:{sequence}" if sequence else "")
        if spoken_key:
            context["spoken_input_id"] = spoken_key
        ref = VoiceTurnRef(id=generate_id(), provider_call_id=event.call_id, text=arguments.request,
                           transcript=words, requested=self.clock(), context=context,
                           lane="remember" if arguments.mode == "remember" else "assistant")
        self._asked = None
        # Acceptance must be durable before telling either the model or the
        # client. Hang-up may cancel observers, never an accepted business job.
        if not self.pending_calls and self.progress is not None:
            self.progress.reset()
        async with self._business_lock:
            if await self._repeat_spoken_action(event, spoken_key):
                return
            self.pending_calls[event.call_id] = ref
            self.refs.append(ref)
            try:
                await self.link.start(ref)
            except Exception as exc:
                log.warning("voice handover acceptance failed call=%s error=%s", self.call_id, type(exc).__name__)
                await self._command("send_tool_output", event.call_id, {
                    "status": "unavailable", "this_call_applied": False, "execution_confirmed": False})
                await self._settle(ref, {"status": "failed", "speech": phrases.speech_text("unavailable", self.lang),
                                         "reason": "unavailable"})
                return
            self._remember_spoken_action(spoken_key, {
                "status": "accepted", "request_id": ref.id, "operation_state": "received",
                "question_ids": [q["request_id"] for q in context.get("task_questions", [])],
                "execution_confirmed": False,
                "note": "这句用户原话已交给个人助理处理，不能再重复交接或从另一条路径提交卡片。"})
        self._turn(ref, "accepted")
        await self._command("send_tool_output", event.call_id, {
            "status": "accepted", "request_id": ref.id, "request": arguments.request, "operation_state": "received",
            "execution_confirmed": False, "execution_result": None,
            "note": "请求已接收，尚未取得执行结果；这不表示任务在排队，进度以后台实际结果为准。"})
        if event.response_id and event.response_id != self.active_response:
            # Handed over after its reply had ended (the answer's words came late): acknowledged only if that
            # reply said nothing and no result comes soon.
            if event.response_id not in self._heard_replies:
                self._ack_at = self.clock() + ACK_WAIT_SECONDS
        else:
            self._acks.add(event.response_id or self.active_response or "")
        if self.handoff is not None and source_item:
            self.handoff.attempted.add(source_item)
            self.handoff.refs.setdefault(source_item, ref)
        self._spawn(self._observe_transcript(ref))
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

    async def _repeat_spoken_action(self, event, key):
        previous = self._spoken_actions.get(key) if key else None
        if previous is None:
            return False
        if event.name == "task_question_answer" and previous.get("question_ids"):
            try:
                target = json.loads(event.arguments or "{}").get("request_id")
            except (ValueError, AttributeError):
                target = None
            if target and target not in previous["question_ids"]:
                return False
        await self._command("send_tool_output", event.call_id,
                            {**previous, "duplicate": True, "this_call_applied": False})
        self.followup_due = True
        return True

    def _remember_spoken_action(self, key, result):
        if key:
            self._spoken_actions[key] = dict(result)
            if len(self._spoken_actions) > 256:
                self._spoken_actions.pop(next(iter(self._spoken_actions)))

    async def _complete_direct_call(self, event) -> None:
        started, session, cards_epoch = self.clock(), self.session, self._cards_epoch
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
        if event.name == "task_question_answer":
            try:
                question_id = json.loads(event.arguments or "{}").get("request_id")
            except (ValueError, AttributeError):
                question_id = None
            if question_id and any(ref.status == "pending" and (
                    (ref.context or {}).get("revision_request", {}).get("request_id") == question_id
                    or any(q.get("request_id") == question_id for q in (ref.context or {}).get("task_questions", [])))
                    for ref in self.refs):
                await self._command("send_tool_output", event.call_id, {
                    "status": "revision_pending", "this_call_applied": False,
                    "note": "该任务卡片的要求已交接，等待处理回执；不能再提交旧方案的答案或重复要求用户确认旧卡。"})
                self.followup_due = True
                return
        key = (event.name, sequence)
        exhausted = decision and self._decision_failures.get(key, 0) >= 2
        if exhausted:
            result = {"status": "retry_exhausted", "this_call_applied": False,
                      "execution_confirmed": False, "retry_allowed": False,
                      "note": "这次回答未提交。停止自动重试，等待用户新的输入，或让用户在原卡片提交。"}
        else:
            if decision:
                spoken_key = source or (f"utterance:{sequence}" if sequence else "")
                async with self._business_lock:
                    if await self._repeat_spoken_action(event, spoken_key):
                        return
                    result = await tools.run(event.name, scope, event.arguments) if scope else dict(tools.UNAVAILABLE)
                    if result.get("this_call_applied") or result.get("status") == "decision_recorded":
                        self._remember_spoken_action(spoken_key, result)
            else:
                result = await tools.run(event.name, scope, event.arguments) if scope else dict(tools.UNAVAILABLE)
            if event.name == "task_question_answer" and result.get("status") == "revision_required":
                # The real form/ownership check found selected files that this
                # old card cannot accept. Preserve the request via the normal
                # durable handoff; never turn it into a partial old-card answer.
                request = (words + "\n所选素材：" + ", ".join(result["attachment_ids"])
                           + "。请结合原任务处理这次修改；旧卡答案尚未提交。")
                await self._ask(replace(event, name=tools.ASSISTANT_ASK, arguments=json.dumps({
                    "request": request, "mode": "revise", "question_id": result["request_id"]}, ensure_ascii=False)))
                return
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
        if self.handoff is not None and decision and result.get("this_call_applied") is True:
            self.handoff.fulfilled.add(source)
        while self.desk.answers:  # a card confirmed: the turn it resumes is followed like any other
            answer = self.desk.answers.pop(0)
            if not answer["declined"]:  # declined: nothing happens, and the model has said so
                self._follow(event.call_id, answer)
        if session != self.session:
            return  # its call was the dropped session's: the fresh one never saw it (the user is asked again)
        if event.name == "cards_pending" and result.get("status") == "ok":
            observed = {self.desk.card_id(c["card"]): c["version"] for c in result.get("cards", [])}
            observed.update({q["request_id"]: q["version"] for q in result.get("task_questions", [])})
            observed = {identity: version for identity, version in observed.items() if identity}
            if self._cards_epoch != cards_epoch and any(self._native_versions.get(k) != v for k, v in observed.items()):
                result = {"status": "stale", "next": "cards_pending", "note": "卡片已变化，请重新查询；不要复述旧选项。"}
            else:
                self._native_pending.update(observed)
                self._native_read_epoch = self._cards_epoch
        if event.name == "tasks_overview" and result.get("status") == "ok":
            tasks = result.get("tasks") or []
            observed = {row["session_id"]: row["version"] for row in tasks if row.get("version")}
            decisions = {row["pending_request"]["request_id"]: row["pending_request"]["version"]
                         for row in tasks if row.get("pending_request", {}).get("version")}
            if self._work_changed(observed) or (self._cards_epoch != cards_epoch and any(
                    self._waiting_versions.get(k) != v for k, v in decisions.items())):
                result = {"status": "stale", "next": "tasks_overview", "note": "任务状态已变化，请重新查询。"}
            else:
                self._native_work_pending.update(observed)
                # tasks_overview reads question_version; cards_pending can use
                # a different spoken-card version, so retain these separately.
                self._native_work_decisions.update(decisions)
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
        if "decision_versions" in result:
            ref.decision_versions = result["decision_versions"]
        # The link has just checked observed versions against SQL. A polling
        # snapshot may precede a newly read card; do not treat that as removal.
        if result.get("speech_stale") or ("decision_versions" not in result and self._decisions_changed(ref)):
            # The work stays in the text conversation. Speech prepared before a
            # manual decision must not ask the user to make that decision again.
            ref.delivery, ref.finished = "superseded", self.clock()
            await asyncio.shield(self.link.done(ref, "cancelled", delivered=False))
            self._update_phase()
            return
        if ref.lane == "remember" and not ref.cards:
            await self._kept_quietly(ref)
            return
        if not self._claim_delivery(ref):
            # A report observer may win the race. The requesting VoiceTurn
            # still needs the real playback outcome, not a false cancellation.
            owner = ref.duplicate_of
            ref.delivery = "shared"
            owner.asked = owner.asked or ref.transcript or ref.text
            owner.listeners = [*(owner.listeners or []), ref]
            if owner.finished is not None:
                await self._finish_listeners(owner, owner.heard)
            self._update_phase()
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
        if not await self._read_current_context():
            # A failed read is not evidence that an old status is still true.
            # Keep the result queued for the next tick; never spin or replay it.
            return
        if ref not in self.deliveries or not self._can_inject():
            return
        self._withdrawn_deliveries.discard(f"delivery:{ref.id}")
        if ref.cards or ref.questions or ref.decision_versions:
            self._decision_deliveries[ref.id] = ref
            self._playing_decision = ref.id
        else:
            self._playing_decision = None
        ref.attempts += 1
        ref.delivery_requested_at = self.clock()
        if ref.settled is not None:
            log.info("voice delivery request call=%s turn=%s source=%s attempt=%s queue_ms=%.1f",
                     self.call_id, ref.id, ref.provider_call_id, ref.attempts,
                     max(ref.delivery_requested_at - ref.settled, 0) * 1000)
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
            self._withdrawn_notes.discard(ref.note)
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
        if any(item.interrupted for item in group):
            instructions += ("\n上次汇报被打断，没有确认完整播放。结合用户刚才的话和当前结果简短接续，"
                "不要重复称呼、问候、‘好的老板’或开场白；不要假定上次整段已听完。")
        await self._create(instructions)

    def _riders(self, ref: VoiceTurnRef) -> list:
        """Plain results queued with this one, told in the same breath ("另外，刚才那个…"); never a card, a
        failure, a question back or a record added to a reply (each has its own words), nor a report blocked by the user’s report preference."""
        if ref.cards or ref.questions or ref.decision_versions or ref.status != "ok" or ref.reason in ("ask", "recall", "running"):
            return []
        return [other for other in self.deliveries
                if other is not ref and other.delivery == "queued" and other.note_item is None
                and not other.note_pending and not other.cards and not other.questions and not other.decision_versions and other.status == "ok"
                and other.reason not in ("ask", "recall", "running")][:TOLD_TOGETHER - 1]

    def _note_body(self, ref: VoiceTurnRef) -> str:
        body = json.loads(phrases.note_body(ref.reason, ref.transcript or ref.text, ref.speech, self.lang,
                          report_title=ref.report, asked=ref.asked,
                          running=[task["title"] for task in ref.running or []]))
        body["request_id"] = ref.id
        if questions := (ref.context or {}).get("task_questions"):
            body["answer_context"] = questions
        return json.dumps(body, ensure_ascii=False)

    def _claim_delivery(self, ref):
        if ref.status != "ok":
            return True  # a timeout/failure must not consume the eventual result
        keys = (["inbox:" + ref.inbox_id] if ref.inbox_id else [])
        if ref.result_id:
            keys.append("result:" + ref.result_id)
        previous = next((self._delivery_sources[key] for key in keys if key in self._delivery_sources), None)
        if previous is not None and previous is not ref:
            ref.duplicate_of = previous
            log.info("voice duplicate suppressed call=%s turn=%s existing=%s", self.call_id, ref.id, previous.id)
            return False
        self._delivery_sources.update({key: ref for key in keys})
        return True

    async def report_batch(self, reports):
        if not reports:
            return
        fresh = await self._read_current_context()
        self._report_batch += 1
        try:
            for report in reports:
                await self.report(report.title, report.text, report.inbox_id, report.task_id,
                    running=report.running, decision_versions=report.decision_versions,
                    result_id=report.result_id, task_revision=report.task_revision, _fresh=fresh)
        finally:
            self._report_batch -= 1
        await self.fill_idle()

    async def report(self, title: str, text: str, key: str, task_id: str = "", *, running=None,
                     decision_versions=None, result_id="", task_revision=None, _fresh=None) -> None:
        """A task's result the assistant reported while the call is on (voice/reports.py): told unasked, after
        the answer to any request still being done and with it (voice/bridge.py ``_next_delivery``), or, for a
        request of this call that was passed to the task, as its answer."""
        from voice.speech_text import clean
        speech = clean(text, self.lang)
        if not speech or self.closing:
            return
        asked = self._passed_on.get(task_id, "")
        if not asked and not self.tell_reports:
            log.info("voice report kept for the conversation call=%s", self.call_id)  # the user turned these off
            return
        ref = VoiceTurnRef(id=generate_id(), provider_call_id=f"report:{key}"[:64], text=title, transcript="",
                           requested=self.clock(), report=title, asked=asked, inbox_id=key,
                           task_id=task_id, task_revision=task_revision, result_id=result_id)
        ref.status, ref.speech, ref.reason, ref.settled = "ok", speech, "running" if running else "ok", self.clock()
        ref.running, ref.decision_versions = running, decision_versions
        # Report and context watchers race. A previously cached result ID is
        # not evidence that this newly committed report is obsolete. If the
        # read fails, queue it until _deliver can verify the current snapshot.
        if _fresh is None:
            _fresh = await self._read_current_context()
        if (_fresh and self._stale_work(ref)) or not self._claim_delivery(ref):
            return
        self._passed_on.pop(task_id, None)
        ref.delivery = "queued"
        self.deliveries.append(ref)
        self._update_phase()
        if not self._report_batch:
            await self.fill_idle()

    def seed_waiting_cards(self, main_cards: list[dict], questions: list[dict]) -> None:
        """A connection is not a request to replay pre-existing decisions.

        Do not mark these cards as shown: the model must read them explicitly
        before answering. A new version during the call may be announced.
        """
        self._initial_cards = {c["card_id"]: cards.version(c) for c in main_cards}
        self._initial_questions = {q["id"]: q.get("version", "") for q in questions}
        self._waiting_versions = {**{c["card_id"]: c.get("question_version") or cards.version(c)
                                    for c in main_cards}, **self._initial_questions}
        self._native_versions = {**{c["card_id"]: cards.version(c) for c in main_cards}, **self._initial_questions}
        self._card_sources = spoken_cards.snapshot(main_cards, questions)

    async def _track_spoken_cards(self, event, *, partial=False):
        if not event.response_id or event.response_id != self._native_response:
            return
        self._native_text = (self._native_text + event.text if partial else event.text)[-6000:]
        self._native_playing.update(spoken_cards.mentioned(self._native_text, self._native_sources))
        if (any(self._native_versions.get(k) != v for k, v in self._native_playing.items())
                and (self.active_response == event.response_id or self.playback_left() > 0)):
            self._native_playing.clear()
            self._native_sources = {}
            await self._interrupt_playback(source="decision_changed")

    def _decisions_changed(self, ref):
        return any(self._waiting_versions.get(identity) != version
                   for identity, version in (ref.decision_versions or {}).items())

    async def waiting_cards(self, main_cards: list[dict], questions: list[dict]) -> None:
        """Announce new decisions, respecting report preferences and the current conversation."""
        if self.closing:
            return
        self.desk.reconcile(main_cards, questions)
        question_ids = {q["id"] for q in questions}
        card_versions = {c["card_id"]: cards.version(c) for c in main_cards}
        question_versions = {q["id"]: q.get("version", "") for q in questions}
        self._cards_epoch += 1
        self._native_versions = {**card_versions, **question_versions}
        self._card_sources = spoken_cards.snapshot(main_cards, questions)
        self._waiting_versions = {**{c["card_id"]: c.get("question_version") or cards.version(c)
                                    for c in main_cards}, **question_versions}
        if any(self._waiting_versions.get(k) != v for k, v in self._native_work_decisions.items()):
            self._native_work_decisions.clear()
            self._native_context_changed = True
            self._native_work_stale = True
            if self._native_response and (self.active_response == self._native_response or self.playback_left() > 0):
                self.followup_due = True
                await self._interrupt_playback(source="decision_changed")
        self.decision_context = ("当前待决定事项（最新状态覆盖旧提醒；不在此列表中的旧卡片已不再等待，"
            "不要再次询问或代答，也不要据此声称业务完成）：\n" + json.dumps({
                "cards": list(card_versions), "task_questions": list(question_versions)}, ensure_ascii=False))
        if (self._native_playing and any(self._native_versions.get(k) != v for k, v in self._native_playing.items())
                and (self.active_response or self.playback_left() > 0)):
            self._native_playing.clear()
            self._native_sources = {}
            await self._interrupt_playback(source="decision_changed")
        notices = {ref.id: ref for ref in [*self._decision_deliveries.values(), *self.deliveries]}
        for notice in notices.values():
            stale = (self._decisions_changed(notice) or (notice.questions and not all(question_versions.get(q["id"]) == q.get("version", "")
                                                   for q in notice.questions))
                     or (notice.cards and not all(
                         card_versions.get(c["card_id"]) == cards.version(c) for c in notice.cards)))
            if stale:
                kind = f"delivery:{notice.id}"
                self._withdrawn_deliveries.add(kind)
                self._decision_deliveries.pop(notice.id, None)
                was_pending = notice in self.deliveries
                if was_pending:
                    self.deliveries.remove(notice)
                notice.delivery = "superseded"
                self._notified_questions.difference_update((q["id"], q.get("version", "")) for q in notice.questions or [])
                if notice.note:
                    self._withdrawn_notes.add(notice.note)
                if (self.response_kind == kind or self.requested == kind
                        or (self._playing_decision == notice.id and self.playback_left() > 0)):
                    await self._interrupt_playback(source="decision_changed")
                if was_pending and notice.recorded:
                    await asyncio.shield(self.link.done(notice, "cancelled", delivered=False))
                if notice.note_item:
                    await self._delete(notice.note_item)
        if self._asked:
            asked = self._asked[1]
            asked_ids = {q["id"] for q in asked.questions or []} | {
                q["request_id"] for q in (asked.context or {}).get("task_questions") or []}
            if asked_ids and not asked_ids & question_ids:
                self._asked = None
        await self._sync_instructions(force=True)
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
        if event.text in self._withdrawn_notes:
            self._spawn(self._delete(event.item_id))
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
        if not heard and (ref.paused_by_user or ref.attempts < DELIVERY_ATTEMPTS) and not self.closing:
            # Partial speech is not delivery. An interrupted unsolicited report
            # yields to the user's new turn instead of automatically repeating
            # after that turn answers using the same task facts.
            for item in (ref, *riders):
                item.delivery = "paused" if item.paused_by_user else "queued"
                item.interrupted = item.interrupted or ref.interrupted
            if ref.note_item is not None:
                await self._delete(ref.note_item)
                ref.note_item, ref.note_at = None, None
            if ref.note:
                self._withdrawn_notes.add(ref.note)
            ref.note_pending = False
            return
        self.keeper.note_delivered(ref.note_item)
        for item in (ref, *riders):
            await self._told(item, heard)
    async def _told(self, ref: VoiceTurnRef, heard: bool) -> None:
        if ref not in self.deliveries:
            return
        self.deliveries.remove(ref)
        ref.delivery, ref.finished, ref.heard = "done", self.clock(), heard
        if not heard:
            self._delivery_sources = {key: value for key, value in self._delivery_sources.items() if value is not ref}
        if heard and any(q.get("assistant_may_answer") for q in ref.questions or []):
            self._asked = (self.heard_count, ref)
        await self._finish_listeners(ref, heard)
        if not ref.recorded:  # a report or an added record: no voice turn of this call to record
            log.info("voice %s told call=%s heard=%s", "report" if ref.report is not None else "recall",
                     self.call_id, heard)
            return
        if ref.status == "ok":
            self._turn(ref, "delivered")
        # A hang-up right after the result cancels the provider pump; the turn's record still lands.
        await asyncio.shield(self.link.done(ref, _outcome(ref, heard=heard), delivered=heard))

    async def _finish_listeners(self, ref, heard):
        listeners, ref.listeners = ref.listeners or [], None
        for listener in listeners:
            listener.delivery, listener.finished, listener.heard = "done", self.clock(), heard
            if heard and listener.status == "ok":
                self._turn(listener, "delivered")
            await asyncio.shield(self.link.done(listener, _outcome(listener, heard=heard), delivered=heard))

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
            item.finished = self.clock()
            await self._finish_listeners(item, False)
            self._delivery_sources = {key: value for key, value in self._delivery_sources.items() if value is not item}
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
