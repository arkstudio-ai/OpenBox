"""Actual loop, processor, SQL effects and finite browser HTTP admission.

Only Docker SDK, Chromium pipe, OSS bytes and the external model are replaced.
The supervisor's startup report is a schema fixture; physical proof is in the
separate real-Docker/Chromium suite, not inferred from these SQL assertions.
"""
import base64
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import struct
from types import SimpleNamespace
import zlib

import httpx
import pytest
from sqlalchemy import select

from agent import inbox, loop, processor
from agent.driver import reserve_run
from assistant.commands import accept_task_command
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import TaskResult
from db.models.external_effect import ExternalEffect
from db.models.file_asset import FileAsset
from db.models.part import Part
from db.models.session import Session
from db.models.workspace import WorkspaceMember
from session.agent_event_log import checkpoint_model_request, load_canonical_model_surface, verify_agent_event_parity
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_browser_resources import browser_world, command  # noqa: F401
from tests.unit.test_private_runtime import assistant_database, private_world  # noqa: F401
from tool.private_browser import private_browser_tool
from tool.tool import ToolContext


def png():
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1024, 768, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff" * (1024 * 3)) * 768)) + chunk(b"IEND", b""))


@pytest.fixture
async def automation(browser_world, monkeypatch):
    w = browser_world
    w.raw, w.objects, w.downloads, w.provider = png(), {}, [], []
    original_pipe = w.pipe.execute

    async def pipe(kind, args):
        value = await original_pipe(kind, args)
        if kind == "capture":
            value.update(png_base64=base64.b64encode(w.raw).decode(), sha256=hashlib.sha256(w.raw).hexdigest())
        return value

    monkeypatch.setattr(w.pipe, "execute", pipe)

    async def put(key, raw, **kwargs):
        assert kwargs == {"content_type": "image/png", "forbid_overwrite": True}
        assert key not in w.objects
        w.objects[key] = raw

    async def storage(request):
        assert request.url.host == "private-browser-objects.invalid"
        key = request.url.path.removeprefix("/")
        w.downloads.append(key)
        return httpx.Response(200, content=w.objects[key])

    actual_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs:
        actual_client(**{**kwargs, "transport": httpx.MockTransport(storage)}))
    monkeypatch.setattr("core.oss.get_oss", lambda: SimpleNamespace(put_object=put,
        presign_get=lambda key, **kwargs: "https://private-browser-objects.invalid/" + key))
    real_images = loop.resolve_images
    config = _loop_config()
    config.private_runtime = w.w.config.private_runtime
    config.permission = {"*": "allow"}
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)
    monkeypatch.setattr(loop, "resolve_images", real_images)
    monkeypatch.setattr(loop, "_IMAGE_CACHE", {})
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")

    async def no_title(*args, **kwargs):
        return None

    async def catalogue(*args, **kwargs):
        return SimpleNamespace(tools={"private_browser": private_browser_tool}, catalogue_availability="available")

    monkeypatch.setattr(loop, "_ensure_title", no_title)
    monkeypatch.setattr(loop, "resolve_step_tools", catalogue)
    monkeypatch.setattr(inbox, "schedule_inbox_wake", lambda *args: None)
    w.task = await accept_task_command(**w.scope, project_id=w.main.project_id,
        idempotency_key="browser-task", prompt="Inspect the private browser and enter the fixture text.", model=config.model)
    w.config = config
    return w


async def run(w, stream):
    lease = await reserve_run(w.task["execution_session_id"], w.w.owner)
    try:
        return await loop.run_loop(lease.session_id, user_id=lease.user_id, lease=lease)
    finally:
        await lease.release(session_status="idle")


@pytest.fixture
async def active(automation):
    from sandbox.browser_operation import prepare_provider
    w = automation
    lease = await reserve_run(w.task["execution_session_id"], w.w.owner)
    batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    await lease.set_phase("running")
    w.resource_id = await prepare_provider(lease)
    w.lease, w.resumed, w.user_message = lease, [], batch.messages[0]
    w.ctx = ToolContext(session_id=lease.session_id, user_id=lease.user_id, workspace_id=w.w.workspace,
        project_id=w.main.project_id, agent_id="build", run_id=lease.run_id, run_generation=lease.generation,
        _assert_current=lease.assert_current, abort=lease.abort)
    try:
        yield w
    finally:
        for current in [lease, *w.resumed]:
            await current.release(session_status="idle")


