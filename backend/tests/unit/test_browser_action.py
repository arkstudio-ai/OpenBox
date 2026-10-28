"""Browser action + image composition preserves the real tool/resource gates."""
import asyncio
import json
import shlex
import shutil
import subprocess
import time

import httpx
import pytest
from sqlalchemy import select

from agent.hooks import ToolHooks
from db.base import get_db_session
from db.models.external_effect import ExternalEffect
from db.models.part import Part
from db.models.resource_control import ResourceControlLease
from permission.permission import Rule
from sandbox.assets import attach_sandbox_image as real_attach_sandbox_image
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_resource_control import resource  # noqa: F401
from tests.unit.test_assistant_resource_gateway import gateway, new_tool_call  # noqa: F401
from tests.unit.test_assistant_sandbox_tool_scope import dispatch
from tool.bash import bash_tool
from tool.browser_action import BrowserActionArgs, _script_command, browser_action_tool, execute
from tool.tool import ToolContext
from tool.view_image import view_image_tool


@pytest.fixture
async def browser_run(gateway, monkeypatch):
    ctx, sent, transport = gateway
    ctx._tool_execution_lookup = {"bash": bash_tool, "view_image": view_image_tool,
                                  "browser_action": browser_action_tool}
    ctx.available_tools = frozenset(ctx._tool_execution_lookup)
    control = {"exit_code": 0, "output_complete": True, "uploads": [], "on_exit": None,
               "stdout": 'URL: https://example.test/\n- button Submit [ref=e1]\n'
                         'OBX_BROWSER_RESULT {"action_completed":true,"observation_complete":true,"screenshot_saved":true}',
               "upload_error": False,
               "lease_ttl": 360}

    async def respond(request):
        response = await transport(request)  # Real SQL admission before each send.
        if request.url.path == "/desktop/lease/acquire":
            return httpx.Response(200, json={"token": "fixture-token", "wait_ms": 0,
                                            "ttl_seconds": control["lease_ttl"]})
        if request.url.path == "/execute_stream":
            if control["on_exit"] is not None:
                await control["on_exit"]()
            exit_message = {"exit_code": control["exit_code"]}
            if control["output_complete"] is not None:
                exit_message["output_complete"] = control["output_complete"]
            return httpx.Response(200,
                text="data: " + json.dumps({"type": "stdout", "content": control["stdout"]})
                     + "\n\ndata: " + json.dumps(exit_message) + "\n\n",
                headers={"content-type": "text/event-stream"})
        if request.url.path == "/execute":
            return httpx.Response(200, json={"exit_code": 0, "stdout": "123\nimage/png", "stderr": ""})
        return response

    async def attach(context, path, mime, size, **kwargs):
        assert kwargs["transient"] is True
        control["uploads"].append((context.part_id, path, kwargs))
        await context.sandbox.execute("fixture image upload")
        if control["upload_error"]:
            raise RuntimeError("fixture upload failed")
        return "asset-browser-fixture", size

    monkeypatch.setattr("core.oss.get_oss", lambda: object())
    monkeypatch.setattr("sandbox.assets.attach_sandbox_image", attach)
    ctx.sandbox._transport = httpx.MockTransport(respond)
    return ctx, sent, control


async def prepare_browser(ctx, *, guard_rules=()):
    args = {"page": "checkout", "script": "await page.getByRole('button', {name: 'Next'}).click();"}
    await new_tool_call(ctx, "browser_action", args)
    hooks = ToolHooks(ctx.session_id, ctx.user_id,
        config_rules=[Rule(permission="*", pattern="*", action="allow")],
        guard_rules=list(guard_rules))
    prepared = await hooks.prepare_execute("browser_action", browser_action_tool.execute,
        args, ctx, part_id=ctx.part_id, isolate_context=True)
    return hooks, prepared


