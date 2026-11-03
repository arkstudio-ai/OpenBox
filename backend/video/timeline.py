"""The timeline OpenBox owns.

Skills describe a cut in this schema and never in a renderer's native format.
Renderers (Aliyun IMS today, Remotion as the verified fallback) are compile
targets: ``video/ims_compiler.py`` turns one of these into an IMS Timeline,
and switching engines means adding a compiler, not touching skills.

Everything is in seconds and canvas fractions. Pixels only appear at the
compiler boundary, because that is where a specific renderer's coordinate
rules live (see docs/VIDEO_RENDER_ENGINE_SELECTION.md §4.2 for why IMS needs
them spelled out).
"""
from __future__ import annotations

import json
from typing import Annotated, Literal, TypeVar

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

_M = TypeVar("_M")


def _object_from_text(value):
    """A nested object sent as JSON text (``"style": "{...}"``), read as the object.

    Tool-calling models do this now and then; on light-test (2026-11-03) it cost a
    你拍我剪 cut three rejected validations and a schema lookup before it went out."""
    if isinstance(value, str) and value.lstrip().startswith("{"):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


#: A nested object field that also accepts its JSON text.
Nested = Annotated[_M, BeforeValidator(_object_from_text)]

class Canvas(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: int = Field(default=720, ge=2, le=4096)
    height: int = Field(default=1280, ge=2, le=4096)
    fps: int = Field(default=24, ge=1, le=60)


class Transition(BaseModel):
    """Attached to the shot it leads *out of*; overlaps the next shot."""

    model_config = ConfigDict(extra="forbid")

    type: str = "fade"
    seconds: float = Field(default=0.5, gt=0, le=5)


class Shot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: ``https://<bucket>.oss-<region>.aliyuncs.com/<key>`` or ``oss://<bucket>/<key>``.
    asset: str = Field(min_length=1, max_length=2048)
    #: Trim inside the source. ``duration_sec`` None means "to the end".
    in_sec: float = Field(default=0.0, ge=0)
    duration_sec: float | None = Field(default=None, gt=0)
    #: How the source fills the canvas. ``cover`` crops, ``contain`` letterboxes
    #: (black bars, on purpose), ``fill`` stretches.
    fit: Literal["cover", "contain", "fill"] = "cover"
    transition_out: Nested[Transition | None] = None
    #: The clip's own sound: 1 keeps it as recorded, 0 mutes it (an image has none).
    volume: float = Field(default=1.0, ge=0, le=2)


class TextStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    font: str = "AlibabaPuHuiTi"
    #: Custom font file on OSS; wins over ``font`` when both are set.
    font_url: str | None = None
    size: int = Field(default=44, ge=8, le=400)
    color: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$")
    outline: int = Field(default=3, ge=0, le=40)
    outline_color: str = Field(default="#000000", pattern=r"^#[0-9A-Fa-f]{6}$")
    #: A 花字 preset id. Overrides the plain colour look on renderers that have it.
    color_style: str | None = None


class Motion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    in_effect: str | None = None
    in_sec: float = Field(default=0.3, gt=0, le=5)
    out_effect: str | None = None
    out_sec: float = Field(default=0.3, gt=0, le=5)


class TextClip(BaseModel):
    """A banner: hook title, lower third, CTA. Captions have their own list."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=500)
    from_sec: float = Field(ge=0)
    to_sec: float = Field(gt=0)
    #: Horizontal placement. ``center`` ignores ``x``.
    align: Literal["left", "center", "right"] = "center"
    #: Left edge (align=left) or right edge (align=right) as a canvas fraction.
    x: float = Field(default=0.0, ge=0.0, le=1.0)
    #: Top edge of the text box as a fraction of canvas height.
    y: float = Field(ge=0.0, le=1.0)
    #: Wrap width as a canvas fraction.
    max_width: float = Field(default=0.88, gt=0, le=1.0)
    style: Nested[TextStyle] = Field(default_factory=TextStyle)
    motion: Nested[Motion] = Field(default_factory=Motion)

    @model_validator(mode="after")
    def _ordered(self) -> "TextClip":
        if self.to_sec <= self.from_sec:
            raise ValueError(f"text clip {self.text!r}: to_sec must exceed from_sec")
        return self


class Cue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_sec: float = Field(ge=0)
    to_sec: float = Field(gt=0)
    text: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _ordered(self) -> "Cue":
        if self.to_sec <= self.from_sec:
            raise ValueError(f"cue {self.text!r}: to_sec must exceed from_sec")
        return self


class AudioClip(BaseModel):
    """A sound bed under the picture (BGM, voice-over): a file placed on the timeline.

    Rendered on its own track, mixed with each shot's own sound. A music-library
    track (``library_id``, IMS public media) repeats when it is shorter than its
    span and is cut when longer; ``duck_under_captions`` lowers it wherever a
    caption is on screen, which in a 你拍我剪 cut is wherever someone speaks.
    """

    model_config = ConfigDict(extra="forbid")

    #: Same forms as ``Shot.asset``: an owned asset id or the account's OSS object.
    asset: str | None = Field(default=None, min_length=1, max_length=2048)
    #: A track of the music library by its IMS MediaId (``icepublic-…``), instead of ``asset``.
    library_id: str | None = Field(default=None, pattern=r"^icepublic-[0-9A-Za-z]+$")
    #: Where it starts and ends on the timeline.
    from_sec: float = Field(default=0.0, ge=0)
    to_sec: float | None = Field(default=None, gt=0)
    #: Where it starts inside the source file.
    in_sec: float = Field(default=0.0, ge=0)
    volume: float = Field(default=0.3, ge=0, le=2)
    fade_in_sec: float = Field(default=0.0, ge=0, le=10)
    fade_out_sec: float = Field(default=0.0, ge=0, le=10)
    #: Lower the bed to ``duck_volume`` while a caption is showing.
    duck_under_captions: bool = False
    duck_volume: float = Field(default=0.1, ge=0, le=2)
    #: Length of the source. Known for library tracks (the tool fills it in);
    #: when the span is longer, the source starts again from ``in_sec``.
    source_sec: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> "AudioClip":
        if (self.asset is None) == (self.library_id is None):
            raise ValueError("audio clip: give exactly one of asset or library_id")
        if self.to_sec is not None and self.to_sec <= self.from_sec:
            raise ValueError("audio clip: to_sec must exceed from_sec")
        if self.source_sec is not None and self.in_sec >= self.source_sec:
            raise ValueError("audio clip: in_sec must be inside the source")
        return self


class CaptionStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Distance from the canvas bottom to the caption's baseline area.
    bottom_ratio: float = Field(default=0.095, ge=0, le=0.5)
    max_width: float = Field(default=0.88, gt=0, le=1.0)
    style: Nested[TextStyle] = Field(default_factory=TextStyle)
    motion: Nested[Motion] = Field(default_factory=lambda: Motion(in_effect="zoomin_in", in_sec=0.15))


class Timeline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = 1
    canvas: Nested[Canvas] = Field(default_factory=Canvas)
    shots: list[Shot] = Field(min_length=1)
    captions: list[Cue] = Field(default_factory=list)
    caption_style: Nested[CaptionStyle] = Field(default_factory=CaptionStyle)
    texts: list[TextClip] = Field(default_factory=list)
    audio: list[AudioClip] = Field(default_factory=list, max_length=4)

    @property
    def duration_sec(self) -> float | None:
        """Total length, or None when any shot runs "to the end" (unknown here)."""
        total = 0.0
        for index, shot in enumerate(self.shots):
            if shot.duration_sec is None:
                return None
            total += shot.duration_sec
            if shot.transition_out and index < len(self.shots) - 1:
                total -= shot.transition_out.seconds
        return round(total, 4)

    @model_validator(mode="after")
    def _transitions_fit(self) -> "Timeline":
        for index, shot in enumerate(self.shots[:-1]):
            if shot.transition_out is None or shot.duration_sec is None:
                continue
            nxt = self.shots[index + 1]
            limit = min(shot.duration_sec, nxt.duration_sec or shot.duration_sec)
            if shot.transition_out.seconds >= limit:
                raise ValueError(
                    f"shot {index}: transition of {shot.transition_out.seconds}s is not shorter "
                    f"than the clips it joins ({limit}s)"
                )
        return self


def spoken_preset(
    shots: list[Shot],
    cues: list[Cue],
    *,
    hook: str | None = None,
    hook_until_sec: float = 3.0,
    handle: str | None = None,
    handle_from_sec: float = 3.0,
    canvas: Canvas | None = None,
) -> Timeline:
    """The vertical talking-head layout the video-production skill ships.

    Hook: gold title near the top for the opening seconds. Handle: a lower
    third that slides in once the hook is gone and stays to the end.
    """
    canvas = canvas or Canvas()
    texts: list[TextClip] = []
    if hook:
        texts.append(TextClip(
            text=hook, from_sec=0, to_sec=hook_until_sec, align="center", y=0.125, max_width=0.9,
            style=TextStyle(size=56, color="#FFD700", outline=2),
            motion=Motion(in_effect="slide_down_in", in_sec=0.5, out_effect="fade_out", out_sec=0.6),
        ))
    if handle:
        end = Timeline(canvas=canvas, shots=shots).duration_sec
        texts.append(TextClip(
            text=handle, from_sec=handle_from_sec, to_sec=end if end is not None else handle_from_sec + 60,
            align="left", x=40 / canvas.width, y=930 / 1280, max_width=0.6,
            style=TextStyle(size=34, color="#FFD700", outline=2, outline_color="#111111"),
            motion=Motion(in_effect="slide_right_in", in_sec=0.5),
        ))
    return Timeline(canvas=canvas, shots=shots, captions=cues, texts=texts)
