"""A change of plan is handed over without treating it as old-card consent."""
import json

from tests.support.voice_fakes import done, started, tool_call
from tests.unit.test_voice_cards import call, greeted, replay, said, FORM  # noqa: F401
from voice import tools


async def test_revision_spoken_before_reading_old_card_preserves_exact_target(call, monkeypatch):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await said(bridge, "讲稿可以，但改用刚上传的水果店照片重新做。", "change")
    form = {**FORM, "version": "a" * 64}
    bridge.desk.show_question(form, "zh")
    args = json.dumps({"request": "沿用讲稿，改用刚上传的水果店照片重新规划视频。",
                       "mode": "revise", "question_id": "q-video"})
    await replay(bridge, started("revision"), tool_call("change-call", arguments=args,
                 response_id="revision"), done("revision"))
    assert len(link.started) == 1
    ref = link.started[0]
    assert ref.context["revision_request"] == {"request_id": "q-video", "version": "a" * 64,
                                               "session_id": "video-session"}
    assert ref.transcript == "讲稿可以，但改用刚上传的水果店照片重新做。"
    calls = []
    async def run(name, scope, arguments):
        calls.append(json.loads(arguments)["request_id"])
        return {"status": "unavailable"}
    monkeypatch.setattr(tools, "run", run)
    for identity in ("q-video", "unrelated"):
        await bridge._complete_direct_call(tool_call("answer-" + identity, name="task_question_answer",
            arguments=json.dumps({"request_id": identity})))
    assert calls == ["unrelated"]
    assert any(command[2].get("status") == "revision_pending" for command in provider.commands("output"))
    await bridge.close()


async def test_revision_without_an_exact_pending_card_does_not_start_main_work(call):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await said(bridge, "用刚才那张照片重做", "change")
    await replay(bridge, started("revision"), tool_call("change-call", arguments=json.dumps({
        "request": "用刚才那张照片重做", "mode": "revise"}), response_id="revision"), done("revision"))
    assert not link.started
    assert provider.commands("output")[-1][2]["status"] == "revision_target_required"
    await bridge.close()


async def test_incompatible_material_answer_automatically_hands_over_complete_request(call, monkeypatch):
    bridge, provider, link, _ = call
    await greeted(bridge, provider)
    await said(bridge, "用刚上传的照片做背景，讲稿不变，加字幕。", "change")
    bridge.desk.show_question({**FORM, "version": "a" * 64}, "zh")
    async def requires_revision(*_):
        return {"status": "revision_required", "request_id": "q-video", "attachment_ids": ["asset-picture"]}
    monkeypatch.setattr(tools, "run", requires_revision)
    await replay(bridge, started("revision"), tool_call("change-call", name="task_question_answer",
        arguments=json.dumps({"request_id": "q-video"}), response_id="revision"), done("revision"))
    assert len(link.started) == 1
    assert link.started[0].transcript == "用刚上传的照片做背景，讲稿不变，加字幕。"
    assert "asset-picture" in link.started[0].text
    assert link.started[0].context["revision_request"]["request_id"] == "q-video"
    assert provider.commands("output")[-1][2]["status"] == "accepted"
    await bridge.close()
