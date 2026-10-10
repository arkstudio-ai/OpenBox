"""Regenerating or dismissing one turn must not withdraw what other turns taught."""
import pytest

from agent import inbox
from core import config as runtime_config
from db.base import get_db_session
from memory import service
from memory.extraction import MemoryExtractionWorker
from memory.policy import resolve_access_scope
from memory.retrieval import authorized_documents
from session.agent_event_log import (
    append_surface_remove_locked,
    prepare_agent_event_write,
)
from tests.unit.test_automatic_knowledge import Verifier
from tests.unit.test_memory_pipeline import (  # noqa: F401
    _finish_turn,
    _seed,
    pipeline_database,
)

FACT = "我对菠萝过敏。"
SUMMARY = "用户对菠萝过敏。"


def allergy(frozen):
    if FACT not in frozen.sources[0]["body"]:
        return {"candidates": []}
    return {"candidates": [{"type": "CONSTRAINT", "summary": SUMMARY, "fact_key": "personal.allergy.pineapple",
        "confidence": 95, "source_indexes": [0], "quotes": [{"source_index": 0, "quote": FACT}]}]}


async def remembered(seed):
    listed = [m["summary"] for m in await service.list_active_memories(
        user_id=seed[0], workspace_id=seed[1], project_id=seed[2])]
    config = runtime_config.get_config().memory
    async with get_db_session() as db:
        access = await resolve_access_scope(db, user_id=seed[0], workspace_id=seed[1], project_id=seed[2])
        recalled = [d.text for d in await authorized_documents(db, access, config) if d.kind == "memory"]
    return listed, recalled


async def remove(seed, *message_ids):
    async with get_db_session() as db:
        owner = await prepare_agent_event_write(db, session_id=seed[3], user_id=seed[0], run_fence=None)
        await append_surface_remove_locked(db, owner, message_ids=list(message_ids))


async def automatic(monkeypatch):
    seed = await _seed(monkeypatch)
    runtime_config.get_config().memory.automatic_knowledge = True
    return seed


@pytest.mark.asyncio
async def test_regenerating_a_later_reply_keeps_what_an_earlier_turn_taught(monkeypatch):
    seed = await automatic(monkeypatch)
    await _finish_turn(seed, text=FACT)
    assert await MemoryExtractionWorker(extractor=allergy, verifier=Verifier()).run_once() == "SUCCEEDED"
    _, _, later = await _finish_turn(seed, text="今天天气怎么样？")
    await remove(seed, later.id)
    assert await remembered(seed) == ([SUMMARY], [SUMMARY])


@pytest.mark.asyncio
async def test_removing_the_statement_itself_still_withdraws_its_memory(monkeypatch):
    seed = await automatic(monkeypatch)
    accepted, _, reply = await _finish_turn(seed, text=FACT)
    assert await MemoryExtractionWorker(extractor=allergy, verifier=Verifier()).run_once() == "SUCCEEDED"
    statement = (await inbox.get_inbox_item(accepted.id, user_id=seed[0])).message_id
    await remove(seed, statement, reply.id)
    assert await remembered(seed) == ([], [])


@pytest.mark.asyncio
async def test_pending_extraction_survives_a_later_regenerate(monkeypatch):
    seed = await automatic(monkeypatch)
    await _finish_turn(seed, text=FACT)
    _, _, later = await _finish_turn(seed, text="今天天气怎么样？")
    await remove(seed, later.id)
    worker = MemoryExtractionWorker(extractor=allergy, verifier=Verifier())
    assert sorted([await worker.run_once(), await worker.run_once()]) == ["CANCELLED", "SUCCEEDED"]
    assert (await remembered(seed))[0] == [SUMMARY]


@pytest.mark.asyncio
async def test_a_chat_reports_how_many_memories_rest_on_it(monkeypatch):
    seed = await automatic(monkeypatch)
    await _finish_turn(seed, text=FACT)
    assert await MemoryExtractionWorker(extractor=allergy, verifier=Verifier()).run_once() == "SUCCEEDED"
    await service.create_note(user_id=seed[0], workspace_id=seed[1], project_id=seed[2], summary="周报每周五发")
    count = service.count_learned_from_session
    assert await count(user_id=seed[0], workspace_id=seed[1], session_id=seed[3]) == 1
    assert await count(user_id=seed[0], workspace_id=seed[1], session_id="another-chat") == 0


@pytest.mark.asyncio
async def test_two_facts_from_one_message_are_recalled_together(monkeypatch):
    from core.config import MemoryConfig
    from memory.retrieval import search_memory
    seed = await automatic(monkeypatch)
    said = "青柠项目预算一千二百元，演示日期定在十月九日。"
    await _finish_turn(seed, text=said)

    def both(frozen):
        return {"candidates": [
            {"type": "PROJECT_CONTEXT", "summary": "青柠项目预算一千二百元。", "fact_key": "project.lime.budget",
             "confidence": 95, "source_indexes": [0], "quotes": [{"source_index": 0, "quote": "青柠项目预算一千二百元"}]},
            {"type": "PROJECT_CONTEXT", "summary": "青柠项目演示日期定在十月九日。", "fact_key": "project.lime.demo_date",
             "confidence": 95, "source_indexes": [0], "quotes": [{"source_index": 0, "quote": "演示日期定在十月九日"}]}]}
    assert await MemoryExtractionWorker(extractor=both, verifier=Verifier()).run_once() == "SUCCEEDED"
    config = MemoryConfig(retrieval_v2=False, route_jev=False, allowed_user_ids=[seed[0]])
    bundle = await search_memory(query="青柠项目 预算 演示日期", user_id=seed[0], workspace_id=seed[1],
                                 project_id=seed[2], config=config)
    recalled = {item["text"] for item in bundle["items"] if item["kind"] == "memory"}
    assert recalled == {"青柠项目预算一千二百元。", "青柠项目演示日期定在十月九日。"}


@pytest.mark.asyncio
async def test_a_delegated_task_prompt_is_never_taken_as_the_persons_words(monkeypatch):
    from datetime import datetime, timezone
    from sqlalchemy import func, select, update
    from db.models.memory_pipeline import MemoryExtractionJob
    from db.models.session import Session
    seed = await automatic(monkeypatch)
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(Session(id=f"{seed[3]}-parent", user_id=seed[0], workspace_id=seed[1], project_id=seed[2],
                       model="test/model", agent="build", status="idle", token_usage={}, tool_exposure_state={},
                       created_at=now, updated_at=now))
        await db.flush()
        # The seeded chat becomes a subagent child whose "user" turn the parent wrote.
        await db.execute(update(Session).where(Session.id == seed[3]).values(parent_id=f"{seed[3]}-parent"))
    await _finish_turn(seed, text="用户偏好极简风格，预算不限。")
    async with get_db_session() as db:
        assert await db.scalar(select(func.count(MemoryExtractionJob.id)).where(
            MemoryExtractionJob.session_id == seed[3])) == 0
