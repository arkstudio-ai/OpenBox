"""Permission probes use original call control before any human approval."""
import asyncio
import base64
import json
import shlex

import httpx
import pytest
from sqlalchemy import select

from agent.hooks import ToolHooks, current_tool_context
from db.base import close_engine, get_db_session, get_engine, init_engine
from db.models.external_effect import ExternalEffect
from db.models.resource_control import ResourceControlLease
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_resource_control import resource, close, drain  # noqa: F401
from tests.unit.test_assistant_resource_gateway import gateway, new_tool_call  # noqa: F401
from tests.unit.test_assistant_resource_commands import remote, accept  # noqa: F401
from tests.unit.test_assistant_sandbox_tool_scope import dispatch
from tool.apply_patch import apply_patch_tool
from tool.read import read_tool
from tool.tool import ToolResult
from tool.write import write_tool


async def effects_for(ctx):
    async with get_db_session() as db:
        return list((await db.scalars(select(ExternalEffect).where(
            ExternalEffect.session_id == ctx.session_id).order_by(ExternalEffect.created_at))).all())


@pytest.fixture
async def probes(gateway, monkeypatch):
    ctx, sent, transport = gateway
    permissions = []

    async def allow(**kwargs):
        permissions.append((kwargs, current_tool_context().part_id))

    monkeypatch.setattr("agent.hooks.perm_mod.ask", allow)

    async def respond(request):
        await transport(request)  # SQL must already contain submitting + resource identity.
        if request.url.path == "/execute":
            command = json.loads(request.content)["command"]
            if not command.startswith("python3 -c "):
                return httpx.Response(200, json={"exit_code": 0, "stderr": "", "stdout": "123"})
            targets = json.loads(base64.b64decode(shlex.split(command)[-1]))
            return httpx.Response(200, json={"exit_code": 0, "stderr": "", "stdout": json.dumps([
                {"canonical_path": t["path"], "workspace_relative": t["path"].removeprefix("/workspace/")}
                for t in targets])})
        if request.url.path == "/read_file":
            return httpx.Response(200, json={"content": "1: fixture"})
        return httpx.Response(200, json={"exit_code": 0, "stderr": "", "stdout": ""})

    ctx.sandbox._transport = httpx.MockTransport(respond)
    return ctx, sent, permissions, respond


async def prepare(ctx, tool_id, args, execute=None, *, part_id=None):
    hooks = ToolHooks(ctx.session_id, ctx.user_id)
    async def unused(*_args):
        return ToolResult(output="unused")
    prepared = await hooks.prepare_execute(tool_id, execute or unused, args, ctx,
        part_id=part_id or ctx.part_id, isolate_context=True)
    return hooks, prepared


@pytest.mark.parametrize("tool_id,args", [
    ("read", {"file_path": "/workspace/fixture.txt"}),
    ("write", {"file_path": "/workspace/fixture.txt", "content": "fixture"}),
    ("edit", {"file_path": "/workspace/fixture.txt", "old_string": "a", "new_string": "b"}),
    ("multiedit", {"file_path": "/workspace/fixture.txt", "edits": [{"old_string": "a", "new_string": "b"}]}),
    ("apply_patch", {"patch": "*** Begin Patch\n*** Add File: fixture.txt\n+fixture\n*** End Patch"}),
    ("grep", {"path": "/workspace", "pattern": "fixture"}),
    ("glob", {"path": "/workspace", "pattern": "*.txt"}),
])
async def test_every_canonical_probe_has_its_own_effect_and_correct_call_identity(probes, tool_id, args):
    ctx, sent, permissions, _ = probes
    await new_tool_call(ctx, tool_id, args)
    part_id = ctx.part_id
    ctx.part_id = "previous-sibling-part"
    hooks, prepared = await prepare(ctx, tool_id, args, part_id=part_id)
    assert prepared.blocked_result is None
    row, = await effects_for(ctx)
    assert (row.adapter, row.operation, row.state, row.attempt_count) == (
        "sandbox_preparation", "permission_paths", "succeeded", 1)
    assert row.safe_context["tool_part_id"] == part_id
    assert row.provider_receipt["remote_exclusivity_verified"] is False
    assert [request.url.path for request in sent] == ["/execute"]
    assert sent[0].headers["X-OpenBox-Resource-Operation"] == row.id
    assert permissions and all(part == part_id for _, part in permissions)
    await hooks.abandon_execute(prepared)