async def call(w, args, *, images=True, model=None):
    from models.message import ToolPartData, ToolStatus
    from session.session import create_assistant_message, save_part
    ctx = w.ctx
    message = await create_assistant_message(ctx.session_id, w.user_message.id, model_id=w.config.model,
        agent="build", user_id=ctx.user_id, run_fence=ctx.run_fence)
    ctx.message_id = message.id
    surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
    evidence = {}
    wire = await loop.resolve_images(loop._to_llm_messages(surface.messages, user_id=ctx.user_id), model,
        resource_images=evidence)
    await checkpoint_model_request(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence,
        request_id="fixture:" + message.id, model_id=w.config.model, provider_binding_digest="a" * 64,
        tool_schema_digest="b" * 64, prompt_shape_digest="c" * 64,
        expected_event_sequence=surface.event_sequence, expected_event_digest=surface.event_digest,
        message_id=message.id, resource_browser_id=w.resource_id,
        resource_images=list(evidence.values()) if images else [])
    part = ToolPartData(session_id=ctx.session_id, message_id=message.id, tool="private_browser",
        canonical_tool_id="private_browser", wire_tool_name="private_browser", call_id="browser-" + message.id,
        status=ToolStatus.RUNNING, input=args, provider_binding_digest="a" * 64, provider_dialect="test", stream_seq=0)
    await save_part(part, is_new=True, user_id=ctx.user_id, run_fence=ctx.run_fence)
    ctx.part_id = part.id
    async with get_db_session() as db:
        request = await db.scalar(select(AgentEvent).where(AgentEvent.message_id == message.id,
            AgentEvent.kind == "model.requested"))
    return request, wire


async def execute(w, args):
    return await private_browser_tool.execute(args, w.ctx)


async def capture(w):
    args = {"action": "capture"}
    await call(w, args)
    result = await execute(w, args)
    assert not result.metadata.get("error"), result
    return result


async def effects_for(w):
    async with get_db_session() as db:
        return list((await db.scalars(select(ExternalEffect).where(ExternalEffect.session_id == w.ctx.session_id)
            .order_by(ExternalEffect.created_at))).all())


async def test_capture_image_provider_and_finite_input_use_original_sql_proof(automation, monkeypatch):
    w = automation

    async def stream(**kwargs):
        ctx = kwargs["ctx"]
        w.provider.append(deepcopy(kwargs["messages"]))
        count = len(w.provider)
        assert "private_browser" in kwargs["tools"]
        if count <= 2:
            args = {"action": "capture"} if count == 1 else {"action": "text", "text": "PRIVATE_BROWSER_FIXTURE"}
            if count == 2:
                assert any("data:image/png;base64," in str(item.get("_images")) for item in kwargs["messages"])
            yield {"type": "tool_call", "tool": "private_browser", "args": args,
                "call_id": "browser-step-" + str(count), "invalid": False}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
        else:
            assert count == 3
            yield {"type": "text_delta", "text": "Entered the fixture text after inspecting its screenshot."}
            yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)
    await run(w, stream)
    assert len(w.provider) == 3
    assert [kind for kind, _ in w.pipe.calls] == ["capture", "text"]
    assert w.pipe.text == "PRIVATE_BROWSER_FIXTURE" and len(w.objects) == len(w.downloads) == 1
    async with get_db_session() as db:
        session_id = w.task["execution_session_id"]
        events = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == session_id)
            .order_by(AgentEvent.sequence))).all())
        requests = [event for event in events if event.kind == "model.requested"]
        observation, = [event for event in events if event.kind == "resource.observed"]
        effects = list((await db.scalars(select(ExternalEffect).where(ExternalEffect.session_id == session_id)
            .order_by(ExternalEffect.created_at))).all())
        assets = list((await db.scalars(select(FileAsset).where(FileAsset.session_id == session_id))).all())
        item = await db.get(AgentInboxItem, w.task["inbox_id"])
        results = list((await db.scalars(select(TaskResult).where(TaskResult.task_id == w.task["task_id"]))).all())
    assert len(requests) == 3 and len(effects) == 2 and len(assets) == len(results) == 1
    assert item.state == "settled" and item.outcome == "succeeded"
    assert requests[0].payload["browser_context"]["observation"] is None
    ref = requests[1].payload["browser_context"]["observation"]
    assert ref["event_id"] == observation.id and observation.payload["eligible"] is True
    assert observation.payload["sha256"] == hashlib.sha256(w.raw).hexdigest()
    assert requests[2].payload["browser_context"]["observation"] is None
    assert all(effect.state == "succeeded" and effect.attempt_count == 1 for effect in effects)
    assert effects[1].safe_context["resource_request_id"] == requests[1].payload["request_id"]
    assert effects[1].safe_context["resource_observation"] == ref
    with w.journal.transaction() as db:
        assert db.execute("SELECT count(*) FROM operations").fetchone()[0] == 2
    await verify_agent_event_parity(session_id, user_id=w.w.owner)


