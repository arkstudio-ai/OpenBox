"""Bind each Driver generation and account for natural model turn endings."""
from __future__ import annotations

from team.commands import member_status, run_status, update_task, wait_member
from team.errors import TeamError
from team.journal import Actor, command, digest
from team.runtime_binding import current_binding


def _actor(lease) -> Actor | None:
    binding = current_binding()
    if binding is None or binding.run_id is None:
        return None
    return Actor(binding.owner_user_id, binding.workspace_id, "member", binding.member_id, lease.run_id, lease.generation)


async def turn_started(lease) -> None:
    actor = _actor(lease)
    if actor is None:
        return
    binding = current_binding()
    async def started(writer):
        from team.capacity import clear
        clear(writer)
        member = writer.state["members"][actor.member_id]
        turns = member["coordinator_turns"] + int(member["role"] == "coordinator")
        if turns > writer.state["policy"]["max_coordinator_turns"]:
            run_status(writer, "pausing", pause_reason="coordinator_turn_limit")
            return {"blocked": True}
        attempt = writer.state["attempts"].get(member["current_attempt"])
        if attempt is not None and attempt["state"] == "running":
            writer.append("team.attempt", "attempt", {**attempt, "driver_run_id": lease.run_id, "generation": lease.generation})
        member_status(writer, actor.member_id, execution_state="running", coordinator_turns=turns,
            wait_after_seq=None, wait_deadline=None, last_seen_seq=writer.state["seq"])
        return {"bound": True}
    result = await command(binding.run_id, actor, f"start:{lease.run_id}:{lease.generation}", {}, started)
    if result.get("blocked"):
        from team.scheduler import schedule
        schedule(binding.run_id, actor)
        raise TeamError("TEAM_PAUSED", "The coordinator turn limit was reached.")


async def turn_ended(lease, *, text: str, outcome: str) -> None:
    actor = _actor(lease)
    if actor is None:
        return
    binding = current_binding()
    if outcome != "succeeded":
        if outcome == "error" and binding.role == "coordinator":
            from team.failures import record
            await command(binding.run_id, actor, f"failed-turn:{lease.generation}", {},
                lambda writer: record(writer, lease.generation))
        # The recovery pass classifies side effects after the Driver has
        # settled. It must not guess a failed/aborted operation's outcome.
        return
    async def ended(writer):
        if writer.state["run"]["state"] not in {"running", "waiting"}:
            return {"closed": True}
        member = writer.state["members"][actor.member_id]
        if member["role"] == "coordinator":
            if member.get("consecutive_failures"):
                member_status(writer, actor.member_id, consecutive_failures=0)
            from sqlalchemy import select
            from db.models.question import QuestionCheckpoint
            checkpoint = await writer.db.scalar(select(QuestionCheckpoint.id).where(
                QuestionCheckpoint.session_id == actor.member_id,
                QuestionCheckpoint.user_id == actor.owner_user_id,
                QuestionCheckpoint.status.in_(["pending", "answered", "rejected"]),
                QuestionCheckpoint.applied.is_(False)).limit(1))
            if checkpoint:
                # Durable questions release the Driver with a successful turn
                # outcome. That is a legitimate user wait, not a no-progress
                # coordinator reply. Answer application owns the next wake.
                member_status(writer, actor.member_id, execution_state="waiting", nudged=False,
                    wait_after_seq=None, wait_deadline=None)
                return {"waiting_for_user": True}
        if member["execution_state"] == "waiting":
            return {"waiting": True}
        attempt = writer.state["attempts"].get(member["current_attempt"])
        if member["role"] == "member" and (attempt is None or attempt["state"] != "running"):
            member_status(writer, actor.member_id, execution_state="idle")
            return {"idle": True}
        if member["role"] == "coordinator":
            result = await wait_member(writer, writer.state["seq"])
            if result["status"] == "waiting":
                return result
            if member["nudged"]:
                run_status(writer, "pausing", pause_reason="stalled")
                return {"paused": True}
            prompt = "本轮没有可继续推进的在途工作。请检查目标，创建可执行任务、明确报告阻塞、向用户提问或验收后调用 team_finish。不要空等或宣称团队已完成。"
        else:
            task = writer.state["tasks"][attempt["task_id"]]
            if text.strip() and member["nudged"]:
                # Schema-bound results still require a proper structured
                # submission; an unstructured answer becomes a blocker.
                action = "block" if task["output_schema"] else "submit"
                return await update_task(writer, task["id"], task["revision"], action,
                    summary=text[:4000], implicit=True)
            if member["nudged"]:
                return await update_task(writer, task["id"], task["revision"], "block", summary="Member stopped twice without submitting a result.")
            prompt = "你仍有运行中的任务。请用 team_task_update 提交 summary 和成果；遇到阻碍请 block，等待其他成员请 team_wait。自然语言答复不会自动验收任务。"
        from team.runtime import runtime
        await runtime.enqueue(writer.db, writer.run, actor.member_id,
            {"kind": "team_control", "after_seq": writer.state["seq"]},
            "team:nudge:" + digest([lease.run_id, lease.generation])[:48], prompt)
        member_status(writer, actor.member_id, nudged=True, execution_state="queued")
        return {"nudged": True}
    await command(binding.run_id, actor, f"end:{lease.run_id}:{lease.generation}", {"outcome": outcome, "text": text}, ended)


def after_release() -> None:
    binding = current_binding()
    if binding is not None and binding.run_id:
        from team.scheduler import schedule
        schedule(binding.run_id, Actor(binding.owner_user_id, binding.workspace_id, "server"))
