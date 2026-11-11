"""The example library's classification: entry → industry → shop type.

A JSON file in the repository so the console's drop-downs and the matcher in
``light/examples.py`` read one dictionary. Industries are the four of the
first-video form (``first_video_templates.json``); each has a short list of
common shop types plus ``other``. Keywords let the matcher guess a shop type
from a shop name or headline when nobody picked one.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

ID = r"^[a-z][a-z0-9_]*$"


class TaxonomyEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=32, pattern=ID)
    label: str = Field(min_length=1, max_length=20)


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


@lru_cache(maxsize=4)
def _load(path: str) -> Taxonomy:
    return Taxonomy.model_validate_json(Path(path).read_text(encoding="utf-8"))


def taxonomy() -> Taxonomy:
    return _load(os.environ.get("LIGHT_TAXONOMY_FILE") or str(Path(__file__).with_name("taxonomy.json")))
