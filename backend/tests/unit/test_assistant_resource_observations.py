"""Actual computer, image projection, SQL checkpoints and HTTP admission."""
import asyncio
import base64
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
import struct
from types import SimpleNamespace
import zlib

import httpx
import pytest
from sqlalchemy import select

from agent import effect_ledger as effects, loop
from assistant.policy import AssistantError
from db.base import close_engine, get_db_session, get_engine, init_engine
from db.models.agent_event import AgentEvent
from db.models.external_effect import ExternalEffect
from db.models.file_asset import FileAsset
from db.models.part import Part
from db.models.resource_control import ResourceControlLease
from sandbox.client import SandboxClient
from sandbox.resource_operation import prepare_desktop_tool
from session.agent_event_log import load_canonical_model_surface
from tool import computer
from tool.tool import ToolContext
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_resource_control import drain, resource  # noqa: F401
from tests.unit.test_assistant_resource_gateway import new_tool_call


@pytest.fixture
async def screen(resource, monkeypatch):
    _, run, _, enrollment = resource
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    raw = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1280, 720, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff" * (1280 * 3)) * 720)) + chunk(b"IEND", b""))
    geometry = {"native": [1920, 1080], "scaled": [1280, 720],
        "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw), "stable": True}
    sent = []

    async def subscribed(*_args):
        pass

    async def head(_key):
        return {"size": len(raw)}

    async def transport(request):
        sent.append(request)
        if request.url.path == "/desktop/lease/acquire":
            return httpx.Response(200, json={"token": "screen-fixture", "wait_ms": 0})
        command = json.loads(request.content).get("command", "")
        stdout = json.dumps(geometry) if "obx-shot " in command else "ok"
        return httpx.Response(200, json={"exit_code": 0, "stdout": stdout, "stderr": ""})

    monkeypatch.setattr("sandbox.entitlement.require_sandbox_subscription", subscribed)
    monkeypatch.setattr("sandbox.assets.ensure_cli", subscribed)
    monkeypatch.setattr(computer, "_prepare", subscribed)
    monkeypatch.setattr("sandbox.assets._use_internal_oss", lambda _: False)
    monkeypatch.setattr("core.oss.get_oss", lambda: SimpleNamespace(head=head,
        presign_put=lambda *_a, **_kw: "https://fixture.invalid/image"))
    monkeypatch.setattr(loop, "_IMAGE_CACHE", {})
    client = SandboxClient("screen.invalid", 80, "fixture-key", desktop_id=enrollment["desktop_id"],
        workspace_id=enrollment["workspace_id"], reuse_connections=True)
    client._transport = httpx.MockTransport(transport)
    ctx = ToolContext(session_id=run.session_id, user_id=run.tenant_id, sandbox=client,
        workspace_id=enrollment["workspace_id"], run_id=run.run_id, run_generation=run.generation)
    fixture = SimpleNamespace(ctx=ctx, sent=sent, raw=raw, geometry=geometry)
    yield fixture
    await client.aclose()


async def observed(screen):
    ctx = screen.ctx
    await new_tool_call(ctx, "computer", {"action": "screenshot"}, resource_images=[])
    result = await computer.execute(computer.ComputerArgs(action="screenshot"), ctx)
    assert not result.metadata.get("error"), result.output
    async with get_db_session() as db:
        event = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == ctx.session_id,
            AgentEvent.kind == "resource.observed").order_by(AgentEvent.sequence.desc()).limit(1))
        assert event is not None
        loop._IMAGE_CACHE[event.payload["asset_id"]] = "data:image/png;base64," + base64.b64encode(screen.raw).decode()
    return event


async def call(screen, *, action="left_click", images=True, model=None):
    ctx = screen.ctx
    surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
    projected = loop._to_llm_messages(surface.messages, user_id=ctx.user_id)
    evidence = {}
    wire = await loop.resolve_images(projected, model, resource_images=evidence)
    args = {"action": action}
    if action == "left_click":
        args["coordinate"] = [100, 200]
    await new_tool_call(ctx, "computer", args, resource_images=list(evidence.values()) if images else [])
    async with get_db_session() as db:
        request = await db.scalar(select(AgentEvent).where(AgentEvent.message_id == ctx.message_id,
            AgentEvent.kind == "model.requested"))
    return computer.ComputerArgs(**args), request, wire


async def execute(screen, args):
    return await computer.execute(args, screen.ctx)


