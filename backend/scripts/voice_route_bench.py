#!/usr/bin/env python
"""How fast and how right a model routes what the user just said (docs/VOICE_DISPATCH_PLAN_20261112.md M0).

M1 moves the "is this work to hand over?" decision ahead of the spoken reply,
so it has to come back in about two seconds. This measures candidates on the
utterances behind the plan (scripts/voice_dispatch_scenarios.json), two ways:

- ``check``: today's after-the-reply handoff check (voice/handoff.py ``required``), unchanged;
- ``route``: one short four-way question (chat / lookup / dispatch / card), the M1 candidate.

Read-only and offline from the call: no audio, no task is created. Run from
``backend/`` with the server's environment (model routes and keys resolve as
they do for the server; inside the backend container that is already so):

    python scripts/voice_route_bench.py                               # voice.summary_model, both prompts
    python scripts/voice_route_bench.py --models openai/a,openai/b --rounds 3 --json
    python scripts/voice_route_bench.py --export /tmp/utterances.txt  # the lines for tests/manual/voice_replay.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from statistics import median

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

SCENARIOS = Path(__file__).resolve().parent / "voice_dispatch_scenarios.json"
ROUTES = ("chat", "lookup", "dispatch", "card")
TIMEOUT_SECONDS = 15.0
ROUTE = (
    "判断电话里用户最新一句话该走哪条路，只输出一个 JSON 对象：{\"route\":\"chat|lookup|dispatch|card\",\"card\":\"卡片标识或空\"}。\n"
    "dispatch：用户要人去办事——创建交付物、修改或继续已有任务、催执行、外部操作、定时安排、"
    "要求新建对话或把谈好的内容递交出去；把决定权交给你（“你定吧”）且前文是在交办，也算。\n"
    "lookup：只是问现状——进度、是否已提交、余额、日程、天气这类查一下就能答的。\n"
    "card：open_cards 里有卡片，且这句话本身明确选了其中一个选项。改要求、提问、犹豫、暂缓都不是 card。\n"
    "chat：其余——闲聊、解释、澄清、讨论方案、假设性提问、应声、调整说话方式。\n"
    "对卡片上的方案提出修改是 dispatch。只依据用户原话判断，前台说过什么不算用户的决定。"
    "card 字段：route 为 card 时填所选卡片的标识；route 为 dispatch 且这句话是在改 open_cards 里某一张卡上的方案"
    "（换档位、改时长、改文案、换素材等）时填那张卡的标识；是另一件事或看不出针对哪张卡就填空字符串。"
    "context_withheld 为 true 时只依据 latest_user 判断，指代不明就选 chat。")


def load(path: Path = SCENARIOS) -> list[dict]:
    """One case per user utterance: its words, the call so far, the user's earlier words, cards shown, the route."""
    cases = []
    for scenario in json.loads(path.read_text(encoding="utf-8"))["scenarios"]:
        lines, said = [], []
        for turn in scenario["turns"]:
            if "front" in turn:
                lines.append("assistant: " + turn["front"])
                continue
            if turn["expect"] not in ROUTES:
                raise ValueError(f"unknown route in {scenario['id']}: {turn['expect']}")
            cases.append({"scenario": scenario["id"], "words": turn["user"], "expect": turn["expect"],
                          "context": "\n".join(lines), "earlier": tuple(said), "cards": tuple(scenario.get("cards") or ())})
            lines.append("user: " + turn["user"])
            said.append(turn["user"])
    return cases


def route_of_check(value) -> str:
    """Today's check answers handoff / direct / a card choice; direct covers both chat and lookup."""
    if isinstance(value, dict):
        return "card"
    return "dispatch" if value else "direct"


def agrees(prompt: str, got: str, expect: str) -> bool:
    return got == expect or (prompt == "check" and got == "direct" and expect in ("chat", "lookup"))


async def ask(prompt: str, model: str, case: dict) -> str:
    from voice import handoff
    from voice.summary import complete
    if prompt == "check":
        from core.config import get_config
        config = get_config()
        previous, config.voice.summary_model = config.voice.summary_model, model
        try:
            extra = (case["earlier"],) if case["earlier"] else ()
            shown = {"cards": case["cards"]} if case["cards"] else {}
            return route_of_check(await handoff.required(case["words"], case["context"], *extra, **shown))
        finally:
            config.voice.summary_model = previous
    payload = {"context": case["context"], "latest_user": case["words"]}
    if case["cards"]:
        payload["open_cards"] = [{"card": card["card"], "title": card["title"], "options": card["options"]}
                                 for card in case["cards"]]
    answer = await complete(model, getattr(handoff, "ROUTE", ROUTE), json.dumps(payload, ensure_ascii=False),
                            TIMEOUT_SECONDS)
    route = json.loads(handoff._unfenced(answer)).get("route")
    return route if route in ROUTES else f"invalid:{str(route)[:20]}"


async def bench(models: list[str], prompts: list[str], rounds: int, cases: list[dict]) -> list[dict]:
    results = []
    for model in models:
        for prompt in prompts:
            times, right, wrong, errors = [], 0, [], 0
            for _ in range(rounds):
                for case in cases:
                    began = time.monotonic()
                    try:
                        got = await asyncio.wait_for(ask(prompt, model, case), TIMEOUT_SECONDS)
                    except Exception as exc:  # a timeout or a malformed answer is a miss, and counted as one
                        errors += 1
                        wrong.append({"words": case["words"], "expect": case["expect"], "got": type(exc).__name__})
                        continue
                    times.append(time.monotonic() - began)
                    if agrees(prompt, got, case["expect"]):
                        right += 1
                    else:
                        wrong.append({"words": case["words"], "expect": case["expect"], "got": got})
            asked = rounds * len(cases)
            ordered = sorted(times)
            results.append({"model": model, "prompt": prompt, "asked": asked, "right": right, "errors": errors,
                            "accuracy": right / asked if asked else None,
                            "p50_s": median(ordered) if ordered else None,
                            "p90_s": ordered[min(len(ordered) - 1, round(0.9 * (len(ordered) - 1)))] if ordered else None,
                            "max_s": ordered[-1] if ordered else None, "wrong": wrong})
    return results


def render(results: list[dict]) -> str:
    def seconds(value):
        return "—" if value is None else f"{value:.2f}s"
    lines = ["model | prompt | right | p50 | p90 | max | errors"]
    for item in results:
        lines.append(f"{item['model']} | {item['prompt']} | {item['right']}/{item['asked']} | {seconds(item['p50_s'])} | "
                     f"{seconds(item['p90_s'])} | {seconds(item['max_s'])} | {item['errors']}")
        for miss in item["wrong"][:12]:
            lines.append(f"    ✗ {miss['words']} → {miss['got']}（应为 {miss['expect']}）")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models", default="", help="comma-separated; default: voice.summary_model")
    parser.add_argument("--prompts", default="check,route")
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--export", default="", help="write the user lines, one per line, and exit")
    args = parser.parse_args()
    cases = load()
    if args.export:
        Path(args.export).write_text("\n".join(case["words"] for case in cases) + "\n", encoding="utf-8")
        print(f"{len(cases)} lines → {args.export}")
        return 0
    from core.config import get_config
    models = [name.strip() for name in args.models.split(",") if name.strip()] or [get_config().voice.summary_model]
    prompts = [name for name in args.prompts.split(",") if name in ("check", "route")]
    results = asyncio.run(bench(models, prompts, max(args.rounds, 1), cases))
    print(json.dumps(results, ensure_ascii=False, indent=2) if args.json else render(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
