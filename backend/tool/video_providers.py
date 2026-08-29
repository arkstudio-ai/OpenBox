"""Multi-channel video provider routing (ported from bossip's executor layer).

Three wire channels behind one route object:

- ``ark``  — the existing Volcano-Ark shape (``/api/v3/contents/generations/tasks``),
  used by the legacy ``doubao``/TokenSpace Seedance provider. Submission for
  this channel stays in ``tool/video_production.py``; this module only routes.
- ``sd2``  — new-api Sora adaptor: ``POST /v1/videos`` / ``GET /v1/videos/{id}``,
  lowercase statuses, references as top-level public URLs.
- ``task`` — new-api task channel: ``POST /v1/video/generations`` with an
  ``{code, message, data}`` envelope and uppercase statuses. Used by Wan3.0;
  the wan3 protocol shim (wan3-video-adapter) is deployed with new-api itself.

Model → channel routing lives in code so the U+2160 ``Ⅰ`` canonicalization is
centralized and a config typo cannot misroute a paid call. Hard lessons from
bossip carried over verbatim:

- sd2 polling must use the create response's ``id`` (``task_`` prefix), never
  ``task_id`` — upstream overwrites ``task_id`` and polling it yields
  ``task_not_exist``.
- ``video-sd-720p-proⅠ`` silently discards video references upstream (0/7
  measured), so video refs on that model are refused loudly.
- Unsupported switches are rejected, never silently dropped — a task that
  "succeeds" while ignoring its reference material is worse than one that
  fails.
"""
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

# ── sd2 (Sora adaptor) ──────────────────────────────────────────────────────
# The trailing Ⅰ on the first two models is ROMAN NUMERAL ONE (U+2160), not
# the letter I; the wrong character makes the gateway report model-not-found.
SD2_MODEL_TYPE = "sd2_video"
SD2_MODELS = ("seedance-2.0-480-fastⅠ", "video-sd-720p-proⅠ", "video-sd-1080p-pro")

_SD2_TRAILING_I = re.compile(r"[iIⅠ]$")

# ── ark relay host ──────────────────────────────────────────────────────────
BOSSIP_RELAY_HOST = "openapi.bossipai.com.cn"

# ── wan3 (task channel via the gateway-side wan3-video-adapter) ─────────────
WAN3_MODEL_TYPE = "wan3_video"
WAN3_DEFAULT_MODEL = "wan3.0-video"
WAN3_MODELS = ("wan3.0-video", "wan3.0-video-prime")


def canonicalize_sd2_model_name(name: str) -> str:
    """Tolerate I/i/Ⅰ spellings of the sd2 model suffix."""
    value = (name or "").strip()
    if not value:
        return value
    normalized = _SD2_TRAILING_I.sub("Ⅰ", value)
    for model in SD2_MODELS:
        if normalized == model:
            return model
    return value


def is_sd2_model(name: str) -> bool:
    return canonicalize_sd2_model_name(name) in SD2_MODELS


def sd2_native_resolution(name: str) -> str:
    model = canonicalize_sd2_model_name(name)
    if "480" in model:
        return "480p"
    if "720" in model:
        return "720p"
    return "1080p"


def canonicalize_wan3_model_name(name: str) -> str:
    value = (name or "").strip().lower()
    for model in WAN3_MODELS:
        if value == model:
            return model
    return WAN3_DEFAULT_MODEL


def is_wan3_model(name: str) -> bool:
    value = (name or "").strip().lower()
    return value.startswith("wan3") or value == WAN3_MODEL_TYPE


def map_wan3_resolution(resolution: str | None) -> str:
    digits = re.sub(r"\D", "", resolution or "")
    if digits in {"480", "512"}:
        return "480p"
    if digits in {"720", "768"}:
        return "720p"
    return "1080p"


def clamp_wan3_duration(seconds: int) -> int:
    """Wan3 accepts 2–30 s and supports -1 smart duration natively.

    Deliberately wider than Seedance's 4–15 — do not reuse another channel's
    clamp here.
    """
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return 5
    if value == -1:
        return -1
    return min(30, max(2, value))


@dataclass(frozen=True)
class VideoRoute:
    """Resolved submission target. Superset of the legacy VideoProviderTarget."""

    provider: str
    model: str
    api_key: str
    base_url: str
    submit_timeout_seconds: int
    status_timeout_seconds: int
    channel: Literal["ark", "sd2", "task"] = "ark"
    model_type: str = "seedance"
    # "raw" = the new-api quirk: Authorization carries the sk-token verbatim,
    # WITHOUT a "Bearer " prefix.
    auth_scheme: Literal["bearer", "raw"] = "bearer"
    # Only meaningful on the ark channel: TokenSpace contents API vs the
    # BossIP public relay (`/v1/videos` with relay-managed material groups).
    wire_format: Literal["tokenspace_contents", "bossip_videos"] = "tokenspace_contents"


