"""Whole-plan estimate: registered before the shots + price card, enforced when it is confirmed.

Until now the only figure for a whole production lived in the prose of
card 2 (the skill summed one ``video_generate action=estimate`` per shot),
the balance precheck before a paid step only asked for *more than zero*
credits, and ``max_credits`` was checked one step at a time. A plan of five
shots priced at 233 credits could therefore be confirmed and then bought
shot by shot across several prompts with a 60-credit cap each
(2026-10-15, Gaode crab-voucher session).

Three pieces close that gap:

- :func:`register` — the skill's ``video_generate action=quote_plan`` hands
  over the planned items (shots, transcriptions, compose). They are priced
  here with the same quotes the steps will be charged at (the partner
  tariff for ``/v1`` sessions, the platform rate card otherwise); a figure
  the model claims is never trusted. The result is stored on the session
  as ``plan_quote`` and compared with the request's cap and the balance.
- :func:`gate_answer` — when the card the skill marked ``kind="plan_confirm"``
  receives its affirmative answer (the option carrying ``estimated_credits``),
  the balance must cover the whole plan in ``enforce`` mode and the plan must
  fit under ``max_credits``; otherwise the answer is refused before anything
  is scheduled. The per-step guards stay in place behind it.
- :func:`view` — ``GET /v1/sessions/{id}`` exposes the plan as ``estimate``.

The plan covers planned work only: retakes after card 3 are quoted on their
own before they are resubmitted, as before.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

KINDS = ("generation", "transcribe", "compose")
#: ``question.kind`` the skill puts on card 2 so the answer is gated here.
PLAN_CONFIRM_KIND = "plan_confirm"
INSUFFICIENT_CODE = "INSUFFICIENT_CREDITS_FOR_PLAN"
MAX_ITEMS = 64
#: Compose is priced by the output's short side; a resolution tier maps to it.
_SHORT_SIDE = {"480p": 480, "512p": 512, "720p": 720, "768p": 768, "1080p": 1080, "2k": 1440, "4k": 2160}


class PlanItem(BaseModel):
    """One planned paid step. ``claimed_credits`` is what the caller believes it
    costs; it is reported back when it differs and never used for pricing."""

    kind: Literal["generation", "transcribe", "compose"]
    shot: int | None = Field(default=None, ge=1, le=200)
    model: str | None = Field(default=None, max_length=160)
    resolution: str | None = Field(default=None, max_length=8)
    seconds: float = Field(gt=0, le=3600)
    claimed_credits: str | None = Field(default=None, max_length=24)


class PlanUnpriced(ValueError):
    """At least one item has no verified price; nothing was registered."""

    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("plan cannot be priced: " + "; ".join(reasons))


class PlanInsufficientCredits(ValueError):
    """The workspace balance does not cover the registered plan (enforce mode)."""

    code = INSUFFICIENT_CODE

    def __init__(self, *, required: Decimal, available: Decimal):
        self.required, self.available = required, available
        self.shortfall = max(Decimal(0), required - available)
        super().__init__(
            f"积分不足以完成本计划：预估需要 {_text(required)} 积分，当前可用 {_text(available)}，"
            f"还差 {_text(self.shortfall)}；请先充值或缩减镜头数/时长后再确认")


def _text(value: Decimal | None) -> str:
    if value is None:
        return "0"
    text = format(value.normalize(), "f")
    return text if text != "-0" else "0"


def _decimal(value: Any) -> Decimal | None:
    try:
        amount = Decimal(str(value)) if value is not None else None
    except InvalidOperation:
        return None
    return amount if amount is not None and amount.is_finite() else None


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat()


# ─── Pricing ───

async def _session_row(session_id: str):
    from db.base import get_db_session
    from db.models.session import Session

    async with get_db_session() as db:
        return await db.get(Session, session_id)


def _default_resolution(model: str, config) -> str:
    from tool.video_providers import declared_model

    declared = declared_model(model, config)
    allowed = list(getattr(declared, "resolutions", None) or [])
    resolution = allowed[0] if len(allowed) == 1 else config.video_generation.default_resolution
    if allowed and resolution not in allowed:
        resolution = allowed[0]
    return resolution


async def price_items(session_id: str, items: list[PlanItem]) -> list[dict]:
    """Server-priced copies of ``items``; raises :class:`PlanUnpriced` if any has no price."""
    from billing.media import quote_transcription
    from billing.partner import quote_compose_for_session, quote_generation_for_session
    from core.config import get_config

    if not items:
        raise PlanUnpriced(["no items"])
    if len(items) > MAX_ITEMS:
        raise PlanUnpriced([f"at most {MAX_ITEMS} items"])
    config = get_config()
    session = await _session_row(session_id)
    session_model = getattr(session, "video_model", None) or ""
    session_resolution = getattr(session, "video_resolution", None) or ""
    priced: list[dict] = []
    reasons: list[str] = []
    for index, item in enumerate(items, start=1):
        label = f"item {index}" + (f" (shot {item.shot})" if item.shot else "") + f" {item.kind}"
        if item.kind == "generation":
            model = (item.model or "").strip() or session_model or config.video_generation.model
            resolution = (item.resolution or "").strip() or session_resolution or _default_resolution(model, config)
            price = await quote_generation_for_session(session_id, model, resolution, item.seconds)
            unit = {"per_second": price.snapshot.get("per_second"), "seconds_billed": price.minutes_billed}
        elif item.kind == "transcribe":
            model = (item.model or "").strip() or config.video_transcription.model
            resolution = None
            price = quote_transcription(model, item.seconds)
            unit = {"per_minute": price.snapshot.get("per_minute"), "minutes_billed": price.minutes_billed}
        else:
            model = None
            resolution = (item.resolution or "").strip() or session_resolution or config.video_generation.default_resolution
            short = _SHORT_SIDE.get(resolution)
            if short is None:
                reasons.append(f"{label}: unknown compose resolution {resolution!r}")
                continue
            price = await quote_compose_for_session(session_id, short, round(short * 16 / 9), item.seconds)
            unit = {"per_second": price.snapshot.get("per_second"), "seconds_billed": price.minutes_billed}
        if price.credits is None:
            reasons.append(f"{label}: {price.snapshot.get('reason') or 'no verified price'}")
            continue
        row = {"kind": item.kind, "shot": item.shot, "model": model, "resolution": resolution,
               "seconds": item.seconds, "credits": _text(price.credits), **{k: v for k, v in unit.items() if v is not None}}
        claimed = _decimal(item.claimed_credits)
        if claimed is not None and claimed != price.credits:
            row["claimed_credits"] = _text(claimed)
        priced.append(row)
    if reasons:
        raise PlanUnpriced(reasons)
    return priced


def total_of(items: list[dict]) -> Decimal:
    return sum((Decimal(row["credits"]) for row in items), Decimal(0))


# ─── Storage ───

async def register(session_id: str, items: list[PlanItem]) -> dict:
    """Price ``items``, store them as the session's plan and return :func:`assess` of it."""
    from sqlalchemy import update

    from db.base import get_db_session
    from db.models.session import Session

    priced = await price_items(session_id, items)
    plan = {"total": _text(total_of(priced)), "items": priced, "created_at": _now_iso(), "confirmed_at": None}
    async with get_db_session() as db:
        await db.execute(update(Session).where(Session.id == session_id).values({Session.plan_quote: plan}))
    return await assess(session_id, plan)


