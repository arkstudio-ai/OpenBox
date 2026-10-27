"""Every model-facing decision about an utterance keeps that utterance's read restriction.

The spoken reply already answers from the current words only. The handoff
check after it, the handover of the words to the assistant, a retried or
carried turn and the assistant's own turn must see the same scope: the real
check runs here with only its model call replaced, so the assertions are on
exactly what the model would have been sent.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.support.voice_fakes import audio, done, drain, item, started
from tests.unit.test_voice_handoff_guard import SEND_CARD
from tests.unit.test_voice_memory_context import _heard, _question, _restricted_by_primary, call, packet
from tests.unit.test_voice_routing import said
from voice.handoff import HandoffGuard, required

EARLIER = "帮我做个城市夜景视频，选快速档"
SUMMARY = "通话摘要：用户之前要过城市夜景视频，选了快速档，还在等确认修改的卡片。"
NOW = "别用之前的记录，帮我重新做一个火车的视频"
DIRECT = '{"decision":"direct","operation":"none","authorization":""}'
HANDOFF = '{"decision":"handoff","operation":"artifact","authorization":"帮我重新做一个火车的视频"}'
LIVE_TASKS = {"source": "business_sql", "sessions": [{"id": "s-1", "title": "春禾果店15秒竖屏宣传视频",
                                                      "status": "busy", "todos": [{"content": "生成分镜"}]}]}


def recorded(monkeypatch, answers=(DIRECT,)):
    """The summary model replaced by a recorder of exactly what the check sends it."""
    bodies, replies = [], iter(answers)
    async def complete(model, system, text, timeout):
        bodies.append(json.loads(text))
        return next(replies, DIRECT)
    async def warm(model):
        return None
    monkeypatch.setattr("voice.summary.complete", complete)
    monkeypatch.setattr("voice.summary.warm", warm)
    return bodies


async def _checked(bodies, count):
    for _ in range(100):
        if len(bodies) >= count:
            break
        await asyncio.sleep(0.01)
    await drain()


async def _answered(bridge, text, item_id, response_id):
    """The user speaks, the turn is prepared, and the spoken reply completes: the check runs after it."""
    await _heard(bridge, text, item_id)
    await bridge.fill_idle()
    await drain()
    from tests.unit.test_voice_bridge import replay
    await replay(bridge, started(response_id), said("好的", response_id), audio(response_id), done(response_id))


async def _call_with_history(monkeypatch, second, answers=(DIRECT, DIRECT)):
    """An earlier allowed request, a call summary and two cards shown; then ``second`` decides the next turn."""
    bodies = recorded(monkeypatch, answers)
    packets = iter([packet(EARLIER), None])
    async def prepare(scope, text, recent):
        return (next(packets) or second(text)), "fresh"
    bridge, provider, clock = call(prepare)
    bridge.handoff = HandoffGuard(bridge, required)
    await _answered(bridge, EARLIER, "first", "a1")
    await _checked(bodies, 1)
    bridge.keeper.summary = SUMMARY
    bridge.desk.show(SEND_CARD)
    bridge.desk.show_question(_question(), "zh")
    return bridge, provider, bodies


def _leaks(body):
    text = json.dumps(body, ensure_ascii=False)
    return [marker for marker in (SUMMARY, EARLIER, "确认发送", "确认修改任务", "q-send", "视频档位", "快速")
            if marker in text]


@pytest.mark.parametrize("policy", ["no_history", "current_only", "documents_only"])
async def test_the_handoff_check_gets_only_the_current_words_under_an_explicit_restriction(monkeypatch, policy):
    bridge, provider, bodies = await _call_with_history(monkeypatch, lambda text: packet(text, policy))
    await _answered(bridge, NOW, "now", "a2")
    await _checked(bodies, 2)
    allowed, restricted = bodies
    # The earlier allowed turn's check had the call's context, as before.
    assert allowed["latest_user"] == EARLIER and allowed["context"]
    assert restricted == {"context": "", "latest_user": NOW, "context_withheld": True}
    assert _leaks(restricted) == []
    await bridge.close()


async def test_no_history_still_lets_the_check_see_live_task_state_and_current_only_does_not(monkeypatch):
    def no_history(text):
        return {**packet(text, "no_history"), "task_state": LIVE_TASKS}
    bridge, provider, bodies = await _call_with_history(monkeypatch, no_history)
    await _answered(bridge, NOW, "now", "a2")
    await _checked(bodies, 2)
    assert bodies[1]["current_task_state"] == [{"title": "春禾果店15秒竖屏宣传视频", "status": "busy"}]
    assert _leaks(bodies[1]) == []
    await bridge.close()

    def current_only(text):
        return {**packet(text, "current_only"), "task_state": LIVE_TASKS}
    bridge, provider, bodies = await _call_with_history(monkeypatch, current_only)
    await _answered(bridge, NOW, "now", "a2")
    await _checked(bodies, 2)
    assert "current_task_state" not in bodies[1] and _leaks(bodies[1]) == []
    await bridge.close()


@pytest.mark.parametrize("reason", ["timeout", "network_error", "invalid_response"])
async def test_a_restriction_the_primary_judge_saw_survives_a_failed_fallback_in_the_check(monkeypatch, reason):
    bridge, provider, bodies = await _call_with_history(monkeypatch, lambda text: _restricted_by_primary(text, reason))
    await _answered(bridge, NOW, "now", "a2")
    await _checked(bodies, 2)
    assert bodies[1] == {"context": "", "latest_user": NOW, "context_withheld": True}
    await bridge.close()


async def test_an_allowed_follow_up_keeps_the_calls_context_cards_and_request_words(monkeypatch):
    bridge, provider, bodies = await _call_with_history(monkeypatch, lambda text: packet(text))
    await _answered(bridge, "竖屏的就行", "now", "a2")
    await _checked(bodies, 2)
    follow_up = bodies[1]
    assert SUMMARY in follow_up["context"] and EARLIER in follow_up["context"]
    assert follow_up["same_request_user_words"] == [EARLIER]
    assert [card["title"] for card in follow_up["open_cards"]] == ["确认修改任务"]
    assert "context_withheld" not in follow_up
    await bridge.close()


async def test_a_recovered_handover_under_a_restriction_carries_it_and_no_call_history(monkeypatch):
    bridge, provider, bodies = await _call_with_history(monkeypatch, lambda text: packet(text, "no_history"),
                                                        answers=(DIRECT, HANDOFF))
    await _answered(bridge, NOW, "now", "a2")
    await _checked(bodies, 2)
    for _ in range(50):
        if bridge.link.started:
            break
        await asyncio.sleep(0.01)
    [handed] = bridge.link.started
    assert handed.transcript == NOW and handed.context["handover_source"] == "verified_missing_handoff"
    assert handed.context["read_scope"] == {"read_policy": "no_history"}
    assert not {"call", "summary", "user_words", "earlier_user_words"} & set(handed.context)
    assert _leaks(handed.context) == []
    await bridge.close()


async def test_a_pinned_restriction_holds_for_a_carried_turn_whatever_the_next_decision_says(monkeypatch):
    """After a reconnect the words are prepared again; an allowed decision cannot reopen the call's history."""
    bodies = recorded(monkeypatch)
    recents = []
    async def prepare(scope, text, recent):
        recents.append(list(recent))
        return packet(text), "fresh"   # this time the judge says "allowed"
    bridge, provider, clock = call(prepare)
    bridge.handoff = HandoffGuard(bridge, required)
    bridge.keeper.summary = SUMMARY
    bridge.desk.show(SEND_CARD)
    bridge.spoken.add("user", EARLIER, "first")
    bridge.spoken.add("assistant", "好的，快速档", "first-reply")
    bridge.spoken.add("user", NOW, "lost-audio")
    bridge._user_utterances.update({"first": 1, "lost-audio": 2})
    bridge._last_user_item = "lost-audio"
    bridge._restricted_items["lost-audio"] = {"read_policy": "no_history"}  # what the lost session decided
    await bridge._carry_utterance("lost-audio", NOW)
    [(_, note)] = provider.commands("note")
    from tests.unit.test_voice_bridge import replay
    await replay(bridge, item("carried", text=note))
    if bridge._memory_task:
        await asyncio.wait_for(bridge._memory_task, 1)
    await bridge.fill_idle()
    await drain()
    assert recents == [[]]   # the router is not shown the call's earlier lines either
    assert bridge._context_withheld("lost-audio")
    assert "q-send" not in provider.commands("instructions")[-1][1]
    await replay(bridge, started("a1"), said("好的", "a1"), audio("a1"), done("a1"))
    await _checked(bodies, 1)
    assert bodies == [{"context": "", "latest_user": NOW, "context_withheld": True}]
    context = bridge._call_context(NOW, source_item="lost-audio")
    assert context["read_scope"] == {"read_policy": "no_history"}
    assert not {"call", "summary", "user_words", "earlier_user_words"} & set(context)
    await bridge.close()


