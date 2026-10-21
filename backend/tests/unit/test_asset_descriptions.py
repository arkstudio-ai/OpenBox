"""Real SQL leases and user-edit races; provider bytes and completion are explicit."""
import asyncio
from datetime import timedelta
import importlib
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

from assets import descriptions as work
from db.base import get_db_session
from db.models.file_asset import FileAsset
from tests.unit.test_assistant_assets import asset_for
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_sessions_v2 import assistant


async def queued():
    user, _, workspace, main = await assistant()
    return await asset_for(user, workspace, name="IMG_0365.png", mime="image/png", size=100,
                           session_id=main.id, description_state="pending")


async def read(asset):
    async with get_db_session() as db:
        return await db.get(FileAsset, asset.id)


async def test_auto_description_is_persisted_and_shared_search_can_find_it(monkeypatch):
    from assistant.assets import list_assets
    asset = await queued()
    async def describe(row):
        assert row.id == asset.id
        return "白色背景上的红色圆形"
    monkeypatch.setattr(work, "caption", describe)
    await work.process(await work.claim())
    row = await read(asset)
    assert (row.description, row.description_state, row.description_attempts) == ("白色背景上的红色圆形", "ready", 1)
    found = await list_assets(user_id=row.user_id, workspace_id=row.workspace_id, main_id=row.session_id, query="红色圆形")
    assert [x["id"] for x in found["items"]] == [row.id]
    assert await work.claim() is None


@pytest.mark.parametrize("manual", ["给门店用的海报", ""])
async def test_late_ai_never_overwrites_manual_edits_including_clear(monkeypatch, manual):
    asset = await queued()
    entered, release = asyncio.Event(), asyncio.Event()
    async def describe(row):
        entered.set()
        await release.wait()
        return "AI description"
    monkeypatch.setattr(work, "caption", describe)
    task = asyncio.create_task(work.process(await work.claim()))
    await entered.wait()
    async with get_db_session() as db:
        row = await db.get(FileAsset, asset.id)
        row.description, row.description_state, row.description_lease_until = manual, "manual", None
    release.set()
    await task
    row = await read(asset)
    assert (row.description, row.description_state) == (manual, "manual")


async def test_two_workers_claim_once_and_old_lease_cannot_publish_after_recovery(monkeypatch):
    asset = await queued()
    claimed = await asyncio.gather(work.claim(), work.claim())
    assert sum(row is not None for row in claimed) == 1
    old = next(row for row in claimed if row is not None)
    async with get_db_session() as db:
        row = await db.get(FileAsset, asset.id)
        row.description_lease_until = work.now() - timedelta(seconds=1)
    new = await work.claim()
    async def describe(row):
        return "旧结果" if row.description_attempts == 1 else "恢复后的结果"
    monkeypatch.setattr(work, "caption", describe)
    await work.process(old)
    assert (await read(asset)).description == ""
    await work.process(new)
    assert (await read(asset)).description == "恢复后的结果"


async def test_failed_provider_retries_are_bounded_and_upload_stays_ready(monkeypatch):
    asset = await queued()
    async def failed(row):
        raise TimeoutError()
    monkeypatch.setattr(work, "caption", failed)
    await work.process(await work.claim())
    async with get_db_session() as db:
        row = await db.get(FileAsset, asset.id)
        assert row.description_state == "pending" and row.status == "ready"
        row.description_lease_until = work.now() - timedelta(seconds=1)
    await work.process(await work.claim())
    row = await read(asset)
    assert row.description_state == "failed" and row.description == "" and row.status == "ready"
    assert await work.claim() is None


async def test_gateway_first_response_timeout_retries_soon_and_keeps_two_attempt_limit(monkeypatch):
    from agent.gateway import GatewayTimeout
    row = await queued()
    async def failed(_):
        raise GatewayTimeout("first_progress")
    monkeypatch.setattr(work, "caption", failed)
    await work.process(await work.claim())
    saved = await read(row)
    assert saved.description_state == "pending" and saved.status == "ready"
    from datetime import timezone
    assert 0 < (saved.description_lease_until.replace(tzinfo=timezone.utc) - work.now()).total_seconds() <= 2


