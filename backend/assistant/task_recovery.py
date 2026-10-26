"""Keep a failed followup on its original task, using current-run receipts.

This only narrows capabilities. Source authorization, fresh revisions and
task controls remain authoritative; no user expression is matched here.
"""
from dataclasses import dataclass

NOT_RUNNING = "ASSISTANT_TASK_NOT_RUNNING"
RECOVERY_ONLY = "ASSISTANT_TASK_RECOVERY_ONLY"
_STOP = {"ASSISTANT_EFFECT_UNRESOLVED", "ASSISTANT_STILL_STOPPING",
         "ASSISTANT_SOURCE_UNVERIFIED", "ASSISTANT_ACTOR_UNAVAILABLE", RECOVERY_ONLY}
_TOOLS = {"tasks.get", "tasks.resume", "tasks.followup", "question"}


@dataclass(frozen=True)
class Recovery:
    task_id: str
    stopped: bool = False


def recovery_state(parts):
    target, failures, stopped = None, 0, False
    for part in parts:
        data = part if isinstance(part, dict) else vars(part)
        tool, args = data.get("tool"), data.get("input") or {}
        metadata = data.get("metadata") or {}
        code = metadata.get("failure_code")
        task_id = args.get("task_id") if isinstance(args, dict) else None
        if tool == "tasks.followup" and code == NOT_RUNNING and task_id:
            target, failures = task_id, failures + 1
        elif target:
            if code in _STOP:
                stopped = True
            elif tool in {"tasks.resume", "tasks.followup"} and task_id == target:
                if code or data.get("status") == "error":
                    failures += 1
                elif tool == "tasks.followup" and data.get("status") == "completed":
                    target, failures, stopped = None, 0, False
    return Recovery(target, stopped or failures >= 3) if target else None


def permits_tool(state, canonical_id, arguments=None):
    if state is None:
        return True
    if state.stopped or canonical_id not in _TOOLS:
        return False
    return (arguments is None or canonical_id == "question"
            or isinstance(arguments, dict) and arguments.get("task_id") == state.task_id)


def instruction(state):
    if state.stopped:
        return ("The original task could not safely continue. Stop tool use and explain the actual "
                "blocker briefly. Preserve the task, saved input and attachments. Do not create a "
                "replacement, claim work started or ask for an already available upload.")
    return (f"Followup on original task {state.task_id} was held. Keep this exact task and Session. "
            "Read tasks.get, then, only if the human requested continuation, tasks.resume and "
            "tasks.followup using the returned revision and current human instructions. A stopped "
            "task needs new input after reopening; canceled input and approvals stay canceled. "
            "Only these original-task tools and clarification are available during recovery. "
            "If intent is unclear, ask briefly rather than resuming. Never submit or link a replacement.")