def auth_header(route: Any) -> str:
    key = route.api_key
    if getattr(route, "auth_scheme", "bearer") == "raw":
        return key
    return key if key.lower().startswith("bearer ") else f"Bearer {key}"


def _gateway_route(model: str, channel: str, model_type: str, config) -> VideoRoute:
    settings = config.video_generation
    provider_name = (settings.channel_providers or {}).get(channel, "")
    if not provider_name:
        raise RuntimeError(
            f"model '{model}' routes to the '{channel}' channel, but "
            f"video_generation.channel_providers has no '{channel}' entry — "
            "the channel is disabled"
        )
    provider = config.provider.get(provider_name)
    if not provider or not provider.api_key or not provider.base_url:
        raise RuntimeError(
            f"provider '{provider_name}' needs api_key and base_url for the "
            f"'{channel}' video channel"
        )
    return VideoRoute(
        provider=provider_name,
        model=model,
        api_key=provider.api_key,
        base_url=provider.base_url.rstrip("/"),
        submit_timeout_seconds=settings.submit_timeout_seconds,
        status_timeout_seconds=settings.status_timeout_seconds,
        channel=channel,  # type: ignore[arg-type]
        model_type=model_type,
        auth_scheme=(provider.options or {}).get("auth_scheme", "bearer"),
    )


def resolve_route(model_override: str | None, config) -> VideoRoute:
    """Route a model name to its wire channel.

    Order matters: the wan3 check must run before any other family mapping so
    an explicit wan3 selection can never be swallowed by a broader rewrite.
    """
    import os

    settings = config.video_generation
    model = (model_override or settings.model).strip()
    if not model:
        raise RuntimeError("video_generation.model is empty")

    if model_override:
        allowed = settings.allowed_models or []
        if allowed and model_override not in allowed:
            raise RuntimeError(
                f"model '{model_override}' is not in video_generation.allowed_models"
            )

    if is_wan3_model(model):
        return _gateway_route(
            canonicalize_wan3_model_name(model), "task", WAN3_MODEL_TYPE, config
        )
    if is_sd2_model(model):
        return _gateway_route(
            canonicalize_sd2_model_name(model), "sd2", SD2_MODEL_TYPE, config
        )

    # Legacy ark path — byte-identical to the historical _configured_target.
    provider = config.provider.get(settings.provider)
    api_key = (provider.api_key if provider else None) or (
        os.environ.get("DOUBAO_API_KEY", "") if settings.provider == "doubao" else ""
    )
    configured_base = (provider.base_url if provider else None) or (
        os.environ.get("DOUBAO_BASE_URL", "") if settings.provider == "doubao" else ""
    )
    if not api_key:
        raise RuntimeError("DOUBAO_API_KEY is empty")
    base_url = configured_base.rstrip("/")
    if not base_url.startswith("https://") or base_url.endswith(".html"):
        raise RuntimeError(
            "DOUBAO_BASE_URL must be an HTTPS API origin (for example "
            "https://api.tokenspace.net.cn or https://openapi.bossipai.com.cn), "
            "not the documentation page"
        )
    from urllib.parse import urlsplit

    wire_format = (
        "bossip_videos"
        if (urlsplit(base_url).hostname or "").lower() == BOSSIP_RELAY_HOST
        else "tokenspace_contents"
    )
    return VideoRoute(
        provider=settings.provider,
        model=model,
        api_key=api_key,
        base_url=base_url,
        submit_timeout_seconds=settings.submit_timeout_seconds,
        status_timeout_seconds=settings.status_timeout_seconds,
        channel="ark",
        model_type="seedance",
        auth_scheme="bearer",
        wire_format=wire_format,
    )


# ── validation ──────────────────────────────────────────────────────────────