@pytest.mark.parametrize("finish", [None, "length", "stop"])
async def test_caption_reads_actual_bytes_and_never_saves_partial_stream(monkeypatch, finish):
    row = await queued()
    async def chunks(key, **kwargs):
        assert key == row.oss_key
        yield b"image-content"
    monkeypatch.setattr("core.oss.get_oss", lambda: SimpleNamespace(get_object_chunks=chunks))
    monkeypatch.setattr("core.config.get_config", lambda: SimpleNamespace(
        asset_description=SimpleNamespace(model="openai/qwen3.8-flash"),
        video_analysis=SimpleNamespace(model="openai/retired-vision"),
        model="openai/qwen3.8-flash", models=[]))
    async def stream(_, system, messages, tools, model, ctx, **kwargs):
        assert messages[0]["_images"][0] == "data:image/png;base64,aW1hZ2UtY29udGVudA=="
        assert row.name not in messages[0]["content"] and not tools
        assert ctx.user_id == row.user_id and kwargs["billing_kind"] == "asset_description"
        assert model == "openai/qwen3.8-flash" and kwargs["variant"] == "none"
        from agent.llm import request_output_tokens
        assert request_output_tokens(model, kwargs["variant"], kwargs["max_output_tokens"]) == 400
        yield {"type": "text_delta", "text": "红色圆形"}
        if finish:
            yield {"type": "finish", "reason": finish}
    monkeypatch.setattr("agent.llm.stream_llm", stream)
    if finish == "stop":
        assert await work.caption(row) == "红色圆形"
    else:
        with pytest.raises(ValueError, match="incomplete"):
            await work.caption(row)


@pytest.mark.parametrize("preferred", ["openai/gpt-5.6-luna", "openai/deepseek-v4-flash"])
def test_caption_replaces_retired_or_text_only_model_with_available_vision(monkeypatch, preferred):
    from core.config import OpenBoxConfig
    config = OpenBoxConfig(model="openai/deepseek-v4-flash", models=[
        {"id": "openai/deepseek-v4-flash", "vision": False},
        {"id": "openai/qwen3.8-flash", "vision": True}], asset_description={"model": preferred})
    monkeypatch.setattr("core.config.get_config", lambda: config)
    assert work.caption_model(config) == "openai/qwen3.8-flash"


def test_caption_without_available_vision_fails_before_any_provider_call(monkeypatch):
    from core.config import OpenBoxConfig
    config = OpenBoxConfig(model="openai/deepseek-v4-flash", models=[
        {"id": "openai/deepseek-v4-flash", "vision": False}])
    monkeypatch.setattr("core.config.get_config", lambda: config)
    with pytest.raises(ValueError, match="available vision"):
        work.caption_model(config)


def test_queue_preserves_human_text_and_does_not_backfill_or_retry_on_duplicate_complete():
    row = SimpleNamespace(source="user", transient=False, status="ready", is_deleted=False,
        description="人写的说明", description_state="none", mime="image/png", size=100)
    work.queue(row)
    assert row.description_state == "none"
    row.description = ""
    work.queue(row)
    assert row.description_state == "pending"
    row.description_state = "failed"
    work.queue(row)
    assert row.description_state == "failed"
    work.queue(row, retry=True)
    assert row.description_state == "pending"


async def test_retry_is_owner_scoped_and_cannot_replace_existing_description(monkeypatch):
    from api.assets import describe_asset
    from fastapi import HTTPException
    owner, other, workspace, main = await assistant()
    row = await asset_for(owner, workspace, session_id=main.id, mime="image/png", description="用户写的说明")
    monkeypatch.setattr("api.assets._oss_or_503", lambda: SimpleNamespace(presign_get=lambda key: "local"))
    for user, status in ((other, 404), (owner, 409)):
        with pytest.raises(HTTPException) as denied:
            await describe_asset(row.id, current_user={"user_id": user, "workspace_id": workspace})
        assert denied.value.status_code == status
    assert (await read(row)).description == "用户写的说明"


async def test_deleted_image_never_receives_a_late_caption(monkeypatch):
    row = await queued()
    leased = await work.claim()
    async def describe(_):
        async with get_db_session() as db:
            asset = await db.get(FileAsset, row.id)
            asset.is_deleted = True
        return "不得发布的迟到结果"
    monkeypatch.setattr(work, "caption", describe)
    await work.process(leased)
    assert (await read(row)).description == ""


def test_migration_preserves_existing_metadata_and_refuses_pending_downgrade(tmp_path):
    migration = importlib.import_module("db.migrations.versions.ph73f4a5b6c7_asset_descriptions")
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    with engine.begin() as db, Operations.context(MigrationContext.configure(db)):
        db.exec_driver_sql("CREATE TABLE file_assets (id TEXT PRIMARY KEY, description TEXT)")
        db.exec_driver_sql("INSERT INTO file_assets VALUES ('old', '保留我的描述')")
        migration.upgrade()
        assert db.execute(text("SELECT description, description_state FROM file_assets")).one() == ("保留我的描述", "none")
        db.exec_driver_sql("UPDATE file_assets SET description_state='pending'")
        with pytest.raises(RuntimeError):
            migration.downgrade()
        db.exec_driver_sql("UPDATE file_assets SET description_state='ready'")
        migration.downgrade()
        assert db.scalar(text("SELECT description FROM file_assets")) == "保留我的描述"
    engine.dispose()
