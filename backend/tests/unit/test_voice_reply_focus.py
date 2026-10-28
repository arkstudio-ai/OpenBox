"""A manual card answer stops the reply asking that decision, however the reply words it.

Reported 2026-10-27: a long explanation of the seagull card stopped when the
card was answered on screen, but "Would you like the gulls at the end or in
the middle?" kept playing. The reply is placed on a decision by meaning
(voice/reply_focus.py); here a fake embedding stands in for the model's sense
of meaning, so what is tested is the call's use of it: which speech an answer
stops, which it leaves alone, and that nothing changes when focus is missing.
"""
import asyncio
import json

import pytest

from tests.support.voice_fakes import (FakeClock, FakeLink, ScriptedProvider, audio, done, drain, event, outbox,
                                       started, transcript)
from voice.bridge import Bridge
from voice.progress import Progress
from voice.reply_focus import MEANING_FLOOR, ReplyFocus, describe, named
from voice.tools import CallScope

SCOPE = CallScope(user_id="u1", workspace_id="w1", main_session_id="main-1", call_id="call-x")
SUNSET = {"id": "q-sunset", "version": "v1", "session_id": "sunset", "session_title": "30秒日落海浪横屏短视频方案",
          "project_name": "日落海浪短视频", "assistant_may_answer": True,
          "questions": [{"header": "海鸥镜头位置", "question": "海鸥飞过放在哪个镜头？",
                         "options": ["放在镜头3（收尾镜头：晚霞倒影与海鸥飞过）", "放在镜头2（中间镜头：金光浪花与海鸥低空掠过）"],
                         "custom": False}]}
ORANGE = {"id": "q-orange", "version": "v1", "session_id": "orange", "session_title": "橙光鲜果周末上新宣传视频脚本",
          "project_name": "橙光鲜果新品宣传", "assistant_may_answer": True,
          "questions": [{"header": "精炼版文案", "question": "文案用哪个版本？",
                         "options": ["确认精炼版文案", "保留原文案"], "custom": False}]}
GULLS = "Would you like the gulls at the end or in the middle?"
COPY = "For the orange one, the trimmed copy or the original wording?"
WEATHER = "It's sunny today, around twenty-five degrees."
# Concepts a real embedding would place close together across languages.
CONCEPTS = (("gull", "海鸥"), ("copy", "wording", "文案"))


def meaning(text: str) -> list[float]:
    lowered = text.casefold()
    return [float(any(word in lowered for word in words)) for words in CONCEPTS] + [0.3]


class Embedding:
    """A stand-in embedding; ``gate`` holds back a reply's comparison, ``fail`` makes it unavailable."""

    def __init__(self, *, gate=None, fail=False):
        self.gate, self.fail, self.calls = gate, fail, []

    async def __call__(self, texts):
        self.calls.append(list(texts))
        if self.gate is not None and not any("等待决定" in text or "任务「" in text for text in texts):
            await self.gate.wait()  # descriptions were embedded beforehand, as in a call
        if self.fail:
            raise TimeoutError("embedding unavailable")
        return [meaning(text) for text in texts]


def reader(*, asks_about=None, fail=False):
    """A stand-in for the small model: it marks each decision whose description shares the reply's concept."""
    calls = []

    async def read(system, body):
        calls.append(json.loads(body))
        if fail:
            raise RuntimeError("model unavailable")
        payload = json.loads(body)
        reply = meaning(payload["assistant_reply"])
        if asks_about is not None:
            chosen = [d["label"] for d in payload["pending_decisions"] if asks_about in d["about"]]
        else:
            chosen = [d["label"] for d in payload["pending_decisions"]
                      if any(a and b for a, b in zip(reply[:-1], meaning(d["about"])[:-1]))]
        return json.dumps({"asks": chosen})
    read.calls = calls
    return read


