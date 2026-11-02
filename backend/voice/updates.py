"""Coalesced wake hints for a call; SQL reads remain authoritative.

Redis carries the bus hints between workers. A bounded fallback scan covers
lost hints and older writers; neither slow reads nor provider sends hold up
the call's heartbeat/credit timer.
"""
import asyncio

from bus import bus
from bus.events import (AGENT_INBOX_UPDATED, ASSET_UPDATED, ASSISTANT_TASK_CONTROL, MESSAGE_CREATED,
                        SESSION_STATUS, SESSION_UPDATED, SESSION_TITLE, QUESTION_ASKED, QUESTION_REPLIED,
                        QUESTION_REJECTED)
from core.log import create_logger

log = create_logger("voice.updates")
POLL_SECONDS = 2.0
READ_TIMEOUT_SECONDS = 2.0


class CallUpdates:
    def __init__(self, scope, rooms=None):
        self.scope = scope
        # The execution sessions of this user's open tasks (voice/shared_context.py knows them after each
        # read): input typed on one of those pages is shared context; other sessions' inbox traffic is not.
        self._rooms = rooms or (lambda: ())
        self.reports, self.context, self.cards = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.controls = asyncio.Event()  # a task was paused/resumed/cancelled/released (voice/control_notes.py)
        self.reports.set()
        self.context.set()
        self.cards.set()
        self.controls.set()
        self._unsubscribers = [bus.subscribe(kind, self._changed) for kind in (
            AGENT_INBOX_UPDATED, ASSET_UPDATED, ASSISTANT_TASK_CONTROL, MESSAGE_CREATED, SESSION_STATUS,
            SESSION_UPDATED, SESSION_TITLE, QUESTION_ASKED, QUESTION_REPLIED, QUESTION_REJECTED)]

    def _changed(self, event):
        data, kind = event.get("data") or {}, event.get("type")
        if data.get("userId") != self.scope.user_id:
            return
        if data.get("workspaceId") not in (None, self.scope.workspace_id):
            return
        # Question events use session_id; session/message events use sessionId.
        # These are wake hints only: the readers enforce owner + workspace in SQL.
        session_id = data.get("sessionId") or data.get("session_id")
        if kind in (QUESTION_ASKED, QUESTION_REPLIED, QUESTION_REJECTED, SESSION_STATUS, SESSION_UPDATED):
            self.cards.set()
        if kind in (QUESTION_ASKED, QUESTION_REPLIED, QUESTION_REJECTED,
                    SESSION_STATUS, SESSION_UPDATED, SESSION_TITLE, MESSAGE_CREATED):
            self.context.set()
        if kind == AGENT_INBOX_UPDATED and session_id in self._rooms():
            # Input on a task's own page is shared context too (VOICE_SEAM_PLAN §3.1).
            self.context.set()
        if kind == ASSISTANT_TASK_CONTROL:
            if session_id == self.scope.main_session_id:
                self.controls.set()
                self.context.set()
        elif kind == ASSET_UPDATED:
            if data.get("workspaceId") == self.scope.workspace_id:
                self.context.set()
        elif session_id == self.scope.main_session_id:
            self.context.set()
            if kind in (AGENT_INBOX_UPDATED, SESSION_STATUS):
                self.reports.set()

    async def watch(self, signal, read, deliver, *, name):
        failures = 0
        while True:
            try:
                await asyncio.wait_for(signal.wait(), POLL_SECONDS)
            except TimeoutError:
                pass
            # Clear BEFORE the read: a commit during that await must cause
            # another read immediately, not disappear until the next poll.
            signal.clear()
            reading = asyncio.create_task(asyncio.wait_for(read(), READ_TIMEOUT_SECONDS))
            try:
                value = await asyncio.shield(reading)
                await deliver(value)
                failures = 0
            except asyncio.CancelledError:
                # Let the bounded DB read release its connection before the
                # hang-up settlement starts (especially important on SQLite).
                await asyncio.gather(reading, return_exceptions=True)
                raise
            except Exception as exc:
                failures += 1
                if failures == 1 or failures % 30 == 0:
                    log.warning("voice update failed call=%s source=%s error=%s",
                                self.scope.call_id, name, type(exc).__name__)

    def close(self):
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
