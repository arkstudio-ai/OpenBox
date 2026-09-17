"""A child Agent runs in the same durable project as its parent."""

from types import SimpleNamespace

import pytest

from tool.task import TaskArgs, execute
from tool.tool import ToolContext, ToolResult
from agent.agent import AgentDef, SUBAGENT_ALL_CAPABILITIES


@pytest.mark.asyncio
async def test_task_child_inherits_parent_project(monkeypatch):
    import agent.agent as agent_mod
    import session.session as session_mod
    import tool.task as task_mod

    parent = SimpleNamespace(model="openai/test-parent", project_id="project_parent")
    captured = {}

    monkeypatch.setattr(
        agent_mod,
        "get_agent",
        lambda _name: AgentDef(
            name="explore",
            description="test",
            mode="subagent",
            model=None,
            tools=["task"],
            subagent_capabilities=SUBAGENT_ALL_CAPABILITIES,
        ),
    )
    monkeypatch.setattr(
        agent_mod,
        "list_subagents",
        lambda: [SimpleNamespace(name="explore")],
    )

    async def get_session(*_args, **_kwargs):
        return parent

    activation = SimpleNamespace(id="activation-1", descriptor_id="subagent-1")
    fork_seed = SimpleNamespace(session_id="session_parent")

    async def accept_spawn(**kwargs):
        captured.update(kwargs)
        return activation

    async def dispatch(ctx, accepted, *, project_id):
        assert accepted is activation
        captured["dispatch_project"] = project_id
        return ToolResult(
            title="inspect",
            output="done",
            metadata={"child_session_id": "session_child"},
        )

    monkeypatch.setattr(session_mod, "get_session", get_session)
    async def freeze_fork(*_args, **_kwargs):
        return fork_seed

    monkeypatch.setattr(
        "session.event_range.freeze_fork_event_range",
        freeze_fork,
    )
    monkeypatch.setattr(
        "core.config.get_config",
        lambda: SimpleNamespace(
            model="openai/test-parent",
            models=[SimpleNamespace(
                id="openai/test-parent",
                provider="openai",
                subagent_capabilities=None,
                subagent_reasoning_variants=None,
            )],
            provider={
                "openai": {
                    "api_key": "test-key",
                    "base_url": "https://provider.invalid/v1",
                    "options": {},
                    "subagent_capabilities": ["model", "tool_filter"],
                    "subagent_reasoning_variants": [],
                }
            },
        ),
    )
    monkeypatch.setattr("agent.subagent_runtime.accept_spawn", accept_spawn)
    monkeypatch.setattr(task_mod, "_dispatch_activation", dispatch)
    monkeypatch.setattr("agent.driver.current_run_fence", lambda: (
        "session_parent", "parent-run", 3
    ))

    result = await execute(
        TaskArgs(
            action="fork",
            description="inspect",
            prompt="inspect it",
            subagent_type="explore",
        ),
        ToolContext(
            session_id="session_parent",
            user_id="user_parent",
            project_id="project_context_fallback",
            message_id="parent-message",
            part_id="parent-part",
            run_id="parent-run",
            _subagent_authority_snapshot={
                "version": 1,
                "tool_ids": ["task"],
                "permission_planes": [[{
                    "permission": "*",
                    "pattern": "*",
                    "action": "allow",
                }]],
                "guard_planes": [],
            },
        ),
    )

    # The gateway copies the locked parent project's identity internally;
    # task.py supplies the inherited parent model and exact parent fence only.
    assert captured["child_model"] == "openai/test-parent"
    assert captured["fork_seed"] is fork_seed
    assert captured["parent_session_id"] == "session_parent"
    assert captured["parent_run_id"] == "parent-run"
    assert captured["dispatch_project"] == "project_parent"
    assert result.metadata["child_session_id"] == "session_child"
