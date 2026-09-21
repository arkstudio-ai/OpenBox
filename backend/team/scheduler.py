"""Bounded convergence and coalesced wakes; the journal owns all decisions."""
from __future__ import annotations

import asyncio
from contextvars import Context
from dataclasses import replace
from datetime import datetime, timezone
import json

from sqlalchemy import select, update

from core.identifier import ascending
from core.log import create_logger
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.team import TeamEvent, TeamRun
from team.commands import dispatch_ready, member_status, run_status, task_status
from team.errors import TeamError
from team.journal import Actor, command, digest, snapshot, utcnow
from team.runtime import ExecutionSnapshot
from team.state import TERMINAL, budget_totals, ready_tasks

log = create_logger("team.scheduler")
_scheduled: dict[str, asyncio.Task] = {}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _live(driver: ExecutionSnapshot | None) -> bool:
    return bool(driver and driver.phase != "idle" and driver.lease_expires_at and _aware(driver.lease_expires_at) > utcnow())


def _ready_after_input(writer, member_id: str) -> None:
    # A live generation can consume new mail while it keeps working. Only
    # waiting or inactive members need to enter the execution queue.
    member = writer.state["members"][member_id]
    execution = "running" if member["execution_state"] == "running" else "queued"
    member_status(writer, member_id, wait_after_seq=None, wait_deadline=None, execution_state=execution)


async def can_wake(session_id: str, user_id: str) -> bool:
    """Keep paused member inputs durable and unclaimed by generic recovery."""
    from db.models.session import Session
    async with get_db_session() as db:
        session = await db.get(Session, session_id)
        if session is None or session.user_id != user_id:
            return False
        if session.kind != "team_member" and session.agent != "team":
            return True
        root_id = session.parent_id if session.kind == "team_member" else session_id
        run = (await db.execute(select(TeamRun).where(TeamRun.root_session_id == root_id, TeamRun.session_active == 1))).scalar_one_or_none()
        if run is None:
            return session.kind != "team_member"
        if run.owner_user_id != user_id or run.state not in {"running", "waiting"}:
            return False
        from team.journal import read_state
        from team.capacity import deferred
        state = await read_state(db, run)
        return not deferred(state) or (session.kind != "team_member" and state["run"].get("capacity_member_id") != session_id)


def _dispatchable(state: dict, live_members: set[str] | None = None, other_desktop=False) -> bool:
    from team.capacity import deferred
    if deferred(state):
        return False
    running = [a for a in state["attempts"].values() if a["state"] == "running"]
    executing = [a for a in running if state["members"][a["member_id"]]["execution_state"] != "waiting"]
    executing_ids = {a["member_id"] for a in executing} | (live_members or set())
    if len(executing_ids) >= state["policy"]["max_concurrent_members"]:
        return False
    occupied = {a["member_id"] for a in running} | (live_members or set())
    from team.resources import occupies_desktop
    desktop_busy = other_desktop or occupies_desktop(state)
    return any(t["owner_member_id"] not in occupied and state["members"][t["owner_member_id"]]["membership_state"] == "active"
        and (t["exclusive_group"] != "desktop" or not desktop_busy) for t in ready_tasks(state))


