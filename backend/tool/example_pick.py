"""example_pick: let the owner choose which finished piece this one should look like.

A guided Light conversation (做海报 / 做菜单 / 你拍我剪) calls this once, right
after its type step. The tool finds up to three library examples for the
entry, the scene and this shop (``light.examples.candidates``) and files a
``light_example_pick`` card (question.cards) that shows them; the call waits
there. The answer is written to the conversation in the same transaction that
resumes the call: ``sessions.light_example_id`` is the picked example, or
``none`` when the owner wanted none of them. From then on the system prompt's
参考样例 section holds that one example (docs/LIGHT_EXAMPLE_PICK_PLAN.md).

No card is shown, and the skill goes on with its own defaults, when the
library has nothing to show, when nobody is there to answer (cron, 省心托管),
or outside a guided conversation (a helper room the assistant started).
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from core.log import create_logger
from question.cards import CardKind, CardOption, ask_card, register
from tool.tool import ToolContext, ToolResult, define_tool

log = create_logger("tool.example_pick")

KIND = "light_example_pick"
STEP_ID = "example"
DECLINE_ID = "none"
LIMIT = 3

#: Everything the card says, per interface language (the example titles are the library's own).
TEXT = {
    "zh-CN": {"header": "挑样例", "question": "这几个是同行做过的成品，哪个更像你想要的？",
              "decline": "都不太像，直接做", "last": "上次选的"},
    "en-US": {"header": "Pick a look", "question": "Other shops made these. Which one is closest to what you want?",
              "decline": "None of these, just start", "last": "Your last pick"},
    "vi-VN": {"header": "Chọn mẫu", "question": "Đây là sản phẩm các cửa hàng khác đã làm. Mẫu nào giống ý bạn nhất?",
              "decline": "Không mẫu nào, cứ làm luôn", "last": "Lần trước bạn chọn"},
}

NO_CARD = ("No example card was shown ({reason}). Go on with the skill's own defaults for the look; "
           "do not mention examples to the owner and do not call example_pick again in this conversation.")


class ExamplePickArgs(BaseModel):
    scene: str | None = Field(default=None, max_length=32, pattern=r"^[a-z][a-z0-9_]*$", description=(
        "What the owner said this piece is for, as the scene id the skill lists for this entry "
        "(poster: new_item | holiday | opening | groupbuy; footage_edit: visit | new_item | promo | daily; "
        "menu: menu_mono | menu_red | menu_chalk | menu_dark). Omit when the answer fits none."))
    again: bool = Field(default=False, description=(
        "True only when the owner already picked an example in this conversation and now asks for a different "
        "look (换个样子 / 换个风格参考); the card is shown again. Otherwise the earlier pick is returned."))


def _texts(lang: str | None) -> dict[str, str]:
    return TEXT.get(lang or "", TEXT["zh-CN"])


def _labels(rows: list) -> list[str]:
    """Distinct option labels: a card answers by label, so two examples with one title get a number."""
    seen: dict[str, int] = {}
    labels = []
    for row in rows:
        title = " ".join(str(row.title or "").split())[:40] or "样例"
        seen[title] = seen.get(title, 0) + 1
        labels.append(title if seen[title] == 1 else f"{title} ({seen[title]})")
    return labels


def _chosen_output(row) -> str:
    from light import examples
    lead = ("The owner picked this example. Make this piece in its structure, rhythm and style, and say so in "
            "one short line before the next step.")
    if examples.usable_card(row):
        lead += (" It comes with 参考卡, written after looking at the finished piece. For a picture, write the "
                 "image prompt's Style and Layout lines from its 版式, 配色, 字体, 图片 and 装饰 sections, in place of "
                 "the skill's default style note and default layout line and even where it differs from a style "
                 "named earlier; keep those two lines unchanged on any retry, a retry changes only what the "
                 "failure was about. For a video, follow its shot rhythm, captions and ending. Take the copy "
                 "structure as levels only; every word, name and price comes from this owner.")
    elif (row.look or "").strip():
        lead += (" It comes with 样子说明, a description of the picked picture: write the image prompt's Style and "
                 "Layout lines from it (palette, layout, imagery, decoration, how names and prices are set), in "
                 "place of the skill's default style note and default layout line and even where it differs from "
                 "a style named earlier. Keep those two lines unchanged on any retry; a retry changes only what "
                 "the failure was about.")
    return lead + "\n" + examples.render([row], chosen=True)


async def _session(ctx: ToolContext):
    from db.base import get_db_session
    from db.models.session import Session
    if not getattr(ctx, "session_id", ""):
        return None
    async with get_db_session() as db:
        row = await db.get(Session, ctx.session_id)
    return row if row is not None and row.user_id == ctx.user_id else None


async def _set(ctx: ToolContext, **values) -> None:
    from sqlalchemy import update
    from db.base import get_db_session
    from db.models.session import Session
    async with get_db_session() as db:
        await db.execute(update(Session).where(Session.id == ctx.session_id, Session.user_id == ctx.user_id)
                         .values(**values))
        await db.commit()


def _skip(reason: str) -> ToolResult:
    return ToolResult(title="没有样例可挑", output=NO_CARD.format(reason=reason), metadata={"example_id": None})


async def execute(args: ExamplePickArgs, ctx: ToolContext) -> ToolResult:
    return await show(ctx, scene=args.scene, again=args.again)


async def show(ctx: ToolContext, *, scene: str | None, again: bool = False, via: str | None = None) -> ToolResult:
    """Show the example card for this conversation, or say why there is none.

    ``via``: the card is filed by the server on another tool's call (light/example_gate.py) because the
    agent went past the step; that call does not run and its result is the card's answer.
    """
    from core.ui_language import ui_language_for
    from db.repository.preference_repo import PgPreferenceRepo
    from light import examples
    from light.taxonomy import refresh, taxonomy
    from tool.video_model_card import unattended

    await refresh()
    session = await _session(ctx)
    entry_id = getattr(session, "light_entry", None)
    if session is None or not entry_id or entry_id not in taxonomy().entry_ids():
        return _skip("this is not a guided Light conversation")
    scene = scene if scene in taxonomy().scene_ids(entry_id) else None
    if scene and scene != session.light_scene:
        await _set(ctx, light_scene=scene)
    if unattended(session, ctx.session_id) or session.parent_id:
        return _skip("nobody is here to answer a card")

    if session.light_example_id and not again:
        chosen = await examples.get_active(session.light_example_id)
        if chosen is not None:
            from light import example_look
            await example_look.ensure(chosen, ctx)
            return ToolResult(title=f"沿用样例「{chosen.title}」", output=_chosen_output(chosen),
                              metadata={"example_id": chosen.id, "example_title": chosen.title, "reused": True})
        if session.light_example_id == examples.DECLINED:
            return _skip("the owner already chose none of the examples in this conversation")

    shop = await examples.shop_context(ctx.user_id)
    extra = ((await PgPreferenceRepo().get(ctx.user_id)) or {}).get("extra") or {}
    last = examples.last_pick(extra if isinstance(extra, dict) else {}, entry_id, scene)
    rows = await examples.candidates(entry_id, scene, shop["industry"], shop["shop_type"], limit=LIMIT, first=last)
    previews = [(row, examples.preview(row)) for row in rows]
    previews = [(row, shown) for row, shown in previews if shown is not None]
    if not previews:
        # Recorded, so the server does not look again before every later card (light/example_gate.py).
        if not session.light_example_id:
            await _set(ctx, light_example_id=examples.SKIPPED)
        return _skip("the library has no finished example for this entry yet")
    # What each candidate looks like, in words, so the one picked can be followed (read once per example).
    from light import example_look
    await example_look.ensure_all([row for row, _ in previews], ctx)

    text = _texts(await ui_language_for(ctx.user_id))
    options = [
        CardOption(id=row.id, label=label, description=text["last"] if row.id == last else "",
                   extra={**shown, "last_pick": row.id == last,
                          "scene_label": taxonomy().scene_label(entry_id, row.scene) if row.scene else ""})
        for (row, shown), label in zip(previews, _labels([row for row, _ in previews]))
    ]
    options.append(CardOption(id=DECLINE_ID, label=text["decline"], role="cancel"))
    await ask_card(ctx, kind=KIND, header=text["header"], question=text["question"], options=options,
                   payload={"entry_id": entry_id, "scene": scene, "example_ids": [row.id for row, _ in previews],
                            **({"via": via} if via else {})},
                   display={"light": {"step_id": STEP_ID}})
    raise AssertionError("ask_card() always suspends")  # pragma: no cover


async def _apply(db, session, row, payload: dict, option: dict | None) -> dict[str, Any]:
    """Keep the answer on the conversation, remember it for next time, and tell the waiting call."""
    from sqlalchemy import update
    from db.models.light_example import LightExample
    from db.repository.preference_repo import locked_preference
    from light import examples

    chosen = await db.get(LightExample, option["id"]) if option else None
    # Filed by the server on another tool's call: that call did not run, so say what happened and to repeat it.
    via = payload.get("via")
    again = ("" if not via else
             f"\nThe server showed the owner the example card before your `{via}` call, because the 挑样例 step "
             f"had been skipped; that `{via}` call did not run. Make it again now, with the look above in mind.")
    if chosen is None or chosen.status != "active":
        session.light_example_id = examples.DECLINED
        return {"title": "不参考样例",
                "output": ("The owner looked at the examples and chose none of them. Go on with the skill's own "
                           "defaults for the look, without any library example, and do not show the card again "
                           "unless the owner asks to see examples." + again),
                "metadata": {"example_id": None}}
    session.light_example_id = chosen.id
    await db.execute(update(LightExample).where(LightExample.id == chosen.id)
                     .values(pick_count=LightExample.pick_count + 1))
    try:
        preference = await locked_preference(db, session.user_id)
        preference.extra = examples.with_pick(preference.extra, payload.get("entry_id") or session.light_entry or "",
                                              payload.get("scene"), chosen.id)
    except Exception as exc:  # remembering the pick is a convenience; the pick itself stands
        log.warning("example pick not remembered session=%s error=%s", session.id, type(exc).__name__)
    return {"title": f"照「{chosen.title}」做", "output": _chosen_output(chosen) + again,
            "metadata": {"example_id": chosen.id, "example_title": chosen.title}}


register(CardKind(kind=KIND, voice_label="挑样例", apply=_apply, voice_answerable=False, present="inline"))


EXAMPLE_PICK_DESCRIPTION = """\
Light guided conversations only (做海报 / 做菜单 / 你拍我剪): show the owner up to three finished pieces from the \
example library and let them pick the one this piece should look like. Call it once, right after the owner \
answered the entry's type step, with that answer as `scene`. The card carries the pictures; you write no text \
about them. The result is either the picked example (its notes and recipe: follow its structure, rhythm and \
style, never copy its wording, shop name, dishes or prices) or a line saying no example applies, in which case \
you use the skill's own defaults. Costs no credits and needs no sandbox. Do not put it inside batch."""

example_pick_tool = define_tool(
    "example_pick",
    description=EXAMPLE_PICK_DESCRIPTION,
    parameters=ExamplePickArgs,
    execute=execute,
    sandbox_required=False,
    parallel_safe=False,
    discovery_hint="show library examples for the owner to pick a look; 挑样例 / 参考样例",
)