@pytest.mark.parametrize("missing", ["omitted", "text_only", "changed_bytes"])
async def test_input_requires_the_image_actually_delivered_in_its_request(active, missing):
    w = active
    captured = await capture(w)
    if missing == "changed_bytes":
        loop._IMAGE_CACHE[captured.metadata["asset_id"]] = "data:image/png;base64,Y2hhbmdlZA=="
    args = {"action": "mouse", "x": 100, "y": 200, "button": "left"}
    if missing == "changed_bytes":
        from assistant.policy import AssistantError
        with pytest.raises(AssistantError, match="new screenshot"):
            await call(w, args)
        assert len(await effects_for(w)) == 1 and len(w.pipe.calls) == 1
        return
    request, wire = await call(w, args, images=missing != "omitted",
        model="openai/deepseek-v4-flash" if missing == "text_only" else None)
    assert request.payload["browser_context"]["observation"] is None
    if missing == "text_only":
        assert not any(message.get("_images") for message in wire)
    before = len(w.requests)
    denied = await execute(w, args)
    assert denied.metadata["error"] and len(w.requests) == before
    assert len(await effects_for(w)) == 1 and [kind for kind, _ in w.pipe.calls] == ["capture"]


@pytest.mark.parametrize("change", ["membership", "runtime_revision", "source_deleted"])
async def test_current_authority_and_source_changes_block_an_original_prepared_input(active, change):
    from db.models.private_runtime import PrivateRuntimeBinding
    from sandbox.browser_operation import prepare
    w = active
    captured = await capture(w)
    args = {"action": "text", "text": "DENIED"}
    await call(w, args)
    original, _, _, _ = await prepare(w.ctx, args)
    async with get_db_session() as db:
        if change == "membership":
            (await db.get(WorkspaceMember, (w.w.workspace, w.w.owner))).status = "removed"
        elif change == "runtime_revision":
            (await db.get(PrivateRuntimeBinding, w.route.binding_id)).revision += 1
        else:
            (await db.get(FileAsset, captured.metadata["asset_id"])).is_deleted = True
    before = len(w.requests)
    if change == "membership":
        from assistant.policy import AssistantError
        with pytest.raises(AssistantError):
            await execute(w, args)
    else:
        assert (await execute(w, args)).metadata["error"]
    assert len(w.requests) == before and w.pipe.text == ""
    row = (await effects_for(w))[-1]
    assert row.id == original.effect_id and row.state == "prepared" and row.attempt_count == 0


