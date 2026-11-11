"""The Light home: which buttons exist, what each one leads to, and how it opens.

Server-side JSON so copy and defaults change with a backend restart, not an
App release (docs/LIGHT_MODE_PLAN.md §4.3, §4.5). Availability is not here:
it depends on the workspace (plan, desktop, bound platforms) and is computed
in ``light/mode.py`` when the list is served.

Three files, one per interface language (docs/LIGHT_MODE_I18N_PLAN.md §2.4):
``entries.zh-CN.json`` is the structure plus the Chinese copy; ``entries.en-US.json``
and ``entries.vi-VN.json`` hold copy only (title, subtitle, price_hint, step and
subtype labels, sample label/description/needs, the opening card), matched by
id. ``light_entries(lang)`` lays that copy over the structure; a string missing
from ``vi-VN`` is taken from ``en-US`` (``core.ui_language.FALLBACK``), one
missing from ``en-US`` stays Chinese, so a half-translated file never breaks
the home.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.ui_language import DEFAULT as DEFAULT_LANG, LANGS, chain as language_chain

ID = r"^[a-z][a-z0-9_]*$"
#: Copy in the two translation files; everything else lives only in ``entries.zh-CN.json``.
COPY_FIELDS = {
    "entry": ("title", "subtitle", "price_hint"),
    "step": ("label",),
    "subtype": ("label",),
    "sample": ("label", "description", "needs"),
    "opening_card": ("text", "text_with_sample", "chips"),
}


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=32, pattern=ID)
    label: str = Field(min_length=1, max_length=40)


from light.media import MEDIA_PATTERN as MEDIA


class Sample(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=40, pattern=ID)
    label: str = Field(min_length=1, max_length=40)
    description: str = Field(default="", max_length=120)
    needs: str = Field(default="", max_length=60)
    #: The sample itself: a 9:16 clip (``.mp4``) or an image.
    media_url: str | None = Field(default=None, max_length=1024, pattern=MEDIA)
    #: A still for the gallery tile when ``media_url`` is a clip.
    poster_url: str | None = Field(default=None, max_length=1024, pattern=MEDIA)


class Subtype(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=32, pattern=ID)
    label: str = Field(min_length=1, max_length=40)
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
    title: str = Field(min_length=1, max_length=40)
    subtitle: str = Field(default="", max_length=60)
    kind: str = Field(pattern=r"^(primary|secondary|coming_soon)$")
    #: An in-app action instead of a topic chat: ``voice_call`` dials the personal assistant
    #: (docs/LIGHT_MODE_I18N_PLAN.md §3). Such an entry has no skill, steps or opening card.
    action: Literal["voice_call"] | None = None
    skill: str = Field(default="", max_length=64)
    default_tier: str | None = Field(default=None, pattern=r"^(ultra|high|medium|low|fast)$")
    price_hint: str = Field(default="", max_length=60)
    requires: Requires = Field(default_factory=Requires)
    steps: list[Step] = Field(default_factory=list)
    subtypes: list[Subtype] = Field(default_factory=list)
    opening_card: OpeningCard | None = None
    sample_media: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _steps_unique(self) -> "Entry":
        ids = [step.id for step in self.steps]
        if len(set(ids)) != len(ids):
            raise ValueError(f"entry {self.id}: duplicate step id")
        if self.action:
            if self.steps or self.skill or self.opening_card or self.subtypes:
                raise ValueError(f"entry {self.id}: an action entry has no steps, skill, subtypes or opening card")
            return self
        if self.kind != "coming_soon":
            if not self.steps:
                raise ValueError(f"entry {self.id}: an available entry needs steps")
            if not self.skill or self.opening_card is None:
                raise ValueError(f"entry {self.id}: a topic entry needs a skill and an opening card")
        return self

    @property
    def is_topic(self) -> bool:
        """Opens a guided chat (as opposed to an in-app action)."""
        return self.action is None

    def step_ids(self) -> list[str]:
        return [step.id for step in self.steps]

    def sample(self, subtype_id: str | None, sample_id: str | None) -> tuple[Subtype | None, Sample | None]:
        subtype = next((item for item in self.subtypes if item.id == subtype_id), None)
        sample = next((item for item in (subtype.samples if subtype else []) if item.id == sample_id), None)
        return subtype, sample

    def picked_label(self, subtype_id: str | None, sample_id: str | None) -> str:
        """「餐品展示 · 烟火气特写」; the subtype is left out when the entry has only one."""
        subtype, sample = self.sample(subtype_id, sample_id)
        if len(self.subtypes) <= 1:
            subtype = None
        return " · ".join(part.label for part in (subtype, sample) if part is not None)

    def opening(self, subtype_id: str | None = None, sample_id: str | None = None) -> OpeningCard:
        """The first assistant message, with the chosen style named when there is one."""
        if self.opening_card is None:
            raise ValueError(f"entry {self.id} has no opening card")
        chosen = self.picked_label(subtype_id, sample_id)
        text = self.opening_card.text
        if chosen and self.opening_card.text_with_sample:
            text = self.opening_card.text_with_sample.replace("{sample}", chosen)
        return OpeningCard(text=text, chips=list(self.opening_card.chips))

    def public(self, sign=None) -> dict:
        """The entry as the App receives it; ``sign`` (``light.media.sign_media``) turns ``oss:`` keys into links."""
        data = self.model_dump(mode="json")
        if sign is None:
            return data
        for subtype in data["subtypes"]:
            for sample in subtype["samples"]:
                for field in ("media_url", "poster_url"):
                    sample[field] = sign(sample.get(field))
        return data


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


# --- the copy-only translation files -------------------------------------------------------------

class SampleCopy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    label: str | None = None
    description: str | None = None
    needs: str | None = None


class SubtypeCopy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    label: str | None = None
    samples: list[SampleCopy] = Field(default_factory=list)


class StepCopy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    label: str | None = None


class OpeningCardCopy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str | None = None
    text_with_sample: str | None = None
    chips: list[str] | None = None


class EntryCopy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str | None = None
    subtitle: str | None = None
    price_hint: str | None = None
    steps: list[StepCopy] = Field(default_factory=list)
    subtypes: list[SubtypeCopy] = Field(default_factory=list)
    opening_card: OpeningCardCopy | None = None


class EntriesCopy(BaseModel):
    """``entries.<lang>.json`` for a language other than zh-CN: copy fields only, matched by id."""
    model_config = ConfigDict(extra="forbid")
    version: str | None = None
    entries: list[EntryCopy] = Field(default_factory=list)

    def entry(self, entry_id: str) -> EntryCopy | None:
        return next((entry for entry in self.entries if entry.id == entry_id), None)


def _overlay(target: dict, copy: BaseModel | None, fields: tuple[str, ...]) -> None:
    if copy is None:
        return
    for field in fields:
        value = getattr(copy, field, None)
        if value is not None:
            target[field] = value


def _by_id(items: list[BaseModel]) -> dict[str, BaseModel]:
    return {item.id: item for item in items}


def _apply_copy(structure: dict, copies: list[EntriesCopy]) -> dict:
    """The structure (as a plain dict) with each language's copy laid over it, lowest priority first."""
    data = json.loads(json.dumps(structure))
    for copy in copies:
        for entry in data["entries"]:
            translated = copy.entry(entry["id"])
            if translated is None:
                continue
            _overlay(entry, translated, COPY_FIELDS["entry"])
            steps = _by_id(translated.steps)
            for step in entry["steps"]:
                _overlay(step, steps.get(step["id"]), COPY_FIELDS["step"])
            subtypes = _by_id(translated.subtypes)
            for subtype in entry["subtypes"]:
                sub_copy = subtypes.get(subtype["id"])
                _overlay(subtype, sub_copy, COPY_FIELDS["subtype"])
                samples = _by_id(sub_copy.samples) if sub_copy else {}
                for sample in subtype["samples"]:
                    _overlay(sample, samples.get(sample["id"]), COPY_FIELDS["sample"])
            if entry.get("opening_card") is not None:
                _overlay(entry["opening_card"], translated.opening_card, COPY_FIELDS["opening_card"])
    return data


