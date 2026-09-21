"""Observe a real local run and optionally submit four ordinary-chat probes.

No existing session or row is removed. The receipt must identify a dedicated
local fixture root. The three WebSocket clients are positive-control (all
members), aggregate (none), and one selected member; payload text is not saved.
This measures one process, not production throughput or multi-user capacity.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlparse
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import psutil
from sqlalchemy import text
from sqlalchemy.engine import make_url
import websockets

from db.base import close_engine, get_db_session, init_engine
from scripts.run_team_evaluation import Api, now, save


async def run(args):
    url = make_url(args.database_url)
    if url.host not in {"127.0.0.1", "localhost"} or not any(word in (url.database or "") for word in ("local", "test", "check")):
        raise ValueError("An explicit loopback local/test database is required")
    if urlparse(args.api).hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("A loopback API is required")
    account = json.loads(args.account_file.read_text())
    if not account.get("username", "").startswith("team_local_"):
        raise ValueError("Use a dedicated local verification account")
    fixture = json.loads(args.receipt.read_text())
    root_id = fixture["session_id"]
    process = psutil.Process(args.pid)
    if not any("serve.py" in value for value in process.cmdline()):
        raise ValueError("The measured PID must be the local verification server")
    data = {"started_at": now(), "root_id": root_id, "pid": args.pid, "probe_model": args.model, "samples": [], "probes": [],
        "streams": {name: {} for name in ("all", "aggregate", "selected")}, "errors": []}
    init_engine(args.database_url)
    sockets, readers, probes = [], [], []
    delta_times = defaultdict(list)
    selected, member_ids, probed = None, [], set()
    started, finished = time.monotonic(), None
    try:
        async with httpx.AsyncClient(base_url=args.api, timeout=60) as client:
            api = Api(client)
            auth = await api.call("POST", "/api/auth/login", {"username": account["username"], "password": account["password"]})
            client.headers["Authorization"] = "Bearer " + auth["access_token"]

            async def read(name, ws):
                part_types = {}
                async for raw in ws:
                    event = json.loads(raw)
                    kind, body = event.get("type"), event.get("data", {})
                    sid = body.get("sessionId")
                    if kind == "part.created" and isinstance(body.get("part"), dict):
                        part_types[body["part"]["id"]] = body["part"].get("type")
                    if kind in {"part.delta", "message.text_delta"} and sid:
                        counts = data["streams"][name]
                        counts[sid] = counts.get(sid, 0) + 1
                        if name == "all":
                            stamp = time.monotonic()
                            delta_times[sid].append(stamp)
                            if kind == "message.text_delta" or part_types.get(body.get("partId")) == "text":
                                for probe in data["probes"]:
                                    if probe.get("session_id") == sid and probe.get("ttft_seconds") is None:
                                        probe["ttft_seconds"] = stamp - probe["submitted_monotonic"]
                    elif kind == "ping":
                        await ws.send(json.dumps({"type": "pong"}))

            for name in data["streams"]:
                ticket = (await api.call("POST", "/api/auth/ticket"))["ticket"]
                ws = await websockets.connect(args.api.replace("http://", "ws://") + "/ws/agent?ticket=" + ticket)
                sockets.append(ws)
                readers.append(asyncio.create_task(read(name, ws)))

            async def probe(active):
                record = {"active_members_at_trigger": active, "created_at": now(), "ttft_seconds": None}
                data["probes"].append(record)
                try:
                    session = await api.call("POST", "/api/agent/session", {"agent": "build", "model": args.model,
                        "project_id": fixture["project"]["id"], "title": f"并发 {active} 成员时的普通聊天探测"})
                    record["session_id"] = session["id"]
                    record["submitted_monotonic"] = time.monotonic()
                    response = await client.post(f"/api/agent/session/{session['id']}/prompt_async", json={
                        "text": "这是普通聊天首字延迟验收。不要调用工具。只回复：容量探测成功，7+8=15。",
                        "model": args.model, "delivery": "followup", "client_message_id": str(uuid.uuid4())})
                    record["http_status"] = response.status_code
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    record["http_status"] = exc.response.status_code
                    record["http_error"] = exc.response.text[:500]
                except Exception as exc:
                    record["error"] = type(exc).__name__

            while time.monotonic() - started < args.seconds:
                async with get_db_session() as db:
                    members = (await db.execute(text("SELECT id FROM sessions WHERE parent_id=:root AND kind='team_member' AND is_deleted=false ORDER BY id"), {"root": root_id})).scalars().all()
                    live = (await db.execute(text("SELECT s.id,s.kind FROM agent_driver_states d JOIN sessions s ON s.id=d.session_id WHERE d.phase NOT IN ('idle','failed') AND d.lease_expires_at>now() AND (s.parent_id=:root OR s.id=:root)"), {"root": root_id})).all()
                    connections = dict((await db.execute(text("SELECT coalesce(state,'unknown'),count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() GROUP BY state"))).all())
                    run_row = (await db.execute(text("SELECT id,state FROM team_runs WHERE root_session_id=:root ORDER BY created_at DESC LIMIT 1"), {"root": root_id})).first()
                    live_probes = await db.scalar(text("SELECT count(*) FROM agent_driver_states WHERE phase NOT IN ('idle','failed') AND lease_expires_at>now() AND session_id=ANY(:ids)"),
                        {"ids": [probe["session_id"] for probe in data["probes"] if probe.get("session_id")]})
                if list(members) != member_ids:
                    member_ids = list(members)
                    selected = member_ids[0] if member_ids else None
                    data.update(member_ids=member_ids, selected_member=selected)
                    await sockets[0].send(json.dumps({"type": "session.subscribe", "sessionIds": member_ids}))
                    await sockets[2].send(json.dumps({"type": "session.subscribe", "sessionIds": [selected] if selected else []}))
                active = sum(kind == "team_member" for _, kind in live)
                data["samples"].append({"seconds": round(time.monotonic()-started, 3), "active_members": active,
                    "active_coordinator": int(any(sid == root_id for sid, _ in live)),
                    "rss_bytes": process.memory_info().rss, "postgres_connections": connections})
                if args.execute_probes and not live_probes and all(task.done() for task in probes) and active not in probed and active in {0, 1, 2, 3}:
                    probed.add(active)
                    probes.append(asyncio.create_task(probe(active)))
                if run_row:
                    data["run_id"], data["run_state"] = run_row
                    if run_row.state in {"completed", "canceled", "failed", "paused"}:
                        finished = finished or time.monotonic()
                save(args.output, data)
                for name, reader in zip(data["streams"], readers):
                    if reader.done():
                        raise RuntimeError(f"WebSocket reader {name} stopped before observation completed")
                if finished and time.monotonic() - finished > 20:
                    break
                await asyncio.sleep(0.5)
            await asyncio.gather(*probes)
            data["ended_at"] = now()
            data["observed_member_counts"] = dict(Counter(row["active_members"] for row in data["samples"]))
            data["member_streams"] = {sid: {"count": len(delta_times[sid]),
                "first_seconds": min(delta_times[sid])-started if delta_times[sid] else None,
                "last_seconds": max(delta_times[sid])-started if delta_times[sid] else None} for sid in member_ids}
            data["unexpected_member_deltas"] = {
                "aggregate": sum(data["streams"]["aggregate"].get(sid, 0) for sid in member_ids),
                "selected_other": sum(data["streams"]["selected"].get(sid, 0) for sid in member_ids if sid != selected)}
            save(args.output, data)
            print(json.dumps({key: data.get(key) for key in ("run_id", "run_state", "observed_member_counts", "unexpected_member_deltas")}))
    finally:
        for ws in sockets:
            await ws.close()
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        await close_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://127.0.0.1:8080")
    parser.add_argument("--account-file", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--seconds", type=int, default=1200)
    parser.add_argument("--execute-probes", action="store_true")
    parser.add_argument("--model", default="openai/gemini-3.8-flash-high")
    asyncio.run(run(parser.parse_args()))