async def test_the_card_being_answered_reaches_the_assistant_only_where_task_state_is_readable(monkeypatch):
    recorded(monkeypatch)
    for policy, kept in (("no_history", True), ("current_only", False), ("allowed", True)):
        async def prepare(scope, text, recent, policy=policy):
            return packet(text, policy), "fresh"
        bridge, provider, clock = call(prepare)
        bridge.desk.show_question(_question(), "zh")
        await _heard(bridge, "把视频档位改成标准", "now")
        context = bridge._call_context("把视频档位改成标准", question_id="q-video", source_item="now")
        assert ("task_questions" in context) is kept, policy
        assert ("read_scope" in context) is (policy != "allowed"), policy
        await bridge.close()


def test_a_compacted_handover_never_drops_its_restriction():
    from voice.assistant_link import bounded_context
    context = {"heard": NOW, "read_scope": {"read_policy": "current_only"}, "spoken_input_id": "now",
               "task_questions": [{"request_id": "q", "session_id": "s", "questions": ["x" * 4000]}]}
    assert bounded_context(context, 1000)["read_scope"] == {"read_policy": "current_only"}


def _voice_message(read_scope=None):
    from voice.prompt import VOICE_ENTRYPOINT
    context = {"heard": NOW, **({"read_scope": read_scope} if read_scope else {})}
    return SimpleNamespace(parts=[{"type": "text", "text": NOW, "origin_ref": {
        "entrypoint": VOICE_ENTRYPOINT, "voice_context": context}}])


