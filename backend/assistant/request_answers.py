"""Scoped task answers and semantic completion reports.

The model interprets the cited human messages; this module validates provenance,
question identity, ownership, version, option labels and declared consequences.
Human-only steps stay human-only. Reporting completion of a desktop takeover
requires its exact completion option, a semantic completion flag and verified
human source; the resumed tool re-checks the page. Confirmation derives from
question metadata and attachments, never workspace visibility or incidental words
in a task title. Legacy questions with no consequence metadata remain gated.
"""
from sqlalchemy import select

from assistant.commands import ToolSource, _authority, _tool_source_locked, command_digest
from assistant.policy import AssistantError
from db.base import get_db_session
from db.models.part import Part
from db.models.project import Project
from db.models.question import QuestionCheckpoint, SessionExecution
from db.models.session import Session
from session.internal_parts import begin_session_write

#: Tools whose questions the assistant may answer. Others ask for a decision
#: or an action only the user can give.
ANSWERABLE_TOOLS = frozenset({"question"})
ANSWERED_BY = "assistant"
TAKEOVER_REASON = ("It is a desktop takeover: only the user can do the verification on that page (on the "
                   "cloud desktop opened from that conversation, or in their own browser when browser is "
                   "extension). Tell them where, with the link, and to press 「{done}」 when finished. Only after "
                   "the user's own message says they finished may you answer it, citing that message, and only "
                   "with 「{done}」.")


def link(session_id: str) -> str:
    return f"/app/s/{session_id}"


def human_only(session_id: str, reason: str) -> AssistantError:
    return AssistantError(403, "ASSISTANT_ANSWER_HUMAN_ONLY",
        f"{reason} Only the user can answer it: tell them and give the link {link(session_id)}")


def is_takeover(row: QuestionCheckpoint, tool: str | None) -> bool:
    return tool == "desktop_takeover" and any(
        (question.get("detail") or {}).get("kind") == "desktop_takeover" for question in row.questions or [])


async def _tool(db, row: QuestionCheckpoint, session: Session) -> str | None:
    if not row.part_id:
        return None
    part = await db.get(Part, row.part_id)
    if part is None or part.session_id != session.id:
        return None
    return part.canonical_tool_id or (part.data or {}).get("tool")


async def answerable(db, row: QuestionCheckpoint, session: Session, answers=None) -> tuple[str | None, bool]:
    """Why the user must answer this question themselves (or None), and whether
    an answer by the assistant is high-risk: the user confirms it on a card."""
    if (row.continuation or {}).get("kind") != "question":
        return "It is an approval or a confirmation, not an ordinary question.", False
    tool = await _tool(db, row, session)
    if is_takeover(row, tool):
        from tool.desktop_takeover import ANSWER_DONE
        # Listing (no answer yet) shows it as the user's; the completion option alone is answerable,
        # and answer_question then requires the user's cited words that they finished.
        if answers != [[ANSWER_DONE]]:
            return TAKEOVER_REASON.format(done=ANSWER_DONE), False
        return None, False
    if tool not in ANSWERABLE_TOOLS:
        return "It asks the user for a decision or an action.", False
    high_risk = False
    for question in row.questions or []:
        if (question.get("detail") or {}).get("kind"):
            return "It is a request for the user's own action.", False
        # Choosing files (or choosing none) decides what the work is built from.
        high_risk = high_risk or bool(question.get("allow_attachments"))
        # New cards declare the effect of answering them. Legacy cards without
        # this metadata require confirmation; their prose cannot safely classify risk.
        high_risk = high_risk or question.get("requires_confirmation", True)
    return None, high_risk



async def _target(db, ctx, request_id: str, *, allow_handled=False):
    row = await db.get(QuestionCheckpoint, request_id)
    session = await db.get(Session, row.session_id) if row is not None else None
    if (row is None or session is None or row.user_id != ctx.user_id or session.user_id != ctx.user_id
            or session.workspace_id != ctx.workspace_id or session.is_deleted or session.kind == "assistant"
            or session.id == ctx.session_id):
        raise AssistantError(404, "ASSISTANT_REQUEST_UNAVAILABLE", "Question is unavailable")
    if allow_handled and row.status in {"answered", "rejected", "superseded"}:
        return row, session
    execution = await db.get(SessionExecution, row.session_id)
    if row.status != "pending" or execution is None or row.generation != execution.generation:
        raise AssistantError(410, "ASSISTANT_REQUEST_UNAVAILABLE", "The question is no longer waiting for an answer")
    return row, session


