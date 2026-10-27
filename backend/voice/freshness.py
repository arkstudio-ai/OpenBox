"""Bind speech to committed work versions; freshness is independent of model wording."""
import asyncio
import json

from core.log import create_logger

log = create_logger("voice.freshness")

# Broad classes are only for generic handover receipts ("handed over, being
# done"). Spoken task status must retain the exact state, see work_claim.
STATE_CLASSES = {
    "active": {"accepted", "queued", "pending", "running", "busy", "resuming", "processing", "decision_recorded"},
    "waiting": {"waiting_input"},
    "stopped": {"pausing", "paused", "canceling", "canceled", "cancelled", "stopped"},
    "done": {"completed", "succeeded", "done", "idle"},
    "failed": {"failed", "error", "blocked", "resume_blocked", "effect_unknown", "output_unavailable"},
}
STATE_CLASS = {state: name for name, states in STATE_CLASSES.items() for state in states}


def work_status(row: dict) -> str:
    """A task row's state, results, decisions and requirements: what a prompt row shows, minus message previews."""
    pending, video = row.get("pending_request") or {}, row.get("video") or {}
    return json.dumps([row.get("task_version"), row.get("state"), row.get("waiting_for_user"),
                       row.get("last_decision"), row.get("result_id"), row.get("intent_revision"),
                       row.get("control_revision"), bool(row.get("execution_error")), pending.get("request_id"),
                       pending.get("version"), video.get("status"), (row.get("human_input") or {}).get("id")],
                      sort_keys=True, default=str)


def work_claim(row: dict) -> str:
    """The actual state, requirements and decision a task reply can have described.

    "Queued" is no longer true once running, nor "paused" once cancelled.
    Message previews alone still do not invalidate speech.
    """
    pending, video = row.get("pending_request") or {}, row.get("video") or {}
    state = row.get("state")
    return json.dumps([state, row.get("intent_revision"), pending.get("request_id"),
                       pending.get("version"), video.get("status"), (row.get("human_input") or {}).get("id")],
                      sort_keys=True, default=str)


def work_change(before: str, row: dict) -> dict:
    """What a task's row says now that a reply bound to it at claim ``before`` could not have said.

    Measured 2026-10-27: told only that something changed, the reply after a
    requirement update asked for the superseded plan's approval again.
    """
    _, revision, request, version, _, human_input_id = json.loads(before)
    pending = row.get("pending_request") or {}
    change = {"task": row.get("title") or "", "state": row.get("state")}
    if row.get("state_summary"):
        change["state_summary"] = row["state_summary"]
    if row.get("intent_revision") != revision:
        change["requirements_updated"] = True
    if (latest := row.get("human_input")) and latest.get("id") != human_input_id:
        change["user_input_updated"] = True
        change["latest_user_input"] = {key: latest[key] for key in ("text", "truncated") if key in latest}
    if request and pending.get("request_id") != request:
        change["earlier_decision"] = "no longer pending"
    if pending.get("request_id") and (pending.get("request_id"), pending.get("version")) != (request, version):
        change["waiting_on"] = [q.get("header") or q.get("question") or "" for q in pending.get("questions") or []][:2]
    return change


