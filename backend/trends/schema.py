"""Normalised hot-list items, the same shape whatever the source."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class HotItem(BaseModel):
    model_config = {"extra": "forbid"}

    #: video | topic | search
    kind: Literal["video", "topic", "search"]
    #: Platform id: aweme id, challenge id, or the search phrase itself.
    id: str
    title: str = ""
    #: Public page for a person to open (video page, topic page, search page).
    url: str | None = None
    author: str | None = None
    author_followers: int | None = None
    duration_sec: float | None = None
    published_at: str | None = None  # ISO-8601 or the source's own relative text
    play_count: int | None = None
    like_count: int | None = None
    #: Source-specific heat / ranking score, not comparable across sources.
    heat: float | None = None
    rank: int | None = None
    topics: list[str] = Field(default_factory=list)
    cover_url: str | None = None
    #: Direct media URL when the source hands one out (热点宝 does). Expires.
    media_url: str | None = None
    extra: dict = Field(default_factory=dict)


def topics_from_title(title: str) -> list[str]:
    """`#话题` tags embedded in a douyin title."""
    out: list[str] = []
    for part in title.replace("＃", "#").split("#")[1:]:
        tag = part.strip().split(" ")[0].strip()
        if tag and tag not in out:
            out.append(tag[:40])
    return out


def parse_cn_number(text: str | None) -> int | None:
    """`3.9万` → 39000, `1,922万` → 19220000, `2407` → 2407, `1.2亿` → 120000000."""
    if text is None:
        return None
    raw = str(text).strip().replace(",", "").replace("，", "")
    if not raw or raw == "--":
        return None
    mult = 1
    if raw.endswith("亿"):
        mult, raw = 100_000_000, raw[:-1]
    elif raw.endswith("万") or raw.lower().endswith("w"):
        mult, raw = 10_000, raw[:-1]
    try:
        return int(round(float(raw) * mult))
    except ValueError:
        return None
