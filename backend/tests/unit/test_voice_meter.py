"""Call cost from provider usage at the qwen3.8-omni-flash-realtime Beijing prices."""
from decimal import Decimal

from voice.meter import PRICE_DATE, RATES, CallMeter, usage_tokens
from voice.meter import call_prices
from voice.models import AUDIO_MODEL

USAGE = {"input_tokens": 1230, "output_tokens": 128,
         "input_tokens_details": {"text_tokens": 1195, "audio_tokens": 35},
         "output_tokens_details": {"text_tokens": 20, "audio_tokens": 108}}


def test_prices_are_the_omni_flash_list():
    assert RATES == {"input_text": Decimal("1.5"), "input_audio": Decimal(6),
                     "output_text": Decimal("4.5"), "output_audio": Decimal(12)}
    assert PRICE_DATE == "2026-10-07"


def test_settled_usage_is_priced_per_modality():
    meter = CallMeter()
    meter.start("r1")
    meter.settle("r1", "completed", USAGE)
    snapshot = meter.snapshot()
    # 1195*1.5 + 35*6 + 20*4.5 + 108*12 = 1792.5 + 210 + 90 + 1296 = 3388.5 per million
    assert snapshot["total_yuan"] == snapshot["confirmed_yuan"] == "0.003389"
    assert snapshot["costs_yuan"] == {"input_text": "0.001793", "input_audio": "0.000210",
                                      "output_text": "0.000090", "output_audio": "0.001296"}
    assert snapshot["tokens"] == {"input_text": 1195, "input_audio": 35, "output_text": 20, "output_audio": 108}
    assert snapshot["settled_rounds"] == 1 and snapshot["unreported_rounds"] == 0
    assert snapshot["type"] == "cost" and not snapshot["pending"] and not snapshot["final"]


def test_a_repeated_done_never_counts_twice_and_replaces_the_estimate():
    meter = CallMeter()
    meter.audio("r1", b"\x00" * 48000, "e1")
    meter.audio("r1", b"\x00" * 48000, "e1")  # the same delta again
    assert meter.responses["r1"]["audio_bytes"] == 48000
    assert meter.snapshot()["provisional_yuan"] == "0.000150"  # 1 s -> 12.5 tokens at 12 yuan
    meter.settle("r1", "completed", USAGE)
    meter.settle("r1", "completed", USAGE)
    snapshot = meter.snapshot()
    assert snapshot["provisional_yuan"] == "0.000000" and snapshot["tokens"]["output_audio"] == 108


def test_interrupted_replies_without_usage_keep_an_estimate_and_are_flagged():
    meter = CallMeter()
    meter.start("r1")
    meter.audio("r1", b"\x00" * 96000)  # 2 s
    assert meter.snapshot()["pending"]
    meter.settle("r1", "cancelled", None)
    meter.start("r2")
    meter.finish()
    snapshot = meter.snapshot()
    assert snapshot["unreported_rounds"] == 2 and snapshot["final"] and not snapshot["pending"]
    assert snapshot["provisional_yuan"] == "0.000300"


def test_malformed_usage_is_not_trusted():
    assert usage_tokens(None) is None
    assert usage_tokens({"input_tokens_details": {"text_tokens": -1}, "output_tokens_details": {}}) is None
    assert usage_tokens({"input_tokens": 5, "input_tokens_details": {}, "output_tokens_details": {}}) is None
    assert usage_tokens({"input_tokens_details": {"text_tokens": "3"}, "output_tokens_details": {}}) is None


def test_audio_call_uses_its_own_rates_and_price_date_even_when_cancelled():
    from billing.media import quote_voice_call
    prices = call_prices(AUDIO_MODEL)
    meter = CallMeter(prices.rates, price_date=prices.date)
    meter.settle("answered", "completed", USAGE)
    assert meter.snapshot()["confirmed_yuan"] == "0.024375"
    meter.audio("interrupted", bytes(48000))
    meter.settle("interrupted", "cancelled", None)
    meter.finish()
    snapshot = meter.snapshot()
    assert snapshot["provisional_yuan"] == "0.001875"
    assert snapshot["price_date"] == "2026-10-10"
    quote = quote_voice_call(AUDIO_MODEL, snapshot, 10)
    assert quote.credits == Decimal("0.026250")


def test_classic_audio_voice_missing_audio_usage_keeps_text_and_estimates_only_audio():
    prices = call_prices(AUDIO_MODEL)
    meter = CallMeter(prices.rates, price_date=prices.date)
    meter.audio("classic", bytes(165120))  # 3.44 s actually received
    usage = {"input_tokens": 1235, "output_tokens": 13,
             "input_tokens_details": {"text_tokens": 1235}, "output_tokens_details": {"text_tokens": 12}}
    meter.settle("classic", "completed", usage)
    snapshot = meter.snapshot()
    assert snapshot["confirmed_yuan"] == "0.006655"
    assert snapshot["provisional_yuan"] == "0.006450"
    assert snapshot["total_yuan"] == "0.013105"
    assert snapshot["unreported_rounds"] == 1 and snapshot["settled_rounds"] == 0
    # A later authoritative usage replaces the estimate, including a genuine zero.
    usage["output_tokens_details"]["audio_tokens"] = 0
    meter.settle("classic", "completed", usage)
    assert meter.snapshot()["provisional_yuan"] == "0.000000"
    assert meter.snapshot()["settled_rounds"] == 1
