"""The front desk's direct reads on the real schema, and the facts the next greeting starts from.

Each read has exactly the assistant tool's authority (user, workspace, main
session): a stranger's call gets ``unavailable``, never someone else's rows.
"""
import asyncio
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import pytest

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


@pytest.mark.parametrize("policy", ["allowed", "uncertain", "current_only", "no_history"])
async def test_asset_descriptions_keep_identity_and_origin_without_bypassing_read_scope(policy):
    from db.models.file_asset import FileAsset
    from memory.packet import TurnContext
    from tests.unit.test_assistant_assets import asset_for
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    chosen = await asset_for(owner, workspace, name="IMG_123.jpg", mime="image/jpeg",
        description="已选定的店面宣传素材", description_state="manual")
    await asset_for(owner, workspace, name="IMG_123.jpg", mime="image/jpeg",
        description="备用门店照片", description_state="ready")
    await asset_for(other, workspace, name="IMG_123.jpg", mime="image/jpeg",
        description="已选定的店面宣传素材", description_state="manual")
    async with get_db_session() as db:
        (await db.get(FileAsset, chosen.id)).name = "宣传照片.jpg"
    scope = replace(scope_of(owner, workspace, main.id), memory_turn=TurnContext(
        user_id=owner, workspace_id=workspace, query="哪张是我选定的图片", read_policy=policy))
    result = await tools.run("resources_list", scope, '{"query":"已选定","kind":"image"}')
    if policy == "allowed":
        row, = result["items"]
        assert row["id"] == chosen.id and row["name"] == "宣传照片.jpg"
        assert row["description"] == "已选定的店面宣传素材" and row["description_state"] == "manual"
    else:
        assert result["checked"] is False and result["items"] == []
        assert result["reason"] == ("read_scope_unresolved" if policy == "uncertain" else "current_turn_restricted")


async def test_asset_name_miss_returns_current_candidates_with_the_same_filters():
    from tests.unit.test_assistant_assets import asset_for
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    chosen = await asset_for(owner, workspace, name="IMG_123.jpg", mime="image/jpeg",
        session_id=main.id, description="用于宣传的选定版本", description_state="manual")
    await asset_for(owner, workspace, name="IMG_123.jpg", mime="image/jpeg", description="别的会话")
    await asset_for(owner, workspace, name="recording.mp4", mime="video/mp4", session_id=main.id)
    await asset_for(other, workspace, name="IMG_123.jpg", mime="image/jpeg", session_id=main.id)
    result = await tools.run("resources_list", scope_of(owner, workspace, main.id),
        json.dumps({"query": "修改前的文件名", "kind": "image", "session_id": main.id}))
    assert result["status"] == "ok" and result["checked"] is True
    assert result["query_matched"] is False and result["items"] == []
    row, = result["candidates"]
    assert row["id"] == chosen.id and row["name"] == "IMG_123.jpg"
    assert row["description"] == "用于宣传的选定版本" and row["description_state"] == "manual"
    assert "selected_id" not in result


async def test_voice_and_assistant_read_current_model_catalogue_after_removal(monkeypatch):
    from assistant.model_choices import catalogue
    from core.config import OpenBoxConfig
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    config = OpenBoxConfig(model="openai/qwen3.8-flash", models=[
        {"id": "openai/gpt-5.6-luna", "name": "GPT-5.6 Luna", "vision": True},
        {"id": "openai/qwen3.8-flash", "name": "Qwen3.8 Flash", "vision": True}])
    monkeypatch.setattr("core.config.get_config", lambda: config)
    scope = scope_of(owner, workspace, main.id)
    assert len((await tools.run("models_available", scope, "{}"))["chat"]) == 2
    config.models = [row for row in config.models if row.id == config.model]
    spoken = await tools.run("models_available", scope, "{}")
    assert [{k: v for k, v in row.items() if k != "name"} for row in spoken["chat"]] == [
        {k: v for k, v in row.items() if k != "name"} for row in catalogue()["chat"]]
    assert spoken["chat"][0]["name"] == "其他任务档位"
    assert [row["id"] for row in spoken["chat"]] == [config.model]


async def test_voice_summary_never_calls_a_removed_model(monkeypatch):
    from core.config import OpenBoxConfig
    config = OpenBoxConfig(model="openai/qwen3.8-flash", models=[{"id": "openai/qwen3.8-flash"}],
                           voice={"summary_model": "openai/gpt-5.6-luna"})
    monkeypatch.setattr("core.config.get_config", lambda: config)
    async def complete(model, *args):
        assert model == "openai/qwen3.8-flash"
        return "聊了水果店的招聘海报。"
    monkeypatch.setattr(summary, "complete", complete)
    assert await summary.summarize("用户：这是水果店的招聘海报。") == "聊了水果店的招聘海报。"


async def test_tasks_overview_reads_the_watch_list_the_assistant_reads():
    owner, workspace, main, accepted, lease, _ = await result_ready()
    await lease.release(session_status="idle")
    value = await tools.run("tasks_overview", scope_of(owner, workspace, main.id), "{}")
    assert value["status"] == "ok" and value["more"] is False
    [task] = value["tasks"]
    assert task["title"] == task["project"] == "默认空间"  # an untitled task goes by its project
    assert task["state"] == "completed" and task["state_label"] == "已完成"
    assert task["latest"] == "The report is saved. Browser verification is still untested." and "月" in task["latest_at"]
    assert value["schedules"] == [] and "交给个人助理" in value["note"]  # scheduled jobs ride along
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
