"""What a session is still doing beyond its transcript rows.

The transcript alone under-reports two things the public contract cares
about: a paid video generation that outlives the model's run (the tool ends
the run and the job finishes minutes later), and a turn that was aborted
before the model wrote anything. Both live in other tables.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select

from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.video_job import VideoJob

#: Every non-terminal state of a paid generation or render, transfer included.
VIDEO_JOB_IN_FLIGHT = frozenset({
    "submitting", "dispatching", "transcribing", "queued", "in_progress",
    "generating", "finalizing", "transfer_failed",
})


@dataclass(frozen=True)
class SessionActivity:
    #: A video job of this session has not reached a terminal state.
    pending_jobs: bool = False
    #: A platform continuation (``vjob:``) is accepted but not yet claimed.
    pending_resume: bool = False
    #: User messages whose turn ended without a reply: the person aborted
    #: it, or the run was stopped before it wrote a step. Only meaningful
    #: once the session is no longer active (see ``_finish_of``).
    aborted_user_message_ids: frozenset[str] = field(default_factory=frozenset)

    @property
    def busy(self) -> bool:
        return self.pending_jobs or self.pending_resume


async def session_activity(session_id: str) -> SessionActivity:
    async with get_db_session() as db:
        pending_jobs = await db.scalar(
            select(VideoJob.id).where(
                VideoJob.session_id == session_id,
                VideoJob.status.in_(tuple(VIDEO_JOB_IN_FLIGHT)),
            ).limit(1)
        )
        pending_resume = await db.scalar(
            select(AgentInboxItem.id).where(
                AgentInboxItem.session_id == session_id,
                AgentInboxItem.state == "accepted",
                AgentInboxItem.client_id.like("vjob:%"),
            ).limit(1)
        )
        aborted = (await db.scalars(
            select(AgentInboxItem.message_id).where(
                AgentInboxItem.session_id == session_id,
                AgentInboxItem.message_id.is_not(None),
                AgentInboxItem.result_message_id.is_(None),
                (AgentInboxItem.outcome.in_(("aborted", "canceled"))
                 | ((AgentInboxItem.state == "claimed") & AgentInboxItem.outcome.is_(None))),
            )
        )).all()
    return SessionActivity(
        pending_jobs=pending_jobs is not None,
        pending_resume=pending_resume is not None,
        aborted_user_message_ids=frozenset(aborted),
    )
