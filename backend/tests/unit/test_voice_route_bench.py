"""The route benchmark's cases and scoring (scripts/voice_route_bench.py); the models themselves are measured live."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "voice_route_bench.py"
spec = importlib.util.spec_from_file_location("voice_route_bench", SCRIPT)
bench = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bench)


def test_every_incident_utterance_is_a_case_with_its_call_so_far():
    cases = bench.load()
    assert len(cases) >= 25 and {case["expect"] for case in cases} == set(bench.ROUTES)
    missed = next(case for case in cases if case["words"].startswith("我并没有看到新的对话建立"))
    # The front desk's false "已经建好了" is in the context; it is never one of the user's own earlier words.
    assert "assistant: 好的，新对话已经建好了。" in missed["context"] and missed["expect"] == "dispatch"
    assert all("建好了。" not in words for words in missed["earlier"]) and "建好了吗" in missed["earlier"]
    assert next(case for case in cases if case["words"] == "确认，开始做吧")["cards"][0]["card"] == "card-1"


def test_todays_check_is_scored_on_what_it_can_say():
    assert bench.route_of_check(False) == "direct" and bench.route_of_check(True) == "dispatch"
    assert bench.route_of_check({"card": "card-1"}) == "card"
    assert bench.agrees("check", "direct", "lookup") and bench.agrees("check", "direct", "chat")
    assert not bench.agrees("route", "chat", "lookup") and not bench.agrees("check", "direct", "dispatch")


async def test_a_model_is_timed_and_its_misses_are_listed(monkeypatch):
    answers = {"今天上海天气怎么样": "lookup"}

    async def fake(prompt, model, case):
        if case["words"] == "你定吧":
            raise TimeoutError
        return answers.get(case["words"], case["expect"] if model == "good" else "chat")

    monkeypatch.setattr(bench, "ask", fake)
    cases = bench.load()
    good, poor = await bench.bench(["good", "poor"], ["route"], 1, cases)
    assert good["right"] == len(cases) - 1 and good["errors"] == 1 and good["p90_s"] is not None
    assert poor["right"] < good["right"] and {"words": "你定吧", "expect": "dispatch", "got": "TimeoutError"} in poor["wrong"]
    assert "good | route" in bench.render([good, poor])