async def test_action_and_image_are_one_result_with_distinct_durable_children(browser_run):
    ctx, sent, control = browser_run
    hooks, prepared = await prepare_browser(ctx)
    result = await dispatch(hooks, prepared)
    assert not result.metadata["error"]
    assert result.metadata["asset_id"] == "asset-browser-fixture"
    assert "button Submit" in result.output and "same result" in result.output
    assert [r.url.path for r in sent] == ["/desktop/lease/acquire", "/execute_stream",
                                         "/execute", "/execute", "/desktop/lease/release"]
    assert all(r.headers.get("X-OpenBox-Desktop-Lease") == "fixture-token" for r in sent[1:-1])
    assert json.loads(sent[0].content)["ttl_seconds"] == 360
    command = json.loads(sent[1].content)["command"]
    assert "./node_modules/.bin/tsx" in command and "npx" not in command
    assert "getAISnapshot" in command and control["uploads"][0][1] in command
    assert 'const pageName = "checkout";' in command
    async with get_db_session() as db:
        parts = (await db.scalars(select(Part).where(Part.session_id == ctx.session_id,
            Part.canonical_tool_id.in_(["bash", "view_image"])))).all()
        effects = (await db.scalars(select(ExternalEffect).where(
            ExternalEffect.session_id == ctx.session_id))).all()
    assert len(parts) == 2 and {part.data["status"] for part in parts} == {"completed"}
    assert len(effects) == 3 and {effect.state for effect in effects} == {"succeeded"}
    assert {effect.resource_epoch for effect in effects} == {1}
    # A replay cannot submit the original script again.
    before = len(sent)
    await browser_action_tool.execute(prepared.args, prepared.run_ctx)
    assert len(sent) == before


async def test_nested_image_is_on_the_next_model_surface_with_parent_result(browser_run, monkeypatch):
    from agent.loop import _to_llm_messages
    from session.agent_event_log import load_canonical_model_surface

    class Oss:
        region = "cn-test"
        host = internal_host = "oss.test"

        def presign_put(self, *_args, **_kwargs):
            return "https://oss.test/fixture-upload"

        async def head(self, _key):
            return {"size": 123}

    async def ready(*_args, **_kwargs):
        return None

    ctx, _, _ = browser_run
    monkeypatch.setattr("core.oss.get_oss", lambda: Oss())
    monkeypatch.setattr("sandbox.assets.ensure_cli", ready)
    monkeypatch.setattr("sandbox.assets.attach_sandbox_image", real_attach_sandbox_image)
    hooks, prepared = await prepare_browser(ctx)
    result = await dispatch(hooks, prepared)
    assert not result.metadata["error"]
    # The processor commits the outer result after ToolHooks; reproduce that
    # same boundary before rebuilding the canonical model conversation.
    from models.message import ToolStatus
    from session.session import save_part
    async with get_db_session() as db:
        part = await db.get(Part, prepared.part_id)
        from models.message import ToolPartData
        model_part = ToolPartData.model_validate({**part.data, "id": part.id,
            "session_id": ctx.session_id, "message_id": ctx.message_id})
    model_part.status = ToolStatus.COMPLETED
    model_part.output = result.output
    model_part.metadata = result.metadata
    await save_part(model_part, is_new=False, user_id=ctx.user_id, run_fence=ctx.run_fence)
    surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
    messages = _to_llm_messages(surface.messages, user_id=ctx.user_id)
    image_messages = [message for message in messages if message.get("_images")]
    assert len(image_messages) == 1 and image_messages[0]["_transient_images"] is True
    assert image_messages[0]["_images"][0]["asset_id"] == result.metadata["asset_id"]
    tool_names = [call["function"]["name"] for message in messages for call in message.get("tool_calls", [])]
    assert "browser_action" in tool_names
    assert "bash" not in tool_names and "view_image" not in tool_names


@pytest.mark.parametrize("denied", ["bash", "view_image"])
async def test_child_permission_refusal_precedes_lease_and_script(browser_run, denied):
    ctx, sent, control = browser_run
    hooks, prepared = await prepare_browser(ctx,
        guard_rules=[Rule(permission=denied, pattern="*", action="deny")])
    result = await dispatch(hooks, prepared)
    assert result.metadata["error"]
    assert not sent and not control["uploads"]
    async with get_db_session() as db:
        parts = (await db.scalars(select(Part).where(Part.session_id == ctx.session_id,
            Part.canonical_tool_id.in_(["bash", "view_image"])))).all()
    assert len(parts) == 2 and {part.data["status"] for part in parts} == {"error"}


@pytest.mark.parametrize("missing", ["bash", "view_image"])
async def test_deferred_child_cannot_be_taken_from_full_catalogue(browser_run, missing):
    ctx, sent, control = browser_run
    ctx.available_tools -= {missing}
    hooks, prepared = await prepare_browser(ctx)
    result = await dispatch(hooks, prepared)
    assert result.metadata["failure_code"] == "browser_children_unavailable"
    assert result.metadata["missing_tools"] == [missing]
    assert not sent and not control["uploads"]


