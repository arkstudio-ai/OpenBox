"""A person controls memory: pause a chat or all saving, take everything with them, or clear it."""
import pytest
from sqlalchemy import func, select

from core import config as runtime_config
from db.base import get_db_session
from db.models.memory_pipeline import MemoryExtractionJob
from memory import service, settings
from memory.extraction import MemoryExtractionWorker
from tests.unit.test_automatic_knowledge import Verifier
from tests.unit.test_memory_pipeline import _finish_turn, _job, _seed, pipeline_database  # noqa: F401


def allergy(frozen):
    return {"candidates": [{"type": "CONSTRAINT", "summary": "用户对菠萝过敏。", "fact_key": "personal.allergy",
        "confidence": 95, "source_indexes": [0], "quotes": [{"source_index": 0, "quote": "我对菠萝过敏"}]}]}


async def jobs_for(seed, state="PENDING"):
    async with get_db_session() as db:
        return await db.scalar(select(func.count(MemoryExtractionJob.id)).where(
            MemoryExtractionJob.session_id == seed[3], MemoryExtractionJob.state == state))


async def automatic(monkeypatch):
    seed = await _seed(monkeypatch)
    runtime_config.get_config().memory.automatic_knowledge = True
    return seed


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["chat", "account"])
async def test_nothing_said_while_paused_becomes_a_memory(monkeypatch, scope):
    seed = await automatic(monkeypatch)
    pause = ({"session_id": seed[3], "session_paused": True} if scope == "chat" else {"auto_save": False})
    expected = {"auto_save": True, "session_paused": True} if scope == "chat" else {"auto_save": False, "session_paused": False}
    assert await settings.update_settings(seed[0], **pause) == expected
    await _finish_turn(seed, text="我对菠萝过敏。")
    assert await jobs_for(seed) == 0
    # Recorded as never-to-extract, so turning saving back on cannot revive it.
    assert (await jobs_for(seed, "CANCELLED"), (await _job(seed)).last_error) == (1, "memory_paused")
    resume = ({"session_id": seed[3], "session_paused": False} if scope == "chat" else {"auto_save": True})
    await settings.update_settings(seed[0], **resume)
    await _finish_turn(seed, text="我对菠萝过敏，记住。")
    assert await jobs_for(seed) == 1


@pytest.mark.asyncio
async def test_a_turn_queued_before_pausing_is_not_saved(monkeypatch):
    seed = await automatic(monkeypatch)
    await _finish_turn(seed, text="我对菠萝过敏。")
    await settings.update_settings(seed[0], session_id=seed[3], session_paused=True)
    assert await MemoryExtractionWorker(extractor=allergy, verifier=Verifier()).run_once() == "CANCELLED"
    assert (await _job(seed)).last_error == "memory_paused"
    assert await service.list_active_memories(user_id=seed[0], workspace_id=seed[1], project_id=seed[2]) == []


@pytest.mark.asyncio
async def test_only_your_own_chats_can_be_paused(monkeypatch):
    seed = await automatic(monkeypatch)
    other = await _seed(monkeypatch)
    with pytest.raises(LookupError):
        await settings.update_settings(seed[0], session_id=other[3], session_paused=True)


@pytest.mark.asyncio
async def test_export_lists_what_is_remembered_and_clear_all_forgets_it(monkeypatch):
    seed = await automatic(monkeypatch)
    identity = {"user_id": seed[0], "workspace_id": seed[1]}
    await service.create_note(**identity, project_id=seed[2], summary="周报每周五下午发")
    kept = await service.create_note(**identity, project_id=None, summary="喜欢简短的中文回复")
    forgotten = await service.create_note(**identity, project_id=seed[2], summary="已经忘记的事")
    await service.forget_memory(**identity, memory_id=forgotten["id"], expected_revision=forgotten["revision"])
    text = await service.export_markdown(**identity)
    assert text.startswith("# 我的记忆") and "共 2 条" in text
    assert "周报每周五下午发" in text and "喜欢简短的中文回复" in text and "已经忘记的事" not in text
    assert "## 未归入项目" in text
    assert await service.forget_all(**identity, project_id=seed[2]) == 1
    remaining = await service.page_memories(**identity, status="ACTIVE", include_all_projects=True)
    assert [item["id"] for item in remaining[0]] == [kept["id"]]
    assert await service.forget_all(**identity) == 1
    assert (await service.page_memories(**identity, status="ACTIVE", include_all_projects=True))[0] == []


def allergy_when_said(frozen):
    said = any("菠萝" in source["body"] for source in frozen.sources)
    return allergy(frozen) if said else {"candidates": []}


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["chat", "account"])
async def test_resuming_never_saves_what_was_said_while_paused(monkeypatch, scope):
    seed = await automatic(monkeypatch)
    worker = MemoryExtractionWorker(extractor=allergy_when_said, verifier=Verifier())
    await _finish_turn(seed, text="你好")
    assert await worker.run_once() == "SUCCEEDED"
    pause, resume = (({"session_id": seed[3], "session_paused": True}, {"session_id": seed[3], "session_paused": False})
                     if scope == "chat" else ({"auto_save": False}, {"auto_save": True}))
    await settings.update_settings(seed[0], **pause)
    await _finish_turn(seed, text="我对菠萝过敏。")
    # Turned back on before the background recovery pass ran.
    await settings.update_settings(seed[0], **resume)
    while await worker.run_once() is not None:
        pass
    assert await service.list_active_memories(user_id=seed[0], workspace_id=seed[1], project_id=seed[2]) == []