async def test_capture_actual_image_checkpoint_and_click_use_durable_geometry_after_restart(screen, resource):
    observation = await observed(screen)
    args, request, wire = await call(screen)
    assert observation.payload["eligible"]
    ref = request.payload["resource_context"]["observation"]
    assert ref["event_id"] == observation.id
    assert any(msg.get("_images") for msg in wire)
    prepared, _, _ = await prepare_desktop_tool(screen.ctx, args)
    await resource[2].release(session_status="idle")
    url = str(get_engine().url.render_as_string(hide_password=False))
    await close_engine()
    init_engine(url)
    from agent.driver import reserve_run
    restored = await reserve_run(screen.ctx.session_id, screen.ctx.user_id)
    await restored.set_phase("running")
    screen.ctx = replace(screen.ctx, run_id=restored.run_id, run_generation=restored.generation)
    # Poison the process-local cache: dispatch must use the shown frame's
    # original dimensions, not fresh guessed geometry or this cache.
    computer._geometry_cache[computer._sandbox_key(screen.ctx)] = {"native": [100, 100], "scaled": [100, 100]}
    try:
        result = await execute(screen, args)
        assert not result.metadata.get("error"), result.output
        commands = [json.loads(r.content).get("command", "") for r in screen.sent]
        assert any("xdotool mousemove 150 300 click" in command for command in commands)
        async with get_db_session() as db:
            row = await db.get(ExternalEffect, prepared.snapshot.effect_id)
            assert row.state == "succeeded" and row.safe_context["resource_observation"] == ref
            assert row.run_id == restored.run_id and row.attempt_count == 1
    finally:
        await restored.release(session_status="idle")


@pytest.mark.parametrize("missing", ["omitted", "text_only", "changed_bytes"])
async def test_tool_cannot_use_an_image_absent_from_its_actual_provider_input(screen, missing):
    event = await observed(screen)
    if missing == "changed_bytes":
        loop._IMAGE_CACHE[event.payload["asset_id"]] = "data:image/png;base64,Y2hhbmdlZA=="
    args, request, wire = await call(screen, images=missing != "omitted",
        model="openai/deepseek-v4-flash" if missing == "text_only" else None)
    assert request.payload["resource_context"]["observation"] is None
    if missing == "text_only":
        assert not any(msg.get("_images") for msg in wire)
    before = len(screen.sent)
    result = await execute(screen, args)
    assert result.metadata["error"] and len(screen.sent) == before


async def test_missing_observation_blocks_before_permission_and_requests_a_screenshot(screen, monkeypatch):
    from agent.hooks import ToolHooks
    args, _, _ = await call(screen)
    hooks = ToolHooks(screen.ctx.session_id, screen.ctx.user_id)

    async def unexpected(*_args):
        pytest.fail("An unobserved operation reached permission approval")

    monkeypatch.setattr(hooks, "authorize_tool", unexpected)
    prepared = await hooks.prepare_execute("computer", computer.computer_tool.execute,
        args.model_dump(mode="json"), screen.ctx, part_id=screen.ctx.part_id, isolate_context=True)
    assert prepared.blocked_result.metadata["error_code"] == "RESOURCE_OBSERVATION_REQUIRED"
    assert prepared.blocked_result.metadata["observation_reason"] == "request_without_observation"
    assert "screenshot" in prepared.blocked_result.output
    assert not screen.sent


@pytest.mark.parametrize("change", ["asset", "file", "epoch", "journal"])
async def test_original_frame_is_rechecked_before_first_send(screen, resource, change):
    event = await observed(screen)
    args, _, _ = await call(screen)
    async with get_db_session() as db:
        if change == "asset":
            row = await db.get(FileAsset, event.payload["asset_id"])
            row.is_deleted = True
        elif change == "file":
            row = await db.get(Part, event.payload["file_part_id"])
            row.data = {**row.data, "path": "/different-frame.png"}
        else:
            row = await db.get(ResourceControlLease, resource[0].resource_id)
            if change == "epoch":
                row.epoch += 1
            else:
                row.remote_journal_id = "b" * 32
    before = len(screen.sent)
    assert (await execute(screen, args)).metadata["error"]
    assert len(screen.sent) == before


