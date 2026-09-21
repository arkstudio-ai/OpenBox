"""Resumable, serial, real-model evaluation through the local public API.

Requires --execute, a loopback API, a dedicated team_local_ account file, and
an explicit loopback local/test database. Retains every root and result. No
media request is dispatched by this text harness. Raw grading evidence is
saved locally; console output contains only case IDs and status.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlparse
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from sqlalchemy import or_, select
from sqlalchemy.engine import make_url

from db.base import close_engine, get_db_session, init_engine
from db.models.billing import UsageEvent
from db.models.message import Message
from db.models.part import Part
from db.models.session import Session
from db.models.team import TeamEvent
from team.journal import Actor, snapshot

MODEL = "openai/gpt-5.6-luna"
ROUND_MODELS = {"v1": MODEL, "v2": "openai/qwen3.8-flash", "v3": "openai/qwen3.8-flash", "v4": "openai/qwen3.8-flash"}
ROUND_BUDGETS = {"v1": "2", "v2": "2", "v3": "10", "v4": None}
GROUPS = ("single", "fixed", "mixed", "automatic")
MANIFEST_DIGEST = "8b1aa2c483c7b2b0042d4fab917928d12a13b076db5a4042d2d652fa8eaa95f6"


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, data):
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def validate_round(receipt, round_name, protocol_digest):
    """A resumed receipt cannot silently switch provider or registered rules."""
    expected_protocol = f"docs/evaluations/agent-team-{round_name}/PROTOCOL.md"
    if (receipt.get("round", "v1") != round_name
            or receipt.get("model", MODEL) != ROUND_MODELS[round_name]
            or receipt.get("budget_credits", ROUND_BUDGETS[round_name]) != ROUND_BUDGETS[round_name]
            or receipt.get("manifest_sha256") != MANIFEST_DIGEST
            or receipt.get("registered_protocol") != expected_protocol
            or receipt.get("protocol_sha256", protocol_digest) != protocol_digest):
        raise ValueError("The receipt belongs to a different frozen evaluation round; use a new output path")
    if round_name != "v1" and not receipt.get("protocol_sha256"):
        raise ValueError("New evaluation rounds require a protocol digest")


def source_digest():
    """Fingerprint executable sources without reading deployment secrets."""
    root = Path(__file__).resolve().parents[1]
    directories = ("agent", "agent_catalog", "api", "billing", "core", "db", "permission",
        "project", "question", "sandbox", "session", "skill", "snapshot", "team", "tool")
    paths = [path for directory in directories for path in (root / directory).rglob("*.py")]
    paths += [root / "main.py", Path(__file__).resolve()]
    digest = hashlib.sha256()
    for path in sorted(set(paths)):
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


class Api:
    def __init__(self, client):
        self.client = client

    async def call(self, method, path, body=None, key=None):
        headers = {"Idempotency-Key": key or str(uuid.uuid4())} if method != "GET" else {}
        response = await self.client.request(method, path, json=body,
            headers=headers)
        if response.status_code == 401 and not path.startswith("/api/auth/"):
            refreshed = await self.client.post("/api/auth/refresh")
            refreshed.raise_for_status()
            self.client.headers["Authorization"] = "Bearer " + refreshed.json()["access_token"]
            response = await self.client.request(method, path, json=body, headers=headers)
        response.raise_for_status()
        return response.json() if response.content else None


def role(name, review=False, *, model=MODEL):
    return {"name": name, "description": "Review and cross-check supplied evidence" if review else "Analyze supplied evidence",
        "when_to_use": "Controlled evaluation of supplied research, numbers, code and scripts",
        "instruction": ("Independently verify facts, calculations, boundaries and requested output before submitting. " if review
            else "Analyze the supplied material carefully and produce the requested deliverable. ")
            + "Use only provided facts; identify missing evidence. Return every requested JSON field and explanation. Do not ask questions for fully specified fixtures.",
        "default_model": model, "model_locked": True, "tool_allowlist": [], "skill_refs": [],
        "generation_options": {"max_output_tokens": 4096}, "execution_policy": {"max_steps": 30, "max_wall_time_seconds": 600}}


async def setup(api, receipt, path):
    if receipt.get("prepared"):
        return
    suffix = receipt["batch_id"][-8:]
    model = receipt.get("model", MODEL)
    budget = ROUND_BUDGETS[receipt.get("round", "v1")]
    receipt.setdefault("agents", {})
    for kind in ("analyst", "reviewer"):
        if kind in receipt["agents"]:
            continue
        spec = role(f"M5 {kind} {suffix}", kind == "reviewer", model=model)
        draft = await api.call("POST", "/api/agent-definitions", {"spec": spec}, key=f"{receipt['batch_id']}:{kind}")
        published = await api.call("POST", f"/api/agent-definitions/{draft['id']}/versions",
            {"expected_revision": draft["revision"]}, key=f"{receipt['batch_id']}:{kind}:publish")
        receipt["agents"][kind] = published
        save(path, receipt)
    receipt.setdefault("templates", {})
    for group in GROUPS[1:]:
        if group in receipt["templates"]:
            continue
        members = []
        if group != "automatic":
            members.append({"alias": "analyst", "agent_ref": receipt["agents"]["analyst"]["id"],
                "responsibility": "Analyze the supplied problem and produce a complete candidate answer."})
            members.append({"alias": "reviewer", "responsibility": "Independently check the candidate and all output requirements.",
                **({"agent_ref": receipt["agents"]["reviewer"]["id"]} if group == "fixed" else {"inline": role("Evaluation reviewer", True, model=model)})})
        policy = {"member_selection": "coordinator_select" if group == "automatic" else "explicit_only",
            "member_creation": "disabled" if group == "fixed" else "run_scoped", "allowed_models": [model],
            "delegable_tools": [], "allowed_skills": [], "mcp_refs": [], "max_members": 4,
            "max_concurrent_members": 2, "max_coordinator_turns": 30, "max_tasks": 30,
            "max_messages": 100, **({"budget_credits": budget} if budget is not None else {}), "max_wall_time_seconds": 600, "paid_tools": {}}
        spec = {"name": f"M5 {group} {suffix}", "coordinator": {"model": model}, "preset_members": members, "policy": policy}
        draft = await api.call("POST", "/api/team-definitions", {"spec": spec}, key=f"{receipt['batch_id']}:{group}")
        published = await api.call("POST", f"/api/team-definitions/{draft['id']}/versions",
            {"expected_revision": draft["revision"]}, key=f"{receipt['batch_id']}:{group}:publish")
        receipt["templates"][group] = published
        save(path, receipt)
    receipt["prepared"] = now()
    save(path, receipt)


def matching_lineup(question, *, model=MODEL, budget="2"):
    entries = question.get("questions", [])
    if len(entries) != 1:
        return False
    detail = entries[0].get("detail") or {}
    if detail.get("kind") != "team_lineup":
        return False
    spec = detail.get("spec") or {}
    policy = spec.get("policy") or {}
    protocol = {"team_view", "team_message_send", "team_task_update", "team_wait"}
    return (detail.get("coordinator", {}).get("model") == model
        and (("budget_credits" not in policy) if budget is None else Decimal(str(policy.get("budget_credits", "0"))) <= Decimal(budget))
        and not policy.get("paid_tools") and not policy.get("mcp_refs") and not policy.get("delegable_tools")
        and policy.get("max_members", 999) <= 4 and policy.get("max_concurrent_members", 999) <= 2
        and policy.get("max_wall_time_seconds", 9999) <= 600 and policy.get("max_coordinator_turns", 999) <= 30
        and all(member.get("model") == model and set(member.get("tool_ids", [])) <= protocol
            and not member.get("skills") for member in detail.get("members", [])))


async def collect(session_id, run_id, started):
    async with get_db_session() as db:
        root = await db.get(Session, session_id)
        assert root is not None
        sessions = list((await db.scalars(select(Session.id).where(Session.user_id == root.user_id,
            Session.workspace_id == root.workspace_id, or_(Session.id == session_id, Session.parent_id == session_id)))).all())
        usage = list((await db.scalars(select(UsageEvent).where(UsageEvent.session_id.in_(sessions),
            UsageEvent.user_id == root.user_id, UsageEvent.workspace_id == root.workspace_id))).all())
        parts = list((await db.scalars(select(Part).where(Part.session_id.in_(sessions), Part.user_id == root.user_id)
            .order_by(Part.created_at))).all())
        messages = list((await db.scalars(select(Message).where(Message.session_id == session_id, Message.user_id == root.user_id,
            Message.role == "assistant").order_by(Message.created_at))).all())
        events = list((await db.scalars(select(TeamEvent).where(TeamEvent.team_run_id == run_id).order_by(TeamEvent.sequence))).all()) if run_id else []
        actor = Actor(root.user_id, root.workspace_id)
    state = await snapshot(run_id, actor) if run_id else None
    final = state["run"].get("final_summary", "") if state else ""
    if not state and messages:
        final = "\n".join(row.data.get("text", "") for row in parts if row.message_id == messages[-1].id and row.type == "text")
    records = [{"id": row.id, "session_id": row.session_id, "model": row.model_id, "kind": row.kind,
        "credits": str(row.credits) if row.credits is not None else None, "tokens": row.total_tokens,
        "status": row.status, "attribution": (row.pricing or {}).get("team_attribution")} for row in usage]
    tool_results = [row for row in parts if row.type == "tool"]
    result_text = [json.dumps({key: row.data.get(key) for key in ("output", "metadata", "error")}, ensure_ascii=False) for row in tool_results]
    deviations = {"implicit": sum(a["implicit"] for a in state["attempts"].values()) if state else 0,
        "nudges": sum(row.kind == "team.member" and row.payload["data"].get("nudged") is True for row in events),
        "no_progress": sum(bool(re.search(r'\\?"status\\?"\s*:\s*\\?"no_progress', value)) for value in result_text),
        "stale_revision": sum("STALE_REVISION" in value for value in result_text),
        "backpressure": sum(row.kind == "team.notice" and row.payload["data"].get("code") == "DRIVER_BACKPRESSURE" for row in events)}
    # Count nudge transitions, not every later full member snapshot with nudged=true.
    previous = {}
    deviations["nudges"] = 0
    for event in events:
        if event.kind == "team.member":
            nudged = event.payload["data"].get("nudged", False)
            deviations["nudges"] += int(nudged and not previous.get(event.entity_id, False))
            previous[event.entity_id] = nudged
    accepted = [row.created_at for row in events if row.kind == "team.task" and row.payload["data"].get("state") == "succeeded"]
    start = datetime.fromisoformat(started)
    first = min(accepted) if accepted else None
    if first and first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    return {"final_answer": final, "usage": records, "known_credits": str(sum((row.credits or Decimal(0) for row in usage), Decimal(0))),
        "unknown_cost_count": sum(row.credits is None for row in usage),
        "coordinator_workflow_credits": str(sum((row.credits or Decimal(0) for row in usage if row.session_id == session_id), Decimal(0))) if state else None,
        "first_accepted_seconds": (first-start).total_seconds() if first else None,
        "protocol_deviations": deviations, "message_count": len(state["messages"]) if state else 0,
        "tasks": list(state["tasks"].values()) if state else [], "attempts": list(state["attempts"].values()) if state else [],
        "events": [{"sequence": row.sequence, "kind": row.kind, "entity_id": row.entity_id,
            "data": row.payload["data"] if row.kind not in {"team.member.admitted", "team.run.created"} else {},
            "created_at": row.created_at.isoformat()} for row in events],
        "manual_review": None, "manual_interventions": 0}


def grade_fields(answer, expected):
    decoder, candidates = json.JSONDecoder(), []
    for match in re.finditer(r"\{", answer):
        try:
            value, _ = decoder.raw_decode(answer[match.start():])
            if isinstance(value, dict):
                candidates.append(value)
        except ValueError:
            pass
    best = max(candidates, key=lambda value: len(set(expected) & set(value)), default={})
    checks = {key: best.get(key) == value for key, value in expected.items()}
    return {"checks": checks, "fraction": sum(checks.values()) / len(checks) if checks else None, "parsed": best}


async def run_case(api, case, group, receipt, path):
    model = receipt.get("model", MODEL)
    budget = ROUND_BUDGETS[receipt.get("round", "v1")]
    identifier = f"{case['id']}:{group}"
    item = receipt.setdefault("results", {}).setdefault(identifier, {"case_id": case["id"], "category": case["category"], "group": group})
    if item.get("finished_at"):
        return
    if case.get("requires_verified_media"):
        item.update(status="blocked_verified_media_required", finished_at=now())
        save(path, receipt)
        return
    if "session_id" not in item:
        project = await api.call("POST", "/api/agent/project", {"name": f"M5 {identifier} {receipt['batch_id'][-8:]}"})
        item["project_id"] = project["id"]
        if group == "single":
            agent = receipt["agents"]["analyst"]
            session = await api.call("POST", f"/api/agent-definitions/{agent['id']}/test-runs",
                {"version_id": agent["current_version_id"], "project_id": project["id"]}, key=receipt["batch_id"]+identifier)
            item["session_id"] = session["session_id"]
        else:
            session = await api.call("POST", "/api/agent/session", {"model": model, "agent": "team", "project_id": project["id"], "title": f"M5 {identifier}"})
            item["session_id"] = session["id"]
        save(path, receipt)
    sid = item["session_id"]
    if "submitted_at" not in item:
        # Persist a stable input ID before acceptance; a response-lost retry uses it again.
        item.setdefault("input_id", str(uuid.uuid4()))
        item.setdefault("started_at", now())
        save(path, receipt)
        body = {"text": case["prompt"], "model": model, "delivery": "followup", "client_message_id": item["input_id"]}
        if group != "single":
            body.update(agent="team", team_request={"template_id": receipt["templates"][group]["id"], "allow_supplement": group == "automatic"})
            body["text"] += f"\n请按选中的模板先提议组队。协调者与成员都使用 {model}，不设置reasoning，不添加工具或Skill。最终汇总必须保留要求的完整JSON与说明。"
        await api.call("POST", f"/api/agent/session/{sid}/prompt_async", body)
        item["submitted_at"] = now()
        save(path, receipt)
    deadline = datetime.fromisoformat(item["started_at"]).timestamp() + 900
    while time.time() < deadline:
        questions = [q for q in await api.call("GET", "/api/agent/question") if q["session_id"] == sid]
        if questions:
            question = questions[0]
            if not item.get("confirmed_at") and group != "single" and matching_lineup(question, model=model, budget=budget):
                await api.call("POST", f"/api/agent/question/{question['id']}", {"answers": [["开始"]]})
                item["confirmed_at"] = now()
                save(path, receipt)
            else:
                item.update(status="unexpected_question", question=question)
                break
        runs = (await api.call("GET", f"/api/team-runs?session_id={sid}"))["items"] if group != "single" else []
        if runs:
            item["run_id"] = runs[0]["id"]
            if runs[0]["state"] in {"completed", "canceled", "failed", "paused"}:
                item["status"] = runs[0]["state"]
                break
        elif group == "single":
            session = await api.call("GET", f"/api/agent/session/{sid}")
            async with get_db_session() as db:
                last = await db.scalar(select(Message).where(Message.session_id == sid, Message.role == "assistant").order_by(Message.created_at.desc()).limit(1))
            if last and session["status"] in {"idle", "error"} and last.finish:
                item["status"] = "completed" if last.finish == "stop" and not last.error else "failed"
                break
        await asyncio.sleep(3)
    else:
        item["status"] = "timeout"
    item["finished_at"] = now()
    item["elapsed_seconds"] = datetime.fromisoformat(item["finished_at"]).timestamp() - datetime.fromisoformat(item["started_at"]).timestamp()
    item.update(await collect(sid, item.get("run_id"), item["started_at"]))
    item["objective_grade"] = grade_fields(item["final_answer"], case["expected"])
    save(path, receipt)
    # Bounded fixture work only: cancel/abort, never delete any evidence.
    if item["status"] not in {"completed", "canceled", "failed"}:
        if item.get("run_id"):
            current = await api.call("GET", f"/api/team-runs/{item['run_id']}")
            if current["run"]["state"] not in {"completed", "canceled", "failed"}:
                await api.call("POST", f"/api/team-runs/{item['run_id']}/cancel", {"expected_revision": current["run"]["revision"]})
        else:
            await api.call("POST", f"/api/agent/session/{sid}/abort")
    print(json.dumps({"case": identifier, "status": item["status"], "seconds": round(item["elapsed_seconds"], 2),
        "objective_fraction": item["objective_grade"]["fraction"], "unknown_cost_count": item["unknown_cost_count"]}), flush=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8080")
    parser.add_argument("--account-file", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", default="")
    parser.add_argument("--groups", default=",".join(GROUPS))
    parser.add_argument("--round", choices=ROUND_MODELS, default="v4", help="Frozen model/protocol round; never changes an existing receipt")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    url = make_url(args.database_url)
    if url.host not in {"127.0.0.1", "localhost"} or not any(word in (url.database or "") for word in ("local", "test", "check")) or urlparse(args.api).hostname not in {"127.0.0.1", "localhost"}:
        raise SystemExit("Only explicit loopback local/test services are supported")
    account = json.loads(args.account_file.read_text())
    if not account.get("username", "").startswith("team_local_"):
        raise SystemExit("Use a dedicated team_local_ test account")
    manifest = Path(__file__).resolve().parents[2] / "docs/evaluations/agent-team-v1/cases.json"
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == MANIFEST_DIGEST, "Preregister a new round when cases change"
    cases = json.loads(manifest.read_text())["cases"]
    if args.cases:
        selected = args.cases.split(",")
        cases = [case for case in cases if case["id"] in selected]
        assert len(cases) == len(selected)
    groups = args.groups.split(",")
    assert groups and set(groups) <= set(GROUPS)
    protocol_path = f"docs/evaluations/agent-team-{args.round}/PROTOCOL.md"
    protocol = Path(__file__).resolve().parents[2] / protocol_path
    protocol_digest = hashlib.sha256(protocol.read_bytes()).hexdigest()
    model = ROUND_MODELS[args.round]
    if not args.execute:
        print(json.dumps({"dry_run": True, "round": args.round, "model": model, "manifest_sha256": MANIFEST_DIGEST,
            "protocol_sha256": protocol_digest, "cases": len(cases), "groups": groups}))
        return
    if args.round != "v4":
        raise ValueError("Rounds v1-v3 are retained historical protocols; use v4 for account-credit execution")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    receipt = json.loads(args.output.read_text()) if args.output.exists() else {"batch_id": f"m5-{args.round}-"+uuid.uuid4().hex,
        "round": args.round, "model": model, "budget_credits": ROUND_BUDGETS[args.round], "manifest_sha256": MANIFEST_DIGEST,
        "registered_protocol": protocol_path, "protocol_sha256": protocol_digest}
    validate_round(receipt, args.round, protocol_digest)
    current_source = source_digest()
    if receipt.get("source_sha256", current_source) != current_source:
        raise ValueError("Executable sources changed since this receipt was registered; preregister a new round")
    receipt.setdefault("source_sha256", current_source)
    save(args.output, receipt)
    init_engine(args.database_url)
    try:
        async with httpx.AsyncClient(base_url=args.api, timeout=60) as client:
            api = Api(client)
            auth = await api.call("POST", "/api/auth/login", {"username": account["username"], "password": account["password"]})
            client.headers["Authorization"] = "Bearer " + auth["access_token"]
            billing = await api.call("GET", "/api/billing/balance")
            mode = billing.get("mode")
            if mode not in {"shadow", "enforce"}:
                raise ValueError("Evaluation requires recorded account billing")
            if receipt.get("billing_mode", mode) != mode:
                raise ValueError("Account billing mode changed during this evaluation round")
            receipt.setdefault("billing_mode", mode)
            receipt.setdefault("initial_account_balance", billing.get("balance"))
            save(args.output, receipt)
            await setup(api, receipt, args.output)
            for case in cases:
                for group in groups:
                    await run_case(api, case, group, receipt, args.output)
    finally:
        await close_engine()


if __name__ == "__main__":
    asyncio.run(main())