async def test_giveback_never_rebinds_pending_call_and_new_request_must_capture(active, monkeypatch):
    from assistant import control as task_controls
    from models.message import TextPart
    from sandbox.browser_operation import prepare
    from session.session import create_assistant_message, save_part, update_message_info
    w = active
    await capture(w)
    args = {"action": "text", "text": "AFTER_NEW_CAPTURE"}
    request, _ = await call(w, args)
    original, _, _, _ = await prepare(w.ctx, args)
    old_ctx = replace(w.ctx)
    real_recover = task_controls.recover_controls

    async def recover(**kwargs):
        changed, leases = await real_recover(**kwargs, launch=False)
        w.resumed.extend(leases)
        return changed, leases

    monkeypatch.setattr(task_controls, "recover_controls", recover)
    pending = await command(w, "takeover", 1, "automation-takeover")
    assert pending.status_code == 200 and pending.json()["state"] == "draining"
    assert w.lease.abort.is_set()
    answer = await create_assistant_message(w.ctx.session_id, w.user_message.id, agent="build",
        model_id=w.config.model, user_id=w.ctx.user_id, run_fence=w.ctx.run_fence)
    await save_part(TextPart(session_id=w.ctx.session_id, message_id=answer.id, text="Paused for browser control"),
        is_new=True, user_id=w.ctx.user_id, run_fence=w.ctx.run_fence)
    answer.finish = "aborted"
    await update_message_info(answer, user_id=w.ctx.user_id, run_fence=w.ctx.run_fence)
    await inbox.settle_claimed_inbox_items(w.lease, result_message_id=answer.id, outcome="aborted")
    await w.lease.release(session_status="idle")
    await real_recover(task_id=w.task["task_id"], launch=False)
    taken = await command(w, "takeover", 1, "automation-takeover")
    assert taken.status_code == 200 and taken.json()["fence"]["epoch"] == 2, taken.text
    returned = await command(w, "giveback", 2, "automation-giveback")
    assert returned.status_code == 200 and returned.json()["fence"]["epoch"] == 3, returned.text
    assert len(w.resumed) == 1
    lease = w.resumed[0]
    await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
    await lease.set_phase("running")
    w.ctx = replace(old_ctx, run_id=lease.run_id, run_generation=lease.generation,
        abort=lease.abort, _assert_current=lease.assert_current)
    before = len(w.requests)
    assert (await execute(w, args)).metadata["error"]
    assert len(w.requests) == before
    row = (await effects_for(w))[-1]
    assert row.id == original.effect_id and row.resource_epoch == 1 and row.attempt_count == 0
    assert row.safe_context["resource_request_id"] == request.payload["request_id"]
    fresh, _ = await call(w, args)
    assert fresh.payload["browser_context"]["fence"]["epoch"] == 3
    assert fresh.payload["browser_context"]["observation"] is None
    assert (await execute(w, args)).metadata["error"] and w.pipe.text == ""
    await capture(w)
    fresh, _ = await call(w, args)
    assert fresh.payload["browser_context"]["observation"] is not None
    assert not (await execute(w, args)).metadata.get("error")
    assert [kind for kind, _ in w.pipe.calls] == ["capture", "capture", "text"]
    assert w.pipe.text == "AFTER_NEW_CAPTURE"


async def test_response_loss_reconciles_original_receipt_without_repeating_input(active, monkeypatch):
    from agent import effect_ledger
    from db.base import close_engine, get_engine, init_engine
    from sandbox.browser_resource_client import BrowserResourceClient
    w = active
    await capture(w)
    args = {"action": "text", "text": "ONCE"}
    await call(w, args)
    original = BrowserResourceClient.operate

    async def lose_reply(self, **kwargs):
        await original(self, **kwargs)
        raise httpx.ReadError("Fixture response lost after finite browser delivery")

    monkeypatch.setattr(BrowserResourceClient, "operate", lose_reply)
    result = await execute(w, args)
    assert result.metadata["error"] and w.pipe.text == "ONCE"
    first = (await effects_for(w))[-1]
    assert first.state == "outcome_unknown" and first.attempt_count == 1
    before = len(w.requests)
    assert (await execute(w, args)).metadata["error"] and len(w.requests) == before
    await w.lease.release(session_status="idle")
    url = get_engine().url.render_as_string(hide_password=False)
    await close_engine()
    init_engine(url)
    assert await effect_ledger.recover_effect_once(first.id) == "reconciled"
    current = (await effects_for(w))[-1]
    assert current.id == first.id and current.state == "succeeded" and current.attempt_count == 1
    assert current.reconcile_count == 1 and current.provider_receipt["state"] == "completed"
    assert w.pipe.text == "ONCE" and [kind for kind, _ in w.pipe.calls] == ["capture", "text"]
    assert not any(key in json.dumps(current.provider_receipt) for key in ("png_base64", "ONCE", "api_key"))


