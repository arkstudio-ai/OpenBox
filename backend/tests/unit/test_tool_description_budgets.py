"""Regression budgets for always-advertised, high-cost tool definitions."""

import json

import pytest

from agent.tool_payload import build_tool_definitions
from tool.bash import bash_tool
from tool.batch import batch_tool
from tool.computer import computer_tool
from tool.task import task_tool
from tool.todo_tool import todo_write_tool


def _responses_item(tool) -> dict:
    return build_tool_definitions({tool.id: tool}, "responses")[0]


def _compact_chars(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


@pytest.mark.parametrize(
    ("tool", "limit"),
    [
        (todo_write_tool, 2_500),
        (task_tool, 2_500),
        (bash_tool, 2_500),
        (computer_tool, 5_000),
        # This was 1,623 chars before PR #1; keep the reduction substantial.
        (batch_tool, 900),
    ],
    ids=lambda value: getattr(value, "id", str(value)),
)
def test_responses_tool_item_fits_description_budget(tool, limit):
    """Measure the exact production Responses item, including its full schema."""
    item = _responses_item(tool)

    assert _compact_chars(item) <= limit


@pytest.mark.parametrize(
    ("tool", "properties", "required"),
    [
        (todo_write_tool, {"todos"}, {"todos"}),
        (task_tool, {"description", "prompt", "subagent_type"}, {"description", "prompt"}),
        (bash_tool, {"command", "timeout", "description"}, {"command"}),
        (batch_tool, {"invocations"}, {"invocations"}),
    ],
    ids=lambda value: getattr(value, "id", str(value)),
)
def test_description_slimming_does_not_remove_tool_schema(tool, properties, required):
    schema = _responses_item(tool)["parameters"]

    assert schema["type"] == "object"
    assert set(schema["properties"]) == properties
    assert set(schema["required"]) == required


def test_computer_union_schema_is_preserved():
    schema = _responses_item(computer_tool)["parameters"]

    assert schema["type"] == "object"
    assert len(schema["oneOf"]) == 2
    batch = next(
        branch
        for branch in schema["oneOf"]
        if branch["properties"]["action"].get("const") == "batch"
    )
    single = next(branch for branch in schema["oneOf"] if branch is not batch)
    assert set(batch["properties"]) == {"action", "actions"}
    assert batch["required"] == ["action", "actions"]
    assert batch["properties"]["actions"]["maxItems"] == 12
    assert "actions" not in single["properties"]
    assert "batch" not in single["properties"]["action"]["enum"]


def test_short_descriptions_keep_non_schema_contracts():
    todo = todo_write_tool.description
    assert "Full-replacement" in todo
    assert "Exactly one" in todo and "`in_progress`" in todo
    assert "(added by user)" in todo and "user cancelled" in todo

    task = task_tool.description
    assert "fresh child conversation and context" in task
    assert "shares the parent's project sandbox/worktree" in task
    assert "independent tasks concurrently" in task and "sequentially" in task

    bash = bash_tool.description
    assert "action server" in bash and "PID 1" in bash
    assert "commit or push only" in bash and "git config" in bash
    assert "destructive/force operations" in bash

    computer = computer_tool.description
    assert "shared, stateful" in computer and "screenshot" in computer
    assert 'action: "batch"' in computer and "generic parallel `batch`" in computer
    assert "one final screenshot" in computer and "OSS" in computer

    batch = batch_tool.description
    assert "independent tool calls concurrently" in batch
    assert "Ordering is not guaranteed" in batch
    assert "computer(action='batch'" in batch
