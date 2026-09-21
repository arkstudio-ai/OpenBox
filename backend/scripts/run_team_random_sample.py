"""Bounded, preregistered local bug-regression sample; never a full M5 result."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse
import uuid

import httpx
from sqlalchemy.engine import make_url

from run_team_evaluation import Api, MANIFEST_DIGEST, close_engine, init_engine, now, run_case, save, setup, source_digest


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account-file", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--api", default="http://127.0.0.1:8080")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    database = make_url(args.database_url)
    if (database.host not in {"localhost", "127.0.0.1"}
            or not any(word in (database.database or "") for word in ("local", "test", "check"))
            or urlparse(args.api).hostname not in {"localhost", "127.0.0.1"}):
        raise ValueError("Only explicit loopback local/test services are supported")
    account = json.loads(args.account_file.read_text())
    if not account.get("username", "").startswith("team_local_"):
        raise ValueError("Use a dedicated team_local_ fixture account")
    repo = Path(__file__).resolve().parents[2]
    protocol_dir = repo / "docs/evaluations/agent-team-random-20260921"
    manifest = repo / "docs/evaluations/agent-team-v1/cases.json"
    selection = protocol_dir / "selection.json"
    plan = json.loads(selection.read_text())
    cases = {case["id"]: case for case in json.loads(manifest.read_text())["cases"]}
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == MANIFEST_DIGEST
    assert len(plan["selection"]) == 3
    assert all(item["groups"][0] == "single" and len(item["groups"]) == 2 for item in plan["selection"])
    assert {item["groups"][1] for item in plan["selection"]} == {"fixed", "mixed", "automatic"}
    fingerprints = {"source_sha256": source_digest(), "manifest_sha256": MANIFEST_DIGEST,
        "protocol_sha256": hashlib.sha256((protocol_dir / "PROTOCOL.md").read_bytes()).hexdigest(),
        "selection_sha256": hashlib.sha256(selection.read_bytes()).hexdigest(),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    if not args.execute:
        print(json.dumps({"dry_run": True, **plan, **fingerprints}))
        return
    receipt = json.loads(args.output.read_text()) if args.output.exists() else {
        "batch_id": "random-" + uuid.uuid4().hex, "scope": "random_bug_regression",
        "round": "v4", "round_note": "v4 helpers only; separate bounded sample protocol",
        "model": plan["model"], "selection": plan, "registered_at": now(), **fingerprints}
    if (receipt.get("scope") != "random_bug_regression" or receipt.get("selection") != plan
            or any(receipt.get(key) != value for key, value in fingerprints.items())):
        raise ValueError("Registered sample changed; retain this receipt and register a separate regression")
    save(args.output, receipt)
    init_engine(args.database_url)
    try:
        async with httpx.AsyncClient(base_url=args.api, timeout=60) as client:
            api = Api(client)
            auth = await api.call("POST", "/api/auth/login", {key: account[key] for key in ("username", "password")})
            client.headers["Authorization"] = "Bearer " + auth["access_token"]
            billing = await api.call("GET", "/api/billing/balance")
            if billing.get("mode") not in {"shadow", "enforce"} or receipt.get("billing_mode", billing["mode"]) != billing["mode"]:
                raise ValueError("Sample requires unchanged recorded account billing")
            receipt.setdefault("billing_mode", billing["mode"])
            receipt.setdefault("initial_account_balance", billing["balance"])
            save(args.output, receipt)
            await setup(api, receipt, args.output)
            for item in plan["selection"]:
                for group in item["groups"]:
                    await run_case(api, cases[item["case_id"]], group, receipt, args.output)
            receipt["finished_at"] = now()
            save(args.output, receipt)
    finally:
        await close_engine()


if __name__ == "__main__":
    asyncio.run(main())
