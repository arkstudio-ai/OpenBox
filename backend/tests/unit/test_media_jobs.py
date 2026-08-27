"""Durable WUYING media queue: serialization, waiting and composition safety."""
import asyncio
from pathlib import Path
import sys

import pytest

_CONTAINER = Path(__file__).resolve().parents[3] / "container"
sys.path.insert(0, str(_CONTAINER))

from media_jobs import MediaJobConfig, MediaJobConflict, MediaJobManager  # noqa: E402


def payload(job_id: str, key: str) -> dict:
    return {
        "job_id": job_id,
        "owner": "user-1",
        "session_id": "session-1",
        "idempotency_key": key,
        "inputs": [
            {
                "name": "segment.mp4",
                "mime": "video/mp4",
                "size": 10,
                "cache_key": "bucket:key:10",
                "url": "https://oss.example.test/input.mp4?signature=hidden",
            }
        ],
        "output": {
            "name": "final.mp4",
            "mime": "video/mp4",
            "put_url": "https://oss.example.test/output.mp4?signature=hidden",
        },
        "captions": ["测试字幕"],
        "subtitles": True,
        "width": 1080,
        "height": 1920,
    }


@pytest.mark.asyncio
async def test_queue_serializes_jobs_and_long_poll_is_bounded(tmp_path: Path):
    config = MediaJobConfig(
        state_root=str(tmp_path / "state"),
        temp_root=str(tmp_path / "temp"),
        max_concurrency=1,
        cache_ttl_seconds=3600,
    )
    manager = MediaJobManager(config)
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    active = 0
    peak = 0

    async def fake_render(job_id, _payload):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            if job_id == "render-job-0001":
                first_started.set()
                await release_first.wait()
            await asyncio.sleep(0.03)
            return {"uploaded": True, "has_audio": True, "duration_seconds": 5.0}
        finally:
            active -= 1

    manager._render = fake_render
    await manager.start()
    try:
        first = await manager.submit(payload("render-job-0001", "project-render-v1"))
        assert first["status"] == "queued"
        await asyncio.wait_for(first_started.wait(), 1)

        second = await manager.submit(payload("render-job-0002", "project-render-v2"))
        assert second["status"] == "queued"
        assert second["queue_position"] == 1

        current = await manager.get("render-job-0002", "user-1")
        unchanged = await manager.wait(
            "render-job-0002",
            "user-1",
            after_version=current["version"],
            timeout=0.03,
        )
        assert unchanged["status"] == "queued"

        release_first.set()
        for job_id in ("render-job-0001", "render-job-0002"):
            for _ in range(100):
                status = await manager.get(job_id, "user-1")
                if status["status"] == "completed":
                    break
                await asyncio.sleep(0.02)
            assert status["status"] == "completed"
            assert status["result"]["resource_check"]["temp_removed"] is True
            assert status["result"]["resource_check"]["remaining_job_processes"] == []
            async with manager._db_lock:
                stored_payload = manager._conn().execute(
                    "SELECT payload FROM media_jobs WHERE id=?", (job_id,)
                ).fetchone()[0]
            assert "signature=hidden" not in stored_payload
        assert peak == 1
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_running_command_cancel_is_reported_as_cancelled(tmp_path: Path):
    manager = MediaJobManager(
        MediaJobConfig(state_root=str(tmp_path / "state"), temp_root=str(tmp_path / "temp"))
    )

    async def slow_render(job_id, _payload):
        await manager._run_command(
            job_id,
            [sys.executable, "-c", "import time; time.sleep(30)"],
            cwd=tmp_path,
            timeout=60,
        )
        return {"uploaded": True}

    manager._render = slow_render
    await manager.start()
    try:
        submitted = await manager.submit(payload("render-job-cancel", "cancel-key"))
        for _ in range(100):
            current = await manager.get(submitted["job_id"], "user-1")
            if current["status"] == "in_progress" and manager._active_processes:
                break
            await asyncio.sleep(0.01)
        assert current["status"] == "in_progress"

        await manager.cancel(submitted["job_id"], "user-1")
        for _ in range(100):
            current = await manager.get(submitted["job_id"], "user-1")
            if current["status"] in {"cancelled", "failed"}:
                break
            await asyncio.sleep(0.01)

        assert current["status"] == "cancelled"
        assert current["result"]["resource_check"]["temp_removed"] is True
        assert current["result"]["resource_check"]["remaining_job_processes"] == []
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_submit_is_idempotent_per_owner_and_key(tmp_path: Path):
    manager = MediaJobManager(
        MediaJobConfig(state_root=str(tmp_path / "state"), temp_root=str(tmp_path / "temp"))
    )

    async def fake_render(_job_id, _payload):
        await asyncio.sleep(0.1)
        return {"uploaded": True}

    manager._render = fake_render
    await manager.start()
    try:
        original = await manager.submit(payload("render-job-1001", "same-key"))
        duplicate_payload = payload("render-job-9999", "same-key")
        duplicate = await manager.submit(duplicate_payload)
        assert duplicate["job_id"] == original["job_id"]
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_retry_replaces_expired_signed_urls_but_preserves_job_identity(tmp_path: Path):
    manager = MediaJobManager(
        MediaJobConfig(state_root=str(tmp_path / "state"), temp_root=str(tmp_path / "temp"))
    )
    original = payload("render-job-2001", "stable-key")

    async def fail_without_network(_job_id, _payload):
        raise RuntimeError("synthetic render failure")

    manager._render = fail_without_network
    await manager.start()
    try:
        submitted = await manager.submit(original)
        for _ in range(100):
            failed = await manager.get(submitted["job_id"], "user-1")
            if failed["status"] == "failed":
                break
            await asyncio.sleep(0.01)
        assert failed["status"] == "failed"
        with pytest.raises(MediaJobConflict, match="fresh signed URLs"):
            await manager.retry(submitted["job_id"], "user-1")
        replacement = payload("render-job-2001", "stable-key")
        replacement["inputs"][0]["url"] = (
            "https://oss.example.test/input.mp4?signature=fresh"
        )
        replacement["output"]["put_url"] = (
            "https://oss.example.test/output.mp4?signature=fresh"
        )
        retried = await manager.retry(
            submitted["job_id"], "user-1", replacement_payload=replacement
        )
        assert retried["job_id"] == submitted["job_id"]
        assert retried["status"] == "queued"
        async with manager._db_lock:
            stored = manager._conn().execute(
                "SELECT payload FROM media_jobs WHERE id=?", (submitted["job_id"],)
            ).fetchone()[0]
        assert "signature=fresh" in stored
        assert "signature=hidden" not in stored
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_retry_rejects_changed_owner_or_idempotency_key(tmp_path: Path):
    manager = MediaJobManager(
        MediaJobConfig(state_root=str(tmp_path / "state"), temp_root=str(tmp_path / "temp"))
    )

    async def fail_without_network(_job_id, _payload):
        raise RuntimeError("synthetic render failure")

    manager._render = fail_without_network
    await manager.start()
    try:
        submitted = await manager.submit(payload("render-job-3001", "stable-key"))
        for _ in range(100):
            failed = await manager.get(submitted["job_id"], "user-1")
            if failed["status"] == "failed":
                break
            await asyncio.sleep(0.01)
        assert failed["status"] == "failed"
        changed = payload("render-job-3001", "different-key")
        with pytest.raises(MediaJobConflict, match="idempotency_key"):
            await manager.retry(
                submitted["job_id"], "user-1", replacement_payload=changed
            )
        changed = payload("render-job-3001", "stable-key")
        changed["owner"] = "another-user"
        with pytest.raises(MediaJobConflict, match="owner"):
            await manager.retry(
                submitted["job_id"], "user-1", replacement_payload=changed
            )
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_command_output_is_file_backed_and_tail_bounded(tmp_path: Path):
    manager = MediaJobManager(
        MediaJobConfig(state_root=str(tmp_path / "state"), temp_root=str(tmp_path / "temp"))
    )
    await manager.start()
    try:
        result = await manager._run_command(
            "command-log-test",
            [
                sys.executable,
                "-c",
                "import sys; print('x' * 300000); print('diagnostic', file=sys.stderr)",
            ],
            cwd=tmp_path,
            timeout=10,
        )
        assert len(result["stdout"].encode()) <= 200_000
        assert result["stdout"].endswith("\n")
        assert "diagnostic" in result["stderr"]
        assert not list(tmp_path.glob(".openbox-command-*"))
    finally:
        await manager.stop()