def validate_request(
    route: Any,
    *,
    resolution: str,
    ratio: str,
    duration: int,
    generate_audio: bool,
    input_mimes: list[str],
) -> None:
    channel = getattr(route, "channel", "ark")
    has_video_ref = any(not mime.startswith("image/") for mime in input_mimes)
    if channel == "sd2":
        native = sd2_native_resolution(route.model)
        if resolution and resolution != native:
            raise RuntimeError(
                f"model {route.model} generates {native} natively; requested "
                f"{resolution} would be silently ignored — pick the matching model tier"
            )
        if has_video_ref and canonicalize_sd2_model_name(route.model) == "video-sd-720p-proⅠ":
            # Upstream drops extra_videos for this tier without erroring; the
            # task then succeeds with output unrelated to the reference.
            raise RuntimeError(
                "video-sd-720p-proⅠ silently discards video references upstream; "
                "use video-sd-1080p-pro for video-referenced segments"
            )
        return
    if channel == "task" and getattr(route, "model_type", "") == WAN3_MODEL_TYPE:
        if ratio == "21:9":
            raise RuntimeError("wan3.0 does not support the 21:9 ratio")
        if duration != -1 and not 2 <= duration <= 30:
            raise RuntimeError("wan3.0 duration must be -1 (smart) or 2-30 seconds")
        return
    # ark / Seedance rules (unchanged from the historical validator).
    lowered = route.model.lower()
    if resolution == "1080p" and route.model != "doubao-seedance-2-0-260128":
        raise RuntimeError("1080p is supported only by doubao-seedance-2-0-260128")
    if "2-5" in lowered:
        if duration == -1 or not 4 <= duration <= 30:
            raise RuntimeError("Seedance 2.5 duration must be 4-30 seconds")
    elif duration != -1 and not 4 <= duration <= 15:
        raise RuntimeError("Seedance 2.0 duration must be -1 or 4-15 seconds")
    if "fast" in lowered and generate_audio:
        raise RuntimeError(
            "Seedance Fast does not support generated audio; use the standard model for spoken video"
        )


# ── payload building ────────────────────────────────────────────────────────

def build_payload(
    route: Any,
    *,
    prompt: str,
    refs: list[dict[str, str]],
    resolution: str,
    ratio: str,
    duration: int,
    generate_audio: bool,
    watermark: bool,
) -> tuple[str, dict[str, Any]]:
    """(url_path, json_body) for the gateway channels.

    ``refs`` items: {"kind": "image"|"video", "url": public_url, "role": role}.
    The ark channel keeps its historical builder in video_production.py.
    """
    channel = getattr(route, "channel", "ark")
    if channel == "sd2":
        body: dict[str, Any] = {
            "model": canonicalize_sd2_model_name(route.model),
            "prompt": prompt,
            "resolution": sd2_native_resolution(route.model),
        }
        if ratio and ratio != "adaptive":
            body["ratio"] = ratio
        if 4 <= duration <= 15:
            body["duration"] = duration
        images = [ref["url"] for ref in refs if ref["kind"] == "image"]
        videos = [ref["url"] for ref in refs if ref["kind"] == "video"]
        if images:
            body["image_url"] = images[0]
            if images[1:]:
                body["extra_images"] = images[1:]
        if videos:
            body["extra_videos"] = videos
        return "/v1/videos", body

    if channel == "task":
        is_wan3 = getattr(route, "model_type", "") == WAN3_MODEL_TYPE
        # All references go into metadata.content[] with mandatory roles —
        # the task-channel payload has no top-level images field, so a bare
        # image would be dropped at the gateway.
        content = []
        for ref in refs:
            if ref["kind"] == "image":
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": ref["url"]},
                        "role": ref.get("role") or "reference_image",
                    }
                )
            else:
                content.append(
                    {
                        "type": "video_url",
                        "video_url": {"url": ref["url"]},
                        "role": ref.get("role") or "reference_video",
                    }
                )
        metadata: dict[str, Any] = {
            "resolution": map_wan3_resolution(resolution) if is_wan3 else resolution,
            "ratio": ratio,
            "duration": clamp_wan3_duration(duration) if is_wan3 else duration,
            "generate_audio": generate_audio,
            "watermark": watermark,
        }
        if content:
            metadata["content"] = content
        body = {
            "model": canonicalize_wan3_model_name(route.model) if is_wan3 else route.model,
            "prompt": prompt,
            "metadata": metadata,
        }
        return "/v1/video/generations", body

    raise RuntimeError(f"build_payload does not handle the '{channel}' channel")


# ── HTTP + status normalization ─────────────────────────────────────────────

async def submit(route: Any, path: str, body: dict[str, Any]) -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(
        timeout=route.submit_timeout_seconds, follow_redirects=True
    ) as client:
        response = await client.post(
            f"{route.base_url}{path}",
            headers={"Authorization": auth_header(route), "Content-Type": "application/json"},
            json=body,
        )
    if response.status_code not in (200, 201, 202):
        response.raise_for_status()
    return response.json()


def extract_task_id(route: Any, raw: dict[str, Any]) -> str:
    if getattr(route, "channel", "ark") == "sd2":
        # ONLY the `id` field (task_ prefix). `task_id` is overwritten by
        # upstream on later responses; polling it returns task_not_exist.
        task_id = str(raw.get("id") or "")
    else:
        task_id = str(raw.get("id") or raw.get("task_id") or "")
    if not task_id:
        raise RuntimeError("video provider response did not include a task id")
    return task_id


