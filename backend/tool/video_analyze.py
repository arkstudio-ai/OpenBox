"""Turn a video into a structured analysis: sampled frames + transcript → JSON.

What only the backend can do lives here: prove the source is the caller's,
stage frames in the account's OSS bucket, call the vision model and the ASR
provider with the platform's credentials, meter both, and keep a durable,
idempotent record so the same video is never analysed (and billed) twice for
one person. The sampling itself runs in the person's sandbox with ffmpeg —
the bytes never pass through the backend.

Hard rule (measured 2026-09-09, docs/spikes/M0_AUTOPILOT_SPIKES_20260909.md §B):
with no frames the model invents the picture in confident detail. Fewer than
``video_analysis.min_frames`` uploaded frames is a failure, never a text-only
fallback.
"""
from __future__ import annotations

import hashlib
import json
import shlex
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, model_validator

from core.log import create_logger
from tool.tool import ToolContext, ToolResult, define_tool
from video.analysis import PROMPT, AnalysisParseError, VideoAnalysis, parse_analysis

log = create_logger("tool.video_analyze")

KIND = "analyze"
_MEDIA_HOSTS = ("douyinvod.com", "aliyuncs.com", "bytecdn.cn", "byteimg.com")
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0 Safari/537.36"


class VideoAnalyzeArgs(BaseModel):
    action: Literal["analyze", "status"] = "analyze"
    #: An owned asset_id (or asset name), a workspace path, or an https media
    #: URL (direct mp4/m3u8 — not a web page).
    source: str | None = Field(default=None, min_length=1, max_length=2048)
    #: Number of frames to sample; default from config.
    frames: int | None = Field(default=None, ge=4, le=16)
    #: Set false to skip speech-to-text (e.g. music-only clips).
    transcribe: bool | None = None
    #: Re-run even if this source was analysed before.
    force: bool = False
    job_id: str | None = Field(default=None, max_length=96)

    @model_validator(mode="after")
    def _by_action(self) -> "VideoAnalyzeArgs":
        if self.action == "analyze" and not self.source:
            raise ValueError("analyze requires source")
        if self.action == "status" and not self.job_id:
            raise ValueError("status requires job_id")
        return self


class AnalyzeRefusal(Exception):
    public_message = True


# ── source resolution ────────────────────────────────────────────────────────

async def _resolve_source(source: str, ctx: ToolContext) -> tuple[str, str, str]:
    """Return (kind, ffmpeg_input, digest_seed). kind: url | asset | path."""
    from tool.video_production import _find_owned_asset, _materialize_asset

    if source.startswith("https://") or source.startswith("http://"):
        host = urlsplit(source).netloc.lower()
        if not host or not any(host.endswith(h) or h in host for h in _MEDIA_HOSTS):
            raise AnalyzeRefusal(
                f"{host or source!r} is not a known media host; pass an owned asset_id, a workspace path, "
                "or a direct media URL (web pages must be resolved by the browser first)"
            )
        return "url", source, source.split("?")[0]
    if source.startswith("/"):
        return "path", source, f"path:{ctx.user_id}:{source}"
    row = await _find_owned_asset(source, ctx)
    if not row:
        raise AnalyzeRefusal(f"{source!r} is not a ready asset owned by you, a workspace path, or a media URL")
    if not row.mime.startswith("video/"):
        raise AnalyzeRefusal(f"asset {source!r} is {row.mime}; only videos can be analysed")
    path = await _materialize_asset(row, ctx)
    return "asset", path, f"asset:{row.id}"


def _digest(seed: str, frames: int, transcribe: bool, model: str) -> str:
    return hashlib.sha256(f"{seed}|{frames}|{int(transcribe)}|{model}".encode()).hexdigest()[:32]


# ── sandbox sampling ─────────────────────────────────────────────────────────

def _headers_for(kind: str, ffmpeg_input: str) -> str:
    if kind != "url":
        return ""
    referer = "https://www.douyin.com/" if "douyin" in ffmpeg_input else ""
    hdr = f"User-Agent: {_UA}\\r\\n" + (f"Referer: {referer}\\r\\n" if referer else "")
    return f"-headers {shlex.quote(hdr)} "


