"""Live memory quality check against the configured extraction and verification models.

Runs every labelled statement in tests/fixtures/memory_eval/extraction.json through the
same extraction and independent verification that the background worker uses, without a
database or any user data, and reports what would have been remembered.

    cd backend && .venv/bin/python scripts/memory_eval.py [--only id,id] [--json report.json]

Metrics
- fact recall: expected facts that were admitted (extracted and verified)
- false memories: cases where forbidden content was admitted, or anything was admitted
  where nothing should have been
- latency and tokens per case

Each case makes up to two model calls and costs real tokens. Exit status is 1 when recall
falls below --min-recall or any false memory is found, so it can gate a model or prompt change.
"""
import argparse
import asyncio
import json
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from dotenv import load_dotenv  # noqa: E402

# Same provider settings as the server (main.py loads this file too).
load_dotenv(BACKEND / ".env")

from core.config import get_config  # noqa: E402
from memory.extraction import ConfiguredMemoryExtractor, ExtractionProviderError, ExtractionSchemaError  # noqa: E402
from memory.grounding import verify_memories  # noqa: E402
from memory.jobs import ExtractionInput  # noqa: E402
from memory.providers.common import MemoryProviderError  # noqa: E402
from wiki_compiler.hashing import canonical_hash  # noqa: E402

CASES = BACKEND / "tests" / "fixtures" / "memory_eval" / "extraction.json"


def frozen_input(text: str) -> ExtractionInput:
    return ExtractionInput(job_id="memory-eval", user_id="eval-user", workspace_id="eval-workspace", project_id=None,
        session_id="eval-session", logical_turn_id="eval-turn", input_hash=canonical_hash(text),
        sources=({"source_kind": "user_statement", "body": text,
                  "occurred_at": datetime.now(timezone.utc).isoformat()},),
        existing_memories=(), base_revisions=(), acl_hash="memory-eval")


async def run_case(case: dict, settings) -> dict:
    frozen = frozen_input(case["say"])
    started = time.monotonic()
    proposals, admitted, error, tokens = [], [], None, 0
    try:
        result = await ConfiguredMemoryExtractor()(frozen)
        proposals = result.proposals
        tokens += int(result.usage.get("input_tokens") or 0) + int(result.usage.get("output_tokens") or 0)
    except ExtractionSchemaError as exc:
        # The worker drops the whole turn on a schema refusal (e.g. a secret).
        error = f"refused:{exc}"
    except ExtractionProviderError as exc:
        error = f"provider:{exc.code}"
    if proposals:
        try:
            grounding, usage = await verify_memories(frozen, proposals, settings)
            supported = set(grounding["supported"])
            admitted = [item["summary"] for item in proposals if canonical_hash(item) in supported]
            tokens += int((usage or {}).get("input_tokens") or 0) + int((usage or {}).get("output_tokens") or 0)
        except MemoryProviderError as exc:
            error = f"verifier:{exc.code}"
    found = [any(all(phrase in summary for phrase in fact) for summary in admitted) for fact in case["expect"]]
    leaked = [phrase for phrase in case["forbid"] if any(phrase in summary for summary in admitted)]
    unwanted = admitted if not case["expect"] else []
    return {"id": case["id"], "say": case["say"], "proposed": [item["summary"] for item in proposals],
            "admitted": admitted, "expected_found": found, "false_memory": bool(leaked or unwanted),
            "leaked": leaked, "error": error, "ms": round((time.monotonic() - started) * 1000), "tokens": tokens}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", type=Path, default=CASES)
    parser.add_argument("--only", default="", help="comma-separated case ids")
    parser.add_argument("--json", type=Path, help="write the full report here")
    parser.add_argument("--min-recall", type=float, default=0.85)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text())["cases"]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [case for case in cases if case["id"] in wanted]
    settings = get_config().memory
    results = [await run_case(case, settings) for case in cases]
    facts = [hit for result in results for hit in result["expected_found"]]
    recall = sum(facts) / len(facts) if facts else 1.0
    false_cases = [result["id"] for result in results if result["false_memory"]]
    errors = [f"{result['id']}={result['error']}" for result in results if result["error"]
              and not result["error"].startswith("refused:")]
    latencies = sorted(result["ms"] for result in results)
    for result in results:
        status = "FALSE" if result["false_memory"] else ("ok" if all(result["expected_found"]) else "miss")
        print(f"{status:5} {result['id']:22} {result['ms']:6} ms  admitted={result['admitted']}"
              + (f"  error={result['error']}" if result["error"] else ""))
    print(f"\nmodel={settings.extract_model or get_config().model} cases={len(results)}")
    print(f"fact recall {recall:.2f} ({sum(facts)}/{len(facts)}) | false memories {len(false_cases)} {false_cases}"
          f" | provider errors {len(errors)} {errors}")
    if latencies:
        p95 = latencies[min(len(latencies) - 1, int(round(len(latencies) * 0.95)) - 1)]
        print(f"latency p50 {statistics.median(latencies):.0f} ms, p95 {p95} ms"
              f" | tokens {sum(result['tokens'] for result in results)}")
    if args.json:
        args.json.write_text(json.dumps({"recall": recall, "false_memories": false_cases, "errors": errors,
                                         "results": results}, ensure_ascii=False, indent=2))
    return 0 if recall >= args.min_recall and not false_cases and not errors else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
