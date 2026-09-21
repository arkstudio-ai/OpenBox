"""Team admission for paid effects; existing usage rows remain the billing truth.

Reservations are committed under the team row lock before provider dispatch.
An ambiguous effect keeps its entire reservation until a durable provider result
and all associated billing facts are available. Recovery only reads those facts;
it never resubmits or debits the billing ledger.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from sqlalchemy import select

from db.models.billing import UsageEvent
from db.models.external_effect import ExternalEffect
from db.models.video_job import VideoJob
from team.errors import TeamError
from team.journal import Actor, command, digest, snapshot, utcnow
from team.state import amount


@dataclass(frozen=True)
class Reservation:
    run_id: str
    actor: Actor
    id: str


def is_member() -> bool:
    from team.runtime_binding import current_binding
    binding = current_binding()
    return bool(binding and binding.run_id and binding.role == "member")


async def reserve(ctx, tool: str, price, *, external_kind: str, external_id: str,
                  billing_keys: list[str], expected_usage_count: int | None = None) -> Reservation | None:
    from billing.service import billing_mode
    from team.commands import require_open
    from team.runtime_binding import current_binding, assert_tool_current

    binding = current_binding()
    if binding is None or binding.run_id is None:
        return None
    await assert_tool_current(tool, ctx)
    if binding.role != "member" or not ctx.part_id or not ctx.run_fence:
        raise TeamError("AUTHORITY_REVOKED", "A paid operation requires an admitted member and a fenced tool call.")
    if billing_mode() == "off":
        raise TeamError("PERMISSION_REQUIRES_USER", "Paid team tools require the existing credit ledger to be enabled.")
    if price.credits is None or not price.snapshot.get("version") or price.snapshot.get("price_bound_verified") is not True:
        raise TeamError("PERMISSION_REQUIRES_USER", "This operation has no verified price upper bound. Choose a priced model and explicit output size or duration.")
    credits = amount(str(price.credits))
    if credits <= 0:
        raise TeamError("PERMISSION_REQUIRES_USER", "This adapter requires a positive verified price upper bound.")
    if external_kind not in {"external_effect", "video_job"} or not external_id:
        raise TeamError("INVALID_RESERVATION", "A paid reservation must identify its durable effect or job.")
    if any(not isinstance(key, str) or not key or len(key) > 200 for key in billing_keys):
        raise TeamError("INVALID_RESERVATION", "Every billing identity must be a bounded nonempty string.")
    billing_keys = list(dict.fromkeys(billing_keys))
    expected = len(billing_keys) if expected_usage_count is None else expected_usage_count
    if type(expected) is not int or not 1 <= expected <= 16 or len(billing_keys) > expected:
        raise TeamError("INVALID_RESERVATION", "A paid operation must declare one to sixteen usage meters and cover all known billing identities.")
    actor = Actor(binding.owner_user_id, binding.workspace_id, "member", binding.member_id, ctx.run_id, ctx.run_generation)
    identifier = digest({"run": binding.run_id, "member": binding.member_id, "part": ctx.part_id, "tool": tool})
    fields = {"id": identifier, "tool": tool, "amount": str(credits), "pricing": price.snapshot,
        "member_id": binding.member_id, "part_id": ctx.part_id, "driver_run_id": ctx.run_id,
        "external_kind": external_kind, "external_id": external_id,
        "billing_keys": billing_keys, "expected_usage_count": expected}
    async def admit(writer):
        require_open(writer)
        member = writer.state["members"][binding.member_id]
        attempt = writer.state["attempts"].get(member["current_attempt"])
        if attempt is None or attempt["state"] != "running" or attempt["driver_run_id"] != ctx.run_id or attempt["generation"] != ctx.run_generation:
            raise TeamError("AUTHORITY_REVOKED", "Paid work must belong to the member's current task attempt.")
        external = await writer.db.get(ExternalEffect if external_kind == "external_effect" else VideoJob, external_id)
        owner = getattr(external, "tenant_id", None) if external_kind == "external_effect" else getattr(external, "user_id", None)
        if external is None or owner != actor.owner_user_id or external.session_id != actor.member_id or external.project_id != writer.run.project_id:
            raise TeamError("INVALID_RESERVATION", "The paid effect does not belong to this member's project.")
        from team.usage import attribution
        data = {**fields, "team_attribution": attribution(writer.state, binding.member_id),
            "attempt_id": attempt["id"], "task_id": attempt["task_id"],
            "created_at": utcnow().isoformat()}
        writer.append("team.budget.reserved", "reservation", data)
        return {"reservation_id": identifier}
    await command(binding.run_id, actor, "paid:" + identifier, fields, admit)
    return Reservation(binding.run_id, actor, identifier)


async def reserve_job(ctx, tool: str, price, job, *, billing_keys: list[str], expected_usage_count: int | None = None):
    """Called before the job's first send; close rejected local placeholders."""
    try:
        return await reserve(ctx, tool, price, external_kind="video_job", external_id=job.id,
            billing_keys=billing_keys, expected_usage_count=expected_usage_count)
    except TeamError as exc:
        await refuse_job(job, exc)
        raise