class FreshnessMixin:
    def _init_freshness(self, reader):
        self.context_reader = reader
        self._context_sequence = 0
        self._work = {}
        self._changed_work = {}  # tasks an outdated reply spoke about → their claims then, until the user speaks
        self._native_work_pending = {}
        self._native_work_playing = {}
        self._native_work_decisions = {}
        self._native_work_read = False
        self._native_work_stale = False
        self._native_context_changed = False
        self._freshness_failed = False
        self._freshness_notice_requested = False
        self._stale_responses = []

    async def _read_answer_context(self):
        # Memory turns use their own bounded, acknowledged context. Only an
        # actual work read needs the additional task snapshot barrier (even
        # when that read returned an empty list). Legacy prompts include it all.
        if self.prepare_memory is not None and not self._native_work_read:
            return True
        return await self._read_current_context()

    async def _read_current_context(self):
        if self.context_reader is None:
            return True
        try:
            async with asyncio.timeout(2):
                snapshot = await self.context_reader()
            self._freshness_failed = False
            await self.refresh_shared_context(snapshot)
            return True
        except Exception as exc:
            self._freshness_failed = True
            log.warning("voice freshness unavailable call=%s error=%s", self.call_id, type(exc).__name__)
            return False

    def _work_changed(self, claims):
        """A task a reply was bound to no longer says what the reply can have said (``work_claim``).

        A bounded overview cannot prove that an absent task was removed.
        """
        return any(key in self._work and work_claim(self._work[key]) != claim for key, claim in claims.items())

    async def _stop_obsolete_work(self, by: str) -> bool:
        """The playing reply speaks about a task whose state has since changed: stop it and answer again."""
        if (self._native_response is None or self.interrupted or not self._work_changed(self._native_work_playing)
                or self._hands_over(self._native_response)  # its own handover or answer changed the task
                or not (self.active_response == self._native_response or self.playback_left() > 0)):
            return False
        log.info("voice obsolete task speech stopped call=%s by=%s", self.call_id, by)
        self._note_changed_work()
        self._native_work_playing = {}
        self._answer_again()
        await self._interrupt_playback(source="work_changed")
        return True

    def _note_changed_work(self) -> None:
        """Keep what the stopped reply's tasks said then, for telling the next answer what changed since."""
        for key, claim in self._native_work_playing.items():
            self._changed_work.setdefault(key, claim)

    def _work_changes(self) -> list[dict]:
        """The tasks an outdated reply spoke about that say something else now (``work_change``)."""
        return [work_change(claim, self._work[key]) for key, claim in self._changed_work.items()
                if key in self._work and work_claim(self._work[key]) != claim]

    def _remember_stale_response(self, response_id):
        if response_id:
            self._stale_responses = [*self._stale_responses[-31:], response_id]

    async def refresh_shared_context(self, snapshot):
        if isinstance(snapshot, str):  # callers without structured work context
            self.shared_context = snapshot
            await self._sync_instructions(force=True)
            return
        if snapshot.sequence and snapshot.sequence <= self._context_sequence:
            return
        self._context_sequence = snapshot.sequence
        work = {row["session_id"]: row for row in snapshot.work}
        changed = {key: row["version"] for key, row in work.items()} != {
            key: row["version"] for key, row in self._work.items()}
        self._work, self.shared_context = work, snapshot.text
        if changed:
            self._native_context_changed = True
        self._refresh_turn_work()  # before any reply below, so it speaks from the new state
        if self.focus is not None:  # what each task is called, embedded before a reply needs it
            from voice.reply_focus import describe_task
            self.focus.soon({key: ("", describe_task(row)) for key, row in work.items()})
        await self._stop_obsolete_work("refresh")
        for ref in list(self.deliveries):
            if self._stale_work(ref):
                await self._supersede(ref, source="work_changed")
        await self._sync_instructions(force=True)

    def _stale_work(self, ref):
        tasks = {row.get("task_id"): row for row in self._work.values() if row.get("task_id")}
        if ref.task_id and (current := tasks.get(ref.task_id)):
            if ref.task_revision is not None and current.get("intent_revision") != ref.task_revision:
                return True
            if ref.control_revision is not None and current.get("control_revision") not in (None, ref.control_revision):
                return True
            if ref.result_id and current.get("result_id") != ref.result_id:
                return True
        for task in ref.running or []:
            if current := tasks.get(task.get("task_id")):
                # Still at it in the same requirements: queued becoming running
                # leaves "handed over, being done" true.
                if task.get("intent_revision") is not None and current.get("intent_revision") != task["intent_revision"]:
                    return True
                if STATE_CLASS.get(current["state"]) != "active":
                    return True
        return False

    async def _supersede(self, ref, *, source):
        if ref not in self.deliveries:
            return
        parent = next((item for item in self.deliveries if ref in (item.riders or [])), ref)
        kind = f"delivery:{parent.id}"
        playing = self.response_kind == kind or self.requested == kind or parent.delivery == "awaiting_playback"
        # Invalidate the whole prepared note, but keep its other results queued.
        self._withdrawn_deliveries.add(kind)
        if parent.note:
            self._withdrawn_notes.add(parent.note)
        for item in (parent, *(parent.riders or [])):
            if item is not ref:
                item.delivery = "queued"
                item.interrupted = item.interrupted or playing
        parent.riders = None
        if playing:
            await self._interrupt_playback(source=source)
        if parent.note_item:
            await self._delete(parent.note_item)
        parent.note_item, parent.note_pending, parent.note_at = None, False, None
        ref.delivery, ref.finished = "superseded", self.clock()
        self.deliveries.remove(ref)
        await self._finish_listeners(ref, False)
        if ref.recorded:
            await asyncio.shield(self.link.done(ref, "cancelled", delivered=False))
        log.info("voice speech superseded call=%s turn=%s source=%s", self.call_id, ref.id, source)
