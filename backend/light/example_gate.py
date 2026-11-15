"""The example card is a step of the page, not a favour the agent may forget (docs/LIGHT_EXAMPLE_PICK_PLAN.md §11).

The skills tell the agent to call ``example_pick`` right after the type step.
It does not always: on gw2 one guided menu chat in three went straight to the
review card, once without even loading the skill. So the server keeps the
step. In a guided conversation whose entry has the 挑样例 step and whose owner
has not been shown the card yet, the first later card (``question``) or the
first production call (``image_gen``, ``footage_analyze``) does not run:
the example card is filed on that very tool call instead. Its answer comes
back as that call's result and tells the agent to make the call again.

Nothing happens when the step is behind us (an example was picked, the owner
chose none, or the library had nothing to show), when nobody is there to
answer, or for a card that belongs before the step (a poster's 用途, a menu's
样式, a cut's 成片类型).
"""
from __future__ import annotations

from core.log import create_logger

log = create_logger(__name__)

EXAMPLE_STEP = "example"


def _entry(session):
    from light.entries import light_entries
    entry = light_entries().entry(getattr(session, "light_entry", None) or "")
    if entry is None or EXAMPLE_STEP not in entry.step_ids():
        return None
    return entry


def before_the_step(entry, step_ids: list[str | None]) -> bool:
    """Every question of this card sits before 挑样例 on the step bar."""
    order = entry.step_ids()
    limit = order.index(EXAMPLE_STEP)
    return bool(step_ids) and all(step in order and order.index(step) < limit for step in step_ids)


async def _scene_from_words(session, entry) -> str | None:
    """The scene the owner named by tapping an opening chip (the chips are the entry's scenes, in order)."""
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.message import Message
    from db.models.part import Part
    from light.entries import light_entries
    from light.taxonomy import taxonomy
    scenes = taxonomy().scene_ids(entry.id)
    labels: dict[str, str] = {}
    for lang in ("zh-CN", "en-US", "vi-VN"):
        translated = light_entries(lang).entry(entry.id)
        chips = translated.opening_card.chips if translated and translated.opening_card else []
        if len(chips) != len(scenes):
            return None  # this entry's chips are not its scenes (a menu's are ways to send the dishes)
        labels.update({chip: scene for chip, scene in zip(chips, scenes)})
    async with get_db_session() as db:
        rows = (await db.execute(select(Part.data).join(Message, Message.id == Part.message_id).where(
            Part.session_id == session.id, Message.role == "user").order_by(Part.created_at).limit(40))).all()
    for (data,) in rows:
        text = str((data or {}).get("text") or "") if isinstance(data, dict) and data.get("type") == "text" else ""
        for chip, scene in labels.items():
            if chip and chip in text:
                return scene
    return None


async def _card_waiting(session_id: str) -> bool:
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.question import QuestionCheckpoint
    from tool.example_pick import KIND
    async with get_db_session() as db:
        rows = (await db.scalars(select(QuestionCheckpoint.continuation).where(
            QuestionCheckpoint.session_id == session_id, QuestionCheckpoint.status == "pending"))).all()
    return any(((row or {}).get("card") or {}).get("kind") == KIND for row in rows)


async def before(ctx, *, tool: str, step_ids: list[str | None] | None = None):
    """Called at the top of a tool that would take the conversation past the step.

    Returns None when the tool may run. Raises ``QuestionSuspended`` after filing the example card on this
    call. Returns a refusal ``ToolResult`` when an example card is already waiting (parallel calls ask once).
    """
    from tool import example_pick
    from tool.tool import ToolResult
    from tool.video_model_card import unattended
    try:
        session = await example_pick._session(ctx)
        entry = _entry(session) if session is not None else None
        if entry is None or session.light_example_id or session.parent_id:
            return None
        if unattended(session, ctx.session_id) or not getattr(ctx, "part_id", "") or not getattr(ctx, "message_id", ""):
            return None
        if tool == "question" and before_the_step(entry, list(step_ids or [])):
            return None
        if await _card_waiting(session.id):
            return ToolResult(title="等老板挑样例", metadata={"blocked": True, "gate": "light_example"}, output=(
                "The example card is waiting for the owner's pick in this conversation. Do not go on until it "
                "is answered."))
        scene = session.light_scene or await _scene_from_words(session, entry)
    except Exception as exc:  # the gate never breaks a tool; the step is then only as good as the agent
        log.warning("light example gate skipped session=%s tool=%s error=%s", getattr(ctx, "session_id", ""), tool,
                    type(exc).__name__)
        return None
    log.info("light example gate: card filed on %s session=%s scene=%s", tool, session.id, scene)
    await example_pick.show(ctx, scene=scene, via=tool)  # suspends when there is something to show
    return None  # nothing to show (now recorded): the tool runs