@pytest.fixture
def call():
    clock, provider, link = FakeClock(), ScriptedProvider(), FakeLink()
    bridge = Bridge(provider, link, lang="en", late_after=12, clock=clock, scope=SCOPE,
                    progress=Progress(user_id="u1", main_session_id="main-1", clock=clock), instructions="BASE")
    return bridge, provider, link, clock


async def replay(bridge, *events):
    for value in events:
        await bridge.on_provider_event(value)
    await drain()


async def greeted(bridge, provider):
    await bridge.start()
    await replay(bridge, started("greet"), audio("greet"), done("greet"))
    provider.sent.clear()


async def settled(bridge):
    for _ in range(50):
        task = bridge._focus_task
        if task is None or task.done():
            break
        await asyncio.sleep(0.01)
    await drain()


async def explaining(bridge, provider, text, *, final=False, response="explain"):
    """The user asks what is needed; the reply starts playing and has said ``text`` so far."""
    await greeted(bridge, provider)
    bridge.seed_waiting_cards([], [SUNSET, ORANGE])
    await replay(bridge, event("user_started"), event("user_stopped"),
                 transcript("What do you need me to decide?", "u-ask"))
    outbox(bridge)  # the user's own barge-in clears playback too
    await replay(bridge, started(response), audio(response, size=480000),
                 event("assistant_transcript" if final else "assistant_transcribing", response_id=response, text=text))
    await settled(bridge)
    outbox(bridge)


def cleared(bridge) -> bool:
    return any(isinstance(value, dict) and value.get("type") == "playback.clear" for value in outbox(bridge))


@pytest.mark.parametrize("changed", [SUNSET, ORANGE])
@pytest.mark.parametrize("buffered", [False, True])
async def test_text_assistant_delivery_stops_only_for_its_spoken_card(call, changed, buffered):
    from voice.assistant_link import VoiceTurnRef
    bridge, provider, _, _ = call
    bridge.playback_ack = True
    bridge.focus = ReplyFocus(embed=Embedding())
    await greeted(bridge, provider)
    await bridge.waiting_cards([], [SUNSET, ORANGE])
    bridge.deliveries.clear()
    ref = VoiceTurnRef(id="main-reply", provider_call_id="answer", text="", transcript="", requested=0.0)
    ref.status = "ok"
    bridge.deliveries.append(ref)
    bridge.requested = "delivery:main-reply"
    # A delayed result can concern different work from the latest spoken question.
    bridge._work = {"orange": {"session_id": "orange", "title": ORANGE["session_title"], "state": "waiting"},
                    "sunset": {"session_id": "sunset", "title": SUNSET["session_title"] + " 海鸥", "state": "waiting"}}
    bridge._reply_words = lambda response_id: ORANGE["session_title"]
    await replay(bridge, started("main-speech"), audio("main-speech", size=480000),
                 event("assistant_transcribing", response_id="main-speech", text=GULLS))
    assert bridge._native_named is None
    await settled(bridge)
    if buffered:
        await replay(bridge, done("main-speech"))
    outbox(bridge)
    await bridge.waiting_cards([], [q for q in [SUNSET, ORANGE] if q is not changed])
    assert cleared(bridge) is (changed is SUNSET)
    assert (ref.delivery == "superseded") is (changed is SUNSET)
    await bridge.close()


async def test_only_pending_card_cannot_capture_a_reply_about_different_work(call):
    """Fruit-store narration must not be stopped by an unrelated copy-choice card.

    A single candidate can be moderately similar to any business update;
    task ownership must compete with that provisional embedding match.
    """
    bridge, provider, _, _ = call
    bridge.playback_ack = True
    bridge.focus = ReplyFocus(embed=Embedding(), read=reader(asks_about="not-mentioned"))
    await greeted(bridge, provider)
    await bridge.waiting_cards([], [ORANGE])
    bridge.deliveries.clear()
    bridge._work = {"sunset": {"title": SUNSET["session_title"], "state": "completed"},
                    "orange": {"title": ORANGE["session_title"], "state": "waiting"}}
    async def misleading_similarity(*args, **kwargs):
        return [{"q-orange": "v1"}, {"sunset": "current"}]
    bridge.focus.place = misleading_similarity
    await replay(bridge, started("different-work"), audio("different-work", size=480000),
                 event("assistant_transcribing", response_id="different-work",
                       text="30秒日落海浪横屏短视频方案 is complete; here is the updated ending."))
    await settled(bridge)
    outbox(bridge)
    await bridge.waiting_cards([], [])
    assert not cleared(bridge)
    assert "q-orange" not in bridge._native_meaning
    assert "orange" not in bridge._native_work_playing
    await bridge.close()


