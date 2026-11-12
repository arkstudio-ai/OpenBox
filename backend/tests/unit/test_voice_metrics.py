"""The dispatch metrics (voice/metrics.py) over real rows, and the lines a call keeps for them."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from db.base import get_db_session
from db.models.assistant import AssistantCommand
from db.models.voice import VoiceLine
from tests.support.voice_fakes import FakeClock, FakeLink, ScriptedProvider, audio, done, event, started
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_sessions_v2 import assistant
from tests.unit.test_voice_bridge import greeted, replay
from voice import calls, metrics
from voice.bridge import Bridge
from voice.provider import translate

T0 = datetime(2026, 11, 12, 2, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("text, claimed", [
    ("好的，已经交给帮手了，正在处理。", 1),
    ("我刚才查了一下，确实已经提交了。", 1),
    ("新对话已经建好。帮手在做了。", 2),
    ("这件事还没有交给帮手，要我现在交吗？", 0),
    ("你想做一条什么样的视频？", 0),
    ("It has not been handed over yet.", 0),
    ("I've handed it over and it is being processed.", 1),
])
def test_a_claim_is_a_sentence_saying_work_was_handed_over_or_is_under_way(text, claimed):
    assert len(metrics.claims(text)) == claimed


def _turn(call_id, turn_id, *, at, inbox=None, outcome="delivered", settled=True):
    return SimpleNamespace(id=turn_id, call_id=call_id, inbox_id=inbox, requested_at=at,
                           settled_at=at + timedelta(seconds=20) if settled else None, outcome=outcome)


def _command(action, task, *, at, run):
    return SimpleNamespace(action=action, target_id=task, created_at=at, source_ref={"run_id": run})


def _line(call_id, seq, role, text, *, at, kind=""):
    return SimpleNamespace(call_id=call_id, seq=seq, role=role, text=text, at=at, kind=kind)


def test_the_six_numbers_from_one_call():
    call = SimpleNamespace(id="call", main_session_id="main")
    turns = [_turn("call", "t1", at=T0 + timedelta(minutes=5), inbox="i1"),
             _turn("call", "t2", at=T0 + timedelta(minutes=8), inbox="i2", outcome="cancelled"),
             _turn("call", "t3", at=T0 + timedelta(minutes=9), inbox="i3", outcome="late", settled=False)]
    commands = [_command("task_create", "task", at=T0 + timedelta(minutes=5, seconds=40), run="r1"),
                *(_command("task_input", "task", at=T0 + timedelta(minutes=8 + index), run="r2") for index in range(3)),
                _command("task_input", "other", at=T0 + timedelta(minutes=9), run="r3")]
    lines = [_line("call", 1, "user", "帮我做一条新品视频", at=T0 + timedelta(minutes=1)),
             _line("call", 2, "assistant", "好的，已经交给帮手了。", at=T0 + timedelta(minutes=1, seconds=5), kind="model"),
             _line("call", 3, "assistant", "已经交给帮手了，在做。", at=T0 + timedelta(minutes=6), kind="model"),
             _line("call", 4, "assistant", "新品视频做完了，十五秒三个镜头。", at=T0 + timedelta(minutes=7), kind="delivery"),
             _line("call", 5, "event", "provider_vad", at=T0 + timedelta(minutes=7, seconds=2), kind="interrupt:delivery"),
             _line("call", 6, "event", "provider_vad", at=T0 + timedelta(minutes=7, seconds=30), kind="interrupt:model")]
    report = metrics._report(T0, T0 + timedelta(days=1), [call], turns, {"i1": "r1", "i2": "r2", "i3": "r3"},
                             commands, lines)

    # Line 2 promised a handover four minutes before any command existed; line 3 said it after one did.
    assert report["unbacked_claims"] == {"measured": True, "heuristic": True, "count": 1, "calls": 1,
                                         "where": [{"call_id": "call", "seq": 2, "sentences": 1}]}
    assert report["dispatch_seconds"] == {"count": 1, "p50": 40.0, "p90": 40.0}
    assert report["results_untold"] == {"settled": 2, "untold": 1, "share": 0.5}  # the late turn never settled
    assert report["repeated_delegation"] == {"tasks": 1, "task_ids": ["task"]}
    assert report["telling_chars"] == {"measured": True, "count": 1, "p50": 16.0, "p90": 16.0}
    # Cutting off an answer the user asked for is conversation, not a telling that ran too long.
    assert report["tellings_interrupted"] == {"measured": True, "proactive": 1, "interrupted": 1, "share": 1.0}
    assert "空口交办（疑似，需复核）：1 句 / 1 通" in metrics.render(report)


def test_a_window_without_kept_lines_says_so_instead_of_reporting_zero():
    call = SimpleNamespace(id="call", main_session_id="main")
    report = metrics._report(T0, T0 + timedelta(days=1), [call], [_turn("call", "t1", at=T0)], {}, [], [])
    assert not report["unbacked_claims"]["measured"] and not report["telling_chars"]["measured"]
    assert report["tellings_interrupted"]["share"] is None
    assert metrics.render(report).count("未采集") == 3


async def test_measure_reads_the_window_from_the_database():
    owner, _, workspace, main = await assistant()
    call_id = await calls.create_call(user_id=owner, workspace_id=workspace, main_session_id=main.id,
                                      client="web", model="test/audio", voice="test-voice")
    await calls.add_turn(turn_id="turn-" + call_id, call_id=call_id, user_id=owner, provider_call_id="ask-1",
                         transcript="帮我做一条新品视频")
    await calls.update_turn("turn-" + call_id, settled=True, outcome="cancelled")
    await calls.add_line(call_id=call_id, user_id=owner, seq=1, role="assistant", kind="model",
                         text="已经交给帮手了。", item_id="item-1")
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        for index in range(3):
            db.add(AssistantCommand(id=f"cmd-{index}-{call_id}", actor_user_id=owner, workspace_id=workspace,
                                    assistant_session_id=main.id, idempotency_key=f"key-{index}", action="task_input",
                                    target_type="task", target_id="task-1", payload_digest="0" * 64, source_ref={},
                                    state="accepted", receipt={}, created_at=now, updated_at=now))
    report = await metrics.measure(now - timedelta(hours=1), now + timedelta(hours=1))
    assert (report["calls"], report["turns"], report["calls_with_lines"]) == (1, 1, 1)
    assert report["unbacked_claims"]["count"] == 1 and report["results_untold"]["share"] == 1.0
    assert report["repeated_delegation"]["task_ids"] == ["task-1"]
    assert (await metrics.measure(now - timedelta(hours=1), now + timedelta(hours=1), user_id="someone-else"))["calls"] == 0


async def test_a_call_keeps_its_lines_and_what_the_user_cut_off_only_when_asked_to():
    kept = []

    async def sink(**line):
        kept.append(line)

    provider = ScriptedProvider()
    bridge = Bridge(provider, FakeLink(), lang="zh", late_after=12, clock=FakeClock(), line_sink=sink)
    await greeted(bridge, provider)
    await replay(bridge, event("user_started"), event("user_stopped"),
                 event("user_transcript", text="帮我做一条新品视频", item_id="user-1"),
                 started("answer"), audio("answer", size=48000),
                 event("assistant_transcript", text="好的，已经交给帮手了。", item_id="reply-1", response_id="answer"))
    partial = translate({"type": "conversation.item.input_audio_transcription.delta",
                         "item_id": "user-2", "text": "等一下", "stash": ""})
    await replay(bridge, partial)
    assert [(line["seq"], line["role"], line["kind"]) for line in kept] == [
        (1, "user", ""), (2, "assistant", "model"), (3, "event", "interrupt:model")]
    assert kept[1]["text"] == "好的，已经交给帮手了。" and kept[1]["item_id"] == "reply-1"

    silent = Bridge(ScriptedProvider(), FakeLink(), lang="zh", late_after=12, clock=FakeClock())
    assert silent.line_sink is None  # the default writes nothing


async def test_a_line_that_cannot_be_kept_never_disturbs_the_call():
    async def broken(**line):
        raise RuntimeError("database away")

    provider = ScriptedProvider()
    bridge = Bridge(provider, FakeLink(), lang="zh", late_after=12, clock=FakeClock(), line_sink=broken)
    await greeted(bridge, provider)
    await replay(bridge, event("user_started"), event("user_stopped"),
                 event("user_transcript", text="在吗", item_id="user-1"))
    assert bridge.heard_count == 1 and not bridge.failed.is_set()
