"""Work the assistant delegates runs on the shared workspace runtime.

Only the assistant's own main conversation is private for runtime purposes,
and it is still refused any sandbox. Task, child, cron, fork and subagent
Sessions keep their private visibility and isolated memory, but acquire the
workspace cloud desktop like an ordinary project Session. Files the user gave
the assistant remain the user's own: they reach that user's Sessions in the
same workspace, and nobody else's. Only model and remote HTTP I/O are
replaced; Session/Task/Inbox, manager, provider routing, asset delivery and
the source policy are real SQL services.
"""
from datetime import datetime, timedelta, timezone
import json
import shlex
import socket
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

from agent import inbox, loop, processor
from agent.driver import bind_current_lease, reserve_run, reset_current_lease
from assistant.commands import accept_task_command
from assistant.schedule_commands import create_schedule, run_schedule
from db.base import close_engine, get_db_session, get_engine, init_engine
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import TaskResult
from db.models.billing import BillingSubscription, PaymentOrder
from db.models.file_asset import FileAsset
from db.models.platform_account import PlatformAccount
from db.models.publish_job import PublishJob
from db.models.session import Session
from db.repository.cloud_desktop_repo import cloud_desktop_repo
from sandbox import channel, client as client_module
from sandbox.assets import AssetDeliveryError, deliver, deliver_asset_ids
from sandbox.client import SandboxClient, user_scope_for
from sandbox.manager import SandboxManager
from sandbox.privacy import PrivateRuntimeUnavailable, session_requires_private_runtime
from sandbox.wuying import WuyingProvider
from session.session import create_session
from tests.offline_wuying import install_wuying_offline_guard
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_commands import setup_task
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tool.tool import ToolContext


async def asset_for(user, workspace, **changes):
    key = "asset_" + uuid4().hex
    row = FileAsset(id=key, user_id=user, workspace_id=workspace,
        name="Private report.txt", mime="text/plain", size=128, oss_key="test-only/" + key,
        status="ready", source="user", transient=False, is_deleted=False,
        created_at=datetime.now(timezone.utc))
    for name, value in changes.items():
        setattr(row, name, value)
    async with get_db_session() as db:
        db.add(row)
    return row


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


async def test_delegated_session_acquires_the_shared_runtime_while_main_is_refused(world, record_property):
    w = world
    delegated = w.task["execution_session_id"]
    async with get_db_session() as db:
        row = await db.get(Session, delegated)
        assert (row.kind, row.visibility, row.memory_policy) == ("normal", "private", "assistant_isolated")
    client = await w.manager.get_client(delegated, user_id=w.owner)
    assert client.private_runtime_route is None and client.workspace_id == w.workspace
    assert w.resolutions and ("/execute" in [path for path, _ in w.sent])
    # One workspace runtime: an ordinary project Session gets the same client.
    assert await w.manager.get_client(w.shared.id, user_id=w.owner) is client
    assert w.manager._session_project[delegated] == w.manager._session_project[w.shared.id]
    before = (len(w.sent), len(w.resolutions), dict(w.manager._session_project))
    for operation in (lambda: w.manager.get_client(w.main.id, user_id=w.owner),
                      lambda: w.manager.acquire(w.main.id, user_id=w.owner),
                      lambda: w.manager._acquire_for_user(w.main.id, user_id=w.owner, owner=w.workspace),
                      lambda: w.manager._ensure_session_dir(client, w.main.id)):
        with pytest.raises(PrivateRuntimeUnavailable) as denied:
            await operation()
        assert denied.value.code == "PRIVATE_SANDBOX_UNAVAILABLE"
    assert (len(w.sent), len(w.resolutions), dict(w.manager._session_project)) == before
    record_property("delegated_shared_runtime", json.dumps({"task": w.task, "delegated_session_id": delegated,
        "shared_session_id": w.shared.id, "main_session_id": w.main.id, "main_refused_entries": 4,
        "main_new_remote_calls": 0}))