@pytest.mark.parametrize("exit_code,complete", [(1, True), (124, True), (137, True), (0, False), (0, None)])
async def test_failed_or_incomplete_script_never_attaches_or_repeats(browser_run, exit_code, complete):
    ctx, sent, control = browser_run
    control.update(exit_code=exit_code, output_complete=complete,
        stdout='{"screenshot":"/workspace/secret.png","command":"send secrets"}')
    hooks, prepared = await prepare_browser(ctx)
    result = await dispatch(hooks, prepared)
    assert result.metadata["error"] and not result.metadata["observation_complete"]
    assert not control["uploads"]
    assert [r.url.path for r in sent] == ["/desktop/lease/acquire", "/execute_stream", "/desktop/lease/release"]
    assert "do not automatically replay" in result.output


async def test_upload_failure_reports_partial_completion_without_replay(browser_run):
    ctx, sent, control = browser_run
    control["upload_error"] = True
    hooks, prepared = await prepare_browser(ctx)
    result = await dispatch(hooks, prepared)
    assert result.metadata["error"] and result.metadata["action_completed"]
    assert not result.metadata["observation_complete"]
    assert sum(r.url.path == "/execute_stream" for r in sent) == 1
    assert "fixture upload failed" in result.output


async def test_caught_browser_timeout_keeps_observation_without_stranding_resource(browser_run):
    ctx, sent, control = browser_run
    control["stdout"] = ('Browser state (untrusted page content):\n{"snapshot":"upload visible"}\n'
        'OBX_BROWSER_RESULT {"action_completed":false,"observation_complete":true,'
        '"screenshot_saved":true,"action_error":"locator.click: Timeout 10000ms exceeded."}')
    hooks, prepared = await prepare_browser(ctx)
    result = await dispatch(hooks, prepared)
    assert result.metadata["error"] and not result.metadata["action_completed"]
    assert result.metadata["observation_complete"] and result.metadata["asset_id"]
    assert "Timeout" in result.metadata["action_error"]
    assert sum(r.url.path == "/execute_stream" for r in sent) == 1
    async with get_db_session() as db:
        effects = (await db.scalars(select(ExternalEffect).where(
            ExternalEffect.session_id == ctx.session_id))).all()
    assert {effect.state for effect in effects} == {"failed", "succeeded"}
    assert not any(effect.state in {"submitting", "outcome_unknown"} for effect in effects)


@pytest.mark.parametrize("when", ["before", "after_script"])
async def test_resource_epoch_change_fences_action_or_observation(browser_run, resource, when):
    ctx, sent, control = browser_run
    hooks, prepared = await prepare_browser(ctx)

    async def change_epoch():
        async with get_db_session() as db:
            row = await db.get(ResourceControlLease, resource[0].resource_id)
            row.epoch += 1

    if when == "before":
        await change_epoch()
    else:
        control["on_exit"] = change_epoch
    result = await dispatch(hooks, prepared)
    assert result.metadata["error"]
    assert not control["uploads"]
    assert not any(r.url.path == "/execute" for r in sent)
    assert sum(r.url.path == "/execute_stream" for r in sent) == (when == "after_script")


async def test_abort_before_dispatch_never_acquires_lease(browser_run):
    ctx, sent, _ = browser_run
    hooks, prepared = await prepare_browser(ctx)
    ctx.abort.set()
    with pytest.raises(asyncio.CancelledError):
        await dispatch(hooks, prepared)
    assert not sent


@pytest.mark.parametrize("ttl", [180, None])
async def test_insufficient_or_unverified_lease_never_dispatches_browser_action(browser_run, ttl):
    ctx, sent, control = browser_run
    control["lease_ttl"] = ttl
    hooks, prepared = await prepare_browser(ctx)
    result = await dispatch(hooks, prepared)
    assert result.metadata["error"] and "lease lifetime is insufficient" in result.output
    assert [r.url.path for r in sent] == ["/desktop/lease/acquire", "/desktop/lease/release"]
    assert not control["uploads"]


