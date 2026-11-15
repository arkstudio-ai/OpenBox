"""The reference example library behind the Light entries (docs/LIGHT_MODE_IMPL_PLAN.md §11).

The content team approves work in the console; this module is the other
half: find the examples that fit this shop (entry → industry → shop type →
tags, falling back level by level), render them as a 参考样例 section of the
Light prompt fragment, and count each use. ``prefill`` reads one finished
session back into the fields of a new example so the team only adds the
shop type, tags, rating and notes.

The owner can also choose: after the entry's type step the ``example_pick``
tool shows up to three finished pieces (``candidates``), and the one picked is
kept on the conversation (``sessions.light_example_id``). From then on the
prompt section holds that one example only (docs/LIGHT_EXAMPLE_PICK_PLAN.md).
"""
from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Iterable

from core.log import create_logger
from light.taxonomy import taxonomy

log = create_logger(__name__)

#: Tools whose ``prompt`` argument is the recipe the model wrote.
PROMPT_TOOLS = ("video_generate", "image_gen", "video_compose", "footage_analyze")
#: File relation roles that are the finished work, and the mimes we keep.
OUTPUT_ROLES = ("result", "final")
OUTPUT_MIMES = ("image/", "video/")
RECIPE_LIMIT = 1200
NOTES_LIMIT = 300
#: The three rules every rendered section ends with.
RULES = (
    "只学样例的结构、节奏和风格，不照抄它的文案、店名、菜名或价格；",
    "一律换成这家店的真实信息（店名、主打、价格、地址都用老板给的）；",
    "老板说要不一样、不要参考，就完全不参考。",
)
#: (session_id, example_id) already counted this process; one reference per
#: conversation, not one per turn the fragment is rebuilt for.
_COUNTED: "OrderedDict[tuple[str, str], None]" = OrderedDict()
_COUNTED_LIMIT = 4096
#: ``sessions.light_example_id`` when the owner looked at the card and chose none of them.
DECLINED = "none"
#: Where the last pick per entry and scene is remembered (``user_preferences.extra``).
PICKS_KEY = "light_example_picks"
#: Preview links on the card; the App asks again (``/api/light/examples/{id}/preview``) when one expired.
PREVIEW_SECONDS = 12 * 3600
PREVIEW_WIDTH = 720


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _clean(text: Any, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


def _tags(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    seen: list[str] = []
    for item in value:
        tag = _clean(item, 20)
        if tag and tag not in seen:
            seen.append(tag)
    return seen[:12]


def public(row) -> dict:
    """The console's view of one example."""
    return {
        "id": row.id, "entry_id": row.entry_id, "industry": row.industry, "shop_type": row.shop_type,
        "subtype": row.subtype, "scene": row.scene,
        "scene_label": taxonomy().scene_label(row.entry_id, row.scene) if row.scene else "",
        "tags": list(row.tags or []), "rating": row.rating, "status": row.status,
        "title": row.title, "notes": row.notes or "", "recipe": row.recipe or "", "look": row.look or "",
        "inputs": list(row.inputs or []), "outputs": list(row.outputs or []), "source": dict(row.source or {}),
        "created_by": row.created_by, "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
        "use_count": row.use_count or 0, "pick_count": row.pick_count or 0,
        "industry_label": taxonomy().label(row.industry),
        "shop_type_label": taxonomy().label(row.industry, row.shop_type),
    }


# --- the shop ---------------------------------------------------------------

async def shop_context(user_id: str | None) -> dict:
    """industry / shop_type / shop_name / headline of this owner, from the first-video form and the profile."""
    empty = {"industry": None, "shop_type": None, "shop_name": "", "headline": ""}
    if not user_id:
        return empty
    try:
        from db.repository.preference_repo import PgPreferenceRepo
        row = await PgPreferenceRepo().get(user_id)
    except Exception as exc:
        log.warning("light examples: shop context unread user=%s error=%s", user_id, type(exc).__name__)
        return empty
    extra = (row or {}).get("extra") or {}
    record = extra.get("first_video") if isinstance(extra, dict) else None
    record = record if isinstance(record, dict) else {}
    tax = taxonomy()
    industry = record.get("industry") if record.get("industry") in tax.industry_ids() else None
    business = ""
    try:
        from assistant import profile
        business = profile.from_extra(extra).business
    except Exception:
        business = ""
    if industry is None and business in tax.industry_ids():
        industry = business
    shop_name = _clean(record.get("shop_name"), 40)
    headline = _clean(record.get("headline"), 40)
    shop_type = tax.guess_shop_type(industry, shop_name, headline, business if business not in tax.industry_ids() else "")
    return {"industry": industry, "shop_type": shop_type, "shop_name": shop_name, "headline": headline}


# --- matching ---------------------------------------------------------------

async def _active(entry_id: str) -> list:
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.light_example import LightExample
    async with get_db_session() as db:
        return list((await db.scalars(select(LightExample).where(
            LightExample.entry_id == entry_id, LightExample.status == "active"))).all())


def _fit(row, industry: str | None, shop_type: str | None) -> int:
    """0 same industry and shop type, 1 same industry, 2 the generic pool, 3 anything else."""
    if industry and row.industry == industry:
        return 0 if shop_type and row.shop_type == shop_type else 1
    return 2 if row.industry == "other" else 3


async def candidates(entry_id: str, scene: str | None, industry: str | None, shop_type: str | None,
                     *, limit: int = 3, first: str | None = None) -> list:
    """What the example card shows: up to ``limit`` active examples with a finished piece to look at.

    Unlike ``pick`` this fills up: the same scene first, then how well the shop
    fits (shop type → industry → generic → any), then rating, then the newest;
    examples of other scenes make up the number when the scene has too few.
    ``first`` (the owner's last pick for this entry and scene) leads when it is
    still among them.
    """
    rows = [row for row in await _active(entry_id) if _shown_output(row) is not None]
    rows.sort(key=lambda r: (0 if scene and r.scene == scene else 1, _fit(r, industry, shop_type),
                             -(r.rating or 0), -((r.created_at or _now()).timestamp()), r.id))
    last = next((row for row in rows if row.id == first), None) if first else None
    if last is not None:
        rows = [last] + [row for row in rows if row.id != last.id]
    return rows[:limit]


async def pick(entry_id: str, industry: str | None, shop_type: str | None, tags: Iterable[str] | None = None,
               limit: int = 3, scene: str | None = None) -> list:
    """Active examples of ``entry_id`` for this shop, best first, falling back level by level.

    Levels: same industry and shop type → same industry → the generic pool
    (industry ``other``) → anything active for the entry. Within a level, the
    conversation's ``scene`` first when it is known, then more shared tags,
    then rating, then the newest.
    """
    wanted = set(_tags(list(tags or [])))
    rows = await _active(entry_id)
    if not rows:
        return []
    levels = []
    if industry and shop_type:
        levels.append(lambda r: r.industry == industry and r.shop_type == shop_type)
    if industry:
        levels.append(lambda r: r.industry == industry)
    levels.append(lambda r: r.industry == "other")
    levels.append(lambda r: True)
    chosen: list = []
    seen: set[str] = set()
    for level in levels:
        pool = [r for r in rows if r.id not in seen and level(r)]
        pool.sort(key=lambda r: (0 if scene and r.scene == scene else 1,
                                 -len(wanted & set(r.tags or [])), -(r.rating or 0),
                                 -((r.created_at or _now()).timestamp()), r.id))
        for row in pool:
            if len(chosen) >= limit:
                break
            chosen.append(row)
            seen.add(row.id)
        if chosen:
            break
    return chosen[:limit]


def _signed(output: dict) -> str | None:
    key = output.get("oss_key")
    if not key:
        return None
    from light.media import sign_media
    return sign_media("oss:" + str(key))


def _shown_output(row) -> dict | None:
    """The finished image or clip an owner can look at; None when the example has none."""
    for output in row.outputs or []:
        if isinstance(output, dict) and output.get("oss_key") and str(output.get("mime") or "").startswith(OUTPUT_MIMES):
            return output
    return None


def preview(row) -> dict | None:
    """``{example_id, title, mime, media_url, poster_url}`` for the card; None without a piece or without OSS.

    ``poster_url`` is a still the bucket renders on the signed GET itself: the
    picture scaled down, or the clip's frame at one second.
    """
    output = _shown_output(row)
    if output is None:
        return None
    key, mime = str(output["oss_key"]), str(output.get("mime") or "")
    process = (f"video/snapshot,t_1000,f_jpg,w_{PREVIEW_WIDTH},m_fast,ar_auto" if mime.startswith("video/")
               else f"image/resize,w_{PREVIEW_WIDTH}/format,jpg/quality,q_85")
    try:
        from core.oss import get_oss
        oss = get_oss()
        media = oss.presign_get(key, PREVIEW_SECONDS)
        poster = oss._presign("GET", key, PREVIEW_SECONDS, subresource={"x-oss-process": process})
    except Exception as exc:
        log.warning("light examples: preview not signed example=%s error=%s", row.id, type(exc).__name__)
        return None
    return {"example_id": row.id, "title": row.title, "mime": mime, "media_url": media, "poster_url": poster}


async def get_active(example_id: str | None):
    """One active example by id; None for the declined marker, an unknown id or a disabled one."""
    if not example_id or example_id == DECLINED:
        return None
    from db.base import get_db_session
    from db.models.light_example import LightExample
    async with get_db_session() as db:
        row = await db.get(LightExample, example_id)
    return row if row is not None and row.status == "active" else None


def last_pick(extra: dict | None, entry_id: str, scene: str | None) -> str | None:
    """The example this owner chose last time for the same entry and scene."""
    picks = (extra or {}).get(PICKS_KEY)
    by_entry = picks.get(entry_id) if isinstance(picks, dict) else None
    value = by_entry.get(scene or "_") if isinstance(by_entry, dict) else None
    return value if isinstance(value, str) and value else None


def with_pick(extra: dict | None, entry_id: str, scene: str | None, example_id: str) -> dict:
    """``extra`` with this pick remembered; the other entries and scenes keep theirs."""
    extra = dict(extra or {})
    picks = dict(extra.get(PICKS_KEY) or {}) if isinstance(extra.get(PICKS_KEY), dict) else {}
    picks[entry_id] = {**(picks.get(entry_id) if isinstance(picks.get(entry_id), dict) else {}), scene or "_": example_id}
    extra[PICKS_KEY] = picks
    return extra


def render(rows: list, ctx: dict | None = None, *, chosen: bool = False) -> str:
    """The 参考样例 section: a summary, the recipe as it was, the output links, then the rules.

    ``chosen``: the one row is what the owner picked on the example card."""
    if not rows:
        return ""
    tax = taxonomy()
    lines = ["参考样例（老板在样例卡上亲自选的这一条，这次就照它的结构、节奏和风格做，不再参考别的）："
             if chosen else "参考样例（同行业同类门店做过、团队认可的产出，照它们的结构和风格做）："]
    for index, row in enumerate(rows, 1):
        head = f"{index}. 「{row.title}」（{tax.label(row.industry, row.shop_type)}，评分 {row.rating}/5）"
        if row.notes:
            head += f"：{_clean(row.notes, NOTES_LIMIT)}"
        lines.append(head)
        look = (row.look or "").strip() if chosen else ""
        if look:
            # The look is what the owner chose; the old prompt names another shop's dishes and prices.
            lines.append("   样子说明（出图提示词的 Style 和 Layout 两行照这个写，配色、版式、图片用法、装饰都跟它走）：")
            lines.extend("   " + line for line in look.splitlines())
        elif row.recipe:
            lines.append("   当时的方案卡/提示词：" + _clean(row.recipe, RECIPE_LIMIT))
        links = []
        for output in row.outputs or []:
            if not isinstance(output, dict):
                continue
            url = _signed(output)
            name = output.get("name") or output.get("asset_id") or ""
            links.append(f"{name}（{url}）" if url else str(name))
        if links:
            lines.append("   产出：" + "；".join(links))
    lines.append("规则：" + " ".join(f"{n}) {rule}" for n, rule in enumerate(RULES, 1)))
    return "\n".join(lines)


async def _count(session_id: str, rows: list) -> None:
    fresh = [row.id for row in rows if (session_id, row.id) not in _COUNTED]
    if not fresh:
        return
    for example_id in fresh:
        _COUNTED[(session_id, example_id)] = None
    while len(_COUNTED) > _COUNTED_LIMIT:
        _COUNTED.popitem(last=False)
    try:
        from sqlalchemy import update
        from db.base import get_db_session
        from db.models.light_example import LightExample
        async with get_db_session() as db:
            await db.execute(update(LightExample).where(LightExample.id.in_(fresh))
                             .values(use_count=LightExample.use_count + 1))
            await db.commit()
    except Exception as exc:
        log.warning("light examples: use not counted session=%s error=%s", session_id, type(exc).__name__)


async def examples_fragment(user_id: str | None, session) -> str:
    """The 参考样例 section for a guided session, or "" when there is nothing to show."""
    entry_id = getattr(session, "light_entry", None) if session is not None else None
    if not entry_id or entry_id not in taxonomy().entry_ids():
        return ""
    picked = getattr(session, "light_example_id", None)
    if picked == DECLINED:  # the owner looked at the card and wanted none of them
        return ""
    try:
        ctx = await shop_context(user_id)
        chosen = await get_active(picked)
        rows = [chosen] if chosen is not None else await pick(
            entry_id, ctx["industry"], ctx["shop_type"], scene=getattr(session, "light_scene", None))
        if not rows:
            return ""
        await _count(getattr(session, "id", "") or "", rows)
        return render(rows, ctx, chosen=chosen is not None)
    except Exception as exc:  # the library never breaks a chat
        log.warning("light examples: fragment skipped session=%s error=%s", getattr(session, "id", None), type(exc).__name__)
        return ""


async def examples_hint(entry_ids: Iterable[str], industry: str | None = None) -> str:
    """One short line for the assistant: how many examples the library holds for these entries."""
    from sqlalchemy import func, select
    from db.base import get_db_session
    from db.models.light_example import LightExample
    wanted = [e for e in dict.fromkeys(entry_ids) if e in taxonomy().entry_ids()]
    if not wanted:
        return ""
    stmt = select(LightExample.entry_id, func.count()).where(
        LightExample.entry_id.in_(wanted), LightExample.status == "active")
    if industry:
        stmt = stmt.where(LightExample.industry == industry)
    async with get_db_session() as db:
        counts = dict((await db.execute(stmt.group_by(LightExample.entry_id))).all())
    tax = taxonomy()
    labels = {entry.id: entry.label for entry in tax.entries}
    bits = [f"{labels.get(e, e)} {counts[e]} 条" for e in wanted if counts.get(e)]
    if not bits:
        return ""
    scope = "同行业" if industry else ""
    return f"这类活有{scope}样例可参考（{'，'.join(bits)}），交给专题页做时会自动照着做。"


# --- reading one session back ------------------------------------------------

def _asset_ref(row) -> dict:
    return {"asset_id": row.id, "oss_key": row.oss_key, "name": row.name, "mime": row.mime}


async def resolve_assets(refs: Iterable[Any]) -> list[dict]:
    """Full ``{asset_id, oss_key, name, mime}`` for refs that may carry only an asset id; unknown ids are dropped."""
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    ids: list[str] = []
    for ref in refs:
        asset_id = ref.get("asset_id") if isinstance(ref, dict) else ref
        asset_id = _clean(asset_id, 64)
        if asset_id and asset_id not in ids:
            ids.append(asset_id)
    if not ids:
        return []
    async with get_db_session() as db:
        rows = {row.id: row for row in (await db.scalars(select(FileAsset).where(
            FileAsset.id.in_(ids), FileAsset.is_deleted.is_(False)))).all()}
    return [_asset_ref(rows[i]) for i in ids if i in rows]


def _recipe_from(questions: list[dict], prompts: list[str]) -> str:
    cards = [str(q.get("question") or "") for q in questions
             if isinstance(q, dict) and isinstance(q.get("detail"), dict) and (q["detail"].get("light") or {}).get("step_id")]
    parts = []
    if cards:
        parts.append("方案卡：\n" + max(cards, key=len).strip())
    if prompts:
        parts.append("生成提示词：\n" + "\n---\n".join(p.strip() for p in prompts))
    return "\n\n".join(parts)


async def prefill(session_id: str, asset_id: str | None = None) -> dict | None:
    """Everything a new example can take from one finished session; None when the session is unknown.

    Inputs are the owner's attachments, outputs the result/final image and
    video parts (only ``asset_id`` when given), the recipe is the review card
    plus every generation prompt, and industry / shop name come from the
    first-video form. The team fills the rest.
    """
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.part import Part
    from db.models.question import QuestionCheckpoint
    from db.models.session import Session
    async with get_db_session() as db:
        session = await db.get(Session, session_id)
        if session is None:
            return None
        parts = (await db.scalars(select(Part).where(Part.session_id == session_id)
                                  .order_by(Part.created_at, Part.id))).all()
        checkpoints = (await db.scalars(select(QuestionCheckpoint).where(QuestionCheckpoint.session_id == session_id)
                                        .order_by(QuestionCheckpoint.created_at))).all()
    inputs, outputs, prompts, job_id = [], [], [], None
    for part in parts:
        data = part.data if isinstance(part.data, dict) else {}
        if data.get("type") == "file" and data.get("asset_id"):
            relation = data.get("relation") or {}
            mime = str(data.get("mime_type") or "")
            ref = {"asset_id": data["asset_id"], "oss_key": data.get("oss_key"),
                   "name": str(data.get("path") or "").rsplit("/", 1)[-1], "mime": mime}
            if relation.get("role") == "input":
                inputs.append(ref)
            elif relation.get("role") in OUTPUT_ROLES and mime.startswith(OUTPUT_MIMES):
                outputs.append(ref)
        elif data.get("type") == "tool" and data.get("tool") in PROMPT_TOOLS:
            arguments = data.get("input") or {}
            prompt = arguments.get("prompt") if isinstance(arguments, dict) else None
            if prompt and str(prompt).strip() not in prompts:
                prompts.append(str(prompt))
            output = data.get("metadata") or {}
            job_id = job_id or (output.get("job_id") if isinstance(output, dict) else None)
    if asset_id:
        outputs = [o for o in outputs if o["asset_id"] == asset_id] or await resolve_assets([asset_id])
    questions = [q for row in checkpoints for q in (row.questions or [])]
    # The bucket facts come from file_assets, not from what the part remembered.
    inputs = await resolve_assets(inputs)
    outputs = await resolve_assets(outputs)
    ctx = await shop_context(session.user_id)
    entry_id = session.light_entry if session.light_entry in taxonomy().entry_ids() else None
    return {
        "entry_id": entry_id,
        "industry": ctx["industry"] or "other",
        "shop_type": ctx["shop_type"] or "other",
        "subtype": None,
        "scene": session.light_scene if session.light_scene in taxonomy().scene_ids(entry_id) else None,
        "title": _clean(session.title, 120) or "",
        "recipe": _recipe_from(questions, prompts),
        "inputs": inputs,
        "outputs": outputs,
        "source": {"session_id": session.id, "job_id": job_id, "user_id": session.user_id,
                   "workspace_id": session.workspace_id, "shop_name": ctx["shop_name"], "headline": ctx["headline"]},
    }