@pytest.mark.parametrize("tool", [read_tool, write_tool], ids=["read", "write"])
async def test_probe_and_actual_body_keep_distinct_effects(probes, tool):
    ctx, sent, _, _ = probes
    args = {"file_path": "/workspace/fixture.txt"}
    if tool is write_tool:
        args["content"] = "fixture"
    await new_tool_call(ctx, tool.id, args)
    hooks, prepared = await prepare(ctx, tool.id, args, tool.execute)
    result = await dispatch(hooks, prepared)
    assert not result.metadata.get("error")
    rows = await effects_for(ctx)
    assert {row.adapter for row in rows} == {"sandbox_preparation", "sandbox_tool"}
    assert len(rows) == 2 and all(row.state == "succeeded" for row in rows)
    assert sent[0].headers["X-OpenBox-Resource-Operation"] != sent[-1].headers["X-OpenBox-Resource-Operation"]


@pytest.mark.parametrize("change", ["hold", "epoch", "journal"])
async def test_stale_call_cannot_probe_or_request_approval(probes, resource, change):
    ctx, sent, permissions, _ = probes
    args = {"file_path": "/workspace/fixture.txt"}
    await new_tool_call(ctx, "read", args)
    if change == "hold":
        await close(resource)
    else:
        async with get_db_session() as db:
            row = await db.get(ResourceControlLease, resource[0].resource_id)
            if change == "epoch":
                row.epoch += 1
            else:
                row.remote_journal_id = "a" * 32
    _, prepared = await prepare(ctx, "read", args, read_tool.execute)
    assert prepared.blocked_result.metadata["canonical_validation"]
    assert not sent and not permissions and not await effects_for(ctx)


async def test_control_change_while_waiting_for_approval_still_blocks_body(probes, resource, monkeypatch):
    ctx, sent, _, _ = probes
    args = {"file_path": "/workspace/fixture.txt", "content": "fixture"}
    await new_tool_call(ctx, "write", args)
    async def allow_after_close(**_kwargs):
        row, = await effects_for(ctx)
        assert row.state == "succeeded"
        await close(resource)
    monkeypatch.setattr("agent.hooks.perm_mod.ask", allow_after_close)
    hooks, prepared = await prepare(ctx, "write", args, write_tool.execute)
    assert prepared.blocked_result is None
    result = await dispatch(hooks, prepared)
    assert result.metadata["error"]
    assert [r.url.path for r in sent] == ["/execute"]
    assert len(await effects_for(ctx)) == 1


async def test_nested_batch_probes_belong_to_their_original_children(probes):
    from tool.batch import batch_tool
    ctx, sent, permissions, _ = probes
    ctx._tool_execution_lookup = {"read": read_tool, "write": write_tool}
    ctx.available_tools = frozenset({"read", "write", "batch"})
    args = {"invocations": [
        {"tool": "read", "parameters": {"file_path": "/workspace/first.txt"}},
        {"tool": "write", "parameters": {"file_path": "/workspace/second.txt", "content": "fixture"}},
    ]}
    await new_tool_call(ctx, "batch", args)
    parent_part = ctx.part_id
    hooks, prepared = await prepare(ctx, "batch", args, batch_tool.execute)
    assert not (await dispatch(hooks, prepared)).metadata.get("error")
    rows = await effects_for(ctx)
    assert len(rows) == 4 and all(row.state == "succeeded" for row in rows)
    children = {row.safe_context["tool_part_id"] for row in rows}
    assert len(children) == 2 and parent_part not in children
    for child in children:
        assert {row.adapter for row in rows if row.safe_context["tool_part_id"] == child} == {
            "sandbox_preparation", "sandbox_tool"}
    assert {part for _, part in permissions} == children | {parent_part}
    assert {r.headers["X-OpenBox-Resource-Operation"] for r in sent} == {row.id for row in rows}


@pytest.mark.parametrize("patch", [
    "*** Begin Patch\n*** End Patch",
    "*** Begin Patch\n*** Add File: \n+x\n*** End Patch",
    "*** Begin Patch\n*** Add File: a\n+x",
    "*** Begin Patch\n*** Update File: a\n*** Move to: b\n*** End Patch",
    "*** Begin Patch\n*** Add File: a\n+x\n*** End Patch\n*** Add File: b\n+y\n*** End Patch",
])
async def test_malformed_patch_is_rejected_before_remote_probe_or_approval(probes, patch):
    ctx, sent, permissions, _ = probes
    args = {"patch": patch}
    await new_tool_call(ctx, "apply_patch", args)
    _, prepared = await prepare(ctx, "apply_patch", args, apply_patch_tool.execute)
    assert prepared.blocked_result.metadata["invalid_input"]
    assert not sent and not permissions and not await effects_for(ctx)


