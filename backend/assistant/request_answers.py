"""The personal assistant answers questions for the user (V2 D6).

docs/PERSONAL_ASSISTANT_DESIGN_V2.md 10: a question an agent asks in one of
the user's own conversations may be answered by their personal assistant; the
conversation shows "由个人助理代答". Decisions stay with the user, who gets a
link instead: permission approvals, plan reviews, memory confirmations,
desktop takeovers and requests for the user's own action.

docs/ASSISTANT_VOICE_FIX_PLAN.md 3.1: a file choice is answerable too, with an
option that needs no file or with the user's own files as attachments. Such an
answer, an answer about money, payment, publishing, authorization or deletion,
and any answer in a workspace-visible conversation (D4) is shown to the user on
a confirmation card first.
"""
import re

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
#: Words that make an answer high-risk wherever they appear: the question, its
#: options, the answer or the conversation's title (a publishing conversation).
#: A false positive only asks the user on a card.
HIGH_RISK = re.compile("|".join((
    "付款", "支付", "付费", "扣费", "扣款", "收费", "金额", "价格", "费用", "购买", "下单", "充值", "转账",
    "退款", "订阅", "发布", "上线", "发表", "公开", "授权", "登录", "删除", "清空", "移除", "销毁", "注销",
    "覆盖", "重置", r"\bpay(?:s|ing|ment|ments)?\b", "purchase", r"\bbuy\b", r"\bprices?\b", "refund",
    "subscri", "publish", "authori", r"\blog ?in\b", r"\bsign ?in\b", "delete", "remove", r"\bwipe",
    "erase", "overwrite", r"[$¥￥]\s*\d", r"\d\s*(?:元|块钱|美元|usd|rmb|cny)",
)), re.IGNORECASE)


def link(session_id: str) -> str:
    return f"/app/s/{session_id}"


def human_only(session_id: str, reason: str) -> AssistantError:
    return AssistantError(403, "ASSISTANT_ANSWER_HUMAN_ONLY",
        f"{reason} Only the user can answer it: tell them and give the link {link(session_id)}")


async def answerable(db, row: QuestionCheckpoint, session: Session, answers=None) -> tuple[str | None, bool]:
    """Why the user must answer this question themselves (or None), and whether
    an answer by the assistant is high-risk: the user confirms it on a card."""
    if (row.continuation or {}).get("kind") != "question":
        return "It is an approval or a confirmation, not an ordinary question.", False
    tool = None
    if row.part_id:
        part = await db.get(Part, row.part_id)
        if part is not None and part.session_id == session.id:
            tool = part.canonical_tool_id or (part.data or {}).get("tool")
    if tool not in ANSWERABLE_TOOLS:
        return "It asks the user for a decision or an action.", False
    texts, high_risk = [session.title or ""], False
    for question in row.questions or []:
        if (question.get("detail") or {}).get("kind"):
            return "It is a request for the user's own action.", False
        # Choosing files (or choosing none) decides what the work is built from.
        high_risk = high_risk or bool(question.get("allow_attachments"))
        texts += [str(question.get("question", "")), str(question.get("header", ""))]
        texts += [f"{option.get('label', '')} {option.get('description', '')}" for option in question.get("options") or []]
    texts += [value for values in answers or [] if isinstance(values, list) for value in values if isinstance(value, str)]
    return None, high_risk or any(HIGH_RISK.search(text) for text in texts)


async def _target(db, ctx, request_id: str):
    row = await db.get(QuestionCheckpoint, request_id)
    session = await db.get(Session, row.session_id) if row is not None else None
    if (row is None or session is None or row.user_id != ctx.user_id or session.user_id != ctx.user_id
            or session.workspace_id != ctx.workspace_id or session.is_deleted or session.kind == "assistant"
            or session.id == ctx.session_id):
        raise AssistantError(404, "ASSISTANT_REQUEST_UNAVAILABLE", "Question is unavailable")
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
    words = sorted({match.group(0) for text in texts for match in HIGH_RISK.finditer(text)})
    if words:
        parts.append("这个问题涉及" + "、".join(f"「{word}」" for word in words[:4]))
    return "；".join(parts) + "。"


async def answer_question(ctx, *, request_id: str, answers: list[list[str]],
                          attachments: list[list[str]] | None = None,
                          source_message_ids: list[str] | None = None) -> dict:
    from question import question as questions
    async with get_db_session() as db:
        await begin_session_write(db)
        main = await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        row, session = await _target(db, ctx, request_id)
        reason, high_risk = await answerable(db, row, session, answers)
        if reason:
            raise human_only(session.id, reason)
        # The user's own words asking for this answer, verified (main is locked
        # before any file row) and kept with the answer.
        sources = (await _tool_source_locked(db, main, ToolSource(ctx.part_id, ctx.run_id, ctx.run_generation,
            tuple(source_message_ids)), "request_answer"))["source_refs"] if source_message_ids else []
        items = [questions.Question(**item) for item in row.questions]
        try:
            files = questions.normalize_attachments(items, attachments)
            clean = questions.validate_answers(items, answers, attachments=files)
            assets = await questions.validate_attachment_ownership(db, session, files) if any(files) else {}
        except ValueError as exc:
            raise AssistantError(400, "ASSISTANT_ANSWER_INVALID", str(exc)) from exc
        title, visibility, saved = session.title or "", session.visibility, list(row.questions)
        names = [[assets[asset_id].name for asset_id in ids] for ids in files]
    confirmation = None
    if high_risk or visibility == "workspace":
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
        for row, session, project_name in rows:
            reason, high_risk = await answerable(db, row, session)
            items.append({
                "kind": "question", "id": row.id, "session_id": session.id,
                "session_title": session.title or "", "project_id": session.project_id,
                "project_name": project_name, "visibility": session.visibility,
                "asked_at": _iso(row.created_at), "link": link(session.id),
                "questions": [{"header": item.get("header", ""), "question": str(item.get("question", ""))[:600],
                               "options": [option.get("label") for option in item.get("options") or []],
                               "multiple": bool(item.get("multiple")), "custom": item.get("custom", True),
                               **({"allow_attachments": True} if item.get("allow_attachments") else {})}
                              for item in row.questions or []],
                "assistant_may_answer": reason is None,
                **({"user_only_reason": reason} if reason else
                   {"high_risk": high_risk or session.visibility == "workspace"}),
            })
        return items
