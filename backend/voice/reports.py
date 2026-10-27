"""Task results the assistant reports while a call is on, told in the call too.

A task handed over in a call ("让它去打开抖音创作者中心") ends its voice turn as
soon as the assistant has passed it on; the task's own result comes back later
as a report turn in the main session (``origin=task_result``, assistant/results.py).
Without this the caller never heard it: the report was only written into the
conversation. While a call lasts, every report the assistant finishes becomes a
background note the front desk tells in its own words at the next quiet moment
(voice/turns.py); several arriving together are told in one go.
"""
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from core.log import create_logger

log = create_logger("voice.reports")

POLL_SECONDS = 2.0
RECENT = 30  # report items looked at per poll: far more than a call ever sees at once


@dataclass(frozen=True)
class Report:
    inbox_id: str
    title: str   # the task's title, as the conversation shows it
    text: str    # the assistant's own report, as written in the conversation
    task_id: str = ""  # the task reporting: a request of the call passed to it is answered by this
    running: list[dict] | None = None
    decision_versions: dict[str, str] | None = None
    result_id: str = ""
    task_revision: int | None = None
    control_revision: int | None = None  # the task's pause/resume/cancel count when the report was read


class ReportWatcher:
    """New finished task reports of one main session since the call began."""

    POLL_SECONDS = POLL_SECONDS

    def __init__(self, *, user_id: str, main_session_id: str, call_id: str = "", workspace_id: str = ""):
        self.user_id, self.main_session_id = user_id, main_session_id
        self.call_id = call_id
        self.workspace_id = workspace_id
        self._seen: set[str] | None = None

    async def start(self) -> None:
        """Reports already finished before the call are the greeting's business, not this one's."""
        self._seen = {row.id for row in await self._recent() if row.state == "settled"}

    async def poll(self) -> list[Report]:
        if self._seen is None:
            await self.start()
            return []
        fresh = [row for row in await self._recent()
                 if row.state == "settled" and row.id not in self._seen]
        reports = []
        for row in sorted(fresh, key=lambda row: row.id):
            # Typed answers belong to the page and shared context. Replaying
            # them as unsolicited speech races the native answer to a user's
            # follow-up question. This call's own late voice results still need
            # the durable observer, as do task results and card continuations.
            if row.origin == "human" and (not self.call_id
                    or (row.origin_ref or {}).get("voice_call_id") != self.call_id):
                continue
            # The bridge owns delivery deduplication across both observers.
            # Keep this path even for this call: its waiter may have timed out.
            if row.outcome != "succeeded" or not row.result_message_id:
                continue
            evidence = await self._result(row)
            if evidence is None:
                continue
            from voice.assistant_link import reply_text, running_tasks
            text = await reply_text(self.main_session_id, row.result_message_id, self.user_id)
            if text.strip():
                versions, stale = {}, False
                if self.workspace_id:
                    from voice.decisions import for_turn
                    versions, stale = await for_turn(self.main_session_id, self.user_id, self.workspace_id, row.id)
                if stale:
                    continue  # a manual click already superseded this typed reply's card explanation
                running = await running_tasks(self.main_session_id, self.user_id, row.id)
                reports.append(Report(row.id, await self._title(row), text, (row.origin_ref or {}).get("task_id") or "",
                                      running or None, versions or None, **evidence))
                if settled := getattr(row, "settled_at", None):
                    delay = datetime.now(timezone.utc) - settled.replace(tzinfo=settled.tzinfo or timezone.utc)
                    log.info("voice report observed call=%s inbox=%s settlement_to_observed_ms=%.1f",
                             self.call_id, row.id, max(delay.total_seconds() * 1000, 0))
        # Do not lose a batch if one result/title read fails: the next wake or
        # fallback scan retries it. Successful polls still deliver only once.
        self._seen.update(row.id for row in fresh)
        if reports:
            log.info("voice reports session=%s count=%s", self.main_session_id, len(reports))
        return reports

    async def _result(self, row):
        """Retry inboxes share a result ID; late old revisions never become new facts."""
        result_id = (row.origin_ref or {}).get("result_id")
        if not result_id:
            return {}
        from assistant.control import CONTROL_ACTIONS
        from db.base import get_db_session
        from db.models.assistant import AssistantCommand, AssistantTask, TaskResult
        async with get_db_session() as db:
            pair = (await db.execute(select(TaskResult, AssistantTask).join(
                AssistantTask, AssistantTask.id == TaskResult.task_id).where(
                TaskResult.id == result_id, AssistantTask.id == (row.origin_ref or {}).get("task_id"),
                AssistantTask.user_id == self.user_id, AssistantTask.assistant_session_id == self.main_session_id,
                AssistantTask.workspace_id == self.workspace_id, AssistantTask.archived_at.is_(None)))).first()
            if not pair:
                return None
            result, task = pair
            if task.latest_result_id != result.id or task.intent_revision != result.observed_intent_revision:
                return None
            # Paused, resumed or cancelled since: the result no longer says
            # where the task is (a failure told after the user retried it).
            if await db.scalar(select(AssistantCommand.id).where(
                    AssistantCommand.target_type == "task", AssistantCommand.target_id == task.id,
                    AssistantCommand.action.in_([f"task_{action}" for action in CONTROL_ACTIONS]),
                    AssistantCommand.created_at > result.created_at).limit(1)):
                return None
            return {"result_id": result.id, "task_revision": result.observed_intent_revision,
                    "control_revision": task.control_revision}

    async def _recent(self):
        from db.base import get_db_session
        from db.models.agent_inbox import AgentInboxItem
        async with get_db_session() as db:
            return list((await db.scalars(select(AgentInboxItem).where(
                AgentInboxItem.session_id == self.main_session_id, AgentInboxItem.user_id == self.user_id,
                AgentInboxItem.origin.in_(("task_result", "human", "system_recovery")))
                .order_by(AgentInboxItem.created_at.desc(), AgentInboxItem.id.desc()).limit(RECENT))).all())

    async def _title(self, row) -> str:
        from db.base import get_db_session
        from db.models.assistant import AssistantTask
        from db.models.question import QuestionCheckpoint
        ref = row.origin_ref or {}
        task_id, question_id = ref.get("task_id"), ref.get("question_id")
        if not task_id and not question_id:
            return ""
        async with get_db_session() as db:
            if task_id:
                task = await db.get(AssistantTask, task_id)
                return task.title if task is not None and task.user_id == self.user_id else ""
            checkpoint = await db.get(QuestionCheckpoint, question_id)
        # A card answered before any task exists (placement, task setup): name
        # what the card was about. Untitled, "the task was cancelled" was told
        # as the call's current topic, a different task (2026-10-26).
        if checkpoint is None or checkpoint.user_id != self.user_id or checkpoint.session_id != self.main_session_id:
            return ""
        continuation = checkpoint.continuation or {}
        for kind in ("assistant_placement", "assistant_task_setup"):
            title = ((continuation.get(kind) or {}).get("input") or {}).get("title")
            if title:
                return str(title)
        questions = checkpoint.questions or []
        return str((questions[0] or {}).get("header") or "") if questions else ""
