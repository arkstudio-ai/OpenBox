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

"Spent" is settled plus in flight. Shot generation and cloud compose are
charged when the job finishes (``billing/media.settle_generation`` and
``settle_compose``), minutes after submission, while the skill submits every
shot of a round at once. Counting only ``usage_events`` would let each
concurrent submit see the same ``spent`` and admit a batch whose sum is far
over the cap. So ``video_jobs`` rows of this request that have not failed
and have no settlement yet count at the quote saved on them at submission
(``request_data.billing_quote.credits``). The guard is further serialised
per session with :func:`reserve`, which callers hold until the job row is
written, so two submits of one session cannot both pass on the same view.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, AsyncIterator

log = logging.getLogger(__name__)

MEDIA_KINDS = ("video_generate", "video_compose", "video_transcribe", "image_gen")
#: ``video_jobs.kind`` values whose charge arrives only when the job completes.
IN_FLIGHT_JOB_KINDS = ("segment", "render")
#: Job states after which no charge will ever be settled.
FAILED_JOB_STATUSES = ("failed", "cancelled", "transfer_failed")
#: ``usage_events.idempotency_key`` prefix the settlement of each job kind uses.
SETTLE_KEY_PREFIX = {"segment": "generate:", "render": "compose:"}
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


def _quoted_credits(job: Any) -> Decimal:
    """Credits a job will settle at, from the quote saved on it at submission; 0 (and a warning) without one."""
    request = job.request_data if isinstance(job.request_data, dict) else {}
    quote = request.get("billing_quote") if isinstance(request, dict) else None
    raw = quote.get("credits") if isinstance(quote, dict) else None
    try:
        amount = Decimal(str(raw)) if raw is not None else None
    except InvalidOperation:
        amount = None
    if amount is None or not amount.is_finite() or amount < 0:
        log.warning("credit cap: job %s (%s, %s) has no usable billing_quote; counted as 0 in flight",
                    job.id, job.kind, job.status)
        return Decimal(0)
    return amount


async def spent_breakdown(session_id: str, since: datetime, *, exclude_job_id: str | None = None,
                          db: Any | None = None) -> tuple[Decimal, Decimal]:
    """``(settled, in_flight)`` credits of this request's media steps.

    ``settled`` is the sum of media ``usage_events`` since the cap was set.
    ``in_flight`` is the quoted price of every generation or compose job of
    the session created since then that has not failed and has no settlement
    row yet (matched on the settlement idempotency key, so a finished job is
    never counted twice). Jobs completed by reusing an identical earlier
    output cost nothing and are skipped. ``exclude_job_id`` leaves out the
    job the caller is about to admit, whose price it passes as ``expected``.
    """
    from sqlalchemy import func, select

    from db.base import get_db_session
    from db.models.billing import UsageEvent
    from db.models.video_job import VideoJob

    async def _compute(conn) -> tuple[Decimal, Decimal]:
        total = await conn.scalar(select(func.sum(UsageEvent.credits)).where(
            UsageEvent.session_id == session_id, UsageEvent.created_at >= since,
            UsageEvent.kind.in_(MEDIA_KINDS), UsageEvent.status.in_(("charged", "shadow", "pending"))))
        settled = Decimal(str(total)) if total is not None else Decimal(0)
        jobs = (await conn.execute(select(VideoJob).where(
            VideoJob.session_id == session_id, VideoJob.created_at >= since,
            VideoJob.kind.in_(IN_FLIGHT_JOB_KINDS), VideoJob.status.not_in(FAILED_JOB_STATUSES)))).scalars().all()
        jobs = [job for job in jobs if job.id != exclude_job_id]
        if not jobs:
            return settled, Decimal(0)
        keys = [SETTLE_KEY_PREFIX[job.kind] + job.id for job in jobs]
        settled_keys = set((await conn.execute(
            select(UsageEvent.idempotency_key).where(UsageEvent.idempotency_key.in_(keys)))).scalars().all())
        in_flight = Decimal(0)
        for job in jobs:
            if SETTLE_KEY_PREFIX[job.kind] + job.id in settled_keys:
                continue
            result = job.result_data if isinstance(job.result_data, dict) else {}
            if result.get("reuse"):
                continue
            in_flight += _quoted_credits(job)
        return settled, in_flight

    if db is not None:
        return await _compute(db)
    async with get_db_session() as conn:
        return await _compute(conn)


async def spent_since(session_id: str, since: datetime, *, exclude_job_id: str | None = None) -> Decimal:
    """Credits this request has settled plus the quoted price of its unsettled jobs."""
    settled, in_flight = await spent_breakdown(session_id, since, exclude_job_id=exclude_job_id)
    return settled + in_flight