def structure_file() -> Path:
    """``entries.zh-CN.json``, or ``LIGHT_ENTRIES_FILE`` (a test or a deployment's own copy of it)."""
    return Path(os.environ.get("LIGHT_ENTRIES_FILE") or Path(__file__).with_name(f"entries.{DEFAULT_LANG}.json"))


def copy_file(lang: str, structure: Path | None = None) -> Path:
    """The copy file beside the structure file: ``entries.en-US.json`` next to ``entries.zh-CN.json``."""
    structure = structure or structure_file()
    stem = structure.name
    for suffix in (f".{DEFAULT_LANG}.json", ".json"):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    return structure.with_name(f"{stem}.{lang}.json")


@lru_cache(maxsize=8)
def _load_structure(path: str) -> LightEntries:
    return LightEntries.model_validate_json(Path(path).read_text(encoding="utf-8"))


@lru_cache(maxsize=8)
def _load_copy(path: str) -> EntriesCopy | None:
    file = Path(path)
    if not file.is_file():
        return None
    return EntriesCopy.model_validate_json(file.read_text(encoding="utf-8"))


@lru_cache(maxsize=8)
def _load(path: str, lang: str) -> LightEntries:
    structure = _load_structure(path)
    if lang == DEFAULT_LANG:
        return structure
    # lowest priority first: en-US under vi-VN, so a string missing from vi comes out English
    copies = [copy for copy in (_load_copy(str(copy_file(item, Path(path)))) for item in reversed(language_chain(lang)))
              if copy is not None]
    if not copies:
        return structure
    return LightEntries.model_validate(_apply_copy(structure.model_dump(mode="json"), copies))


def light_entries(lang: str | None = None) -> LightEntries:
    """The home in one interface language; structure (ids, skills, media, requirements) is the same in every one."""
    return _load(str(structure_file()), lang if lang in LANGS else DEFAULT_LANG)