async def tick(run_id: str, actor: Actor) -> dict:
    from core.config import get_config
    from team.runtime import runtime
    server = replace(actor, kind="server", member_id=None, driver_run_id=None, generation=None)
    state = await snapshot(run_id, server)
    from team.paid_tools import reconcile, pending_for_attempt
    if await reconcile(run_id, server, state):
        state = await snapshot(run_id, server)
    if state["run"]["state"] in TERMINAL | {"paused"}:
        return {"changed": False}
    if state["run"]["state"] == "canceling":
        from team.service import cancel_waiting_questions
        await cancel_waiting_questions(run_id, server)
    root = state["run"]["root_session_id"]
    closing = state["run"]["state"] in {"pausing", "canceling", "completing"}
    async with get_db_session() as db:
        drivers = await runtime.observe(db, state["members"], actor.owner_user_id)
        from team.resources import other_desktop_busy
        run = await db.get(TeamRun, run_id)
        desktop_busy = bool(run) and any(t["exclusive_group"] == "desktop" for t in ready_tasks(state)) and await other_desktop_busy(db, run)
        from team.execution import expired_members
        expired_agents = set() if closing else await expired_members(db, state, drivers, utcnow())
        from db.models.session import Session
        from db.models.question import QuestionCheckpoint
        root_driver = drivers.get(root)
        failed_root = bool(not closing and root_driver and root_driver.phase == "idle"
            and root_driver.generation > (state["members"][root].get("last_failure_generation") or 0)
            and await db.scalar(select(Session.id).where(Session.id == root, Session.status == "error"))
            and not await db.scalar(select(QuestionCheckpoint.id).where(QuestionCheckpoint.session_id == root,
                QuestionCheckpoint.applied.is_(False), QuestionCheckpoint.status.in_(["pending", "answered", "rejected"])).limit(1)))
        from team import liveness
        attention = [] if closing else await liveness.candidates(db, state, drivers)
    if closing:
        # A normal finish allows the coordinator's closing turn to return its
        # final prose. Pause/cancel interrupt every live generation.
        for member_id, driver in drivers.items():
            if _live(driver) and (state["run"]["state"] != "completing" or member_id != root):
                if not runtime.capabilities().cancel:
                    raise TeamError("CAPABILITY_UNSUPPORTED", "The runtime cannot interrupt this execution.")
                await runtime.interrupt(member_id, actor.owner_user_id,
                    expected_run_id=driver.run_id, expected_generation=driver.generation)
    else:
        for member in state["members"].values():
            if member["interrupt_requested"] and _live(drivers.get(member["id"])):
                if not runtime.capabilities().cancel:
                    raise TeamError("CAPABILITY_UNSUPPORTED", "The runtime cannot interrupt this execution.")
                driver = drivers[member["id"]]
                await runtime.interrupt(member["id"], actor.owner_user_id,
                    expected_run_id=driver.run_id, expected_generation=driver.generation)
    now = utcnow()
    debounce = max(0, float(get_config().team_wake_debounce_seconds))
    pending = [m for m in state["messages"].values() if m["state"] == "queued" and m["kind"] != "progress"]
    root_ready = any(m["to_member_id"] == root and (not m.get("created_at") or
        (now - _aware(datetime.fromisoformat(m["created_at"]))).total_seconds() >= debounce) for m in pending)
    pending = [m for m in pending if m["to_member_id"] != root or root_ready]
    expired = [m for m in state["members"].values() if m["wait_deadline"] and _aware(datetime.fromisoformat(m["wait_deadline"])) <= now]
    stopping_all = state["run"]["state"] in {"canceling", "completing"}
    stopped = [a for a in state["attempts"].values() if a["state"] == "running" and (a["driver_run_id"] or stopping_all)
        and not _live(drivers.get(a["member_id"])) and (stopping_all or state["members"][a["member_id"]]["execution_state"] != "waiting")]
    can_close = closing and not any(_live(d) for d in drivers.values())
    wall_time = (now - _aware(datetime.fromisoformat(state["run"]["created_at"]))).total_seconds()
    overdue = not closing and wall_time > state["policy"]["max_wall_time_seconds"]
    # Missing model pricing remains visible in account usage. It does not make
    # an accepted deliverable unfinished. Ambiguous external effects still do.
    unresolved = any(a["state"] in {"running", "outcome_unknown"} for a in state["attempts"].values()) or budget_totals(state)[0] > 0
    if can_close and not unresolved and state["run"]["state"] in {"canceling", "completing"}:
        if state["run"]["state"] == "completing":
            from team.completion import recover_final_response
            await recover_final_response(run_id, server)
        from team.workspace_snapshots import ensure
        if not await ensure(run_id, server, "end"):
            return {"changed": False, "wake": []}
        state = await snapshot(run_id, server)
    needs_change = (can_close and (state["run"]["state"] == "pausing" or not unresolved)) or stopped or (not closing and (pending or expired or expired_agents or failed_root or attention or _dispatchable(state, {mid for mid, driver in drivers.items() if _live(driver)}, desktop_busy) or overdue))
    wake = set()
    changed = False
    if needs_change:
        async def advance(writer):
            nonlocal changed
            wake_ids = set()
            current_closing = writer.state["run"]["state"] in {"pausing", "canceling", "completing"}
            if failed_root and not current_closing:
                current_driver = (await runtime.observe(writer.db, [root], actor.owner_user_id)).get(root)
                if current_driver and current_driver.phase == "idle" and current_driver.generation == root_driver.generation:
                    from team.failures import record
                    failure = await record(writer, current_driver.generation)
                    if failure.get("wake"):
                        wake_ids.add(root)
                    current_closing = writer.state["run"]["state"] in {"pausing", "canceling", "completing"}
            # Recheck Driver rows inside the decision transaction; do not
            # confuse a pre-lock observation with authority to stop an attempt.
            for candidate in stopped:
                attempt = writer.state["attempts"].get(candidate["id"])
                if attempt is None or attempt["state"] != "running" or attempt["driver_run_id"] != candidate["driver_run_id"]:
                    continue
                observed = await runtime.reconcile(writer.db, attempt["member_id"], actor.owner_user_id, attempt["driver_run_id"])
                driver = observed.execution
                if _live(driver) or (driver is not None and driver.phase != "idle"):
                    continue  # Driver recovery repairs the exact tail first.
                if observed.accepted_input and not current_closing:
                    wake_ids.add(attempt["member_id"])
                    continue
                effects = observed.unresolved_effects
                paid = pending_for_attempt(writer.state, attempt["id"])
                outcome = "outcome_unknown" if effects or paid else "blocked"
                reason = "External outcome requires reconciliation" if effects or paid else "Execution stopped before submitting a result"
                writer.append("team.attempt", "attempt", {**attempt, "state": outcome, "error": reason, "ended_at": utcnow().isoformat(),
                    "external_effects": list(effects),
                    "paid_reservations": [row["id"] for row in paid]})
                task = writer.state["tasks"][attempt["task_id"]]
                task_status(writer, task, state=outcome, blocked_reason=reason)
                member_status(writer, attempt["member_id"], execution_state="stopped", current_attempt=None, interrupt_requested=False)
            if overdue and not current_closing:
                run_status(writer, "pausing", pause_reason="wall_time_exceeded")
                current_closing = True
            if expired_agents and not current_closing:
                current_drivers = await runtime.observe(writer.db, expired_agents, actor.owner_user_id)
                still_expired = await expired_members(writer.db, writer.state, current_drivers, utcnow())
                if still_expired:
                    for member_id in sorted(still_expired):
                        member_status(writer, member_id, execution_state="stopped", interrupt_requested=True,
                            wait_after_seq=None, wait_deadline=None)
                        writer.append("team.notice", "notice", {"id": ascending("tnotice"), "code": "AGENT_TIME_LIMIT",
                            "member_id": member_id, "message": "The Agent execution time limit was reached; review its interrupted task before retrying."})
                    run_status(writer, "pausing", pause_reason="agent_wall_time_exceeded")
                    current_closing = True
            if not current_closing:
                from team.commands import deliver_message
                root_batch = []
                for receipt in pending:
                    latest = writer.state["messages"].get(receipt["id"])
                    if latest is None or latest["state"] != "queued":
                        continue
                    event = (await writer.db.execute(select(TeamEvent).where(TeamEvent.team_run_id == run_id,
                        TeamEvent.sequence == receipt["queued_seq"]))).scalar_one()
                    target = writer.state["members"][receipt["to_member_id"]]
                    if target["membership_state"] != "active":
                        writer.append("team.message.failed", "message", {"id": receipt["id"], "to_member_id": target["id"], "reason": "Member is unavailable"})
                        continue
                    if target["id"] == root:
                        root_batch.append(event.payload["data"])
                        continue
                    await deliver_message(writer, event.payload["data"])
                    _ready_after_input(writer, target["id"])
                    wake_ids.add(target["id"])
                if root_batch:
                    # One durable input, hence one cold Driver wake, carries
                    # every result observed in this coalescing window. Full
                    # messages remain available in the journal.
                    prompt = json.dumps({"team_updates": [{"message_id": item["id"],
                        "from_member_id": item["from_member_id"], "kind": item["kind"],
                        "task_id": item.get("task_id"), "summary": item["body"][:1500],
                        "task": {key: task[key] for key in ("id", "state", "revision", "current_attempt", "deliverable")}
                            if (task := writer.state["tasks"].get(item.get("task_id"))) else None}
                        for item in root_batch]}, ensure_ascii=False)
                    inbox_id = await runtime.enqueue(writer.db, writer.run, root,
                        {"kind": "team_control", "after_seq": writer.state["seq"]},
                        "team:batch:" + digest([item["id"] for item in root_batch])[:48], prompt)
                    for item in root_batch:
                        writer.append("team.message.delivered", "message", {"id": item["id"], "to_member_id": root, "inbox_id": inbox_id})
                    _ready_after_input(writer, root)
                    wake_ids.add(root)
                for member in expired:
                    current = writer.state["members"][member["id"]]
                    if current["wait_deadline"] != member["wait_deadline"]:
                        continue
                    await runtime.enqueue(writer.db, writer.run, member["id"], {"kind": "team_control", "after_seq": member["wait_after_seq"]},
                        "team:wait:" + digest([member["id"], member["wait_deadline"]])[:48], "等待已超时。请查看团队状态并决定下一步，不要假定任务已完成。")
                    _ready_after_input(writer, member["id"])
                    wake_ids.add(member["id"])
                dispatched = await dispatch_ready(writer)
                wake_ids.update(item["member_id"] for item in dispatched["dispatched"])
                observed = await runtime.observe(writer.db, writer.state["members"], actor.owner_user_id)
                wake_ids.update(await liveness.advance(writer, observed))
            else:
                still_live = (await runtime.observe(writer.db, writer.state["members"], actor.owner_user_id)).values()
                if not any(_live(driver) for driver in still_live):
                    target = writer.state["run"]["state"]
                    unresolved = any(a["state"] in {"running", "outcome_unknown"} for a in writer.state["attempts"].values()) or budget_totals(writer.state)[0] > 0
                    if target == "pausing":
                        run_status(writer, "paused")
                    elif target == "canceling" and not unresolved:
                        if writer.state["run"].get("workspace_snapshots", {}).get("end", {}).get("status", "unavailable") not in {"ready", "unavailable"}:
                            return {"changed": bool(writer.events), "wake": [], "seq": writer.state["seq"], "state": target}
                        for task in list(writer.state["tasks"].values()):
                            if task["state"] not in {"succeeded", "canceled"}:
                                task_status(writer, task, state="canceled", cancellation_reason="Team canceled by its owner")
                        await close_inputs(writer)
                        run_status(writer, "canceled", ended_at=utcnow().isoformat())
                    elif target == "completing" and not unresolved:
                        if writer.state["run"].get("workspace_snapshots", {}).get("end", {}).get("status", "unavailable") not in {"ready", "unavailable"}:
                            return {"changed": bool(writer.events), "wake": [], "seq": writer.state["seq"], "state": target}
                        await close_inputs(writer)
                        run_status(writer, writer.state["run"].get("final_status", "completed"), ended_at=utcnow().isoformat())
            changed = bool(writer.events)
            return {"changed": changed, "wake": sorted(wake_ids), "seq": writer.state["seq"], "state": writer.state["run"]["state"]}
        result = await command(run_id, server, f"scheduler:{state['seq']}", {"seq": state["seq"]}, advance)
        wake.update(result.get("wake", []))
        changed = result.get("changed", False)
        closing = result.get("state") in TERMINAL | {"pausing", "paused", "canceling", "completing"}
    # Queue acceptance is durable even if this process exits before wake.
    # Also re-wake preexisting accepted inputs after a capacity slot frees.
    if not closing:
        async with get_db_session() as db:
            wake.update((await db.execute(select(AgentInboxItem.session_id).where(AgentInboxItem.session_id.in_(state["members"]),
                AgentInboxItem.state == "accepted", AgentInboxItem.delivery.in_(["followup", "steer"])))).scalars().all())
        for member_id in sorted(wake):
            await runtime.wake(member_id, actor.owner_user_id)
    if changed:
        from bus import bus
        latest = await snapshot(run_id, server)
        bus.publish("team.run.updated", {"userId": actor.owner_user_id, "sessionId": root, "teamRunId": run_id,
            "seq": latest["seq"], "state": latest["run"]["state"]})
        if latest["run"]["state"] in TERMINAL:
            bus.publish("session.updated", {"userId": actor.owner_user_id, "sessionId": root, "agent": "build"})
    return {"changed": changed, "wake": sorted(wake)}


