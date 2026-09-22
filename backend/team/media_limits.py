"""Quantity bounds for the media adapters that do not have a fixed unit price."""
from __future__ import annotations

import math
import shlex
from decimal import Decimal

from billing.media import MediaQuote, quote_transcription
from team.errors import TeamError


async def transcription_input(ctx, job, target, audio_url: str, oss) -> tuple[str, MediaQuote | None]:
    """Team STT submits an explicitly bounded audio copy, never unknown duration."""
    from core.config import get_config
    from team.paid_tools import is_member
    if not is_member():
        return audio_url, None
    if ctx.sandbox is None:
        raise TeamError("PERMISSION_REQUIRES_USER", "A sandbox with ffprobe and ffmpeg is required to bound transcription cost.")
    probe = await ctx.sandbox.execute(
        f"ffprobe -v error -show_entries format=duration -of csv=p=0 {shlex.quote(audio_url)}", timeout=60)
    try:
        duration = float((probe.stdout or "").strip())
    except (TypeError, ValueError):
        duration = 0
    limit = get_config().team_max_media_seconds
    if probe.exit_code or not math.isfinite(duration) or not 0 < duration <= limit:
        raise TeamError("PERMISSION_REQUIRES_USER", f"Transcription needs a verified duration of at most {limit} seconds. Trim or repair the source first.")
    # ffmpeg enforces the sent quantity even for inaccurate container metadata.
    # Two seconds cover the encoder's final frame padding at a minute boundary.
    seconds = math.ceil(duration)
    price = quote_transcription(target.model, seconds + 2)
    if price.credits is None:
        raise TeamError("PERMISSION_REQUIRES_USER", "This transcription model has no verified price.")
    workdir = f"/tmp/obx-team-stt/{job.id}"
    result = await ctx.sandbox.execute(
        f"mkdir -p {shlex.quote(workdir)} && ffmpeg -y -v error -i {shlex.quote(audio_url)} "
        f"-t {seconds} -vn -ac 1 -ar 16000 -b:a 48k {shlex.quote(workdir + '/audio.mp3')}", timeout=300)
    if result.exit_code:
        raise TeamError("PERMISSION_REQUIRES_USER", "Could not prepare a bounded audio input; no transcription was submitted.")
    from tool.media.video_analyze import _stage
    staged = await _stage(ctx, oss, workdir=workdir, job_id=job.id, frame_files=[], audio=True)
    if not staged.get("audio_url"):
        raise TeamError("PERMISSION_REQUIRES_USER", "The bounded audio copy could not be staged; no transcription was submitted.")
    return staged["audio_url"], price


def analysis_price(model: str, *, transcribe_model: str | None, max_audio_seconds: int) -> MediaQuote:
    """A conservative bound using an explicit deployment model context limit.

    Unknown model limits are refused instead of assuming a nominal token cost.
    The normal adapter still imposes its 2,000-token output limit. The model's
    configured total context is the maximum admitted input, including images.
    """
    from billing.pricing import catalogue
    from core.config import get_config
    data = catalogue()
    configured = next((entry for entry in get_config().models if entry.id == model), None)
    context_limit = getattr(configured, "context_limit", None)
    if not isinstance(context_limit, int) or not 2000 < context_limit <= 10_000_000:
        raise TeamError("PERMISSION_REQUIRES_USER", "Video analysis needs an explicit model context_limit to establish a price upper bound.")
    from team.model_limits import model_bound
    try:
        bound = model_bound(model, output_tokens=2000, rates=data)
    except TeamError as exc:
        raise TeamError("PERMISSION_REQUIRES_USER", str(exc), current=exc.current) from exc
    model_key = data.get("aliases", {}).get(model.rsplit("/", 1)[-1].lower(), model.rsplit("/", 1)[-1].lower())
    vision = bound.credits
    stt = quote_transcription(transcribe_model, max_audio_seconds + 2, rates=data) if transcribe_model else None
    if stt is not None and stt.credits is None:
        raise TeamError("PERMISSION_REQUIRES_USER", "The analysis transcription model has no verified price.")
    return MediaQuote(model, "analysis", 0, vision + (stt.credits if stt else Decimal(0)), {
        "version": data["version"], "kind": "video_analyze", "model": model,
        "price_bound_verified": stt is None or stt.snapshot.get("price_bound_verified") is True,
        "context_limit": context_limit, "max_output_tokens": 2000,
        "vision": bound.pricing, "transcription": stt.snapshot if stt else None,
        "vision_rates": {**{key: data[key] for key in ("version", "verified_at", "credits_per_cny", "usd_cny", "fx_date", "sources") if key in data},
            "models": {model_key: data["models"][model_key]},
            "aliases": {model.rsplit("/", 1)[-1].lower(): model_key}}})
