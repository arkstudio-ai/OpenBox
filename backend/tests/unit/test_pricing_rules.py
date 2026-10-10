"""Operator rules lay over rates.json without changing what any caller reads; costs ride along."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from billing import media, rules
from billing.pricing import base_catalogue, catalogue, normalize_usage, quote
from billing.rules import ItemKey, RuleError, RuleView


def _rule(key, *, sale=None, cost=None, status="active", valid_until=None, rid="pricing_01TEST", revision=1):
    return RuleView(id=rid, key=key, revision=revision, status=status, sale=sale, cost=cost, valid_from=None,
                    valid_until=valid_until, reason="test", actor_user_id="admin", audit_id=None,
                    created_at=datetime(2026, 10, 10, tzinfo=timezone.utc))


@pytest.fixture(autouse=True)
def no_cached_rules():
    rules.clear()
    yield
    rules.clear()


def test_keys_parse_and_map_usage_rows_back_to_items():
    assert ItemKey.parse("llm:gemini-3.8-flash") == ItemKey("llm", "gemini-3.8-flash")
    assert ItemKey.parse("video-gen:MiniMax-H3:768p") == ItemKey("video-gen", "MiniMax-H3", "768p")
    assert ItemKey.parse("ims-compose:720p").key == "ims-compose:720p"
    for bad in ("", "llm", "video-gen:x", "llm:a:b", "unknown:x"):
        with pytest.raises(RuleError):
            ItemKey.parse(bad)
    assert ItemKey.for_usage("chat", "openai/gemini-3.8-flash").key == "llm:gemini-3.8-flash"
    assert ItemKey.for_usage("video_generate", "video-gen:wan3.0-video:720p").key == "video-gen:wan3.0-video:720p"
    assert ItemKey.for_usage("video_compose", "ims-compose-720p").key == "ims-compose:720p"
    assert ItemKey.for_usage("voice_call", "qwen3.8-omni-flash-realtime").key == "voice-realtime:qwen3.8-omni-flash-realtime"
    assert ItemKey.for_usage("hot_trends", "hot-trends:douyin_public").key == "hot-trends:douyin_public"


def test_without_rules_the_catalogue_is_the_base_file_itself():
    assert catalogue() is base_catalogue()
    assert quote("openai/gemini-3.8-flash", normalize_usage({"input": 1_000_000})).credits == Decimal("10.2")


def test_llm_rule_replaces_the_price_in_cny_and_detaches_the_alias():
    # gpt-5.6 is an alias of gpt-5.6-sol's USD price; pricing it directly ends that.
    rule = _rule("llm:gpt-5.6", sale={"input": "8", "output": "40", "cache_read": "1"})
    data = rules.overlay(base_catalogue(), [rule])
    assert "gpt-5.6" not in data["aliases"]
    assert data["version"].startswith(base_catalogue()["version"] + "+db")
    q = quote("openai/gpt-5.6", normalize_usage({"input": 1_000_000, "output": 1000}), rates=data)
    assert q.credits == Decimal("8.04") and q.snapshot["rule_id"] == rule.id
    assert q.snapshot["currency"] == "CNY" and "long_context_above" not in data["models"]["gpt-5.6"]
    # The base object was not touched.
    assert "gpt-5.6" in base_catalogue()["aliases"]
    assert base_catalogue().get("rules_applied") is None


def test_llm_quote_reports_the_cost_at_the_recorded_basis():
    q = quote("openai/gemini-3.8-flash", normalize_usage({"input": 1_000_000, "output": 100_000, "cache_read": 500_000}))
    # RovinAI basis: 0.5M input @6 + 0.5M cache @0.7 + 0.1M output @30 = 3 + 0.35 + 3 = 6.35
    assert q.cost == Decimal("6.35") and q.snapshot["cost"]["basis"] == "rovinai"
    assert q.snapshot["cost_credits"] == "6.350000000000"
    # T1 sale: 0.5M @10.2 + 0.5M @1.19 + 0.1M @51 = 5.1 + 0.595 + 5.1 = 10.795 — 70% over cost.
    assert q.credits == Decimal("10.795") and q.credits > q.cost
    none = quote("openai/claude-opus-5", normalize_usage({"input": 1000}))
    assert none.credits is not None and none.cost is None and "cost" not in none.snapshot


def test_video_rule_touches_one_resolution_and_disabling_removes_it():
    priced = _rule("video-gen:MiniMax-H3:768p", sale={"per_second": "0.40"}, rid="pricing_01A")
    data = rules.overlay(base_catalogue(), [priced])
    assert media.quote_generation("MiniMax-H3", "768p", 10, rates=data).credits == Decimal("4.00")
    assert media.quote_generation("MiniMax-H3", "768p", 10, rates=data).snapshot["rule_id"] == "pricing_01A"
    assert media.quote_generation("MiniMax-H3", "512p", 10, rates=data).credits == Decimal("1.70")  # untouched
    # Cost still comes from the base cost block when the rule carries none.
    assert media.quote_generation("MiniMax-H3", "768p", 10, rates=data).cost == Decimal("0.90")
    off = _rule("video-gen:MiniMax-H3:768p", status="disabled", rid="pricing_01B", revision=2)
    data = rules.overlay(base_catalogue(), [off])
    assert media.quote_generation("MiniMax-H3", "768p", 10, rates=data).credits is None
    assert media.quote_generation("MiniMax-H3", "512p", 10, rates=data).credits == Decimal("1.70")


def test_a_rule_can_price_an_item_the_base_file_never_had():
    rule = _rule("video-gen:brand-new-model:720p", sale={"per_second": "1.25"},
                 cost={"per_second": "1", "currency": "CNY", "basis": "vendor"})
    data = rules.overlay(base_catalogue(), [rule])
    q = media.quote_generation("brand-new-model", "720p", 4, rates=data)
    assert (q.credits, q.cost) == (Decimal("5.00"), Decimal("4.00"))
    llm = _rule("llm:brand-new-llm", sale={"input": "1", "output": "2"}, rid="pricing_01C")
    data = rules.overlay(base_catalogue(), [llm])
    assert quote("x/brand-new-llm", normalize_usage({"input": 1_000_000}), rates=data).credits == Decimal("1")


def test_expired_and_future_rules_do_not_apply():
    at = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
    expired = _rule("video-gen:wan3.0-video:720p", sale={"per_second": "9"}, valid_until=at - timedelta(seconds=1))
    live = _rule("video-gen:wan3.0-video:720p", sale={"per_second": "9"}, valid_until=at + timedelta(days=1))
    assert rules.overlay(base_catalogue(), [expired], at=at) is base_catalogue() or \
        media.quote_generation("wan3.0-video", "720p", 1, rates=rules.overlay(base_catalogue(), [expired], at=at)).credits == Decimal("1.40")
    assert media.quote_generation("wan3.0-video", "720p", 1, rates=rules.overlay(base_catalogue(), [live], at=at)).credits == Decimal("9")


def test_usd_cost_is_converted_at_the_table_rate():
    rule = _rule("llm:qwen3.8-flash", sale={"input": "0.8", "output": "2.7"},
                 cost={"input": "0.1", "output": "0.3", "currency": "USD", "basis": "test"})
    data = rules.overlay(base_catalogue(), [rule])
    q = quote("qwen3.8-flash", normalize_usage({"input": 1_000_000}), rates=data)
    assert q.cost == (Decimal("0.1") * Decimal(data["usd_cny"])).quantize(Decimal("0.000000000001"))
    assert q.snapshot["cost"]["currency"] == "USD"


def test_fragment_validation_rejects_floats_negatives_and_wrong_shapes():
    item = ItemKey.parse("video-gen:MiniMax-H3:768p")
    assert rules.validate_fragment(item, {"per_second": "0.40"}, side="sale") == {"per_second": "0.4"}
    with pytest.raises(RuleError):
        rules.validate_fragment(item, {"per_second": 0.4}, side="sale")
    with pytest.raises(RuleError):
        rules.validate_fragment(item, {"per_second": "-1"}, side="sale")
    with pytest.raises(RuleError):
        rules.validate_fragment(ItemKey.parse("llm:x"), {"input": "1"}, side="sale")  # output missing
    with pytest.raises(RuleError):
        rules.validate_fragment(ItemKey.parse("llm:x"), {"input": "1", "output": "2", "duration_bands": []}, side="cost")
    voice = rules.validate_fragment(ItemKey.parse("voice-realtime:m"), {"per_million": {
        "input_text": "1", "input_audio": "2", "output_text": "3", "output_audio": "4"}}, side="sale")
    assert voice["per_million"]["output_audio"] == "4"


def test_item_sale_and_cost_read_the_effective_fragment():
    base = base_catalogue()
    assert rules.item_sale(base, ItemKey.parse("video-gen:wan3.0-video:720p")) == {"per_second": "1.40"}
    assert rules.item_cost(base, ItemKey.parse("video-gen:MiniMax-H3:2k"))["per_second"] == "0.15"
    assert rules.item_sale(base, ItemKey.parse("video-gen:wan3.0-video:2k")) is None
    assert rules.item_sale(base, ItemKey.parse("llm:gemini-3.8-flash"))["input"] == "10.2"
    assert rules.item_cost(base, ItemKey.parse("llm:gpt-5.6-luna")) is None


async def test_refresh_reads_rules_from_the_database_and_catalogue_follows():
    from db.base import get_db_session
    from db.models.billing import PricingRule

    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(PricingRule(id="pricing_01DBRULE", key="video-gen:wan3.0-video:1080p", revision=1, status="active",
                           sale={"per_second": "0.5"}, cost=None, reason="t", actor_user_id="a", created_at=now))
        db.add(PricingRule(id="pricing_01OLD", key="video-gen:wan3.0-video:1080p", revision=0, status="active",
                           sale={"per_second": "7"}, cost=None, reason="t", actor_user_id="a", created_at=now,
                           superseded_at=now))
    loaded = {r.id for r in await rules.refresh()}
    # Superseded rows stay in the table for history but never load; other tests' live rules may.
    assert "pricing_01DBRULE" in loaded and "pricing_01OLD" not in loaded
    assert media.quote_generation("wan3.0-video", "1080p", 2).credits == Decimal("1.0")
    assert "pricing_01DBRULE" in catalogue()["rules_applied"]
