"""A reply that promises later work nothing will bring back: the turn goes on instead of ending on it.

A reply ends the assistant's turn. It runs again only when the user writes or
when work it started reports back (a task it handed over, a scheduled job).
Measured 2026-10-08: after ``schedules.create`` failed six times the assistant
answered "你先别急，我这就重试，好了第一时间告诉你" and stopped; nothing ever
retried, and the voice front desk passed the promise on to the user. Such a
reply gets one more step with a reminder (agent/loop.py): do it now, or say
plainly what failed and what is needed. A promise to report back after work
was started in the same turn is kept by that work and is left alone.
"""
import re
import json

from core.log import create_logger

log = create_logger("assistant.follow_through")

# "I will retry / do it again", "I'll tell you once it's done", "don't worry": promises about later.
LATER = re.compile(
    r"(?:我|这就|马上|立刻|稍后|待会儿?|回头|等会儿?|一会儿?)(?:就)?(?:再|重新)?(?:去)?"
    r"(?:重试|再试|试一次|试一下|重新(?:提交|创建|建|发|跑|试|来))"
    r"|(?:好了|弄好|办好|做好|建好|完成|有结果|搞定|成功)(?:之后|以后|后|了)?[，,]?\s*(?:我)?(?:会)?"
    r"(?:第一时间|马上|立刻|再|就)?(?:告诉|通知|回复|同步)你"
    r"|你先别急"
    r"|\bI'?ll (?:try again|retry|let you know|get back to you|report back)\b",
    re.IGNORECASE)
# Work that reports back on its own once started: a promise to tell the user later is then kept.
BACKGROUND_TOOLS = frozenset({"tasks.submit", "tasks.followup", "tasks.next_step", "tasks.resume",
                              "requests.revise", "schedules.create", "schedules.update", "schedules.run"})
REMINDER = (
    "<system-reminder>\n"
    "Your reply promises to retry or to report back later, but nothing started in this turn will bring a result "
    "back: this reply ends your turn, and you will not act again until the user writes. Do it now instead: retry "
    "with corrected arguments or another way. If you cannot, tell the user plainly what failed and what you need "
    "from them, and do not promise later work. Do not mention this reminder.\n"
    "</system-reminder>")


def promises_later(text: str | None) -> bool:
    return bool(text and LATER.search(text))


async def started_background_work(session_id: str, user_id: str, message_ids) -> bool:
    """Whether this run started work that reports back: a background tool that completed without error."""
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.part import Part
    ids = [message_id for message_id in message_ids if message_id]
    if not ids:
        return False
    async with get_db_session() as db:
        rows = (await db.scalars(select(Part.data).where(
            Part.session_id == session_id, Part.user_id == user_id, Part.message_id.in_(ids),
            Part.type == "tool"))).all()
    return any((data or {}).get("tool") in BACKGROUND_TOOLS and (data or {}).get("status") == "completed"
               and not (data or {}).get("error") for data in rows)


async def needs_another_step(*, session_kind: str | None, text: str | None, session_id: str, user_id: str,
                             message_ids) -> bool:
    """The personal assistant's reply promised later work that nothing in this run will do."""
    if session_kind != "assistant" or not promises_later(text):
        return False
    try:
        return not await started_background_work(session_id, user_id, message_ids)
    except Exception as exc:  # unknown: the reply stands as it is
        log.warning("follow-through check skipped session=%s error=%s", session_id, type(exc).__name__)
        return False