@pytest.mark.parametrize("target", ["ordinary", "unlinked_private", "disabled_execution"])
async def test_ineligible_sessions_keep_plain_provider_context_and_no_browser_tool(automation, monkeypatch, target):
    from session.session import create_session
    w = automation
    if target == "disabled_execution":
        w.config.private_runtime.enabled = False
        session_id = w.task["execution_session_id"]
    else:
        session_id = (w.w.shared.id if target == "ordinary" else (await create_session(
            user_id=w.w.owner, workspace_id=w.w.workspace, project_id=w.main.project_id,
            visibility="private", memory_policy="assistant_isolated", model=w.config.model,
            title="Unlinked private fixture")).id)
        await inbox.accept_inbox_item(session_id=session_id, user_id=w.w.owner, delivery="followup",
            prompt="Answer only in text", model=w.config.model, origin="human",
            origin_ref={"actor_user_id": w.w.owner})
    before = list(w.requests)
    sent = []

    async def stream(**kwargs):
        assert not any(tool.id == "private_browser" for tool in kwargs["tools"].values())
        sent.append(kwargs["ctx"].session_id)
        yield {"type": "text_delta", "text": "Plain response."}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)
    lease = await reserve_run(session_id, w.w.owner)
    try:
        await loop.run_loop(session_id, user_id=w.w.owner, lease=lease)
    finally:
        await lease.release(session_status="idle")
    assert sent == [session_id] and w.requests == before and not w.pipe.calls
    async with get_db_session() as db:
        request, = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == session_id,
            AgentEvent.kind == "model.requested"))).all())
    assert "browser_context" not in request.payload


async def test_deleted_cached_frame_cannot_reach_another_provider_request(active):
    from assistant.policy import AssistantError
    w = active
    captured = await capture(w)
    await call(w, {"action": "capture"})  # Resolve the real OSS bytes into the image cache.
    assert captured.metadata["asset_id"] in loop._IMAGE_CACHE
    async with get_db_session() as db:
        (await db.get(FileAsset, captured.metadata["asset_id"])).is_deleted = True
        before = len(list((await db.scalars(select(AgentEvent.id).where(
            AgentEvent.session_id == w.ctx.session_id, AgentEvent.kind == "model.requested"))).all()))
    with pytest.raises(AssistantError, match="new screenshot"):
        await call(w, {"action": "capture"})
    async with get_db_session() as db:
        after = len(list((await db.scalars(select(AgentEvent.id).where(
            AgentEvent.session_id == w.ctx.session_id, AgentEvent.kind == "model.requested"))).all()))
    assert after == before and len(w.pipe.calls) == 1


async def test_real_task_child_inherits_the_link_but_uses_its_own_provider_frame(automation, monkeypatch):
    from db.models.browser_resource import BrowserResourceSession
    from db.models.subagent import SubagentActivation, SubagentDescriptor
    from tool.task import task_tool
    w = automation
    counts, child_ids = {}, set()

    async def catalogue(*args, **kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in (private_browser_tool, task_tool)},
            catalogue_availability="available")

    monkeypatch.setattr(loop, "resolve_step_tools", catalogue)

    async def stream(**kwargs):
        ctx = kwargs["ctx"]
        counts[ctx.session_id] = counts.get(ctx.session_id, 0) + 1
        count = counts[ctx.session_id]
        if ctx.session_id == w.task["execution_session_id"]:
            if count == 1:
                yield {"type": "tool_call", "tool": "task", "args": {
                    "description": "Private browser child", "prompt": "Inspect and enter child fixture text",
                    "subagent_type": "general", "tools": ["private_browser"]}, "call_id": "private-child", "invalid": False}
                yield {"type": "finish", "reason": "tool_calls", "usage": {}}
                return
            assert count == 2
        else:
            child_ids.add(ctx.session_id)
            assert ctx.agent_id == "general" and "private_browser" in kwargs["tools"]
            if count <= 2:
                if count == 2:
                    assert any(message.get("_images") for message in kwargs["messages"])
                yield {"type": "tool_call", "tool": "private_browser",
                    "args": {"action": "capture"} if count == 1 else {"action": "text", "text": "CHILD"},
                    "call_id": "child-browser-" + str(count), "invalid": False}
                yield {"type": "finish", "reason": "tool_calls", "usage": {}}
                return
            assert count == 3
        yield {"type": "text_delta", "text": "Private browser child completed."}
        yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)
    await run(w, stream)
    assert len(child_ids) == 1 and sorted(counts.values()) == [2, 3]
    child_id, = child_ids
    assert w.pipe.text == "CHILD" and [kind for kind, _ in w.pipe.calls] == ["capture", "text"]
    async with get_db_session() as db:
        child = await db.get(Session, child_id)
        assert child.parent_id == w.task["execution_session_id"]
        assert child.visibility == "private" and child.memory_policy == "assistant_isolated"
        descriptors = list((await db.scalars(select(SubagentDescriptor).where(
            SubagentDescriptor.parent_session_id == child.parent_id))).all())
        activations = list((await db.scalars(select(SubagentActivation).where(
            SubagentActivation.descriptor_id == descriptors[0].id))).all())
        sessions = set((await db.scalars(select(BrowserResourceSession.session_id).where(
            BrowserResourceSession.session_id.in_((child_id, child.parent_id))))).all())
    assert len(descriptors) == len(activations) == 1 and sessions == {child_id, child.parent_id}


