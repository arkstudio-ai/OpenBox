"""Commercial terms copied from bossip/apps/center/src/billing/plans.config.yaml."""
import os
from functools import lru_cache
from pathlib import Path
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Literal["free", "pro", "max"]
    prices_fen: dict[Literal["monthly", "yearly"], int]
    credits: Decimal = Field(gt=0)
    credit_period: Literal["weekly", "monthly"]
    recommended: bool = False
    #: Welcome credits posted once when a workspace is created (billing/signup_gift.py);
    #: 0 disables the gift. Only the free plan is read.
    signup_gift_credits: Decimal = Field(default=Decimal("0"), ge=0)

    @model_validator(mode="after")
    def validate_prices(self):
        if set(self.prices_fen) != {"monthly", "yearly"}:
            raise ValueError("Both billing cycles require prices")
        if any(price < 0 or price > 100_000_000 for price in self.prices_fen.values()):
            raise ValueError("Invalid plan price")
        if self.id == "free" and any(self.prices_fen.values()):
            raise ValueError("The free plan must be free")
        if self.id != "free" and any(price == 0 for price in self.prices_fen.values()):
            raise ValueError("Paid plans require a positive price")
        return self


class TopupRules(BaseModel):
    min_amount_fen: int = Field(ge=100)
    max_amount_fen: int = Field(le=100_000_000)
    presets_fen: list[int]
    credits_per_yuan: Literal["1"] = "1"

    @model_validator(mode="after")
    def validate_range(self):
        if self.min_amount_fen > self.max_amount_fen or any(
            not self.min_amount_fen <= value <= self.max_amount_fen for value in self.presets_fen
        ):
            raise ValueError("Invalid top-up range or presets")
        return self


class PlanCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str
    currency: Literal["CNY"]
    plans: list[Plan]
    topup: TopupRules

    @model_validator(mode="after")
    def validate_plans(self):
        if sorted(plan.id for plan in self.plans) != ["free", "max", "pro"]:
            raise ValueError("Exactly one free, pro and max plan is required")
        return self

    def plan(self, plan_id: str) -> Plan:
        return next(plan for plan in self.plans if plan.id == plan_id)


@lru_cache(maxsize=8)
def _load(path: str) -> PlanCatalog:
    return PlanCatalog.model_validate_json(Path(path).read_text())


def plan_catalog() -> PlanCatalog:
    return _load(os.environ.get("BILLING_PLANS_FILE") or str(Path(__file__).with_name("plans.json")))
