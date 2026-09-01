"""Detection for an agent stuck repeating one tool call.

The threshold lives here because two layers watch for this at different
scopes and previously each carried its own copy of the number:

* `ToolHooks` looks at the calls made within the current step, and gates on a
  permission prompt before the tool runs.
* The step processor looks across completed steps, catching a model that
  re-issues the same call every turn.

The windows differ on purpose — one includes the call being evaluated, the
other only prior ones — so the predicates stay separate. Only the constant is
shared, so raising the tolerance raises it in both places.
"""
import json

DOOM_LOOP_THRESHOLD = 3

# These tools expose long-running jobs. Repeating a read-only ``wait`` or
# ``status`` call with the same job id is their documented polling contract,
# not evidence that the model is stuck. Mutating actions such as ``submit``,
# ``retry`` and ``cancel`` deliberately remain protected by the ordinary doom
# loop guard.
_REPEATABLE_POLL_ACTIONS = {
    "video_generate": frozenset({"wait", "status"}),
    "video_transcribe": frozenset({"wait", "status"}),
}


def is_repeatable_poll(tool_name: str, tool_args: dict) -> bool:
    """Whether an identical call is a safe, read-only async-job poll."""
    actions = _REPEATABLE_POLL_ACTIONS.get(tool_name)
    if not actions or not isinstance(tool_args, dict):
        return False
    return str(tool_args.get("action") or "").strip().lower() in actions


def is_repeat_of_recent(completed_tool_parts: list, tool_name: str, tool_args: dict) -> bool:
    """Whether this call repeats the last DOOM_LOOP_THRESHOLD - 1 completed ones.

    Args are compared by canonical JSON so key ordering never masks a repeat.
    Mirrors opencode's processor.ts doom-loop detection.
    """
    if is_repeatable_poll(tool_name, tool_args):
        return False
    if len(completed_tool_parts) < DOOM_LOOP_THRESHOLD - 1:
        return False
    recent = completed_tool_parts[-(DOOM_LOOP_THRESHOLD - 1):]
    current_key = json.dumps(tool_args, sort_keys=True)
    for part in recent:
        if part.tool != tool_name:
            return False
        if json.dumps(part.input, sort_keys=True) != current_key:
            return False
    return True