def test_composition_has_audio_clips_and_optional_subtitles():
    with_subtitles = MediaJobManager._composition_html(
        job_id="render-job-1",
        inputs=[Path("segment-1.mp4"), Path("segment-2.mp4")],
        durations=[4.2, 5.1],
        captions=["第一段", "<第二段>"],
        subtitles=True,
        channel_name="旅途频道",
        width=1080,
        height=1920,
    )
    assert with_subtitles.count('data-has-audio="true"') == 2
    assert 'src="assets/segment-1.mp4"' in with_subtitles
    assert "../assets/" not in with_subtitles
    assert 'data-start="4.2"' in with_subtitles
    assert "&lt;第二段&gt;" in with_subtitles
    assert 'window.__timelines["render-job-1"]' in with_subtitles
    assert "https://" not in with_subtitles

    clean = MediaJobManager._composition_html(
        job_id="render-job-2",
        inputs=[Path("segment-1.mp4")],
        durations=[4.0],
        captions=["不应出现"],
        subtitles=False,
        channel_name="",
        width=1920,
        height=1080,
    )
    assert "不应出现" not in clean
    assert 'id="channel"' not in clean


def test_auto_engine_uses_ffmpeg_and_hyperframes_is_explicit():
    manager = MediaJobManager(MediaJobConfig(render_engine="auto"))
    assert manager._resolved_engine({"render_engine": "auto"}) == "ffmpeg"
    assert manager._resolved_engine({"render_engine": "ffmpeg"}) == "ffmpeg"
    assert manager._resolved_engine({"render_engine": "hyperframes"}) == "hyperframes"


