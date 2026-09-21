"""Durable, bounded recovery of coordinator execution failures."""
from team.commands import member_status, run_status


async def record(writer, generation: int) -> dict:
    root = writer.run.root_session_id
    member = writer.state["members"][root]
    if writer.state["run"]["state"] not in {"running", "waiting"} or (member.get("last_failure_generation") or 0) >= generation:
        return {"recorded": False}
    count = member.get("consecutive_failures", 0) + 1
    member_status(writer, root, consecutive_failures=count, last_failure_generation=generation,
        execution_state="stopped" if count >= 3 else "queued", wait_after_seq=None, wait_deadline=None)
    writer.append("team.notice", "notice", {"id": f"coordinator-failure:{generation}",
        "code": "COORDINATOR_EXECUTION_FAILED", "consecutive_failures": count,
        "message": "The coordinator execution failed; completed work remains available."})
    if count >= 3:
        run_status(writer, "pausing", pause_reason="coordinator_errors")
        return {"recorded": True, "paused": True}
    from team.runtime import runtime
    await runtime.enqueue(writer.db, writer.run, root, {"kind": "team_control", "after_seq": writer.state["seq"]},
        f"team:coordinator-retry:{writer.run.id}:{generation}",
        "协调者上轮执行失败。请读取权威团队状态，从已提交的结果继续；不要重做已完成的操作，也不要重试结果未知的外部请求。")
    return {"recorded": True, "wake": root}
