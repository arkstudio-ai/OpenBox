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


class ReportWatcher:
    """New finished task reports of one main session since the call began."""

    POLL_SECONDS = POLL_SECONDS

    def __init__(self, *, user_id: str, main_session_id: str):
        self.user_id, self.main_session_id = user_id, main_session_id
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
            self._seen.add(row.id)
            if row.outcome != "succeeded" or not row.result_message_id:
                continue
            from voice.assistant_link import reply_text
            text = await reply_text(self.main_session_id, row.result_message_id, self.user_id)
            if text.strip():
                reports.append(Report(row.id, await self._title(row), text))
        if reports:
            log.info("voice reports session=%s count=%s", self.main_session_id, len(reports))
        return reports

    async def _recent(self):
        from db.base import get_db_session
        from db.models.agent_inbox import AgentInboxItem
        async with get_db_session() as db:
            return list((await db.scalars(select(AgentInboxItem).where(
                AgentInboxItem.session_id == self.main_session_id, AgentInboxItem.user_id == self.user_id,
                AgentInboxItem.origin == "task_result").order_by(AgentInboxItem.id.desc()).limit(RECENT))).all())

    async def _title(self, row) -> str:
        from db.base import get_db_session
        from db.models.assistant import AssistantTask
        task_id = (row.origin_ref or {}).get("task_id")
        if not task_id:
            return ""
        async with get_db_session() as db:
            task = await db.get(AssistantTask, task_id)
        return task.title if task is not None and task.user_id == self.user_id else ""
