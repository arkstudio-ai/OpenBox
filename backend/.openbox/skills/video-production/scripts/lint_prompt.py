#!/usr/bin/env python3
"""Check a spoken-segment prompt before spending anything on it.

Ported from the server-side gate that used to REFUSE a submit. Here it advises:
run it, read what it says, then decide. The rules encode what was measured to
matter for this kind of video — the exact line after @, one consistent visual
base, a locked camera, a stated tone, and "no subtitles" so captions stay a
post step — but a shot that breaks one on purpose is allowed to.

    lint_prompt.py --script "本段台词" --prompt-file seg1.txt --anchor "画面基底"
    lint_prompt.py --broll --prompt-file broll.txt --anchor "画面基底"

--broll drops the rules that only make sense for someone delivering lines.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

#: Punctuation/fillers are inaudible, so they do not count toward the limit.
_PUNCT = re.compile(r"[\s。！？；：，、,.!?;:…·~—\-\"\'“”‘’（）()《》<>【】\[\]]+")
_FILLERS = "嗯呃唔诶哦噢喔呀啊吧呢啦嘛"


def normalize_spoken_text(value: str) -> str:
    compact = _PUNCT.sub("", value or "")
    return compact.translate({ord(char): None for char in _FILLERS})


PROMPT_LINT_RULES = {
    "dialogue_exact": {
        "requirement": (
            "The prompt must contain @ immediately followed by the exact segment dialogue."
        ),
        "accepted_examples": ["@<本段逐字台词>", "Speak exactly: @<exact segment dialogue>"],
    },
    "visual_continuity": {
        "requirement": "Declare one consistent visual base/anchor for the whole video.",
        "accepted_examples": [
            "全片一致的画面基底：同一人物、服装、场景、光线和产品",
            (
                "Consistent visual base: same presenter, wardrobe, set, lighting, "
                "and product throughout"
            ),
            "Visual anchor: same presenter and setting in every segment",
        ],
    },
    "fixed_camera": {
        "requirement": "Explicitly use a fixed/locked shot.",
        "accepted_examples": [
            "固定镜头",
            "固定机位",
            "Fixed shot",
            "Fixed camera",
            "Locked-off camera",
        ],
    },
    "framing": {
        "requirement": "Specify medium, half-body, or close-up framing.",
        "accepted_examples": ["中景", "半身", "近景", "Medium shot", "Half-body", "Close-up"],
    },
    "natural_action": {
        "requirement": "Describe a natural body/hand action for the presenter.",
        "accepted_examples": [
            "自然肢体动作：抬手展示产品",
            "Natural gestures: gently point to the product",
        ],
    },
    "tone": {
        "requirement": "Describe the speaking/performance tone.",
        "accepted_examples": ["语气：专业亲切", "Tone: calm, professional, and friendly"],
    },
    "no_subtitles": {
        "requirement": (
            "State that the generated segment has no subtitles; captions are added only in post."
        ),
        "accepted_examples": [
            "无字幕，字幕只能后期合成",
            "No subtitles; subtitles are added only in post-production",
            "Subtitles: none; captions added in post",
        ],
    },
    "unsafe_asset_reference": {
        "requirement": (
            "Do not put URLs or asset IDs in prompt text; use numbered reference labels."
        ),
        "accepted_examples": ["参考图片1", "参考视频1"],
    },
    "invalid_image_reference": {
        "requirement": (
            "Every numbered image reference must exist in this segment's supplied assets."
        ),
        "accepted_examples": ["参考图片1"],
    },
    "invalid_video_reference": {
        "requirement": (
            "Every numbered video reference must exist in this segment's supplied assets."
        ),
        "accepted_examples": ["参考视频1"],
    },
    "invalid_asset": {
        "requirement": "Every input_assets entry must be a ready image/video owned by this user.",
        "accepted_examples": ["Remove the invalid id or replace it with a ready owned asset id"],
    },
    "dialogue_too_long": {
        "requirement": "A segment may contain at most 48 normalized spoken characters.",
        "accepted_examples": ["Split this dialogue into two contiguous segments"],
    },
    "generated_output_as_reference": {
        "requirement": (
            "Reference the originally uploaded material, not a previously generated "
            "segment output of this production."
        ),
        "accepted_examples": ["Use the original upload's asset id as the reference"],
    },
}


def contains_any(value: str, phrases: tuple[str, ...]) -> bool:
    lowered = value.casefold()
    return any(phrase.casefold() in lowered for phrase in phrases)


def lint_issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def lint_prompt(
    *,
    script_text: str,
    prompt: str,
    visual_anchor: str,
    image_count: int,
    video_count: int,
    speech: bool = True,
) -> dict:
    """Lint one segment's prompt.

    ``speech=False`` (a b-roll shot) drops the rules that only make sense for
    a person delivering lines — exact dialogue, framing of that person, their
    gestures and tone. What stays is what holds for any shot in the film:
    visual continuity, no burned-in subtitles, and honest asset references.
    """
    failures: list[str] = []
    issues: list[dict[str, str]] = []
    warnings: list[str] = []

    def fail(code: str, message: str) -> None:
        failures.append(message)
        issues.append(lint_issue(code, message))

    if speech:
        spoken_length = len(normalize_spoken_text(script_text))
        if spoken_length > 48:
            fail("dialogue_too_long", f"台词 {spoken_length} 字，超过 48 字硬上限")
        elif spoken_length > 40:
            warnings.append(f"台词 {spoken_length} 字，建议压到 40 字以内")
        if f"@{script_text.strip()}" not in prompt:
            fail("dialogue_exact", "prompt 必须用 @ 紧接本段逐字台词")

    anchor = visual_anchor.strip()
    anchor_is_literal = bool(anchor) and anchor.casefold() in prompt.casefold()
    anchor_is_declared = contains_any(
        prompt,
        (
            "全片一致的画面基底",
            "全片一致画面基底",
            "画面一致性",
            "一致的视觉基底",
            "consistent visual base",
            "consistent visual anchor",
            "visual anchor",
        ),
    )
    if not (anchor_is_literal or anchor_is_declared):
        fail("visual_continuity", "prompt 缺少全片一致的画面基底")
    if speech and not contains_any(
        prompt,
        (
            "固定镜头",
            "固定机位",
            "锁定镜头",
            "fixed shot",
            "fixed camera",
            "locked-off camera",
            "locked off camera",
            "static camera",
        ),
    ):
        fail("fixed_camera", "prompt 必须显式声明固定镜头")
    if speech and not contains_any(
        prompt,
        (
            "中景",
            "半身",
            "近景",
            "中近景",
            "medium shot",
            "half-body",
            "half body",
            "close-up",
            "close up",
            "medium close-up",
        ),
    ):
        fail("framing", "prompt 缺少中景/半身/近景构图")
    if speech and not contains_any(
        prompt,
        (
            "自然肢体动作",
            "自然动作",
            "手势",
            "姿态",
            "微笑",
            "点头",
            "前倾",
            "抬手",
            "举起",
            "拿起",
            "转动",
            "托住",
            "放回",
            "指向",
            "natural gesture",
            "natural movement",
            "hand gesture",
            "point to",
            "point toward",
            "gently lift",
            "gently hold",
        ),
    ):
        fail("natural_action", "prompt 缺少自然肢体动作")
    if speech and not contains_any(
        prompt,
        ("语气", "口播语气", "tone", "delivery", "speaking style", "performance style"),
    ):
        fail("tone", "prompt 缺少语气描述")
    if not contains_any(
        prompt,
        (
            "无字幕",
            "不要字幕",
            "不显示字幕",
            "no subtitles",
            "without subtitles",
            "subtitles: none",
            "captions: none",
        ),
    ):
        fail("no_subtitles", "prompt 必须写明无字幕，字幕只能后期合成")
    if re.search(r"https?://|asset://|asset[_-][A-Za-z0-9]", prompt, re.IGNORECASE):
        fail(
            "unsafe_asset_reference",
            "prompt 正文不得包含 URL 或素材 ID，只能用参考图片N/参考视频N",
        )
    for value in re.findall(r"参考图片(\d+)", prompt):
        if int(value) < 1 or int(value) > image_count:
            fail(
                "invalid_image_reference",
                f"prompt 引用了不存在的参考图片{value}（实际 {image_count} 张）",
            )
    for value in re.findall(r"参考视频(\d+)", prompt):
        if int(value) < 1 or int(value) > video_count:
            fail(
                "invalid_video_reference",
                f"prompt 引用了不存在的参考视频{value}（实际 {video_count} 个）",
            )
    return {"ok": not failures, "failures": failures, "issues": issues, "warnings": warnings}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--script", default="")
    parser.add_argument("--anchor", required=True, help="The shared visual base, verbatim.")
    parser.add_argument("--images", type=int, default=0)
    parser.add_argument("--videos", type=int, default=0)
    parser.add_argument("--broll", action="store_true", help="A shot with nobody speaking.")
    parser.add_argument("--rules", action="store_true", help="Print the rule book and exit.")
    args = parser.parse_args()

    if args.rules:
        print(json.dumps(PROMPT_LINT_RULES, ensure_ascii=False, indent=2))
        return 0

    prompt = open(args.prompt_file, encoding="utf-8").read()
    report = lint_prompt(
        script_text=args.script,
        prompt=prompt,
        visual_anchor=args.anchor,
        image_count=args.images,
        video_count=args.videos,
        speech=not args.broll,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # Advice, not a gate: a non-zero code would turn "look at this" into
    # "stop", which is the behaviour this skill exists to get away from.
    return 0


if __name__ == "__main__":
    sys.exit(main())