def _answers_text(questions, answers, files=None) -> str:
    lines = []
    for index, (question, values) in enumerate(zip(questions, answers)):
        names = (files or [[] for _ in questions])[index]
        answer = "、".join([*values, *(f"文件「{name}」" for name in names)]) or "（不填）"
        lines.append(f"{question.get('question', '')} → {answer}")
    return "；\n".join(lines)


def _impact(title, visibility, questions, files, texts) -> str:
    """What answering changes, from the conversation and the answer itself."""
    parts = [f"「{title or '这个会话'}」会按这个回答继续执行"]
    if visibility == "workspace":
        parts.append("工作区成员都能看到这条回答")
    if any(files):
        parts.append(f"会把 {sum(map(len, files))} 个文件交给它使用")
    elif any(question.get("allow_attachments") for question in questions):
        parts.append("不提供文件，它会按所选方式直接做")
    return "；".join(parts) + "。"


async def answer_question(ctx, *, request_id: str, answers: list[list[str]],
                          attachments: list[list[str]] | None = None,
                          source_message_ids: list[str] | None = None,
                          completion_reported: bool = False, requires_confirmation: bool = False) -> dict:
    from question import question as questions
    async with get_db_session() as db:
        await begin_session_write(db)
        main = await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        row, session = await _target(db, ctx, request_id, allow_handled=True)
        if row.status != "pending":
            # A concurrent voice/manual decision already owns the outcome.
            # This is an observation, never a second answer or fresh consent.
            return {"request_id": row.id, "state": row.status, "this_call_applied": False,
                    "session_id": session.id, "link": link(session.id),
                    "note": "这张卡片已处理，没有再次提交。请读取原任务的当前状态，不重复确认或重做。"}
        reason, high_risk = await answerable(db, row, session, answers)
        if reason:
            raise human_only(session.id, reason)
        # The user's own words asking for this answer, verified (main is locked
        # before any file row) and kept with the answer.
        sources = (await _tool_source_locked(db, main, ToolSource(ctx.part_id, ctx.run_id, ctx.run_generation,
            tuple(source_message_ids)), "request_answer"))["source_refs"] if source_message_ids else []
        if is_takeover(row, await _tool(db, row, session)):
            # Pressing 「我已完成，继续」 for the user needs their own words that they finished.
            # The assistant interprets the cited human message in context. Only
            # provenance, the exact takeover and its completion option are server gates.
            if not sources or not completion_reported:
                from tool.desktop_takeover import ANSWER_DONE
                raise human_only(session.id, ("The user has not said they finished the verification. "
                    if sources else "Cite the user's own message saying they finished. ")
                    + TAKEOVER_REASON.format(done=ANSWER_DONE))
        items = [questions.Question(**item) for item in row.questions]
        try:
            files = questions.normalize_attachments(items, attachments)
            clean = questions.validate_answers(items, answers, attachments=files)
            assets = await questions.validate_attachment_ownership(db, session, files) if any(files) else {}
        except ValueError as exc:
            raise AssistantError(400, "ASSISTANT_ANSWER_INVALID", str(exc)) from exc
        title, saved = session.title or "", list(row.questions)
        names = [[assets[asset_id].name for asset_id in ids] for ids in files]
    confirmation = None
    if high_risk or requires_confirmation:
        from assistant.confirmations import CONFIRM_KIND, require_card
        texts = [title, *(str(item.get("question", "")) for item in saved),
                 *(str(option.get("label", "")) for item in saved for option in item.get("options") or []),
                 *(value for values in clean for value in values)]
        # The user sees the exact answer and files first; members may read a shared conversation.
        confirmation = await require_card(ctx, kind=CONFIRM_KIND, action="request_answer",
            digest=command_digest({"answer": request_id, "answers": clean,
                                   **({"attachments": files} if any(files) else {})}),
            prompt=f"以你的名义回答「{title}」里的问题：" + ("\n" if len(saved) > 1 else "")
                   + _answers_text(saved, clean, names),
            impact=_impact(title, visibility, saved, files, texts),
            header="确认代答", description="由个人助理代你回答", confirm="确认代答",
            target={"request_id": request_id})
    answered_by = {"kind": ANSWERED_BY, "main_session_id": ctx.session_id, "part_id": ctx.part_id,
                   **({"source_refs": sources} if sources else {})}
    from assistant.requests import assistant_answer
    try:
        receipt = await assistant_answer(row, clean, answered_by=answered_by,
                                         attachments=files if any(files) else None)
    except BaseException:
        from assistant.confirmations import release_confirmation
        await release_confirmation(ctx, confirmation)
        raise
    return {"state": "answered", "request_id": request_id, "session_id": session.id, "session_title": title,
            "answers": clean, "link": link(session.id),
            **({"attachments": [[{"asset_id": asset_id, "name": name} for asset_id, name in zip(ids, labels)]
                                for ids, labels in zip(files, names)]} if any(files) else {}),
            "note": "The conversation shows this as answered by the personal assistant (由个人助理代答) and continues.",
            **({"command_id": receipt["command_id"]} if receipt and receipt.get("command_id") else {})}