async def test_new_control_epoch_cannot_relabel_old_frame_but_fresh_capture_allows_input(screen, resource):
    old = await observed(screen)
    async with get_db_session() as db:
        row = await db.get(ResourceControlLease, resource[0].resource_id)
        row.epoch += 1
    args, request, _ = await call(screen)
    assert request.payload["resource_context"]["fence"]["epoch"] == 2
    assert request.payload["resource_context"]["observation"] is None
    assert (await execute(screen, args)).metadata["error"]
    fresh = await observed(screen)
    assert fresh.id != old.id and fresh.payload["fence"]["epoch"] == 2
    args, request, _ = await call(screen)
    assert request.payload["resource_context"]["observation"]["event_id"] == fresh.id
    assert not (await execute(screen, args)).metadata.get("error")


async def test_new_driver_model_request_requires_its_own_observation(screen, resource):
    from agent.driver import reserve_run
    await observed(screen)
    await resource[2].release(session_status="idle")
    resumed = await reserve_run(screen.ctx.session_id, screen.ctx.user_id)
    await resumed.set_phase("running")
    screen.ctx = replace(screen.ctx, run_id=resumed.run_id, run_generation=resumed.generation)
    try:
        args, request, wire = await call(screen)
        assert any(msg.get("_images") for msg in wire)
        assert request.payload["resource_context"]["observation"] is None
        before = len(screen.sent)
        assert (await execute(screen, args)).metadata["error"] and len(screen.sent) == before
    finally:
        await resumed.release(session_status="idle")


async def test_other_operation_during_permission_wait_invalidates_prepared_coordinates(screen, resource, monkeypatch):
    from agent.hooks import ToolHooks
    from sandbox.resource_operation import prepare_desktop_tool
    await observed(screen)
    args, _, _ = await call(screen)
    hooks = ToolHooks(screen.ctx.session_id, screen.ctx.user_id)
    waiting, approved = asyncio.Event(), asyncio.Event()

    async def approval(*_args):
        waiting.set()
        await approved.wait()

    monkeypatch.setattr(hooks, "authorize_tool", approval)
    pending = asyncio.create_task(hooks.prepare_execute("computer", computer.computer_tool.execute,
        args.model_dump(mode="json"), screen.ctx, part_id=screen.ctx.part_id, isolate_context=True))
    try:
        await asyncio.wait_for(waiting.wait(), 10)
        other = await effects.prepare_effect(resource[1], adapter="sandbox_tool", provider="wuying",
            operation="tool_execute", logical_key="intervening-shell", request_payload={},
            resource_fence=resource[0])
        claim = await effects.claim_effect_for_dispatch(other.snapshot.effect_id, resource[1])
        await effects.mark_effect_submitting(claim)
        await effects.settle_effect(claim, state="succeeded", receipt={"fixture": True})
        approved.set()
        prepared = await pending
        assert prepared.blocked_result is None
        before = len(screen.sent)
        outcome = await hooks.dispatch_execute(prepared)
        assert outcome.result.metadata["error"]
        assert len(screen.sent) == before
        with pytest.raises(AssistantError, match="screenshot"):
            await prepare_desktop_tool(screen.ctx, args)
    finally:
        approved.set()
        await pending


async def test_two_calls_for_one_observed_frame_have_only_one_admission(screen, resource):
    from sandbox.resource_operation import prepare_desktop_tool
    await observed(screen)
    args, _, _ = await call(screen)
    first_ctx = replace(screen.ctx)
    first, _, _ = await prepare_desktop_tool(first_ctx, args)
    args, _, _ = await call(screen)
    second, _, _ = await prepare_desktop_tool(screen.ctx, args)
    claims = [await effects.claim_effect_for_dispatch(p.snapshot.effect_id, resource[1])
        for p in (first, second)]
    outcomes = await asyncio.gather(*(effects.mark_effect_submitting(c) for c in claims), return_exceptions=True)
    assert sum(outcome is None for outcome in outcomes) == 1
    async with get_db_session() as db:
        rows = [await db.get(ExternalEffect, p.snapshot.effect_id) for p in (first, second)]
        assert sorted(row.attempt_count for row in rows) == [0, 1]


