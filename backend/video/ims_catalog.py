"""What Aliyun IMS cloud editing accepts, split by how sure we are.

``VERIFIED_*`` values rendered correctly in a real job on 2026-09-09
(work/ims-spike). ``KNOWN_*`` values come from the IMS docs and have not been
exercised; the compiler accepts them with a warning so a skill can try them
without a code change, and rejects anything outside both sets before money is
spent. Promote a value to VERIFIED once a job has used it and the frame was
checked.

Sources:
- 转场: help.aliyun.com/zh/ims/use-cases/transition-effect-filter
- 字幕动效: help.aliyun.com/zh/ims/developer-reference/example-of-subtitle-effects-1
- 花字: help.aliyun.com/zh/ims/developer-reference/flower-effect-example
- 字体/Timeline: help.aliyun.com/zh/ims/developer-reference/timeline-configuration-description
"""
from __future__ import annotations

VERIFIED_TRANSITIONS: frozenset[str] = frozenset({"fade"})
KNOWN_TRANSITIONS: frozenset[str] = VERIFIED_TRANSITIONS | frozenset({
    # Named in the IMS transition example page.
    "wiperight", "wipeleft", "wipeup", "wipedown", "perlin", "random",
    # The rest of the documented set follows ffmpeg xfade naming.
    "slideleft", "slideright", "slideup", "slidedown",
    "circlecrop", "rectcrop", "distance", "fadeblack", "fadewhite", "radial",
    "smoothleft", "smoothright", "smoothup", "smoothdown",
    "circleopen", "circleclose", "vertopen", "vertclose", "horzopen", "horzclose",
    "dissolve", "pixelize", "diagtl", "diagtr", "diagbl", "diagbr",
    "hlslice", "hrslice", "vuslice", "vdslice", "hblur", "fadegrays",
    "wipetl", "wipetr", "wipebl", "wipebr", "squeezeh", "squeezev", "zoomin",
})

_MOTION_STEMS = (
    "blur", "wave", "scroll_right", "scroll_left", "rotateup", "close", "slingshot",
    "rotateflip", "elasticzoom", "zoom", "spring", "fade", "growth", "open", "dissovle",
    "zoomslightout", "zoomin", "zoomout", "angular", "rotate1", "rotate2",
    "brokentypewriter", "typewriter1", "typewriter2", "typewriter3",
    "slide_left", "slide_right", "slide_down", "slide_up",
    "wipe_left", "wipe_right", "wipe_down", "wipe_up",
)
VERIFIED_MOTION_IN: frozenset[str] = frozenset({"slide_down_in", "slide_right_in", "zoomin_in"})
KNOWN_MOTION_IN: frozenset[str] = VERIFIED_MOTION_IN | frozenset(f"{s}_in" for s in _MOTION_STEMS) | {"sunrise_in"}
VERIFIED_MOTION_OUT: frozenset[str] = frozenset({"fade_out"})
KNOWN_MOTION_OUT: frozenset[str] = VERIFIED_MOTION_OUT | frozenset(f"{s}_out" for s in _MOTION_STEMS) | {"sunset_out"}

VERIFIED_FONTS: frozenset[str] = frozenset({"AlibabaPuHuiTi"})
KNOWN_FONTS: frozenset[str] = VERIFIED_FONTS | frozenset({"SimSun", "KaiTi", "HappyZcool-2016"})

VERIFIED_COLOR_STYLES: frozenset[str] = frozenset()
KNOWN_COLOR_STYLES: frozenset[str] = frozenset(
    [f"CS0001-{n:06d}" for n in range(1, 17)] + [f"CS0002-{n:06d}" for n in range(1, 17)] + [
        "white_grad", "red_grad", "blue_grad", "yellow_grad", "random_grad",
        "cd", "brushed_aluminium", "golden", "wood_1", "wood_2",
        "flare_glow_angular_1", "flare_glow_radial_1", "flare_glow_radial_2",
        "flare_glow_radial_3", "flare_glow_radial_4",
        "neon_green", "neon_cyan", "tropical_colors", "skyline", "sunrise",
        "cold_steel", "deep_sea", "burning_paper", "metallic_something",
    ]
)

#: IMS re-times every output to this regardless of the sources (measured).
OUTPUT_FPS = 25
