"""The assistant's own memory writes and project briefs (V2 P3).

docs/PERSONAL_ASSISTANT_DESIGN_V2.md 8.3 and 9.2: the assistant remembers,
corrects and forgets only on the user's own words in this conversation (a
quote of a human message is required). Ordinary preferences take effect at
once and can be undone from the chip under the answer. Health, money,
relationships and similar matters wait for a confirmation card (D5).
Credentials, identity or card numbers and similar data are never kept.
"""
import re

from sqlalchemy import select

from assistant.commands import _authority, _project
from assistant.policy import AssistantError
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem

#: A backstop for the model's own `sensitive` flag. A false positive only asks.
SOFT_SENSITIVE = re.compile(
    "|".join((
        "病", "诊断", "药物", "服药", "医院", "怀孕", "抑郁", "焦虑", "心理", "健康", "体检",
        "工资", "薪水", "收入", "存款", "负债", "欠款", "贷款", "资产", "理财", "股票",
        "离婚", "恋爱", "男朋友", "女朋友", "伴侣", "家暴", "宗教", "信仰", "政治", "性取向",
        r"\bhealth\b", "diagnos", "medicat", "pregnan", "therapy", "salary", "income", r"\bdebt",
        r"\bloan", "divorce", "religio", "politic", "sexual",
    )), re.IGNORECASE)
MAX_SUMMARY = 500
QUOTE_WINDOW = 200
MIN_QUOTE = 4


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def soft_sensitive(summary: str) -> bool:
    return bool(SOFT_SENSITIVE.search(summary or ""))


async def _human_quote(db, main, quote: str) -> str:
    """The user's own words in this main session authorize the write."""
    quote = _normalized(quote)
    if not 1 <= len(quote) <= 1000:
        raise AssistantError(400, "ASSISTANT_MEMORY_SOURCE", "Quote the user's own words that ask for this")
    prompts = (await db.execute(select(AgentInboxItem.message_id, AgentInboxItem.prompt).where(
        AgentInboxItem.session_id == main.id, AgentInboxItem.user_id == main.user_id,
        AgentInboxItem.origin == "human", AgentInboxItem.message_id.is_not(None))
        .order_by(AgentInboxItem.created_at.desc()).limit(QUOTE_WINDOW))).all()
    for message_id, prompt in prompts:
        prompt = _normalized(prompt)
        # A word or two could come from anywhere; ask for the user's request itself.
        if quote in prompt and (len(quote) >= MIN_QUOTE or quote == prompt):
            return message_id
    raise AssistantError(403, "ASSISTANT_MEMORY_SOURCE",
                         "The quote must be the user's own words from this conversation")


async def _scope(ctx, quote, project_id):
    async with get_db_session() as db:
        main = await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        message_id = await _human_quote(db, main, quote)
        if project_id:
            await _project(db, project_id, ctx.user_id, ctx.workspace_id)
    return message_id


def _memory_error(exc):
    from memory import service
    if isinstance(exc, service.MemoryConflict):
        return AssistantError(409, "ASSISTANT_MEMORY_CONFLICT", "This memory changed; search it again")
    if isinstance(exc, LookupError):
        return AssistantError(404, "ASSISTANT_MEMORY_UNAVAILABLE", "Memory is unavailable")
    return AssistantError(400, "ASSISTANT_MEMORY_INVALID", str(exc) or "Invalid memory request")


async def remember(ctx, *, summary: str, quote: str, project_id: str | None = None,
                   sensitive: bool = False, fact_key: str | None = None) -> dict:
    from memory import service
    from memory.settings import saving_paused
    summary = _normalized(summary)
    if not 1 <= len(summary) <= MAX_SUMMARY:
        raise ValueError(f"A memory summary of 1 to {MAX_SUMMARY} characters is required")
    if await saving_paused(ctx.user_id, ctx.session_id):
        return {"state": "paused", "instruction": "The user turned memory saving off. Nothing was saved; say so."}
    await _scope(ctx, quote, project_id)
    scope = {"scope": "project" if project_id else "personal", "project_id": project_id}
    if sensitive or soft_sensitive(summary):
        try:
            proposal = await service.propose_note(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
                                                  project_id=project_id, summary=summary, session_id=None)
        except service.MemorySensitiveContent as exc:
            return _refused(exc.kind)
        except service.MemoryDeclined:
            return {"state": "declined_before", **scope,
                    "instruction": "The user declined or forgot this before. Nothing was saved; do not propose it again."}
        except (service.MemoryConflict, LookupError, ValueError) as exc:
            raise _memory_error(exc) from exc
        if proposal["status"] == "ACTIVE":
            return {"state": "already_remembered", "memory_id": proposal["id"], **scope}
        from question.question import Question, QuestionOption, ask
        await ask(ctx.session_id, [Question(question=f"要我记住这条吗？\n「{summary}」", header="记忆确认", custom=False,
            options=[QuestionOption(label="记住", description="确认保存为长期记忆"),
                     QuestionOption(label="不用记", description="不保存，且不再提议这条")],
            detail={"kind": "memory_proposal", "summary": summary, "memory_id": proposal["id"]})],
            tool={"callID": ctx.part_id, "messageID": ctx.message_id}, user_id=ctx.user_id,
            continuation={"kind": "memory_proposal", "memory_id": proposal["id"],
                          "workspace_id": proposal.get("workspace_id") or ctx.workspace_id,
                          "expected_revision": proposal["revision"]})
    try:
        row = await service.create_note(user_id=ctx.user_id, workspace_id=ctx.workspace_id, project_id=project_id,
                                        summary=summary, request_id=f"assistant:{ctx.part_id}", fact_key=fact_key)
    except service.MemorySensitiveContent as exc:
        return _refused(exc.kind)
    except (service.MemoryConflict, LookupError, ValueError) as exc:
        raise _memory_error(exc) from exc
    return {"state": "remembered", "memory_id": row["id"], "summary": row["summary"], "revision": row["revision"],
            **scope, "undo": "The user can undo this from the chip under your answer."}