async def test_revoked_image_after_lease_acquire_blocks_input_but_releases_exact_token(screen):
    event = await observed(screen)
    args, _, _ = await call(screen)
    transport = screen.ctx.sandbox._transport

    async def revoke(request):
        response = await transport.handle_async_request(request)
        if request.url.path == "/desktop/lease/acquire":
            async with get_db_session() as db:
                asset = await db.get(FileAsset, event.payload["asset_id"])
                asset.is_deleted = True
        return response

    screen.ctx.sandbox._transport = httpx.MockTransport(revoke)
    before = len(screen.sent)
    assert (await execute(screen, args)).metadata["error"]
    assert [r.url.path for r in screen.sent[before:]] == ["/desktop/lease/acquire", "/desktop/lease/release"]


async def test_unknown_competing_operation_keeps_screenshot_non_actionable(screen, resource):
    pending = await effects.prepare_effect(resource[1], adapter="sandbox_tool", provider="wuying",
        operation="tool_execute", logical_key="unknown-shell", request_payload={}, resource_fence=resource[0])
    claim = await effects.claim_effect_for_dispatch(pending.snapshot.effect_id, resource[1])
    await effects.mark_effect_submitting(claim)
    await effects.record_effect_outcome_unknown(claim, error={"code": "fixture-response-loss"})
    image = await observed(screen)
    assert image.payload["eligible"] is False
    assert (image.payload["competing_effect_id"], image.payload["competing_state"]) == (
        pending.snapshot.effect_id, "outcome_unknown")
    assert await request_reason(screen, resource, [image_input(screen, image)]) == (
        f"competing:{pending.snapshot.effect_id}/outcome_unknown")
    args, request, wire = await call(screen)
    assert any(msg.get("_images") for msg in wire)
    assert request.payload["resource_context"]["observation"] is None
    before = len(screen.sent)
    result = await execute(screen, args)
    assert result.metadata["error"] and len(screen.sent) == before
    assert result.metadata["observation_reason"] == "request_without_observation"


@pytest.mark.parametrize("other", ["manual_review", "catalogue_read_in_flight"])
async def test_parked_or_read_only_operation_does_not_make_screenshot_non_actionable(screen, resource, other):
    """A parked unknown is terminal; a fixed catalogue GET cannot change the screen."""
    adapter, operation = (("sandbox_tool", "tool_execute") if other == "manual_review"
                          else ("sandbox_runtime", "catalogue_read"))
    pending = await effects.prepare_effect(resource[1], adapter=adapter, provider="wuying",
        operation=operation, logical_key="other-operation", request_payload={}, resource_fence=resource[0])
    claim = await effects.claim_effect_for_dispatch(pending.snapshot.effect_id, resource[1])
    await effects.mark_effect_submitting(claim)
    if other == "manual_review":
        await effects.record_effect_outcome_unknown(claim, error={"code": "fixture-response-loss"})
        async with get_db_session() as db:
            row = await db.get(ExternalEffect, pending.snapshot.effect_id)
            row.state = "manual_review"  # What recovery parks without a reconciler.
    image = await observed(screen)
    assert image.payload["eligible"] is True and "competing_effect_id" not in image.payload
    if other == "catalogue_read_in_flight":
        # The read stays in flight across the next capture and request.
        assert (await drain(resource))["blocking_effect_ids"] == [pending.snapshot.effect_id]
    args, request, _ = await call(screen)
    assert request.payload["resource_context"]["observation"]["event_id"] == image.id
    result = await execute(screen, args)
    assert not result.metadata.get("error"), result.output
    commands = [json.loads(r.content).get("command", "") for r in screen.sent if r.url.path == "/execute"]
    assert any("xdotool mousemove 150 300 click" in command for command in commands)


def image_input(screen, event):
    return {"asset_id": event.payload["asset_id"], "sha256": hashlib.sha256(screen.raw).hexdigest(),
            "size_bytes": len(screen.raw)}


async def request_reason(screen, resource, images):
    from assistant import resource_observations as observations
    from db.models.session import Session
    async with get_db_session() as db:
        row = await db.get(ResourceControlLease, resource[0].resource_id)
        session = await db.get(Session, screen.ctx.session_id)
        with pytest.raises(AssistantError) as refused:
            await observations._request_observation_locked(db, row, session, images, screen.ctx.run_fence)
        assert await observations.for_request_locked(db, row, session, images, screen.ctx.run_fence) is None
    assert refused.value.code == "RESOURCE_OBSERVATION_REQUIRED"
    return refused.value.detail["reason"]


