"""The front desk's direct reads on the real schema, and the facts the next greeting starts from.

Each read has exactly the assistant tool's authority (user, workspace, main
session): a stranger's call gets ``unavailable``, never someone else's rows.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from assistant.service import ensure_main_session
from db.base import get_db_session
from db.models.voice import VoiceCall
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tests.unit.test_assistant_results import result_ready
from voice import calls, prompt, summary, tools
from voice.meter import CallMeter
from voice.tools import CallScope


def scope_of(owner, workspace, main_id):
    return CallScope(user_id=owner, workspace_id=workspace, main_session_id=main_id, call_id="call-t")


async def test_tasks_overview_reads_the_watch_list_the_assistant_reads():
    owner, workspace, main, accepted, lease, _ = await result_ready()
    await lease.release(session_status="idle")
    value = await tools.run("tasks_overview", scope_of(owner, workspace, main.id), "{}")
    assert value["status"] == "ok" and value["more"] is False
    [task] = value["tasks"]
    assert task["title"] == task["project"] == "默认空间"  # an untitled task goes by its project
    assert task["state"] == "已完成"
    assert task["latest"] == "The report is saved. Browser verification is still untested." and "月" in task["latest_at"]
    stranger, _, other_workspace = await accounts()
    assert await tools.run("tasks_overview", scope_of(stranger, workspace, main.id), "{}") == {"status": "unavailable"}


async def test_projects_schedules_and_credits_are_read_with_the_main_sessions_authority():
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    scope = scope_of(owner, workspace, main.id)
    projects = await tools.run("projects_list", scope, "{}")
    assert projects["status"] == "ok" and projects["projects"]  # the default project at least
    assert (await tools.run("schedules_list", scope, "{}")) == {"status": "ok", "more": False, "schedules": []}
    credits = await tools.run("credits", scope, "{}")
    assert credits["status"] == "ok" and "balance" in credits and credits["since"]
    stranger, _, _ = await accounts()
    for name in ("projects_list", "schedules_list", "credits"):
        assert await tools.run(name, scope_of(stranger, workspace, main.id), "{}") == {"status": "unavailable"}


async def test_memory_search_is_the_assistants_own_recall_across_projects(monkeypatch):
    """The same recall an assistant turn reads: personal and project memories, documents, knowledge pages."""
    from core.config import get_config
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    scope = scope_of(owner, workspace, main.id)
    assert await tools.run("memory_search", scope, '{"query": " "}') == {"status": "need_query"}
    # Retrieval is off in unit tests: the recall refuses, the call goes on.
    assert await tools.run("memory_search", scope, '{"query": "称呼"}') == {"status": "unavailable"}
    monkeypatch.setattr(get_config(), "memory", get_config().memory.model_copy(update={"retrieval_v2": True}))
    seen = []

    async def search_memory(**kwargs):
        seen.append(kwargs)
        return {"items": [
            {"kind": "memory", "text": "云杉项目负责人是小李", "valid_from": "2026-10-02T01:55:52+00:00"},
            {"kind": "wiki", "text": "# 团队协作\n- **云杉项目**：负责人是小李。"},
            {"kind": "source", "text": "   "}]}
    monkeypatch.setattr("memory.retrieval.search_memory", search_memory)
    found = await tools.run("memory_search", scope, '{"query": "云杉项目的负责人是谁"}')
    assert found == {"status": "ok", "note": tools.DATA_NOTE, "memories": [
        {"text": "云杉项目负责人是小李。", "from": "记忆", "time": "10月2日 09:55"},
        {"text": "团队协作。云杉项目：负责人是小李。", "from": "知识页"}]}  # spoken text: no markdown
    [kwargs] = seen
    assert (kwargs["query"], kwargs["limit"], kwargs["include_all_projects"]) == ("云杉项目的负责人是谁", 5, True)
    assert (kwargs["user_id"], kwargs["workspace_id"]) == (owner, workspace)

    async def nothing(**kwargs):
        return {"items": []}
    monkeypatch.setattr("memory.retrieval.search_memory", nothing)
    assert await tools.run("memory_search", scope, '{"query": "松鼠青柠"}') == {"status": "nothing_found", "memories": []}
    stranger, _, _ = await accounts()  # another user's call never reads this main session's memories
    assert await tools.run("memory_search", scope_of(stranger, workspace, main.id), '{"query": "x"}') == {
        "status": "unavailable"}


async def test_the_next_greeting_knows_the_last_call_and_what_finished_since():
    owner, workspace, main, accepted, lease, _ = await result_ready()
    await lease.release(session_status="idle")
    call_id = await calls.create_call(user_id=owner, workspace_id=workspace, main_session_id=main.id,
                                      client="web", model="m", voice="Serena")
    await calls.finish_call(call_id, status="ended", end_reason="hangup", duration_seconds=30, turns=1,
                            snapshot=CallMeter().snapshot())
    async with get_db_session() as db:  # the result came in after that call
        await db.execute(VoiceCall.__table__.update().where(VoiceCall.id == call_id).values(
            ended_at=datetime.now(timezone.utc) - timedelta(hours=2)))

    async def summarizer(transcript, previous, final):
        assert final and "以后叫我 Mary" in transcript
        return "聊了报告的进展；用户希望以后被叫 Mary。"
    task = summary.save_after_call(call_id, "20:10 用户：以后叫我 Mary\n20:10 前台：好的 Mary。", summarizer=summarizer)
    await asyncio.wait_for(task, 5)
    facts = await prompt.front_context(user_id=owner, workspace_id=workspace, main_session_id=main.id)
    assert facts.last_call.endswith("，聊了报告的进展；用户希望以后被叫 Mary。")
    assert facts.last_call.startswith(("今天", "昨天"))
    assert facts.finished.startswith("「") and "The report is saved" in facts.finished
    instructions = prompt.front_instructions(facts, "zh", prompt.local_now())
    assert "上次通话：" in instructions and "上次通话后办完的事：「" in instructions
    assert summary.save_after_call(call_id, "  ") is None  # nothing said: nothing to keep
