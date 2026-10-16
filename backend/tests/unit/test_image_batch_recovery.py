"""Production regressions: serial paid children, terminal feedback and replay."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from agent.hooks import ToolHooks
from agent.nested_tool_runtime import NestedToolRuntime
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.message import Message
from db.models.notification import Notification
from db.models.part import Part
from tests.unit.test_image_gen import fenced_image_env, _seed_image_run, _effect_and_assets, _sdk_status_error
from tool.image_gen import ImageGenArgs, ProviderTarget, execute, image_gen_tool
from tool.tool import tool_body_timeout


@pytest.fixture(autouse=True)
async def isolated_database(tmp_path, ensure_test_db):
    # Driver monitors and notification badge reads open concurrent sessions.
    # A file database keeps their transactions independent (unlike SQLite's
    # single shared in-memory connection), and isolates restart-scan fixtures.
    from db.base import Base, close_engine, init_engine
    await close_engine()
    engine = init_engine(f"sqlite+aiosqlite:///{tmp_path / 'image-qa.sqlite'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield
    await close_engine()


@pytest.mark.parametrize("received,requested", [(2, 1), (1, 3)])
async def test_actual_outputs_are_saved_and_replay_never_resends_or_overcharges(
        fenced_image_env, monkeypatch, received, requested):
    user, lease, context = await _seed_image_run("image-count")
    calls, charges = [], []

    async def provider(*_args, **_kwargs):
        calls.append(1)
        return [b"\x89PNG\r\n\x1a\n" + bytes([i]) for i in range(received)]

    async def charge(**kw):
        charges.append(kw)

    monkeypatch.setattr(fenced_image_env, "_call_provider", provider)
    monkeypatch.setattr("billing.media.settle_image", charge)
    try:
        args = ImageGenArgs(prompt="a dessert on white", n=requested, input_images=["source"])
        result = await execute(args, context("part-count"))
        assert len(result.metadata["asset_ids"]) == received
        assert result.metadata.get("error", False) == (received < requested)
        effects, states = await _effect_and_assets(user, result.metadata["asset_ids"])
        assert states == {"ready"}
        assert effects[0].state == ("failed" if received < requested else "succeeded")
        replay = await execute(args, context("part-count"))
        assert replay.metadata["asset_ids"] == result.metadata["asset_ids"]
        assert len(calls) == 1
        assert {c["key"] for c in charges} == {"image:part-count"}
        assert {c["count"] for c in charges} == {min(received, requested)}
        async with get_db_session() as db:
            assets = (await db.execute(select(FileAsset).where(FileAsset.user_id == user))).scalars().all()
        assert all(a.status in {"ready", "rejected"} for a in assets)
    finally:
        await lease.release(session_status="idle")


async def test_cancel_after_send_closes_assets_and_notifies_once_without_replay(fenced_image_env, monkeypatch):
    user, lease, context = await _seed_image_run("image-cancel")
    sent = asyncio.Event()
    calls = []

    async def provider(*_args, **_kwargs):
        calls.append(1)
        sent.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(fenced_image_env, "_call_provider", provider)
    ctx = context("part-cancel")
    from db.models.workspace import Workspace, WorkspaceMember
    from db.models.session import Session
    from db.models.project import Project
    async with get_db_session() as db:
        now = datetime.now(timezone.utc)
        workspace_id = f"ws-{user}"
        db.add(Workspace(id=workspace_id, name="Image QA", owner_user_id=user, created_at=now, updated_at=now))
        db.add(WorkspaceMember(workspace_id=workspace_id, user_id=user, role="owner", status="active", created_at=now, updated_at=now))
        await db.execute(update(Session).where(Session.id == ctx.session_id).values(workspace_id=workspace_id))
        await db.execute(update(Project).where(Project.id == ctx.project_id).values(workspace_id=workspace_id))
        await db.flush()
    from notifications.store import can_receive
    async with get_db_session() as db:
        assert await can_receive(db, user, workspace_id, ctx.session_id)
    try:
        task = asyncio.create_task(execute(ImageGenArgs(prompt="a chair"), ctx))
        await asyncio.wait_for(sent.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        async with get_db_session() as db:
            assets = (await db.execute(select(FileAsset).where(FileAsset.user_id == user))).scalars().all()
        assert {a.status for a in assets} == {"outcome_unknown"}
        await execute(ImageGenArgs(prompt="a chair"), context("part-cancel"))
        effects, _ = await _effect_and_assets(user, [])
        assert effects[0].state == "outcome_unknown" and len(calls) == 1
        from agent.effect_ledger import recover_effect_once
        await lease.release(session_status="idle")
        assert await recover_effect_once(effects[0].id) == "manual_review"
        async with get_db_session() as db:
            notices = (await db.execute(select(Notification).where(Notification.user_id == user))).scalars().all()
            states = (await db.execute(select(FileAsset.status).where(FileAsset.user_id == user))).scalars().all()
        assert states == ["manual_review"]
        assert len(notices) == 1
    finally:
        if lease is not None:
            await lease.release(session_status="idle")


async def test_seven_image_children_are_serial_commit_immediately_and_continue_after_failure(fenced_image_env, monkeypatch):
    user, lease, context = await _seed_image_run("image-batch")
    ctx = context("part-batch")
    ctx._tool_execution_lookup = {"image_gen": image_gen_tool}
    ctx.available_tools = frozenset({"batch", "image_gen"})
    async with get_db_session() as db:
        db.add(Message(id=ctx.message_id, session_id=ctx.session_id, user_id=user,
                       role="assistant", created_at=datetime.now(timezone.utc)))
    hooks = ToolHooks(session_id=ctx.session_id, user_id=user)

    async def allow(*_args, **_kw):
        return None

    hooks.authorize_tool = allow
    progress, calls = [], []
    ctx._on_output = lambda text: _append(progress, text)
    active = 0

    async def provider(_target, args, **kw):
        nonlocal active
        assert active == 0
        active += 1
        try:
            if calls:
                async with get_db_session() as db:
                    previous = await db.get(Part, calls[-1])
                    assert previous.data["status"] in {"completed", "error"}
            calls.append(kw["ctx"].part_id)
            await asyncio.sleep(0.001)
            if args.prompt == "dessert 2":
                raise _sdk_status_error(403, "not available")
            return [b"\x89PNG\r\n\x1a\ndessert"]
        finally:
            active -= 1

    monkeypatch.setattr(fenced_image_env, "_call_provider", provider)
    runtime = NestedToolRuntime(hooks, ctx)
    invocations = [("image_gen", {"prompt": f"dessert {i}"}) for i in range(7)]
    try:
        results = await runtime.execute_batch(invocations)
        assert len(results) == 7 and len(calls) == len(set(calls)) == 7
        assert sum(bool(r.metadata.get("error")) for r in results) == 1
        assert "6 completed, 1 failed, 0 remaining" in progress[-1]
        async with get_db_session() as db:
            parts = (await db.execute(select(Part).where(Part.session_id == ctx.session_id))).scalars().all()
        assert len(parts) == 7 and all(p.data["status"] in {"completed", "error"} for p in parts)
        await runtime.execute_batch(invocations)
        assert len(calls) == 7
    finally:
        await lease.release(session_status="idle")


async def _append(items, value):
    items.append(value)


def test_composite_deadline_covers_serial_image_budgets():
    from tool.batch import batch_tool
    ctx = SimpleNamespace(_tool_execution_lookup={"image_gen": image_gen_tool})
    one = tool_body_timeout(image_gen_tool, {}, ctx)
    total = tool_body_timeout(batch_tool, {"invocations": [{"tool": "image_gen"}] * 7}, ctx)
    assert image_gen_tool.parallel_safe is False and image_gen_tool.batch_serial_safe is True
    assert total > 7 * one > 600


async def test_generation_and_edits_share_batch_limit_before_any_provider_call(monkeypatch):
    from tool.batch import BatchArgs, execute as batch, image_batch_limit
    from tool.tool import ToolContext
    monkeypatch.setenv("IMAGE_BATCH_MAX_CALLS", "3")
    assert image_batch_limit() == 3
    class Dispatcher:
        async def execute_batch(self, _invocations):
            pytest.fail("An over-limit batch reached dispatch")
    ctx = ToolContext(_tool_execution_lookup={"image_gen": image_gen_tool}, _nested_tool_runtime=Dispatcher())
    result = await batch(BatchArgs(invocations=[
        {"tool": "image_gen", "parameters": {"prompt": "generate"}},
        *[{"tool": "image_gen", "parameters": {"prompt": "edit", "input_images": ["source"]}}] * 3,
    ]), ctx)
    assert result.metadata["failure_code"] == "image_batch_limit_exceeded"
    assert result.metadata["max_batch_calls"] == 3


def test_native_calls_and_multiple_batches_share_the_same_limit(monkeypatch):
    from tool.batch import batch_tool, image_call_count, image_batch_rejection
    lookup = {"image_gen": image_gen_tool, "batch": batch_tool}
    monkeypatch.setenv("IMAGE_BATCH_MAX_CALLS", "5")
    calls = [("image_gen", {}), ("batch", {"invocations": [{"tool": "image_gen"}] * 5})]
    count = sum(image_call_count(name, args, lookup) for name, args in calls)
    assert image_batch_rejection(count).metadata["requested_calls"] == 6
    assert image_batch_rejection(5) is None


async def test_provider_deadline_leaves_time_for_terminal_cleanup(fenced_image_env, monkeypatch):
    user, lease, context = await _seed_image_run("image-deadline")
    target, settings = fenced_image_env._configured_target()
    monkeypatch.setattr(fenced_image_env, "_configured_target", lambda: (
        ProviderTarget(target.provider, target.model, target.api_key, target.base_url, 0.02), settings))

    async def provider(*_args, **_kw):
        await asyncio.Event().wait()

    monkeypatch.setattr(fenced_image_env, "_call_provider", provider)
    try:
        result = await asyncio.wait_for(execute(ImageGenArgs(prompt="a lamp"), context("part-deadline")), 2)
        assert result.metadata["do_not_retry"] and result.metadata["outcome_unknown"]
        _, states = await _effect_and_assets(user, result.metadata["asset_ids"])
        assert states == {"outcome_unknown"}
    finally:
        await lease.release(session_status="idle")


@pytest.mark.parametrize("valid_digest", [True, False])
async def test_recovery_uses_owned_uploaded_bytes_and_the_original_billing_key(
        fenced_image_env, monkeypatch, valid_digest):
    from agent.effect_ledger import recover_effect_once
    import core.oss
    user, lease, context = await _seed_image_run("image-upload")
    data = b"\x89PNG\r\n\x1a\nretained"
    sent, uploaded, charged = [], {}, []

    class Oss:
        async def get_object(self, key, **_kw):
            return uploaded[key] if valid_digest else b"wrong digest"

    async def provider(*_args, **_kw):
        sent.append(1)
        return [data]

    async def interrupted(_ctx, _oss, _data, *_args, reserved_asset):
        uploaded[reserved_asset.oss_key] = _data
        raise asyncio.CancelledError()

    async def charge(**kw):
        charged.append(kw)

    monkeypatch.setattr(core.oss, "get_oss", Oss)
    monkeypatch.setattr(fenced_image_env, "_call_provider", provider)
    monkeypatch.setattr(fenced_image_env, "_store_output", interrupted)
    monkeypatch.setattr("billing.media.settle_image", charge)
    try:
        with pytest.raises(asyncio.CancelledError):
            await execute(ImageGenArgs(prompt="a red mug"), context("part-upload"))
        effects, _ = await _effect_and_assets(user, [])
    finally:
        await lease.release(session_status="idle")
    assert await recover_effect_once(effects[0].id) == ("reconciled" if valid_digest else "manual_review")
    async with get_db_session() as db:
        states = (await db.execute(select(FileAsset.status).where(FileAsset.user_id == user))).scalars().all()
    assert states == ["ready" if valid_digest else "manual_review"]
    assert len(sent) == 1
    assert [c["key"] for c in charged] == (["image:part-upload"] if valid_digest else [])