def _bundle(policy):
    from memory.packet import TurnContext
    return {"_semantic_packet": True, "_turn": TurnContext("user", "workspace", NOW, read_policy=policy),
            "route": {"read_policy": policy}, "status": "complete", "settings": {"address": "林姐"},
            "context_version": 3, "items": [{"kind": "memory", "id": "m-1", "text": SUMMARY}],
            "stable_background": {"items": [{"kind": "memory", "id": "m-2", "text": EARLIER}]},
            "task_state": LIVE_TASKS}


@pytest.mark.parametrize("floor", ["no_history", "current_only", "documents_only", "uncertain"])
def test_the_assistant_cannot_lift_a_restriction_the_voice_turn_established(floor):
    from assistant.projection import voice_read_floor
    from memory.read_contract import permits
    floored = voice_read_floor(_bundle("allowed"), _voice_message({"read_policy": floor}))
    assert floored["route"]["read_policy"] == floor and floored["route"]["memory_forbidden"]
    assert floored["items"] == [] and floored["stable_background"] == {"items": []}
    assert "task_state" not in floored and SUMMARY not in json.dumps(floored, default=str)
    assert floored["settings"] == {"address": "林姐"} and floored["context_version"] == 3
    assert floored["_turn"].read_policy == floor and not floored["_turn"].deferred_read
    # Live task tools follow the kept policy: still readable under no_history only.
    assert permits(floored["_turn"], "task_state") is (floor == "no_history")
    assert not permits(floored["_turn"], "history")


