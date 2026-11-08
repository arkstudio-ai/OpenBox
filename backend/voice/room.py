"""The call inside one task's room (VOICE_SEAM_PLAN §9.1, §9.4 step 3).

Entering is explicit ("进艾薇兰那间"): from then on what the user says is handed to that task's own
conversation, not to the global assistant, and that conversation's replies are told to the user as they
land. Leaving is explicit too ("回去", hanging up). Another task's result while inside is only announced
("「X」那间出结果了，要过去吗"), never played in full. Nothing here persists: a room lasts one call.
"""
from dataclasses import dataclass

from sqlalchemy import select

from core.log import create_logger
from voice.phrases import english

log = create_logger("voice.room")


@dataclass(frozen=True)
class RoomReply:
    inbox_id: str
    title: str
    text: str
    result_id: str = ""


def room_section(room: dict, lang: str = "zh") -> str:
    title = room.get("title") or ""
    if english(lang):
        return (f"You are watching the work of “{title}”: what the user says is input for that task and assistant_ask "
                "hands it straight there; the helper's replies are told to the user as they come. A reply takes minutes: "
                "acknowledge, say it will be told when done, do not re-ask. When the user wants out (“回去”, "
                "“back”), call room_leave. Another task's result is only announced, not told.")
    return (f"正在盯「{title}」的现场：用户说的话都是给这件活的，assistant_ask 会直接交给它，帮手的回复一到就告诉用户。"
            "帮手一轮是分钟级：接收后说一句好了会叫他，不追问。用户说“回去”“不盯了”就调用 room_leave。"
            "别的活出了结果只提一句要不要看，不在这里展开。对用户只说“帮手”“现场”“任务单”，不说房间、任务间、执行会话。")


async def _find_task(scope, arguments: dict):
    from assistant.commands import similar_task_title
    from db.base import get_db_session
    from db.models.assistant import AssistantTask
    task_id = str(arguments.get("task_id") or "").strip()
    title = str(arguments.get("title") or "").strip()
    async with get_db_session() as db:
        rows = (await db.scalars(select(AssistantTask).where(
            AssistantTask.assistant_session_id == scope.main_session_id, AssistantTask.user_id == scope.user_id,
            AssistantTask.workspace_id == scope.workspace_id, AssistantTask.archived_at.is_(None))
            .order_by(AssistantTask.updated_at.desc()).limit(50))).all()
    if task_id:
        return next((row for row in rows if row.id == task_id), None), rows
    if title:
        return next((row for row in rows if title in row.title or similar_task_title(row.title, title)), None), rows
    return None, rows


async def enter(scope, arguments: dict) -> dict:
    """``room_enter``: bind this call to one task's room."""
    call = getattr(scope, "call", None)
    if call is None:
        return {"status": "unavailable"}
    task, rows = await _find_task(scope, arguments)
    if task is None:
        return {"status": "not_found", "known_titles": [row.title for row in rows[:8]],
                "note": "没找到这件活；可以用 known_titles 里的标题再试，或问用户是哪一件。"}
    room = {"task_id": task.id, "title": task.title, "session_id": task.execution_session_id}
    await call.enter_room(room)
    say = (f"好，盯着「{task.title}」的现场，你说的话我直接给这件活；说“回去”就回来。" if not english(scope.lang) else
           f"Okay, watching the work of “{task.title}”: what you say goes straight to it. Say “back” to return.")
    return {"status": "ok", **room, "say": say}


async def leave(scope, arguments: dict) -> dict:
    """``room_leave``: back to the global layer."""
    call = getattr(scope, "call", None)
    if call is None:
        return {"status": "unavailable"}
    was = call.room
    await call.leave_room()
    say = ("好，回来了。" if not english(scope.lang) else "Okay, back with me.")
    return {"status": "ok", "left": (was or {}).get("title"), "say": say}


class RoomWatcher:
    """Replies landing in the bound room's conversation, told to the user directly.

    ``room`` returns the call's current binding or None. The first look at a room only learns what is
    already there; from then on each settled run with a reply is one ``RoomReply``."""

    def __init__(self, *, user_id: str, workspace_id: str, call_id: str, room, lang: str = "zh"):
        self.user_id, self.workspace_id, self.call_id, self.room, self.lang = user_id, workspace_id, call_id, room, lang
        self._seen: dict[str, set[str]] = {}

    async def poll(self) -> list[RoomReply]:
        room = self.room()
        if not room or not room.get("session_id"):
            return []
        session_id = room["session_id"]
        rows = await self._recent(session_id)
        primed = session_id in self._seen
        seen = self._seen.setdefault(session_id, set())
        replies = []
        for row in sorted(rows, key=lambda row: row.id):
            if row.id in seen:
                continue
            seen.add(row.id)
            if not primed or row.state != "settled" or row.outcome != "succeeded" or not row.result_message_id:
                continue
            from voice.assistant_link import reply_text
            text = await reply_text(session_id, row.result_message_id, self.user_id)
            if text.strip():
                replies.append(RoomReply(row.id, room.get("title") or "", text, await self._result_id(row)))
        if not primed:
            # Items still being worked on when the room was entered are told once they settle.
            seen.difference_update(row.id for row in rows if row.state != "settled")
        if replies:
            log.info("voice room replies call=%s room=%s count=%s", self.call_id, session_id, len(replies))
        return replies

    async def _recent(self, session_id: str):
        from db.base import get_db_session
        from db.models.agent_inbox import AgentInboxItem
        async with get_db_session() as db:
            return list((await db.scalars(select(AgentInboxItem).where(
                AgentInboxItem.session_id == session_id, AgentInboxItem.user_id == self.user_id)
                .order_by(AgentInboxItem.created_at.desc(), AgentInboxItem.id.desc()).limit(20))).all())

    async def _result_id(self, row) -> str:
        """The task result behind this reply, so the assistant's later report of it is not told twice."""
        from db.base import get_db_session
        from db.models.assistant import TaskResult
        async with get_db_session() as db:
            return await db.scalar(select(TaskResult.id).where(
                TaskResult.result_message_id == row.result_message_id).limit(1)) or ""
