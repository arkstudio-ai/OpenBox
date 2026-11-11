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

#: 2026-11-04 engine sampler (job c565… refused slideleft; job …b rendered): wiperight and perlin blended as named.
VERIFIED_TRANSITIONS: frozenset[str] = frozenset({"fade", "wiperight", "perlin"})
#: IMS validates the transition name before rendering (2026-11-04: "slideleft",
#: an ffmpeg xfade name, was refused with InvalidTimelineFormat), so only the
#: names its own pages use are offered: the four wipes, 蔓延 and random.
KNOWN_TRANSITIONS: frozenset[str] = VERIFIED_TRANSITIONS | frozenset({
    "wiperight", "wipeleft", "wipeup", "wipedown", "perlin", "random",
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

#: 2026-11-04 sampler: "neon_cyan" rendered as plain white outlined text — EffectColorStyle alone does not
#: switch the look (the 花字 example page may need SubtitleEffects too); none verified yet.
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

#: Colour filters (EffectTracks items, ``Type: Filter``): the named presets on the
#: IMS use-case page. ``color`` takes its own grading parameters and is not offered.
#: 2026-11-04 sampler: warm (saturation 35→84, r-b 9→48 on the dance clip) and m7 (灰橙: 20→51, 6→19).
VERIFIED_FILTERS: frozenset[str] = frozenset({"warm", "m7"})
KNOWN_FILTERS: frozenset[str] = VERIFIED_FILTERS | frozenset({"warm", "m7", "pl3", "pj2", "pj3"})

#: Clip-level VFX (``Effects: [{Type: VFX, SubType}]``). ``h_blur`` is what a
#: blurred backdrop under a landscape clip uses.
#: 2026-11-04 sampler: h_blur as the blurred backdrop under a contain shot rendered as intended.
VERIFIED_VFX: frozenset[str] = frozenset({"h_blur"})
KNOWN_VFX: frozenset[str] = VERIFIED_VFX | frozenset({"h_blur", "withcircleflashlight", "movie", "image_in_image"})

#: Camera moves (``Effects: [{Type: KenBurns, Start, End}]``), each a start and an
#: end window as ``x,y,w,h`` fractions of the source frame.
MOVE_PRESETS: dict[str, tuple[tuple[float, float, float, float], tuple[float, float, float, float]]] = {
    "zoom_in": ((0.0, 0.0, 1.0, 1.0), (0.08, 0.08, 0.84, 0.84)),
    "zoom_out": ((0.08, 0.08, 0.84, 0.84), (0.0, 0.0, 1.0, 1.0)),
    "pan_left": ((0.15, 0.0, 0.85, 1.0), (0.0, 0.0, 0.85, 1.0)),
    "pan_right": ((0.0, 0.0, 0.85, 1.0), (0.15, 0.0, 0.85, 1.0)),
    "pan_up": ((0.0, 0.15, 1.0, 0.85), (0.0, 0.0, 1.0, 0.85)),
    "pan_down": ((0.0, 0.0, 1.0, 0.85), (0.0, 0.15, 1.0, 0.85)),
}
#: 2026-11-04 sampler: zoom_in visibly enlarged the frame over 6 s; pan_right could not be told from the clip's own camera move.
VERIFIED_MOVES: frozenset[str] = frozenset({"zoom_in"})

#: AI_TTS voices, from ``aliyun ice list-smart-voice-groups`` (cn-shanghai, 2026-11-04).
#: Keyed by id; the label is what a plan card may show.
KNOWN_VOICES: dict[str, str] = {
    # 通用
    "zhiyuan": "知媛 · 普通话女声", "zhiyue": "知悦 · 普通话女声", "zhistella": "知莎 · 普通话女声",
    "zhida": "知达 · 普通话男声", "aiqi": "艾琪 · 温柔女声", "aicheng": "艾诚 · 标准男声",
    "aijia": "艾佳 · 标准女声", "siqi": "思琪 · 温柔女声", "sijia": "思佳 · 标准女声",
    "ruoxi": "若兮 · 温柔女声", "aida": "艾达 · 标准男声", "sicheng": "思诚 · 标准男声",
    # 文学 / 解说
    "aiyuan": "艾媛 · 知心姐姐", "aichu": "艾厨 · 舌尖男声", "ainan": "艾楠 · 广告男声",
    "aixiang": "艾祥 · 磁性男声", "aimo": "艾墨 · 情感男声", "aiming": "艾茗 · 诙谐男声",
    "aiye": "艾晔 · 青年男声", "aiqian": "艾倩 · 资讯女声", "aixiao": "艾笑 · 资讯女声",
    # 超高清
    "zhixiang": "知祥 · 磁性男声", "zhiqi": "知琪 · 温柔女声", "zhiqian": "知倩 · 资讯女声",
    "zhifei": "知飞 · 激昂解说", "zhinan": "知楠 · 广告男声",
    # 多情感
    "zhimi_emo": "知米 · 亲和女声", "zhitian_emo": "知甜 · 甜美女声", "zhiyan_emo": "知燕 · 直播女声",
    "zhibing_emo": "知冰 · 多情感男声", "zhifeng_emo": "知锋 · 多情感男声",
}
#: 2026-11-04 voice-only sampler: zhiyuan spoke 16 characters in about 5 s (−24 dB), silence after.
#: 2026-11-05: zhixiang (磁性男声) and zhitian_emo (甜美女声) rendered the same line with sound 0–5 s, silence after;
#: they are the 产品大片 and 快剪动感 voices of the 你拍我剪 styles.
VERIFIED_VOICES: frozenset[str] = frozenset({"zhiyuan", "zhixiang", "zhitian_emo"})

#: IMS re-times every output to this regardless of the sources (measured).
OUTPUT_FPS = 25