def test_the_floor_only_narrows():
    from assistant.projection import voice_read_floor
    restricted = _bundle("current_only")
    assert voice_read_floor(restricted, _voice_message({"read_policy": "no_history"})) is restricted
    allowed = _bundle("allowed")
    assert voice_read_floor(allowed, _voice_message()) is allowed  # no restriction was carried
    typed = SimpleNamespace(parts=[{"type": "text", "text": NOW, "origin_ref": {
        "entrypoint": "web", "voice_context": {"read_scope": {"read_policy": "no_history"}}}}])
    assert voice_read_floor(allowed, typed) is allowed  # only a voice input carries a call's restriction


async def test_the_next_allowed_turn_gets_back_what_a_restricted_turn_retired(monkeypatch):
    """A restriction holds for its own utterance; it does not erase the call for the turns after it."""
    recorded(monkeypatch)
    policies = iter(["allowed", "no_history", "allowed"])
    async def prepare(scope, text, recent):
        return packet(text, next(policies)), "fresh"
    bridge, provider, clock = call(prepare)
    await _answered(bridge, "草莓最近卖得特别好", "first", "a1")
    bridge.keeper.summary = SUMMARY
    before = len(provider.commands("instructions"))
    await _answered(bridge, NOW, "restricted", "a2")
    restricted = [text for _, text in provider.commands("instructions")[before:]]
    assert restricted  # the restricted turn sent its own instructions
    assert not any(SUMMARY in text or "草莓" in text for text in restricted)
    assert ("delete", "first") in provider.sent  # the provider no longer holds the earlier lines
    await _heard(bridge, "我们刚才聊到了哪些水果？", "follow-up")
    allowed = provider.commands("instructions")[-1][1]
    assert SUMMARY in allowed and "草莓最近卖得特别好" in allowed
    assert NOW not in allowed  # the restricted turn's own words are still held by the provider, not repeated
    await bridge.close()


async def test_a_fresh_session_keeps_the_calls_recent_lines_for_an_allowed_turn(monkeypatch):
    recorded(monkeypatch)
    bridge, provider, clock = call()
    await _heard(bridge, "芒果也不错，就是进价有点贵", "first")
    bridge.spoken.add("assistant", "进价高会压缩利润", "first-reply")
    bridge.spoken.reset_items()  # a rotation or a recovered session starts without the call's items
    instructions = bridge._instructions()
    assert "芒果也不错，就是进价有点贵" in instructions and "进价高会压缩利润" in instructions
    bridge.memory_packet = packet("x", "current_only")
    assert "芒果" not in bridge._instructions()
    await bridge.close()


WORK = {"s-lemon": {"session_id": "s-lemon", "title": "青柠果铺柠檬茶10秒竖屏短视频", "state": "running",
                    "waiting_for_user": False, "state_summary": "用户已经确认，无需再次确认；后台正在恢复或更新执行状态。",
                    "last_decision": {"request_id": "q-plan", "state": "answered", "applied": True},
                    "updated_at": "2026-10-27T06:29:46+00:00", "version": "v2"}}


@pytest.mark.parametrize("policy, shown", [("allowed", True), ("no_history", True), ("current_only", False),
                                           ("documents_only", False)])
async def test_a_turn_gets_the_current_task_state_only_where_its_policy_reads_it(monkeypatch, policy, shown):
    """Measured 2026-10-27: asked for progress after a card was answered on screen, the voice repeated
    its earlier progress line; the turn had no current task state unless the model chose to read it."""
    recorded(monkeypatch)
    async def prepare(scope, text, recent):
        return packet(text, policy), "fresh"
    bridge, provider, clock = call(prepare)
    bridge._work = dict(WORK)
    await _heard(bridge, "那个柠檬茶视频现在进展到哪一步了？", "now")
    instructions = provider.commands("instructions")[-1][1]
    assert ("青柠果铺柠檬茶10秒竖屏短视频" in instructions) is shown
    assert ("用户已经确认，无需再次确认" in instructions) is shown
    await bridge.close()


async def test_the_turns_task_state_is_fixed_until_the_next_turn(monkeypatch):
    recorded(monkeypatch)
    bridge, provider, clock = call()
    bridge._work = dict(WORK)
    await _heard(bridge, "那个柠檬茶视频现在进展到哪一步了？", "now")
    first = bridge._instructions()
    bridge._work = {"s-lemon": {**WORK["s-lemon"], "state": "completed", "version": "v3"}}
    assert bridge._instructions() == first  # task events between turns do not resend the session
    await bridge.close()