def _unwrap_task_envelope(raw: dict[str, Any]) -> dict[str, Any]:
    data = raw.get("data")
    return data if isinstance(data, dict) else raw


async def status(route: Any, task_id: str) -> dict[str, Any]:
    import httpx

    channel = getattr(route, "channel", "ark")
    path = f"/v1/videos/{task_id}" if channel == "sd2" else f"/v1/video/generations/{task_id}"
    async with httpx.AsyncClient(
        timeout=route.status_timeout_seconds, follow_redirects=True
    ) as client:
        response = await client.get(
            f"{route.base_url}{path}", headers={"Authorization": auth_header(route)}
        )
    response.raise_for_status()
    raw = response.json()
    return _unwrap_task_envelope(raw) if channel == "task" else raw


def normalize_state(route: Any, data: dict[str, Any]) -> str:
    channel = getattr(route, "channel", "ark")
    value = str(data.get("status") or "")
    if channel == "sd2":
        state = {
            "queued": "queued",
            "pending": "queued",
            "completed": "completed",
            "succeeded": "completed",
            "success": "completed",
            "failed": "failed",
            "error": "failed",
            "cancelled": "cancelled",
            "canceled": "cancelled",
        }.get(value.lower(), "in_progress")
        # Completed-but-no-URL: upstream flips status before the URL lands;
        # keep polling instead of finalizing an empty result.
        if state == "completed" and not result_video_url(route, data):
            return "in_progress"
        return state
    if channel == "task":
        return {
            "QUEUED": "queued",
            "IN_PROGRESS": "in_progress",
            "SUCCESS": "completed",
            "FAILURE": "failed",
            "FAILED": "failed",
        }.get(value.upper(), "in_progress")
    # ark
    return {
        "queued": "queued",
        "pending": "queued",
        "running": "in_progress",
        "in_progress": "in_progress",
        "succeeded": "completed",
        "success": "completed",
        "completed": "completed",
        "failed": "failed",
        "failure": "failed",
        "cancelled": "cancelled",
        "canceled": "cancelled",
    }.get(value.lower(), "in_progress")


def result_video_url(route: Any, data: dict[str, Any]) -> str:
    channel = getattr(route, "channel", "ark")
    if channel == "sd2":
        for candidate in (
            data.get("video_url"),
            data.get("url"),
            data.get("download_url"),
            (data.get("result") or {}).get("video_url") if isinstance(data.get("result"), dict) else None,
            (data.get("result") or {}).get("url") if isinstance(data.get("result"), dict) else None,
            (data.get("data") or {}).get("video_url") if isinstance(data.get("data"), dict) else None,
            (data.get("data") or {}).get("url") if isinstance(data.get("data"), dict) else None,
        ):
            if candidate:
                return str(candidate)
        return ""
    if channel == "task":
        return str(data.get("result_url") or "")
    content = data.get("content") or {}
    if not isinstance(content, dict):
        return ""
    return str(content.get("video_url") or content.get("url") or "")


def failure_detail(route: Any, data: dict[str, Any]) -> str:
    if getattr(route, "channel", "ark") == "task":
        return str(data.get("fail_reason") or data.get("message") or "")
    detail = data.get("error")
    if isinstance(detail, dict):
        return str(detail.get("message") or "")
    return str(detail or "")


# ── content-addressed prompt hash ───────────────────────────────────────────

def compute_prompt_hash(
    *,
    prompt: str,
    model_type: str,
    model_name: str,
    duration: int,
    ratio: str,
    resolution: str,
    inputs: list[dict[str, Any]] | None,
    extra_params: dict[str, Any] | None,
    character_reference_type: str,
    character_identity_id: str | None,
) -> str:
    """Cross-user content key over everything that shapes the output.

    Deliberately excludes user/session/trace/time (cross-user reuse is the
    point) and deliberately INCLUDES inputs and extra_params — same prompt
    with a different reference image, or generate_audio true vs false, must
    never collide (both were real false-hit bugs in the reference system).
    Inputs are identified by content digest ("etag:size"), not per-user asset
    ids, so identical bytes match across users.

    The character fields are separate required arguments rather than another
    optional bag entry because omitting them is silent and unsafe: the same
    portrait routes to a LivenessFace group under "real_person" and an AIGC
    group under "virtual", so a hash blind to them serves a virtual render as
    a verified real-person one — the one path where provenance is the product.
    The identity id is included too, so a consented render never crosses to a
    different identity.
    """
    payload = {
        "prompt": prompt,
        "modelType": model_type,
        "modelName": model_name,
        "durationSec": duration,
        "ratio": ratio,
        "resolution": resolution,
        "inputs": inputs or None,
        "extraParams": extra_params or None,
        "characterReferenceType": character_reference_type,
        "characterIdentityId": character_identity_id,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