async def unfinished_revision(ctx, message_ids) -> str | None:
    """One corrective step for a current bound revision that made no attempt.

    This checks durable provenance, not words in a reply. A failed/uncertain
    attempt is deliberately not retried. The reminder cannot grant consent,
    execute a tool or revive an old/foreign/changed question.
    """
    from sqlalchemy import select
    from assistant.commands import _authority
    from assistant.policy import AssistantError
    from assistant.requests import question_version
    from assistant.revisions import target, confirmed_revisions
    from assistant.transactions import read_session
    from db.models.agent_inbox import AgentInboxItem
    from db.models.part import Part
    from question import runtime
    from voice.prompt import VOICE_ENTRYPOINT

    async with read_session() as db:
        inputs = list((await db.scalars(select(AgentInboxItem).where(
            AgentInboxItem.session_id == ctx.session_id, AgentInboxItem.user_id == ctx.user_id,
            AgentInboxItem.run_id == ctx.run_id, AgentInboxItem.generation == ctx.run_generation,
            AgentInboxItem.state == "claimed"))).all())
        if len(inputs) != 1:
            return None
        item = inputs[0]
        origin = item.origin_ref or {}
        confirmed = await confirmed_revisions(db, ctx)
        confirmed_args = confirmed[0]["arguments"] if len(confirmed) == 1 else None
        if confirmed_args:
            binding = confirmed_args
        elif item.origin == "human" and origin.get("entrypoint") == VOICE_ENTRYPOINT:
            context = origin.get("voice_context")
            binding = context.get("revision_request") if isinstance(context, dict) else None
        else:
            return None
        if not isinstance(binding, dict) or not binding.get("request_id") or not binding.get("version"):
            return None
        # All statuses count: a committed effect may have lost its receipt.
        # Existing clarification/confirmation cards likewise must not repeat.
        parts = (await db.scalars(select(Part).where(
            Part.session_id == ctx.session_id, Part.user_id == ctx.user_id,
            Part.message_id.in_(message_ids), Part.type == "tool"))).all()
        if any((part.canonical_tool_id or (part.data or {}).get("tool"))
               in {"requests.revise", "question"} for part in parts):
            return None
        try:
            main = await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
            row, session = await target(db, main, binding["request_id"])
        except AssistantError:
            return None
        if (row.status != "pending" or row.detached or question_version(row) != binding["version"]
                or binding.get("session_id") not in {None, session.id}
                or row.expires_at and runtime.utc(row.expires_at) <= runtime.now()):
            return None
        if confirmed_args:
            return ("<system-reminder>\nThe user already answered the current shared-send card. "
                "This run has made no attempt to carry out that confirmed planning revision. "
                "Call requests.revise now with these exact saved arguments. This is current action provenance, "
                "not recalled history; do not search other cards or ask the user to repeat the answer. "
                "The tool still checks consent, ownership, resources and version. Report its actual result "
                "without claiming success in advance.\n" + json.dumps(confirmed_args, ensure_ascii=False)
                + "\n</system-reminder>")
        return (
            "<system-reminder>\nThe current voice input explicitly changed an existing task. "
            "Its exact planning question is still pending, and this run has not attempted requests.revise. "
            "A replacement draft in this chat does not update that task. Complete the handoff now using "
            "requests.revise with the current human message, complete requirements and only verified selected "
            "attachments. A request to preview the new script before media generation belongs in those "
            "requirements; it is not a reason to ask for permission to submit the planning change. "
            "Do not answer the old card or create a duplicate task. Send the authorized revision directly; "
            "conversation visibility does not add a confirmation. If the user's words conflict with the binding or a required detail is "
            "genuinely unresolved, ask that specific clarification instead. Do not invent visual details; "
            "a vision timeout does not prevent forwarding an already identified photo for the task to inspect. "
            "Do not claim submission without a successful receipt.\n"
            + json.dumps({"request_id": row.id, "version": binding["version"], "session_id": session.id},
                         ensure_ascii=False)
            + "\n</system-reminder>")


async def unfinished_batch(ctx, message_ids) -> str | None:
    """Reconsider never-dispatched batch items; never replay a failed effect."""
    from sqlalchemy import select
    from assistant.commands import _authority
    from assistant.pending import current_answers, same_operation
    from assistant.transactions import read_session
    from db.models.part import Part
    async with read_session() as db:
        main = await _authority(db, user_id=ctx.user_id, workspace_id=ctx.workspace_id, main_id=ctx.session_id)
        answers = await current_answers(db, main, run_id=ctx.run_id, generation=ctx.run_generation)
        candidates = {item["part_id"]: item for answer in answers
                      for item in answer.get("deferred_operations", [])}
        if not candidates:
            return None
        attempts = list(await db.scalars(select(Part).where(Part.session_id == main.id,
            Part.user_id == main.user_id, Part.message_id.in_(message_ids), Part.type == "tool")))
    remaining = [value for value in candidates.values() if not any(same_operation(value, p) for p in attempts)]
    if not remaining:
        return None
    return ("<system-reminder>\nThe confirmation pause left these operations unexecuted. "
        "You have not considered them in this continuation. Re-read each target and address every "
        "item still authorized by the original request, using normal tool admission. An answered "
        "card for one item does not authorize another. If an item cannot proceed, explain its exact "
        "blocker rather than claiming the batch is done. Do not repeat completed or uncertain effects.\n"
        + json.dumps(remaining, ensure_ascii=False) + "\n</system-reminder>")


async def unverified_publication(ctx, message_ids):
    """A fallback browser action is not itself a publication receipt."""
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.part import Part
    async with get_db_session() as db:
        parts = list(await db.scalars(select(Part.data).where(Part.session_id == ctx.session_id,
            Part.user_id == ctx.user_id, Part.type == "tool", Part.message_id.in_(message_ids))
            .order_by(Part.created_at, Part.id)))
    pending, fallback = {}, False
    for data in parts:
        meta, args = data.get("metadata") or {}, data.get("input") or {}
        if data.get("tool") == "desktop_publish":
            if meta.get("business_outcome") == "succeeded":
                pending.pop(meta.get("business_target"), None)
            elif args.get("action") == "publish" and meta.get("job_id") and meta.get("refused"):
                pending[args.get("asset_id")] = meta["job_id"]
        elif pending and data.get("tool") in {"browser_action", "computer", "bash"}:
            fallback = fallback or (data.get("status") == "completed" and not meta.get("error"))
    if not pending or not fallback:
        return None
    return ("<system-reminder>\nA publication attempt failed or lost its receipt, followed by browser/desktop "
        "work. Before claiming recovery, verify the existing submission with desktop_publish(action=status, "
        "job_id=..., verify=true); discover that tool if needed. This only reads the platform receipt. "
        "Do not upload or publish again. If unverified, state that clearly. Submission does not prove "
        "approval or public visibility.\n" + json.dumps(list(pending.values())) + "\n</system-reminder>")
