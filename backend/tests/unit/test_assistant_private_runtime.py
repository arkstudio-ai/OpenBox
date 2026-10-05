"""Future private input admission fails before shared physical runtime I/O.

The historical raw-file disclosure remains a separate retained failing
fixture. These tests do not delete, move or certify any old desktop files.
Only model and remote HTTP I/O are replaced; Session/Task/Inbox, manager,
provider routing, asset delivery and source policy are real SQL services.
"""
from datetime import datetime, timedelta, timezone
import json
import shlex
import socket
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from agent import inbox, loop, processor
from agent.driver import bind_current_lease, reserve_run, reset_current_lease
from assistant.commands import accept_task_command
from assistant.schedule_commands import create_schedule, run_schedule
from db.base import close_engine, get_db_session, get_engine, init_engine
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import TaskResult
from db.models.billing import BillingSubscription, PaymentOrder
from db.models.file_asset import FileAsset
from db.models.part import Part
from db.models.platform_account import PlatformAccount
from db.models.publish_job import PublishJob
from db.models.session import Session
from db.repository.cloud_desktop_repo import cloud_desktop_repo
from sandbox import channel, client as client_module
from sandbox.assets import AssetDeliveryError, deliver, deliver_asset_ids
from sandbox.client import SandboxClient, user_scope_for
from sandbox.manager import SandboxManager
from sandbox.privacy import PrivateRuntimeUnavailable
from sandbox.wuying import WuyingProvider
from session.session import create_session
from tests.offline_wuying import install_wuying_offline_guard
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_assets import asset_for
from tests.unit.test_assistant_commands import setup_task
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tool.tool import ToolContext


