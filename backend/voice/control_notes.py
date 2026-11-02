"""Task control changes told in the call: stopped, waiting for a check, resumed, paused (VOICE_SEAM_PLAN §3.2).

Until 2026-10-31 a task cancelled from its page while a call was on changed nothing the caller could hear;
the front desk went on as if it ran. Now each change of a task's control revision since the call began
becomes one short background note with the next step in it, told at the next quiet moment like a report
(voice/turns.py ``control_note``). The note is written here as speech, never as field names.
"""
from dataclasses import dataclass

from sqlalchemy import select

from core.log import create_logger

log = create_logger("voice.control_notes")


@dataclass(frozen=True)
class ControlNote:
    task_id: str
    title: str
    kind: str          # canceled / effect_unknown / paused / resumed / blocked
    text: str          # what the front desk says, in the call's language
    control_revision: int


def kind_of(desired: str, observed: str, *, previous_desired: str | None, previous_observed: str | None) -> str | None:
    """Which change to tell; None when nothing the user needs to hear changed."""
    if observed == "effect_unknown":
        return "effect_unknown"
    if desired == "canceled":
        return "canceled" if previous_desired != "canceled" else None
    if observed == "resume_blocked":
        return "blocked"
    if desired == "paused":
        return "paused" if previous_desired != "paused" or previous_observed == "effect_unknown" else None
    if desired == "running" and previous_desired in {"paused", "canceled"}:
        return "resumed"
    if desired == "running" and previous_observed == "effect_unknown":
        return "resumed"
    return None


def note_text(kind: str, title: str, lang: str, *, by_user_on_page: bool = False) -> str:
    name = f"「{title}」" if title else ("那个任务" if lang != "en" else "that task")
    if lang == "en":
        return {
            "canceled": f"{name} was just stopped." + (" You pressed stop on its page." if by_user_on_page else "")
                        + " Say the word if you want it to continue.",
            "effect_unknown": f"When {name} stopped, one step's result could not be confirmed. If you have checked and it "
                              "is fine, tell me “checked, continue” and I will let it carry on.",
            "resumed": f"{name} is going again.",
            "paused": f"{name} is on hold for now; it can continue whenever you say.",
            "blocked": f"{name} could not resume yet; I will tell you when it can.",
        }[kind]
    return {
        "canceled": f"{name}刚刚被停下了。" + ("是你在任务页按了停止。" if by_user_on_page else "") + "要继续的话跟我说一声。",
        "effect_unknown": f"{name}停下时有一步的结果没来得及确认。你确认过没问题的话，跟我说“核对过了，继续”，我就能让它接着做。",
        "resumed": f"{name}接着做了。",
        "paused": f"{name}先停在这儿了，随时可以让它继续。",
        "blocked": f"{name}暂时还续不上，能继续的时候我告诉你。",
    }[kind]


class ControlWatcher:
    """Control revisions of one main session's tasks that moved since the call began."""

    def __init__(self, *, user_id: str, main_session_id: str, workspace_id: str, call_id: str = "", lang: str = "zh"):
        self.user_id, self.main_session_id, self.workspace_id = user_id, main_session_id, workspace_id
        self.call_id, self.lang = call_id, lang
        self._seen: dict[str, tuple[int, str, str]] | None = None  # task → (revision, desired, observed)

    async def start(self) -> None:
        self._seen = {task.id: (task.control_revision, task.desired_state, task.observed_state)
                      for task in await self._tasks()}

    async def poll(self) -> list[ControlNote]:
        if self._seen is None:
            await self.start()
            return []
        notes = []
        for task in await self._tasks():
            known = self._seen.get(task.id)
            self._seen[task.id] = (task.control_revision, task.desired_state, task.observed_state)
            if known is None or known[0] >= task.control_revision:
                continue  # a task created during the call is told by its own receipt; nothing moved otherwise
            kind = kind_of(task.desired_state, task.observed_state, previous_desired=known[1], previous_observed=known[2])
            if kind is None:
                continue
            by_user = kind == "canceled" and await self._stopped_on_page(task)
            notes.append(ControlNote(task.id, task.title, kind,
                                     note_text(kind, task.title, self.lang, by_user_on_page=by_user),
                                     task.control_revision))
        if notes:
            log.info("voice control notes call=%s count=%s kinds=%s", self.call_id, len(notes),
                     [note.kind for note in notes])
        return notes

    async def _tasks(self):
        from db.base import get_db_session
        from db.models.assistant import AssistantTask
        async with get_db_session() as db:
            return list((await db.scalars(select(AssistantTask).where(
                AssistantTask.assistant_session_id == self.main_session_id, AssistantTask.user_id == self.user_id,
                AssistantTask.workspace_id == self.workspace_id, AssistantTask.archived_at.is_(None))
                .order_by(AssistantTask.updated_at.desc()).limit(50))).all())

    async def _stopped_on_page(self, task) -> bool:
        """The latest cancel came from a person (the page's stop or the card), not from the assistant's tool."""
        from db.base import get_db_session
        from db.models.assistant import AssistantCommand
        async with get_db_session() as db:
            command = await db.scalar(select(AssistantCommand).where(
                AssistantCommand.target_type == "task", AssistantCommand.target_id == task.id,
                AssistantCommand.action == "task_cancel").order_by(AssistantCommand.created_at.desc()).limit(1))
        return command is not None and not (command.source_ref or {}).get("source_refs")
