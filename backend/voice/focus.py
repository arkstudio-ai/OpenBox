"""The task a call is about, said as a state the user understands (VOICE_SEAM_PLAN §3.4, §11.2).

Three readers share one wording: the greeting ("上次说到「X」，还在做"), the session instructions' focus
section while the call lasts, and the front desk's ``task_blocker`` quick read when the user asks "怎么还没动".
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

FOCUS_HOURS = 24  # a task nobody touched for longer is not "what we were talking about"


def state_phrase(desired: str, observed: str, lang: str = "zh", *, blocked_by: dict | None = None) -> str:
    """Where the task is and who has to do what, in one clause; never a field name."""
    en = lang == "en"
    if observed == "effect_unknown":
        return ("stopped with one step unconfirmed; say “checked, continue” once you have looked" if en
                else "停下时有一步结果没确认，你核对过说一声“核对过了，继续”就能接着做")
    if blocked_by or observed == "waiting_input":
        question = (blocked_by or {}).get("question") or ""
        head = "waiting for your answer" if en else "在等你回答"
        return f"{head}: {question}" if question else head
    if desired == "canceled" or observed == "canceled":
        return "stopped; say the word to continue" if en else "已停止，要继续跟我说一声"
    if observed == "resume_blocked":
        return "could not resume yet" if en else "暂时续不上，我会告诉你什么时候能继续"
    if desired == "paused" or observed in {"paused", "pausing"}:
        return "on hold; it continues whenever you say" if en else "暂停中，说继续就接着做"
    if observed == "error":
        return "stopped on an error" if en else "出错停了，要重试跟我说"
    if observed == "completed":
        return "finished" if en else "已经做完了"
    if observed in {"queued", "idle"}:
        return "waiting to start; nothing is stuck" if en else "排队中还没开始，不是卡住"
    return "in progress; nothing is stuck" if en else "正在做，没有卡住"


def focus_view(task, lang: str = "zh", *, blocked_by: dict | None = None) -> dict:
    return {"task_id": task.id, "title": task.title, "session_id": task.execution_session_id,
            "state": state_phrase(task.desired_state, task.observed_state, lang, blocked_by=blocked_by)}


async def recent_focus(user_id: str, workspace_id: str, main_session_id: str, lang: str = "zh") -> dict | None:
    """The open task most recently touched within FOCUS_HOURS: what the next call opens with."""
    from db.base import get_db_session
    from db.models.assistant import AssistantTask
    since = datetime.now(timezone.utc) - timedelta(hours=FOCUS_HOURS)
    async with get_db_session() as db:
        task = await db.scalar(select(AssistantTask).where(
            AssistantTask.assistant_session_id == main_session_id, AssistantTask.user_id == user_id,
            AssistantTask.workspace_id == workspace_id, AssistantTask.archived_at.is_(None),
            AssistantTask.updated_at >= since.replace(tzinfo=None) if _naive(db) else AssistantTask.updated_at >= since,
        ).order_by(AssistantTask.updated_at.desc(), AssistantTask.id.desc()).limit(1))
        return focus_view(task, lang) if task is not None else None


def _naive(db) -> bool:
    return db.bind.dialect.name == "sqlite"


async def blocker(scope, arguments: dict) -> dict:
    """``task_blocker``: where one task is and what it waits for, in a second, without an assistant turn."""
    from assistant.commands import similar_task_title
    from db.base import get_db_session
    from db.models.assistant import AssistantTask
    from db.models.session import Session
    task_id = str(arguments.get("task_id") or "").strip()
    title = str(arguments.get("title") or "").strip()
    async with get_db_session() as db:
        rows = (await db.scalars(select(AssistantTask).where(
            AssistantTask.assistant_session_id == scope.main_session_id, AssistantTask.user_id == scope.user_id,
            AssistantTask.workspace_id == scope.workspace_id, AssistantTask.archived_at.is_(None))
            .order_by(AssistantTask.updated_at.desc()).limit(50))).all()
        if task_id:
            task = next((row for row in rows if row.id == task_id), None)
        elif title:
            # A short spoken name ("艾薇兰") is part of the title; similar_task_title covers variant titles.
            task = next((row for row in rows if title in row.title or similar_task_title(row.title, title)), None)
        else:
            task = rows[0] if rows else None
        if task is None:
            return {"status": "not_found", "known_titles": [row.title for row in rows[:8]]}
        from assistant.blocking import blocked_by_locked
        session = await db.get(Session, task.execution_session_id)
        card = await blocked_by_locked(db, session) if session is not None else None
    view = focus_view(task, scope.lang, blocked_by=card)
    say = f"「{task.title}」{view['state']}" if scope.lang != "en" else f"“{task.title}” is {view['state']}"
    return {"status": "ok", **view, "desired_state": task.desired_state, "observed_state": task.observed_state,
            "waiting_on_card": bool(card), "say": say,
            "note": "say 是可直接口播的一句话；状态字段不能念。用户要让它继续、恢复或核对时，走 assistant_ask 转交，不在这里办。"}