@pytest.mark.parametrize("case", ["no_frame", "image_omitted", "shell_after_frame", "new_run"])
async def test_request_without_verified_frame_names_the_failed_check(screen, resource, case):
    if case == "no_frame":
        assert await request_reason(screen, resource, []) == "observation_missing"
        return
    image = await observed(screen)
    images = [image_input(screen, image)]
    if case == "image_omitted":
        images = []
    elif case == "shell_after_frame":
        shell = await effects.prepare_effect(resource[1], adapter="sandbox_tool", provider="wuying",
            operation="tool_execute", logical_key="shell-after-frame", request_payload={},
            resource_fence=resource[0])
        claim = await effects.claim_effect_for_dispatch(shell.snapshot.effect_id, resource[1])
        await effects.mark_effect_submitting(claim)
        await effects.settle_effect(claim, state="succeeded", receipt={"fixture": True})
    else:
        screen.ctx = replace(screen.ctx, run_generation=screen.ctx.run_generation + 1)
    expected = {"image_omitted": "image_not_in_request", "shell_after_frame": "superseded_by:sandbox_tool/tool_execute",
                "new_run": "run_fence_changed"}[case]
    assert await request_reason(screen, resource, images) == expected


async def test_capture_after_effect_claim_expiry_cannot_publish_an_observation(screen, monkeypatch):
    from core import oss
    from question.runtime import now
    client = oss.get_oss()
    original = client.head

    async def expire(key):
        async with get_db_session() as db:
            row = await db.scalar(select(ExternalEffect).where(
                ExternalEffect.session_id == screen.ctx.session_id, ExternalEffect.adapter == "computer"))
            row.claim_expires_at = now() - timedelta(seconds=1)
        return await original(key)

    client.head = expire
    monkeypatch.setattr(oss, "get_oss", lambda: client)
    await new_tool_call(screen.ctx, "computer", {"action": "screenshot"}, resource_images=[])
    result = await execute(screen, computer.ComputerArgs(action="screenshot"))
    assert result.metadata["error"]
    async with get_db_session() as db:
        assert not await db.scalar(select(AgentEvent.id).where(
            AgentEvent.session_id == screen.ctx.session_id, AgentEvent.kind == "resource.observed"))
        row = await db.scalar(select(ExternalEffect).where(
            ExternalEffect.session_id == screen.ctx.session_id, ExternalEffect.adapter == "computer"))
        assert row.state != "succeeded"


