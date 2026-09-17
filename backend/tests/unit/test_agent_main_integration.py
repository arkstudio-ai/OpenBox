"""The durable kernel composes with main's workspace and question lifecycle."""
import pytest

from agent import driver
from db.models.question import SessionExecution
from db.models.session import Session
from question import runtime
from tests.unit.test_durable_questions import read, state  # noqa: F401
from tests.unit.test_run_fencing_api import loop_harness  # noqa: F401


async def test_driver_and_question_runtime_share_run_identity(state):
    lease = await driver.reserve_run("s1", "u1")
    try:
        ticket = await runtime.start_run("s1", "u1", driver_lease=lease)
        assert ticket is not None
        assert ticket.run_id == lease.run_id
        assert (await read(Session, "s1")).workspace_id == "w1"
        assert (await read(SessionExecution, "s1")).run_id == lease.run_id
        await runtime.finish_run(ticket, completed=True)
    finally:
        await lease.release()


async def test_durable_reservation_rejects_another_workspace_member(state):
    with pytest.raises(LookupError):
        await driver.reserve_run("s1", "u2")
    assert await driver.get_driver_state("s1") is None
    assert (await read(Session, "s1")).status == "idle"


async def test_current_main_turn_runs_through_the_durable_kernel(state, loop_harness, monkeypatch):
    import asyncio
    from session.session import create_user_message

    async def provider(**kwargs):
        yield {"type": "text_delta", "text": "A complete answer"}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(loop_harness.processor, "stream_llm", provider)
    await create_user_message("s1", "Hello", user_id="u1")
    answer = await asyncio.wait_for(loop_harness.loop.run_loop("s1", user_id="u1"), 5)
    assert answer is not None
    from session.session import get_messages
    saved = next(message for message in await get_messages("s1", user_id="u1") if message.id == answer.id)
    assert saved.finish == "stop"
    assert any(getattr(part, "text", None) == "A complete answer" for part in saved.parts)
    assert (await read(Session, "s1")).status == "idle"
    assert (await read(SessionExecution, "s1")).run_id is None
    assert (await driver.get_driver_state("s1")).phase == "idle"


async def test_question_answer_updates_canonical_history_with_driver_generation(state):
    from question import question
    from question.continuation import apply_answers
    from session.agent_event_log import load_canonical_model_surface
    from tests.unit.test_durable_questions import checkpoint

    lease = await driver.reserve_run("s1", "u1")
    ticket = await runtime.start_run("s1", "u1", driver_lease=lease)
    try:
        token = runtime.current_run.set(ticket)
        try:
            request_id = await checkpoint(part_id="p-generation")
        finally:
            runtime.current_run.reset(token)
        await runtime.finish_run(ticket)
        await lease.release()
        await question.reply(request_id, [["Yes"]], "u1")
        statuses = [data for event, data in state if event == "session.status"]
        assert statuses[-1]["status"] == "queued"
        assert statuses[-1]["generation"] == lease.generation
        await apply_answers("s1", "u1")
        surface = await load_canonical_model_surface("s1", user_id="u1", repair_tail=False)
        part = next(part for message in surface.messages for part in message.parts
                    if part.id == "p-generation")
        assert part.status == "completed"
        assert part.metadata["answers"] == [["Yes"]]
    finally:
        await runtime.finish_run(ticket)
        await lease.release()


def test_existing_provider_configuration_supports_basic_subagents():
    from types import SimpleNamespace
    from agent.agent import get_agent
    from agent.subagent_composition import build_subagent_composition
    from core.config import ProviderConfig

    config = SimpleNamespace(model="openai/gpt-4o", models=[], provider={
        "openai": ProviderConfig(api_key="test-key", base_url="https://provider.invalid/v1"),
    })
    composition = build_subagent_composition(
        agent_def=get_agent("explore"), parent_tool_ids=frozenset({"read", "task"}),
        config=config, inherited_model="openai/gpt-4o", requested_model=None,
        reasoning=None, persona=None, requested_tools=None, output_schema=None, seed_mode="fresh",
    )
    assert composition.model == "openai/gpt-4o"
    assert composition.tool_allowlist == frozenset({"read"})