def test_a_decision_is_described_by_its_task_question_and_options():
    text = describe(SUNSET)
    assert "30秒日落海浪横屏短视频方案" in text and "海鸥飞过放在哪个镜头" in text and "收尾镜头" in text
    card = {"card_id": "q-del", "kind": "assistant_confirm", "header": "删除项目",
            "prompt": "删除项目「测试A」。\n影响：其中 2 个会话一起删除。", "options": ["确认", "取消"]}
    assert describe(card) == "等待决定：删除项目：删除项目「测试A」。。选项：确认；取消"


def test_the_words_name_a_task_only_when_one_title_shares_clearly_more():
    """Measured 2026-10-27: meaning put 日出海浪 above 日落海浪 for "日落海浪那个视频现在进展到哪了？"."""
    tasks = {"sunset": "30秒日落海浪横屏短视频方案 日落海浪短视频", "sunrise": "日出海浪竖屏短视频 日出海浪短视频",
             "orange": "橙光鲜果周末上新宣传视频脚本 橙光鲜果新品宣传", "coffee": "Small Coffee Shop Business Plan"}
    assert named("日落海浪那个视频现在进展到哪了？", tasks) == "sunset"
    assert named("日出那个视频呢？", tasks) == "sunrise"
    assert named("那个视频怎么样了？", tasks) is None  # a word every video shares decides nothing
    assert named("How is the coffee shop plan going?", tasks) == "coffee"
    assert named("How is the sunset video going?", tasks) is None  # a translated name is left to meaning
    assert named("", tasks) is None


async def test_tasks_too_close_to_tell_apart_are_each_kept_but_decisions_are_not():
    async def same(texts):  # a reply equally close to both
        return [[1.0, 0.0] if "海浪" in text or "waves" in text else [0.0, 1.0] for text in texts]
    focus = ReplyFocus(embed=same)
    tasks = {"sunset": ("", "任务「日落海浪」"), "sunrise": ("", "任务「日出海浪」"), "orange": ("", "任务「橙光」")}
    decisions = {"q-sunset": ("v1", "任务「日落海浪」等待决定"), "q-sunrise": ("v1", "任务「日出海浪」等待决定")}
    await focus.prepare(tasks, decisions)
    meaning, about = await focus.place("The waves video is regenerating.", decisions, tasks, close=(1,))
    assert meaning == {} and about == {"sunset": "", "sunrise": ""}


async def test_meaning_places_a_reply_only_on_a_clearly_closest_decision():
    focus = ReplyFocus(embed=Embedding())
    decisions = {"q-sunset": ("v1", describe(SUNSET)), "q-orange": ("v1", describe(ORANGE))}
    assert await focus.by_meaning(GULLS, decisions) == {}  # descriptions are embedded beforehand, not on its path
    await focus.prepare(decisions)
    assert await focus.by_meaning(GULLS, decisions) == {"q-sunset": "v1"}
    assert await focus.by_meaning(COPY, decisions) == {"q-orange": "v1"}
    assert await focus.by_meaning(WEATHER, decisions) == {}  # small talk is near nothing
    both = "The gulls and the copy are both waiting for you."
    assert await focus.by_meaning(both, decisions) == {}  # equally near two: neither one alone
    assert MEANING_FLOOR > 0.3


