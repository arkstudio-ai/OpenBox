"""The example library's classification: entry → industry → shop type.

A JSON file in the repository so the console's drop-downs and the matcher in
``light/examples.py`` read one dictionary. Industries are the four of the
first-video form (``first_video_templates.json``); each has a short list of
common shop types plus ``other``. Keywords let the matcher guess a shop type
from a shop name or headline when nobody picked one.

The file is what ships. The content team edits the scenes, industries and shop
types in the console (``light_taxonomy``, one row), and ``taxonomy()`` is the
file with that edit laid over it. What the code depends on stays: the entries,
every scene and industry the file names (the App's chips and the first-video
form point at them), and each industry's ``other`` shop type.
"""
from __future__ import annotations

import os
import time
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ID = r"^[a-z][a-z0-9_]*$"


class Scene(BaseModel):
    """What a piece is made for: a poster's purpose, a cut's type, a menu's style."""
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=32, pattern=ID)
    label: str = Field(min_length=1, max_length=20)


class TaxonomyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=32, pattern=ID)
    label: str = Field(min_length=1, max_length=20)
    #: The types the entry's opening step offers; an example carries one (``light_examples.scene``).
    scenes: list[Scene] = Field(default_factory=list)
    #: What a finished piece of this entry is: an example needs one of these to count for the entry.
    media: Literal["image", "video"] = "image"
    #: The opening card's chips are the scenes, in order (a poster's 用途, a cut's 成片类型).
    chips_are_scenes: bool = False

    @model_validator(mode="after")
    def _unique_scenes(self) -> "TaxonomyEntry":
        ids = [scene.id for scene in self.scenes]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate scene in {self.id}")
        return self


class ShopType(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=32, pattern=ID)
    label: str = Field(min_length=1, max_length=20)
    keywords: list[str] = Field(default_factory=list, max_length=16)


class Industry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=32, pattern=ID)
    label: str = Field(min_length=1, max_length=20)
    shop_types: list[ShopType] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> "Industry":
        ids = [shop.id for shop in self.shop_types]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate shop type in {self.id}")
        return self

    def shop_type(self, shop_type_id: str) -> ShopType | None:
        return next((shop for shop in self.shop_types if shop.id == shop_type_id), None)


class Taxonomy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1, max_length=64)
    entries: list[TaxonomyEntry] = Field(min_length=1)
    industries: list[Industry] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> "Taxonomy":
        for ids in ([e.id for e in self.entries], [i.id for i in self.industries]):
            if len(set(ids)) != len(ids):
                raise ValueError("duplicate id")
        return self

    def entry_ids(self) -> list[str]:
        return [entry.id for entry in self.entries]

    def entry(self, entry_id: str | None) -> TaxonomyEntry | None:
        return next((item for item in self.entries if item.id == entry_id), None)

    def media(self, entry_id: str | None) -> str | None:
        """``image`` or ``video`` for a known entry; None for one the taxonomy does not list."""
        entry = self.entry(entry_id)
        return entry.media if entry else None

    def scene_ids(self, entry_id: str | None) -> list[str]:
        entry = next((item for item in self.entries if item.id == entry_id), None)
        return [scene.id for scene in entry.scenes] if entry else []

    def scene_label(self, entry_id: str | None, scene_id: str | None) -> str:
        entry = next((item for item in self.entries if item.id == entry_id), None)
        scene = next((item for item in (entry.scenes if entry else []) if item.id == scene_id), None)
        return scene.label if scene else (scene_id or "")

    def industry(self, industry_id: str | None) -> Industry | None:
        return next((item for item in self.industries if item.id == industry_id), None)

    def industry_ids(self) -> list[str]:
        return [item.id for item in self.industries]

    def shop_type_ids(self, industry_id: str) -> list[str]:
        industry = self.industry(industry_id)
        return [shop.id for shop in industry.shop_types] if industry else []

    def label(self, industry_id: str | None, shop_type_id: str | None = None) -> str:
        industry = self.industry(industry_id)
        if industry is None:
            return industry_id or ""
        if not shop_type_id:
            return industry.label
        shop = industry.shop_type(shop_type_id)
        return f"{industry.label} / {shop.label if shop else shop_type_id}"

    def guess_shop_type(self, industry_id: str | None, *texts: str | None) -> str | None:
        """The shop type whose keyword appears in a shop name or headline; None when none does."""
        industry = self.industry(industry_id)
        if industry is None:
            return None
        haystack = " ".join(t for t in texts if t)
        if not haystack:
            return None
        best: tuple[int, str] | None = None
        for shop in industry.shop_types:
            for keyword in shop.keywords:
                if keyword and keyword in haystack and (best is None or len(keyword) > best[0]):
                    best = (len(keyword), shop.id)
        return best[1] if best else None

    def public(self) -> dict:
        return self.model_dump()


