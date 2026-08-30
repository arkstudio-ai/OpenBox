"""Ending a turn on purpose, and telling the truth about it afterwards.

Stopping a run used to be three lines copy-pasted into four call sites, and
none of them left any trace: the todo list kept its ``in_progress`` item, the
transcript said nothing, and the next turn's model had no way to know the
previous one had been cut short. The card went on animating a task that had
been dead for an hour.

This module is the single place a turn is ended deliberately. It does three
things, in an order that matters:

1. Signals the loop and parks the session at idle (what the call sites always
   did).
2. Writes an interruption marker into the transcript, so the *model* learns
   in-band what happened — the same shape codex uses, where an interrupted
   turn leaves a note in history rather than relying on out-of-band state.
3. Settles the stored todo list, because after a stop nothing is in progress.

Only step 1 is required for the stop itself. Steps 2 and 3 are about not
lying afterwards, and each is written so that failing leaves the stop intact.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Literal

from core.log import create_logger
from models.message import SessionStatus

log = create_logger("session.abort")

AbortReason = Literal["user_stop", "preempted", "error"]

#: Reserved client_message_id prefix for the marker, so the frontend can render
#: it as a divider instead of a chat bubble, and so a replay cannot forge one.
MARKER_PREFIX = "tabort:"

#: The loop checks its abort signal between steps; give it that long to notice
#: before we start settling state out from under it.
_ABORT_SETTLE_SECONDS = 0.3

_OPENING = {
    "user_stop": "[上一回合已被用户主动中断]",
    "preempted": "[上一回合已被用户的新消息打断]",
    "error": "[上一回合因内部错误终止]",
}


def _marker_id(session_id: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    return f"{MARKER_PREFIX}{session_id}:{stamp}"


def marker_text(reason: AbortReason, running_subject: str | None, step: int, total: int) -> str:
    """What the next turn's model reads about the interruption.

    Written to be shown to a person as-is: until the transcript renders it as
    a divider it appears as an ordinary message, and a debug-looking one would
    be worse than the problem being fixed.
    """
    lines = [_OPENING.get(reason, _OPENING["user_stop"])]
    lines.append("- 被打断的工具调用可能只执行了一半。")
    lines.append("- 已提交的异步供应商任务可能仍在运行；先查询状态，不要重复提交。")
    if running_subject:
        lines.append(
            f"- 任务清单停在中断时刻：第 {step}/{total} 步「{running_subject}」进行中被打断。"
        )
    elif total:
        lines.append(f"- 任务清单停在中断时刻，共 {total} 步。")
    lines.append(
        "是否延续该清单，由你根据用户下一条消息判断；与新请求无关时不要主动接手。"
    )
    return "\n".join(lines)


async def _already_marked(session_id: str, user_id: str) -> bool:
    """Has the tail of this conversation already been marked?

    Double-clicking stop, or a stop landing right after a preemption, must not
    stack markers. Anything the user or the model said since the last marker
    means the conversation moved on and a new one is warranted.
    """
    from session.session import get_messages

    messages = await get_messages(session_id, user_id=user_id)
    for message in reversed(messages):
        client_id = getattr(message, "client_message_id", None) or ""
        if client_id.startswith(MARKER_PREFIX):
            return True
        # Any real turn content after the last marker resets the question.
        return False
    return False


async def settle_running_todos(session_id: str, user_id: str) -> tuple[str | None, int, int]:
    """Drop the running flag; report what was running, for the marker.

    Returns ``(subject, ordinal, total)`` describing the interrupted item.
    The stored list keeps every item and its text — only "this is happening
    right now", which is no longer true, is cleared. The status vocabulary is
    deliberately unchanged: "interrupted" is a rendering decision the frontend
    derives, not a value every consumer of this list would have to learn.
    """
    from session.todo import get_todo, save_todo, session_lock

    subject: str | None = None
    ordinal = 0
    async with session_lock(session_id):
        todo = await get_todo(session_id)
        items = list(todo.items or [])
        changed = False
        for index, item in enumerate(items, start=1):
            if item.status == "in_progress":
                subject = subject or (item.subject or None)
                ordinal = ordinal or index
                item.status = "pending"
                item.started_at = None
                changed = True
        if changed:
            await save_todo(session_id, todo, user_id=user_id)
        return subject, ordinal, len(items)


async def abort_session_turn(
    session_id: str,
    user_id: str,
    *,
    reason: AbortReason = "user_stop",
    was_active: bool = True,
) -> bool:
    """End the run in flight and leave an honest record of it.

    ``was_active`` is the caller's answer to "was there actually a turn to
    stop": stopping an idle session is a no-op that must not write a marker.

    Returns whether a marker was written.
    """
    from session import session as session_mod
    from session.status import trigger_abort

    trigger_abort(session_id)
    await session_mod.set_session_status(session_id, SessionStatus.IDLE, user_id=user_id)
    if not was_active:
        return False
    # The loop only notices between steps; settling before it does would let
    # the dying run write its state back on top of ours.
    await asyncio.sleep(_ABORT_SETTLE_SECONDS)

    try:
        if await _already_marked(session_id, user_id):
            return False
        subject, ordinal, total = await settle_running_todos(session_id, user_id)
        await session_mod.create_user_message(
            session_id=session_id,
            text=marker_text(reason, subject, ordinal, total),
            synthetic=True,
            client_message_id=_marker_id(session_id),
            user_id=user_id,
        )
        return True
    except Exception as exc:
        # The stop itself already happened and is what the user asked for.
        # Losing the marker degrades the next turn's context; it must not turn
        # a successful stop into an error.
        log.warning(f"Could not record interruption for {session_id}", exc_info=True)
        return False