async def refuse_job(job, exc: TeamError) -> None:
    """Only an adapter that has not dispatched may record this evidence."""
    from tool.video_production import _update_job, _mark_asset
    await _update_job(job.id, status="failed", error=f"{exc.code}: {exc}", completed_at=utcnow(),
        request_data={**(job.request_data or {}), "_team_not_dispatched": True})
    await _mark_asset(job.output_asset_id, status="failed")


async def mark_job_dispatch(job_id: str, ctx) -> None:
    """Invalidate an earlier unsent proof before a later provider attempt.

    This fact is independent of optional Trace recording. A crash afterwards
    is conservatively ambiguous. An ordinary owner retry of a former team
    job must not retain the first attempt's proof that nothing was submitted.
    """
    from db.base import get_db_session
    async with get_db_session() as db:
        job = await db.get(VideoJob, job_id, with_for_update=True)
        if job is None:
            return
        # Ordinary owner retries may come from another conversation. The job
        # keeps its original lineage, matching the existing owned-job API.
        if job.user_id != ctx.user_id:
            raise TeamError("AUTHORITY_REVOKED", "Provider dispatch is not owned by this caller.", status=403)
        metadata = dict(job.request_data or {})
        job.request_data = {**metadata, "_team_not_dispatched": False,
            "_team_provider_dispatches": int(metadata.get("_team_provider_dispatches", 0)) + 1}
        job.updated_at = utcnow()


async def link_usage(reservation: Reservation | None, event_id: str | None) -> None:
    """Attach a tool-side model meter before its provider request begins."""
    if reservation is None:
        return
    if not event_id:
        raise TeamError("INVALID_RESERVATION", "The tool-side model request requires a durable usage meter.")
    key = "llm:" + event_id
    async def link(writer):
        prior = writer.state["reservations"][reservation.id]
        usage = await writer.db.get(UsageEvent, event_id)
        if (usage is None or usage.user_id != writer.run.owner_user_id or usage.workspace_id != writer.run.workspace_id
                or usage.session_id != prior["member_id"] or usage.kind != "video_analyze"):
            raise TeamError("INVALID_RESERVATION", "The usage meter does not belong to this paid operation.")
        writer.append("team.budget.linked", "reservation", {"id": reservation.id, "tool": prior["tool"], "billing_key": key})
        return {"linked": True}
    await command(reservation.run_id, reservation.actor, f"paid-meter:{reservation.id}:{event_id}", {"key": key}, link)


def pending_for_attempt(state: dict, attempt_id: str) -> list[dict]:
    return [r for r in state["reservations"].values() if r["state"] == "reserved" and r.get("attempt_id") == attempt_id]