def _reopening(bridge):
    """Fresh provider sessions for the call's recoveries, as the provider pump opens them."""
    from tests.unit.test_voice_memory_context import Provider
    fresh = []
    async def opener(instructions):
        fresh.append(Provider())
        fresh[-1].instructions = instructions
        return fresh[-1]
    bridge.opener = opener
    bridge.answered()
    return fresh


def _sent(session):
    """Everything a provider session was given to answer from: its opening, updates and response prompts."""
    return [session.instructions, *(text or "" for _, text in session.commands("instructions")),
            *(text or "" for _, text in session.commands("create"))]


def _history_in(texts):
    return [marker for marker in (SUMMARY, EARLIER, "确认修改任务", "修改视频总时长为10秒")
            if any(marker in text for text in texts)]


def _router_down(text):
    raise RuntimeError("router unavailable after the reconnect")


def _router_undecided(text):
    """A routing timeout that decided nothing: weaker than the restriction the words already carry."""
    return {**packet(text), "route": {"read_policy": "uncertain", "reason_code": "timeout"}}


def _router_hangs(text):
    return asyncio.Event().wait()


async def _dropped_after_restriction(monkeypatch, third):
    """An allowed turn, a call summary and a card; then "don't use my history…", whose answer the dropped
    session never spoke. ``third`` prepares those words again in the replacement session; the next is allowed.
    """
    from tests.unit.test_voice_bridge import replay
    bodies = recorded(monkeypatch)
    decisions = iter([packet, lambda text: packet(text, "no_history"), third, packet])
    async def prepare(scope, text, recent):
        decided = next(decisions)(text)
        return (await decided if asyncio.iscoroutine(decided) else decided), "fresh"
    bridge, provider, clock = call(prepare)
    bridge.handoff = HandoffGuard(bridge, required)
    fresh = _reopening(bridge)
    await _answered(bridge, EARLIER, "first", "a1")
    await _checked(bodies, 1)
    bridge.keeper.summary = SUMMARY
    bridge.desk.show(SEND_CARD)
    await _heard(bridge, NOW, "lost-audio")
    assert provider.commands("create")  # its answer was asked for, never spoken
    assert await bridge.recover(provider)
    [new] = fresh
    [(_, note)] = new.commands("note")
    assert NOW in note
    await replay(bridge, item("carried", text=note))
    for _ in range(5):
        await drain()
    return bridge, new, clock, bodies


@pytest.mark.parametrize("third", [_router_down, _router_hangs, _router_undecided, packet],
                         ids=["exception", "overdue", "undecided", "allowed"])
async def test_a_reconnected_turn_keeps_its_restriction_whatever_its_next_preparation_does(monkeypatch, third):
    """Reproduced 2026-10-27: after a reconnect a failed preparation answered from the summary, the earlier
    lines and the open card, though the words had already been restricted to no_history."""
    from tests.unit.test_voice_bridge import replay
    from voice.memory_context import keeps_call_context
    bridge, new, clock, bodies = await _dropped_after_restriction(monkeypatch, third)
    if bridge._memory_pending:  # still preparing: the watchdog answers without new evidence
        clock.advance(25)
        await bridge.fill_idle()
        for _ in range(5):
            await drain()
    await bridge.fill_idle()
    await drain()
    assert len(new.commands("create")) == 1  # the carried words are answered...
    assert _history_in(_sent(new)) == []     # ...from their own scope only
    assert not keeps_call_context(bridge.memory_packet)
    assert bridge._context_withheld("lost-audio") and bridge._withheld_policy("lost-audio") == "no_history"
    # The check after the reply is held to the same scope.
    await replay(bridge, started("a2"), said("好的", "a2"), audio("a2"), done("a2"))
    await _checked(bodies, 2)
    assert bodies[1] == {"context": "", "latest_user": NOW, "context_withheld": True}
    # A permitted follow-up gets the call's own record back.
    await _heard(bridge, "我们刚才都聊了哪些视频？", "follow-up")
    allowed = new.commands("instructions")[-1][1]
    assert SUMMARY in allowed and EARLIER in allowed
    await bridge.close()


