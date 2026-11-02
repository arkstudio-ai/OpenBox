"""Fixed prompts behind the first video a new user gets (docs/LIGHT_MODE_PLAN.md §3.3).

Industry × template → a prompt with and without a reference image. The shop
name never goes into the prompt (the model draws wrong characters); it is
overlaid as a text clip after generation. ``negative`` is appended to every
prompt so no template can forget it.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

FALLBACK_INDUSTRY = "other"


class Option(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=32, pattern=r"^[a-z][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=200)


class TemplatePrompt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    without_image: str = Field(min_length=10, max_length=2000)
    with_image: str = Field(min_length=10, max_length=2000)
    slogan: str = Field(default="", max_length=40)
    verified_at: str | None = None


class FirstVideoTemplates(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1, max_length=64)
    eta_seconds: int = Field(ge=10, le=1800)
    duration_seconds: int = Field(ge=4, le=30)
    tier: str = Field(default="low", pattern=r"^(ultra|high|medium|low|fast)$")
    ratio: str = Field(default="9:16", max_length=16)
    negative: str = Field(min_length=1, max_length=500)
    industries: list[Option] = Field(min_length=1)
    templates: list[Option] = Field(min_length=1)
    prompts: dict[str, dict[str, TemplatePrompt]]

    @model_validator(mode="after")
    def _complete(self) -> "FirstVideoTemplates":
        industry_ids = [option.id for option in self.industries]
        template_ids = [option.id for option in self.templates]
        if len(set(industry_ids)) != len(industry_ids) or len(set(template_ids)) != len(template_ids):
            raise ValueError("duplicate industry or template id")
        if FALLBACK_INDUSTRY not in industry_ids:
            raise ValueError(f"industries must include {FALLBACK_INDUSTRY!r}")
        for industry in industry_ids:
            missing = set(template_ids) - set(self.prompts.get(industry) or {})
            if missing:
                raise ValueError(f"prompts missing for {industry}: {sorted(missing)}")
        unknown = set(self.prompts) - set(industry_ids)
        if unknown:
            raise ValueError(f"prompts for unknown industries: {sorted(unknown)}")
        return self

    def industry_ids(self) -> set[str]:
        return {option.id for option in self.industries}

    def template_ids(self) -> set[str]:
        return {option.id for option in self.templates}

    def label(self, kind: str, option_id: str) -> str:
        options = self.industries if kind == "industry" else self.templates
        return next((option.label for option in options if option.id == option_id), option_id)

    def entry(self, industry: str, template: str) -> TemplatePrompt:
        table = self.prompts.get(industry) or self.prompts[FALLBACK_INDUSTRY]
        if template not in table:
            raise KeyError(template)
        return table[template]

    def prompt(self, industry: str, template: str, *, with_image: bool) -> str:
        entry = self.entry(industry, template)
        text = (entry.with_image if with_image else entry.without_image).strip()
        return f"{text}{self.negative}"

    def public(self) -> dict:
        return {
            "eta_seconds": self.eta_seconds,
            "duration_seconds": self.duration_seconds,
            "industries": [option.model_dump() for option in self.industries],
            "templates": [option.model_dump() for option in self.templates],
        }


@lru_cache(maxsize=4)
def _load(path: str) -> FirstVideoTemplates:
    return FirstVideoTemplates.model_validate_json(Path(path).read_text(encoding="utf-8"))


def first_video_templates() -> FirstVideoTemplates:
    path = os.environ.get("LIGHT_FIRST_VIDEO_TEMPLATES_FILE") or str(Path(__file__).with_name("first_video_templates.json"))
    return _load(path)
