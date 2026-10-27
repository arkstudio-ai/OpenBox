"""Native voice turn preparation; never dispatches ordinary speech to an agent.

Bailian's VAD can generate before final ASR. Cancel and quarantine that reply,
preserve user audio, then release one explicit response after current evidence
and the exact session instruction acknowledgement are ready.
"""
import asyncio
import copy
from collections import deque
from dataclasses import replace
from types import SimpleNamespace

from core.log import create_logger
from voice import events

log = create_logger("voice.memory")

# A quarantined VAD reply gets this long to end after its cancel. A provider
# that keeps generating (measured 2026-10-26: a speculative tool call and no
# response.done for 15 s) is abandoned instead of ending the call.
QUARANTINE_SECONDS = 8.0
# Preparation still unfinished this long after the user stopped: answer the
# utterance without new memory evidence rather than dropping it.
DEGRADE_AFTER_SECONDS = 20
# A prepared answer the provider refuses (a reply it still holds) or never
# starts is requested again this long after, at most this many times.
MEMORY_ANSWER_RETRIES, MEMORY_ANSWER_RETRY_SECONDS = 3, 1.5
# Explicit restrictions retire earlier call context from the provider.
RETIRING_POLICIES = frozenset({"no_history", "current_only", "documents_only"})


def keeps_call_context(packet) -> bool:
    route = (packet or {}).get("route") or {}
    policy = route.get("read_policy")
    if policy == "allowed":
        return True
    if policy != "uncertain":
        return False
    # A restriction any judge saw stays in force whatever followed it: a
    # fallback that timed out, failed or stayed unsure cannot lift "don't use
    # my history" that the primary judge heard below its confidence bar.
    # Without one, the turn keeps what was already said in this call, so a
    # short follow-up stays coherent: a decision that never arrived, or judges
    # unsure only whether reads would help (measured 2026-10-26: "The fast one
    # is fine." came back as policy=allowed, role=none at confidence 1.0).
    # History and memory reads stay gated by the uncertain policy either way;
    # the call's live state (pending cards, task rows) is its own context.
    return not any(_restriction_seen(decision) for decision in (route, route.get("primary_decision") or {}))


def _restriction_seen(decision) -> bool:
    answer = decision.get("read_policy_answer") or {}
    role = decision.get("restriction_role") or {}
    return (answer.get("choice") not in (None, "allowed")
            or answer.get("restriction_role") == "access" or role.get("choice") == "access")


def restriction(route) -> str | None:
    """The read restriction a decision put on the call: its policy, "uncertain" when unnamed, None when none."""
    from memory.read_contract import narrowest
    route = route or {}
    if keeps_call_context({"route": route}):
        return None
    named = [value for decision in (route, route.get("primary_decision") or {})
             for value in (decision.get("read_policy"), (decision.get("read_policy_answer") or {}).get("choice"))
             if value in RETIRING_POLICIES]
    return narrowest(*named) if named else "uncertain"


def held_restriction(*routes):
    """The narrowest restriction among decisions about the same words, as a route; None when none restricts.

    Ties go to the first route given, so a fresh decision as narrow as a held
    one keeps its own evidence.
    """
    from memory.read_contract import narrowest
    found = [(name, route) for route in routes if route and (name := restriction(route))]
    if not found:
        return None
    held = narrowest(*(name for name, _ in found))
    return next((route for name, route in found if name == held), {"read_policy": held})


class RecentContext(list):
    """A server-bound epoch; callback consumers still receive ordinary lines."""
    def __init__(self, lines, epoch):
        super().__init__(lines)
        self.epoch = epoch


async def prewarm():
    from core.config import get_config
    from memory.providers.configured_read import warm_connection
    await warm_connection(get_config().memory)


async def prepare(scope, text, recent):
    from memory.packet import TurnContext, prepare_context
    turn = TurnContext(user_id=scope.user_id, workspace_id=scope.workspace_id,
        session_id=scope.main_session_id, turn_id=scope.call_id, query=text,
        include_all_projects=True, recent_context=tuple(recent), entry="voice",
        recent_context_epoch=getattr(recent, "epoch", None),
        visible_conversation_available=bool(scope.viewed_session_id))
    packet = await prepare_context(turn)
    from voice.view_context import attach
    await attach(scope, packet)
    return packet, instructions(scope, packet)