def test_skill_catalogue_and_current_desktop_share_user_scope():
    from project.workspace import user_scope_for_identity
    from sandbox.client import SandboxClient, user_scope_for

    client = SandboxClient("desktop.invalid", 80, "test", user_scope=user_scope_for("workspace-owner"))
    assert user_scope_for_identity("workspace-owner") == client.user_scope


async def test_existing_desktop_path_probe_handles_quotes_and_canonical_aliases(tmp_path, monkeypatch):
    import asyncio
    import shlex
    import sys
    from types import SimpleNamespace
    from sandbox.client import PathResolveTarget, SandboxClient

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    owned = workspace / "a '$() \" 中文.txt"
    owned.write_text("owned")
    outside = tmp_path / "private.txt"
    outside.write_text("outside")
    (workspace / "escape").symlink_to(outside)
    client = SandboxClient("desktop.invalid", 80, "test")

    async def local_probe(command, **kwargs):
        # Execute the exact shipped probe in a disposable workspace, using
        # the same argument quoting as the Action Server's shell transport.
        args = shlex.split(command)
        assert args[:2] == ["python3", "-c"] and len(args) == 4
        script = args[2].replace("'/workspace'", repr(str(workspace)))
        process = await asyncio.create_subprocess_exec(sys.executable, "-c", script, args[3],
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        return SimpleNamespace(exit_code=process.returncode, stdout=stdout.decode(), stderr=stderr.decode())

    monkeypatch.setattr(client, "execute", local_probe)
    [resolved] = await client.resolve_paths([PathResolveTarget(str(owned))])
    assert resolved.canonical_path == str(owned)
    assert resolved.workspace_relative == owned.name
    [alias] = await client.resolve_paths([PathResolveTarget(str(workspace / "escape"))])
    assert alias.canonical_path == str(outside.resolve())
    assert alias.workspace_relative is None
    [missing] = await client.resolve_paths([PathResolveTarget(str(workspace / "new.txt"), allow_missing=True)])
    assert missing.workspace_relative == "new.txt"


async def test_shared_workspace_project_supports_fork_and_subagent(state):
    from db.base import get_db_session
    from db.models.project import Project
    from agent.subagent_authority import compose_subagent_authority
    from agent.subagent_runtime import accept_spawn
    from permission.permission import Rule
    from models.message import ToolPartData
    from session.session import create_user_message, create_assistant_message, update_message_info, save_part
    from session.fork import fork_session

    # Projects are shared within a workspace; their creator need not own the
    # current conversation. Session ownership remains enforced separately.
    async with get_db_session() as db:
        (await db.get(Project, "p1")).user_id = "u2"
    prompt = await create_user_message("s1", "First turn", user_id="u1")
    answer = await create_assistant_message("s1", prompt.id, user_id="u1")
    answer.finish = "stop"
    await update_message_info(answer, user_id="u1")
    fork = await fork_session("s1", user_id="u1")
    assert fork.workspace_id == "w1" and fork.project_id == "p1"
    prompt = await create_user_message("s1", "Delegate", user_id="u1")
    lease = await driver.reserve_run("s1", "u1", trigger_message_id=prompt.id)
    fence = (lease.session_id, lease.run_id, lease.generation)
    try:
        answer = await create_assistant_message("s1", prompt.id, user_id="u1", run_fence=fence)
        part = ToolPartData(tool="task", status="running", input={}, call_id="task-call",
                            message_id=answer.id, session_id="s1")
        await save_part(part, is_new=True, user_id="u1", run_fence=fence)
        child = await accept_spawn(user_id="u1", parent_session_id="s1", parent_message_id=answer.id,
            parent_part_id=part.id, parent_run_id=lease.run_id, parent_generation=lease.generation,
            task_title="Research", prompt="Find the answer", subagent_type="explore",
            child_model="openai/gpt-4o", lifecycle="one_shot", authority_snapshot=compose_subagent_authority(
                tool_ids=("task", "read"), permission_rules=[Rule(permission="*", pattern="*", action="allow")],
                guard_rules=(),
            ).to_json())
        assert (await read(Session, child.child_session_id)).workspace_id == "w1"
        async with get_db_session() as db:
            (await db.get(Project, "p1")).workspace_id = "another-workspace"
        with pytest.raises(ValueError, match="project"):
            await fork_session("s1", user_id="u1")
    finally:
        await lease.release()