async def test_catalogue_refresh_at_a_real_step_boundary_keeps_the_screenshot_actionable(
        screen, resource, monkeypatch):
    """Production 2026-10-08..10: screenshot, step boundary, Skill/MCP refresh, click.

    The real loop re-reads the sandbox catalogue before every provider request.
    With an expired TTL that read is a durable ``sandbox_runtime/catalogue_read``
    effect under the bound Driver lease. It must not replace the screenshot as
    the desktop's actionable frame, so the next request still cites it and the
    click dispatches with the shown frame's geometry.
    """
    import copy
    from agent import processor, tool_resolution
    from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
    lease = resource[2]
    client = screen.ctx.sandbox
    client._catalogue_ttl_seconds = 0  # Every step boundary refreshes the directory.
    desktop = client._transport

    async def transport(request):
        if request.url.path == "/catalog":
            screen.sent.append(request)
            return httpx.Response(200, headers={"etag": f'"catalogue-{len(screen.sent)}"'}, json={
                "skills": [], "mcp_tools": [], "mcp_resources": [], "generation": "fixture"})
        return await desktop.handle_async_request(request)

    client._transport = httpx.MockTransport(transport)
    frame = "data:image/png;base64," + base64.b64encode(screen.raw).decode()

    class Frames(dict):
        """Resolve every stored screenshot to the fixture's exact PNG bytes."""
        def __contains__(self, _key):
            return True

        def __getitem__(self, _key):
            return frame

    config = _loop_config()
    config.compaction.auto = False
    config.permission = {"*": "allow"}
    real = (loop.resolve_step_tools, loop.resolve_images, loop._resolve_history_tool_names)
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)
    monkeypatch.setattr(loop, "resolve_step_tools", real[0])
    monkeypatch.setattr(loop, "resolve_images", real[1])
    monkeypatch.setattr(loop, "_resolve_history_tool_names", real[2])
    monkeypatch.setattr(loop, "_IMAGE_CACHE", Frames())

    async def desktop_client(*_args, **_kwargs):
        return client

    async def no_title(*_args, **_kwargs):
        return None

    original_agent = loop.get_agent

    def computer_agent(name):
        agent = copy.copy(original_agent(name))
        agent.tools = ["computer"]
        return agent

    monkeypatch.setattr("sandbox.sandbox_manager.get_client", desktop_client)
    monkeypatch.setattr(loop, "_ensure_title", no_title)
    monkeypatch.setattr(loop, "get_agent", computer_agent)
    monkeypatch.setattr(tool_resolution, "get_tools_for_agent",
        lambda ids: {tool_id: computer.computer_tool for tool_id in ids if tool_id == "computer"})
    calls = []

    async def provider(**kwargs):
        calls.append(kwargs)
        wire = next(name for name, tool in kwargs["tools"].items() if tool.id == "computer")
        if len(calls) == 1:
            yield {"type": "tool_call", "tool": wire, "args": {"action": "screenshot"},
                   "call_id": "frame", "invalid": False}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
        elif len(calls) == 2:
            yield {"type": "tool_call", "tool": wire, "args": {"action": "left_click", "coordinate": [100, 200]},
                   "call_id": "click", "invalid": False}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
        else:
            yield {"type": "text_delta", "text": "Clicked the shown control."}
            yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", provider)
    await asyncio.wait_for(loop.run_loop(lease.session_id, lease.user_id, lease=lease), timeout=60)
    assert len(calls) == 3
    async with get_db_session() as db:
        observed = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == lease.session_id,
            AgentEvent.kind == "resource.observed").order_by(AgentEvent.sequence))).all())
        requests = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == lease.session_id,
            AgentEvent.kind == "model.requested").order_by(AgentEvent.sequence))).all())
        reads = list((await db.scalars(select(ExternalEffect).where(
            ExternalEffect.session_id == lease.session_id, ExternalEffect.adapter == "sandbox_runtime",
            ExternalEffect.operation == "catalogue_read").order_by(ExternalEffect.created_at))).all())
        inputs = list((await db.scalars(select(ExternalEffect).where(
            ExternalEffect.session_id == lease.session_id, ExternalEffect.adapter == "computer")
            .order_by(ExternalEffect.created_at))).all())
    # The screenshot's frame, then the click's own post-action frame.
    frame_event, after_click = observed
    assert frame_event.payload["eligible"] is True and after_click.payload["eligible"] is True
    observation = requests[1].payload["resource_context"]["observation"]
    assert observation is not None and observation["event_id"] == frame_event.id
    # One catalogue read per step; the second crossed the boundary between
    # the captured frame and the click that cites it.
    assert len(reads) == 3 and all(row.state == "succeeded" for row in reads)
    order = [r.url.path if r.url.path != "/execute" else json.loads(r.content).get("command", "")
             for r in screen.sent]
    shot = next(i for i, item in enumerate(order) if "obx-shot " in item)
    click = next(i for i, item in enumerate(order) if "xdotool mousemove 150 300 click" in item)
    assert [i for i, item in enumerate(order) if item == "/catalog"][1] in range(shot, click)
    assert [row.safe_context["action"] for row in inputs] == ["screenshot", "left_click"]
    assert all(row.state == "succeeded" for row in inputs)
    assert inputs[1].safe_context["resource_observation"] == observation


def test_actual_capture_helper_hashes_the_emitted_png(tmp_path, monkeypatch, capsys):
    Image = pytest.importorskip("PIL.Image")
    from PIL import ImageGrab
    from sandbox.desktop import OBX_SHOT_SCRIPT
    frame = Image.new("RGB", (320, 180), "white")
    frame.paste("black", (40, 40, 100, 100))
    monkeypatch.setattr(ImageGrab, "grab", lambda: frame.copy())
    output = tmp_path / "screen.png"
    monkeypatch.setattr("sys.argv", ["obx-shot", "160", "90", str(output)])
    # Actual X11 sampling belongs to the remote host. Run the complete helper
    # against a local captured image and a no-pointer sampler here.
    script = OBX_SHOT_SCRIPT.replace("sampler = ScreenSampler()", "sampler = FixtureSampler()")
    sampler = SimpleNamespace(pointer=lambda: None, close=lambda: None)
    exec(compile(script, "obx-shot", "exec"), {"__name__": "__main__", "FixtureSampler": lambda: sampler})
    geometry = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert geometry["native"] == [320, 180] and geometry["scaled"] == [160, 90]
    assert geometry["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert geometry["bytes"] == output.stat().st_size
