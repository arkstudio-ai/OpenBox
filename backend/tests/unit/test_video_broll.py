"""A shot with no speaker: b-roll.

The pipeline was built around a talking head, so every segment had to carry
dialogue that matched the approved script word for word and a prompt describing
how the person delivers it. "Make me a five-second shot of a cat" was therefore
impossible — and forcing one through produced a transcription verdict of
"suspect" at similarity 0, because there was nothing to hear.

What b-roll relaxes is exactly the speech-performance rules. Every approval and
spend gate still applies: this is not a way around being approved or paying.
"""
from types import SimpleNamespace

import pytest

from tool.video_workflow import _derive_status, lint_segment_prompt

ANCHOR = "全片一致的画面基底：阳光柔和的草地、同一只橘猫、清透写实自然光"
BROLL_PROMPT = f"{ANCHOR}，缓步走过草地，无字幕"


def _lint(prompt, *, speech, script="橘猫走过草地"):
    return lint_segment_prompt(
        script_text=script, prompt=prompt, visual_anchor=ANCHOR,
        image_count=0, video_count=0, speech=speech,
    )


def test_a_shot_with_no_speaker_needs_no_performance_direction():
    assert _lint(BROLL_PROMPT, speech=False)["ok"]


def test_the_same_prompt_is_still_refused_for_a_spoken_segment():
    """The relaxation must not leak: a talking head still needs all of it."""
    failed = _lint(BROLL_PROMPT, speech=True)
    assert not failed["ok"]
    codes = {issue["code"] for issue in failed["issues"]}
    assert {"dialogue_exact", "fixed_camera", "framing", "natural_action", "tone"} <= codes


def test_b_roll_still_obeys_the_rules_that_hold_for_any_shot():
    """Continuity, no burned-in subtitles, and honest asset references."""
    no_anchor = _lint("缓步走过草地，无字幕", speech=False)
    assert "visual_continuity" in {i["code"] for i in no_anchor["issues"]}

    subtitled = _lint(f"{ANCHOR}，缓步走过草地", speech=False)
    assert "no_subtitles" in {i["code"] for i in subtitled["issues"]}

    leaked = _lint(f"{ANCHOR}，无字幕，https://cdn.example/cat.png", speech=False)
    assert "unsafe_asset_reference" in {i["code"] for i in leaked["issues"]}


def test_b_roll_is_not_a_way_to_smuggle_a_long_script():
    """No dialogue rules apply because there is no dialogue to police."""
    long_text = "字" * 200
    assert _lint(BROLL_PROMPT, speech=False, script=long_text)["ok"]
    # The same text as actual dialogue is still over the hard limit.
    assert "dialogue_too_long" in {
        i["code"] for i in _lint(BROLL_PROMPT, speech=True, script=long_text)["issues"]
    }


def _segment(**kw):
    base = dict(
        id="seg", status="generated", output_asset_id="asset",
        review_status="user_approved", stt_verdict=None,
        transcript_text=None, stt_similarity=None, role="broll",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _production():
    return SimpleNamespace(
        id="prod", script_hash="sh", plan_hash="ph", quality_policy="required",
        render_asset_id=None, resolution="720p", ratio="9:16", channel_name="",
    )


@pytest.mark.asyncio
async def test_b_roll_does_not_wait_for_a_transcription_verdict(monkeypatch):
    """Otherwise the production parks forever on a shot with nothing to hear."""
    import tool.video_workflow as wf

    async def approved(*_a, **_k):
        return SimpleNamespace(id="appr", max_calls=1, used_calls=0)

    monkeypatch.setattr(wf, "_matching_approval", approved)
    status = await _derive_status(None, _production(), [_segment()])
    assert status != "generated", "b-roll must not hold the production at generated"


@pytest.mark.asyncio
async def test_a_spoken_segment_still_waits_for_its_verdict(monkeypatch):
    import tool.video_workflow as wf

    async def approved(*_a, **_k):
        return SimpleNamespace(id="appr", max_calls=1, used_calls=0)

    monkeypatch.setattr(wf, "_matching_approval", approved)
    status = await _derive_status(None, _production(), [_segment(role="body")])
    assert status == "generated"
