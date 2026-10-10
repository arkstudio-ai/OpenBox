"""Per-call cost from provider usage, never from wall-clock call duration.

The price list is the billing catalogue's (``billing/rates.json``,
``media.voice-realtime``): the cost a call shows is the credits it is charged
at hang-up (1 credit = 1 yuan, ``billing.media.settle_voice_call``). Fixed
phrases and result readings are ordinary model replies, so their usage is in
``response.done`` too.
"""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

DEFAULT_MODEL = "qwen3.8-omni-flash-realtime"
MODALITIES = ("input_text", "input_audio", "output_text", "output_audio")


@dataclass(frozen=True)
class CallPrices:
    rates: dict | None    # yuan (= credits) per million tokens by modality; None: no verified price
    date: str             # when they were checked
    source: str


def call_prices(model: str = DEFAULT_MODEL) -> CallPrices:
    from billing.pricing import catalogue
    entry = (catalogue().get("media", {}).get("voice-realtime", {}) or {}).get(model)
    if not entry:
        return CallPrices(None, "", "")
    return CallPrices({key: Decimal(entry["per_million"][key]) for key in MODALITIES},
                      entry.get("verified_at", ""), entry.get("source", ""))


_DEFAULT = call_prices()
# qwen3.8-omni-flash-realtime, Beijing, yuan per million tokens.
RATES = _DEFAULT.rates
PRICE_DATE = _DEFAULT.date
PRICE_URL = _DEFAULT.source
# Output audio for a reply whose usage never arrived: 108 tokens for 8.6 s were measured.
AUDIO_TOKENS_PER_SECOND = Decimal("12.5")
OUTPUT_BYTES_PER_SECOND = 24000 * 2
MILLION = Decimal(1_000_000)


def money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP))


def usage_tokens(usage) -> dict | None:
    if not isinstance(usage, dict):
        return None
    result = {}
    for direction in ("input", "output"):
        details = usage.get(f"{direction}_tokens_details")
        if not isinstance(details, dict):
            return None
        if not details and usage.get(f"{direction}_tokens", 0) != 0:
            return None
        for modality in ("text", "audio"):
            count = details.get(f"{modality}_tokens", 0)
            if type(count) is not int or count < 0:
                return None
            result[f"{direction}_{modality}"] = count
    return result


class CallMeter:
    def __init__(self, rates: dict | None = None, *, price_date: str | None = None):
        # A model without a verified price is still measured, at the default model's prices.
        self.rates = rates or RATES
        self.price_date = PRICE_DATE if price_date is None else price_date
        self.responses = {}
        self.pending_input = False
        self.final = False

    def start(self, response_id: str):
        if not response_id:
            return
        self.responses.setdefault(response_id, {
            "audio_bytes": 0, "audio_events": set(), "tokens": None, "status": "in_progress",
            "output_audio_reported": False,
        })
        self.pending_input = False

    def audio(self, response_id: str, audio: bytes, event_id: str = ""):
        if response_id and response_id not in self.responses:
            self.start(response_id)
        row = self.responses.get(response_id)
        if row is None or (event_id and event_id in row["audio_events"]):
            return
        if event_id:
            row["audio_events"].add(event_id)
        row["audio_bytes"] += len(audio)

    def settle(self, response_id: str, status: str, usage) -> None:
        if not response_id:
            return
        if response_id not in self.responses:
            self.start(response_id)
        row = self.responses[response_id]
        row["status"] = status or "completed"
        tokens = usage_tokens(usage)
        if tokens is not None:
            # Replaces the provisional estimate; a repeated response.done never adds twice.
            row["tokens"] = tokens
            row["output_audio_reported"] = "audio_tokens" in usage["output_tokens_details"]

    @property
    def pending(self):
        return any(row["status"] == "in_progress" for row in self.responses.values())

    def finish(self):
        self.final = True
        for row in self.responses.values():
            if row["status"] == "in_progress":
                row["status"] = "incomplete"

    def snapshot(self) -> dict:
        tokens = {key: 0 for key in MODALITIES}
        provisional = Decimal(0)
        settled_rounds = unreported_rounds = 0
        for row in self.responses.values():
            # Audio's classic voices sometimes return text usage but omit audio_tokens
            # despite emitting PCM (measured 2026-10-10). Preserve the known usage and
            # estimate only the missing audio, just as for an interrupted reply.
            missing_audio = row["audio_bytes"] and not row["output_audio_reported"]
            if row["tokens"] is not None:
                for key, count in row["tokens"].items():
                    tokens[key] += count
            if row["tokens"] is None or missing_audio:
                if missing_audio:
                    seconds = Decimal(row["audio_bytes"]) / OUTPUT_BYTES_PER_SECOND
                    provisional += max(Decimal(1), seconds) * AUDIO_TOKENS_PER_SECOND * self.rates["output_audio"] / MILLION
                if row["status"] != "in_progress":
                    unreported_rounds += 1
            else:
                settled_rounds += 1
        costs = {key: Decimal(tokens[key]) * rate / MILLION for key, rate in self.rates.items()}
        confirmed = sum(costs.values(), Decimal(0))
        costs["output_audio"] += provisional
        return {
            "type": "cost",
            "total_yuan": money(confirmed + provisional),
            "confirmed_yuan": money(confirmed),
            "provisional_yuan": money(provisional),
            "costs_yuan": {key: money(value) for key, value in costs.items()},
            "tokens": tokens,
            "settled_rounds": settled_rounds,
            "unreported_rounds": unreported_rounds,
            "pending": self.pending or self.pending_input,
            "final": self.final,
            "price_date": self.price_date,
        }