async def test_descriptions_are_embedded_once_and_failures_place_nothing():
    embedding = Embedding()
    focus = ReplyFocus(embed=embedding)
    decisions = {"q-sunset": ("v1", describe(SUNSET))}
    await focus.prepare(decisions)
    await focus.by_meaning(GULLS, decisions)
    assert embedding.calls == [[describe(SUNSET)], [GULLS]]  # a reply is embedded alone
    failing = ReplyFocus(embed=Embedding(fail=True))
    await failing.prepare(decisions)
    assert await failing.by_meaning(GULLS, decisions) == {}
    assert await ReplyFocus(read=reader(fail=True)).by_reading(GULLS, "", [("q-sunset", "v1", "x")]) is None


@pytest.mark.parametrize("answer", ['{"asks": ["2"]}', '```json\n{"asks": [2, 7]}\n```', '{"asks": "2"}', "not json"])
async def test_the_reading_maps_labels_back_to_decisions(answer):
    async def read(system, body):
        return answer
    reading = await ReplyFocus(read=read).by_reading(GULLS, "what now?", [("a", "1", "x"), ("b", "2", "y")])
    assert reading == ({"b": "2"} if answer.startswith(("{\"asks\": [", "```")) else None)


async def test_a_short_paraphrase_stops_when_its_card_is_answered_on_screen(call):
    bridge, provider, _, _ = call
    bridge.focus = ReplyFocus(embed=Embedding(), read=reader())
    await explaining(bridge, provider, GULLS)
    assert bridge._native_meaning == {"q-sunset": "v1"}  # placed before the user acts
    await bridge.waiting_cards([], [ORANGE])  # the seagull card answered in the personal assistant
    assert cleared(bridge) and provider.commands("cancel")
    await bridge.close()


async def test_answering_another_card_leaves_the_reply_playing(call):
    bridge, provider, _, _ = call
    bridge.focus = ReplyFocus(embed=Embedding(), read=reader())
    await explaining(bridge, provider, GULLS)
    await bridge.waiting_cards([], [SUNSET])  # the orange card answered instead
    assert not cleared(bridge) and not provider.commands("cancel")
    await bridge.close()


async def test_a_reply_about_the_other_card_belongs_to_it(call):
    bridge, provider, _, _ = call
    bridge.focus = ReplyFocus(embed=Embedding(), read=reader())
    await explaining(bridge, provider, COPY)
    assert bridge._native_meaning == {"q-orange": "v1"}
    await bridge.waiting_cards([], [ORANGE])  # the seagull card answered: not this reply's
    assert not cleared(bridge)
    await bridge.waiting_cards([], [])  # now its own card
    assert cleared(bridge)
    await bridge.close()


async def test_small_talk_is_never_stopped_by_a_card(call):
    bridge, provider, _, _ = call
    bridge.focus = ReplyFocus(embed=Embedding(), read=reader())
    await explaining(bridge, provider, WEATHER)
    await bridge.waiting_cards([], [])
    assert not cleared(bridge)
    await bridge.close()


async def test_a_comparison_still_running_when_the_card_is_answered_stops_the_reply_when_it_lands(call):
    bridge, provider, _, _ = call
    gate = asyncio.Event()
    bridge.focus = ReplyFocus(embed=Embedding(gate=gate), read=reader())
    await explaining(bridge, provider, GULLS)
    await bridge.waiting_cards([], [ORANGE])
    assert not cleared(bridge)  # nothing placed it yet
    gate.set()
    await settled(bridge)
    assert cleared(bridge)
    await bridge.close()


async def test_the_small_models_reading_replaces_the_embeddings_guess(call):
    bridge, provider, _, _ = call
    read = reader(asks_about="never-matches")
    bridge.focus = ReplyFocus(embed=Embedding(), read=read)
    await explaining(bridge, provider, GULLS, final=True)
    assert read.calls and bridge._native_meaning == {}  # read as asking nothing
    await bridge.waiting_cards([], [ORANGE])
    assert not cleared(bridge)
    await bridge.close()


