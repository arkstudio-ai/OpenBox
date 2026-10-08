"""Keeping a long call's context small: summaries, deleted items and a fresh provider session.

Measured on QA (2026-10-07): a 520 s call had sent 73k input tokens, every
reply re-reading every word said and every result read out. Every 10 user
turns, or when a reply's input passes 60k tokens, the small model folds the
call so far into a short memo the session prompt carries; then the early
items (and notes delivered three replies ago) are deleted, oldest first.
After 25 minutes or 120k tokens the bridge opens a fresh provider session
with the memo; the client never notices (``max_call_seconds`` stays the
user-facing limit).
"""
import asyncio
from contextlib import suppress

from core.log import create_logger
from voice.transcript import CallTranscript

log = create_logger("voice.upkeep")

SUMMARY_EVERY_TURNS = 10
SUMMARY_INPUT_TOKENS, TOKEN_SUMMARY_MIN_TURNS = 60_000, 3
ROTATE_SECONDS, ROTATE_INPUT_TOKENS, ROTATE_RETRY_SECONDS = 25 * 60, 120_000, 20.0
KEEP_ITEMS = 8           # the latest exchanges stay as they are
NOTE_KEEP_REPLIES = 3    # a delivered note stays for the replies right after it
SUMMARY_WAIT_SECONDS = 15.0
LAST_LINES, LAST_CHARS = 6, 600


class ContextKeeper:
    def __init__(self, transcript: CallTranscript, *, clock, summarizer=None, protected=lambda: set()):
        self.transcript, self.clock, self.protected = transcript, clock, protected
        self.summarizer = summarizer
        self.summary = ""            # the call so far, for the prompt
        self.carried = ""            # the latest lines word for word, after a rotation, until the next memo
        self.covered = 0             # transcript lines the summary covers
        self.turns_since = 0         # user lines since the last summary started
        self.input_tokens = 0        # the latest reply's input: the whole context
        self.replies = 0
        self.session_started = clock()
        self.task: asyncio.Task | None = None
        self.retry_rotation_at: float | None = None
        self._deletions: list[str] = []
        self._notes: list[tuple[int, str]] = []  # (replies when delivered, note item id)

    def user_turn(self) -> None:
        self.turns_since += 1

    def response_done(self, usage: dict | None) -> None:
        self.replies += 1
        tokens = (usage or {}).get("input_tokens")
        if type(tokens) is int:
            self.input_tokens = tokens
        if self.task is None and (self.turns_since >= SUMMARY_EVERY_TURNS or (
                self.input_tokens > SUMMARY_INPUT_TOKENS and self.turns_since >= TOKEN_SUMMARY_MIN_TURNS)):
            self.start_summary()

    def start_summary(self) -> asyncio.Task:
        """Fold every line so far into the memo; the items older than the latest few go once it exists."""
        end, cut = len(self.transcript.lines), self.transcript.early_items(KEEP_ITEMS, self.protected())
        self.turns_since = 0
        self.task = asyncio.create_task(self._summarize(end, cut))
        return self.task

    async def _summarize(self, end: int, cut: list[str]) -> None:
        try:
            text = await self._summarizer()(self.transcript.render(self.covered, end), self.summary)
        except Exception as exc:  # the call goes on with its full context
            log.info("voice upkeep summary failed error=%s", type(exc).__name__)
            text = ""
        finally:
            self.task = None
        if text:  # without a memo nothing may be deleted
            self.summary, self.covered, self.carried = text, end, ""
            self._deletions += [item_id for item_id in cut if item_id not in self._deletions]
            log.info("voice upkeep summary lines=%s chars=%s deletions=%s", end, len(text), len(cut))

    def _summarizer(self):
        if self.summarizer is None:
            from voice.summary import summarize
            return summarize
        return self.summarizer

    def note_delivered(self, item_id: str | None) -> None:
        if item_id:
            self._notes.append((self.replies, item_id))

    def due_deletions(self) -> list[str]:
        """Item ids to delete now, oldest first; each one is handed out once."""
        ripe = [item_id for count, item_id in self._notes if self.replies - count >= NOTE_KEEP_REPLIES]
        self._notes = [(count, item_id) for count, item_id in self._notes if self.replies - count < NOTE_KEEP_REPLIES]
        order = {item.id: index for index, item in enumerate(self.transcript.items)}
        protected = self.protected()
        due = [item_id for item_id in dict.fromkeys(self._deletions + ripe)
               if item_id in order and item_id not in protected]
        self._deletions = [item_id for item_id in self._deletions if item_id in protected]
        return sorted(due, key=order.__getitem__)

    def rotation_due(self) -> bool:
        now = self.clock()
        if self.retry_rotation_at is not None and now < self.retry_rotation_at:
            return False
        return now - self.session_started >= ROTATE_SECONDS or self.input_tokens > ROTATE_INPUT_TOKENS

    async def summary_now(self) -> str:
        """The memo covering the whole call, made now if needed (bounded); the old one on failure."""
        if self.task is None and self.covered < len(self.transcript.lines):
            self.start_summary()
        if self.task is not None:
            with suppress(Exception):
                await asyncio.wait_for(asyncio.shield(self.task), SUMMARY_WAIT_SECONDS)
        return self.summary

    def carry_last_lines(self) -> None:
        """The latest lines word for word, so a fresh session continues mid-conversation."""
        text = self.transcript.render(max(len(self.transcript.lines) - LAST_LINES, 0))
        self.carried = text if len(text) <= LAST_CHARS else "…" + text[-LAST_CHARS:]

    def rotated(self) -> None:
        self.session_started, self.input_tokens, self.retry_rotation_at = self.clock(), 0, None
        self._deletions, self._notes = [], []

    def rotation_failed(self) -> None:
        self.retry_rotation_at = self.clock() + ROTATE_RETRY_SECONDS

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()
