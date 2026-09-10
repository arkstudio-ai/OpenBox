"""The one-time budget authorisation a person gives a marketing-autopilot cron job.

Replaces the per-step confirmation cards (plan §2.3): a cron run has nobody to
ask, so everything it may spend or decide is fixed here when the job is
created and only read at run time. Validation is strict (`extra=forbid`) so a
typo in a field name is a rejected job, not a silently ignored limit.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TEMPLATE_KIND = "marketing_autopilot"
ModelTier = Literal["high", "medium", "low"]
PublishMode = Literal["auto", "package"]
Visibility = Literal["public", "friends", "private"]
#: Forms `video_analyze` can report (video/analysis.py Form) that a recipe exists for.
CONTENT_FORMS: tuple[str, ...] = ("口播", "画面+旁白", "产品展示", "剧情", "混剪", "字幕型")
HOT_SOURCES: tuple[str, ...] = ("auto", "douhot", "douyin_public")


class Tolerances(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Seconds a generated shot may deviate from its planned length.
    duration_deviation_sec: float = Field(default=2.0, ge=0, le=10)
    #: Transcript similarity a spoken shot must reach (口播 recipes only).
    stt_similarity: float = Field(default=0.85, ge=0.5, le=1.0)
    #: Regenerations allowed per shot before the video is dropped from the run.
    max_regenerations: int = Field(default=1, ge=0, le=2)


class AutopilotTemplate(BaseModel):
    """Stored verbatim in `cron_jobs.template`; read by the marketing-autopilot skill."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["marketing_autopilot"] = TEMPLATE_KIND
    version: Literal[1] = 1
    #: Who the account is / what it sells — steers topic filtering and copy.
    account_profile: str = Field(default="", max_length=500)
    #: Hot-list categories to follow (热点宝 taxonomy labels); empty = whole board.
    categories: list[str] = Field(default_factory=list, max_length=10)
    hot_source: Literal["auto", "douhot", "douyin_public"] = "auto"
    credits_cap_per_run: Decimal = Field(gt=0, le=Decimal("2000"))
    videos_per_run: int = Field(default=1, ge=1, le=5)
    model_tier: ModelTier = "medium"
    tolerances: Tolerances = Field(default_factory=Tolerances)
    publish_mode: PublishMode = "auto"
    visibility: Visibility = "public"
    content_forms: list[str] = Field(default_factory=lambda: list(CONTENT_FORMS), min_length=1, max_length=len(CONTENT_FORMS))
    topics_blocklist: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("content_forms")
    @classmethod
    def _known_forms(cls, forms: list[str]) -> list[str]:
        bad = [f for f in forms if f not in CONTENT_FORMS]
        if bad:
            raise ValueError(f"unknown content form(s) {bad}; allowed: {', '.join(CONTENT_FORMS)}")
        return list(dict.fromkeys(forms))

    @field_validator("topics_blocklist", "categories")
    @classmethod
    def _short_words(cls, words: list[str]) -> list[str]:
        cleaned = [w.strip() for w in words if w and w.strip()]
        if any(len(w) > 40 for w in cleaned):
            raise ValueError("each word must be 40 characters or fewer")
        return list(dict.fromkeys(cleaned))

    @model_validator(mode="after")
    def _budget_can_buy_one_video(self) -> "AutopilotTemplate":
        from autopilot.tiers import minimum_credits_per_video

        floor = minimum_credits_per_video(self.model_tier)
        if self.credits_cap_per_run < floor:
            raise ValueError(
                f"credits_cap_per_run {self.credits_cap_per_run} cannot pay for one {self.model_tier}-tier video "
                f"(about {floor} credits with analysis and composition); raise the cap or lower the tier"
            )
        return self

    def public(self) -> dict:
        data = self.model_dump(mode="json")
        data["credits_cap_per_run"] = str(self.credits_cap_per_run)
        return data


def parse_template(value: dict | None) -> AutopilotTemplate | None:
    if value is None:
        return None
    return AutopilotTemplate.model_validate(value)
