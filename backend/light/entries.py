"""The Light home: which buttons exist, what each one leads to, and how it opens.

Server-side JSON so copy and defaults change with a backend restart, not an
App release (docs/LIGHT_MODE_PLAN.md §4.3, §4.5). Availability is not here:
it depends on the workspace (plan, desktop, bound platforms) and is computed
in ``light/mode.py`` when the list is served.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

ID = r"^[a-z][a-z0-9_]*$"


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=32, pattern=ID)
    label: str = Field(min_length=1, max_length=20)


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=40, pattern=ID)
    label: str = Field(min_length=1, max_length=20)
    description: str = Field(default="", max_length=120)
    needs: str = Field(default="", max_length=60)
    media_url: str | None = Field(default=None, max_length=1024)


class Subtype(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=32, pattern=ID)
    label: str = Field(min_length=1, max_length=20)
    samples: list[Sample] = Field(default_factory=list)


class Requires(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan: bool = False
    desktop: bool = False
    binding: str | None = Field(default=None, max_length=32)


class OpeningCard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=500)
    #: Used instead of ``text`` when the person picked a sample in the gallery; ``{sample}`` is replaced.
    text_with_sample: str | None = Field(default=None, max_length=500)
    chips: list[str] = Field(default_factory=list, max_length=6)


class Entry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=32, pattern=ID)
    title: str = Field(min_length=1, max_length=20)
    subtitle: str = Field(default="", max_length=60)
    kind: str = Field(pattern=r"^(primary|secondary|coming_soon)$")
    skill: str = Field(min_length=1, max_length=64)
    default_tier: str | None = Field(default=None, pattern=r"^(ultra|high|medium|low|fast)$")
    price_hint: str = Field(default="", max_length=60)
    requires: Requires = Field(default_factory=Requires)
    steps: list[Step] = Field(default_factory=list)
    subtypes: list[Subtype] = Field(default_factory=list)
    opening_card: OpeningCard
    sample_media: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _steps_unique(self) -> "Entry":
        ids = [step.id for step in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError(f"entry {self.id}: duplicate step id")
        if self.kind != "coming_soon" and not self.steps:
            raise ValueError(f"entry {self.id}: an available entry needs steps")
        return self

    def step_ids(self) -> list[str]:
        return [step.id for step in self.steps]

    def sample(self, subtype_id: str | None, sample_id: str | None) -> tuple[Subtype | None, Sample | None]:
        subtype = next((item for item in self.subtypes if item.id == subtype_id), None)
        sample = next((item for item in (subtype.samples if subtype else []) if item.id == sample_id), None)
        return subtype, sample

    def opening(self, subtype_id: str | None = None, sample_id: str | None = None) -> OpeningCard:
        """The first assistant message, with the chosen style named when there is one."""
        subtype, sample = self.sample(subtype_id, sample_id)
        chosen = " · ".join(part.label for part in (subtype, sample) if part is not None)
        text = self.opening_card.text
        if chosen and self.opening_card.text_with_sample:
            text = self.opening_card.text_with_sample.replace("{sample}", chosen)
        return OpeningCard(text=text, chips=list(self.opening_card.chips))

    def public(self) -> dict:
        return self.model_dump(mode="json")


class LightEntries(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1, max_length=64)
    entries: list[Entry] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> "LightEntries":
        ids = [entry.id for entry in self.entries]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate entry id")
        return self

    def entry(self, entry_id: str) -> Entry | None:
        return next((entry for entry in self.entries if entry.id == entry_id), None)


@lru_cache(maxsize=4)
def _load(path: str) -> LightEntries:
    return LightEntries.model_validate_json(Path(path).read_text(encoding="utf-8"))


def light_entries() -> LightEntries:
    return _load(os.environ.get("LIGHT_ENTRIES_FILE") or str(Path(__file__).with_name("entries.json")))
