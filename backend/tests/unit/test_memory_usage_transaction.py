"""Current ranking and display writes share a transaction, never authority."""
import pytest

from db.base import get_db_session
from db.models.memory import UserMemory
from db.models.memory_v2 import MemoryRecall
from memory import recalls, service
from tests.unit.test_memory_write_protocol import setup
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401


async def test_usage_deduplicates_core_hits_and_keeps_only_recalled_display_ids(monkeypatch):
    owner, other, workspace, main, message = await setup(monkeypatch)
    note = await service.create_note(user_id=owner, workspace_id=workspace, summary="周末爱拼模型")
    core = await service.create_note(user_id=owner, workspace_id=workspace, summary="喜欢散步")
    foreign = await service.create_note(user_id=other, workspace_id=workspace, summary="别人的偏好")
    item = lambda x: {"id": x["id"], "kind": "memory"}
    bundle = {"items": [item(note)], "stable_background": {"items": [item(note), item(core), item(foreign)]}}
    await recalls.record_usage(user_id=owner, workspace_id=workspace, project_id=None,
        include_all_projects=True, session_id=main.id, message_id=message.id, bundle=bundle)
    async with get_db_session() as db:
        assert (await db.get(UserMemory, note["id"])).hit_count == 1
        assert (await db.get(UserMemory, core["id"])).hit_count == 1
        assert (await db.get(UserMemory, foreign["id"])).hit_count == 0
        assert (await db.get(MemoryRecall, message.id)).memory_ids == [note["id"]]


async def test_usage_rolls_back_counters_if_display_write_fails(monkeypatch):
    owner, _, workspace, main, message = await setup(monkeypatch)
    note = await service.create_note(user_id=owner, workspace_id=workspace, summary="周末爱拼模型")
    async def fail(*args):
        raise RuntimeError("interrupted display write")
    monkeypatch.setattr(recalls, "_record_locked", fail)
    with pytest.raises(RuntimeError):
        await recalls.record_usage(user_id=owner, workspace_id=workspace, project_id=None,
            include_all_projects=True, session_id=main.id, message_id=message.id,
            bundle={"items": [{"id": note["id"], "kind": "memory"}]})
    async with get_db_session() as db:
        assert (await db.get(UserMemory, note["id"])).hit_count == 0
        assert await db.get(MemoryRecall, message.id) is None


async def test_packet_refresh_updates_pause_and_sharing_from_current_sql(monkeypatch):
    from sqlalchemy import update
    from db.models.workspace import WorkspaceMember
    from memory import packet, settings
    from memory.policy import resolve_access_scope
    from tests.unit.test_memory_read_protocol import context
    config, turn = await context(monkeypatch)
    config.lazy_read_planning = True
    bundle = await packet.prepare_context(turn, config)
    await settings.update_settings(turn.user_id, session_id=turn.session_id, session_paused=True)
    async with get_db_session() as db:
        await db.execute(update(WorkspaceMember).where(WorkspaceMember.workspace_id == turn.workspace_id,
            WorkspaceMember.user_id != turn.user_id).values(status="inactive"))
        scope = await resolve_access_scope(db, user_id=turn.user_id, workspace_id=turn.workspace_id,
            include_all_projects=True)
    current = await packet.refresh_packet(bundle, scope, config)
    assert current["saving_paused"] == "chat" and current["shared_chat"] is False
    assert current["settings"]["address"] == "老板"