MAX_SCENES = 24
MAX_INDUSTRIES = 24
MAX_SHOP_TYPES = 40
#: The shop type every industry keeps: where an example goes when nobody named one.
GENERIC = "other"
ROW_ID = "default"
#: How long a process trusts its copy before reading the row again (another worker may have saved).
FRESH_SECONDS = 30.0


class Custom(BaseModel):
    """What the console saved: an entry's scenes in full, and the industries in full."""
    model_config = ConfigDict(extra="ignore")
    scenes: dict[str, list[Scene]] = Field(default_factory=dict)
    industries: list[Industry] | None = None


class TaxonomyInvalid(ValueError):
    """An edit the console cannot save; the message says which rule it broke."""


@lru_cache(maxsize=4)
def _load(path: str) -> Taxonomy:
    return Taxonomy.model_validate_json(Path(path).read_text(encoding="utf-8"))


def base() -> Taxonomy:
    """The taxonomy as it ships, without the console's edit."""
    return _load(os.environ.get("LIGHT_TAXONOMY_FILE") or str(Path(__file__).with_name("taxonomy.json")))


def new_id() -> str:
    """An id for something the console added; the team types a label, not a slug."""
    return "c_" + uuid4().hex[:10]


def merge(shipped: Taxonomy, custom: Custom | None, stamp: str = "") -> Taxonomy:
    """``shipped`` with the console's edit over it.

    A scene or industry the file names and the edit lacks (the file grew after
    the edit was saved) is put back at the end; so is a missing ``other``.
    """
    if custom is None:
        return shipped
    entries = []
    for entry in shipped.entries:
        scenes = custom.scenes.get(entry.id)
        if scenes is None:
            entries.append(entry)
            continue
        seen = {scene.id for scene in scenes}
        entries.append(entry.model_copy(update={
            "scenes": list(scenes) + [scene for scene in entry.scenes if scene.id not in seen]}))
    industries = shipped.industries
    if custom.industries is not None:
        seen = {industry.id for industry in custom.industries}
        industries = list(custom.industries) + [i for i in shipped.industries if i.id not in seen]
        industries = [i if i.shop_type(GENERIC) else i.model_copy(update={
            "shop_types": i.shop_types + [ShopType(id=GENERIC, label="其他")]}) for i in industries]
    version = f"{shipped.version}+{stamp}"[:64] if stamp else shipped.version
    return Taxonomy(version=version, entries=entries, industries=industries)