def test_ffmpeg_fast_path_command_and_ass_timeline():
    manager = MediaJobManager(
        MediaJobConfig(
            output_fps=24,
            ffmpeg_threads=4,
            ffmpeg_preset="veryfast",
            ffmpeg_crf=21,
            ffmpeg_audio_bitrate_kbps=160,
        )
    )
    ass = manager._ass_document(
        durations=[4.2, 5.1],
        captions=["第一段", "第二段\n换行"],
        subtitles=True,
        channel_name="旅途频道",
        width=720,
        height=1280,
    )
    assert "Dialogue: 0,0:00:00.00,0:00:04.20,Subtitle" in ass
    assert "0:00:04.20,0:00:09.30,Subtitle" in ass
    assert r"第二段\N换行" in ass
    assert "● 旅途频道" in ass

    command = manager._ffmpeg_render_command(
        inputs=[Path("one.mp4"), Path("two.mp4")],
        output=Path("final.mp4"),
        width=720,
        height=1280,
        ass_file=Path("render.ass"),
    )
    rendered = " ".join(command)
    assert command[0] == "ffmpeg"
    assert "concat=n=2:v=1:a=1" in rendered
    assert "scale=720:1280" in rendered
    assert "fps=24" in rendered
    assert "ass=filename=render.ass" in rendered
    assert "-preset veryfast" in rendered
    assert "-crf 21" in rendered
    assert "-threads 4" in rendered
    assert "-vsync cfr" in rendered
    assert "fps_mode" not in rendered
    assert "-b:a 160k" in rendered


def test_hyperframes_command_is_bounded_for_small_wuying():
    manager = MediaJobManager(
        MediaJobConfig(
            output_fps=24,
            hyperframes_workers=1,
            hyperframes_quality="standard",
            hyperframes_low_memory_mode=True,
            hyperframes_video_frame_format="jpg",
        )
    )
    command = manager._hyperframes_render_command(Path("rendered.mp4"))
    assert command[-9:] == [
        "--fps", "24", "--quality", "standard", "--workers", "1",
        "--low-memory-mode", "--video-frame-format", "jpg",
    ]


def test_payload_rejects_unknown_render_engine():
    manager = MediaJobManager(MediaJobConfig())
    invalid = payload("render-job-engine", "engine-key")
    invalid["render_engine"] = "chrome-everything"
    with pytest.raises(MediaJobConflict, match="render_engine"):
        manager._validate_payload(invalid)
