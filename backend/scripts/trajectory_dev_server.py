"""Isolated UI acceptance server using real app routes and synthetic trace data.

Run from backend with --data-dir pointing at a disposable directory. No .env,
real model, paid sandbox, production database or scheduled workers are used.
Credentials are generated into a mode-0600 file, never printed in the log.
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def make_app(data_dir: Path):
    data_dir.mkdir(parents=True, exist_ok=True)
    credential_file = data_dir / "credentials.json"
    if credential_file.exists():
        credentials = json.loads(credential_file.read_text())
    else:
        credentials = {key: {"username": f"trajectory_{key}", "password": secrets.token_urlsafe(24)}
                       for key in ("admin", "user_a", "user_b")}
        credentials["jwt_secret"] = secrets.token_urlsafe(48)
        credential_file.write_text(json.dumps(credentials))
        credential_file.chmod(0o600)

    os.environ["TRAJECTORY_RECORDING_ENABLED"] = "true"
    os.environ["TRAJECTORY_ADMIN_ENABLED"] = "true"
    os.environ["TRAJECTORY_RECORD_USER_IDS"] = ""
    os.environ["TRAJECTORY_ADMIN_USER_IDS"] = ""
    from core import config as config_module
    config = config_module.OpenBoxConfig(
        jwt_secret=credentials["jwt_secret"], app_env="dev", blob_provider="local",
        blob_local_path=str(data_dir / "blobs"),
        database_url=f"sqlite+aiosqlite:///{data_dir / 'acceptance.sqlite3'}",
        cors_origins=["http://127.0.0.1:3101", "http://localhost:3101"],
        public_base_url="http://127.0.0.1:3101", model="fixture/model",
    )
    config_module._config = config
    from main import create_app
    app = create_app()

    @asynccontextmanager
    async def acceptance_lifespan(_app):
        from auth import setup_auth
        from auth.password import hash_password
        from cache import set_cache
        from cache.memory_cache import MemoryCache
        from db.base import Base, init_engine, get_db_session, close_engine
        import db.models
        from db.repository.user_repo import PgUserRepo
        from db.models.session import Session
        from db.models.project import Project
        from sqlalchemy import select
        from trajectory import TraceContext, append_events_in_tx
        from trajectory.payload import set_storage
        from blob.local_blob import LocalBlobStorage

        engine = init_engine(config.database_url)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        cache = MemoryCache()
        set_cache(cache)
        setup_auth(config, cache)
        set_storage(LocalBlobStorage(config.blob_local_path))
        repo = PgUserRepo()
        users = {}
        for key in ("admin", "user_a", "user_b"):
            login = credentials[key]
            user = await repo.get_by_username(login["username"])
            if user is None:
                user = await repo.create(id=key, username=login["username"],
                                         password_hash=hash_password(login["password"]),
                                         role="admin" if key == "admin" else "user")
            users[key] = user
        fixture = json.loads((Path(__file__).resolve().parents[1] / "trajectory/fixtures/session_v1.json").read_text())
        now = datetime.now(timezone.utc)
        for owner in ("user_a", "user_b"):
            sid = f"session_{owner}"
            async with get_db_session() as db:
                if await db.get(Session, sid):
                    continue
                workspace_id = users[owner]["default_workspace_id"]
                project = await db.scalar(select(Project).where(Project.user_id == owner))
                sources = {event["source_session_id"] for event in fixture["events"]}
                mapping = {source: sid if source == "session_a" else f"{owner}_{source}"
                           for source in sources}
                mapping["session_a"] = sid
                for original, source in mapping.items():
                    db.add(Session(id=source, user_id=owner, workspace_id=workspace_id, project_id=project.id,
                                   title=f"{owner} · 旧会话续聊与工具回放" if source == sid else "子 Agent",
                                   parent_id=None if source == sid else sid, agent="build", model="fixture/model",
                                   status="idle", created_at=now, updated_at=now))
                await db.flush()
                events = []
                for original in fixture["events"]:
                    if original["type"] == "trajectory.started":
                        continue  # Recorder owns the true recording start.
                    event = {key: value for key, value in original.items()
                             if key not in {"trajectory_id", "seq", "recorded_at", "session_id", "user_id"}}
                    event["event_id"] = f"{owner}_{original['event_id']}"
                    event["source_session_id"] = mapping[original["source_session_id"]]
                    events.append(event)
                await append_events_in_tx(db, TraceContext(user_id=owner, session_id=sid,
                                                           workspace_id=workspace_id), events)
                await append_events_in_tx(db, TraceContext(user_id=owner, session_id=sid,
                                                           workspace_id=workspace_id, call_id=f"{owner}_file_call"), [{
                    "event_id": f"{owner}_file_change", "type": "artifact.recorded",
                    "data": {"artifact_id": "file:/workspace/report.md", "artifact_type": "file_diff",
                             "name": "report.md", "path": "/workspace/report.md", "operation": "edit",
                             "before": {"availability": "available", "text": "状态：草稿\n"},
                             "after": {"availability": "available", "text": "状态：已完成\n"},
                             "diff": "--- /workspace/report.md\n+++ /workspace/report.md\n@@ -1 +1 @@\n-状态：草稿\n+状态：已完成\n",
                             "media_type": "text/plain", "availability": "available", "capture_level": "executor_content"},
                }])
        # Use the actual routed application and auth, while refusing sandbox
        # provisioning in this local fixture process even if a chat WS opens.
        import api.ws
        async def no_sandbox(_user_id):
            return None
        api.ws._ensure_user_container = no_sandbox
        print(f"Trajectory acceptance server ready; credentials file: {credential_file}", flush=True)
        try:
            yield
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