@pytest.fixture
async def world(monkeypatch):
    install_wuying_offline_guard(monkeypatch)
    config = _loop_config()
    config.permission = {"*": "allow"}
    config.sandbox_provider, config.wuying_routing = "wuying", "per_desktop"
    config.wuying_channel_key = "11" * 32
    monkeypatch.setattr("core.config.get_config", lambda: config)
    monkeypatch.setattr(channel, "get_config", lambda: config)
    monkeypatch.setattr(inbox, "schedule_inbox_wake", lambda *_: None)
    monkeypatch.setattr("assistant.schedule_commands.schedule_inbox_wake", lambda *_: None)
    owner, peer, workspace, main, args = await setup_task()
    task = await accept_task_command(**args)
    shared = await create_session(user_id=owner, workspace_id=workspace,
        project_id=main.project_id, title="Ordinary shared execution", model=config.model)
    private_asset = await asset_for(owner, workspace, name="private-note.txt", session_id=main.id,
        project_id=main.project_id)
    shared_asset = await asset_for(owner, workspace, name="shared-note.txt", session_id=shared.id,
        project_id=main.project_id)
    stamp = datetime.now(timezone.utc)
    order_id = "privacy-order-" + workspace
    async with get_db_session() as db:
        db.add(PaymentOrder(id=order_id, workspace_id=workspace, user_id=owner,
            request_key=order_id, provider="fixture", amount_fen=100, credits=1,
            currency="CNY", kind="subscription", status="paid", created_at=stamp))
        await db.flush()
        db.add(BillingSubscription(order_id=order_id, workspace_id=workspace, plan_id="pro",
            cycle="monthly", plan={}, starts_at=stamp - timedelta(days=1), ends_at=stamp + timedelta(days=1)))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config.wuying_endpoint = f"http://127.0.0.1:{port}"
    record = await cloud_desktop_repo.create(workspace, "cn-private-fixture", "running", user_id=owner,
        desktop_id="privacy-" + workspace, pool_state="assigned", assigned_at=stamp, channel_kind="ssh",
        tunnel_bind="127.0.0.1", tunnel_port=port, tunnel_state="up", tunnel_fingerprint="fixture-" + workspace,
        tunnel_pubkey="fixture-public-" + workspace, action_api_key_hash=channel.action_key_hash("fixture-key"),
        action_api_key_ciphertext=channel.encrypt_action_key("fixture-key"))
    state = SimpleNamespace(config=config, owner=owner, peer=peer, workspace=workspace, main=main,
        task=task, args=args, shared=shared, private_asset=private_asset, shared_asset=shared_asset,
        record=record, sent=[], signed=[], files={}, resolutions=[], fail_http=False)

    def presign(key, **kwargs):
        state.signed.append(key)
        return "http://127.0.0.1/fixture-object/" + key

    state.oss = SimpleNamespace(presign_get=presign, host="127.0.0.1", internal_host="127.0.0.1", region="fixture")
    monkeypatch.setattr("core.oss.get_oss", lambda: state.oss)

    async def remote(request):
        assert (request.url.host, request.url.port) == ("127.0.0.1", port)
        assert request.headers["X-API-Key"] == "fixture-key"
        payload = json.loads(request.content) if request.content else {}
        state.sent.append((request.url.path, payload))
        if state.fail_http:
            raise httpx.ConnectError("fixture remote unavailable", request=request)
        if request.url.path == "/execute":
            words = shlex.split(payload["command"])
            if len(words) == 5 and words[1:3] == ["obx-file", "get"]:
                state.files[words[4]] = words[3]
            return httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": ""})
        if request.url.path == "/alive":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/desktop/lease/release":
            return httpx.Response(200, json={"ok": True})
        raise AssertionError("Unexpected fixture HTTP path: " + request.url.path)

    transport = httpx.MockTransport(remote)
    actual_client = httpx.AsyncClient
    http_namespace = dict(vars(httpx))
    http_namespace["AsyncHTTPTransport"] = lambda **kwargs: transport
    http_namespace["AsyncClient"] = lambda **kwargs: actual_client(**{
        **kwargs, "transport": kwargs.get("transport") or transport})
    monkeypatch.setattr(client_module, "httpx", SimpleNamespace(**http_namespace))
    provider = WuyingProvider()
    resolve = provider.resolve_user_container

    async def observed_resolution(*args, **kwargs):
        state.resolutions.append(args)
        return await resolve(*args, **kwargs)

    monkeypatch.setattr(provider, "resolve_user_container", observed_resolution)
    monkeypatch.setattr("sandbox.provider", provider)
    state.manager = SandboxManager()
    monkeypatch.setattr("sandbox.sandbox_manager", state.manager)
    monkeypatch.setattr("sandbox.manager.sandbox_manager", state.manager)
    state.direct = SandboxClient("127.0.0.1", port, "fixture-key", user_scope=user_scope_for(workspace))
    try:
        yield state
    finally:
        for client in {*state.manager._clients.values(), state.direct}:
            await client.aclose()
        await cloud_desktop_repo.update(record["id"], tunnel_port=None)
        sock.close()


async def test_real_manager_refuses_task_child_cron_and_private_before_resolution_or_warm_probe(world, record_property):
    w = world
    shared_client = await w.manager.get_client(w.shared.id, user_id=w.owner)
    assert w.sent and w.resolutions and w.shared.id in w.manager._session_project
    child = await create_session(user_id=w.owner, workspace_id=w.workspace, parent_id=w.task["execution_session_id"])
    ordinary_private = await create_session(user_id=w.owner, workspace_id=w.workspace, visibility="private")
    schedule = await create_schedule(user_id=w.owner, workspace_id=w.workspace, main_id=w.main.id,
        idempotency_key="private-runtime-schedule", project_id=w.main.project_id, name="Private text schedule",
        instructions="Return a text answer", schedule={"kind": "every", "every_ms": 600000}, enabled=False)
    cron = await run_schedule(user_id=w.owner, workspace_id=w.workspace, main_id=w.main.id,
        idempotency_key="private-runtime-run", job_id=schedule["job_id"], expected_revision=1)
    targets = [w.main.id, w.task["execution_session_id"], child.id, cron["execution_session_id"], ordinary_private.id]
    before = (len(w.sent), len(w.resolutions), dict(w.manager._session_project))
    for target in targets:
        for operation in (lambda: w.manager.get_client(target, user_id=w.owner),
                          lambda: w.manager.acquire(target, user_id=w.owner),
                          lambda: w.manager._acquire_for_user(target, user_id=w.owner, owner=w.workspace),
                          lambda: w.manager._ensure_session_dir(shared_client, target)):
            with pytest.raises(PrivateRuntimeUnavailable) as denied:
                await operation()
            assert denied.value.code == "PRIVATE_SANDBOX_UNAVAILABLE"
    assert (len(w.sent), len(w.resolutions), dict(w.manager._session_project)) == before
    record_property("private_runtime_manager", json.dumps({"task": w.task, "cron": cron,
        "private_targets": targets, "shared_session_id": w.shared.id, "rejected_entries": 20,
        "new_remote_calls": 0, "new_resolutions": 0}))


