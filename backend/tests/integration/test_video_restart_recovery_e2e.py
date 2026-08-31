"""Process-boundary E2E for direct video provider recovery.

No real provider or production database is reachable from this test.  A
loopback-only HTTP server records the provider protocol while two independent
Python interpreters share one temporary SQLite database.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


TASK_ID = "mock-provider-task-1"
RESULT_PREFIX = "RESTART_E2E_RESULT="


class _ProviderState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.submit_count = 0
        self.status_count = 0
        self.authorization_headers: list[str] = []


class _MockProvider(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler):
        super().__init__(address, handler)
        self.state = _ProviderState()


class _MockProviderHandler(BaseHTTPRequestHandler):
    server: _MockProvider

    def _json(self, value: dict) -> None:
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path != "/api/v3/contents/generations/tasks":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length))
        assert payload["model"] == "restart-e2e-seedance"
        with self.server.state.lock:
            self.server.state.submit_count += 1
            self.server.state.authorization_headers.append(
                self.headers.get("Authorization", "")
            )
        self._json({"id": TASK_ID, "status": "processing"})

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path != f"/api/v3/contents/generations/tasks/{TASK_ID}":
            self.send_error(404)
            return
        with self.server.state.lock:
            self.server.state.status_count += 1
            self.server.state.authorization_headers.append(
                self.headers.get("Authorization", "")
            )
        self._json(
            {
                "id": TASK_ID,
                "status": "succeeded",
                "content": {"video_url": "https://mock.invalid/recovered.mp4"},
            }
        )

    def log_message(self, _format: str, *_args) -> None:
        return


def _run_phase(backend: Path, phase: str, database: Path, provider_url: str) -> dict:
    helper = backend / "tests" / "support" / "video_restart_worker.py"
    env = os.environ.copy()
    # The helper patches provider resolution, but also scrub the conventional
    # direct-provider variables as defense in depth against future refactors.
    for name in ("DOUBAO_API_KEY", "DOUBAO_BASE_URL"):
        env.pop(name, None)
    for name in (
        "ALL_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "all_proxy",
        "https_proxy",
        "http_proxy",
    ):
        env.pop(name, None)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    env["DATABASE_URL"] = f"sqlite+aiosqlite:///{database}"
    env["OSS_BUCKET"] = ""
    completed = subprocess.run(
        [
            sys.executable,
            str(helper),
            phase,
            "--database",
            str(database),
            "--provider-url",
            provider_url,
        ],
        cwd=backend,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (
        f"{phase} subprocess failed\nstdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    result_line = next(
        (
            line[len(RESULT_PREFIX) :]
            for line in reversed(completed.stdout.splitlines())
            if line.startswith(RESULT_PREFIX)
        ),
        None,
    )
    assert result_line is not None, completed.stdout
    return json.loads(result_line)


def test_direct_video_recovers_across_process_restart_without_resubmit(tmp_path: Path):
    """Persisted provider identity, not process memory, owns recovery."""
    backend = Path(__file__).resolve().parents[2]
    database = tmp_path / "video-restart-e2e.sqlite3"
    server = _MockProvider(("127.0.0.1", 0), _MockProviderHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    provider_url = f"http://127.0.0.1:{server.server_port}"

    try:
        submitted = _run_phase(backend, "submit", database, provider_url)
        with server.state.lock:
            assert server.state.submit_count == 1
            assert server.state.status_count == 0

        assert submitted["phase"] == "submit"
        assert submitted["tool_status"] == "in_progress"
        assert submitted["job_status"] == "in_progress"
        assert submitted["provider_task_id"] == TASK_ID
        assert submitted["attempt"] == 1
        assert submitted["segment_status"] == "generating"
        assert submitted["segment_job_id"] == submitted["job_id"]
        assert submitted["route_fingerprint"].startswith("v1:")

        recovered = _run_phase(backend, "recover", database, provider_url)
        with server.state.lock:
            assert server.state.submit_count == 1
            assert server.state.status_count == 1
            assert server.state.authorization_headers == [
                "Bearer restart-e2e-secret",
                "Bearer restart-e2e-secret",
            ]

        # Different PIDs prove every process-local task/map/monkeypatch was
        # discarded between submit and startup recovery.
        assert recovered["pid"] != submitted["pid"]
        assert recovered["job_id"] == submitted["job_id"]
        assert recovered["provider_task_id"] == submitted["provider_task_id"]
        assert recovered["idempotency_key"] == submitted["idempotency_key"]
        assert recovered["route_fingerprint"] == submitted["route_fingerprint"]
        assert recovered["attempt"] == 1
        assert recovered["pre_recovery_replay_status"] == "in_progress"
        assert recovered["pre_recovery_replay_idempotent_reuse"] is True
        assert recovered["startup_job_status"] == "completed"
        assert recovered["job_status"] == "completed"
        assert recovered["segment_status"] == "generated"
        assert recovered["segment_job_id"] == recovered["job_id"]
        assert submitted["segment_asset_id"] is None
        assert recovered["segment_asset_id"] == submitted["job_asset_id"]
        assert recovered["job_asset_id"] == submitted["job_asset_id"]
        assert recovered["asset_status"] == "ready"
        assert recovered["asset_size"] == 777
        assert recovered["replay_status"] == "completed"
        assert recovered["replay_idempotent_reuse"] is True
        assert recovered["second_sweep"] == 0
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        assert not server_thread.is_alive()
