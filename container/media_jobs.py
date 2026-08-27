"""Durable, bounded media rendering jobs for the sandbox action server.

The action server is the right concurrency boundary for rendering: today one
WUYING desktop may be shared, while the intended deployment gives each user a
desktop of their own.  A queue stored on that desktop therefore serialises the
real CPU/RAM consumer without coupling unrelated users once the fleet grows.

Only short-lived, object-scoped OSS URLs enter this process.  Provider keys and
Alibaba credentials stay on the backend.  Input downloads are cached by the
stable OSS object identity supplied by the backend; per-attempt work lives in
``/tmp/openbox-media/jobs`` and is removed on every terminal path.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import json
import os
import re
import shutil
import signal
import sqlite3
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

import httpx


TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
ACTIVE_STATUSES = frozenset({"queued", "in_progress"})


class MediaJobError(RuntimeError):
    """Base error exposed to the action-server route layer."""


class MediaJobNotFound(MediaJobError):
    pass


class MediaJobConflict(MediaJobError):
    pass


class MediaJobCancelled(MediaJobError):
    pass


@dataclass(frozen=True)
class MediaJobConfig:
    state_root: str = "/data/openbox-media"
    temp_root: str = "/tmp/openbox-media"
    max_concurrency: int = 1
    render_engine: str = "auto"
    output_fps: int = 24
    ffmpeg_threads: int = 4
    ffmpeg_preset: str = "veryfast"
    ffmpeg_crf: int = 21
    ffmpeg_audio_bitrate_kbps: int = 160
    hyperframes_workers: int = 1
    hyperframes_quality: str = "standard"
    hyperframes_low_memory_mode: bool = True
    hyperframes_video_frame_format: str = "jpg"
    job_timeout_seconds: int = 3600
    command_timeout_seconds: int = 3000
    max_input_bytes: int = 1024 * 1024 * 1024
    cache_max_bytes: int = 20 * 1024 * 1024 * 1024
    cache_ttl_seconds: int = 24 * 3600
    job_record_ttl_seconds: int = 7 * 24 * 3600
    hyperframes_cli: str = "/opt/openbox/media/node_modules/.bin/hyperframes"
    hyperframes_version: str = "0.7.94"
    gsap_path: str = "/opt/openbox/media/node_modules/gsap/dist/gsap.min.js"
    browser_path: str = "/usr/bin/google-chrome"

    @classmethod
    def load(cls) -> "MediaJobConfig":
        path = Path(os.environ.get("MEDIA_JOBS_CONFIG", "/opt/openbox/media/media-jobs.json"))
        raw: dict[str, Any] = {}
        if path.is_file():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    raw = value
            except (OSError, ValueError):
                raw = {}

        allowed = {field.name for field in fields(cls)}
        values = {key: value for key, value in raw.items() if key in allowed}
        env_map = {
            "state_root": "MEDIA_JOB_STATE_ROOT",
            "temp_root": "MEDIA_JOB_TEMP_ROOT",
            "max_concurrency": "MEDIA_JOB_MAX_CONCURRENCY",
            "render_engine": "MEDIA_JOB_RENDER_ENGINE",
            "output_fps": "MEDIA_JOB_OUTPUT_FPS",
            "ffmpeg_threads": "MEDIA_JOB_FFMPEG_THREADS",
            "ffmpeg_preset": "MEDIA_JOB_FFMPEG_PRESET",
            "ffmpeg_crf": "MEDIA_JOB_FFMPEG_CRF",
            "ffmpeg_audio_bitrate_kbps": "MEDIA_JOB_FFMPEG_AUDIO_BITRATE_KBPS",
            "hyperframes_workers": "MEDIA_JOB_HYPERFRAMES_WORKERS",
            "hyperframes_quality": "MEDIA_JOB_HYPERFRAMES_QUALITY",
            "hyperframes_low_memory_mode": "MEDIA_JOB_HYPERFRAMES_LOW_MEMORY_MODE",
            "hyperframes_video_frame_format": "MEDIA_JOB_HYPERFRAMES_VIDEO_FRAME_FORMAT",
            "job_timeout_seconds": "MEDIA_JOB_TIMEOUT_SECONDS",
            "hyperframes_cli": "MEDIA_JOB_HYPERFRAMES_CLI",
            "gsap_path": "MEDIA_JOB_GSAP_PATH",
            "browser_path": "HYPERFRAMES_BROWSER_PATH",
        }
        numeric = {
            "max_concurrency", "output_fps", "ffmpeg_threads", "ffmpeg_crf",
            "ffmpeg_audio_bitrate_kbps", "hyperframes_workers",
            "job_timeout_seconds",
        }
        booleans = {"hyperframes_low_memory_mode"}
        for key, env_name in env_map.items():
            if env_name in os.environ:
                raw_value = os.environ[env_name]
                if key in numeric:
                    values[key] = int(raw_value)
                elif key in booleans:
                    values[key] = raw_value.strip().lower() in {"1", "true", "yes", "on"}
                else:
                    values[key] = raw_value

        config = cls(**values)
        render_engine = str(config.render_engine).lower()
        if render_engine not in {"auto", "ffmpeg", "hyperframes"}:
            render_engine = "auto"
        ffmpeg_preset = str(config.ffmpeg_preset).lower()
        if ffmpeg_preset not in {
            "ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow"
        }:
            ffmpeg_preset = "veryfast"
        hyperframes_quality = str(config.hyperframes_quality).lower()
        if hyperframes_quality not in {"draft", "standard", "high"}:
            hyperframes_quality = "standard"
        frame_format = str(config.hyperframes_video_frame_format).lower()
        if frame_format not in {"auto", "jpg", "png"}:
            frame_format = "jpg"
        return cls(
            **{
                **asdict(config),
                "max_concurrency": max(1, min(4, int(config.max_concurrency))),
                "render_engine": render_engine,
                "output_fps": max(12, min(60, int(config.output_fps))),
                "ffmpeg_threads": max(1, min(4, int(config.ffmpeg_threads))),
                "ffmpeg_preset": ffmpeg_preset,
                "ffmpeg_crf": max(0, min(51, int(config.ffmpeg_crf))),
                "ffmpeg_audio_bitrate_kbps": max(
                    64, min(320, int(config.ffmpeg_audio_bitrate_kbps))
                ),
                "hyperframes_workers": max(1, min(24, int(config.hyperframes_workers))),
                "hyperframes_quality": hyperframes_quality,
                "hyperframes_video_frame_format": frame_format,
                "job_timeout_seconds": max(60, min(6 * 3600, int(config.job_timeout_seconds))),
            }
        )


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _safe_name(value: str, fallback: str) -> str:
    name = Path(value or fallback).name
    name = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", name).strip("._")
    return (name or fallback)[:180]


def _cache_name(cache_key: str, requested_name: str) -> str:
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    suffix = Path(requested_name).suffix.lower()
    if not re.fullmatch(r"\.[a-z0-9]{1,8}", suffix):
        suffix = ".bin"
    return f"{digest}{suffix}"


def _scrub_signed_urls(payload: dict[str, Any]) -> dict[str, Any]:
    """Retain retry metadata without persisting expired OSS signatures."""
    scrubbed = dict(payload)
    scrubbed["inputs"] = [
        {**item, "url": ""} if isinstance(item, dict) else item
        for item in (payload.get("inputs") or [])
    ]
    output = payload.get("output")
    if isinstance(output, dict):
        scrubbed["output"] = {**output, "put_url": ""}
    return scrubbed


class MediaJobManager:
    def __init__(self, config: MediaJobConfig | None = None) -> None:
        self.config = config or MediaJobConfig.load()
        self.state_root = Path(self.config.state_root)
        self.temp_root = Path(self.config.temp_root)
        self.cache_root = self.temp_root / "cache"
        self.jobs_root = self.temp_root / "jobs"
        self.db_path = self.state_root / "jobs.sqlite3"
        self._db: sqlite3.Connection | None = None
        self._db_lock = asyncio.Lock()
        self._condition = asyncio.Condition()
        self._wake = asyncio.Event()
        self._workers: list[asyncio.Task] = []
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        self._job_process_groups: dict[str, set[int]] = {}
        self._stopping = False

    async def start(self) -> None:
        if self._workers:
            return
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS media_jobs (
              id TEXT PRIMARY KEY,
              owner TEXT NOT NULL,
              session_id TEXT NOT NULL DEFAULT '',
              idempotency_key TEXT NOT NULL,
              status TEXT NOT NULL,
              payload TEXT NOT NULL,
              progress TEXT NOT NULL DEFAULT '{}',
              result TEXT NOT NULL DEFAULT '{}',
              error TEXT NOT NULL DEFAULT '',
              version INTEGER NOT NULL DEFAULT 1,
              attempt INTEGER NOT NULL DEFAULT 0,
              cancel_requested INTEGER NOT NULL DEFAULT 0,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              started_at REAL,
              finished_at REAL,
              UNIQUE(owner, idempotency_key)
            )
            """
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS ix_media_jobs_queue ON media_jobs(status, created_at)"
        )
        now = time.time()
        self._db.execute(
            """
            UPDATE media_jobs
               SET status='queued', cancel_requested=0, started_at=NULL,
                   updated_at=?, version=version+1,
                   progress=?
             WHERE status='in_progress'
            """,
            (now, _json({"stage": "recovered_after_restart"})),
        )
        self._db.commit()
        await self._remove_stale_job_dirs()
        await self.prune()
        self._stopping = False
        self._workers = [
            asyncio.create_task(self._worker_loop(index), name=f"media-worker-{index}")
            for index in range(self.config.max_concurrency)
        ]
        self._wake.set()

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        for process in list(self._active_processes.values()):
            await self._terminate_process(process)
        for worker in self._workers:
            worker.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        if self._db is not None:
            self._db.close()
            self._db = None

    def capabilities(self) -> dict[str, Any]:
        return {
            "version": 2,
            "max_concurrency": self.config.max_concurrency,
            "render_engine": self.config.render_engine,
            "output_fps": self.config.output_fps,
            "ffmpeg_threads": self.config.ffmpeg_threads,
            "ffmpeg_preset": self.config.ffmpeg_preset,
            "ffmpeg_crf": self.config.ffmpeg_crf,
            "hyperframes_workers": self.config.hyperframes_workers,
            "hyperframes_low_memory_mode": self.config.hyperframes_low_memory_mode,
            "temp_root": str(self.temp_root),
            "state_root": str(self.state_root),
            "hyperframes_version": self.config.hyperframes_version,
        }

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            raise MediaJobError("media job manager has not started")
        return self._db

    def _validate_payload(self, payload: dict[str, Any]) -> tuple[str, str, str]:
        job_id = str(payload.get("job_id") or "").strip()
        owner = str(payload.get("owner") or "").strip()
        idempotency_key = str(payload.get("idempotency_key") or "").strip()
        inputs = payload.get("inputs")
        output = payload.get("output")
        captions = payload.get("captions") or []
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{8,96}", job_id):
            raise MediaJobConflict("job_id must be 8-96 safe identifier characters")
        if not owner or len(owner) > 128:
            raise MediaJobConflict("owner is required")
        if not idempotency_key or len(idempotency_key) > 180:
            raise MediaJobConflict("idempotency_key is required")
        if not isinstance(inputs, list) or not 1 <= len(inputs) <= 100:
            raise MediaJobConflict("inputs must contain 1-100 video objects")
        for item in inputs:
            if not isinstance(item, dict):
                raise MediaJobConflict("each input must be an object")
            if not str(item.get("url") or "").startswith("https://"):
                raise MediaJobConflict("each input requires an HTTPS presigned URL")
            if not str(item.get("cache_key") or "") or len(str(item.get("cache_key"))) > 1024:
                raise MediaJobConflict("each input requires a bounded cache_key")
            if not str(item.get("mime") or "").startswith("video/"):
                raise MediaJobConflict("render inputs must be video media")
            try:
                size = int(item.get("size") or 0)
            except (TypeError, ValueError) as exc:
                raise MediaJobConflict("input size must be an integer") from exc
            if size < 0 or size > self.config.max_input_bytes:
                raise MediaJobConflict("input size exceeds the configured limit")
        if not isinstance(output, dict) or not str(output.get("put_url") or "").startswith("https://"):
            raise MediaJobConflict("output.put_url must be an HTTPS presigned URL")
        if not isinstance(captions, list) or len(captions) > 100:
            raise MediaJobConflict("captions must be a list of at most 100 items")
        if captions and len(captions) != len(inputs):
            raise MediaJobConflict("captions length must match inputs length")
        if any(len(str(value)) > 2000 for value in captions):
            raise MediaJobConflict("each caption must be at most 2000 characters")
        render_engine = str(payload.get("render_engine") or "auto").lower()
        if render_engine not in {"auto", "ffmpeg", "hyperframes"}:
            raise MediaJobConflict("render_engine must be auto, ffmpeg, or hyperframes")
        for dimension in ("width", "height"):
            try:
                value = int(payload.get(dimension) or (720 if dimension == "width" else 1280))
            except (TypeError, ValueError) as exc:
                raise MediaJobConflict(f"{dimension} must be an integer") from exc
            if not 320 <= value <= 3840:
                raise MediaJobConflict(f"{dimension} must be between 320 and 3840")
        return job_id, owner, idempotency_key

    async def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id, owner, idempotency_key = self._validate_payload(payload)

        now = time.time()
        async with self._db_lock:
            db = self._conn()
            existing = db.execute(
                "SELECT * FROM media_jobs WHERE owner=? AND idempotency_key=?",
                (owner, idempotency_key),
            ).fetchone()
            if existing:
                return self._public(existing)
            same_id = db.execute("SELECT owner FROM media_jobs WHERE id=?", (job_id,)).fetchone()
            if same_id:
                raise MediaJobConflict("job_id already belongs to another submission")
            db.execute(
                """
                INSERT INTO media_jobs
                  (id, owner, session_id, idempotency_key, status, payload,
                   progress, result, error, version, attempt, cancel_requested,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, 'queued', ?, ?, '{}', '', 1, 0, 0, ?, ?)
                """,
                (
                    job_id,
                    owner,
                    str(payload.get("session_id") or "")[:128],
                    idempotency_key,
                    _json(payload),
                    _json({"stage": "queued"}),
                    now,
                    now,
                ),
            )
            db.commit()
            row = db.execute("SELECT * FROM media_jobs WHERE id=?", (job_id,)).fetchone()
        self._wake.set()
        await self._notify()
        return self._public(row)

    async def get(self, job_id: str, owner: str) -> dict[str, Any]:
        async with self._db_lock:
            row = self._owned_row(job_id, owner)
            return self._public(row)

    async def wait(
        self,
        job_id: str,
        owner: str,
        *,
        after_version: int = 0,
        timeout: float = 25.0,
    ) -> dict[str, Any]:
        timeout = max(0.0, min(25.0, float(timeout)))
        deadline = time.monotonic() + timeout
        while True:
            current = await self.get(job_id, owner)
            if current["status"] in TERMINAL_STATUSES or current["version"] > after_version:
                return current
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return current
            try:
                async with self._condition:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                return await self.get(job_id, owner)

    async def cancel(self, job_id: str, owner: str) -> dict[str, Any]:
        process: asyncio.subprocess.Process | None = None
        now = time.time()
        async with self._db_lock:
            row = self._owned_row(job_id, owner)
            if row["status"] in TERMINAL_STATUSES:
                return self._public(row)
            db = self._conn()
            if row["status"] == "queued":
                payload = _scrub_signed_urls(json.loads(row["payload"]))
                db.execute(
                    """
                    UPDATE media_jobs SET status='cancelled', cancel_requested=1,
                      payload=?, error='cancelled before execution', finished_at=?,
                      updated_at=?, version=version+1
                    WHERE id=?
                    """,
                    (_json(payload), now, now, job_id),
                )
            else:
                db.execute(
                    "UPDATE media_jobs SET cancel_requested=1, updated_at=?, version=version+1 WHERE id=?",
                    (now, job_id),
                )
                process = self._active_processes.get(job_id)
            db.commit()
            updated = db.execute("SELECT * FROM media_jobs WHERE id=?", (job_id,)).fetchone()
        if process:
            await self._terminate_process(process)
        await self._notify()
        return self._public(updated)

    async def retry(
        self,
        job_id: str,
        owner: str,
        replacement_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        async with self._db_lock:
            row = self._owned_row(job_id, owner)
            if row["status"] not in {"failed", "cancelled"}:
                raise MediaJobConflict("only failed or cancelled jobs can be retried")
            payload_json = row["payload"]
            if replacement_payload is not None:
                if str(replacement_payload.get("job_id") or "") != job_id:
                    raise MediaJobConflict("replacement payload job_id does not match")
                if str(replacement_payload.get("owner") or "") != owner:
                    raise MediaJobConflict("replacement payload owner does not match")
                if (
                    str(replacement_payload.get("idempotency_key") or "")
                    != row["idempotency_key"]
                ):
                    raise MediaJobConflict("replacement payload idempotency_key does not match")
                self._validate_payload(replacement_payload)
                payload_json = _json(replacement_payload)
            else:
                retained = json.loads(payload_json)
                urls = [str(item.get("url") or "") for item in retained.get("inputs") or []]
                put_url = str((retained.get("output") or {}).get("put_url") or "")
                if not put_url.startswith("https://") or any(
                    not url.startswith("https://") for url in urls
                ):
                    raise MediaJobConflict(
                        "retry requires a replacement payload with fresh signed URLs"
                    )
            db = self._conn()
            db.execute(
                """
                UPDATE media_jobs
                   SET status='queued', payload=?, progress=?, result='{}', error='',
                       cancel_requested=0, started_at=NULL, finished_at=NULL,
                       updated_at=?, version=version+1
                 WHERE id=?
                """,
                (payload_json, _json({"stage": "queued_for_retry"}), now, job_id),
            )
            db.commit()
            updated = db.execute("SELECT * FROM media_jobs WHERE id=?", (job_id,)).fetchone()
        self._wake.set()
        await self._notify()
        return self._public(updated)

    async def queue_status(self) -> dict[str, Any]:
        async with self._db_lock:
            rows = self._conn().execute(
                "SELECT status, COUNT(*) AS count FROM media_jobs GROUP BY status"
            ).fetchall()
        counts = {row["status"]: row["count"] for row in rows}
        return {**self.capabilities(), "counts": counts}

    def _owned_row(self, job_id: str, owner: str) -> sqlite3.Row:
        row = self._conn().execute(
            "SELECT * FROM media_jobs WHERE id=? AND owner=?", (job_id, owner)
        ).fetchone()
        if not row:
            raise MediaJobNotFound("media job not found")
        return row

    def _public(self, row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            raise MediaJobNotFound("media job not found")
        status = row["status"]
        queue_position = 0
        if status == "queued":
            queue_position = self._conn().execute(
                """
                SELECT COUNT(*) FROM media_jobs
                 WHERE status='queued' AND (created_at < ? OR (created_at = ? AND id <= ?))
                """,
                (row["created_at"], row["created_at"], row["id"]),
            ).fetchone()[0]
        return {
            "job_id": row["id"],
            "status": status,
            "version": row["version"],
            "queue_position": queue_position,
            "retry_after_seconds": 0 if status in TERMINAL_STATUSES else (5 if status == "queued" else 10),
            "attempt": row["attempt"],
            "progress": json.loads(row["progress"] or "{}"),
            "result": json.loads(row["result"] or "{}"),
            "error": row["error"] or None,
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    async def _notify(self) -> None:
        async with self._condition:
            self._condition.notify_all()

    async def _worker_loop(self, worker_index: int) -> None:
        while not self._stopping:
            # Clear before looking for work.  A submit racing after this point
            # either becomes visible to _claim_next or leaves the event set;
            # clearing after the query could lose that wakeup for ten seconds.
            self._wake.clear()
            row = await self._claim_next(worker_index)
            if not row:
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    await self.prune()
                continue
            await self._execute_claimed(row)

    async def _claim_next(self, worker_index: int) -> sqlite3.Row | None:
        now = time.time()
        async with self._db_lock:
            db = self._conn()
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM media_jobs WHERE status='queued' ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if not row:
                db.commit()
                return None
            db.execute(
                """
                UPDATE media_jobs
                   SET status='in_progress', started_at=?, updated_at=?,
                       attempt=attempt+1, version=version+1, progress=?
                 WHERE id=? AND status='queued'
                """,
                (now, now, _json({"stage": "starting", "worker": worker_index}), row["id"]),
            )
            db.commit()
            return db.execute("SELECT * FROM media_jobs WHERE id=?", (row["id"],)).fetchone()

    async def _set_progress(self, job_id: str, progress: dict[str, Any]) -> None:
        now = time.time()
        async with self._db_lock:
            db = self._conn()
            db.execute(
                "UPDATE media_jobs SET progress=?, updated_at=?, version=version+1 WHERE id=?",
                (_json(progress), now, job_id),
            )
            db.commit()
        await self._notify()

    async def _execute_claimed(self, row: sqlite3.Row) -> None:
        job_id = row["id"]
        payload = json.loads(row["payload"])
        memory_before = self._memory_snapshot()
        started = time.monotonic()
        status = "completed"
        error = ""
        result: dict[str, Any] = {}
        cleanup_errors: list[str] = []
        leaked_before_cleanup: list[int] = []
        remaining_after_termination: list[int] = []
        try:
            result = await asyncio.wait_for(
                self._render(job_id, payload), timeout=self.config.job_timeout_seconds
            )
        except MediaJobCancelled as exc:
            status, error = "cancelled", str(exc) or "cancelled"
        except asyncio.TimeoutError:
            status, error = "failed", f"render exceeded {self.config.job_timeout_seconds}s timeout"
        except asyncio.CancelledError:
            await self._kill_job_process(job_id)
            raise
        except Exception as exc:
            status, error = "failed", str(exc)[:1200]
        finally:
            job_dir = self.jobs_root / job_id
            try:
                await self._kill_job_process(job_id)
                leaked_before_cleanup = await self._wait_for_job_process_exit(
                    job_dir, job_id, grace_seconds=3.0
                )
                if leaked_before_cleanup:
                    await self._terminate_pids(leaked_before_cleanup)
                remaining_after_termination = self._job_processes(job_dir, job_id)
            except Exception as exc:
                cleanup_errors.append(f"process cleanup: {str(exc)[:300]}")
            self._job_process_groups.pop(job_id, None)
            shutil.rmtree(job_dir, ignore_errors=True)
            try:
                await self.prune()
            except Exception as exc:
                cleanup_errors.append(f"cache cleanup: {str(exc)[:300]}")

        memory_after = self._memory_snapshot()
        temp_removed = not (self.jobs_root / job_id).exists()
        resource_check = {
            "memory_before": memory_before,
            "memory_after": memory_after,
            "available_memory_delta_mb": round(
                memory_after.get("available_mb", 0) - memory_before.get("available_mb", 0), 1
            ),
            "orphan_processes_found": leaked_before_cleanup,
            "remaining_job_processes": remaining_after_termination,
            "temp_removed": temp_removed,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        if error:
            result["execution_error"] = error
        result = {**result, "resource_check": resource_check}
        if leaked_before_cleanup or remaining_after_termination or cleanup_errors or not temp_removed:
            status = "failed"
            if leaked_before_cleanup or remaining_after_termination:
                error = "resource cleanup left child processes alive"
            elif not temp_removed:
                error = "resource cleanup did not remove the job temp directory"
            else:
                error = "; ".join(cleanup_errors)
        resource_check["cleanup_errors"] = cleanup_errors

        now = time.time()
        scrubbed_payload = _scrub_signed_urls(payload)
        async with self._db_lock:
            db = self._conn()
            db.execute(
                """
                UPDATE media_jobs SET status=?, payload=?, result=?, error=?, progress=?,
                  cancel_requested=0, finished_at=?, updated_at=?, version=version+1
                WHERE id=?
                """,
                (
                    status,
                    _json(scrubbed_payload),
                    _json(result),
                    error,
                    _json({"stage": status}),
                    now,
                    now,
                    job_id,
                ),
            )
            db.commit()
        await self._notify()
        self._wake.set()

    def _resolved_engine(self, payload: dict[str, Any]) -> str:
        requested = str(payload.get("render_engine") or "auto").lower()
        if requested != "auto":
            return requested
        if self.config.render_engine != "auto":
            return self.config.render_engine
        # The current payload contract is a linear spoken-video timeline. It
        # does not need a browser unless the caller explicitly requests the
        # HyperFrames authoring path.
        return "ffmpeg"

    @staticmethod
    def _ass_timestamp(seconds: float) -> str:
        centiseconds = max(0, round(float(seconds) * 100))
        hours, remainder = divmod(centiseconds, 360_000)
        minutes, remainder = divmod(remainder, 6_000)
        whole_seconds, fraction = divmod(remainder, 100)
        return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{fraction:02d}"

    @staticmethod
    def _ass_escape(value: str) -> str:
        return (
            str(value)
            .replace("\\", r"\\")
            .replace("{", r"\{")
            .replace("}", r"\}")
            .replace("\r", "")
            .replace("\n", r"\N")
        )

    @classmethod
    def _ass_document(
        cls,
        *,
        durations: list[float],
        captions: list[str],
        subtitles: bool,
        channel_name: str,
        width: int,
        height: int,
    ) -> str:
        subtitle_size = max(28, round(width * 0.048))
        channel_size = max(20, round(width * 0.027))
        subtitle_margin = max(24, round(height * 0.095))
        channel_margin_v = max(18, round(height * 0.04))
        side_margin = max(24, round(width * 0.055))
        header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
ScaledBorderAndShadow: yes
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Subtitle,Noto Sans CJK SC,{subtitle_size},&H00FFFFFF,&H000000FF,&H00000000,&H78000000,-1,0,0,0,100,100,1,0,1,3,1,2,{side_margin},{side_margin},{subtitle_margin},1
Style: Channel,Noto Sans CJK SC,{channel_size},&H20FFFFFF,&H000000FF,&H00000000,&H78000000,-1,0,0,0,100,100,0,0,1,2,1,1,{side_margin},{side_margin},{channel_margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
        events: list[str] = []
        current = 0.0
        for duration, caption in zip(durations, captions):
            end = current + float(duration)
            if subtitles and str(caption).strip():
                events.append(
                    "Dialogue: 0,"
                    f"{cls._ass_timestamp(current)},{cls._ass_timestamp(end)},"
                    f"Subtitle,,0,0,0,,{cls._ass_escape(str(caption).strip())}"
                )
            current = end
        if channel_name.strip() and current > 0:
            events.append(
                "Dialogue: 1,"
                f"0:00:00.00,{cls._ass_timestamp(current)},Channel,,0,0,0,,"
                f"● {cls._ass_escape(channel_name.strip())}"
            )
        return header + "\n".join(events) + "\n"

    def _ffmpeg_render_command(
        self,
        *,
        inputs: list[Path],
        output: Path,
        width: int,
        height: int,
        ass_file: Path | None,
    ) -> list[str]:
        command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        for path in inputs:
            command.extend(["-i", str(path)])
        filters: list[str] = []
        concat_inputs: list[str] = []
        for index in range(len(inputs)):
            filters.append(
                f"[{index}:v:0]"
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},fps={self.config.output_fps},"
                "setsar=1,settb=AVTB,setpts=PTS-STARTPTS"
                f"[v{index}]"
            )
            filters.append(
                f"[{index}:a:0]aresample=48000:async=1:first_pts=0,"
                "aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
                f"asetpts=PTS-STARTPTS[a{index}]"
            )
            concat_inputs.extend([f"[v{index}]", f"[a{index}]"])
        filters.append(
            f"{''.join(concat_inputs)}concat=n={len(inputs)}:v=1:a=1[vcat][acat]"
        )
        if ass_file is not None:
            filters.append(f"[vcat]ass=filename={ass_file.name}[vout]")
        else:
            filters.append("[vcat]null[vout]")
        command.extend(
            [
                "-filter_complex_threads", str(self.config.ffmpeg_threads),
                "-filter_complex", ";".join(filters),
                "-map", "[vout]", "-map", "[acat]",
                "-c:v", "libx264", "-preset", self.config.ffmpeg_preset,
                "-crf", str(self.config.ffmpeg_crf),
                "-threads", str(self.config.ffmpeg_threads),
                "-pix_fmt", "yuv420p", "-vsync", "cfr",
                "-c:a", "aac", "-b:a", f"{self.config.ffmpeg_audio_bitrate_kbps}k",
                "-ar", "48000", "-movflags", "+faststart",
                "-max_muxing_queue_size", "2048", str(output),
            ]
        )
        return command

    def _hyperframes_render_command(self, output: Path) -> list[str]:
        return [
            self.config.hyperframes_cli,
            "render",
            "compositions",
            "-o",
            output.name,
            "--fps",
            str(self.config.output_fps),
            "--quality",
            self.config.hyperframes_quality,
            "--workers",
            str(self.config.hyperframes_workers),
            "--low-memory-mode"
            if self.config.hyperframes_low_memory_mode
            else "--no-low-memory-mode",
            "--video-frame-format",
            self.config.hyperframes_video_frame_format,
        ]

    async def _render(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        engine = self._resolved_engine(payload)
        self._require_runtime(engine)
        job_dir = self.jobs_root / job_id
        comps_dir = job_dir / "compositions"
        # The directory passed to HyperFrames is its project root. Keep every
        # locally-referenced asset inside that tree so lint, Studio semantics,
        # and the headless renderer resolve the same URLs.
        assets_dir = comps_dir / "assets"
        shutil.rmtree(job_dir, ignore_errors=True)
        assets_dir.mkdir(parents=True)

        await self._set_progress(job_id, {"stage": "downloading_inputs", "completed": 0, "total": len(payload["inputs"])})
        local_inputs: list[Path] = []
        cache_hits = 0
        for index, item in enumerate(payload["inputs"]):
            await self._raise_if_cancelled(job_id)
            cached, reused = await self._cached_input(job_id, item)
            cache_hits += int(reused)
            name = f"segment-{index + 1}{cached.suffix or '.mp4'}"
            local = assets_dir / name
            try:
                os.link(cached, local)
            except OSError:
                shutil.copy2(cached, local)
            local_inputs.append(local)
            await self._set_progress(
                job_id,
                {
                    "stage": "downloading_inputs",
                    "completed": index + 1,
                    "total": len(payload["inputs"]),
                    "cache_hits": cache_hits,
                },
            )

        await self._set_progress(job_id, {"stage": "probing_inputs"})
        probes = [await self._probe(job_id, path) for path in local_inputs]
        if any(not item["has_audio"] for item in probes):
            missing = [index + 1 for index, item in enumerate(probes) if not item["has_audio"]]
            raise MediaJobError(f"input segment(s) {missing} have no audio track")
        durations = [item["duration_seconds"] for item in probes]
        if any(value <= 0 for value in durations):
            raise MediaJobError("one or more input durations are zero")

        captions = payload.get("captions") or []
        if captions and len(captions) != len(local_inputs):
            raise MediaJobError("captions length must match inputs length")
        if not captions:
            captions = [""] * len(local_inputs)

        width = max(320, min(3840, int(payload.get("width") or 720)))
        height = max(320, min(3840, int(payload.get("height") or 1280)))
        final_output = job_dir / "final.mp4"
        if engine == "ffmpeg":
            ass_file: Path | None = None
            if (
                bool(payload.get("subtitles", True)) and any(str(value).strip() for value in captions)
            ) or str(payload.get("channel_name") or "").strip():
                ass_file = job_dir / "render.ass"
                ass_file.write_text(
                    self._ass_document(
                        durations=durations,
                        captions=[str(value) for value in captions],
                        subtitles=bool(payload.get("subtitles", True)),
                        channel_name=str(payload.get("channel_name") or ""),
                        width=width,
                        height=height,
                    ),
                    encoding="utf-8",
                )
            await self._set_progress(
                job_id,
                {
                    "stage": "ffmpeg_render",
                    "duration_seconds": round(sum(durations), 3),
                    "fps": self.config.output_fps,
                },
            )
            ffmpeg_metrics = await self._run_command(
                job_id,
                self._ffmpeg_render_command(
                    inputs=local_inputs,
                    output=final_output,
                    width=width,
                    height=height,
                    ass_file=ass_file,
                ),
                cwd=job_dir,
                timeout=self.config.command_timeout_seconds,
            )
            render_metrics = ffmpeg_metrics
        else:
            gsap_target = comps_dir / "gsap.min.js"
            shutil.copy2(self.config.gsap_path, gsap_target)
            composition = self._composition_html(
                job_id=job_id,
                inputs=local_inputs,
                durations=durations,
                captions=[str(value) for value in captions],
                subtitles=bool(payload.get("subtitles", True)),
                channel_name=str(payload.get("channel_name") or ""),
                width=width,
                height=height,
                fps=self.config.output_fps,
            )
            # HyperFrames 0.7.94 discovers each composition by its index.html
            # entrypoint. Other filenames lint as "No composition found".
            (comps_dir / "index.html").write_text(composition, encoding="utf-8")
            await self._set_progress(job_id, {"stage": "hyperframes_lint"})
            await self._run_command(
                job_id,
                [self.config.hyperframes_cli, "lint", "compositions/"],
                cwd=job_dir,
                timeout=min(600, self.config.command_timeout_seconds),
            )
            raw_output = job_dir / "rendered.mp4"
            await self._set_progress(
                job_id,
                {
                    "stage": "hyperframes_render",
                    "duration_seconds": round(sum(durations), 3),
                    "fps": self.config.output_fps,
                    "workers": self.config.hyperframes_workers,
                },
            )
            env = {
                **os.environ,
                "HYPERFRAMES_BROWSER_PATH": self.config.browser_path,
                "PRODUCER_LOW_MEMORY_MODE": (
                    "1" if self.config.hyperframes_low_memory_mode else "0"
                ),
            }
            render_metrics = await self._run_command(
                job_id,
                self._hyperframes_render_command(raw_output),
                cwd=job_dir,
                timeout=self.config.command_timeout_seconds,
                env=env,
            )
            await self._set_progress(job_id, {"stage": "ffmpeg_faststart"})
            ffmpeg_metrics = await self._run_command(
                job_id,
                [
                    "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                    "-threads", str(self.config.ffmpeg_threads), "-i", str(raw_output),
                    "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
                    "-movflags", "+faststart", str(final_output),
                ],
                cwd=job_dir,
                timeout=min(900, self.config.command_timeout_seconds),
            )
        final_probe = await self._probe(job_id, final_output)
        expected = sum(durations)
        tolerance = max(2.0, expected * 0.05)
        if not final_probe["has_audio"]:
            raise MediaJobError("rendered output has no audio track")
        if abs(final_probe["duration_seconds"] - expected) > tolerance:
            raise MediaJobError(
                f"rendered duration {final_probe['duration_seconds']:.3f}s differs from inputs {expected:.3f}s"
            )

        await self._set_progress(job_id, {"stage": "uploading_output"})
        size = await self._upload_output(job_id, final_output, payload["output"]["put_url"])
        return {
            "uploaded": True,
            "bytes": size,
            "duration_seconds": final_probe["duration_seconds"],
            "has_audio": final_probe["has_audio"],
            "input_count": len(local_inputs),
            "input_durations": durations,
            "cache_hits": cache_hits,
            "subtitles": bool(payload.get("subtitles", True)),
            "render_engine": engine,
            "output_fps": self.config.output_fps,
            "width": width,
            "height": height,
            "render_peak_rss_mb": render_metrics["peak_rss_mb"],
            "ffmpeg_peak_rss_mb": ffmpeg_metrics["peak_rss_mb"],
        }

    def _require_runtime(self, engine: str) -> None:
        required = {
            "ffmpeg": shutil.which("ffmpeg"),
            "ffprobe": shutil.which("ffprobe"),
        }
        if engine == "hyperframes":
            required.update(
                {
                    "hyperframes": self.config.hyperframes_cli
                    if Path(self.config.hyperframes_cli).is_file()
                    else None,
                    "gsap": self.config.gsap_path
                    if Path(self.config.gsap_path).is_file()
                    else None,
                    "browser": self.config.browser_path
                    if Path(self.config.browser_path).is_file()
                    else None,
                }
            )
        missing = [name for name, path in required.items() if not path]
        if missing:
            raise MediaJobError(f"ENV_MISSING: {', '.join(missing)}")

    async def _cached_input(self, job_id: str, item: dict[str, Any]) -> tuple[Path, bool]:
        url = str(item.get("url") or "")
        cache_key = str(item.get("cache_key") or "")
        expected_size = max(0, int(item.get("size") or 0))
        if not url.startswith("https://") or not cache_key:
            raise MediaJobError("input url/cache_key is missing")
        target = self.cache_root / _cache_name(cache_key, str(item.get("name") or "segment.mp4"))
        if target.is_file() and target.stat().st_size > 0:
            if not expected_size or target.stat().st_size == expected_size:
                os.utime(target, None)
                return target, True
            target.unlink(missing_ok=True)

        part = target.with_name(f".{target.name}.{os.getpid()}.{time.time_ns()}.part")
        total = 0
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, read=600.0), follow_redirects=True) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        raise MediaJobError(f"OSS input download returned HTTP {response.status_code}")
                    declared = int(response.headers.get("content-length") or 0)
                    if declared > self.config.max_input_bytes:
                        raise MediaJobError("input exceeds configured size limit")
                    with part.open("wb") as handle:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            await self._raise_if_cancelled(job_id)
                            total += len(chunk)
                            if total > self.config.max_input_bytes:
                                raise MediaJobError("input exceeds configured size limit")
                            handle.write(chunk)
            if total <= 0:
                raise MediaJobError("OSS input download was empty")
            if expected_size and total != expected_size:
                raise MediaJobError(
                    f"OSS input size changed: expected {expected_size} bytes, downloaded {total}"
                )
            os.replace(part, target)
            return target, False
        finally:
            part.unlink(missing_ok=True)

    async def _probe(self, job_id: str, path: Path) -> dict[str, Any]:
        metrics = await self._run_command(
            job_id,
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                "-of", "json", str(path),
            ],
            cwd=path.parent,
            timeout=60,
        )
        try:
            data = json.loads(metrics["stdout"])
            duration = float((data.get("format") or {}).get("duration") or 0)
            streams = data.get("streams") or []
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise MediaJobError(f"ffprobe returned invalid metadata for {path.name}") from exc
        return {
            "duration_seconds": round(duration, 3),
            "has_audio": any(item.get("codec_type") == "audio" for item in streams),
            "has_video": any(item.get("codec_type") == "video" for item in streams),
        }

    async def _upload_output(self, job_id: str, path: Path, put_url: str) -> int:
        size = path.stat().st_size

        async def body():
            with path.open("rb") as handle:
                while True:
                    await self._raise_if_cancelled(job_id)
                    chunk = await asyncio.to_thread(handle.read, 1024 * 1024)
                    if not chunk:
                        break
                    yield chunk

        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, write=1800.0)) as client:
            response = await client.put(
                put_url,
                content=body(),
                headers={"Content-Type": "video/mp4", "Content-Length": str(size)},
            )
        if response.status_code not in (200, 201, 204):
            raise MediaJobError(f"OSS output upload returned HTTP {response.status_code}")
        return size

    async def _run_command(
        self,
        job_id: str,
        command: list[str],
        *,
        cwd: Path,
        timeout: int,
        env: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        await self._raise_if_cancelled(job_id)
        log_token = f".openbox-command-{time.time_ns()}"
        stdout_path = cwd / f"{log_token}.stdout"
        stderr_path = cwd / f"{log_token}.stderr"
        stdout_handle = stdout_path.open("wb")
        stderr_handle = stderr_path.open("wb")
        process: asyncio.subprocess.Process | None = None
        waiter: asyncio.Task | None = None
        deadline = time.monotonic() + timeout
        peak_rss = 0
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(cwd),
                env=env,
                stdout=stdout_handle,
                stderr=stderr_handle,
                start_new_session=True,
            )
            self._active_processes[job_id] = process
            self._job_process_groups.setdefault(job_id, set()).add(process.pid)
            waiter = asyncio.create_task(process.wait())
            while not waiter.done():
                if time.monotonic() >= deadline:
                    await self._terminate_process(process)
                    raise MediaJobError(f"command timed out after {timeout}s: {Path(command[0]).name}")
                if await self._cancel_requested(job_id):
                    await self._terminate_process(process)
                    raise MediaJobCancelled("cancelled during rendering")
                peak_rss = max(peak_rss, self._tree_rss(process.pid))
                try:
                    await asyncio.wait_for(asyncio.shield(waiter), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
            await waiter
            peak_rss = max(peak_rss, self._tree_rss(process.pid))
            stdout_handle.close()
            stderr_handle.close()
            stdout = self._tail_text(stdout_path, 200_000)
            stderr = self._tail_text(stderr_path, 20_000)
            # cancel() terminates the active process immediately. The waiter
            # can therefore finish between loop iterations, before the loop
            # observes cancel_requested. Preserve the user's intent instead
            # of misclassifying SIGTERM's non-zero exit as a render failure.
            if await self._cancel_requested(job_id):
                raise MediaJobCancelled("cancelled during rendering")
            if process.returncode != 0:
                # Some CLIs (notably HyperFrames lint) report validation
                # failures on stdout while returning a non-zero code.
                detail = (stderr.strip() or stdout.strip())[-1600:]
                raise MediaJobError(
                    f"{Path(command[0]).name} exited {process.returncode}: {detail or 'no stderr'}"
                )
            return {
                "stdout": stdout,
                "stderr": stderr,
                "peak_rss_mb": round(peak_rss / 1024 / 1024, 1),
            }
        finally:
            stdout_handle.close()
            stderr_handle.close()
            if waiter is not None and not waiter.done():
                waiter.cancel()
            if process is not None and self._active_processes.get(job_id) is process:
                self._active_processes.pop(job_id, None)
            if process is not None and not self._process_group_members(process.pid):
                groups = self._job_process_groups.get(job_id)
                if groups:
                    groups.discard(process.pid)
            stdout_path.unlink(missing_ok=True)
            stderr_path.unlink(missing_ok=True)

    @staticmethod
    def _tail_text(path: Path, limit: int) -> str:
        try:
            with path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - limit))
                return handle.read().decode("utf-8", "replace")
        except OSError:
            return ""

    async def _cancel_requested(self, job_id: str) -> bool:
        async with self._db_lock:
            row = self._conn().execute(
                "SELECT cancel_requested FROM media_jobs WHERE id=?", (job_id,)
            ).fetchone()
            return bool(row and row[0])

    async def _raise_if_cancelled(self, job_id: str) -> None:
        if await self._cancel_requested(job_id):
            raise MediaJobCancelled("cancelled")

    async def _kill_job_process(self, job_id: str) -> None:
        process = self._active_processes.pop(job_id, None)
        if process:
            await self._terminate_process(process)

    async def _terminate_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

    async def _terminate_pids(self, pids: list[int]) -> None:
        """Best-effort cleanup for descendants that escaped the tracked root."""
        for sig, delay in ((signal.SIGTERM, 0.5), (signal.SIGKILL, 0.2)):
            for pid in pids:
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            await asyncio.sleep(delay)

    def _tree_rss(self, pid: int) -> int:
        try:
            import psutil

            root = psutil.Process(pid)
            return root.memory_info().rss + sum(
                child.memory_info().rss for child in root.children(recursive=True) if child.is_running()
            )
        except Exception:
            return 0

    def _memory_snapshot(self) -> dict[str, float]:
        try:
            import psutil

            memory = psutil.virtual_memory()
            return {
                "total_mb": round(memory.total / 1024 / 1024, 1),
                "available_mb": round(memory.available / 1024 / 1024, 1),
                "used_percent": round(memory.percent, 1),
            }
        except Exception:
            return {}

    def _process_group_members(self, process_group: int) -> list[int]:
        found: list[int] = []
        try:
            import psutil

            for process in psutil.process_iter(["pid"]):
                try:
                    if process.pid != os.getpid() and os.getpgid(process.pid) == process_group:
                        found.append(process.pid)
                except (psutil.Error, OSError):
                    continue
        except Exception:
            pass
        return found

    def _job_processes(self, job_dir: Path, job_id: str | None = None) -> list[int]:
        found: list[int] = []
        try:
            import psutil

            target = str(job_dir)
            process_groups = self._job_process_groups.get(job_id or "", set())
            for process in psutil.process_iter(["pid", "cwd", "cmdline"]):
                try:
                    if process.pid == os.getpid():
                        continue
                    cwd = process.info.get("cwd") or ""
                    cmdline = process.info.get("cmdline") or []
                    same_tree = cwd == target or cwd.startswith(target + os.sep)
                    mentions_job = any(
                        arg == target or arg.startswith(target + os.sep) for arg in cmdline
                    )
                    same_group = bool(
                        process_groups and os.getpgid(process.pid) in process_groups
                    )
                    if same_tree or mentions_job or same_group:
                        found.append(process.pid)
                except (psutil.Error, OSError):
                    continue
        except Exception:
            pass
        return found

    async def _wait_for_job_process_exit(
        self, job_dir: Path, job_id: str, *, grace_seconds: float
    ) -> list[int]:
        """Allow short-lived Chromium helpers to reap before classifying a leak."""
        deadline = time.monotonic() + max(0.0, grace_seconds)
        remaining = self._job_processes(job_dir, job_id)
        while remaining and time.monotonic() < deadline:
            await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
            remaining = self._job_processes(job_dir, job_id)
        return remaining

    async def prune(self) -> dict[str, int]:
        now = time.time()
        deleted_files = 0
        freed_bytes = 0
        files: list[tuple[float, int, Path]] = []
        if self.cache_root.exists():
            for path in self.cache_root.iterdir():
                if not path.is_file():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if path.name.startswith(".") or now - stat.st_mtime > self.config.cache_ttl_seconds:
                    size = stat.st_size
                    path.unlink(missing_ok=True)
                    deleted_files += 1
                    freed_bytes += size
                else:
                    files.append((stat.st_mtime, stat.st_size, path))
        total = sum(size for _, size, _ in files)
        for _, size, path in sorted(files):
            if total <= self.config.cache_max_bytes:
                break
            path.unlink(missing_ok=True)
            total -= size
            deleted_files += 1
            freed_bytes += size

        if self._db is not None:
            cutoff = now - self.config.job_record_ttl_seconds
            async with self._db_lock:
                db = self._conn()
                db.execute(
                    "DELETE FROM media_jobs WHERE status IN ('completed','failed','cancelled') AND finished_at < ?",
                    (cutoff,),
                )
                db.commit()
        return {"deleted_files": deleted_files, "freed_bytes": freed_bytes, "cache_bytes": total}

    async def _remove_stale_job_dirs(self) -> None:
        if not self.jobs_root.exists():
            return
        for path in self.jobs_root.iterdir():
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)

    @staticmethod
    def _composition_html(
        *,
        job_id: str,
        inputs: list[Path],
        durations: list[float],
        captions: list[str],
        subtitles: bool,
        channel_name: str,
        width: int,
        height: int,
        fps: int = 24,
    ) -> str:
        total = round(sum(durations), 3)
        current = 0.0
        clips: list[str] = []
        subtitle_nodes: list[str] = []
        animations: list[str] = []
        for index, (path, duration, caption) in enumerate(zip(inputs, durations, captions), start=1):
            start = round(current, 3)
            duration = round(duration, 3)
            clips.append(
                f'<video id="seg-{index}" class="video clip" data-start="{start}" '
                f'data-duration="{duration}" data-track-index="0" data-media-start="0" '
                f'data-has-audio="true" src="assets/{html.escape(path.name)}" playsinline></video>'
            )
            if subtitles and caption.strip():
                subtitle_nodes.append(
                    f'<div id="sub-{index}" class="subtitle clip" data-start="{start}" '
                    f'data-duration="{duration}" data-track-index="2">{html.escape(caption.strip())}</div>'
                )
                animations.append(
                    f'tl.from("#sub-{index}", {{y: 24, opacity: 0, duration: 0.35, ease: "power2.out"}}, {start});'
                )
            current += duration
        channel = ""
        channel_animation = ""
        if channel_name.strip():
            channel = (
                f'<div id="channel" class="channel clip" data-start="0" data-duration="{total}" '
                f'data-track-index="3"><span class="dot"></span><span>{html.escape(channel_name.strip())}</span></div>'
            )
            channel_animation = 'tl.from("#channel", {y: 30, opacity: 0, duration: 0.5}, 0);'
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "-", job_id)
        return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width={width}, height={height}" />
  <script src="gsap.min.js"></script>
  <style>
    @font-face {{ font-family:"Noto Sans SC"; src:local("Noto Sans CJK SC"),local("Noto Sans SC"); font-weight:100 900; }}
    * {{ margin:0; padding:0; box-sizing:border-box; }}
    html,body {{ width:{width}px; height:{height}px; overflow:hidden; background:#000; font-family:"Noto Sans SC",sans-serif; color:#fff; }}
    .video {{ position:absolute; inset:0; width:{width}px; height:{height}px; object-fit:cover; background:#000; }}
    .vignette {{ position:absolute; inset:0; pointer-events:none; background:linear-gradient(to bottom,rgba(0,0,0,.28),transparent 35%,transparent 62%,rgba(0,0,0,.68)); }}
    .subtitle {{ position:absolute; left:5.5%; right:5.5%; bottom:9.5%; font-size:{max(28, round(width * .048))}px; font-weight:700; line-height:1.42; text-align:center; letter-spacing:1px; text-shadow:0 3px 18px rgba(0,0,0,.95),0 1px 2px #000; }}
    .channel {{ position:absolute; left:5.5%; bottom:4%; display:flex; gap:14px; align-items:center; font-size:{max(20, round(width * .027))}px; font-weight:700; color:rgba(255,255,255,.88); }}
    .dot {{ width:12px; height:12px; border-radius:50%; background:#ff6b35; box-shadow:0 0 12px rgba(255,107,53,.8); }}
  </style>
</head>
<body>
  <div id="root" data-composition-id="{safe_id}" data-start="0" data-duration="{total}" data-width="{width}" data-height="{height}" data-fps="{fps}">
    {''.join(clips)}
    <div id="vignette" class="vignette clip" data-start="0" data-duration="{total}" data-track-index="1"></div>
    {''.join(subtitle_nodes)}
    {channel}
  </div>
  <script>
    window.__timelines = window.__timelines || {{}};
    const tl = gsap.timeline({{paused:true}});
    {''.join(animations)}
    {channel_animation}
    tl.to({{}}, {{duration:{total}}}, 0);
    window.__timelines["{safe_id}"] = tl;
  </script>
</body>
</html>
"""


media_job_manager = MediaJobManager()
