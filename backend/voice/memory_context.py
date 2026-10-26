"""Native voice turn preparation; never dispatches ordinary speech to an agent.

Bailian's VAD can generate before final ASR. Cancel and quarantine that reply,
preserve user audio, then release one explicit response after current evidence
and the exact session instruction acknowledgement are ready.
"""
import asyncio
import copy
from collections import deque
from dataclasses import replace

from core.log import create_logger
from voice import events

log = create_logger("voice.memory")


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
            if not previous.get("viewed_task"):
                return None
            from voice.view_context import read, history_needed
            value = await read(db, replace(turn, read_policy=previous["route"]["read_policy"]),
                previous.get("_viewed_session_id"), include_history=history_needed(previous))
            if value == previous.get("viewed_task"):
                return None
            packet = copy.deepcopy(previous)
            packet.pop("viewed_task", None)
            if value:
                packet["viewed_task"] = value
            apply_budget(packet, get_config().memory)
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
        self.memory_applied_version = packet["context_version"]
        self._memory_revocation_epoch = packet.get("_revocation_epoch")
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
        self._prewarm_memory_connection()
        self._memory_generation += 1
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
        self.memory_packet = self.memory_turn = self.memory_budget = None
        self._memory_text = ""

    async def _memory_event(self, event):
        if self.prepare_memory is None:
            return False
        if event.kind == "response_started" and (self.requested is None or self._memory_pending):
            # Pending speech wins even over a greeting/notification requested
            # just before VAD. No speculative output can execute a tool.
            rid = event.response_id or ""
            claimed = self.requested is not None
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

    def _memory_delete(self, item_id):
        if item_id and item_id not in self._memory_deletions:
            provider = self.provider
            async def retire():
                try:
                    await provider.delete_item_confirmed(item_id)
                    if self.provider is provider:
                        self.spoken.item_deleted(item_id)
                except Exception as exc:
                    if self.provider is provider and not self.closing:
                        log.warning("voice retired output cleanup failed call=%s error=%s", self.call_id, type(exc).__name__)
                        self.failed.set()
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
        if self._memory_task and not self._memory_task.done():
            return  # duplicate ASR
        self._memory_started = self.clock()
        self._memory_task = self._spawn(self._prepare_memory(event, self._memory_generation, self.session))

    async def _prepare_memory(self, event, generation, session):
        from memory.orchestrator import render_memory_context
        from memory.packet import ReadBudget
        started = self.clock()
        automatic_seen = self._memory_auto_seen
        try:
            recent = RecentContext([{"role": line.role, "text": line.text} for line in self.spoken.context_lines
                      if line.role in {"user", "assistant"} and line.item_id != event.item_id][-4:],
                      self._memory_revocation_epoch)
            packet, base = await self.prepare_memory(self._memory_scope, event.text, recent)
            # ASR may arrive before response.created. Waiting for this turn's
            # VAD response prevents mistaking a late automatic reply for our
            # explicitly requested, freshly prepared answer.
            await asyncio.wait_for(automatic_seen.wait(), 2.0)
            async with asyncio.timeout(5):
                for value in list(self._memory_blocked.values()):
                    await value["done"].wait()
                for item_id in {item for value in self._memory_blocked.values() for item in value["items"]}:
                    self._memory_delete(item_id)
                if self._memory_deletions:
                    retired = await asyncio.gather(*(asyncio.shield(task) for task in self._memory_deletions.values()))
                    if not all(retired):
                        raise RuntimeError("context_cleanup_unconfirmed")
            if generation != self._memory_generation or session != self.session or self.closing:
                return
            if not await self._retire_memory_history(packet, event.item_id):
                raise RuntimeError("context_cleanup_unconfirmed")
            if generation != self._memory_generation or session != self.session or self.closing:
                return
            policy = packet["route"]["read_policy"]
            # Tool observations are per-turn evidence. Keeping old tool bodies
            # in the provider conversation would bypass the refreshed packet.
            for item in list(self.spoken.items):
                if item.type in {"function_call", "function_call_output"}:
                    self._memory_delete(item.id)
            if policy != "allowed":
                # The provider still holds old audio/tool messages. Remove only
                # those transient conversation items, preserving this utterance.
                for item in list(self.spoken.items):
                    if item.id != event.item_id:
                        self._memory_delete(item.id)
            if self._memory_deletions:
                retired = await asyncio.gather(*(asyncio.shield(task) for task in self._memory_deletions.values()))
                if not all(retired):
                    raise RuntimeError("context_cleanup_unconfirmed")
            self.memory_packet = packet
            self.memory_turn = replace(packet["_turn"], read_policy=policy, turn_id=event.item_id)
            self.memory_budget = ReadBudget()
            self.base_instructions = base
            self._memory_text = render_memory_context(packet)
            if not await self._sync_instructions(force=True):
                raise RuntimeError("context_ack_missing")
            if generation != self._memory_generation or session != self.session or self.closing:
                return
            self._memory_applied(packet)
            self._memory_pending, self._memory_ready = False, True
            self.awaiting_since = None
            log.info("voice memory ready call=%s generation=%d version=%s status=%s elapsed_ms=%d view=%s policy=%s tokens=%s",
                self.call_id, generation, self.memory_applied_version, packet["status"],
                round((self.clock() - started) * 1000), (packet.get("viewed_task") or {}).get("session_id"),
                policy, packet.get("budget", {}).get("tokens"))
            await self.fill_idle()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if generation == self._memory_generation and session == self.session and not self.closing:
                log.warning("voice memory preparation failed call=%s error=%s", self.call_id, type(exc).__name__)
                self._memory_pending = False
                self.failed.set()  # fail visibly; never speak from unacknowledged stale context

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
                        raise RuntimeError("context_cleanup_unconfirmed")
                    if generation != self._memory_generation or session != self.session or self.closing:
                        return
                    self._memory_text = render_memory_context(packet)
                    # Render the same fresh settings whose version is marked
                    # applied after ACK. The stored packet remains old until
                    # then, so reading it here can send the previous language.
                    if not await self._sync_instructions(force=True, memory_packet=packet):
                        raise RuntimeError("context_ack_missing")
                    if generation != self._memory_generation or session != self.session or self.closing:
                        return
                    self.memory_packet = packet
                    self._memory_applied(packet)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("voice final memory refresh failed call=%s error=%s", self.call_id, type(exc).__name__)
                self._memory_ready = False
                self.failed.set()
                return
        self._memory_ready = False
        self.requested = "model"
        await self._create(None)

    def _memory_reset(self):
        if self._memory_task and not self._memory_task.done():
            self._memory_task.cancel()
        self._memory_task = None
        self._memory_generation += 1
        self._memory_pending = self._memory_ready = False
        self._memory_blocked.clear()
        self._memory_deletions.clear()
        self._memory_expected_autos.clear()