def instructions(scope, packet):
    from voice.prompt import FrontFacts, front_instructions, local_now
    settings = {**packet["settings"], **packet.get("settings_details", {})}
    facts = FrontFacts(name=settings.get("name", ""), address=settings.get("address", ""),
        language=settings.get("language"),
        native_memory=True,
        persona=settings.get("persona", ""), detail=settings.get("call_detail", "brief"),
        recap=settings.get("call_recap", True), reports=settings.get("call_reports", True),
        business=settings.get("business", ""))
    # Bridge appends current response settings after the evidence exactly once.
    return front_instructions(facts, scope.lang, local_now(), include_response_policy=False)


async def refresh(scope, previous):
    """Refresh an idle call locally, retaining its last resolved read restriction.

    No speech has authorized history on a new call. Only settings are sent then;
    later refreshes revalidate the existing packet and rebuild its bounded core.
    Routing/search remain tied to the next actual utterance, never the timer.
    """
    from core.config import get_config
    from db.base import get_db_session
    from memory.packet import (TurnContext, apply_budget, authorized_packet_state, _settings_from_extra,
                               _refresh_packet_snapshot, _complete_packet_refresh)
    turn = previous["_turn"] if previous else TurnContext(user_id=scope.user_id,
        workspace_id=scope.workspace_id, session_id=scope.main_session_id,
        turn_id=scope.call_id, query="", include_all_projects=True, entry="voice")
    async with get_db_session() as db:
        current, state = await authorized_packet_state(db, turn)
        revision = state["version"]
        if previous is not None and revision == previous["context_version"]:
            if not any(previous.get(key) for key in ("viewed_task", "assistant_context", "task_state")):
                return None
            from voice.view_context import refresh_live
            packet = copy.deepcopy(previous)
            await refresh_live(db, scope, packet)
            if previous.get("task_state"):
                from memory.retrieval import read_task_state
                fresh = await read_task_state(current, session_id=previous.get("_task_session_id"))
                # Observation time changes on every poll, not the evidence.
                before = {key: value for key, value in previous["task_state"].items() if key != "observed_at"}
                after = {key: value for key, value in fresh.items() if key != "observed_at"}
                if before != after:
                    packet["task_state"] = fresh
            apply_budget(packet, get_config().memory)
            if packet == previous:
                return None
            return packet, instructions(scope, packet)
        if previous is not None:
            packet = await _refresh_packet_snapshot(db, copy.deepcopy(previous), current,
                get_config().memory, state=state)
    config = get_config().memory
    if previous is None:
        packet = {"_semantic_packet": True, "_turn": turn, "context_version": revision,
            "settings": _settings_from_extra(state["extra"]), "route": {"read_policy": "uncertain", "memory_forbidden": True},
            "status": "restricted", "items": [], "stable_background": {"items": []},
            "known_gaps": [], "degraded_reasons": [], "_revocation_epoch": state["epoch"]}
        apply_budget(packet, config)
    else:
        packet = await _complete_packet_refresh(packet, previous, current, config)
    from voice.view_context import attach
    await attach(scope, packet)
    if previous is not None and packet == previous:
        return None
    return packet, instructions(scope, packet)