async def list_waiting(*, user_id: str, workspace_id: str, main_id: str, limit: int = 30) -> list[dict]:
    """Pending questions across all the user's own conversations, newest first."""
    from assistant.task_context import _iso
    from assistant.requests import question_version
    from db.models.message import Message
    from db.models.file_asset import FileAsset
    from assets.service import owned_ready_query
    async with get_db_session() as db:
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        rows = (await db.execute(select(QuestionCheckpoint, Session, Project.name).join(
            Session, Session.id == QuestionCheckpoint.session_id).join(
            SessionExecution, SessionExecution.session_id == QuestionCheckpoint.session_id).outerjoin(
            Project, Project.id == Session.project_id).where(
            QuestionCheckpoint.user_id == user_id, QuestionCheckpoint.status == "pending",
            QuestionCheckpoint.generation == SessionExecution.generation,
            Session.user_id == user_id, Session.workspace_id == workspace_id, Session.is_deleted.is_(False),
            Session.kind != "assistant", Session.id != main_id)
            .order_by(QuestionCheckpoint.created_at.desc(), QuestionCheckpoint.id.desc()).limit(limit))).all()
        items = []
        from assistant.blocking import view as card_view
        for row, session, project_name in rows:
            reason, high_risk = await answerable(db, row, session)
            card = card_view(row, session)
            # Skills display the complete script/quote in prose before asking the
            # question. The voice model must see it too, not just "可以吗？".
            parts = (await db.scalars(select(Part).join(Message, Message.id == Part.message_id).where(
                Part.session_id == session.id, Part.user_id == user_id, Part.type == "text",
                Message.role == "assistant", Message.created_at <= row.created_at)
                .order_by(Part.created_at.desc(), Part.id.desc()).limit(4))).all()
            context = "\n\n".join(str((part.data or {}).get("text") or "") for part in reversed(parts))
            draft = row.draft or []
            asset_ids = {identity for answer in draft for identity in answer.get("attachments") or []}
            draft_assets = {asset.id: {"asset_id": asset.id, "name": asset.name, "mime": asset.mime}
                for asset in (await db.scalars(owned_ready_query(user_id, workspace_id).where(
                    FileAsset.id.in_(asset_ids)))).all()} if asset_ids else {}
            items.append({
                "kind": "question", "id": row.id, "session_id": session.id,
                "version": question_version(row),
                "context": context[-12000:], "context_truncated": len(context) > 12000,
                "session_title": session.title or "", "project_id": session.project_id,
                "project_name": project_name, "visibility": session.visibility,
                "asked_at": _iso(row.created_at), "link": link(session.id),
                # What kind of card and where to act: a desktop_takeover is done on the cloud desktop
                # (browser "local") or in the user's own browser ("extension"), at host.
                "card_kind": card["kind"],
                **{key: card[key] for key in ("host", "browser", "done_option") if card.get(key)},
                "questions": [{"header": item.get("header", ""), "question": str(item.get("question", "")),
                               "options": [option.get("label") for option in item.get("options") or []],
                               "option_descriptions": [option.get("description", "") for option in item.get("options") or []],
                               "multiple": bool(item.get("multiple")), "custom": item.get("custom", True),
                               "draft_attachments": [draft_assets[identity] for identity in
                                   (draft[index].get("attachments") or [] if index < len(draft) else [])
                                   if identity in draft_assets] if item.get("allow_attachments") else [],
                               **({"allow_attachments": True} if item.get("allow_attachments") else {})}
                              for index, item in enumerate(row.questions or [])],
                "assistant_may_answer": reason is None,
                **({"user_only_reason": reason} if reason else
                   {"high_risk": high_risk}),
            })
        return items
