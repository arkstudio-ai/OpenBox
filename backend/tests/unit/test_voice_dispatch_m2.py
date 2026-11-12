"""voice.dispatch_mode=server, plan M2 (docs/VOICE_DISPATCH_PLAN_20261112.md §4.2, §4.3, §4.8): an unsupported
status is taken back, a result the user spoke over is offered once more, progress goes on within a budget."""
from tests.support.voice_fakes import audio, done, event, item, started, transcript
from tests.unit.test_voice_dispatch_route import routed
from tests.unit.test_voice_routing import greeted, make, replay, said
from voice import handoff
from voice.progress import RoomProgress


def creates(provider):
    return [text for _, text in provider.commands("create")]


def reads(monkeypatch, **verdict):
    asked = []

    async def claimed(text, words):
        asked.append((text, words))
        return verdict

    monkeypatch.setattr(handoff, "claimed", claimed)
    return asked


async def heard(bridge, words, reply, source="u1", response="r1"):
    await replay(bridge, event("user_started"), item(source), transcript(words, source), event("user_stopped"),
                 started(response), said(reply, response), audio(response), done(response))


async def test_a_status_no_record_supports_is_taken_back_and_the_words_handed_over(monkeypatch):
    """2026-11-07: "是的，确实已经提交" with nothing submitted; the route had read the words as talk."""
    asked = reads(monkeypatch, claims=True, work=True)
    bridge, provider, link, _ = routed({})
    await greeted(bridge, provider)
    await heard(bridge, "那条酒吧视频你弄一下", "好的，已经交给帮手在做了。")
    assert asked == [("好的，已经交给帮手在做了", "那条酒吧视频你弄一下")]
    assert [ref.transcript for ref in link.started] == ["那条酒吧视频你弄一下"]
    correction = [text for text in creates(provider) if "更正" in text]
    assert len(correction) == 1 and "现在已经把用户的原话交过去了" in correction[0]
    await bridge.close()


async def test_a_claim_about_nothing_asked_is_corrected_without_handing_anything_over(monkeypatch):
    reads(monkeypatch, claims=True, work=False)
    bridge, provider, link, _ = routed({})
    await greeted(bridge, provider)
    await heard(bridge, "你那边现在忙吗", "不忙，帮手正在做你的视频。")
    assert not link.started
    assert any("现在也没有交办任何任务" in text for text in creates(provider))
    await bridge.close()


async def test_a_status_with_a_record_behind_it_is_not_questioned(monkeypatch):
    asked = reads(monkeypatch, claims=True, work=True)
    bridge, provider, link, _ = routed({"帮我做一条酒吧视频": True})
    await greeted(bridge, provider)
    await heard(bridge, "帮我做一条酒吧视频", "好的，已经交给助理了。")
    assert len(link.started) == 1 and not asked and not any("更正" in text for text in creates(provider))
    # ... and talk that claims nothing is never sent to the model
    await heard(bridge, "谢谢", "不客气。", source="u2", response="r2")
    assert not asked
    await bridge.close()


async def test_the_model_mode_never_checks_what_was_said(monkeypatch):
    asked = reads(monkeypatch, claims=True, work=True)
    bridge, provider, link = make()
    await greeted(bridge, provider)
    await heard(bridge, "那条酒吧视频你弄一下", "好的，已经交给帮手在做了。")
    assert not asked and not link.started
    await bridge.close()


async def test_a_report_the_user_spoke_over_is_offered_once_more_after_their_words_are_answered():
    bridge, provider, link, _ = routed({})
    await greeted(bridge, provider)
    await bridge.report("酒吧视频", "酒吧视频做好了，成片在对话里。", "res-1", "t1")
    await replay(bridge)
    note = len(provider.commands("note"))
    await replay(bridge, started("d1"), audio("d1", size=48000), event("user_started"))  # spoken over
    await replay(bridge, done("d1", status="cancelled"), item("u1"), transcript("等一下，今天几号", "u1"),
                 event("user_stopped"), started("r1"), said("今天十二号。", "r1"), audio("r1"), done("r1"))
    ref = bridge.deliveries[0]
    assert ref.replayed and len(provider.commands("note")) == note + 1  # told again, once
    assert "补上这条结果的结论" in creates(provider)[-1]
    await replay(bridge, started("d2"), audio("d2", size=48000), event("user_started"))
    await replay(bridge, done("d2", status="cancelled"), item("u2"), transcript("知道了", "u2"),
                 event("user_stopped"), started("r2"), said("好。", "r2"), audio("r2"), done("r2"))
    assert len(provider.commands("note")) == note + 1  # spoken over again: it stays in the conversation
    await bridge.close()