async def load(session_id: str) -> dict | None:
    row = await _session_row(session_id)
    return plan_of(row)


def plan_of(row: Any) -> dict | None:
    plan = getattr(row, "plan_quote", None) if row is not None else None
    return plan if isinstance(plan, dict) and plan.get("total") is not None and isinstance(plan.get("items"), list) else None


async def _balance(workspace_id: str) -> Decimal:
    """Current balance with the period allowance credited, as the paid steps will see it."""
    from billing.service import lock_balance
    from billing.subscriptions import ensure_period_allowance
    from db.base import get_db_session

    async with get_db_session() as db:
        account = await lock_balance(db, workspace_id)
        await ensure_period_allowance(db, account, datetime.now(timezone.utc))
        return Decimal(account.balance)


async def assess(session_id: str, plan: dict | None = None) -> dict | None:
    """The plan beside the cap and the balance: what a caller needs before raising card 2.

    ``exceeds_cap`` compares the plan plus what this request has already
    spent (settled + in flight) with ``max_credits``; ``balance_sufficient``
    is checked only in ``enforce`` mode, where the steps would actually be
    charged — elsewhere ``available`` is null and nothing is refused.
    """
    from billing import credit_cap
    from billing.service import billing_mode

    row = await _session_row(session_id)
    plan = plan if plan is not None else plan_of(row)
    if plan is None or row is None:
        return None
    total = Decimal(plan["total"])
    cap = getattr(row, "credit_cap", None)
    exceeds_cap = False
    cap_block = None
    if isinstance(cap, dict) and cap.get("max"):
        limit = Decimal(cap["max"])
        settled, in_flight = await credit_cap.spent_breakdown(row.id, datetime.fromisoformat(cap["since"]))
        spent = settled + in_flight
        exceeds_cap = spent + total > limit
        cap_block = {"max_credits": _text(limit), "spent": _text(spent), "remaining": _text(max(Decimal(0), limit - spent))}
    available = None
    balance_sufficient = True
    shortfall = Decimal(0)
    if billing_mode() == "enforce":
        available = await _balance(row.workspace_id)
        balance_sufficient = available >= total
        shortfall = max(Decimal(0), total - available)
    return {
        "total": _text(total), "items": list(plan["items"]),
        "exceeds_cap": exceeds_cap, "credit_cap": cap_block,
        "balance_sufficient": balance_sufficient,
        "available": _text(available) if available is not None else None,
        "shortfall": _text(shortfall),
        "created_at": plan.get("created_at"), "confirmed_at": plan.get("confirmed_at"),
    }