async def test_patch_checks_all_resolved_targets_before_any_approval(probes):
    from permission.permission import Rule
    ctx, sent, permissions, _ = probes
    ctx.workdir = "/workspace/project-one"
    args = {"patch": "*** Begin Patch\n*** Add File: ordinary.txt\n+fixture\n"
        "*** Add File: private.txt\n+fixture\n*** End Patch"}
    await new_tool_call(ctx, "apply_patch", args)
    hooks = ToolHooks(ctx.session_id, ctx.user_id,
        guard_rules=[Rule(permission="edit", pattern="/workspace/project-one/private.txt", action="deny")])
    prepared = await hooks.prepare_execute("apply_patch", apply_patch_tool.execute, args, ctx,
        part_id=ctx.part_id, isolate_context=True)
    assert prepared.blocked_result.metadata["blocked"]
    assert "platform policy" in prepared.blocked_result.output
    assert len(sent) == 1 and not permissions
    row, = await effects_for(ctx)
    assert row.adapter == "sandbox_preparation" and row.state == "succeeded"


@pytest.mark.parametrize("failure", ["timeout", "cancel", "malformed"])
async def test_lost_probe_blocks_approval_and_is_not_resent_after_database_reopen(probes, resource, failure):
    ctx, sent, permissions, respond = probes
    entered = asyncio.Event()
    async def fail(request):
        await respond(request)
        entered.set()
        if failure == "timeout":
            raise httpx.ReadTimeout("fixture response lost", request=request)
        if failure == "cancel":
            await asyncio.Event().wait()
        return httpx.Response(200, json={"exit_code": 0, "stderr": "", "stdout": "not-json"})
    ctx.sandbox._transport = httpx.MockTransport(fail)
    args = {"file_path": "/workspace/fixture.txt"}
    await new_tool_call(ctx, "read", args)
    pending = asyncio.create_task(prepare(ctx, "read", args, read_tool.execute))
    await asyncio.wait_for(entered.wait(), 5)
    if failure == "cancel":
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
    else:
        _, prepared = await pending
        assert prepared.blocked_result is not None
    row, = await effects_for(ctx)
    assert row.state == "outcome_unknown" and row.attempt_count == 1
    assert not permissions and len(sent) == 1
    url = get_engine().url.render_as_string(hide_password=False)
    await close_engine()
    init_engine(url)
    ctx.sandbox._transport = httpx.MockTransport(respond)
    _, retry = await prepare(ctx, "read", args, read_tool.execute)
    assert retry.blocked_result is not None
    assert not permissions and len(sent) == 1
    assert (await drain(resource))["blocking_effect_ids"] == [row.id]


@pytest.mark.parametrize("patching", [False, True], ids=["read", "relative_patch"])
async def test_real_v2_server_probes_then_executes_with_distinct_pinned_receipts(remote, resource, tmp_path, monkeypatch, patching):
    from assistant import resource_commands as commands
    ctx = remote[2]
    command = await accept(remote, resource, "bind")
    assert await commands.dispatch(command["command_id"])
    target = tmp_path / "probe-fixture.txt"
    if not patching:
        target.write_text("permission probe fixture")
    ctx.workdir = str(tmp_path)
    tool = apply_patch_tool if patching else read_tool
    args = {"patch": "*** Begin Patch\n*** Add File: probe-fixture.txt\n+permission probe fixture\n*** End Patch"} if patching else {"file_path": str(target)}
    await new_tool_call(ctx, tool.id, args)
    async def allow(**_kwargs):
        return None
    monkeypatch.setattr("agent.hooks.perm_mod.ask", allow)
    hooks, prepared = await prepare(ctx, tool.id, args, tool.execute)
    assert prepared.blocked_result is None
    result = await dispatch(hooks, prepared)
    assert not result.metadata.get("error")
    assert target.read_text() == "permission probe fixture"
    assert (str(target) if patching else "permission probe fixture") in result.output
    rows = await effects_for(ctx)
    assert len(rows) == 2 and all(row.state == "succeeded" for row in rows)
    assert all(row.safe_context["resource_journal_id"] == remote[1].status()["journal_id"] for row in rows)