class MemoryContextMixin:
    def _init_memory(self, prepare_memory, refresh_memory=None, prewarm_memory=None):
        from voice.call_language import CallLanguage
        self.call_language = CallLanguage()
        self.prepare_memory = prepare_memory
        self.refresh_memory = refresh_memory
        self.prewarm_memory = prewarm_memory
        self._memory_warm_task = None
        self.memory_packet = None
        self.memory_turn = None
        self.memory_budget = None
        self._memory_scope = self.scope
        self._memory_generation = 0
        self._memory_pending = False
        self._memory_started = None
        self._memory_task = None
        self._memory_blocked = {}  # kept for late audio/text/tool events until socket replacement
        self._memory_deletions = {}
        self._memory_ready = False
        self._memory_auto_seen = asyncio.Event()
        self._memory_expected_autos = deque()
        self._memory_text = ""
        self._memory_sync = asyncio.Lock()
        self.memory_applied_version = None
        self._memory_revocation_epoch = None
        self._memory_last_packet = None  # the latest applied packet; settings survive a failed read
        self._memory_utterance = ""      # the item the pending preparation answers
        self._memory_retries = 0         # refused/unanswered explicit answers for this utterance
        self._memory_retry_at = None
        self._uncaptured: set[str] = set()  # final ASR not durably captured: answered without recall
        self._memory_detected = None     # (generation, route) once this turn's read decision is known
        self._restricted_items: dict[str, dict] = {}  # utterance → the restriction it carried, for carried retries
        self._turn_work = ""                # live task rows fixed for the current turn (turns._live_work)
        self._turn_work_status = {}         # …and each task's state when they were fixed

    def _prewarm_memory_connection(self):
        if (self.prewarm_memory is not None and not self.closing
                and (self._memory_warm_task is None or self._memory_warm_task.done())):
            self._memory_warm_task = self._spawn(self.prewarm_memory())

    async def refresh_idle_memory(self):
        """Apply SQL changes silently; a new utterance always supersedes this read."""
        if self.refresh_memory is None or not self._memory_idle():
            return
        generation, session, previous = self._memory_generation, self.session, self.memory_packet
        started = self.clock()
        fresh = await self.refresh_memory(self.scope, previous)
        if fresh is None:
            return
        async with self._memory_sync:
            if (not self._memory_idle() or generation != self._memory_generation
                    or session != self.session or previous is not self.memory_packet):
                return
            from memory.orchestrator import render_memory_context
            packet, base = fresh
            if not await self._retire_memory_history(packet):
                return
            if generation != self._memory_generation or session != self.session or self.closing:
                return
            self.base_instructions = base
            self._memory_text = render_memory_context(packet)
            text = self._instructions(memory_packet=packet)
            if not await self._command("update_instructions_confirmed", text):
                # Keep the old version so the next idle check retries. The
                # next utterance also rebuilds from authority independently.
                return
            self._sent_instructions, self._instructions_at = text, self.clock()
            if generation != self._memory_generation or session != self.session or self.closing:
                return
            self.memory_packet = packet
            self._memory_applied(packet)
            log.info("voice idle memory applied call=%s version=%s elapsed_ms=%d reports=%s detail=%s",
                self.call_id, self.memory_applied_version, round((self.clock() - started) * 1000),
                self.tell_reports, self.detail)

    def _memory_idle(self):
        return (not self.closing and not self.greeting and not self.recovering and not self.swapping
            and not self.rotating and self.state == "idle" and self.active_response is None
            and self.requested is None and not self._memory_pending and not self._memory_ready
            and not self.pending_calls and not self._memory_deletions_pending())

    def _memory_deletions_pending(self):
        return any(not task.done() for task in self._memory_deletions.values())

    def _memory_applied(self, packet):
        """The provider confirmed this packet's instructions: its version is now in effect."""
        self._memory_last_packet = packet
        self.memory_applied_version = packet["context_version"]
        self._memory_revocation_epoch = packet.get("_revocation_epoch")
        self._memory_settings(packet)

    def _memory_settings(self, packet):
        settings = packet["settings"]
        # These flags also control bridge behavior outside the model prompt.
        self.tell_reports = settings.get("call_reports", True)
        self.detail = settings.get("call_detail", "brief")
        self.recap = settings.get("call_recap", True)

    async def _retire_memory_history(self, packet, preserve_item=""):
        """A refreshed prompt cannot revoke facts already spoken into the socket.

        On a correction/revocation epoch change, retire the old provider
        messages and derived summaries. Durable chats/tasks remain untouched;
        the current utterance and freshly verified memory remain available.
        """
        epoch = packet.get("_revocation_epoch")
        if self._memory_revocation_epoch is None or epoch is None or self._memory_revocation_epoch == epoch:
            return True
        for item in list(self.spoken.items):
            if item.id != preserve_item:
                self._memory_delete(item.id)
        await self.keeper.invalidate_context(preserve_item)
        if self.context_sink and not self.closing:
            self._context_dirty = True
            if self._context_task is None or self._context_task.done():
                self._context_task = self._spawn(self._share_context())
        if self._memory_deletions:
            retired = await asyncio.gather(*(asyncio.shield(task) for task in self._memory_deletions.values()))
            if not all(retired):
                self.failed.set()
                return False
        log.info("voice transient history retired call=%s from_epoch=%s to_epoch=%s",
                 self.call_id, self._memory_revocation_epoch, epoch)
        return True

    def _memory_new_speech(self):
        if self.prepare_memory is None:
            return
        self.call_language.begin_turn()
        self._prewarm_memory_connection()
        self._memory_generation += 1
        self._native_work_read = False
        self._native_work_pending = {}
        self._native_work_decisions = {}
        self._native_work_stale = False
        self._memory_scope = self.scope
        if self._memory_task and not self._memory_task.done():
            self._memory_task.cancel()
        self._memory_task = None
        if self.active_response and self.active_response not in self._memory_blocked:
            self._memory_blocked[self.active_response] = {
                "done": asyncio.Event(), "items": set(), "was_active": True}
        self._memory_pending, self._memory_ready = True, False
        self._memory_auto_seen = asyncio.Event()
        self._memory_expected_autos.append((self._memory_generation, self._memory_auto_seen))
        self._memory_started = self.clock()
        self._memory_utterance, self._memory_retries, self._memory_retry_at = "", 0, None
        self.memory_packet = self.memory_turn = self.memory_budget = None
        self._memory_text = self._turn_work = ""

    async def _memory_event(self, event):
        if self.prepare_memory is None:
            return False
        if event.kind == "response_started" and (self.requested is None or self._memory_pending):
            # Pending speech wins even over a greeting/notification requested
            # just before VAD. No speculative output can execute a tool.
            rid = event.response_id or ""
            claimed = self.requested is not None
            if claimed and (ref := self._delivery(self.requested)) is not None and ref.delivery == "creating":
                # Our telling never begins: whichever reply this is gets
                # cancelled, and a refusal of our request is absorbed below.
                # It waits, untold, until the user's turn is answered.
                self._requeue(ref)
            if (not self._memory_pending and self._last_user_item
                    and self._last_user_item not in self._user_utterances):
                self._memory_new_speech()
            if not claimed and self._memory_expected_autos:
                _, seen = self._memory_expected_autos.popleft()
                seen.set()
            self._memory_blocked[rid] = {"done": asyncio.Event(), "items": set(), "uncertain_origin": claimed}
            self.requested = None
            self.meter.start(rid)
            self.settled.clear()
            await self._command("cancel_response")
            return True
        if event.kind == "provider_error" and event.reason == "active_response" and self._memory_pending:
            uncertain = next((value for value in reversed(list(self._memory_blocked.values()))
                              if value.get("uncertain_origin")), None)
            if uncertain is not None and self._memory_expected_autos:
                uncertain["uncertain_origin"] = False
                _, seen = self._memory_expected_autos.popleft()
                seen.set()
                return True
        blocked = self._memory_blocked.get(event.response_id or "")
        if event.kind == "item_created" and (event.role == "assistant" or event.item_type == "function_call"):
            # item.created can precede output_item.added. It belongs to the
            # still-active quarantined response, not the user's audio item.
            active = next((value for value in reversed(list(self._memory_blocked.values()))
                           if not value["done"].is_set()), None)
            if active is not None:
                active["items"].add(event.item_id)
                return True
        if blocked is None:
            return False
        if event.kind == "tool_call":
            await self._void_quarantined_call(event)
            return True
        if event.item_id and event.kind in {"output_item", "assistant_transcript", "assistant_transcribing"}:
            blocked["items"].add(event.item_id)
            self.spoken.item_deleted(event.item_id)
            if blocked["done"].is_set():
                self._memory_delete(event.item_id)
        if event.kind == "audio":
            self.meter.audio(event.response_id, event.audio, event.event_id)
        elif event.kind == "response_done":
            blocked["items"].update(event.output_items)
            if blocked.get("was_active"):
                blocked["done"].set()
                for item_id in blocked["items"]:
                    self._memory_delete(item_id)
                await self._on_response_done(event)
                return True
            self.meter.settle(event.response_id, event.status, event.usage)
            self.keeper.responded()  # provider idle clock only, never add its answer to history
            if not self.meter.pending:
                self.settled.set()
            self.emit(events.cost(self.meter.snapshot()))
            blocked["done"].set()
            for item_id in blocked["items"]:
                self._memory_delete(item_id)
            if not self._memory_pending:
                self._spawn(self.fill_idle())
        return True

    async def _void_quarantined_call(self, event):
        """A speculative reply never executes, but each call still gets exactly one output.

        Left unanswered, the provider may hold that response open and the
        quarantine never ends. The prepared answer decides again afterwards.
        """
        if not event.call_id or event.call_id in self._seen_calls:
            return
        self._seen_calls.add(event.call_id)
        await self._command("send_tool_output", event.call_id, {
            "status": "superseded", "this_call_applied": False, "execution_confirmed": False,
            "note": "这条自动回复在核对上下文之前已作废，没有执行任何操作；以随后的正式回复为准。"})

    def _memory_delete(self, item_id):
        if item_id and item_id not in self._memory_deletions:
            provider = self.provider
            async def retire():
                try:
                    await provider.delete_item_confirmed(item_id)
                    if self.provider is provider:
                        self.spoken.item_deleted(item_id)
                except Exception as exc:
                    # Callers that depend on the removal (an explicit read
                    # restriction, a revocation) check the result themselves.
                    if self.provider is provider and not self.closing:
                        log.warning("voice retired output cleanup failed call=%s error=%s", self.call_id, type(exc).__name__)
                    return False
                finally:
                    # This task is still running here. Schedule after its
                    # completion so the final response barrier sees it done.
                    asyncio.get_running_loop().call_soon(lambda: self._spawn(self.fill_idle()) if not self.closing else None)
                return True
            self._memory_deletions[item_id] = self._spawn(retire())

    def _memory_heard(self, event):
        if self.prepare_memory is None or not event.text.strip():
            return
        if event.item_id and self._last_user_item and event.item_id != self._last_user_item:
            return  # delayed final ASR cannot replace a newer utterance
        if not self._memory_pending:
            self._memory_new_speech()  # providers may omit speech_started
            self._carried_note, self._carried_item, self._carry_due = "", "", False
        if self._memory_task and not self._memory_task.done():
            return  # duplicate ASR
        self._memory_started = self.clock()
        self._memory_utterance = event.item_id or self._last_user_item
        self._memory_task = self._spawn(self._prepare_memory(event, self._memory_generation, self.session))

    def skip_recall(self, item_id: str) -> None:
        """This utterance has no durable human source: answer it, but never recall from it."""
        self._uncaptured.add(item_id or "")

    def _memory_stale(self, generation, session):
        return generation != self._memory_generation or session != self.session or self.closing

    async def _prepare_memory(self, event, generation, session):
        started = self.clock()
        automatic_seen = self._memory_auto_seen
        stage = "read"
        try:
            pinned = self._restricted_items.get(event.item_id or "")
            # A retried or carried turn already carries its restriction: the
            # call's earlier lines may not enter this decision either.
            recent = RecentContext([] if pinned is not None else [
                {"role": line.role, "text": line.text} for line in self.spoken.context_lines
                if line.role in {"user", "assistant"} and line.item_id != event.item_id][-4:],
                self._memory_revocation_epoch)
            if (event.item_id or "") in self._uncaptured:
                packet, base = self._restricted_packet("uncaptured", pinned)
            else:
                packet, base = await self.prepare_memory(self._memory_scope, event.text, recent)
                held = held_restriction(packet.get("route"), pinned) if pinned is not None else None
                if held is not None and held is not packet.get("route"):
                    # These words already carried a restriction (a carried or
                    # retried turn): a later decision may narrow it, never
                    # lift or loosen it (current_only stays current_only).
                    packet, base = self._restricted_packet("pinned_restriction", held, enforced=True)
            if event.item_id and not keeps_call_context(packet):
                self._restricted_items[event.item_id] = packet["route"]
            self._memory_detected = (generation, packet.get("route") or {})
            # ASR may arrive before response.created. Waiting briefly for this
            # turn's VAD response prevents mistaking a late automatic reply for
            # our prepared answer; a provider that never starts one cannot hold
            # the user's turn hostage.
            stage = "automatic_reply"
            try:
                await asyncio.wait_for(automatic_seen.wait(), 2.0)
            except TimeoutError:
                log.info("voice automatic reply not seen call=%s generation=%d", self.call_id, generation)
            stage = "quarantine"
            await self._settle_quarantine()
            if self._memory_stale(generation, session):
                return
            if not await self._retire_memory_history(packet, event.item_id):
                return  # an unconfirmed revocation ends the call visibly (failed is set)
            if self._memory_stale(generation, session):
                return
            stage = "context"
            if not await self._retire_call_context(packet, event.item_id):
                return
            if self._memory_stale(generation, session):
                return
            stage = "instructions"
            await self._apply_memory_packet(packet, base, event.item_id, event.text, generation, session, started)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._memory_stale(generation, session) and not self.failed.is_set():
                log.warning("voice memory preparation failed call=%s stage=%s error=%s elapsed_ms=%d",
                            self.call_id, stage, type(exc).__name__, round((self.clock() - started) * 1000))
                await self._memory_degrade(event.item_id, event.text, generation, session, stage)

    async def _retire_call_context(self, packet, item_id) -> bool:
        """Remove what this turn's packet may not see; False when a restriction could not be honoured."""
        keep = keeps_call_context(packet)
        # Tool observations are per-turn evidence. Keeping old tool bodies
        # in the provider conversation would bypass the refreshed packet.
        removed = {item.id for item in self.spoken.items if item.type in {"function_call", "function_call_output"}}
        if not keep:
            # The provider still holds old audio/tool messages. Remove only
            # those transient conversation items, preserving this utterance.
            kept = {item_id, getattr(self, "_carried_item", "")}
            removed |= {item.id for item in self.spoken.items if item.id not in kept}
            for ref in self.deliveries:  # an untold note goes too: its telling adds it again
                if ref.note_item in removed:
                    ref.note_item, ref.note_pending, ref.note_at = None, False, None
        if not await self._deleted(removed) and not keep:
            # A restriction cannot be honoured while older call context may
            # remain in the provider conversation: end visibly instead.
            log.warning("voice restricted context cleanup unconfirmed call=%s", self.call_id)
            self._memory_pending = False
            self.failed.set()
            return False
        return True

    async def _settle_quarantine(self):
        """Let quarantined automatic replies end and retire their items; never fatal.

        A reply still running after its cancel is abandoned: its later events
        stay quarantined (no audio, no tools) and its items are retired as they
        appear. Our own answer is retried if the provider still refuses it.
        """
        try:
            async with asyncio.timeout(QUARANTINE_SECONDS):
                for value in list(self._memory_blocked.values()):
                    await value["done"].wait()
        except TimeoutError:
            stuck = [rid for rid, value in self._memory_blocked.items() if not value["done"].is_set()]
            log.warning("voice quarantined reply abandoned call=%s responses=%d", self.call_id, len(stuck))
            await self._command("cancel_response")
            for rid in stuck:
                self._memory_blocked[rid]["done"].set()
                if rid == self.active_response:
                    self.active_response = self.response_kind = self._claimed = None
        if not await self._deleted({item for value in self._memory_blocked.values() for item in value["items"]}):
            log.warning("voice quarantined output cleanup unconfirmed call=%s", self.call_id)

    async def _deleted(self, item_ids) -> bool:
        """Delete these items (once each) and report whether every removal was confirmed."""
        for item_id in item_ids:
            self._memory_delete(item_id)
        tasks = [self._memory_deletions[item_id] for item_id in item_ids if item_id in self._memory_deletions]
        if not tasks:
            return True
        return all(await asyncio.gather(*(asyncio.shield(task) for task in tasks)))

    async def _apply_memory_packet(self, packet, base, item_id, text, generation, session, started, *, degraded=""):
        from memory.orchestrator import render_memory_context
        from memory.packet import ReadBudget
        policy = packet["route"]["read_policy"]
        self.memory_packet = packet
        self.memory_turn = replace(packet["_turn"], read_policy=policy, turn_id=item_id)
        self.memory_budget = ReadBudget()
        self._turn_work = self._live_work()
        self.call_language.apply(packet, text)
        self.base_instructions = base
        self._memory_text = render_memory_context(packet)
        confirmed = await self._sync_instructions(force=True)
        if not confirmed and not self._memory_stale(generation, session):
            # A gateway's receipt can trail the update (measured up to 13 s);
            # the same text again is confirmed by whichever receipt arrives.
            confirmed = await self._sync_instructions(force=True)
        if self._memory_stale(generation, session):
            return
        if not confirmed:
            log.warning("voice instruction receipt missing call=%s generation=%d", self.call_id, generation)
            if not keeps_call_context(packet):
                # The service may still hold the previous instructions, with
                # earlier memory or task context, and an answer request carries
                # no replacement of its own (Audio sends only a short prompt
                # item). Never answer from it under a restriction.
                await self._replace_unconfirmed_session(item_id)
                return
            # Unrestricted: events on one socket apply in order, and a lost
            # turn is worse than a late receipt. Answer, but the version is not
            # established, so the idle refresh sends it again.
            self._memory_settings(packet)
        else:
            self._memory_applied(packet)
        self._memory_pending, self._memory_ready = False, True
        self.awaiting_since = None
        log.info("voice memory ready call=%s generation=%d version=%s status=%s elapsed_ms=%d route_ms=%s view=%s "
                 "policy=%s reason=%s tokens=%s degraded=%s",
            self.call_id, generation, self.memory_applied_version, packet["status"],
            round((self.clock() - started) * 1000), (packet.get("phase_timings") or {}).get("route_ms"),
            (packet.get("viewed_task") or {}).get("session_id"), policy,
            packet["route"].get("reason_code", "-"), packet.get("budget", {}).get("tokens"), degraded or "-")
        await self.fill_idle()

    async def _replace_unconfirmed_session(self, item_id):
        """A restricted turn whose instructions were never confirmed: leave the session they may not reach.

        Closing it hands the call to the provider pump's recovery: a fresh
        session opened with the current (restricted) instructions, the user's
        words carried into it and their restriction pinned. Without a fresh
        session the words go to the assistant with the current words only, and
        the call ends visibly rather than speak from the old instructions.
        """
        self._memory_pending, self._memory_ready = False, False
        if blocker := self._recovery_blocker():
            log.warning("voice restricted instructions unconfirmed call=%s recovery=%s", self.call_id, blocker)
            await self._hand_over_unanswered(item_id, "instructions_unconfirmed")
            self.failed.set()
            return
        log.warning("voice restricted instructions unconfirmed call=%s: replacing the session", self.call_id)
        await self._command("close")

    def _restricted_packet(self, reason, detected=None, *, enforced=False):
        """No memory evidence and no reads: only the latest applied settings.

        ``detected`` is this turn's read decision when it arrived before the
        failure; a restriction it carries stays in force for the answer.
        ``enforced``: preparation did answer, and only a decision weaker than
        the words' held restriction was set aside. A named restriction is then
        the turn's policy and keeps what it permits (no_history: task state).
        """
        from memory.packet import TurnContext
        previous = self._memory_last_packet or {}
        scope = self._memory_scope or self.scope
        turn = previous.get("_turn") or TurnContext(
            user_id=getattr(scope, "user_id", ""), workspace_id=getattr(scope, "workspace_id", ""), query="",
            session_id=getattr(scope, "main_session_id", None), turn_id=self.call_id,
            include_all_projects=True, entry="voice")
        route = {"called": False, "read_policy": "uncertain", "memory_forbidden": True,
                 "reason_code": "preparation_unavailable"}
        if detected and not keeps_call_context({"route": detected}):
            # Evidence of the restriction, never its reads: the policy stays
            # uncertain so nothing is recalled, and the call's context retires.
            restricted_answer = ({"choice": detected["read_policy"]} if detected.get("read_policy") in RETIRING_POLICIES
                                 else detected.get("read_policy_answer"))
            route.update({key: value for key, value in (("read_policy_answer", restricted_answer),
                ("restriction_role", detected.get("restriction_role")),
                ("primary_decision", detected.get("primary_decision"))) if value})
            if enforced and (held := restriction(detected)) in RETIRING_POLICIES:
                route.update(read_policy=held, reason_code="pinned_restriction")
        packet = {"_semantic_packet": True, "_turn": turn, "context_version": previous.get("context_version"),
            "settings": dict(previous.get("settings") or {}),
            "route": route,
            "status": "restricted" if enforced else "unavailable", "items": [], "stable_background": {"items": []},
            "known_gaps": [],
            "degraded_reasons": ["voice_preparation_" + reason], "_revocation_epoch": previous.get("_revocation_epoch")}
        if previous.get("settings_details"):
            packet["settings_details"] = previous["settings_details"]
        return packet, instructions(SimpleNamespace(lang=self.lang), packet)

    async def _memory_degrade(self, item_id, text, generation, session, reason):
        """Answer the utterance without new memory evidence instead of dropping the user's turn."""
        if self._memory_stale(generation, session):
            return
        if self._memory_task is not None and self._memory_task is not asyncio.current_task():
            self._memory_task.cancel()
        try:
            await self._settle_quarantine()  # bounded; a stuck speculative reply cannot hold the answer
            if self._memory_stale(generation, session):
                return
            detected = self._memory_detected[1] if (self._memory_detected or (None,))[0] == generation else None
            # A carried or retried turn's restriction holds even when this
            # attempt failed before deciding anything.
            packet, base = self._restricted_packet(
                reason, held_restriction(detected, self._restricted_items.get(item_id or "")))
            if not keeps_call_context(packet):
                # The failure may have come before the restricted cleanup ran.
                if not await self._retire_call_context(packet, item_id) or self._memory_stale(generation, session):
                    return
            await self._apply_memory_packet(packet, base, item_id, text, generation, session, self.clock(),
                                            degraded=reason)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("voice degraded answer unavailable call=%s error=%s", self.call_id, type(exc).__name__)
            self._memory_pending = False
            self.failed.set()

    async def _say_memory_answer(self):
        # Playback pacing can hold a prepared answer. Recheck SQL at the actual
        # response boundary so a setting changed during that wait wins too.
        generation, session = self._memory_generation, self.session
        if self.refresh_memory is not None:
            try:
                fresh = await self.refresh_memory(self.scope, self.memory_packet)
                if generation != self._memory_generation or session != self.session or self.closing:
                    return
                if fresh is not None:
                    from memory.orchestrator import render_memory_context
                    packet, self.base_instructions = fresh
                    if not await self._retire_memory_history(packet, self._last_user_item):
                        self._memory_ready = False
                        return  # an unconfirmed revocation ends the call visibly (failed is set)
                    if generation != self._memory_generation or session != self.session or self.closing:
                        return
                    self._memory_text = render_memory_context(packet)
                    # Render the same fresh settings whose version is marked
                    # applied after ACK. The stored packet remains old until
                    # then, so reading it here can send the previous language.
                    if not await self._sync_instructions(force=True, memory_packet=packet):
                        # Something changed while the answer waited; keep the
                        # acknowledged packet and ask again shortly, not on
                        # unconfirmed settings, without ending the call.
                        self._memory_ready = False
                        if generation == self._memory_generation and session == self.session:
                            self._retry_memory_answer("instructions")
                        return
                    if generation != self._memory_generation or session != self.session or self.closing:
                        return
                    self.memory_packet = packet
                    self._memory_applied(packet)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # The prepared packet is still the latest acknowledged evidence.
                log.warning("voice final memory refresh failed call=%s error=%s", self.call_id, type(exc).__name__)
                if generation != self._memory_generation or session != self.session or self.closing:
                    return
        self._memory_ready = False
        if self._state_changed:
            # A cancelled status answer may have required a fresh provider
            # session. The memory barrier still runs first; then tell the
            # current committed state, not the carried question's old premise.
            await self._say_followup()
            return
        self.requested = "model"
        await self._create(None)

    def _retry_memory_answer(self, why):
        """The provider refused or never started our prepared answer: ask again shortly, a few times."""
        self._memory_retries += 1
        if self._memory_pending or self.closing:
            return  # a newer utterance supersedes this answer, or the call is ending
        if self._memory_retries > MEMORY_ANSWER_RETRIES:
            log.warning("voice prepared answer abandoned call=%s why=%s attempts=%d",
                        self.call_id, why, self._memory_retries)
            # Never return to listening as if the turn had been answered: the
            # user's own words go to the assistant, whose answer reaches the
            # conversation and is told once the call can speak again.
            self._memory_ready = False
            self._spawn(self._hand_over_unanswered(self._memory_utterance or self._last_user_item, why))
            return
        log.info("voice prepared answer retry call=%s why=%s attempt=%d", self.call_id, why, self._memory_retries)
        if why == "active_response":
            # A reply we no longer track (an abandoned speculative one) still
            # holds the provider; it is never played, so end it.
            self._spawn(self._command("cancel_response"))
        self._memory_ready = True
        self._memory_retry_at = self.clock() + MEMORY_ANSWER_RETRY_SECONDS
        if not self.closing:
            asyncio.get_running_loop().call_later(MEMORY_ANSWER_RETRY_SECONDS + .05,
                                                  lambda: self._spawn(self.fill_idle()) if not self.closing else None)

    def _memory_reset(self):
        if self._memory_task and not self._memory_task.done():
            self._memory_task.cancel()
        self._memory_task = None
        self._memory_generation += 1
        self._memory_pending = self._memory_ready = False
        self._memory_blocked.clear()
        self._memory_deletions.clear()
        self._memory_expected_autos.clear()