@pytest.mark.parametrize("transition", ["disabled", "plan"])
async def test_historical_cached_browser_image_is_revalidated_without_browser_tools(automation, monkeypatch, transition):
    from agent import effect_ledger
    w = automation
    original = effect_ledger.settle_effect
    switched, provider_calls = [], []

    async def settle(claim, **kwargs):
        result = await original(claim, **kwargs)
        if result.operation == "capture" and result.state == "succeeded" and not switched:
            surface = await load_canonical_model_surface(claim.session_id, user_id=claim.tenant_id,
                run_fence=(claim.session_id, result.run_id, result.run_generation))
            # Warm the real OSS image cache, exactly as a prior successful
            # provider projection can; do not fabricate cached image bytes.
            await loop.resolve_images(loop._to_llm_messages(surface.messages, user_id=claim.tenant_id))
            asset_id = kwargs["projection"]["asset_id"]
            assert asset_id in loop._IMAGE_CACHE
            async with get_db_session() as db:
                (await db.get(FileAsset, asset_id)).is_deleted = True
            if transition == "disabled":
                w.config.private_runtime.enabled = False
            else:
                await inbox.accept_inbox_item(session_id=claim.session_id, user_id=claim.tenant_id,
                    delivery="steer", agent="plan", prompt="Continue in plan mode without browser tools.",
                    origin="human", origin_ref={"actor_user_id": claim.tenant_id}, model=w.config.model)
            switched.append(asset_id)
        return result

    monkeypatch.setattr(effect_ledger, "settle_effect", settle)

    async def stream(**kwargs):
        provider_calls.append(deepcopy(kwargs["messages"]))
        if len(provider_calls) == 1:
            yield {"type": "tool_call", "tool": "private_browser", "args": {"action": "capture"},
                "call_id": "historical-capture", "invalid": False}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
        else:
            assert not any(tool.id == "private_browser" for tool in kwargs["tools"].values())
            assert any(message.get("_images") for message in kwargs["messages"])
            if transition == "plan":
                assert kwargs["ctx"].agent_id == "plan"
            yield {"type": "text_delta", "text": "Historical image was sent."}
            yield {"type": "finish", "reason": "stop", "usage": {}}

    monkeypatch.setattr(processor, "stream_llm", stream)
    await run(w, stream)
    assert len(switched) == 1
    assert len(provider_calls) == 1, "A deleted cached browser screenshot reached a later provider request"
    async with get_db_session() as db:
        requests = list((await db.scalars(select(AgentEvent).where(
            AgentEvent.session_id == w.task["execution_session_id"], AgentEvent.kind == "model.requested"))).all())
    assert len(requests) == 1 and len(w.pipe.calls) == 1