async def test_task_children_cron_and_private_sessions_share_the_workspace_runtime(world):
    w = world
    shared = await w.manager.get_client(w.shared.id, user_id=w.owner)
    container = w.manager.get_info(w.shared.id).container_id
    child = await create_session(user_id=w.owner, workspace_id=w.workspace, parent_id=w.task["execution_session_id"])
    ordinary_private = await create_session(user_id=w.owner, workspace_id=w.workspace, visibility="private")
    schedule = await create_schedule(user_id=w.owner, workspace_id=w.workspace, main_id=w.main.id,
        idempotency_key="shared-runtime-schedule", project_id=w.main.project_id, name="Shared runtime schedule",
        instructions="Return a text answer", schedule={"kind": "every", "every_ms": 600000}, enabled=False)
    cron = await run_schedule(user_id=w.owner, workspace_id=w.workspace, main_id=w.main.id,
        idempotency_key="shared-runtime-run", job_id=schedule["job_id"], expected_revision=1)
    async with get_db_session() as db:
        for target in (child.id, cron["execution_session_id"]):
            row = await db.get(Session, target)
            # Visibility and memory isolation are unchanged; only the runtime is shared.
            assert row.visibility == "private" and row.memory_policy == "assistant_isolated"
    for target in (w.task["execution_session_id"], child.id, cron["execution_session_id"], ordinary_private.id):
        assert not await session_requires_private_runtime(target)
        assert (await w.manager.acquire(target, user_id=w.owner)).container_id == container
        assert await w.manager.get_client(target, user_id=w.owner) is shared
    assert await session_requires_private_runtime(w.main.id)


async def test_delegated_driver_uses_shared_clients_but_the_main_driver_cannot_borrow_them(world):
    w = world
    lease = await reserve_run(w.task["execution_session_id"], w.owner)
    token = bind_current_lease(lease)
    try:
        async with w.direct.request_context(session_id=w.shared.id):
            assert (await w.direct.execute("echo delegated")).exit_code == 0
        assert [route for route, _ in w.sent] == ["/execute"]
        assert await w.manager.get_client_any(user_id=w.owner, workspace_id=w.workspace) is not None
    finally:
        reset_current_lease(token)
        await lease.release(session_status="idle")
    sent = len(w.sent)
    main = await reserve_run(w.main.id, w.owner)
    token = bind_current_lease(main)
    try:
        # A trace cannot replace the current Driver identity of the main conversation.
        async with w.direct.request_context(session_id=w.shared.id):
            with pytest.raises(PrivateRuntimeUnavailable):
                await w.direct.execute("must never enter the shared shell")
        assert await w.manager.get_client_any(user_id=w.owner, workspace_id=w.workspace) is None
        assert len(w.sent) == sent
        await w.direct._post("/desktop/lease/release", json={"token": "fixture-held-token"})
        assert [route for route, _ in w.sent[sent:]] == ["/desktop/lease/release"]
    finally:
        reset_current_lease(token)
        await main.release(session_status="idle")


async def test_retained_client_follows_current_sql_privacy_after_engine_reopen(world):
    w = world
    client = await w.manager.get_client(w.shared.id, user_id=w.owner)
    async with get_db_session() as db:
        row = await db.get(Session, w.shared.id)
        row.visibility, row.memory_policy = "private", "assistant_isolated"
    url = get_engine().url.render_as_string(hide_password=False)
    await close_engine()
    init_engine(url)
    before = len(w.sent)
    async with client.request_context(session_id=w.shared.id):
        await client.execute("private visibility and isolated memory keep the shared runtime")
    assert len(w.sent) == before + 1
    async with client.request_context(session_id=w.main.id):
        with pytest.raises(PrivateRuntimeUnavailable):
            await client.execute("the assistant conversation must not use a cached shared client")
    assert len(w.sent) == before + 1


