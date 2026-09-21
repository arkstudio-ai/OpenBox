"""Enrich a receipt without modifying it, its runs, or any grading result."""
import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select
from sqlalchemy.engine import make_url

from db.base import close_engine, get_db_session, init_engine
from db.models.agent_event import AgentEvent
from db.models.billing import UsageEvent
from db.models.session import Session
from db.models.team import TeamRun
from scripts.team_eval_metrics import journal_metrics, moment


async def report(receipt):
    rows = {}
    async with get_db_session() as db:
        for identifier, item in receipt["results"].items():
            if not item.get("finished_at") or not item.get("session_id"):
                continue
            root = await db.get(Session, item["session_id"])
            if root is None:
                raise ValueError("Retained root is missing")
            completed_at = None
            run_created_at = None
            if item.get("run_id"):
                run = await db.get(TeamRun, item["run_id"])
                if run is None or run.root_session_id != root.id or run.owner_user_id != root.user_id or run.workspace_id != root.workspace_id:
                    raise ValueError("Run does not belong to retained root")
                if item["status"] in {"completed", "failed", "canceled"} and run.state == item["status"]:
                    completed_at = run.ended_at
                run_created_at = run.created_at
            elif item["status"] == "completed":
                events = (await db.scalars(select(AgentEvent).where(AgentEvent.session_id == root.id,
                    AgentEvent.user_id == root.user_id, AgentEvent.kind == "turn.finished")
                    .order_by(AgentEvent.sequence))).all()
                finishes = [event.created_at for event in events if event.payload.get("finish") == "stop"
                    and not event.payload.get("error") and moment(event.created_at) <= moment(item["finished_at"])]
                completed_at = max(finishes) if finishes else None
            meters = (await db.scalars(select(UsageEvent).where(UsageEvent.id.in_([row["id"] for row in item.get("usage", [])]),
                UsageEvent.user_id == root.user_id, UsageEvent.workspace_id == root.workspace_id))).all()
            timestamps = {meter.id: meter.created_at for meter in meters}
            enriched = {**item, "run_created_at": run_created_at,
                "usage": [{**row, "created_at": timestamps.get(row["id"])} for row in item.get("usage", [])]}
            rows[identifier] = {"status": item["status"], "category": item["category"], "group": item["group"],
                "session_id": root.id, "run_id": item.get("run_id"), "metrics": journal_metrics(enriched, completed_at=completed_at),
                "known_credits": item["known_credits"], "unknown_cost_count": item["unknown_cost_count"],
                "objective_grade": item["objective_grade"], "manual_review": item.get("manual_review")}
            baseline = receipt["results"].get(item["case_id"] + ":single")
            if baseline and baseline.get("known_credits") and Decimal(baseline["known_credits"]) > 0:
                rows[identifier]["workflow_cost_ratio_to_single"] = str(Decimal(item["known_credits"]) / Decimal(baseline["known_credits"]))
            if item.get("coordinator_workflow_credits") and Decimal(item["known_credits"]) > 0:
                rows[identifier]["coordinator_workflow_share"] = str(Decimal(item["coordinator_workflow_credits"]) / Decimal(item["known_credits"]))
    return {"batch_id": receipt["batch_id"], "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest_sha256": receipt["manifest_sha256"], "results": rows,
        "note": "Partial evidence; no quality or release pass without complete cases and rubric review."}


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--review-pack", type=Path)
    args = parser.parse_args()
    if args.output.resolve() == args.receipt.resolve() or args.review_pack and args.review_pack.resolve() in {args.receipt.resolve(), args.output.resolve()}:
        raise SystemExit("Report and review pack must be distinct from the source receipt")
    url = make_url(args.database_url)
    if url.host not in {"127.0.0.1", "localhost"} or not any(word in (url.database or "") for word in ("local", "test", "check")):
        raise SystemExit("Only explicit loopback local/test databases are supported")
    receipt = json.loads(args.receipt.read_text())
    init_engine(args.database_url)
    try:
        result = await report(receipt)
    finally:
        await close_engine()
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if args.review_pack:
        manifest = Path(__file__).resolve().parents[2] / "docs/evaluations/agent-team-v1/cases.json"
        if hashlib.sha256(manifest.read_bytes()).hexdigest() != receipt["manifest_sha256"]:
            raise ValueError("The original manifest must match")
        cases = {case["id"]: case for case in json.loads(manifest.read_text())["cases"]}
        pack = [{"output_id": hashlib.sha256((receipt["batch_id"] + identifier).encode()).hexdigest()[:16],
            "prompt": cases[item["case_id"]]["prompt"], "answer": item["final_answer"],
            "expected": cases[item["case_id"]]["expected"]} for identifier, item in receipt["results"].items()
            if identifier in result["results"]]
        args.review_pack.write_text(json.dumps(sorted(pack, key=lambda row: row["output_id"]), ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"reported": len(result["results"]), "report": str(args.output), "review_pending": True}))


if __name__ == "__main__":
    asyncio.run(main())
