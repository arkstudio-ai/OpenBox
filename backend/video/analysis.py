"""What `video_analyze` returns, and the prompt that produces it.

The schema is the contract between hot-video analysis and everything after
it (the autopilot skill writes generation prompts and a compose timeline from
these fields), so it is pinned here rather than left to the model's mood.
"""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

Form = Literal["口播", "画面+旁白", "产品展示", "剧情", "混剪", "字幕型", "其他"]


class Beat(BaseModel):
    model_config = ConfigDict(extra="ignore")
    from_sec: float = Field(ge=0)
    to_sec: float = Field(ge=0)
    what: str = ""


class RecreateElements(BaseModel):
    model_config = ConfigDict(extra="ignore")
    presenter: str = ""
    scene: str = ""
    pace: str = ""
    caption_style: str = ""
    music: str = ""


class VideoAnalysis(BaseModel):
    model_config = ConfigDict(extra="ignore")
    form: Form = "其他"
    topic: str = ""
    audience: str = ""
    hook: str = ""
    structure: list[Beat] = Field(default_factory=list)
    visual_style: str = ""
    script_text: str = ""
    on_screen_text: list[str] = Field(default_factory=list)
    topics: list[str] = Field(default_factory=list)
    recreate_elements: RecreateElements = Field(default_factory=RecreateElements)
    risk_notes: str = ""


PROMPT = """你是短视频拆解专家。下面是一条短视频按时间顺序抽取的 {n} 张画面，以及它的语音转写（可能为空：说明没有配音，或文案全在字幕里）。
只输出一个 JSON 对象，不要解释、不要 Markdown 代码块。字段与含义：
{{
 "form": "口播|画面+旁白|产品展示|剧情|混剪|字幕型|其他",
 "topic": "一句话主题",
 "audience": "目标人群",
 "hook": "前 3 秒的钩子（原话或画面）",
 "structure": [{{"from_sec": 0, "to_sec": 5, "what": "这一段在做什么"}}],
 "visual_style": "镜头/画面/人物/场景描述，具体到能让视频生成模型复刻",
 "script_text": "完整文案（以转写为主，字幕补充；没有就空字符串）",
 "on_screen_text": ["画面里出现过的字幕/花字/标题"],
 "topics": ["话题词，不带#"],
 "recreate_elements": {{"presenter": "人物", "scene": "场景", "pace": "节奏", "caption_style": "字幕样式", "music": "音乐"}},
 "risk_notes": "复刻时要避开的版权/人物/医疗/广告法风险"
}}
视频时长约 {duration} 秒。转写：
{transcript}"""


#: One piece of text the vision model saw in the sampled frames.
TextKind = Literal["subtitle", "title", "watermark", "logo", "scene", "other"]


class ScreenTextItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str = ""
    kind: TextKind = "other"
    #: 1-based indices of the sampled frames the text appears in.
    frames: list[int] = Field(default_factory=list)
    #: Filled in by the tool from `frames` and the sampling rate.
    seconds: list[float] = Field(default_factory=list)


class ScreenTextReport(BaseModel):
    """`video_analyze(action="screen_text")`: is there any text in the picture?

    Built for the spoken-video skill's acceptance card. Seedance 2.0 rendered a
    presenter's lines as burnt-in captions on 2026-10-15 (Gaode, crab-voucher,
    shots 4–5, with 限时 drawn as 随时) although every prompt said 无字幕 twice;
    STT alone passed the take because the audio was right. Only the frames show
    this, so the check is a separate, cheap vision pass with a prompt that asks
    about nothing but text.
    """
    model_config = ConfigDict(extra="ignore")
    has_text: bool = False
    items: list[ScreenTextItem] = Field(default_factory=list)
    notes: str = ""


SCREEN_TEXT_PROMPT = """你是视频质检员。下面是一段生成视频按时间顺序每隔约 {interval} 秒抽取的 {n} 张画面（第 1 张约在第 0 秒，第 k 张约在第 {interval}×(k-1) 秒），视频总长约 {duration} 秒。
任务只有一个：找出画面里出现的一切文字——字幕、台词字幕、标题、花字、水印、logo、角标，以及场景里实物上的文字（招牌、包装、海报、屏幕）。不要描述画面内容，不要评价画质。
逐字抄写看到的文字（含错别字，照抄不纠正）；同一句文字出现在多张画面里只写一条，并列出它出现的画面序号。模糊到无法辨认的也要报，text 写“（无法辨认）”。没有任何文字时 has_text 为 false，items 为空数组。
只输出一个 JSON 对象，不要解释、不要 Markdown 代码块：
{{
 "has_text": true,
 "items": [{{"text": "逐字抄写", "kind": "subtitle|title|watermark|logo|scene|other", "frames": [4, 5]}}],
 "notes": "一句话补充，例如字幕位置、是否随语音变化；没有可留空"
}}
kind 说明：subtitle=随语音出现的台词字幕；title=标题/花字；watermark=水印/角标；logo=品牌标识；scene=场景实物上的文字；other=其他。"""


class AnalysisParseError(ValueError):
    pass


def _json_object(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise AnalysisParseError("model returned no JSON object")
    try:
        return json.loads(raw[start:end + 1])
    except json.JSONDecodeError as exc:
        raise AnalysisParseError(f"model JSON is invalid: {exc.msg}") from exc


def parse_screen_text(text: str, *, frame_count: int, interval_sec: float) -> ScreenTextReport:
    """Validate the text report and turn frame indices into seconds.

    Frame k (1-based) was sampled at about (k-1) × interval seconds. Indices
    outside 1..frame_count are dropped rather than trusted: a model that
    invents frame 20 of 15 is not describing the picture.
    """
    data = _json_object(text)
    try:
        report = ScreenTextReport.model_validate(data)
    except ValidationError as exc:
        raise AnalysisParseError(f"screen text does not match the schema: {exc.errors()[0]['msg']}") from exc
    for item in report.items:
        item.frames = sorted({int(k) for k in item.frames if 1 <= int(k) <= frame_count})
        item.seconds = [round((k - 1) * interval_sec, 1) for k in item.frames]
    # The flag follows the evidence, not the model's mood: items mean text.
    report.has_text = bool(report.items) or report.has_text
    return report


def parse_analysis(text: str) -> VideoAnalysis:
    data = _json_object(text)
    try:
        return VideoAnalysis.model_validate(data)
    except ValidationError as exc:
        raise AnalysisParseError(f"analysis does not match the schema: {exc.errors()[0]['msg']}") from exc