async def test_a_reply_begun_before_the_answer_reached_it_is_stopped_once_read(call):
    """The card was answered moments before the reply started: only the model's reading can tell it asks it."""
    bridge, provider, _, clock = call
    read = reader()
    bridge.focus = ReplyFocus(embed=Embedding(), read=read)
    await greeted(bridge, provider)
    bridge.seed_waiting_cards([], [SUNSET, ORANGE])
    await bridge.waiting_cards([], [ORANGE])  # answered on screen
    clock.advance(2)
    await replay(bridge, event("user_started"), event("user_stopped"), transcript("What do you need?", "u-ask"))
    outbox(bridge)  # the user's own barge-in clears playback too
    await replay(bridge, started("late"), audio("late", size=480000),
                 event("assistant_transcript", response_id="late", text=GULLS))
    await settled(bridge)
    assert [d["about"] for d in read.calls[0]["pending_decisions"]] == [describe(ORANGE), describe(SUNSET)]
    assert cleared(bridge)
    await bridge.close()


async def test_an_answer_long_before_the_reply_is_not_something_it_can_still_be_asking(call):
    bridge, provider, _, clock = call
    read = reader()
    bridge.focus = ReplyFocus(embed=Embedding(), read=read)
    await greeted(bridge, provider)
    bridge.seed_waiting_cards([], [SUNSET, ORANGE])
    await bridge.waiting_cards([], [ORANGE])
    clock.advance(60)  # "what did I choose for the gulls?" a minute later is about the answer, not asking it
    await replay(bridge, event("user_started"), event("user_stopped"), transcript("What did I pick?", "u-ask"))
    outbox(bridge)
    await replay(bridge, started("later"), audio("later", size=480000),
                 event("assistant_transcript", response_id="later", text="You put the gulls at the end."))
    await settled(bridge)
    assert [d["about"] for d in read.calls[0]["pending_decisions"]] == [describe(ORANGE)]
    assert not cleared(bridge)
    await bridge.close()


async def test_an_answer_given_while_the_question_was_prepared_is_still_checked(call):
    """Measured 2026-10-27: transcription and preparation took 20 s; the reply then asked the card answered meanwhile."""
    bridge, provider, _, clock = call
    read = reader()
    bridge.focus = ReplyFocus(embed=Embedding(), read=read)
    await greeted(bridge, provider)
    bridge.seed_waiting_cards([], [SUNSET, ORANGE])
    await replay(bridge, event("user_started"), event("user_stopped"))
    clock.advance(2)
    await bridge.waiting_cards([], [ORANGE])  # answered on screen while the question is transcribed and prepared
    clock.advance(20)
    await replay(bridge, transcript("What do I need to choose for the gulls?", "u-ask"))
    outbox(bridge)
    await replay(bridge, started("late"), audio("late", size=480000),
                 event("assistant_transcript", response_id="late", text=GULLS))
    await settled(bridge)
    assert describe(SUNSET) in [d["about"] for d in read.calls[0]["pending_decisions"]]
    assert cleared(bridge)
    await bridge.close()


@pytest.mark.parametrize("restricted", [False, True])
async def test_a_card_that_stops_waiting_is_noted_as_gone_in_the_conversation(call, restricted):
    """Measured 2026-10-27: a card answered on screen was asked again from the call's own earlier announcement."""
    bridge, provider, _, _ = call
    await greeted(bridge, provider)
    bridge.seed_waiting_cards([], [SUNSET, ORANGE])
    if restricted:  # a turn withholding the cards it showed says nothing about them either
        bridge._context_withheld = lambda source_item="": True
    provider.sent.clear()
    await bridge.waiting_cards([], [ORANGE])
    notes = provider.commands("note")
    if restricted:
        assert notes == []
    else:
        [(_, note)] = notes
        assert json.loads(note.split(") ", 1)[1])["no_longer_waiting"] == [describe(SUNSET)]
    await bridge.close()