async def test_current_private_driver_cannot_borrow_direct_or_management_client_but_can_release(world):
    w = world
    lease = await reserve_run(w.task["execution_session_id"], w.owner)
    token = bind_current_lease(lease)
    try:
        # An ordinary trace cannot replace the current private Driver identity.
        async with w.direct.request_context(session_id=w.shared.id):
            with pytest.raises(PrivateRuntimeUnavailable):
                await w.direct.execute("must never enter shared shell")
        assert await w.manager.get_client_any(user_id=w.owner, workspace_id=w.workspace) is None
        assert w.sent == w.resolutions == []
        await w.direct._post("/desktop/lease/release", json={"token": "fixture-held-token"})
        assert [route for route, _ in w.sent] == ["/desktop/lease/release"]
        with pytest.raises(PrivateRuntimeUnavailable):
            await w.direct._get("/desktop/lease/release")
        assert len(w.sent) == 1
    finally:
        reset_current_lease(token)
        await lease.release(session_status="idle")
    # Unbound management has no private Session and keeps its existing path.
    assert await w.manager.get_client_any(user_id=w.owner, workspace_id=w.workspace) is not None


@pytest.mark.parametrize("field", ["visibility", "memory_policy"])
async def test_retained_client_rechecks_sql_privacy_after_engine_reopen(world, field):
    w = world
    client = await w.manager.get_client(w.shared.id, user_id=w.owner)
    async with get_db_session() as db:
        setattr(await db.get(Session, w.shared.id), field,
                "private" if field == "visibility" else "assistant_isolated")
    url = get_engine().url.render_as_string(hide_password=False)
    await close_engine()
    init_engine(url)
    before = len(w.sent)
    async with client.request_context(session_id=w.shared.id):
        with pytest.raises(PrivateRuntimeUnavailable):
            await client.execute("must not use a cached public audience")
    assert len(w.sent) == before


async def test_private_attachment_target_and_source_are_refused_before_prepare_or_signing(world, record_property):
    w = world
    for session_id, asset in [(w.task["execution_session_id"], w.shared_asset), (w.shared.id, w.private_asset)]:
        with pytest.raises(AssetDeliveryError) as denied:
            await deliver_asset_ids(session_id, w.owner, [asset.id])
        assert denied.value.code == "private_runtime_unavailable" and denied.value.retryable is False
    with pytest.raises(PrivateRuntimeUnavailable):
        await deliver(w.direct, w.record["id"], w.oss, [w.private_asset])
    assert w.sent == w.resolutions == w.signed == [] and not w.files
    paths = await deliver_asset_ids(w.shared.id, w.owner, [w.shared_asset.id])
    assert paths == ["/workspace/uploads/shared-note.txt"] and set(w.files) == set(paths)
    assert w.signed == [w.shared_asset.oss_key]
    record_property("private_runtime_attachments", json.dumps({"private_asset_id": w.private_asset.id,
        "private_asset_session": w.private_asset.session_id, "shared_session": w.shared.id,
        "shared_asset_id": w.shared_asset.id, "landed_shared_paths": paths, "private_transfers": 0}))


