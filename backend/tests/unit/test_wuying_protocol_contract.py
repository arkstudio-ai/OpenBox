"""Keep the standalone Action Server and backend readiness contract aligned."""

import ast
from pathlib import Path

from sandbox.protocol import REQUIRED_ACTION_SERVER_CAPABILITIES


def _advertised_action_server_capabilities() -> set[str]:
    source_path = Path(__file__).resolve().parents[3] / "container" / "action_server.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "alive":
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Return) or not isinstance(child.value, ast.Dict):
                continue
            for key, value in zip(child.value.keys, child.value.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == "capabilities":
                    advertised = ast.literal_eval(value)
                    assert isinstance(advertised, list)
                    assert all(isinstance(item, str) for item in advertised)
                    return set(advertised)
    raise AssertionError("Action Server /alive capability list was not found")


def test_action_server_advertises_every_backend_required_capability():
    advertised = _advertised_action_server_capabilities()

    assert REQUIRED_ACTION_SERVER_CAPABILITIES <= advertised