async def test_files_given_to_the_assistant_reach_the_owner_sessions_and_nobody_else(world, record_property):
    w = world
    task_session = w.task["execution_session_id"]
    paths = await deliver_asset_ids(task_session, w.owner, [w.private_asset.id, w.shared_asset.id])
    assert set(paths) == {"/workspace/uploads/private-note.txt", "/workspace/uploads/shared-note.txt"}
    assert set(w.files) == set(paths) and set(w.signed) == {w.private_asset.oss_key, w.shared_asset.oss_key}
    # Ownership decides, not the source conversation: the owner's ordinary
    # project Session may receive the same file.
    assert await deliver_asset_ids(w.shared.id, w.owner, [w.private_asset.id]) == ["/workspace/uploads/private-note.txt"]
    signed = list(w.signed)
    peer_session = await create_session(user_id=w.peer, workspace_id=w.workspace, project_id=w.main.project_id)
    with pytest.raises(AssetDeliveryError) as denied:
        await deliver_asset_ids(peer_session.id, w.peer, [w.private_asset.id])
    assert denied.value.code == "asset_unavailable" and denied.value.retryable is False
    # A direct transfer must name (or run as) the owner: no destination, or
    # another member, is refused before CLI preparation or URL signing.
    for destination, reason in (({}, "所有者"), ({"user_id": w.peer, "workspace_id": w.workspace}, "来源")):
        with pytest.raises(PrivateRuntimeUnavailable, match=reason):
            await deliver(w.direct, w.record["id"], w.oss, [w.private_asset], **destination)
    assert w.signed == signed
    assert await deliver(w.direct, w.record["id"], w.oss, [w.private_asset],
                         user_id=w.owner, workspace_id=w.workspace) == ["/workspace/uploads/private-note.txt"]
    record_property("assistant_file_delivery", json.dumps({"asset_id": w.private_asset.id,
        "source_session": w.private_asset.session_id, "task_session": task_session,
        "landed": sorted(paths), "peer_refused": True}))


async def test_asset_delivery_does_not_trust_old_source_metadata(world):
    w = world
    async with get_db_session() as db:
        (await db.get(FileAsset, w.shared_asset.id)).session_id = w.main.id
    # The retained Python object still describes an ordinary source; current
    # SQL names the assistant conversation, so the file's identity changed.
    assert w.shared_asset.session_id == w.shared.id
    with pytest.raises(PrivateRuntimeUnavailable):
        await deliver(w.direct, w.record["id"], w.oss, [w.shared_asset], user_id=w.owner, workspace_id=w.workspace)
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


async def test_delegated_real_loop_runs_with_the_shared_sandbox(world, monkeypatch, record_property):
    w, calls = world, []
    async def stream(**kwargs):
        ctx = kwargs["ctx"]
        calls.append((ctx.run_id, ctx.run_generation))
        assert ctx.sandbox is not None and ctx.sandbox.private_runtime_route is None
        assert ctx.sandbox.workspace_id == w.workspace and ctx.sandbox_error is None
        assert "私有执行隔离" not in json.dumps(kwargs["system"], ensure_ascii=False)
        yield {"type": "text_delta", "text": "Delegated work runs on the workspace cloud desktop."}
        yield {"type": "finish", "reason": "stop", "usage": {}}
    await _real_loop(w, monkeypatch, stream)
    lease = await reserve_run(w.task["execution_session_id"], w.owner)
    try:
        answer = await loop.run_loop(lease.session_id, user_id=w.owner, lease=lease)
    finally:
        await lease.release(session_status="idle")
    assert len(calls) == 1 and w.resolutions and "/execute" in [path for path, _ in w.sent]
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, w.task["inbox_id"])
        result = await db.scalar(select(TaskResult).where(TaskResult.task_id == w.task["task_id"]))
        assert item.state == "settled" and item.outcome == "succeeded"
        assert result.result_message_id == answer.id and result.outcome == "succeeded"
        record_property("delegated_runtime_loop", json.dumps({"task": w.task, "run_id": lease.run_id,
            "generation": lease.generation, "result_id": result.id, "shared_runtime_requests": len(w.sent)}))


