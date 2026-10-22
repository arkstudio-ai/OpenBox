"""Real-schema regressions for the user-reported voice/task consistency races."""
import asyncio
import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantTask, TaskResult
from db.models.file_asset import FileAsset
from question import question as questions
from question.continuation import QuestionContinuationWorker
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_placement import quiet  # noqa: F401
from tests.unit.test_assistant_pending import pause
from tests.unit.test_assistant_sessions_v2 import assistant, card, main_turn, tool_call, conversation
from tests.unit.test_voice_assistant_link import answer
from tests.unit.test_voice_delivery_consistency import facts, finish
from tests.support.voice_fakes import FakeClock, FakeLink, ScriptedProvider, audio, done, event, started
from voice import calls
from voice.assistant_link import AssistantLink, VoiceTurnRef, latest_inbox_id
from voice.bridge import Bridge
from voice.reports import ReportWatcher
from voice.shared_context import SharedContextWatcher
from voice.tools import CallScope
from voice.work import overview


@pytest.mark.parametrize("arrival", ["follow_first", "report_first", "both_waiting"])
async def test_real_question_continuation_is_observed_twice_but_spoken_once(arrival):
    owner, _, workspace, main = await assistant()
    call_id = await calls.create_call(user_id=owner, workspace_id=workspace, main_session_id=main.id,
                                      client="web", model="test/audio", voice="test-voice")
    ctx, lease, human = await main_turn(owner, workspace, main, "帮我写一份新品发布计划",
        origin_ref={"entrypoint": "assistant_voice", "voice_call_id": call_id}, model="test/model")
    asked, _ = await tool_call(ctx, "tasks.submit", {"project_id": main.project_id, "title": "新品发布计划",
        "instructions": "写一份新品发布计划", "source_message_ids": [human],
        "model": "test/model", "model_confirmed": True})
    await pause(ctx, lease)
    question = await card(asked["suspended"])
    watcher = ReportWatcher(user_id=owner, main_session_id=main.id, workspace_id=workspace, call_id=call_id)
    await watcher.start()
    after = await latest_inbox_id(main.id, owner)
    await questions.reply(question.id, [[question.questions[0]["options"][0]["label"]]], owner)
    await QuestionContinuationWorker()._resume_candidate(main.id, owner, question.generation)
    async with get_db_session() as db:
        resumed = await db.scalar(select(AgentInboxItem).where(AgentInboxItem.session_id == main.id,
                                                              AgentInboxItem.origin == "system_recovery"))
        assert resumed.origin_ref["entrypoint"] == "question_answer"
    speech = "已确认归属，接下来整理发布计划。"
    await answer(owner, main, [(speech, None)])
    link = AssistantLink(call_id=call_id, user_id=owner, workspace_id=workspace, main_session_id=main.id,
                         lang="zh", turn_timeout=5)
    provider = ScriptedProvider()
    scope = CallScope(user_id=owner, workspace_id=workspace, main_session_id=main.id, call_id=call_id)
    bridge = Bridge(provider, link, scope=scope, clock=FakeClock(), instructions="Test")
    bridge.greeting = False
    ref = VoiceTurnRef(id="follow-" + call_id, provider_call_id="actual-card-tool:card", text="确认归属",
                       transcript="第一个", requested=1000)
    result = await asyncio.wait_for(link.follow(ref, after=after), 5)
    found = await watcher.poll()
    assert ref.inbox_id == resumed.id == found[0].inbox_id
    assert result["speech"] == found[0].text
    if arrival == "both_waiting":
        bridge.state = "user_speaking"
    if arrival == "report_first":
        await bridge.report_batch(found)
    await bridge._settle(ref, result)
    if arrival != "report_first":
        await bridge.report_batch(found)
    bridge.state = "idle"
    await bridge.fill_idle()
    await finish(bridge, provider)
    assert facts(provider) == [speech]
    assert not bridge.deliveries
    assert ref.delivery == "done" and ref.heard


async def test_task_only_changes_and_ready_outputs_reach_open_call_without_main_report():
    from tests.unit.test_assistant_results import result_ready
    owner, workspace, main, accepted, lease, _ = await result_ready()
    await lease.release(session_status="idle")
    scope = CallScope(user_id=owner, workspace_id=workspace, main_session_id=main.id)
    watcher = SharedContextWatcher(scope)
    async with get_db_session() as db:
        task = await db.get(AssistantTask, accepted["task_id"])
        task.observed_state = "running"
        session_id, project_id = task.execution_session_id, task.project_id
    first = await watcher.poll()
    assert first.work[0]["state"] == "running"
    async with get_db_session() as db:
        db.add(FileAsset(id="ready-image", user_id=owner, workspace_id=workspace,
            session_id=session_id, project_id=project_id, name="成品.png", mime="image/png",
            oss_key="unused", source="agent", status="pending", created_at=datetime.now(timezone.utc)))
    assert not (await watcher.read()).work[0].get("available_outputs")
    async with get_db_session() as db:
        (await db.get(FileAsset, "ready-image")).status = "ready"
    ready = await watcher.poll()
    assert ready.work[0]["available_outputs"][0]["asset_id"] == "ready-image"
    assert ready.work[0]["state"] == "running"  # file readiness is not whole-task completion
    async with get_db_session() as db:
        (await db.get(AssistantTask, accepted["task_id"])).observed_state = "completed"
    completed = await watcher.poll()
    assert completed.work[0]["state"] == "completed"
    assert completed.work[0]["version"] != ready.work[0]["version"]
    assert await watcher.poll() is None


