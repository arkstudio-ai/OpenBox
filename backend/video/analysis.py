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


class AnalysisParseError(ValueError):
    pass


def parse_analysis(text: str) -> VideoAnalysis:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise AnalysisParseError("model returned no JSON object")
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as exc:
        raise AnalysisParseError(f"model JSON is invalid: {exc.msg}") from exc
    try:
        return VideoAnalysis.model_validate(data)
    except ValidationError as exc:
        raise AnalysisParseError(f"analysis does not match the schema: {exc.errors()[0]['msg']}") from exc