async def _sample(ctx: ToolContext, *, kind: str, ffmpeg_input: str, workdir: str, frames: int,
                  width: int, max_seconds: int, want_audio: bool) -> dict[str, Any]:
    """ffprobe + ffmpeg in the sandbox. Returns duration and the produced file names."""
    hdr = _headers_for(kind, ffmpeg_input)
    src = shlex.quote(ffmpeg_input)
    probe = await ctx.sandbox.execute(
        f"command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null || {{ echo NO_FFMPEG; exit 9; }}; "
        f"ffprobe -v error {hdr}-show_entries format=duration -of csv=p=0 {src}", timeout=60)
    if probe.exit_code == 9 or "NO_FFMPEG" in (probe.stdout or ""):
        raise AnalyzeRefusal("the sandbox has no ffmpeg/ffprobe; this desktop image cannot sample video")
    try:
        duration = float((probe.stdout or "").strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise AnalyzeRefusal("could not read the video's duration: " + (probe.stderr or "").strip()[-200:])
    if duration <= 0:
        raise AnalyzeRefusal("the video reports zero duration")
    if duration > max_seconds:
        raise AnalyzeRefusal(f"video is {duration:.0f}s; the analysis limit is {max_seconds}s")
    fps = frames / max(duration, 1.0)
    cmd = (
        f"mkdir -p {shlex.quote(workdir)} && cd {shlex.quote(workdir)} && rm -f frame_*.jpg audio.mp3 && "
        f"ffmpeg -y -v error {hdr}-i {src} -vf fps={fps:.6f},scale={width}:-2 -frames:v {frames} -q:v 4 frame_%02d.jpg"
    )
    if want_audio:
        cmd += f" ; ffmpeg -y -v error {hdr}-i {src} -vn -ac 1 -ar 16000 -b:a 48k audio.mp3 || true"
    cmd += " ; ls -1"
    res = await ctx.sandbox.execute(cmd, timeout=300)
    names = [n.strip() for n in (res.stdout or "").splitlines() if n.strip()]
    produced = sorted(n for n in names if n.startswith("frame_") and n.endswith(".jpg"))
    return {"duration": duration, "frames": produced, "audio": "audio.mp3" in names,
            "stderr": (res.stderr or "").strip()[-300:]}


async def _stage(ctx: ToolContext, oss, *, workdir: str, job_id: str, frame_files: list[str], audio: bool) -> dict[str, Any]:
    """Push frames/audio to OSS under a per-job prefix; return presigned GET URLs."""
    from sandbox.assets import _use_internal_oss, ensure_cli

    await ensure_cli(ctx.sandbox, getattr(ctx.sandbox, "base_url", "") or ctx.session_id)
    internal = _use_internal_oss(oss)
    prefix = f"analysis/{ctx.user_id}/{job_id}"
    items = [(f, "image/jpeg") for f in frame_files] + ([("audio.mp3", "audio/mpeg")] if audio else [])
    script = []
    for name, mime in items:
        put = oss.presign_put(f"{prefix}/{name}", mime, expires_sec=900, internal=internal)
        script.append(
            f'PATH="$HOME/.local/bin:$PATH" obx-file put {shlex.quote(workdir + "/" + name)} {shlex.quote(put)} {mime} '
            f'>/dev/null 2>&1 && echo OK:{name} || echo FAIL:{name}'
        )
    res = await ctx.sandbox.execute(" ; ".join(script), timeout=240)
    ok = {line[3:] for line in (res.stdout or "").splitlines() if line.startswith("OK:")}
    frames = [oss.presign_get(f"{prefix}/{f}", expires_sec=3600) for f in frame_files if f in ok]
    audio_url = oss.presign_get(f"{prefix}/audio.mp3", expires_sec=3600) if audio and "audio.mp3" in ok else None
    return {"frames": frames, "audio_url": audio_url, "prefix": prefix, "uploaded": len(ok)}


# ── model + ASR ──────────────────────────────────────────────────────────────

async def _transcribe(audio_url: str) -> dict[str, Any]:
    from tool.video_production import _configured_transcription_target, _provider_transcribe

    return await _provider_transcribe(_configured_transcription_target(), audio_url)


async def _complete(ctx: ToolContext, *, model: str, frames: list[str], duration: float, transcript: str,
                    timeout: int) -> tuple[str, dict[str, Any], Any]:
    """One vision call, metered like any other LLM call (kind video_analyze)."""
    import litellm

    from agent.llm import _get_provider_kwargs
    from billing.pricing import normalize_usage
    from billing.service import UsageMeter

    content: list[dict[str, Any]] = [{"type": "text", "text": PROMPT.format(n=len(frames), duration=round(duration, 1), transcript=transcript or "（无）")}]
    content += [{"type": "image_url", "image_url": {"url": u}} for u in frames]
    meter = await UsageMeter.start(model_id=model, session_id=ctx.session_id, user_id=ctx.user_id,
                                   message_id=ctx.message_id, kind="video_analyze")
    usage = None
    credits = None
    try:
        response = await litellm.acompletion(model=model, messages=[{"role": "user", "content": content}],
                                             temperature=0.2, max_tokens=2000, timeout=timeout, **_get_provider_kwargs(model))
        if getattr(response, "usage", None):
            usage = normalize_usage(response.usage)
        text = response.choices[0].message.content or ""
    finally:
        if meter:
            credits = await meter.finish(usage)
    return text, usage or {}, credits


# ── job plumbing ─────────────────────────────────────────────────────────────

async def _find_cached(ctx: ToolContext, key: str):
    from sqlalchemy import select

    from db.base import get_db_session
    from db.models.video_job import VideoJob

    async with get_db_session() as db:
        return (await db.execute(select(VideoJob).where(
            VideoJob.user_id == ctx.user_id, VideoJob.kind == KIND, VideoJob.idempotency_key == key,
            VideoJob.status == "completed"))).scalar_one_or_none()


def _lines(job, analysis: dict[str, Any] | None, *, cached: bool) -> list[str]:
    lines = [f"job_id={job.id}", f"status={job.status}"]
    result = job.result_data or {}
    if cached:
        lines.append("cached=true")
    for k in ("frames_used", "duration_sec", "transcript_chars", "credits"):
        if result.get(k) is not None:
            lines.append(f"{k}={result[k]}")
    if job.error:
        lines.append(f"error={job.error}")
    if analysis:
        lines.append("analysis=" + json.dumps(analysis, ensure_ascii=False))
    return lines


def _fmt(value) -> str | None:
    if value is None:
        return None
    from decimal import Decimal

    return format(Decimal(value).normalize(), "f")


# ── execute ──────────────────────────────────────────────────────────────────

async def execute(args: VideoAnalyzeArgs, ctx: ToolContext) -> ToolResult:
    from core.config import get_config
    from tool import video_production as vp

    cfg = get_config().video_analysis
    if args.action == "status":
        job = await vp._owned_job(args.job_id or "", ctx, KIND)
        if not job:
            return ToolResult(title="Analysis job not found", output="No owned analysis job has that job_id.")
        return ToolResult(title="Video analysis", output="\n".join(_lines(job, (job.result_data or {}).get("analysis"), cached=False)),
                          metadata={"job_id": job.id, "status": job.status})

    if not ctx.sandbox:
        return ToolResult(title="No sandbox", output="Sampling frames needs the person's cloud desktop (ffmpeg runs there).")
    frames_n = args.frames or cfg.frames
    want_audio = cfg.transcribe if args.transcribe is None else args.transcribe
    try:
        from core.oss import OssNotConfigured, get_oss

        try:
            oss = get_oss()
        except OssNotConfigured as exc:
            raise AnalyzeRefusal(f"video_analyze needs the OSS asset bucket: {exc}") from exc
        kind, ffmpeg_input, seed = await _resolve_source(args.source or "", ctx)
    except Exception as exc:
        return ToolResult(title="Analysis refused", output=_public(exc))

    key = f"analyze:{_digest(seed, frames_n, want_audio, cfg.model)}"
    if not args.force:
        cached = await _find_cached(ctx, key)
        if cached:
            return ToolResult(title="Video analysis (cached)", output="\n".join(_lines(cached, (cached.result_data or {}).get("analysis"), cached=True)),
                              metadata={"job_id": cached.id, "status": "completed", "cached": True,
                                        "analysis": (cached.result_data or {}).get("analysis")})
    if args.force:
        key = f"{key}:{int(datetime.now(timezone.utc).timestamp())}"

    job, _asset, created = await vp._create_pending_job(
        ctx=ctx, kind=KIND, idempotency_key=key, model=cfg.model, prompt=None,
        request_data={"source_kind": kind, "source": seed, "frames": frames_n, "transcribe": want_audio},
        filename=None, request_hash=vp.content_hash({"seed": seed, "frames": frames_n, "t": want_audio, "m": cfg.model}),
        reserve_output=False,
    )
    if not created and job.status == "completed":
        return ToolResult(title="Video analysis (cached)", output="\n".join(_lines(job, (job.result_data or {}).get("analysis"), cached=True)),
                          metadata={"job_id": job.id, "status": "completed", "cached": True})

    workdir = f"/tmp/obx-analyze/{job.id}"
    try:
        await ctx.update_output("Sampling frames in the sandbox…")
        sampled = await _sample(ctx, kind=kind, ffmpeg_input=ffmpeg_input, workdir=workdir, frames=frames_n,
                                width=cfg.frame_width, max_seconds=cfg.max_video_seconds, want_audio=want_audio)
        if len(sampled["frames"]) < cfg.min_frames:
            raise AnalyzeRefusal(
                f"only {len(sampled['frames'])} frame(s) could be sampled (need ≥{cfg.min_frames}); "
                f"the analysis was NOT run without pictures. ffmpeg: {sampled['stderr'] or 'no detail'}"
            )
        await ctx.update_output("Staging frames for the model…")
        staged = await _stage(ctx, oss, workdir=workdir, job_id=job.id, frame_files=sampled["frames"], audio=sampled["audio"])
        if len(staged["frames"]) < cfg.min_frames:
            raise AnalyzeRefusal(f"only {len(staged['frames'])} frame(s) reached OSS (need ≥{cfg.min_frames}); analysis not run")

        transcript: dict[str, Any] = {}
        stt_credits = None
        if staged["audio_url"]:
            await ctx.update_output("Transcribing…")
            try:
                transcript = await _transcribe(staged["audio_url"])
                from billing.media import settle_transcription

                stt_credits = await settle_transcription(
                    job, workspace_id=ctx.workspace_id, model_id=str(transcript.get("model") or "fun-asr"),
                    duration_sec=(float(transcript["duration_ms"]) / 1000.0) if transcript.get("duration_ms") else sampled["duration"],
                )
            except Exception as exc:  # a failed transcript is reported, not fatal: the frames still tell the story
                log.info(f"analysis {job.id}: transcription failed: {type(exc).__name__}")
                transcript = {"error": vp._public_error(exc)}

        await ctx.update_output("Reading the frames…")
        text, usage, llm_credits = await _complete(ctx, model=cfg.model, frames=staged["frames"], duration=sampled["duration"],
                                                   transcript=str(transcript.get("text") or ""), timeout=cfg.timeout_seconds)
        analysis = parse_analysis(text).model_dump()
        total = (stt_credits or 0) + (llm_credits or 0)
        result = {
            "analysis": analysis, "frames_used": len(staged["frames"]), "duration_sec": round(sampled["duration"], 2),
            "transcript_chars": len(str(transcript.get("text") or "")), "transcript": transcript.get("text"),
            "transcript_error": transcript.get("error"), "usage": usage, "credits": _fmt(total) if total else None,
            "oss_prefix": staged["prefix"],
        }
        await vp._update_job(job.id, status="completed", result_data=result, completed_at=datetime.now(timezone.utc), attempt=1)
    except AnalysisParseError as exc:
        await vp._update_job(job.id, status="failed", error=f"analysis unparseable: {exc}", completed_at=datetime.now(timezone.utc))
        return ToolResult(title="Analysis failed", output=str(exc), metadata={"job_id": job.id, "status": "failed"})
    except Exception as exc:
        await vp._update_job(job.id, status="failed", error=_public(exc), completed_at=datetime.now(timezone.utc))
        return ToolResult(title="Analysis failed", output=_public(exc), metadata={"job_id": job.id, "status": "failed"})
    finally:
        try:
            await ctx.sandbox.execute(f"rm -rf {shlex.quote(workdir)}", timeout=30)
        except Exception:
            pass

    job = await vp._owned_job(job.id, ctx, KIND)
    return ToolResult(title=f"Video analysis · {analysis.get('form') or '—'}", output="\n".join(_lines(job, analysis, cached=False)),
                      metadata={"job_id": job.id, "status": job.status, "analysis": analysis, "credits": result["credits"]})


def _public(exc: Exception) -> str:
    from billing.service import BillingError
    from tool.video_production import _public_error

    if isinstance(exc, BillingError):
        return f"{exc.code}: {exc}"
    return _public_error(exc)


VIDEO_ANALYZE_DESCRIPTION = """\
Break a video down into a structured analysis you can create from: form \
(口播 / 画面+旁白 / 展示 / 剧情 / 混剪 / 字幕型), topic, audience, hook, timed \
structure, visual style, full script (from speech + on-screen text), topics, \
and concrete recreate_elements (presenter, scene, pace, caption style, music). \
Pass `source` as an owned asset_id, a workspace path, or a direct media URL. \
Frames are sampled with ffmpeg in the sandbox and shown to a vision model \
together with the transcript; the result is cached per source. Fewer than the \
configured minimum of frames is an error — the tool never analyses from text \
alone. Costs: transcription per minute plus the vision call's tokens \
(reported as credits=)."""

video_analyze_tool = define_tool(
    "video_analyze",
    description=VIDEO_ANALYZE_DESCRIPTION,
    parameters=VideoAnalyzeArgs,
    execute=execute,
    sandbox_required=True,
    parallel_safe=True,
)