async def test_asset_delivery_does_not_trust_old_public_source_metadata(world):
    w = world
    async with get_db_session() as db:
        (await db.get(FileAsset, w.shared_asset.id)).session_id = w.main.id
    # The retained Python object still describes an ordinary source; current
    # SQL identifies its private source before CLI preparation or URL signing.
    assert w.shared_asset.session_id == w.shared.id
    with pytest.raises(PrivateRuntimeUnavailable):
        await deliver(w.direct, w.record["id"], w.oss, [w.shared_asset])
    assert w.sent == w.signed == [] and not w.files


async def _real_loop(world, monkeypatch, stream):
    manager_method = world.manager.get_client
    _patch_real_loop_runtime(monkeypatch, config=world.config, process_step=processor.process_step)
    monkeypatch.setattr(world.manager, "get_client", manager_method)
    monkeypatch.setattr(processor, "stream_llm", stream)
    async def no_background(*args, **kwargs):
        return None
    monkeypatch.setattr(loop, "_ensure_title", no_background)
    monkeypatch.setattr("agent.suggestions.generate_suggestions", no_background)


async def test_private_real_loop_keeps_text_and_reports_write_and_plan_preparation_unavailable(world, monkeypatch, record_property):
    from tool.plan import plan_exit_tool
    from tool.write import write_tool
    w, calls = world, []
    async def stream(**kwargs):
        ctx = kwargs["ctx"]
        calls.append((ctx.run_id, ctx.run_generation))
        assert ctx.sandbox is None and ctx.sandbox_error["code"] == "PRIVATE_SANDBOX_UNAVAILABLE"
        assert "私有执行隔离" in json.dumps(kwargs["system"], ensure_ascii=False)
        tools = {tool.id: name for name, tool in kwargs["tools"].items()}
        if len(calls) < 3:
            tool_id = "write" if len(calls) == 1 else "plan_exit"
            args = {"file_path": "/workspace/private-note.txt", "content": "PRIVATE_PHYSICAL_CANARY"} if tool_id == "write" else {}
            yield {"type": "tool_call", "tool": tools[tool_id], "args": args,
                "call_id": "private-" + tool_id, "invalid": False}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
        else:
            yield {"type": "text_delta", "text": "Text analysis is available; no file or plan was read or written."}
            yield {"type": "finish", "reason": "stop", "usage": {}}
    await _real_loop(w, monkeypatch, stream)
    async def tools(*args, **kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in (write_tool, plan_exit_tool)}, catalogue_availability="available")
    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    lease = await reserve_run(w.task["execution_session_id"], w.owner)
    try:
        answer = await loop.run_loop(lease.session_id, user_id=w.owner, lease=lease)
    finally:
        await lease.release(session_status="idle")
    assert len(calls) == 3 and w.sent == w.resolutions == [] and not w.files
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, w.task["inbox_id"])
        result = await db.scalar(select(TaskResult).where(TaskResult.task_id == w.task["task_id"]))
        parts = list((await db.scalars(select(Part).where(Part.session_id == lease.session_id, Part.type == "tool"))).all())
        assert item.state == "settled" and item.outcome == "succeeded"
        assert result.result_message_id == answer.id and result.outcome == "succeeded"
        assert len(parts) == 2 and all("私有执行" in json.dumps(part.data, ensure_ascii=False) for part in parts)
        assert await db.scalar(select(func.count()).select_from(Part).where(Part.session_id == lease.session_id, Part.type == "plan")) == 0
        requested = await db.scalar(select(func.count()).select_from(AgentEvent).where(AgentEvent.session_id == lease.session_id,
            AgentEvent.kind == "model.requested"))
        assert requested == 3
        record_property("private_runtime_loop", json.dumps({"task": w.task, "run_id": lease.run_id,
            "generation": lease.generation, "result_id": result.id, "result_message_id": answer.id,
            "provider_requests": requested, "blocked_tool_calls": len(parts), "physical_requests": 0}))