async def test_a_reconnected_restricted_turn_whose_answers_keep_failing_is_handed_over_in_its_scope(monkeypatch):
    from tests.unit.test_voice_memory_context import _refuse_every_answer
    bridge, new, clock, bodies = await _dropped_after_restriction(monkeypatch, _router_down)
    await bridge.fill_idle()
    await drain()
    await _refuse_every_answer(bridge, new, clock, 4)
    for _ in range(50):
        if bridge.link.started:
            break
        await asyncio.sleep(0.01)
    [handed] = bridge.link.started
    assert handed.transcript == NOW and handed.context["handover_source"] == "unanswered_voice_reply"
    assert handed.context["read_scope"] == {"read_policy": "no_history"}
    assert not {"call", "summary", "user_words", "earlier_user_words"} & set(handed.context)
    assert _leaks(handed.context) == []
    assert len(new.commands("create")) == 4 and _history_in(_sent(new)) == []  # every retry kept the scope
    await bridge.close()


async def test_an_unconfirmed_restricted_turn_keeps_its_restriction_when_the_replacement_preparation_fails(
        monkeypatch):
    from tests.unit.test_voice_bridge import replay
    from tests.unit.test_voice_memory_context import _unconfirmed
    recorded(monkeypatch)
    decisions = iter([lambda text: _restricted_by_primary(text, "timeout"), _router_down])
    async def prepare(scope, text, recent):
        return next(decisions)(text), "fresh"
    bridge, provider, clock = call(prepare)
    fresh = _reopening(bridge)
    bridge.keeper.summary = SUMMARY
    bridge.desk.show(SEND_CARD)
    bridge.spoken.add("user", EARLIER, "first")
    _unconfirmed(provider)
    await _heard(bridge, NOW, "now")
    assert provider.closed and not provider.commands("create")  # never answered from unconfirmed instructions
    assert await bridge.recover(provider)
    [new] = fresh
    [(_, note)] = new.commands("note")
    await replay(bridge, item("carried", text=note))
    for _ in range(5):
        await drain()
    await bridge.fill_idle()
    await drain()
    assert len(new.commands("create")) == 1 and _history_in(_sent(new)) == []
    assert bridge.memory_packet["degraded_reasons"] == ["voice_preparation_read"]
    await bridge.close()


async def test_an_uncaptured_carried_turn_keeps_its_restriction_when_a_later_stage_fails(monkeypatch):
    from tests.unit.test_voice_bridge import replay
    from voice.memory_context import keeps_call_context
    recorded(monkeypatch)
    bridge, provider, clock = call()
    bridge.keeper.summary = SUMMARY
    bridge.desk.show(SEND_CARD)
    bridge.spoken.add("user", EARLIER, "first")
    bridge.spoken.add("user", NOW, "lost-audio")
    bridge._user_utterances.update({"first": 1, "lost-audio": 2})
    bridge._last_user_item = "lost-audio"
    bridge._restricted_items["lost-audio"] = {"read_policy": "no_history"}
    bridge.skip_recall("lost-audio")  # its final ASR was never durably captured
    original, failed = bridge._retire_call_context, []
    async def flaky(packet, item_id):
        if not failed:
            failed.append(item_id)
            raise RuntimeError("cleanup stage failed")
        return await original(packet, item_id)
    bridge._retire_call_context = flaky
    await bridge._carry_utterance("lost-audio", NOW)
    [(_, note)] = provider.commands("note")
    await replay(bridge, item("carried", text=note))
    for _ in range(5):
        await drain()
    await bridge.fill_idle()
    await drain()
    assert failed == ["lost-audio"] and len(provider.commands("create")) == 1
    assert _history_in(_sent(provider)) == [] and not keeps_call_context(bridge.memory_packet)
    await bridge.close()


