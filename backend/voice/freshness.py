"""Bind speech to committed work versions; freshness is independent of model wording."""
import asyncio

from core.log import create_logger

log = create_logger("voice.freshness")


class FreshnessMixin:
    def _init_freshness(self, reader):
        self.context_reader = reader
        self._context_sequence = 0
        self._work = {}
        self._native_work_pending = {}
        self._native_work_playing = {}
        self._native_work_decisions = {}
        self._native_work_stale = False
        self._native_context_changed = False
        self._freshness_failed = False
        self._freshness_notice_requested = False
        self._stale_responses = []

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

    def _work_changed(self, versions):
        # A bounded overview cannot prove that an absent task was removed.
        return any(key in self._work and self._work[key]["version"] != version
                   for key, version in versions.items())

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
        if self._work_changed(self._native_work_playing) and (
                self.active_response == self._native_response or self.playback_left() > 0):
            self._native_work_playing = {}
            self.followup_due = True
            await self._interrupt_playback(source="work_changed")
        for ref in list(self.deliveries):
            if self._stale_work(ref):
                await self._supersede(ref, source="work_changed")
        await self._sync_instructions(force=True)

    def _stale_work(self, ref):
        tasks = {row.get("task_id"): row for row in self._work.values() if row.get("task_id")}
        if ref.task_id and (current := tasks.get(ref.task_id)):
            if ref.task_revision is not None and current.get("intent_revision") != ref.task_revision:
                return True
            if ref.result_id and current.get("result_id") != ref.result_id:
                return True
        for task in ref.running or []:
            if current := tasks.get(task.get("task_id")):
                if task.get("task_version") and current.get("task_version") != task["task_version"]:
                    return True
                if current["state"] not in {"queued", "running", "resuming"}:
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