def _refused(kind):
    return {"state": "refused", "reason": kind,
            "instruction": "Memory never keeps passwords, identity or card numbers, phone numbers, email "
                           "addresses or house numbers. Nothing was saved; tell the user and offer to save the rest."}


async def update(ctx, *, memory_id: str, summary: str, quote: str, expected_revision: int | None = None) -> dict:
    from memory import service
    summary = _normalized(summary)
    if not 1 <= len(summary) <= MAX_SUMMARY:
        raise ValueError(f"A memory summary of 1 to {MAX_SUMMARY} characters is required")
    await _scope(ctx, quote, None)
    try:
        row = await service.edit_note(user_id=ctx.user_id, workspace_id=ctx.workspace_id, memory_id=memory_id,
                                      summary=summary, expected_revision=expected_revision,
                                      request_id=f"assistant:{ctx.part_id}")
    except service.MemorySensitiveContent as exc:
        return _refused(exc.kind)
    except (service.MemoryConflict, LookupError, ValueError) as exc:
        raise _memory_error(exc) from exc
    if row is None:
        raise _memory_error(LookupError(memory_id))
    return {"state": "updated", "memory_id": row["id"], "summary": row["summary"], "revision": row["revision"]}


async def forget(ctx, *, memory_id: str, quote: str) -> dict:
    """Forget on the user's explicit request; earlier chats are not rewritten (D1)."""
    from memory import service
    await _scope(ctx, quote, None)
    try:
        result = await service.forget_memory(user_id=ctx.user_id, workspace_id=ctx.workspace_id,
                                             memory_id=memory_id, request_id=f"assistant:{ctx.part_id}")
    except (service.MemoryConflict, LookupError, ValueError) as exc:
        raise _memory_error(exc) from exc
    if result is None:
        raise _memory_error(LookupError(memory_id))
    return {"state": "forgotten", "memory_id": memory_id,
            "note": "Not used from now on; earlier chat history is not rewritten."}


async def brief_read(ctx, *, project_id: str) -> dict:
    from project.brief import ProjectBriefError, get_brief
    try:
        value = await get_brief(user_id=ctx.user_id, workspace_id=ctx.workspace_id, project_id=project_id)
    except ProjectBriefError as exc:
        raise AssistantError(404, "ASSISTANT_PROJECT_UNAVAILABLE", str(exc) or "Project is unavailable") from exc
    return {"project_id": project_id, "brief": value, "untrusted_data": True}


async def brief_update(ctx, *, project_id: str, content: str, expected_revision: int) -> dict:
    from project.brief import (ProjectBriefConflict, ProjectBriefError, ProjectBriefNotFound,
                               ProjectBriefSensitiveContent, update_brief)
    async with get_db_session() as db:
        await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
    try:
        return await update_brief(user_id=ctx.user_id, workspace_id=ctx.workspace_id, project_id=project_id,
                                  content=content, expected_revision=expected_revision, updated_by="assistant")
    except ProjectBriefConflict as exc:
        raise AssistantError(409, "ASSISTANT_BRIEF_CONFLICT", "The brief changed; read it again") from exc
    except ProjectBriefNotFound as exc:
        raise AssistantError(404, "ASSISTANT_PROJECT_UNAVAILABLE", "Project is unavailable") from exc
    except ProjectBriefSensitiveContent as exc:
        raise AssistantError(422, "ASSISTANT_BRIEF_SENSITIVE",
                             "A brief holds project facts only, never credentials or personal identifiers") from exc
    except ProjectBriefError as exc:
        raise AssistantError(400, "ASSISTANT_BRIEF_INVALID", str(exc) or "Invalid brief") from exc