async def view(row: Any) -> dict | None:
    """``estimate`` block of the public session view, or None when no plan is registered."""
    from api.v1.public import iso_z

    plan = plan_of(row)
    if plan is None:
        return None
    full = await assess(row.id, plan)
    if full is None:
        return None
    return {key: full[key] for key in ("total", "items", "exceeds_cap", "balance_sufficient", "shortfall")} | {
        "created_at": iso_z(full["created_at"]), "confirmed_at": iso_z(full["confirmed_at"])}


# ─── The gate on card 2 ───

def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def affirmative_plan_answer(questions: list, answers: list | None) -> bool:
    """True when ``answers`` pick the plan-confirming option of a ``plan_confirm`` question.

    The confirming option is the one that carries ``estimated_credits`` (the
    skill puts the plan total on 「可以」). Free text and the other options
    (edit the shots, change the model) commit to nothing and pass untouched.
    """
    if not isinstance(answers, list):
        return False
    for index, question in enumerate(questions or []):
        if _field(question, "kind") != PLAN_CONFIRM_KIND:
            continue
        confirming = {
            str(_field(option, "label") or "").strip()
            for option in (_field(question, "options") or [])
            if _field(option, "estimated_credits") is not None
        }
        picked = answers[index] if index < len(answers) and isinstance(answers[index], list) else []
        if any(str(label).strip() in confirming for label in picked if isinstance(label, str)):
            return True
    return False


async def gate_answer(session_id: str, questions: list, answers: list | None) -> dict | None:
    """Refuse the confirming answer of card 2 when the plan cannot be paid for.

    Raises :class:`PlanInsufficientCredits` (enforce mode, balance below the
    plan) or :class:`billing.credit_cap.CreditCapExceeded` (plan plus what
    this request already spent over ``max_credits``; the refusal is recorded
    as ``credit_cap.hit`` with ``step="plan"``). Returns the plan when the
    answer may proceed, None when there was nothing to check.
    """
    if not session_id or not affirmative_plan_answer(questions, answers):
        return None
    from billing import credit_cap
    from billing.service import billing_mode

    row = await _session_row(session_id)
    plan = plan_of(row)
    if plan is None:
        log.warning("plan gate: session %s confirmed a plan_confirm card without a registered plan", session_id)
        return None
    total = Decimal(plan["total"])
    if billing_mode() == "enforce":
        available = await _balance(row.workspace_id)
        if available < total:
            raise PlanInsufficientCredits(required=total, available=available)
    await credit_cap.guard(session_id, step="plan", credits=total, note="plan confirmation")
    return plan


def mark_confirmed(session_row: Any, questions: list, answers: list | None) -> None:
    """Stamp ``confirmed_at`` on the plan when its card is accepted; caller owns the transaction."""
    plan = plan_of(session_row)
    if plan is None or not affirmative_plan_answer(questions, answers):
        return
    session_row.plan_quote = {**plan, "confirmed_at": _now_iso()}
