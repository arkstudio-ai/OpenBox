"""Subprocess worker for the direct-video restart recovery integration test.

This is intentionally a test helper rather than a pytest module.  Each phase
runs in a fresh interpreter so no coroutine, module global, or monkeypatch can
accidentally bridge the simulated backend restart.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit


USER_ID = "restart-e2e-user"
MODEL = "restart-e2e-seedance"
PROMPT = "restart-e2e shot: a locked-off half-body presenter"
IDEMPOTENCY_KEY = "restart-e2e:shot1:v1"
TASK_ID = "mock-provider-task-1"
RESULT_PREFIX = "RESTART_E2E_RESULT="


def _assert_loopback(url: str) -> None:
    """Make an accidental real-provider call impossible in this helper."""
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("restart E2E provider must be a loopback HTTP endpoint")


def _install_loopback_network_guard() -> None:
    """Fail closed if production code tries any non-loopback socket."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def checked_address(sock: socket.socket, address) -> None:
        if sock.family not in {socket.AF_INET, socket.AF_INET6}:
            return
        host = str(address[0])
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = host == "localhost"
        if not is_loopback:
            raise RuntimeError(f"restart E2E blocked non-loopback connection to {host}")

    def guarded_connect(sock: socket.socket, address):
        checked_address(sock, address)
        return original_connect(sock, address)

    def guarded_connect_ex(sock: socket.socket, address):
        checked_address(sock, address)
        return original_connect_ex(sock, address)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex


def _patch_runtime(provider_url: str):
    from tool import video_production as video
    from tool.video_providers import VideoRoute

    _assert_loopback(provider_url)
    target = VideoRoute(
        provider="restart-e2e-mock",
        model=MODEL,
        api_key="restart-e2e-secret",
        base_url=provider_url.rstrip("/"),
        submit_timeout_seconds=5,
        status_timeout_seconds=5,
        channel="ark",
        model_type="seedance",
        auth_scheme="bearer",
        wire_format="tokenspace_contents",
    )
    settings = SimpleNamespace(
        dedupe=False,
        max_provider_output_bytes=1024 * 1024,
        poll_interval_seconds=0.01,
        provider_input_url_ttl_seconds=60,
    )
    video._configured_target = lambda model_override=None: (target, settings)

    # A minimal local config: the declared-model list plus the defaults an
    # open request falls back to. Supplying it here also keeps this E2E from
    # consulting a developer's real provider configuration.
    import core.config

    core.config.get_config = lambda: SimpleNamespace(
        video_generation=SimpleNamespace(
            models=[],
            model=MODEL,
            default_resolution="720p",
            default_ratio="9:16",
            default_duration=-1,
            default_generate_audio=True,
            default_watermark=False,
            daily_job_limit=0,
            refuse_duplicate_in_flight=False,
            dedupe=False,
            provider_input_url_ttl_seconds=60,
        )
    )
    return video, target


async def _open_database(path: Path, *, create: bool) -> None:
    from db.base import Base, init_engine

    engine = init_engine(f"sqlite+aiosqlite:///{path}")
    import db.models  # noqa: F401

    if create:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)


async def _snapshot() -> dict:
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from db.models.video_job import VideoJob

    async with get_db_session() as db:
        job = (
            await db.execute(
                select(VideoJob).where(
                    VideoJob.user_id == USER_ID,
                    VideoJob.kind == "segment",
                    VideoJob.idempotency_key == IDEMPOTENCY_KEY,
                )
            )
        ).scalar_one()
        asset = await db.get(FileAsset, job.output_asset_id)
        return {
            "pid": os.getpid(),
            "job_id": job.id,
            "job_status": job.status,
            "provider_task_id": job.provider_task_id,
            "idempotency_key": job.idempotency_key,
            "attempt": job.attempt,
            "job_asset_id": job.output_asset_id,
            "route_fingerprint": (job.request_data or {}).get(
                "provider_route_fingerprint"
            ),
            "asset_status": asset.status,
            "asset_size": asset.size,
        }


async def _submit(database: Path, provider_url: str) -> dict:
    await _open_database(database, create=True)
    video, _target = _patch_runtime(provider_url)

    from tool.tool import ToolContext

    result = await video.execute_generate(
        video.VideoGenerateArgs(
            action="submit",
            prompt=PROMPT,
            model=MODEL,
            idempotency_key=IDEMPOTENCY_KEY,
        ),
        ToolContext(user_id=USER_ID),
    )
    snapshot = await _snapshot()
    snapshot.update(
        {
            "phase": "submit",
            "tool_status": result.metadata.get("status"),
        }
    )
    return snapshot


async def _recover(database: Path, provider_url: str) -> dict:
    await _open_database(database, create=False)
    video, _target = _patch_runtime(provider_url)

    # Model a reconnect racing startup recovery: the persisted in-progress row
    # must win before the sweep has had a chance to poll the provider.
    from tool.tool import ToolContext

    replay_before_recovery = await video.execute_generate(
        video.VideoGenerateArgs(
            action="submit",
            prompt=PROMPT,
            model=MODEL,
            idempotency_key=IDEMPOTENCY_KEY,
        ),
        ToolContext(user_id=USER_ID),
    )

    # The integration boundary under test is provider polling plus durable DB
    # finalization. OSS byte transfer is orthogonal and must not leave loopback.
    async def local_copy(url, _oss, _key, _max_bytes):
        if url != "https://mock.invalid/recovered.mp4":
            raise AssertionError(f"unexpected mock result URL: {url}")
        return 777

    video._copy_provider_video_to_oss = local_copy
    import core.oss

    core.oss.get_oss = lambda: object()

    from video import job_recovery

    # Avoid making the test sleep for the production two-minute staleness
    # window.  A negative threshold means the just-restarted process owns the
    # row immediately; it does not alter any production state transition.
    job_recovery.STALE_AFTER_SECONDS = -1
    job_recovery.schedule_startup_recovery()
    await job_recovery._startup_task
    after_startup = await _snapshot()

    # A client can retry the same submit after reconnecting.  The exhausted
    # approval and completed row must still resolve to the existing job without
    # another provider POST.
    replay = await video.execute_generate(
        video.VideoGenerateArgs(
            action="submit",
            prompt=PROMPT,
            model=MODEL,
            idempotency_key=IDEMPOTENCY_KEY,
        ),
        ToolContext(user_id=USER_ID),
    )
    second_sweep = await job_recovery.sweep()
    final = await _snapshot()
    final.update(
        {
            "phase": "recover",
            "pre_recovery_replay_status": replay_before_recovery.metadata.get(
                "status"
            ),
            "pre_recovery_replay_idempotent_reuse": (
                replay_before_recovery.metadata.get("idempotent_reuse")
            ),
            "startup_job_status": after_startup["job_status"],
            "replay_status": replay.metadata.get("status"),
            "replay_idempotent_reuse": replay.metadata.get("idempotent_reuse"),
            "second_sweep": second_sweep,
        }
    )
    return final


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("submit", "recover"))
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--provider-url", required=True)
    args = parser.parse_args()
    _install_loopback_network_guard()
    result = (
        await _submit(args.database, args.provider_url)
        if args.phase == "submit"
        else await _recover(args.database, args.provider_url)
    )
    from db.base import close_engine

    await close_engine()
    print(RESULT_PREFIX + json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(_main())
