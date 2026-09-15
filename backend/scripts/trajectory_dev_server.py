"""Isolated admin trajectory UI server: the business app with its embedded trajectory worker.

Run from backend with --data-dir pointing at a disposable directory. The business
database, the trace database, the spool and the trajectory blobs all live there;
no .env, real model, paid sandbox, production database or scheduled workers are
used. Credentials are generated into a mode-0600 file, never printed in the log.

The app is ``main.create_app()`` in embedded worker mode with a lifespan that
initializes only the database, cache and auth, then starts trajectory recording
as the real lifespan does: the spool emitter, metadata sync and the embedded
worker. The fixture session (trajectory/fixtures/session_v1.json) is emitted
through the spool once per owner, and the worker ingests and projects it like
any recorded session. Reusing a data-dir keeps its data; a new one starts empty.
"""
from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sys

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

FIXTURE = BACKEND_DIR / "trajectory" / "fixtures" / "session_v1.json"
OWNERS = ("user_a", "user_b")
#: The fixture's root session; its other sessions are sub-agents of it.
FIXTURE_ROOT = "session_a"
#: Fixture fields the worker assigns, which a producer never sends.
WORKER_FIELDS = frozenset({"trajectory_id", "seq", "recorded_at", "session_id", "user_id", "version"})


def load_credentials(data_dir: Path) -> dict:
    """The server's logins and JWT secret, generated once into data_dir/credentials.json (mode 0600)."""
    data_dir.mkdir(parents=True, exist_ok=True)
    credential_file = data_dir / "credentials.json"
    if credential_file.exists():
        return json.loads(credential_file.read_text())
    credentials = {key: {"username": f"trajectory_{key}", "password": secrets.token_urlsafe(24)}
                   for key in ("admin", *OWNERS)}
    credentials["jwt_secret"] = secrets.token_urlsafe(48)
    credential_file.write_text(json.dumps(credentials))
    credential_file.chmod(0o600)
    return credentials


def dev_environment(data_dir: Path) -> dict[str, str]:
    """The trajectory settings of the server: recording for everyone, an embedded worker, all state in data_dir."""
    return {
        "TRAJECTORY_RECORDING_ENABLED": "true",
        "TRAJECTORY_ADMIN_ENABLED": "true",
        "TRAJECTORY_RECORD_USER_IDS": "",
        "TRAJECTORY_ADMIN_USER_IDS": "",
        # JWT_SECRET would otherwise select an external worker.
        "TRAJECTORY_WORKER_MODE": "embedded",
        "TRAJECTORY_SPOOL_DIR": str(data_dir / "trajectory-spool"),
        "TRAJECTORY_DATABASE_URL": f"sqlite+aiosqlite:///{data_dir / 'trajectory.sqlite3'}",
        "TRAJECTORY_BLOB_PROVIDER": "local",
        "TRAJECTORY_BLOB_LOCAL_PATH": str(data_dir / "trajectory-blobs"),
        "TRAJECTORY_META_SYNC_SECONDS": "2",
    }


def session_id(owner: str, source: str) -> str:
    return f"session_{owner}" if source == FIXTURE_ROOT else f"{owner}_{source}"


async def seed_business(credentials: dict, fixture: dict) -> dict:
    """The admin and both owners, each owner with the fixture's root session and its sub-agent sessions."""
    from sqlalchemy import select

    from auth.password import hash_password
    from db.base import get_db_session
    from db.models.project import Project
    from db.models.session import Session
    from db.repository.user_repo import PgUserRepo

    repo = PgUserRepo()
    users = {}
    for key in ("admin", *OWNERS):
        login = credentials[key]
        user = await repo.get_by_username(login["username"])
        if user is None:
            user = await repo.create(id=key, username=login["username"],
                                     password_hash=hash_password(login["password"]),
                                     role="admin" if key == "admin" else "user")
        users[key] = user
    sources = [FIXTURE_ROOT, *sorted({event["source_session_id"] for event in fixture["events"]} - {FIXTURE_ROOT})]
    now = datetime.now(timezone.utc)
    for owner in OWNERS:
        root = session_id(owner, FIXTURE_ROOT)
        async with get_db_session() as db:
            if await db.get(Session, root) is not None:
                continue
            project = await db.scalar(select(Project).where(Project.user_id == owner))
            for source in sources:
                sid = session_id(owner, source)
                db.add(Session(id=sid, user_id=owner, workspace_id=users[owner]["default_workspace_id"],
                               project_id=project.id,
                               title=f"{owner} · 旧会话续聊与工具回放" if sid == root else "子 Agent",
                               parent_id=None if sid == root else root, agent="build", model="fixture/model",
                               status="idle", created_at=now, updated_at=now))
    return users