@pytest.mark.parametrize("held, decided, kept", [
    ("current_only", "no_history", "current_only"),    # a weaker restriction cannot loosen it
    ("no_history", "current_only", "current_only"),    # a narrower one applies
    ("no_history", "documents_only", "current_only"),  # together: only the current input
    ("no_history", "allowed", "no_history"),           # lifted: held, with the live task state it permits
])
async def test_a_carried_turns_restriction_only_narrows(monkeypatch, held, decided, kept):
    from tests.unit.test_voice_bridge import replay
    from memory.read_contract import permits
    from voice.memory_context import keeps_call_context
    recorded(monkeypatch)
    async def prepare(scope, text, recent):
        return packet(text, decided), "fresh"
    bridge, provider, clock = call(prepare)
    bridge.spoken.add("user", NOW, "lost-audio")
    bridge._user_utterances["lost-audio"] = 1
    bridge._last_user_item = "lost-audio"
    bridge._restricted_items["lost-audio"] = {"read_policy": held}
    await bridge._carry_utterance("lost-audio", NOW)
    [(_, note)] = provider.commands("note")
    await replay(bridge, item("carried", text=note))
    for _ in range(5):
        await drain()
    assert not keeps_call_context(bridge.memory_packet)
    assert bridge._withheld_policy("lost-audio") == kept
    assert bridge.memory_turn.read_policy == kept
    assert permits(bridge.memory_turn, "task_state") is (kept == "no_history")
    await bridge.close()


async def test_a_carried_note_is_an_earlier_line_once_the_user_speaks_again(monkeypatch):
    """Review 2026-10-27: the carried note outlived its turn, so later restricted turns kept it."""
    from tests.unit.test_voice_bridge import replay
    recorded(monkeypatch)
    policies = iter(["allowed", "no_history"])
    async def prepare(scope, text, recent):
        return packet(text, next(policies)), "fresh"
    bridge, provider, clock = call(prepare)
    bridge.spoken.add("user", "我老婆叫王丽，帮我订她生日的花", "lost")
    bridge._user_utterances["lost"] = 1
    bridge.heard_count = 1
    bridge._last_user_item = "lost"
    await bridge._carry_utterance("lost", "我老婆叫王丽，帮我订她生日的花")
    [(_, note)] = provider.commands("note")
    await replay(bridge, item("carried", text=note))
    for _ in range(5):
        await drain()
    await bridge.fill_idle()
    await drain()
    await replay(bridge, started("a1"), audio("a1"), done("a1"))
    assert bridge._carried_item == "carried"  # held while its own turn is answered
    await _heard(bridge, "别用之前说的任何内容，现在几点了", "u2")
    assert ("delete", "carried") in provider.sent
    assert [held.id for held in bridge.spoken.items] == ["u2"]
    await bridge.close()


def test_two_read_decisions_combine_to_what_both_permit():
    from memory.read_contract import narrowest
    assert narrowest("allowed", "no_history") == "no_history"
    assert narrowest("no_history", "documents_only") == "current_only"
    assert narrowest("uncertain", "no_history") == "uncertain"
    assert narrowest("current_only", "uncertain") == "current_only"
    assert narrowest(None, "documents_only") == "documents_only"
    assert narrowest("allowed", "action_only") == "action_only"
    assert narrowest("unknown-policy", "allowed") == "uncertain"
    assert narrowest() is None and narrowest(None, "") is None


@pytest.mark.parametrize("floor, own, held", [
    ("current_only", "no_history", "current_only"),
    ("no_history", "documents_only", "current_only"),
    ("documents_only", "no_history", "current_only"),
    ("current_only", "documents_only", "current_only"),
    ("uncertain", "no_history", "uncertain"),
])
def test_the_floor_keeps_only_what_both_decisions_permit(floor, own, held):
    """Review 2026-10-27: a floor applied only over allowed let current_only read task state and
    no_history search documents when the assistant's own decision was another restriction."""
    from assistant.projection import voice_read_floor
    from memory.read_contract import permits
    floored = voice_read_floor(_bundle(own), _voice_message({"read_policy": floor}))
    assert floored["route"]["read_policy"] == held and floored["_turn"].read_policy == held
    assert not permits(floored["_turn"], "task_state")
    assert not permits(floored["_turn"], "documents", bounded=True)
    assert floored["items"] == [] and "task_state" not in floored
