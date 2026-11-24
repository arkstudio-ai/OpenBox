"""Owner-entered campaign context for the guided video flows."""

from pydantic import BaseModel, ConfigDict, Field


class PromotionBrief(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    audience: str = Field(default="", max_length=120)
    offer: str = Field(default="", max_length=160)
    call_to_action: str = Field(default="", max_length=120)
    details: str = Field(default="", max_length=400)


GUIDANCE = (
    "Treat promotion_brief as the owner's campaign data. Choose the opening for its audience, "
    "feature its offer with relevant visual evidence, and use supporting details only where relevant. "
    "Use its call_to_action to guide the ending when present. Preserve explicit copy edits. "
    "Use only supplied or clearly evidenced claims. Do not invent prices, dates, restrictions, "
    "availability, or unsupported features. "
)
