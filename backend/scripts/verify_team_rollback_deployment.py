"""Boot an untouched pre-team checkout against retained, closed team rows.

Requires two distinct, migrated loopback test databases. Unknown work remains
in the compatible database; the old application receives only the dedicated
rollback database. An HTTP model fixture replaces external providers. The
old app, its startup recovery, API, Driver and loop run unchanged. Every row,
log and sandbox is retained. No production configuration is loaded.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from aiohttp import web
from jose import jwt
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

from db.base import close_engine, get_db_session, init_engine
from db.models.agent_driver import AgentDriverState
from db.models.external_effect import ExternalEffect
from db.models.part import Part
from db.models.session import Session
from db.models.team import TeamEvent, TeamRun
from db.models.workspace import WorkspaceMember
from scripts.verify_team_recovery import configure, verify_remote_pause
from scripts.verify_team_rollback import retained
from team import scheduler
from team.journal import Actor, snapshot, utcnow
from team.service import control, start_confirmed_locked
from team.errors import TeamError
from agent_catalog.compiler import compile_agent
from agent_catalog.schemas import AgentSpec, TeamPolicy
from core import config as config_module


def local_database(value: str):
    url = make_url(value)
    if url.drivername != "postgresql+asyncpg" or url.host not in {"127.0.0.1", "localhost"} or not any(
        token in (url.database or "") for token in ("test", "check")
    ):
        raise ValueError("An explicit migrated loopback PostgreSQL test/check database is required")
    return url


async def inspect_isolated(database_url: str, run_id: str):
    init_engine(database_url)
    try:
        row = await retained(run_id)
        state = row["state"]
        assert state["run"]["state"] == "paused"
        assert not any(attempt["state"] == "running" for attempt in state["attempts"].values())
        reserved = [item for item in state["reservations"].values() if item["state"] == "reserved"]
        assert reserved, "The isolated fixture must retain its unknown reservation"
        async with get_db_session() as db:
            effects = [await db.get(ExternalEffect, item["external_id"]) for item in reserved]
            assert all(effect and effect.state == "manual_review" for effect in effects)
            from team.runtime import runtime
            assert not any(item.live for item in (await runtime.observe(db, state["members"], row["actor"].owner_user_id)).values())
        return {"run_id": run_id, "database": local_database(database_url).database,
            "counts": row["counts"], "state": "paused", "reservations": reserved,
            "events_sha256": hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()}
    finally:
        await close_engine()


async def prepare_closed(database_url: str):
    init_engine(database_url)
    async with get_db_session() as db:
        # The old binary must not discover somebody else's active work.
        active = await db.scalar(select(func.count()).select_from(TeamRun).where(TeamRun.state.not_in(("completed", "failed", "canceled"))))
        pending = await db.scalar(text("SELECT count(*) FROM agent_inbox_items WHERE state IN ('accepted', 'claimed')"))
        assert not active and not pending, "Use a dedicated database with no active work"
    paused = await verify_remote_pause()
    known = await retained(paused["run_id"])
    config = config_module.get_config()
    for field in ("team_admission_enabled", "team_generated_members_enabled", "team_tools_enabled", "team_ui_enabled"):
        setattr(config, field, False)
    compiled = compile_agent(AgentSpec(name="Rollback fixture", description="Fixture only", when_to_use="Rollback",
        instruction="No real provider calls", default_model="openai/test", tool_allowlist=[]), config=config, role="coordinator")
    async with get_db_session() as db:
        root = await db.get(Session, known["state"]["run"]["root_session_id"])
        try:
            await start_confirmed_locked(db, root=root, question_id="rollback-no-admission", title="Must reject",
                goal="Must reject", policy=TeamPolicy(), grant={}, coordinator=compiled, members=[])
        except TeamError as exc:
            assert exc.code == "TEAM_ADMISSION_DISABLED"
        else:
            raise AssertionError("Admission gate allowed another run")
    assert (await retained(paused["run_id"]))["counts"] == known["counts"]
    actor = replace(known["actor"], kind="user")
    await control(paused["run_id"], actor, "rollback-deployment-cancel", "cancel", known["state"]["run"]["revision"])
    await scheduler.tick(paused["run_id"], actor)
    closed = await retained(paused["run_id"])
    assert closed["state"]["run"]["state"] == "canceled"
    assert not any(item["state"] == "running" for item in closed["state"]["attempts"].values())
    now = utcnow()
    async with get_db_session() as db:
        db.add(WorkspaceMember(workspace_id=actor.workspace_id, user_id=actor.owner_user_id, role="owner",
            status="active", created_at=now, updated_at=now))
        root = await db.get(Session, closed["state"]["run"]["root_session_id"])
        assert root.agent == "build"
    return closed


class Gateway:
    def __init__(self):
        self.requests = 0

    async def complete(self, request):
        body = await request.json()
        self.requests += 1
        base = {"id": "chatcmpl-fixture-" + uuid.uuid4().hex, "created": int(time.time()), "model": body.get("model", "team-fixture")}
        answer = "ROLLBACK_ROOT_OK"
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if not body.get("stream"):
            return web.json_response({**base, "object": "chat.completion", "choices": [
                {"index": 0, "message": {"role": "assistant", "content": answer}, "finish_reason": "stop"}], "usage": usage})
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for choice in ({"index": 0, "delta": {"role": "assistant", "content": answer}, "finish_reason": None},
                       {"index": 0, "delta": {}, "finish_reason": "stop"}):
            await response.write(("data: " + json.dumps({**base, "object": "chat.completion.chunk", "choices": [choice]}) + "\n\n").encode())
        await response.write(("data: " + json.dumps({**base, "object": "chat.completion.chunk", "choices": [], "usage": usage}) + "\n\ndata: [DONE]\n\n").encode())
        await response.write_eof()
        return response


def launcher_source(legacy: Path, config: dict):
    return f'''import os, sys
from pathlib import Path
legacy = Path({str(legacy)!r})
sys.path.insert(0, str(legacy / "backend"))
from core import config as cfg
cfg._config = cfg.OpenBoxConfig(**{config!r})
import main, agent.loop
assert Path(main.__file__).is_relative_to(legacy)
assert Path(agent.loop.__file__).is_relative_to(legacy)
import uvicorn
uvicorn.run(main.app, host="127.0.0.1", port={config["port"]}, log_level="warning")
'''


async def run(args):
    url, isolated_url = local_database(args.database_url), local_database(args.isolated_database_url)
    assert url != isolated_url, "Unknown work must be routed to a separate compatible database"
    legacy = args.legacy_root.resolve()
    assert (legacy / "backend/main.py").is_file() and not (legacy / "backend/.env").exists()
    source = (legacy / "backend/agent/subagent_authority.py").read_bytes()
    assert b"from team.runtime_binding import load_binding" not in source
    assert not subprocess.check_output(["git", "status", "--porcelain"], cwd=legacy).strip(), "The baseline checkout must be untouched"
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=legacy, text=True).strip()
    configure()
    isolated_before = await inspect_isolated(args.isolated_database_url, args.isolated_run)
    os.environ["TEAM_TEST_DATABASE_URL"] = args.database_url
    closed = await prepare_closed(args.database_url)
    root_id = closed["state"]["run"]["root_session_id"]
    member_id = next(mid for mid, member in closed["state"]["members"].items() if member["role"] == "member")
    actor = closed["actor"]
    before = closed["counts"]
    await close_engine()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    secret = uuid.uuid4().hex
    config = {"model": "openai/team-fixture", "models": [{"id": "openai/team-fixture"}],
        "provider": {"openai": {"api_key": "local-fixture", "base_url": f"http://127.0.0.1:{args.gateway_port}/v1"}},
        "database_url": args.database_url, "redis_url": args.redis_url,
        "jwt_secret": secret, "host": "127.0.0.1", "port": args.port, "app_env": "dev",
        "blob_provider": "local", "blob_local_path": str(args.output_dir / "blobs"),
        "sandbox_provider": "docker", "sandbox_image": args.sandbox_image,
        "container_name_prefix": "openbox-team-rollback-" + root_id[-10:] + "-",
        "rate_limit_api": "1000/minute"}
    launcher = args.output_dir / ("legacy-launch-" + root_id[-10:] + ".py")
    launcher.write_text(launcher_source(legacy, config))
    launcher.chmod(0o600)
    environment = {key: value for key, value in os.environ.items() if key in {
        "PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "DOCKER_HOST", "SSL_CERT_FILE", "USER"}}
    environment.update(TRAJECTORY_WORKER_MODE="off", TRAJECTORY_RECORDING_ENABLED="false",
        TRAJECTORY_ADMIN_ENABLED="false", BILLING_MODE="off", OPENBOX_CONFIG="", OPENBOX_CONFIG_CONTENT="")
    gateway = Gateway()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", gateway.complete)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", args.gateway_port).start()
    log_path = args.output_dir / ("legacy-server-" + root_id[-10:] + ".log")
    log = log_path.open("w")
    process = await asyncio.create_subprocess_exec(sys.executable, str(launcher), cwd=args.output_dir,
        env=environment, stdout=log, stderr=log)
    try:
        token = jwt.encode({"sub": actor.owner_user_id, "role": "user", "type": "access", "client": "web",
            "jti": uuid.uuid4().hex, "exp": int(time.time()) + 600}, secret, algorithm="HS256")
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{args.port}", timeout=60,
            headers={"Authorization": "Bearer " + token, "X-Workspace-Id": actor.workspace_id}) as client:
            for _ in range(120):
                if process.returncode is not None:
                    raise RuntimeError("Legacy app exited; inspect " + str(log_path))
                try:
                    response = await client.get("/health")
                    if response.status_code == 200:
                        break
                except httpx.ConnectError:
                    pass
                await asyncio.sleep(0.25)
            else:
                raise AssertionError("Legacy readiness did not pass")
            response = await client.get(f"/api/agent/session/{root_id}")
            response.raise_for_status()
            assert response.json()["agent"] == "build"
            assert (await client.get("/api/team-runs")).status_code == 404
            assert gateway.requests == 0, "Startup recovery must not dispatch closed work"
            response = await client.post(f"/api/agent/session/{member_id}/prompt_async", json={
                "text": "Must be rejected before provider execution", "delivery": "followup",
                "client_message_id": "legacy-member-refusal", "model": "openai/team-fixture"})
            # A pre-team HTTP route may accept an Inbox item; the authority
            # guard must refuse its execution before any provider request.
            assert response.status_code in {200, 202, 403, 409}, response.text
            init_engine(args.database_url)
            for _ in range(100):
                async with get_db_session() as db:
                    member = await db.get(Session, member_id)
                    driver = await db.get(AgentDriverState, member_id)
                inactive = driver is None or driver.phase in {"idle", "failed"} or (
                    driver.lease_expires_at is not None and driver.lease_expires_at <= utcnow())
                if member.status == "error" and inactive:
                    break
                await asyncio.sleep(0.1)
            else:
                raise AssertionError("Legacy member did not reach a refused terminal state")
            assert gateway.requests == 0, "Legacy member contacted a provider"
            assert "parented Agent Session has no durable subagent authority descriptor" in log_path.read_text()
            response = await client.post(f"/api/agent/session/{root_id}/prompt_async", json={
                "text": "Reply ROLLBACK_ROOT_OK", "delivery": "followup", "client_message_id": "legacy-root-fallback",
                "model": "openai/team-fixture"})
            response.raise_for_status()
            for _ in range(600):
                async with get_db_session() as db:
                    parts = list((await db.scalars(select(Part).where(Part.session_id == root_id, Part.type == "text"))).all())
                    root = await db.get(Session, root_id)
                if any(part.data.get("text") == "ROLLBACK_ROOT_OK" for part in parts) and root.status == "idle":
                    break
                await asyncio.sleep(0.1)
            else:
                raise AssertionError("Legacy ordinary root did not finish; inspect " + str(log_path))
            after = await retained(closed["run_id"])
            for field in ("runs", "sessions", "events"):
                assert after["counts"][field] == before[field], (field, before, after["counts"])
            async with get_db_session() as db:
                migration = await db.scalar(text("SELECT version_num FROM alembic_version"))
            assert migration == "e2b4d6f8a0c1"
            await close_engine()
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 30)
            except TimeoutError:
                process.kill()
                await process.wait()
        log.close()
        await runner.cleanup()
        await close_engine()
    isolated_after = await inspect_isolated(args.isolated_database_url, args.isolated_run)
    assert isolated_after == isolated_before, "The isolated unknown work changed during legacy execution"
    report = {"status": "passed", "legacy_revision": revision, "legacy_authority_sha256": hashlib.sha256(source).hexdigest(),
        "legacy_checkout": str(legacy), "legacy_started_with_recovery": True, "readiness": 200,
        "admission_disabled": True, "new_team_route": 404, "run_id": closed["run_id"], "root_session_id": root_id,
        "member_session_id": member_id, "legacy_member_provider_calls": 0, "legacy_member_refusal": "missing durable authority descriptor; expired recovery marker retained",
        "legacy_root_answer": "ROLLBACK_ROOT_OK",
        "retained_before": before, "retained_after": after["counts"], "migration_retained": migration,
        "isolated_unknown": isolated_after, "destructive_downgrade": False, "provider": "loopback scripted HTTP fixture",
        "legacy_log": str(log_path), "scope": "Full untouched old application startup, recovery, HTTP API and execution; local fixture rollout only"}
    destination = args.output_dir / "rollback-deployment-report.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    destination.chmod(0o600)
    print(json.dumps({"status": "passed", "report": str(destination), "run_id": closed["run_id"]}))


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-root", type=Path, required=True)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--isolated-database-url", required=True)
    parser.add_argument("--isolated-run", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--sandbox-image", required=True)
    parser.add_argument("--port", type=int, default=19084)
    parser.add_argument("--gateway-port", type=int, default=19085)
    args = parser.parse_args()
    try:
        await run(args)
    finally:
        await close_engine()


if __name__ == "__main__":
    asyncio.run(main())
