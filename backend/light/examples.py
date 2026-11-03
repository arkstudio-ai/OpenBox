"""The reference example library behind the Light entries (docs/LIGHT_MODE_IMPL_PLAN.md §11).

The content team approves work in the console; this module is the other
half: find the examples that fit this shop (entry → industry → shop type →
tags, falling back level by level), render them as a 参考样例 section of the
Light prompt fragment, and count each use. ``prefill`` reads one finished
session back into the fields of a new example so the team only adds the
shop type, tags, rating and notes.
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
        "subtype": row.subtype, "tags": list(row.tags or []), "rating": row.rating, "status": row.status,
        "title": row.title, "notes": row.notes or "", "recipe": row.recipe or "",
        "inputs": list(row.inputs or []), "outputs": list(row.outputs or []), "source": dict(row.source or {}),
        "created_by": row.created_by, "created_at": _iso(row.created_at), "updated_at": _iso(row.updated_at),
        "use_count": row.use_count or 0,
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

async def pick(entry_id: str, industry: str | None, shop_type: str | None, tags: Iterable[str] | None = None,
               limit: int = 3) -> list:
    """Active examples of ``entry_id`` for this shop, best first, falling back level by level.

    Levels: same industry and shop type → same industry → the generic pool
    (industry ``other``) → anything active for the entry. Within a level, more
    shared tags first, then rating, then the newest.
    """
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.light_example import LightExample
    wanted = set(_tags(list(tags or [])))
    async with get_db_session() as db:
        rows = list((await db.scalars(select(LightExample).where(
            LightExample.entry_id == entry_id, LightExample.status == "active"))).all())
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
        pool.sort(key=lambda r: (-len(wanted & set(r.tags or [])), -(r.rating or 0),
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


def render(rows: list, ctx: dict | None = None) -> str:
    """The 参考样例 section: a summary, the recipe as it was, the output links, then the rules."""
    if not rows:
        return ""
    tax = taxonomy()
    lines = ["参考样例（同行业同类门店做过、团队认可的产出，照它们的结构和风格做）："]
    for index, row in enumerate(rows, 1):
        head = f"{index}. 「{row.title}」（{tax.label(row.industry, row.shop_type)}，评分 {row.rating}/5）"
        if row.notes:
            head += f"：{_clean(row.notes, NOTES_LIMIT)}"
        lines.append(head)
        if row.recipe:
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
    try:
        ctx = await shop_context(user_id)
        rows = await pick(entry_id, ctx["industry"], ctx["shop_type"])
        if not rows:
            return ""
        await _count(getattr(session, "id", "") or "", rows)
        return render(rows, ctx)
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
        "title": _clean(session.title, 120) or "",
        "recipe": _recipe_from(questions, prompts),
        "inputs": inputs,
        "outputs": outputs,
        "source": {"session_id": session.id, "job_id": job_id, "user_id": session.user_id,
                   "workspace_id": session.workspace_id, "shop_name": ctx["shop_name"], "headline": ctx["headline"]},
    }