async def test_focus_being_unavailable_changes_nothing(call):
    bridge, provider, _, _ = call
    bridge.focus = ReplyFocus(embed=Embedding(fail=True), read=reader(fail=True))
    await explaining(bridge, provider, GULLS, final=True)
    await bridge.waiting_cards([], [ORANGE])
    assert not cleared(bridge)  # no meaning, no exact name: as before
    await bridge.close()


async def test_exact_names_still_stop_a_reply_without_any_focus(call):
    bridge, provider, _, _ = call
    assert bridge.focus is None
    await explaining(bridge, provider, "The task 30秒日落海浪横屏短视频方案 is waiting for you.")
    await bridge.waiting_cards([], [ORANGE])
    assert cleared(bridge)
    await bridge.close()


async def test_the_reply_asked_after_a_stopped_one_survives_the_cancel_the_provider_finds_nothing_to_do_for(call):
    """Measured 2026-10-27: "Conversation has no active response" for our cancel dropped the fresh answer."""
    bridge, provider, _, _ = call
    bridge.focus = ReplyFocus(embed=Embedding(), read=reader())
    await explaining(bridge, provider, GULLS)
    await bridge.waiting_cards([], [ORANGE])
    assert cleared(bridge) and bridge.followup_due
    await replay(bridge, done("explain", status="cancelled"))
    await bridge.fill_idle()
    await drain()
    assert bridge.requested == "followup"
    from voice.provider import translate
    refusal = translate({"type": "error", "error": {"code": "invalid_value", "param": "response.cancel",
                                                    "message": "Conversation has no active response."}})
    await replay(bridge, refusal)
    assert bridge.requested == "followup"  # still waiting for its own reply
    await bridge.close()


async def test_a_reply_built_on_a_card_read_before_the_answer_is_answered_again_twice_at_most(call):
    """Measured 2026-10-27: the answer to the stopped question came from the card read before the click."""
    bridge, provider, _, _ = call
    await greeted(bridge, provider)
    bridge.seed_waiting_cards([], [SUNSET, ORANGE])
    await replay(bridge, event("user_started"), event("user_stopped"), transcript("What do you need?", "u-ask"))
    for attempt in range(3):
        bridge._native_pending = {"q-sunset": "v1"}  # read with cards_pending, then answered on screen
        bridge._native_read_epoch = bridge._cards_epoch
        await bridge.waiting_cards([], [ORANGE] if attempt == 0 else [])
        bridge.followup_due = False
        outbox(bridge)
        await replay(bridge, started(f"stale-{attempt}"))
        assert cleared(bridge)
        assert bridge.followup_due is (attempt < 2)  # answered again, but not for ever
        await replay(bridge, done(f"stale-{attempt}", status="cancelled"))
    await bridge.close()


@pytest.mark.parametrize("answered", [True, False])
async def test_the_answer_given_again_names_the_decision_that_no_longer_waits(call, answered):
    """Measured 2026-10-27: told only that something changed, the next answer asked the answered card again;
    told that a card new requirements had withdrawn was answered, it said the user had confirmed the plan."""
    bridge, provider, _, _ = call
    bridge.focus = ReplyFocus(embed=Embedding(), read=reader())
    await explaining(bridge, provider, GULLS)
    await bridge.waiting_cards([], [ORANGE])
    assert cleared(bridge)
    if answered:  # the task's row records the answer (voice/work.py last_decision)
        bridge._work = {"sunset": {"session_id": "sunset", "title": SUNSET["session_title"], "state": "resuming",
                                   "last_decision": {"request_id": "q-sunset", "state": "answered"}}}
    provider.sent.clear()
    await replay(bridge, done("explain", status="cancelled"))  # the call is free: it answers again at once
    await bridge.fill_idle()
    await drain()
    [(_, instructions)] = provider.commands("create")
    [decided] = json.loads(instructions.split("These decisions no longer wait for the user: ")[1].split(". Do not ask")[0])
    assert decided["decision"] == describe(SUNSET)
    assert (decided["outcome"] == "answered") is answered
    assert "even if the user's last words asked you to ask" in instructions
    await bridge.close()