@pytest.mark.parametrize("lost_response", [False, True])
async def test_navigation_error_is_reported_without_resending_the_delivered_input(active, monkeypatch, lost_response):
    from agent import effect_ledger
    from sandbox.browser_resource_client import BrowserResourceClient
    from browser_pipe import BrowserPipe
    w = active
    original_pipe = w.pipe.execute
    cdp_calls = []

    async def cdp(method, args, **kwargs):
        cdp_calls.append((method, args))
        return {"errorText": "net::ERR_NAME_NOT_RESOLVED"}

    async def pipe(kind, args):
        if kind == "navigate":
            w.pipe.calls.append((kind, args))
            # Real Page.navigate result translation; only Chrome's RPC is fake.
            return await BrowserPipe.execute(SimpleNamespace(_events=[], call=cdp), kind, args)
        return await original_pipe(kind, args)

    monkeypatch.setattr(w.pipe, "execute", pipe)
    await capture(w)
    args = {"action": "navigate", "url": "https://browser-navigation.invalid/"}
    await call(w, args)
    original_request = BrowserResourceClient.operate

    async def lose_reply(self, **kwargs):
        await original_request(self, **kwargs)
        raise httpx.ReadError("Fixture lost response after failed navigation")

    if lost_response:
        monkeypatch.setattr(BrowserResourceClient, "operate", lose_reply)
    result = await execute(w, args)
    assert result.metadata.get("error") is True
    current = (await effects_for(w))[-1]
    if lost_response:
        assert current.state == "outcome_unknown"
        await w.lease.release(session_status="idle")
        assert await effect_ledger.recover_effect_once(current.id) == "reconciled"
        current = (await effects_for(w))[-1]
    else:
        assert result.metadata["error_code"] == "BROWSER_NAVIGATION_FAILED"
        assert result.metadata["state"] == "navigation_failed"
        assert "net::ERR_NAME_NOT_RESOLVED" in result.output
        before = len(w.requests)
        assert (await execute(w, args)).metadata["error"] and len(w.requests) == before
    assert current.state == "failed" and current.attempt_count == 1
    assert current.provider_receipt["state"] == "completed"
    assert cdp_calls == [("Page.navigate", {"url": args["url"]})]
    assert [kind for kind, _ in w.pipe.calls] == ["capture", "navigate"]


async def test_ordinary_user_image_keeps_provider_bytes_without_browser_authority(automation, monkeypatch):
    from core.identifier import ascending
    from models.message import FilePart
    from question.runtime import now
    from session.session import save_part
    w = automation
    session_id = w.w.shared.id
    await inbox.accept_inbox_item(session_id=session_id, user_id=w.w.owner, delivery="followup",
        prompt="Describe this ordinary uploaded image", model=w.config.model, origin="human",
        origin_ref={"actor_user_id": w.w.owner})
    lease = await reserve_run(session_id, w.w.owner)
    try:
        batch = await inbox.claim_inbox_boundary(lease, step=1, include_next_turn=True)
        asset_id, key = ascending("asset"), "ordinary-upload.png"
        w.objects[key] = w.raw
        async with get_db_session() as db:
            db.add(FileAsset(id=asset_id, user_id=w.w.owner, workspace_id=w.w.workspace, session_id=session_id,
                project_id=w.w.shared.project_id, name=key, oss_key=key, mime="image/png", size=len(w.raw),
                source="user", transient=False, status="ready", created_at=now()))
        await save_part(FilePart(session_id=session_id, message_id=batch.messages[0].id, path=key,
            asset_id=asset_id, oss_key=key, mime_type="image/png", size=len(w.raw)), is_new=True,
            user_id=w.w.owner, run_fence=(session_id, lease.run_id, lease.generation))
        sent = []

        async def stream(**kwargs):
            assert not any(tool.id == "private_browser" for tool in kwargs["tools"].values())
            images = [image for message in kwargs["messages"] for image in message.get("_images", [])]
            assert images == ["data:image/png;base64," + base64.b64encode(w.raw).decode()]
            sent.append(True)
            yield {"type": "text_delta", "text": "The ordinary uploaded image is visible."}
            yield {"type": "finish", "reason": "stop", "usage": {}}

        monkeypatch.setattr(processor, "stream_llm", stream)
        await loop.run_loop(session_id, user_id=w.w.owner, lease=lease)
        assert sent == [True] and not w.pipe.calls
        async with get_db_session() as db:
            request, = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == session_id,
                AgentEvent.kind == "model.requested"))).all())
        assert "browser_context" not in request.payload and "resource_context" not in request.payload
    finally:
        await lease.release(session_status="idle")