async def test_claimed_private_attachment_settles_once_without_model_or_transfer(world, monkeypatch, record_property):
    w = world
    accepted = await accept_task_command(**{**w.args, "idempotency_key": "private-attachment-run",
        "attachments": (w.private_asset.id,)})
    async def stream(**kwargs):
        pytest.fail("Private attachment admission reached a model")
        yield
    await _real_loop(w, monkeypatch, stream)
    lease = await reserve_run(accepted["execution_session_id"], w.owner)
    try:
        await loop.run_loop(lease.session_id, user_id=w.owner, lease=lease)
    finally:
        await lease.release(session_status="idle")
    assert w.sent == w.resolutions == w.signed == [] and not w.files
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, accepted["inbox_id"])
        results = list((await db.scalars(select(TaskResult).where(TaskResult.task_id == accepted["task_id"]))).all())
        assert item.state == "settled" and item.outcome == "delivery_error"
        assert item.delivery_attempts == 1 and item.delivery_last_error["retryable"] is False
        assert item.delivery_last_error["code"] == "private_runtime_unavailable"
        assert len(results) == 1 and results[0].outcome == "error"
        assert await db.scalar(select(func.count()).select_from(AgentEvent).where(AgentEvent.session_id == lease.session_id,
            AgentEvent.kind == "model.requested")) == 0
        record_property("private_runtime_delivery", json.dumps({"receipt": accepted, "run_id": lease.run_id,
            "generation": lease.generation, "result_id": results[0].id, "attempts": item.delivery_attempts,
            "error": item.delivery_last_error, "remote_requests": 0, "provider_requests": 0}))


@pytest.mark.parametrize("private_source", ["session", "asset"])
async def test_private_publish_stops_before_shared_title_record_and_shared_flow_still_reaches_transport(world, monkeypatch, private_source):
    from publish import desktop_service as publish
    from tool.desktop_publish import DesktopPublishArgs, execute
    w = world
    stamp = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(PlatformAccount(id="privacy-account-" + w.workspace, workspace_id=w.workspace,
            bound_by_user_id=w.owner, platform="douyin_creator", auth_kind="desktop_cookie", external_id="fixture",
            nickname="Fixture account", status="bound", desktop_id=w.record["desktop_id"],
            bound_at=stamp, created_at=stamp, updated_at=stamp))
        for asset in (w.private_asset, w.shared_asset):
            (await db.get(FileAsset, asset.id)).mime = "video/mp4"
    monkeypatch.setattr(publish, "_now", lambda: stamp.replace(hour=2))  # 10:00 Shanghai, inside existing posting window.
    ctx = ToolContext(user_id=w.owner, workspace_id=w.workspace,
        session_id=w.task["execution_session_id"] if private_source == "session" else w.shared.id)
    result = await execute(DesktopPublishArgs(action="publish", mode="auto", title="PRIVATE_TITLE_CANARY",
        asset_id=w.shared_asset.id if private_source == "session" else w.private_asset.id, dry_run=True), ctx)
    assert result.metadata["refused"] and result.metadata["retryable"] is False
    assert not result.metadata["job_id"] and "私有执行" in result.output
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(PublishJob).where(PublishJob.workspace_id == w.workspace)) == 0
    assert w.sent == w.signed == []
    w.fail_http = True
    shared = await execute(DesktopPublishArgs(action="publish", mode="auto", title="Ordinary shared draft",
        asset_id=w.shared_asset.id, dry_run=True), ToolContext(user_id=w.owner, workspace_id=w.workspace, session_id=w.shared.id))
    assert shared.metadata["refused"] and shared.metadata["retryable"] and shared.metadata["job_id"]
    assert w.sent
    async with get_db_session() as db:
        jobs = list((await db.scalars(select(PublishJob).where(PublishJob.workspace_id == w.workspace))).all())
        assert len(jobs) == 1 and jobs[0].title == "Ordinary shared draft" and jobs[0].status == "failed"