async def test_a_report_the_answer_already_named_is_not_said_again():
    bridge, provider, link, _ = routed({})
    await greeted(bridge, provider)
    await bridge.report("酒吧视频", "酒吧视频做好了，成片在对话里。", "res-1", "t1")
    await replay(bridge)
    note = len(provider.commands("note"))
    await replay(bridge, started("d1"), audio("d1", size=48000), event("user_started"))
    await replay(bridge, done("d1", status="cancelled"), item("u1"), transcript("酒吧视频好了吗", "u1"),
                 event("user_stopped"), started("r1"), said("酒吧视频已经做好了。", "r1"), audio("r1"), done("r1"))
    assert bridge.deliveries[0].replayed and len(provider.commands("note")) == note
    await bridge.close()


async def test_the_helpers_new_step_is_said_within_the_budget_and_never_twice():
    bridge, provider, link, _ = routed({"帮我做一条酒吧视频": True})
    progress = bridge.room_progress = RoomProgress(user_id="u1", rooms=lambda: {"s-1"}, clock=bridge.clock)
    await greeted(bridge, provider)
    await heard(bridge, "帮我做一条酒吧视频", "好。")
    link.finish(link.started[0].provider_call_id, speech="安排了。",
                running=[{"task_id": "t1", "title": "酒吧视频", "state": "running", "session_id": "s-1"}])
    await replay(bridge)
    await replay(bridge, started("d1"), audio("d1"), done("d1"))

    def step(tool):
        progress.on_event({"type": "tool.running", "data": {"sessionId": "s-1", "userId": "u1", "tool": tool}})

    async def tick(seconds, response):
        before = len(creates(provider))
        bridge.clock.advance(seconds)
        await bridge.fill_idle()
        spoke = len(creates(provider)) > before
        if spoke:
            await replay(bridge, started(response), audio(response), done(response))
        return spoke

    step("video_production")
    assert not await tick(20, "p0")                       # a minute since the receipt has not passed
    assert await tick(45, "p1") and "在制作视频" in creates(provider)[-1]
    assert not await tick(90, "p2")                       # the same step is not said again
    step("video_compose")
    assert await tick(1, "p3") and "在剪视频" in creates(provider)[-1]
    step("share_file")
    assert await tick(90, "p4")
    step("web_search")
    assert not await tick(90, "p5")                       # three unasked lines about one task, then its result
    await bridge.report("酒吧视频", "做好了。", "res-1", "t1")
    await bridge.close()


async def test_the_model_mode_says_nothing_about_the_helpers_steps():
    bridge, provider, link = make()
    progress = bridge.room_progress = RoomProgress(user_id="u1", rooms=lambda: {"s-1"}, clock=bridge.clock)
    await greeted(bridge, provider)
    await replay(bridge, item("u1"), transcript("帮我做一条酒吧视频", "u1"), started("r1"))
    from tests.support.voice_fakes import tool_call
    await replay(bridge, tool_call("c1", "帮我做一条酒吧视频", response_id="r1"), done("r1"))
    link.finish("c1", speech="安排了。",
                running=[{"task_id": "t1", "title": "酒吧视频", "state": "running", "session_id": "s-1"}])
    await replay(bridge)
    await replay(bridge, started("d1"), audio("d1"), done("d1"))
    progress.on_event({"type": "tool.running", "data": {"sessionId": "s-1", "userId": "u1", "tool": "video_compose"}})
    before = len(creates(provider))
    bridge.clock.advance(120)
    await bridge.fill_idle()
    assert not [text for text in creates(provider)[before:] if "进度" in text]
    assert "session_id" not in (bridge.focus_task or {})
    await bridge.close()