def check(shipped: Taxonomy, custom: Custom) -> None:
    """Raise ``TaxonomyInvalid`` for an edit that would break what the code depends on."""
    for entry_id, scenes in custom.scenes.items():
        entry = shipped.entry(entry_id)
        if entry is None:
            raise TaxonomyInvalid(f"unknown entry {entry_id}")
        ids = [scene.id for scene in scenes]
        if len(set(ids)) != len(ids) or len({scene.label for scene in scenes}) != len(scenes):
            raise TaxonomyInvalid(f"duplicate scene in {entry.label}")
        if len(scenes) > MAX_SCENES:
            raise TaxonomyInvalid(f"at most {MAX_SCENES} scenes per entry")
        lost = [scene.label for scene in entry.scenes if scene.id not in ids]
        if lost:
            raise TaxonomyInvalid(f"built-in scene cannot be removed: {'、'.join(lost)}")
    if custom.industries is None:
        return
    ids = [industry.id for industry in custom.industries]
    if len(set(ids)) != len(ids) or len({i.label for i in custom.industries}) != len(ids):
        raise TaxonomyInvalid("duplicate industry")
    if len(ids) > MAX_INDUSTRIES:
        raise TaxonomyInvalid(f"at most {MAX_INDUSTRIES} industries")
    lost = [industry.label for industry in shipped.industries if industry.id not in ids]
    if lost:
        raise TaxonomyInvalid(f"built-in industry cannot be removed: {'、'.join(lost)}")
    for industry in custom.industries:
        if len(industry.shop_types) > MAX_SHOP_TYPES:
            raise TaxonomyInvalid(f"at most {MAX_SHOP_TYPES} shop types per industry")
        if len({shop.label for shop in industry.shop_types}) != len(industry.shop_types):
            raise TaxonomyInvalid(f"duplicate shop type in {industry.label}")
        if industry.shop_type(GENERIC) is None:
            raise TaxonomyInvalid(f"{industry.label} needs its generic shop type")


def builtin(shipped: Taxonomy | None = None) -> dict:
    """The ids the console may rename but not remove."""
    shipped = shipped or base()
    return {"scenes": {entry.id: [scene.id for scene in entry.scenes] for entry in shipped.entries},
            "industries": shipped.industry_ids(), "shop_types": [GENERIC]}


#: (the file the copy was merged over, the merged taxonomy, when it was read).
_current: tuple[Taxonomy, Taxonomy, float] | None = None


def taxonomy() -> Taxonomy:
    """The taxonomy in force: the file with the console's edit, as last read by ``refresh``."""
    shipped = base()
    if _current is not None and _current[0] is shipped:
        return _current[1]
    return shipped


def forget() -> None:
    """Drop this process's copy of the console's edit (tests; the next ``refresh`` reads it again)."""
    global _current
    _current = None


async def stored() -> tuple[Custom | None, str]:
    """The console's edit and a stamp of when it was saved; ``(None, "")`` when nobody edited."""
    from db.base import get_db_session
    from db.models.light_taxonomy import LightTaxonomy
    async with get_db_session() as db:
        row = await db.get(LightTaxonomy, ROW_ID)
    if row is None or not isinstance(row.data, dict):
        return None, ""
    return Custom.model_validate(row.data), row.updated_at.strftime("%Y%m%d%H%M%S") if row.updated_at else ""


async def refresh(*, force: bool = False) -> Taxonomy:
    """Read the console's edit when this process's copy is older than ``FRESH_SECONDS``; never raises."""
    global _current
    shipped = base()
    if not force and _current is not None and _current[0] is shipped and time.monotonic() - _current[2] < FRESH_SECONDS:
        return _current[1]
    try:
        custom, stamp = await stored()
        _current = (shipped, merge(shipped, custom, stamp), time.monotonic())
    except Exception:  # a broken row or an unreachable database leaves the last good copy in force
        if _current is None or _current[0] is not shipped:
            return shipped
    return _current[1]


async def lock_edit(db) -> None:
    """Serialize category edits with submissions that reference a category."""
    from sqlalchemy import text
    if db.bind.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 0x4D45444941544158})


async def for_write(db) -> Taxonomy:
    """Read the current classification inside the writer transaction."""
    from db.models.light_taxonomy import LightTaxonomy
    row = await db.get(LightTaxonomy, ROW_ID)
    custom = Custom.model_validate(row.data) if row and isinstance(row.data, dict) else None
    return merge(base(), custom)
