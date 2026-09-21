"""Ceilings cover each real ledger price category, not a fixed token estimate."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from billing.pricing import quote
from team.errors import TeamError
from team.model_limits import model_bound


@pytest.fixture
def bounded_model(monkeypatch):
    from core import config
    entry = SimpleNamespace(id="fixture/model", context_limit=300000)
    monkeypatch.setattr(config, "get_config", lambda: SimpleNamespace(models=[entry]))
    rates = {"version": "test", "verified_at": "2026-09-21", "credits_per_cny": "1", "usd_cny": "7",
        "sources": {"fixture": "https://fixture.invalid"}, "aliases": {},
        "models": {"model": {"vendor": "fixture", "currency": "USD", "input": "1", "output": "2",
            "cache_read": "0.5", "cache_write": "3", "cache_write_1h": "4", "cache_read_explicit": "0.2",
            "long_context_above": 200000, "peak_multiplier": "2"}}}
    return entry, rates


def test_every_cache_bucket_long_context_fx_and_peak_is_covered(bounded_model):
    _, rates = bounded_model
    bound = model_bound("fixture/model", output_tokens=5000, rates=rates)
    # Entire input charged at the highest cache tier; long-context and peak
    # tariffs apply to input and output, using the existing credit conversion.
    assert bound.credits == Decimal("33.81")
    for hour in (0, 3):  # Shanghai off-peak and weekday peak
        for inp in (1, 200000, 200001, 300000):
            for bucket in (None, "cache_read", "cache_write", "cache_write_1h", "cache_read_explicit"):
                usage = {"input": inp, "output": 5000}
                if bucket:
                    usage[bucket] = inp
                    if bucket == "cache_write_1h":
                        usage["cache_write"] = inp
                    if bucket == "cache_read_explicit":
                        usage["cache_read"] = inp
                price = quote("fixture/model", usage, rates=rates, at=datetime(2026, 9, 21, hour, tzinfo=timezone.utc))
                assert price.credits <= bound.credits


@pytest.mark.parametrize("limit", [None, 0, -1, True, 10000001])
def test_unknown_or_invalid_provider_context_is_not_a_cost_bound(bounded_model, limit):
    entry, rates = bounded_model
    entry.context_limit = limit
    with pytest.raises(TeamError, match="context_limit"):
        model_bound("fixture/model", output_tokens=5000, rates=rates)


def test_unknown_price_and_unbounded_output_are_rejected(bounded_model):
    _, rates = bounded_model
    for output in (0, -1, True, 300000):
        with pytest.raises(TeamError, match="output cap"):
            model_bound("fixture/model", output_tokens=output, rates=rates)
    rates["models"] = {}
    with pytest.raises(TeamError, match="tariff"):
        model_bound("fixture/model", output_tokens=5000, rates=rates)