_locks: dict[str, asyncio.Lock] = {}
_lock_users: dict[str, int] = {}


@asynccontextmanager
async def _session_lock(session_id: str) -> AsyncIterator[None]:
    """Serialise cap checks of one session within this process; idle locks are dropped."""
    lock = _locks.setdefault(session_id, asyncio.Lock())
    _lock_users[session_id] = _lock_users.get(session_id, 0) + 1
    try:
        async with lock:
            yield
    finally:
        _lock_users[session_id] -= 1
        if _lock_users[session_id] <= 0:
            _lock_users.pop(session_id, None)
            _locks.pop(session_id, None)


async def guard(session_id: str | None, *, step: str, credits: Decimal | None, note: str = "",
                exclude_job_id: str | None = None) -> None:
    """Refuse ``step`` when its estimate would push this request past its cap.

    Runs the read of the cap, the spend (settled + in flight) and the refusal
    record in one transaction holding the session row, so concurrent guards
    of one session across processes see each other's refusals in order.
    Prefer :func:`reserve`, which also holds the per-session lock until the
    caller has written the job row the next guard must see.
    """
    if not session_id:
        return
    from sqlalchemy import select, update

    from db.base import get_db_session
    from db.models.session import Session

    async with get_db_session() as db:
        row = await db.scalar(select(Session).where(Session.id == session_id).with_for_update())
        cap = getattr(row, "credit_cap", None) if row is not None else None
        if not isinstance(cap, dict) or not cap.get("max"):
            return
        limit = Decimal(cap["max"])
        since = datetime.fromisoformat(cap["since"])
        settled, in_flight = await spent_breakdown(session_id, since, exclude_job_id=exclude_job_id, db=db)
        spent = settled + in_flight
        expected = Decimal(credits) if credits is not None else Decimal(0)
        if spent + expected <= limit:
            return
        hit = {"code": CODE, "step": step, "expected": _text(expected), "spent": _text(spent),
               "settled": _text(settled), "in_flight": _text(in_flight),
               "max": _text(limit), "at": _now().isoformat(), "note": note}
        await db.execute(update(Session).where(Session.id == session_id).values({Session.credit_cap: {**cap, "hit": hit}}))
    raise CreditCapExceeded(cap=limit, spent=spent, expected=expected, step=step, note=note)


@asynccontextmanager
async def reserve(session_id: str | None, *, step: str, credits: Decimal | None, note: str = "",
                  exclude_job_id: str | None = None, already_admitted: bool = False) -> AsyncIterator[None]:
    """Guard ``step`` and keep the session's cap lock while the caller writes its job row.

    The body must create (or, for a job that already exists, price) the job
    the guard admitted, so the next ``reserve`` of this session counts it in
    flight. ``already_admitted`` skips the check for an idempotent re-submit
    whose job row exists and was admitted when it was created.
    """
    if not session_id:
        yield
        return
    async with _session_lock(session_id):
        if not already_admitted:
            await guard(session_id, step=step, credits=credits, note=note, exclude_job_id=exclude_job_id)
        yield


async def status(row: Any) -> dict | None:
    """``credit_cap`` block of the public session view, or None when no cap is set."""
    cap = getattr(row, "credit_cap", None)
    if not isinstance(cap, dict) or not cap.get("max"):
        return None
    limit = Decimal(cap["max"])
    settled, in_flight = await spent_breakdown(row.id, datetime.fromisoformat(cap["since"]))
    spent = settled + in_flight
    hit = cap.get("hit")
    return {
        "max_credits": _text(limit), "spent": _text(spent), "remaining": _text(max(Decimal(0), limit - spent)),
        "settled": _text(settled), "in_flight": _text(in_flight),
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
    settled, in_flight = await spent_breakdown(session_id, datetime.fromisoformat(cap["since"]))
    spent = settled + in_flight
    return [f"credit_cap_max={_text(limit)}", f"credit_cap_spent={_text(spent)}",
            f"credit_cap_settled={_text(settled)}", f"credit_cap_in_flight={_text(in_flight)}",
            f"credit_cap_remaining={_text(max(Decimal(0), limit - spent))}",
            "credit_cap_note=本次请求的付费步骤（镜头生成、合成）预估总额不得超过 credit_cap_max，超过会被拒绝；"
            "credit_cap_spent 已包含尚未完成的镜头/合成按报价计的在途金额；请在报价时据此裁剪镜头数或时长"]
