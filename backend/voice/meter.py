"""Per-call cost from provider usage, never from wall-clock call duration.

Copied from demos/realtime-voice/pricing.py (the demo stays independent) with
the qwen3.8-omni-flash-realtime price list. Fixed phrases and result readings
are ordinary model replies, so their usage is in ``response.done`` too.
"""
from decimal import Decimal, ROUND_HALF_UP

# qwen3.8-omni-flash-realtime, Beijing, yuan per million tokens (checked 2026-10-07).
PRICE_DATE = "2026-10-07"
PRICE_URL = "https://help.aliyun.com/zh/model-studio/model-pricing"
RATES = {"input_text": Decimal("1.5"), "input_audio": Decimal(6),
         "output_text": Decimal("4.5"), "output_audio": Decimal(12)}
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
    def __init__(self):
        self.responses = {}
        self.pending_input = False
        self.final = False

    def start(self, response_id: str):
        if not response_id:
            return
        self.responses.setdefault(response_id, {
            "audio_bytes": 0, "audio_events": set(), "tokens": None, "status": "in_progress",
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

    @property
    def pending(self):
        return any(row["status"] == "in_progress" for row in self.responses.values())

    def finish(self):
        self.final = True
        for row in self.responses.values():
            if row["status"] == "in_progress":
                row["status"] = "incomplete"

    def snapshot(self) -> dict:
        tokens = {key: 0 for key in RATES}
        provisional = Decimal(0)
        settled_rounds = unreported_rounds = 0
        for row in self.responses.values():
            if row["tokens"] is not None:
                settled_rounds += 1
                for key, count in row["tokens"].items():
                    tokens[key] += count
            else:
                if row["audio_bytes"]:
                    seconds = Decimal(row["audio_bytes"]) / OUTPUT_BYTES_PER_SECOND
                    provisional += max(Decimal(1), seconds) * AUDIO_TOKENS_PER_SECOND * RATES["output_audio"] / MILLION
                if row["status"] != "in_progress":
                    unreported_rounds += 1
        costs = {key: Decimal(tokens[key]) * rate / MILLION for key, rate in RATES.items()}
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
            "price_date": PRICE_DATE,
        }
