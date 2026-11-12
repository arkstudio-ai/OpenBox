"""Plan M4 (docs/VOICE_DISPATCH_PLAN_20261112.md §4.5, §4.6): a small question is answered within the assistant's
turn, the phone's dispatch rule is written once, and every work type a helper can get has its guidance."""
import typing

from tests.support.voice_fakes import event, item, transcript
from tests.unit.test_voice_dispatch_route import routed
from tests.unit.test_voice_routing import greeted, replay
from voice import handoff, prompt


async def test_a_small_question_is_handed_over_marked_to_be_answered_within_the_turn():
    bridge, provider, link, _ = routed({"明天上海天气怎么样": handoff.QuickAnswer(), "帮我做一条酒吧视频": True})
    await greeted(bridge, provider)
    await replay(bridge, event("user_started"), item("u1"), transcript("明天上海天气怎么样", "u1"),
                 event("user_stopped"))
    await replay(bridge, event("user_started"), item("u2"), transcript("帮我做一条酒吧视频", "u2"),
                 event("user_stopped"))
    quick, work = link.started
    assert quick.context.get("direct_answer") is True and "direct_answer" not in work.context
    assert "no tasks.submit" in prompt.voice_turn_block(quick.context)
    assert "no tasks.submit" not in prompt.voice_turn_block(work.context)
    await bridge.close()


async def test_route_reads_the_fifth_way(monkeypatch):
    async def complete(model, system, payload, timeout):
        assert "answer" in system
        return '{"route":"answer","card":""}'

    monkeypatch.setattr("voice.summary.complete", complete)
    assert isinstance(await handoff.route("明天天气怎么样", ""), handoff.QuickAnswer)


def test_the_phone_dispatch_rule_is_written_once_and_reaches_every_phone_turn():
    from light import prompt as light_prompt
    block = prompt.voice_turn_block({})
    assert prompt.PHONE_DISPATCH in block and prompt.PHONE_DISPATCH not in prompt.voice_turn_block({"direct_answer": True})
    rules = "".join(light_prompt.ASSISTANT_LIGHT_RULES)
    assert "以那段来电上下文里写的为准" in rules
    # Light keeps no wording of its own for the phone: no second place to drift (2026-11-07).
    assert "电话里提的这几类活一律" not in rules


def test_every_work_type_a_helper_can_get_has_its_guidance():
    """2026-11-03 (QWH): other_video reached a helper with a bare brief; it planned and stopped."""
    from assistant.commands import VIDEO_WORKFLOW_GUIDANCE, VIDEO_WORKFLOWS
    from light.work_types import WORKFLOWS as LIGHT_WORKFLOWS
    from tool.assistant_tools import SubmitArgs
    work_types = set(typing.get_args(SubmitArgs.model_fields["work_type"].annotation))
    # The same map tasks.submit uses (tool/assistant_tools.py), whichever entry the request came through:
    # a phone call, the text conversation and a Light home entry all end in tasks.submit.
    workflows = {"spoken_video": "video-production", "other_video": "video", **LIGHT_WORKFLOWS}
    unguided = {"general", "video"}  # general work has no fixed skill; "video" is "format not decided yet"
    assert work_types - unguided == set(workflows), "a new work type needs a workflow and its guidance"
    for work_type, workflow in workflows.items():
        assert workflow in VIDEO_WORKFLOWS, work_type
        guidance = VIDEO_WORKFLOW_GUIDANCE[workflow]
        assert "skill" in guidance, f"{work_type}: the helper is not told which skill to load"
        assert "done only when" in guidance or "delivery" in guidance, f"{work_type}: no rule for what done means"
