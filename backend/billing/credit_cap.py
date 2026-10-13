"""Per-request credit cap for ``/v1`` callers: refuse a paid step before it is bought.

A partner sends ``max_credits`` with a prompt. The cap is written on the
session together with the moment it was set; every paid media step the turn
triggers (shot generation, cloud compose, transcription, image generation)
is checked against *cap − what this request has already spent*, using the
same quote the step would be charged at. Text calls are not counted: they
are cents, and the partner contract prices the cap in generated output.

A refusal records ``hit`` on the session so ``GET /v1/sessions/{id}`` can
show the machine-readable reason; the tool result tells the model to stop
and explain. Nothing is submitted upstream, so nothing is charged.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

MEDIA_KINDS = ("video_generate", "video_compose", "video_transcribe", "image_gen")
MAX_CAP = Decimal("1000000")
CODE = "CREDIT_CAP_EXCEEDED"


class CreditCapExceeded(Exception):
    """Raised instead of submitting a paid step that would overrun the cap."""

    code = CODE

    def __init__(self, *, cap: Decimal, spent: Decimal, expected: Decimal, step: str, note: str = ""):
        self.cap, self.spent, self.expected, self.step, self.note = cap, spent, expected, step, note
        super().__init__(
            f"credit cap exceeded: this request may spend at most {_text(cap)} credits, "
            f"{_text(spent)} already spent and the next {step} step is estimated at {_text(expected)}"
            + (f" ({note})" if note else "")
            + ". Nothing was submitted. Stop and tell the user; a new request with a higher max_credits may continue."
        )


def _text(value: Decimal | None) -> str:
    if value is None:
        return "0"
    return format(value.normalize(), "f") if value else "0"


def parse_max_credits(value: str | int | float | None) -> Decimal | None:
    """Validate a ``max_credits`` body value; None when absent."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("max_credits must be a decimal string")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("max_credits must be a decimal string") from exc
    if not amount.is_finite() or amount <= 0 or amount > MAX_CAP:
        raise ValueError(f"max_credits must be between 0 (exclusive) and {_text(MAX_CAP)}")
    if amount.as_tuple().exponent < -6:
        raise ValueError("max_credits allows at most 6 decimal places")
    return amount


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def set_cap(session_id: str, max_credits: Decimal | None) -> None:
    """Bind (or clear) the cap for the request that is about to run."""
    from sqlalchemy import update

    from db.base import get_db_session
    from db.models.session import Session

    value = None
    if max_credits is not None:
        value = {"max": _text(max_credits), "since": _now().isoformat(), "hit": None}
    async with get_db_session() as db:
        await db.execute(update(Session).where(Session.id == session_id).values({Session.credit_cap: value}))


async def _load(session_id: str) -> dict | None:
    from db.base import get_db_session
    from db.models.session import Session

    async with get_db_session() as db:
        row = await db.get(Session, session_id)
    cap = getattr(row, "credit_cap", None) if row is not None else None
    return cap if isinstance(cap, dict) and cap.get("max") else None


async def spent_since(session_id: str, since: datetime) -> Decimal:
    """Credits the media steps of this request have already settled."""
    from sqlalchemy import func, select

    from db.base import get_db_session
    from db.models.billing import UsageEvent

    async with get_db_session() as db:
        total = await db.scalar(select(func.sum(UsageEvent.credits)).where(
            UsageEvent.session_id == session_id, UsageEvent.created_at >= since,
            UsageEvent.kind.in_(MEDIA_KINDS), UsageEvent.status.in_(("charged", "shadow", "pending"))))
    return Decimal(str(total)) if total is not None else Decimal(0)


async def guard(session_id: str | None, *, step: str, credits: Decimal | None, note: str = "") -> None:
    """Refuse ``step`` when its estimate would push this request past its cap."""
    if not session_id:
        return
    cap = await _load(session_id)
    if cap is None:
        return
    limit = Decimal(cap["max"])
    since = datetime.fromisoformat(cap["since"])
    spent = await spent_since(session_id, since)
    expected = Decimal(credits) if credits is not None else Decimal(0)
    if spent + expected <= limit:
        return
    hit = {"code": CODE, "step": step, "expected": _text(expected), "spent": _text(spent),
           "max": _text(limit), "at": _now().isoformat(), "note": note}
    from sqlalchemy import update

    from db.base import get_db_session
    from db.models.session import Session

    async with get_db_session() as db:
        await db.execute(update(Session).where(Session.id == session_id).values({Session.credit_cap: {**cap, "hit": hit}}))
    raise CreditCapExceeded(cap=limit, spent=spent, expected=expected, step=step, note=note)


async def status(row: Any) -> dict | None:
    """``credit_cap`` block of the public session view, or None when no cap is set."""
    cap = getattr(row, "credit_cap", None)
    if not isinstance(cap, dict) or not cap.get("max"):
        return None
    limit = Decimal(cap["max"])
    spent = await spent_since(row.id, datetime.fromisoformat(cap["since"]))
    hit = cap.get("hit")
    return {
        "max_credits": _text(limit), "spent": _text(spent), "remaining": _text(max(Decimal(0), limit - spent)),
        "since": cap["since"] + "Z" if not cap["since"].endswith("Z") else cap["since"],
        "exceeded": {"code": hit["code"], "step": hit["step"], "expected": hit["expected"], "spent": hit["spent"],
                     "at": hit["at"] + "Z"} if hit else None,
    }


async def estimate_lines(session_id: str | None) -> list[str]:
    """Lines for the estimate tools so the model can plan inside the cap."""
    if not session_id:
        return []
    cap = await _load(session_id)
    if cap is None:
        return []
    limit = Decimal(cap["max"])
    spent = await spent_since(session_id, datetime.fromisoformat(cap["since"]))
    return [f"credit_cap_max={_text(limit)}", f"credit_cap_spent={_text(spent)}",
            f"credit_cap_remaining={_text(max(Decimal(0), limit - spent))}",
            "credit_cap_note=本次请求的付费步骤（镜头生成、合成）预估总额不得超过 credit_cap_max，超过会被拒绝；请在报价时据此裁剪镜头数或时长"]
