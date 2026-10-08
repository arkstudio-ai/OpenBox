"""Arguments a model double-encoded: a JSON object or list sent as a string where one is expected.

Measured 2026-10-08: qwen3.8-flash sent ``schedules.create``'s ``schedule`` as
'{"kind": "every", "every_ms": 300000}' six times in a row; every call failed
validation and the assistant gave up on a request the user had confirmed.
Only a string that parses into the container the schema asked for is
replaced, and only at the place the validation error names; anything else
fails exactly as before.
"""
import copy
import json

from pydantic import BaseModel, ValidationError

from core.log import create_logger

log = create_logger("tool.argument_repair")

# Pydantic error types that mean "a dict (or list) was expected here".
_EXPECTED = {"model_attributes_type": dict, "model_type": dict, "dict_type": dict,
             "list_type": list, "tuple_type": list, "set_type": list}


def validate(model: type[BaseModel], args, *, tool_id: str = ""):
    """``model.model_validate(args)``, once more with JSON strings decoded where containers were expected."""
    try:
        return model.model_validate(args)
    except ValidationError as exc:
        repaired = decoded(args, exc)
        if repaired is None:
            raise
        log.info("Tool %s arguments repaired: decoded JSON text where an object or list was expected", tool_id)
        return model.model_validate(repaired)


def decoded(args, exc: ValidationError):
    """A copy of ``args`` with each offending JSON string decoded, or None when there is nothing to decode."""
    if not isinstance(args, dict):
        return None
    repaired, changed = copy.deepcopy(args), False
    for error in exc.errors():
        expected, value = _EXPECTED.get(error.get("type")), error.get("input")
        if expected is None or not isinstance(value, str):
            continue
        try:
            parsed = json.loads(value)
        except ValueError:
            continue
        if isinstance(parsed, expected) and _replace(repaired, tuple(error.get("loc") or ()), value, parsed):
            changed = True
    return repaired if changed else None


def _replace(container, loc: tuple, old: str, new) -> bool:
    if not loc:
        return False
    *path, last = loc
    target = container
    for key in path:
        if isinstance(target, dict) and key in target:
            target = target[key]
        elif isinstance(target, list) and isinstance(key, int) and 0 <= key < len(target):
            target = target[key]
        else:
            return False  # a union branch name or a path the arguments do not have
    if isinstance(target, dict) and target.get(last) == old:
        target[last] = new
        return True
    if isinstance(target, list) and isinstance(last, int) and 0 <= last < len(target) and target[last] == old:
        target[last] = new
        return True
    return False
