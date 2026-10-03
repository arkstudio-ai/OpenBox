"""Which tool results carry memory text that is only ever read fresh.

The assistant gets that text from memory.tool_projection, re-read under the
current permissions; saved chat history, events and anyone else who can open
the chat keep only references. Dependency-free so chat persistence and the
projection share one definition.
"""
from collections.abc import Mapping
from typing import Any

TRANSIENT_TOOL_IDS = frozenset({"memory_search", "memory_read_sources", "current_task_state"})
#: creator_context's read actions return memory text as well; its saves do not.
LEGACY_READ_ACTIONS = frozenset({"get_user_context", "search_memories", "list_active_memories"})


def tool_input(part: Mapping[str, Any]) -> Mapping[str, Any]:
    value = part.get("input")
    if not isinstance(value, Mapping):
        state = part.get("state")
        value = state.get("input") if isinstance(state, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def memory_operation(identity: Any, part: Mapping[str, Any]) -> str | None:
    """The memory read a tool part performed, or None when it holds no memory text."""
    if identity in TRANSIENT_TOOL_IDS:
        return identity
    if identity == "creator_context" and tool_input(part).get("action") in LEGACY_READ_ACTIONS:
        return "creator_context"
    return None