async def test_typed_answer_is_shared_without_a_second_unsolicited_voice_report():
    from assistant.inputs import accept_turn
    owner, _, workspace, main = await assistant()
    scope = CallScope(user_id=owner, workspace_id=workspace, main_session_id=main.id, call_id="active-call")
    context = SharedContextWatcher(scope)
    reports = ReportWatcher(user_id=owner, workspace_id=workspace, main_session_id=main.id, call_id=scope.call_id)
    await reports.start()
    await accept_turn(user_id=owner, workspace_id=workspace, main_id=main.id, client_id="typed-sync",
                      text="只在网页回复今天的测试值", model="test/model")
    pending = await context.read()
    assert '"reply"' not in pending.text
    await answer(owner, main, [("不应分享的过程说明", "commentary"), ("海风蓝，96像素。", "final")])
    completed = await context.read()
    data = json.loads(completed.text.split("\n", 1)[1])
    assert data["typed_input"][-1]["reply"] == "海风蓝，96像素。"
    assert "不应分享的过程说明" not in completed.text
    assert await reports.poll() == []


async def test_old_result_is_explicit_history_and_cannot_reannounce_after_revision_change():
    from tests.unit.test_assistant_results import result_ready
    owner, workspace, main, accepted, lease, _ = await result_ready()
    await lease.release(session_status="idle")
    async with get_db_session() as db:
        task = await db.get(AssistantTask, accepted["task_id"])
        previous = await db.get(TaskResult, task.latest_result_id)
        previous.summary = "上一版已完成。"
        task.intent_revision += 1
        task.observed_state = "running"
        result_id, revision = previous.id, task.intent_revision
    scope = CallScope(user_id=owner, workspace_id=workspace, main_session_id=main.id)
    row = (await overview(scope, {}))["items"][0]
    assert row["state"] == "running" and row["intent_revision"] == revision
    assert not row["latest_is_current"] and "latest" not in row
    assert row["previous_result"]["summary"] == "上一版已完成。"
    from types import SimpleNamespace
    report = SimpleNamespace(origin_ref={"task_id": task.id, "result_id": result_id})
    watcher = ReportWatcher(user_id=owner, main_session_id=main.id, workspace_id=workspace)
    assert await watcher._result(report) is None


async def test_committed_answer_overrides_a_lagging_waiting_input_projection():
    from tests.unit.test_assistant_results import result_ready
    from tests.unit.test_assistant_requests_v2 import ask_in
    from db.models.session import Session
    owner, workspace, main, accepted, lease, _ = await result_ready()
    await lease.release(session_status="idle")
    async with get_db_session() as db:
        task = await db.get(AssistantTask, accepted["task_id"])
        session = await db.get(Session, task.execution_session_id)
    question_id, _ = await ask_in(session, owner, question="确认模型？", options=[{"label": "确认"}])
    async with get_db_session() as db:
        (await db.get(AssistantTask, task.id)).observed_state = "waiting_input"
    scope = CallScope(user_id=owner, workspace_id=workspace, main_session_id=main.id)
    assert (await overview(scope, {}))["items"][0]["waiting_for_user"]
    shown = await questions.get_request(question_id, owner)
    await questions.reply(question_id, [["确认"]], owner, reply_id="confirm-model",
        expected_request_revision=shown.assistant["request_revision"],
        options_hash=shown.assistant["options_hash"], source_ref={"kind": "card"})
    value = (await overview(scope, {}))["items"][0]
    assert not value["waiting_for_user"]
    assert value["state"] == "resuming" and value["projection_lagging"]
    assert "projected_state" not in value
    assert "无需再次确认" in value["state_summary"]
    assert value["last_decision"]["state"] == "answered"


def test_selecting_an_option_does_not_create_a_new_spoken_question_version():
    from types import SimpleNamespace
    from assistant.requests import question_version
    row = SimpleNamespace(id="question", session_id="session", user_id="owner", generation=1,
        questions=[{"question": "选哪个模型？", "options": [{"label": "快速"}]}],
        draft=[], continuation={}, expires_at=None)
    original = question_version(row)
    row.draft = [{"selected": ["快速"], "attachments": []}]
    assert question_version(row) == original
    row.draft = [{"selected": ["快速"], "attachments": ["new-image"]}]
    assert question_version(row) != original


@pytest.mark.parametrize("read_tool", ["tasks_overview", "cards_pending"])
async def test_manual_confirmation_cancels_prepared_native_answer_from_either_reader(read_tool):
    from tests.unit.test_assistant_requests_v2 import ask_in
    owner, _, workspace, main = await assistant()
    session = await conversation(owner, workspace, main, "图片任务", visibility="private")
    question_id, _ = await ask_in(session, owner, question="是否继续生成图片？", options=[{"label": "继续"}])
    scope = CallScope(user_id=owner, workspace_id=workspace, main_session_id=main.id, call_id="card-call")
    provider = ScriptedProvider()
    bridge = Bridge(provider, FakeLink(), scope=scope, clock=FakeClock(), instructions="Test")
    bridge.greeting, bridge.state = False, "user_speaking"
    arguments = {"request_id": question_id} if read_tool == "cards_pending" else {}
    await bridge._complete_direct_call(event("tool_call", name=read_tool, call_id="read-current",
                                             arguments=json.dumps(arguments)))
    assert provider.commands("output")[-1][2]["status"] == "ok"
    await questions.reply(question_id, [["继续"]], owner)
    assert (await card(question_id)).status == "answered"
    await bridge.waiting_cards([], [])
    bridge.state = "idle"
    await bridge.fill_idle()
    await bridge.on_provider_event(started("old-answer"))
    await bridge.on_provider_event(audio("old-answer"))
    assert not any(isinstance(value, bytes) for value in bridge.outbox._queue)
    assert provider.commands("cancel")
