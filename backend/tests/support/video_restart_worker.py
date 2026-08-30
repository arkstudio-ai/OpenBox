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
PRODUCTION_ID = "restart-e2e-production"
SEGMENT_ID = "restart-e2e-segment"
MODEL = "restart-e2e-seedance"
IDEMPOTENCY_KEY = f"{PRODUCTION_ID}:{SEGMENT_ID}:generate"
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

    # execute_generate only needs the declared-model list after route resolution.
    # Supplying a minimal, local config also prevents this E2E from consulting a
    # developer's real provider configuration.
    import core.config

    core.config.get_config = lambda: SimpleNamespace(
        video_generation=SimpleNamespace(models=[])
    )
    return video, target


async def _open_database(path: Path, *, create: bool) -> None:
    from db.base import Base, init_engine

    engine = init_engine(f"sqlite+aiosqlite:///{path}")
    import db.models  # noqa: F401

    if create:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)


async def _seed_approved_segment() -> None:
    from db.base import get_db_session
    from db.models.user import User
    from db.models.video_production import VideoApproval, VideoProduction, VideoSegment
    from tool.video_workflow import content_hash, spend_scope

    now = datetime.now(timezone.utc)
    script = "重启恢复验收。"
    script_hash = content_hash({"script_text": script})
    production = VideoProduction(
        id=PRODUCTION_ID,
        user_id=USER_ID,
        session_id=None,
        project_id=None,
        title="进程级恢复验收",
        brief="仅调用本地 mock provider",
        mode="standard",
        status="spend_ok",
        target_duration_seconds=5,
        ratio="9:16",
        resolution="720p",
        quality_policy="required",
        subtitles=None,
        channel_name="",
        visual_anchor="固定镜头",
        character_asset_id=None,
        character_reference_type="virtual",
        character_identity_id=None,
        script_text=script,
        script_hash=script_hash,
        plan_hash="restart-e2e-plan",
        render_asset_id=None,
        error=None,
        created_at=now,
        updated_at=now,
        completed_at=None,
    )
    segment = VideoSegment(
        id=SEGMENT_ID,
        production_id=PRODUCTION_ID,
        ordinal=1,
        revision=1,
        is_active=True,
        role="body",
        script_text=script,
        prompt="固定镜头，中景，主持人自然说：@重启恢复验收。 无字幕。",
        content_hash=content_hash({"restart-e2e-segment": 1}),
        model=MODEL,
        input_asset_ids=[],
        lint_data={"ok": True},
        status="planned",
        generation_job_id=None,
        output_asset_id=None,
        transcript_text=None,
        transcript_data={},
        stt_similarity=None,
        stt_verdict=None,
        stt_notes=[],
        stt_checked_at=None,
        review_status=None,
        review_note=None,
        created_at=now,
        updated_at=now,
    )
    approvals = [
        VideoApproval(
            id="restart-e2e-script-approval",
            production_id=PRODUCTION_ID,
            user_id=USER_ID,
            session_id=None,
            kind="script",
            scope_hash=script_hash,
            decision="approved",
            answer="测试批准",
            max_calls=None,
            used_calls=0,
            evidence_message_id=None,
            evidence_part_id=None,
            metadata_data={},
            created_at=now,
        ),
        VideoApproval(
            id="restart-e2e-segments-approval",
            production_id=PRODUCTION_ID,
            user_id=USER_ID,
            session_id=None,
            kind="segments",
            scope_hash=production.plan_hash,
            decision="approved",
            answer="测试批准",
            max_calls=None,
            used_calls=0,
            evidence_message_id=None,
            evidence_part_id=None,
            metadata_data={},
            created_at=now,
        ),
        VideoApproval(
            id="restart-e2e-spend-approval",
            production_id=PRODUCTION_ID,
            user_id=USER_ID,
            session_id=None,
            kind="spend",
            scope_hash=spend_scope(production, [segment]),
            decision="approved",
            answer="仅限本地 mock",
            max_calls=1,
            used_calls=0,
            evidence_message_id=None,
            evidence_part_id=None,
            metadata_data={},
            created_at=now,
        ),
    ]
    async with get_db_session() as db:
        db.add(
            User(
                id=USER_ID,
                username=USER_ID,
                email=None,
                password_hash=None,
                avatar_url=None,
                role="user",
                is_active=True,
                oauth_provider=None,
                oauth_id=None,
                failed_login_count=0,
                locked_until=None,
                monthly_cost_limit=None,
                is_deleted=False,
                deleted_at=None,
                created_at=now,
                updated_at=now,
            )
        )
        db.add(production)
        db.add(segment)
        db.add_all(approvals)


async def _snapshot() -> dict:
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from db.models.video_job import VideoJob
    from db.models.video_production import VideoApproval, VideoSegment

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
        segment = await db.get(VideoSegment, SEGMENT_ID)
        asset = await db.get(FileAsset, job.output_asset_id)
        spend = await db.get(VideoApproval, "restart-e2e-spend-approval")
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
            "segment_status": segment.status,
            "segment_job_id": segment.generation_job_id,
            "segment_asset_id": segment.output_asset_id,
            "asset_status": asset.status,
            "asset_size": asset.size,
            "spend_used_calls": spend.used_calls,
        }


async def _submit(database: Path, provider_url: str) -> dict:
    await _open_database(database, create=True)
    await _seed_approved_segment()
    video, _target = _patch_runtime(provider_url)

    from tool.tool import ToolContext

    result = await video.execute_generate(
        video.VideoGenerateArgs(
            action="submit",
            production_id=PRODUCTION_ID,
            segment_id=SEGMENT_ID,
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
            production_id=PRODUCTION_ID,
            segment_id=SEGMENT_ID,
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
            production_id=PRODUCTION_ID,
            segment_id=SEGMENT_ID,
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