async def _settlement(db, run, reservation: dict) -> dict | None:
    """Unknown/failed local jobs without a bill are never treated as free."""
    kind = reservation.get("external_kind")
    if kind not in {"external_effect", "video_job"}:
        return None
    external = await db.get(ExternalEffect if kind == "external_effect" else VideoJob, reservation["external_id"])
    if external is None:
        return None
    owner = external.tenant_id if kind == "external_effect" else external.user_id
    status = external.state if kind == "external_effect" else external.status
    if (owner != run.owner_user_id or external.session_id != reservation["member_id"] or external.project_id != run.project_id
            or status not in {"succeeded", "completed", "failed", "cancelled"}):
        return None
    if (kind == "video_job" and status in {"failed", "cancelled"}
            and (external.request_data or {}).get("_team_not_dispatched") is True
            and not (external.request_data or {}).get("_team_provider_dispatches")
            and not external.provider_task_id):
        return {"id": reservation["id"], "tool": reservation["tool"], "confirmed_not_dispatched": True,
            "external_status": status}
    if (kind == "external_effect" and status == "failed" and external.attempt_count == 0
            and external.submitting_at is None and external.accepted_at is None and not external.provider_handle
            and (external.last_error or {}).get("code") == "abandoned_before_dispatch"):
        # Only the existing fenced effect recovery may produce this terminal
        # evidence. A merely prepared effect can still be sent and cannot free
        # the reservation; a generic failed/unknown effect is not free either.
        return {"id": reservation["id"], "tool": reservation["tool"], "confirmed_not_dispatched": True,
            "external_status": status}
    keys = reservation.get("billing_keys", [])
    if len(keys) != reservation.get("expected_usage_count", 0) or not keys:
        return None
    rows = (await db.execute(select(UsageEvent).where(UsageEvent.idempotency_key.in_(keys),
        UsageEvent.user_id == run.owner_user_id, UsageEvent.workspace_id == run.workspace_id,
        UsageEvent.session_id == reservation["member_id"]))).scalars().all()
    if len(rows) != len(keys) or any(row.credits is None or row.status not in {"charged", "shadow"} for row in rows):
        return None
    spent = sum((Decimal(row.credits) for row in rows), Decimal(0))
    if spent > amount(reservation["amount"]):
        return {"over_bound": True, "amount": str(spent), "billing_refs": sorted(row.id for row in rows)}
    return {"id": reservation["id"], "tool": reservation["tool"], "settled_amount": str(spent),
        "billing_ref": ",".join(sorted(row.id for row in rows)), "external_status": status}


async def reconcile(run_id: str, actor: Actor, state: dict | None = None) -> bool:
    from db.base import get_db_session
    from team.commands import run_status, queue_message, member_status, task_status
    from team.journal import owned_run
    server = replace(actor, kind="server", member_id=None, driver_run_id=None, generation=None)
    state = state if state is not None else await snapshot(run_id, server)
    candidates = [r for r in state["reservations"].values() if r["state"] == "reserved" and r.get("external_id")]
    changed = False
    for reservation in candidates:
        async with get_db_session() as db:
            run = await owned_run(db, run_id, server)
            evidence = await _settlement(db, run, reservation)
        if evidence is None:
            continue
        async def settle(writer):
            prior = writer.state["reservations"].get(reservation["id"])
            if prior is None or prior["state"] != "reserved":
                return {"settled": False}
            confirmed = await _settlement(writer.db, writer.run, prior)
            if confirmed is None:
                return {"settled": False}
            if confirmed.get("over_bound"):
                writer.append("team.notice", "notice", {"id": reservation["id"], "code": "PAID_PRICE_BOUND_EXCEEDED", **confirmed})
                if writer.run.state in {"running", "waiting"}:
                    run_status(writer, "pausing", pause_reason="paid_price_bound_exceeded")
                return {"settled": False, "changed": True}
            kind = "released" if confirmed.get("confirmed_not_dispatched") else "settled"
            writer.append("team.budget." + kind, "reservation", confirmed)
            attempt = writer.state["attempts"].get(prior["attempt_id"])
            if attempt and not pending_for_attempt(writer.state, attempt["id"]):
                target = prior["member_id"]
                if attempt["state"] == "outcome_unknown":
                    writer.append("team.attempt", "attempt", {**attempt, "state": "failed", "error": "External billing and outcome reconciled; inspect the existing job before retrying."})
                    task_status(writer, writer.state["tasks"][attempt["task_id"]], state="failed",
                        blocked_reason="External outcome reconciled; existing job is available for review.")
                    member_status(writer, target, current_attempt=None, execution_state="idle")
                    target = writer.run.root_session_id
                if writer.run.state in {"running", "waiting"}:
                    await queue_message(writer, to_member_id=target, from_member_id=writer.run.root_session_id,
                        kind="result", task_id=prior["task_id"], task_attempt_id=prior["attempt_id"],
                        body=f"Paid operation {prior['tool']} ({prior['external_id']}) is {confirmed['external_status']}; effect accounting is {kind}. Inspect its existing result; do not resubmit it.")
            return {"settled": True}
        result = await command(run_id, server, "paid-settle:" + reservation["id"] + ":" + digest(evidence), evidence, settle)
        changed |= result["settled"] or result.get("changed", False)
    return changed
