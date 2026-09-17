"""Monotonic parent -> child Agent authority boundaries."""
import pytest

from agent.hooks import ToolHooks
from agent.subagent_authority import (
    SubagentAuthorityError,
    authority_for_spawn,
    compose_subagent_authority,
    parse_subagent_authority,
    restrict_tools,
)
from permission.permission import Rule


def _allow_all() -> list[Rule]:
    return [Rule(permission="*", pattern="*", action="allow")]


def _plan_boundary():
    return compose_subagent_authority(
        tool_ids={"task", "read", "write", "edit", "grep"},
        permission_rules=[
            *_allow_all(),
            Rule(permission="edit", pattern="*", action="deny"),
            Rule(
                permission="edit",
                pattern=".openbox/plans/*.md",
                action="allow",
            ),
        ],
        guard_rules=[],
    )


@pytest.mark.asyncio
async def test_plan_parent_cannot_gain_project_writes_via_general_child():
    parent = _plan_boundary()
    effective = compose_subagent_authority(
        tool_ids={"read", "write", "edit", "bash"},
        permission_rules=_allow_all(),
        guard_rules=[],
        inherited=parent,
    )

    # Tool authority is an intersection: general's extra Bash capability does
    # not exist, while common definitions still pass the fine-grained planes.
    assert effective.tool_ids == frozenset({"read", "write", "edit"})
    hooks = ToolHooks(
        "child-session",
        config_rules=_allow_all(),
        authority_rule_planes=parent.permission_planes,
    )
    blocked = await hooks.authorize_tool(
        "write",
        {"file_path": "src/escalated.py", "content": "not allowed"},
    )
    assert blocked is not None
    assert blocked.metadata["blocked"] is True

    # The child may use only the parent's narrow plan-file exception; this is
    # equal authority, not a general project write escalation.
    allowed = await hooks.authorize_tool(
        "write",
        {"file_path": ".openbox/plans/refactor.md", "content": "plan"},
    )
    assert allowed is None


def test_child_tool_allowlist_and_rules_both_remain_intersections():
    parent = compose_subagent_authority(
        tool_ids={"task", "read", "grep"},
        permission_rules=_allow_all(),
        guard_rules=[Rule(permission="read", pattern="secret/**", action="deny")],
    )
    child = compose_subagent_authority(
        tool_ids={"read", "write"},
        permission_rules=[
            *_allow_all(),
            Rule(permission="read", pattern="generated/**", action="deny"),
        ],
        guard_rules=[],
        inherited=parent,
    )
    assert set(restrict_tools({"read": 1, "write": 2}, parent)) == {"read"}
    assert child.tool_ids == frozenset({"read"})
    assert len(child.permission_planes) == 2
    assert len(child.guard_planes) == 1


def test_spawn_snapshot_is_canonical_and_missing_or_legacy_state_fails_loudly():
    raw = _plan_boundary().to_json()
    assert authority_for_spawn(raw) == raw
    assert parse_subagent_authority(raw).to_json() == raw

    with pytest.raises(SubagentAuthorityError, match="unsupported"):
        authority_for_spawn({})
    without_task = compose_subagent_authority(
        tool_ids={"read"},
        permission_rules=_allow_all(),
        guard_rules=[],
    )
    with pytest.raises(SubagentAuthorityError, match="Task is outside"):
        authority_for_spawn(without_task.to_json())


def test_snapshot_rejects_duplicate_or_unknown_authority_fields():
    raw = _plan_boundary().to_json()
    duplicate = {**raw, "tool_ids": [*raw["tool_ids"], raw["tool_ids"][0]]}
    with pytest.raises(SubagentAuthorityError, match="duplicate"):
        parse_subagent_authority(duplicate)
    with pytest.raises(SubagentAuthorityError, match="unsupported"):
        parse_subagent_authority({**raw, "future_widening": True})
