"""Maximum token cost under the deployment's declared provider limits."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING

from billing.pricing import PRECISION, catalogue, quote
from team.errors import TeamError


@dataclass(frozen=True)
class ModelBound:
    input_tokens: int
    output_tokens: int
    credits: Decimal
    pricing: dict


def model_bound(model_id: str, *, output_tokens: int | None = None,
                variant: str | None = None, rates: dict | None = None) -> ModelBound:
    """Never infer a price or a provider context ceiling from a model name.

    context_limit is a deployment assertion about the provider's hard accepted
    input limit (including images/cache). It must match the configured gateway.
    Output is the same explicit cap passed to the wire adapter, including its
    thinking reserve. The bound is conditional on these provider contracts.
    """
    from agent.llm import request_output_tokens
    from core.config import get_config
    configured = next((entry for entry in get_config().models if entry.id == model_id), None)
    limit = getattr(configured, "context_limit", None)
    output = output_tokens if output_tokens is not None else request_output_tokens(model_id, variant)
    if type(limit) is not int or not 1 <= limit <= 10_000_000:
        raise TeamError("MODEL_BOUND_UNAVAILABLE", "Team models require an explicit provider context_limit in deployment configuration.", current={"model": model_id})
    if type(output) is not int or not 0 < output < limit:
        raise TeamError("MODEL_BOUND_UNAVAILABLE", "The model output cap must be positive and smaller than its context_limit.", current={"model": model_id, "output_tokens": output, "context_limit": limit})
    data = rates if rates is not None else catalogue()
    quoted = quote(model_id, {"input": limit, "output": output}, rates=data)
    prices = quoted.snapshot.get("credits_per_million", {})
    if quoted.credits is None or "input" not in prices or "output" not in prices:
        raise TeamError("MODEL_BOUND_UNAVAILABLE", "The selected model has no verified token tariff; no team request was sent.", current={"model": model_id})
    key = quoted.snapshot["model"]
    multiplier = Decimal(str(data["models"][key].get("peak_multiplier", "1")))
    if not multiplier.is_finite() or multiplier <= 0:
        raise TeamError("MODEL_BOUND_UNAVAILABLE", "The model tariff multiplier is invalid.")
    peak = max(Decimal(1), multiplier)
    if quoted.snapshot.get("peak"):
        peak = max(Decimal(1), 1 / multiplier)
    input_rate = max(Decimal(value) for category, value in prices.items() if category != "output")
    credits = ((input_rate * limit + Decimal(prices["output"]) * output) * peak / Decimal(1_000_000))
    return ModelBound(limit, output, credits.quantize(PRECISION, rounding=ROUND_CEILING), quoted.snapshot)