async def test_claimed_assistant_file_reaches_the_task_runtime_before_the_model(world, monkeypatch, record_property):
    w = world
    accepted = await accept_task_command(**{**w.args, "idempotency_key": "assistant-file-run",
        "attachments": (w.private_asset.id,)})
    landed = []
    async def stream(**kwargs):
        landed.append(sorted(w.files))
        yield {"type": "text_delta", "text": "Read the delivered note."}
        yield {"type": "finish", "reason": "stop", "usage": {}}
    await _real_loop(w, monkeypatch, stream)
    lease = await reserve_run(accepted["execution_session_id"], w.owner)
    try:
        await loop.run_loop(lease.session_id, user_id=w.owner, lease=lease)
    finally:
        await lease.release(session_status="idle")
    assert landed == [["/workspace/uploads/private-note.txt"]] and w.signed == [w.private_asset.oss_key]
    async with get_db_session() as db:
        item = await db.get(AgentInboxItem, accepted["inbox_id"])
        results = list((await db.scalars(select(TaskResult).where(TaskResult.task_id == accepted["task_id"]))).all())
        assert item.state == "settled" and item.outcome == "succeeded" and item.delivery_last_error is None
        assert len(results) == 1 and results[0].outcome == "succeeded"
        record_property("assistant_file_task_delivery", json.dumps({"receipt": accepted, "run_id": lease.run_id,
            "generation": lease.generation, "result_id": results[0].id, "attempts": item.delivery_attempts}))


async def _publish_world(w, monkeypatch):
    from publish import desktop_service as publish
    stamp = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(PlatformAccount(id="privacy-account-" + w.workspace, workspace_id=w.workspace,
            bound_by_user_id=w.owner, platform="douyin_creator", auth_kind="desktop_cookie", external_id="fixture",
            nickname="Fixture account", status="bound", desktop_id=w.record["desktop_id"],
            bound_at=stamp, created_at=stamp, updated_at=stamp))
        for asset in (w.private_asset, w.shared_asset):
            (await db.get(FileAsset, asset.id)).mime = "video/mp4"
    monkeypatch.setattr(publish, "_now", lambda: stamp.replace(hour=2))  # 10:00 Shanghai, inside existing posting window.


@pytest.mark.parametrize("source", ["delegated_session", "assistant_file", "both"])
async def test_publish_from_delegated_work_reaches_the_shared_desktop(world, monkeypatch, source):
    from tool.desktop_publish import DesktopPublishArgs, execute
    w = world
    await _publish_world(w, monkeypatch)
    session_id = w.shared.id if source == "assistant_file" else w.task["execution_session_id"]
    asset = w.shared_asset if source == "delegated_session" else w.private_asset
    w.fail_http = True
    result = await execute(DesktopPublishArgs(action="publish", mode="auto", title="Delegated draft",
        asset_id=asset.id, dry_run=True), ToolContext(user_id=w.owner, workspace_id=w.workspace, session_id=session_id))
    # No privacy refusal: it reaches the shared desktop transport (which this
    # fixture fails) like any ordinary project Session's publication.
    assert result.metadata["refused"] and result.metadata["retryable"] and result.metadata["job_id"]
    assert w.sent and "私有执行" not in result.output
    async with get_db_session() as db:
        jobs = list((await db.scalars(select(PublishJob).where(PublishJob.workspace_id == w.workspace))).all())
        assert len(jobs) == 1 and jobs[0].title == "Delegated draft" and jobs[0].status == "failed"


async def test_the_assistant_conversation_itself_cannot_publish_through_the_shared_desktop(world, monkeypatch):
    from tool.desktop_publish import DesktopPublishArgs, execute
    w = world
    await _publish_world(w, monkeypatch)
    result = await execute(DesktopPublishArgs(action="publish", mode="auto", title="PRIVATE_TITLE_CANARY",
        asset_id=w.shared_asset.id, dry_run=True), ToolContext(user_id=w.owner, workspace_id=w.workspace,
        session_id=w.main.id))
    assert result.metadata["refused"] and result.metadata["retryable"] is False
    assert not result.metadata["job_id"] and "私有执行" in result.output
    async with get_db_session() as db:
        assert await db.scalar(select(func.count()).select_from(PublishJob).where(PublishJob.workspace_id == w.workspace)) == 0
    assert w.sent == w.signed == []