async def close_inputs(writer) -> None:
    """Close only this team's synthetic queue before releasing its root.

    A queued user message remains a user message and may run in build mode.
    A late team result must never wake that same root as an ordinary prompt.
    """
    from db.models.session import Session
    now = utcnow()
    await writer.db.execute(update(AgentInboxItem).where(
        AgentInboxItem.session_id.in_(writer.state["members"]),
        AgentInboxItem.user_id == writer.run.owner_user_id,
        AgentInboxItem.source_type.is_not(None), AgentInboxItem.state == "accepted",
    ).values(state="canceled", canceled_at=now, updated_at=now, outcome="canceled"))
    for message in list(writer.state["messages"].values()):
        if message["state"] == "queued":
            writer.append("team.message.canceled", "message", {"id": message["id"], "to_member_id": message["to_member_id"]})
    for member in list(writer.state["members"].values()):
        member_status(writer, member["id"], wait_after_seq=None, wait_deadline=None, execution_state="idle", interrupt_requested=False)
    root = await writer.db.get(Session, writer.run.root_session_id, with_for_update=True)
    if root and root.agent == "team":
        root.agent = "build"


def schedule(run_id: str, actor: Actor) -> None:
    if run_id in _scheduled and not _scheduled[run_id].done():
        return
    async def work():
        try:
            await tick(run_id, actor)
            # A second pass handles coordinator result-message coalescing.
            from core.config import get_config
            await asyncio.sleep(max(0, float(get_config().team_wake_debounce_seconds)))
            await tick(run_id, actor)
        except TeamError as exc:
            if exc.code != "TEAM_NOT_FOUND":
                log.warning("Team scheduler deferred run=%s code=%s", run_id, exc.code)
        except Exception:
            log.exception("Team scheduler deferred run=%s", run_id)
        finally:
            _scheduled.pop(run_id, None)
    # Convergence belongs to the server, not the model turn that requested it.
    # Inheriting that turn's lease/question ticket leaves later sandbox
    # snapshots and cold wakes bound to an already revoked generation.
    # Each tick still authorizes the explicit owner/workspace from the journal.
    _scheduled[run_id] = asyncio.create_task(work(), name=f"team-scheduler:{run_id}", context=Context())


async def recover_teams() -> int:
    async with get_db_session() as db:
        rows = (await db.execute(select(TeamRun.id, TeamRun.owner_user_id, TeamRun.workspace_id)
            .where(TeamRun.session_active == 1).order_by(TeamRun.updated_at).limit(100))).all()
    changed = 0
    for row in rows:
        try:
            result = await tick(row.id, Actor(row.owner_user_id, row.workspace_id, "server"))
            changed += bool(result["changed"])
        except Exception:
            log.exception("Team recovery deferred run=%s", row.id)
    return changed