async def emit_fixture(data_dir: Path, fixture: dict, users: dict) -> None:
    """Emit the fixture session for both owners through the spool, once per data-dir."""
    from trajectory.context import TraceContext
    from trajectory.emitter import emit, flush_spool

    marker = data_dir / "fixture-emitted"
    if marker.exists():
        return
    for owner in OWNERS:
        context = TraceContext(user_id=owner, session_id=session_id(owner, FIXTURE_ROOT),
                               workspace_id=users[owner]["default_workspace_id"])
        for original in fixture["events"]:
            if original["type"] == "trajectory.started":
                continue  # the worker starts every trajectory itself
            ids = {key: value for key, value in original.items()
                   if key not in WORKER_FIELDS | {"type", "data", "event_id", "occurred_at"}}
            ids["source_session_id"] = session_id(owner, original["source_session_id"])
            emit(original["type"], original["data"], context=context, event_id=f"{owner}_{original['event_id']}",
                 occurred_at=datetime.fromisoformat(original["occurred_at"].replace("Z", "+00:00")), **ids)
        emit("artifact.recorded", {
            "artifact_id": "file:/workspace/report.md", "artifact_type": "file_diff", "name": "report.md",
            "path": "/workspace/report.md", "operation": "edit",
            "before": {"availability": "available", "text": "状态：草稿\n"},
            "after": {"availability": "available", "text": "状态：已完成\n"},
            "diff": "--- /workspace/report.md\n+++ /workspace/report.md\n@@ -1 +1 @@\n-状态：草稿\n+状态：已完成\n",
            "media_type": "text/plain", "availability": "available", "capture_level": "executor_content",
        }, context=context.derive(call_id=f"{owner}_file_call"), event_id=f"{owner}_file_change")
    await flush_spool()
    marker.write_text(datetime.now(timezone.utc).isoformat())


def make_app(data_dir: Path):
    credentials = load_credentials(data_dir)
    os.environ.update(dev_environment(data_dir))
    from core import config as config_module
    config = config_module.OpenBoxConfig(
        jwt_secret=credentials["jwt_secret"], app_env="dev", blob_provider="local",
        blob_local_path=str(data_dir / "blobs"),
        database_url=f"sqlite+aiosqlite:///{data_dir / 'acceptance.sqlite3'}",
        cors_origins=["http://127.0.0.1:3101", "http://localhost:3101"],
        public_base_url="http://127.0.0.1:3101", model="fixture/model",
    )
    config_module._config = config
    import main
    app = main.create_app()

    @asynccontextmanager
    async def acceptance_lifespan(application):
        from auth import setup_auth
        from cache import set_cache
        from cache.memory_cache import MemoryCache
        from db.base import Base, close_engine, init_engine
        import db.models  # noqa: F401  (registers the business tables)

        engine = init_engine(config.database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        cache = MemoryCache()
        set_cache(cache)
        setup_auth(config, cache)
        fixture = json.loads(FIXTURE.read_text())
        users = await seed_business(credentials, fixture)
        # The real lifespan's trajectory start: the emitter, metadata sync and the embedded worker.
        await main._start_trajectory(application, "embedded")
        try:
            await emit_fixture(data_dir, fixture, users)
            # Use the actual routed application and auth, while refusing sandbox
            # provisioning in this local fixture process even if a chat WS opens.
            import api.ws

            async def no_sandbox(_user_id):
                return None

            api.ws._ensure_user_container = no_sandbox
            print(f"Trajectory acceptance server ready; credentials file: {data_dir / 'credentials.json'}", flush=True)
            yield
        finally:
            try:
                await main._shutdown_trajectory("embedded")
            finally:
                await close_engine()

    app.router.lifespan_context = acceptance_lifespan
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8091)
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(make_app(args.data_dir.resolve()), host="127.0.0.1", port=args.port, access_log=False)
