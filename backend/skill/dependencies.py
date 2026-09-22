"""Bounded dependency declarations used when a user binds a Skill to an Agent.

Reading a Skill at runtime is not an authorization operation. Definition
compilation adds these requested capabilities before the existing grant checks.
"""
from collections.abc import Mapping


def required_mcp(value) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        value = value.get("requires_mcp") or value.get("requires-mcp") or []
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(item.strip() for item in value
        if isinstance(item, str) and 0 < len(item.strip()) <= 128
        and not any(ord(char) < 32 or ord(char) == 127 for char in item)))[:64]