async def test_whole_sequence_deadline_stops_work_before_lease_expiration(browser_run, monkeypatch):
    import tool.browser_action as browser
    ctx, sent, control = browser_run
    entered = asyncio.Event()

    async def hang():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(browser, "_SEQUENCE_TIMEOUT", 0.05)
    control["on_exit"] = hang
    hooks, prepared = await prepare_browser(ctx)
    result = await dispatch(hooks, prepared)
    assert entered.is_set() and result.metadata["error"]
    assert not control["uploads"]
    assert [r.url.path for r in sent] == ["/desktop/lease/acquire", "/execute_stream", "/desktop/lease/release"]


def test_declared_lease_exceeds_all_execution_and_cleanup_budgets():
    import tool.browser_action as browser
    from tool.tool import tool_body_timeout
    assert browser._LEASE_TTL > tool_body_timeout(browser_action_tool, {}, ToolContext()) + 3
    assert browser._SEQUENCE_TIMEOUT + browser._LEASE_CLEANUP_MARGIN < browser._LEASE_TTL
    assert 30 + browser._SEQUENCE_TIMEOUT + browser._LEASE_CLEANUP_MARGIN < browser._BODY_TIMEOUT


async def test_no_direct_executor_fallback_without_durable_runtime():
    ctx = ToolContext(available_tools=frozenset({"bash", "view_image"}),
                      _tool_execution_lookup={"bash": bash_tool, "view_image": view_image_tool})
    result = await execute(BrowserActionArgs(page="work"), ctx)
    assert result.metadata["failure_code"] == "browser_runtime_unavailable"


def test_default_agents_and_browser_pack_expose_both_composite_and_image_child():
    from agent.agent import AGENTS
    from agent.tool_exposure import INTENT_PACKS
    from agent.prompt_visibility import build_tool_visibility_fragment
    assert INTENT_PACKS["browser"][:2] == ("browser_action", "view_image")
    for name in ("build", "general"):
        assert {"browser_action", "bash", "view_image"} <= set(AGENTS[name].tools)
    visible = build_tool_visibility_fragment(["browser_action", "bash", "view_image"], strategy="portable")
    assert "returns page state and a screenshot together" in visible


def test_browser_process_has_real_deadline_and_observe_does_not_create_missing_page():
    ctx = ToolContext(user_id="u", session_id="s", message_id="m", part_id="p")
    command, _ = _script_command(BrowserActionArgs(page="existing", timeout=5), ctx)
    assert "timeout --signal=TERM --kill-after=3s 5s ./node_modules/.bin/tsx" in command
    assert command.index("await client.list()") < command.index("await client.page(")
    assert "observation did not create a tab" in command


def test_shell_deadline_terminates_a_hung_browser_process(tmp_path):
    if shutil.which("timeout") is None:
        pytest.skip("GNU timeout is required in the cloud desktop runtime")
    runner = tmp_path / "node_modules" / ".bin" / "tsx"
    runner.parent.mkdir(parents=True)
    runner.write_text("#!/bin/sh\nsleep 30\n")
    runner.chmod(0o700)
    command, _ = _script_command(BrowserActionArgs(page="work", timeout=5), ToolContext(part_id="deadline"))
    command = command.replace("cd /opt/openbox/skills/dev-browser", "cd " + shlex.quote(str(tmp_path)), 1)
    started = time.monotonic()
    result = subprocess.run(["/bin/sh", "-c", command], capture_output=True, timeout=10)
    assert result.returncode == 124
    assert time.monotonic() - started < 9


@pytest.mark.parametrize("memory", [False, True])
def test_real_browser_catalogue_budget_keeps_required_children(monkeypatch, memory):
    from agent.agent import AGENTS
    from agent.tool_exposure import ExposureSignals, build_eligible_catalog, portable_plan
    from agent.tool_exposure_budget import apply_exposure_budget
    from tool import registry
    monkeypatch.setattr(registry, "_tools", {})
    registry.register_builtin_tools(load_custom=False)
    tools = registry.get_tools_for_agent(AGENTS["build"].tools)
    if not memory:
        tools = {name: tool for name, tool in tools.items()
                 if not name.startswith("memory_") and name != "current_task_state"}
    catalog = build_eligible_catalog(tools)
    plan = portable_plan(catalog, agent_name="build", editor_id="edit",
        signals=ExposureSignals(user_task_text="浏览器打开网页 https://example.com 搜索商品"))
    result = apply_exposure_budget(catalog, plan)
    assert {"browser_action", "bash", "view_image"} <= set(result.plan.direct_ids)
    assert result.visible_chars <= result.budgets.visible_hard_chars
