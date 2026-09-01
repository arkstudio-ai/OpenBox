"""Multi-channel video provider routing (ported from bossip's executor layer).

Three wire channels behind one route object:

- ``ark``  — the existing Volcano-Ark shape (``/api/v3/contents/generations/tasks``),
  used by the ``bossip`` relay credential (historically named ``doubao``,
  before the gateway moved off the doubao endpoint). Submission for this
  channel stays in ``tool/video_production.py``; this module only routes.
- ``sd2``  — new-api Sora adaptor: ``POST /v1/videos`` / ``GET /v1/videos/{id}``,
  lowercase statuses, references as top-level public URLs.
- ``task`` — new-api task channel: ``POST /v1/video/generations`` with an
  ``{code, message, data}`` envelope and uppercase statuses. Used by Wan3.0;
  the wan3 protocol shim (wan3-video-adapter) is deployed with new-api itself.

Which model speaks which protocol is *declarable* — a ``video_generation.models``
entry binds a model to a channel and a credential, so adding a model that
speaks an existing protocol needs no release. What stays in code is the
protocol itself: no config schema can express "poll ``metadata.url``" or
"unwrap a ``{code,message,data}`` envelope". Deployments that declare nothing
keep the historical name-inference below, including the U+2160 ``Ⅰ``
canonicalization. Hard lessons from bossip carried over verbatim:

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
import os
import re
from dataclasses import dataclass
from typing import Any, Literal

from core.log import create_logger

log = create_logger("tool.video_providers")


class VideoRequestError(RuntimeError):
    """A request this backend refuses on its own, before any provider call.

    These messages are authored here and name only the caller's own request —
    a model id, a ratio, a duration — so they carry none of the provider
    bodies or signed URLs that ``_public_error`` scrubs. Surfacing them is the
    whole point: a caller told "invalid" without being told *what* is invalid
    cannot fix the request, which would make the free estimate useless.
    """

    public_message = True

# ── sd2 (Sora adaptor) ──────────────────────────────────────────────────────
# The trailing Ⅰ on the first two models is ROMAN NUMERAL ONE (U+2160), not
# the letter I; the wrong character makes the gateway report model-not-found.
SD2_MODEL_TYPE = "sd2_video"
SD2_MODELS = ("seedance-2.0-480-fastⅠ", "video-sd-720p-proⅠ", "video-sd-1080p-pro")

_SD2_TRAILING_I = re.compile(r"[iIⅠ]$")

# ── ark relay host ──────────────────────────────────────────────────────────
BOSSIP_RELAY_HOST = "openapi.bossipai.com.cn"

#: Provider-entry names that may fall back to the relay credential environment.
#: ``bossip`` is the current name; ``doubao`` predates the gateway's move off
#: the doubao endpoint and is kept so an un-migrated deployment still resolves.
RELAY_PROVIDER_NAMES = ("bossip", "doubao")

_LEGACY_ENV_WARNED: set[str] = set()


def relay_env(provider_name: str, suffix: str) -> str:
    """Read ``BOSSIP_<suffix>``, falling back once to the legacy ``DOUBAO_`` name.

    The fallback warns rather than failing: a deployment whose ``.env`` still
    says ``DOUBAO_API_KEY`` keeps working through one release, and the warning
    is what tells the operator to rename it.
    """
    if provider_name not in RELAY_PROVIDER_NAMES:
        return ""
    value = os.environ.get(f"BOSSIP_{suffix}", "")
    if value:
        return value
    legacy = os.environ.get(f"DOUBAO_{suffix}", "")
    if legacy and suffix not in _LEGACY_ENV_WARNED:
        _LEGACY_ENV_WARNED.add(suffix)
        log.warning(
            f"DOUBAO_{suffix} is deprecated; rename it to BOSSIP_{suffix}. "
            "The legacy name will stop being read in a future release."
        )
    return legacy

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


def sd2_native_resolution(name: str) -> str | None:
    """The one resolution a name-encoded sd2 model produces, if it is one.

    Only `SD2_MODELS` carry their resolution in the name — there is a separate
    model id per tier, and asking a 720p model for 1080p silently returns 720p.
    Other models routed over this channel (wan3, MiniMax) pick resolution as a
    parameter, so returning "1080p" for them was a guess that then failed its
    own check: it made every non-1080p choice on those models unreachable.
    """
    if not is_sd2_model(name):
        return None
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


def provider_route_fingerprint(route: Any) -> str:
    """Return a non-secret identity for the route that owns a provider task.

    Provider task ids are scoped to an endpoint/account, not just a model. A
    later config change must therefore not send an old id to today's route.
    The credential is hashed before it enters the canonical route document and
    that document is hashed again, so neither the API key nor its standalone
    digest is persisted in ``video_jobs.request_data``.
    """
    api_key_sha256 = hashlib.sha256(
        str(getattr(route, "api_key", "") or "").encode("utf-8")
    ).hexdigest()
    identity = {
        "provider": str(getattr(route, "provider", "") or "").strip(),
        "channel": str(getattr(route, "channel", "ark") or "ark").strip().lower(),
        "wire_format": str(
            getattr(route, "wire_format", "tokenspace_contents")
            or "tokenspace_contents"
        )
        .strip()
        .lower(),
        "base_url": str(getattr(route, "base_url", "") or "").strip().rstrip("/"),
        "auth_scheme": str(
            getattr(route, "auth_scheme", "bearer") or "bearer"
        )
        .strip()
        .lower(),
        "api_key_sha256": api_key_sha256,
    }
    canonical = json.dumps(
        identity,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "v1:" + hashlib.sha256(canonical).hexdigest()


def provider_route_mismatch(request_data: Any, route: Any) -> str | None:
    """Explain why ``route`` must not operate on a persisted provider task.

    New jobs carry a complete route fingerprint.  A legacy row without one is
    never safe to poll automatically: matching wire formats still do not prove
    that the endpoint or credential/account is the one that accepted the paid
    task.  Its stored wire is used only to make the quarantine reason useful.
    """
    snapshot = request_data if isinstance(request_data, dict) else {}
    if "provider_route_fingerprint" in snapshot:
        submitted = snapshot.get("provider_route_fingerprint")
        if not isinstance(submitted, str) or not submitted.strip():
            return "stored provider route fingerprint is invalid"
        if submitted.strip() != provider_route_fingerprint(route):
            return "stored provider route fingerprint differs from the current route"
        return None

    submitted_wire = str(
        snapshot.get("provider_wire_format") or "tokenspace_contents"
    ).strip().lower()
    current_wire = str(
        getattr(route, "wire_format", "tokenspace_contents")
        or "tokenspace_contents"
    ).strip().lower()
    if submitted_wire != current_wire:
        return (
            f"legacy submitted wire {submitted_wire!r} differs from current wire "
            f"{current_wire!r}"
        )
    return (
        f"legacy submitted wire {submitted_wire!r} has no complete provider "
        "route fingerprint"
    )


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


def declared_model(model: str, config) -> Any | None:
    """The ``video_generation.models`` entry for this id, if declared."""
    for entry in getattr(config.video_generation, "models", None) or []:
        if entry.id == model:
            return entry
    return None


def _declared_route(entry: Any, config) -> VideoRoute:
    """Build a route from a declared model, so adding one needs no release.

    Only the *binding* is declarative — channel, credential, limits. How each
    channel talks stays in this module's adapters, because that is the part a
    config schema cannot express.
    """
    settings = config.video_generation
    channel = entry.channel
    if channel == "ark":
        return _ark_route(entry.id, config, provider_name=entry.provider or settings.provider)
    model_type = WAN3_MODEL_TYPE if channel == "task" else SD2_MODEL_TYPE
    provider_name = entry.provider or (settings.channel_providers or {}).get(channel, "")
    if not provider_name:
        raise RuntimeError(
            f"model '{entry.id}' is declared on the '{channel}' channel, but no "
            f"credential is bound — set its `provider`, or add a "
            f"'{channel}' entry to video_generation.channel_providers"
        )
    provider = config.provider.get(provider_name)
    if not provider or not provider.api_key or not provider.base_url:
        raise RuntimeError(
            f"provider '{provider_name}' needs api_key and base_url for the "
            f"'{channel}' video channel"
        )
    return VideoRoute(
        provider=provider_name,
        model=entry.id,
        api_key=provider.api_key,
        base_url=provider.base_url.rstrip("/"),
        submit_timeout_seconds=settings.submit_timeout_seconds,
        status_timeout_seconds=settings.status_timeout_seconds,
        channel=channel,
        model_type=model_type,
        auth_scheme=(provider.options or {}).get("auth_scheme", "bearer"),
    )


def resolve_route(model_override: str | None, config) -> VideoRoute:
    """Route a model name to its wire channel.

    A declared ``video_generation.models`` entry wins outright; without one the
    historical name-inference applies, so an existing deployment that declares
    nothing keeps behaving exactly as before.

    Order matters in the inference path: the wan3 check must run before any
    other family mapping so an explicit wan3 selection can never be swallowed
    by a broader rewrite.
    """
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

    declared = list(getattr(settings, "models", None) or [])
    entry = declared_model(model, config)
    if entry is not None:
        return _declared_route(entry, config)
    if declared:
        # Declarations are the whole list, not a set of hints. Falling back to
        # name inference here would let a near-miss id ("wan3.0" for a declared
        # "wan3.0-video") silently contradict the deployment and bill against a
        # channel it never enabled — observed in the browser, 2026-08-29.
        raise RuntimeError(
            f"model '{model}' is not declared in video_generation.models; "
            f"available: {', '.join(m.id for m in declared)}"
        )

    if is_wan3_model(model):
        return _gateway_route(
            canonicalize_wan3_model_name(model), "task", WAN3_MODEL_TYPE, config
        )
    if is_sd2_model(model):
        return _gateway_route(
            canonicalize_sd2_model_name(model), "sd2", SD2_MODEL_TYPE, config
        )

    return _ark_route(model, config, provider_name=settings.provider)


def _ark_route(model: str, config, *, provider_name: str) -> VideoRoute:
    """The ark path — byte-identical to the historical _configured_target."""
    from urllib.parse import urlsplit

    settings = config.video_generation
    provider = config.provider.get(provider_name)
    api_key = (provider.api_key if provider else None) or relay_env(provider_name, "API_KEY")
    configured_base = (provider.base_url if provider else None) or relay_env(
        provider_name, "BASE_URL"
    )
    if not api_key:
        raise RuntimeError("BOSSIP_API_KEY is empty")
    base_url = configured_base.rstrip("/")
    if not base_url.startswith("https://") or base_url.endswith(".html"):
        raise RuntimeError(
            "BOSSIP_BASE_URL must be an HTTPS API origin (for example "
            "https://openapi.bossipai.com.cn), not the documentation page"
        )
    wire_format = (
        "bossip_videos"
        if (urlsplit(base_url).hostname or "").lower() == BOSSIP_RELAY_HOST
        else "tokenspace_contents"
    )
    return VideoRoute(
        provider=provider_name,
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

def _validate_declared(
    entry: Any,
    *,
    resolution: str,
    ratio: str = "",
    duration: int,
    has_video_ref: bool,
    has_image_ref: bool,
    roles: tuple[str, ...] = (),
) -> None:
    """Refuse what a declared model cannot do, before it costs anything.

    Gateways fail in two different ways and both are expensive. Some drop a
    parameter they do not understand, substitute their own default and bill
    for it, so the task comes back "successful" having ignored half the
    request; others reject it outright after the request has already been
    routed. The declared limits below are the only place either can be caught
    for free.
    """
    allowed = list(entry.resolutions or [])
    if resolution and allowed and resolution not in allowed:
        raise VideoRequestError(
            f"model {entry.id} supports {'/'.join(allowed)}; requested "
            f"{resolution} would be silently substituted upstream"
        )
    ratios = list(getattr(entry, "ratios", None) or [])
    if ratio and ratios and ratio not in ratios:
        raise VideoRequestError(
            f"model {entry.id} supports ratios {'/'.join(ratios)}; requested {ratio}"
        )
    span = getattr(entry, "duration_range", None)
    if duration == -1:
        if not getattr(entry, "supports_smart_duration", True):
            raise VideoRequestError(
                f"model {entry.id} needs an explicit duration; -1 smart duration is unsupported"
            )
    elif span is not None:
        low, high = span
        if not low <= duration <= high:
            raise VideoRequestError(
                f"model {entry.id} accepts {low}-{high}s; requested {duration}s"
            )
    cap = entry.max_duration_seconds
    if cap is not None and duration != -1 and duration > cap:
        raise VideoRequestError(f"model {entry.id} accepts at most {cap}s; requested {duration}s")
    if has_video_ref and not entry.supports_reference_video:
        raise VideoRequestError(f"model {entry.id} does not accept video references")
    if has_image_ref and not entry.supports_reference_image:
        raise VideoRequestError(f"model {entry.id} does not accept image references")
    # A seed is deliberately NOT a refusal. Missing it costs reproducibility,
    # not content — the video is still the one that was asked for — whereas
    # refusing costs the whole generation. The caller is told it was dropped.
    # Frame roles and reference audio are different: those change what the
    # video *is*, so they stay refusals below.
    frame_roles = {"first_frame", "last_frame"}
    if frame_roles & set(roles) and not getattr(entry, "supports_first_last_frame", False):
        raise VideoRequestError(
            f"model {entry.id} does not accept first_frame/last_frame references"
        )
    if "reference_audio" in roles and not getattr(entry, "supports_reference_audio", False):
        raise VideoRequestError(f"model {entry.id} does not accept an audio reference")


def validate_request(
    route: Any,
    *,
    resolution: str,
    ratio: str,
    duration: int,
    generate_audio: bool,
    input_mimes: list[str],
    declared: Any | None = None,
    roles: tuple[str, ...] = (),
) -> None:
    channel = getattr(route, "channel", "ark")
    has_video_ref = any(not mime.startswith("image/") for mime in input_mimes)
    if declared is not None and generate_audio and not getattr(
        declared, "supports_generated_audio", True
    ):
        raise VideoRequestError(
            f"model {declared.id} renders silent video; pass generate_audio=false "
            "for a b-roll shot, or pick a model that speaks"
        )
    if declared is not None:
        _validate_declared(
            declared,
            resolution=resolution,
            ratio=ratio,
            duration=duration,
            has_video_ref=has_video_ref,
            has_image_ref=any(mime.startswith("image/") for mime in input_mimes),
            roles=roles,
        )
    # first_frame / last_frame / reference_audio have no place to live in the
    # sd2 body (it carries bare image_url / extra_* lists with no role field).
    # Refuse instead of sending a reference the gateway will read as an
    # ordinary one: an undeclared role that silently becomes a plain reference
    # produces a paid take that ignores the continuity the caller asked for.
    if channel == "sd2" and sd2_native_resolution(route.model) is not None:
        # Only the name-encoded tiers are stuck with the flat body. Everything
        # else on this channel reaches the task adaptor through `metadata`,
        # where content[] carries an explicit role.
        unsupported = {"first_frame", "last_frame", "reference_audio"} & set(roles)
        if unsupported:
            raise VideoRequestError(
                f"model {route.model} takes references as a flat list and cannot "
                f"express the {'/'.join(sorted(unsupported))} role; use a model "
                "that accepts frame roles"
            )
    if channel == "sd2":
        native = sd2_native_resolution(route.model)
        if native and resolution and resolution != native:
            raise VideoRequestError(
                f"model {route.model} generates {native} natively; requested "
                f"{resolution} would be silently ignored — pick the matching model tier"
            )
        if has_video_ref and canonicalize_sd2_model_name(route.model) == "video-sd-720p-proⅠ":
            # Upstream drops extra_videos for this tier without erroring; the
            # task then succeeds with output unrelated to the reference.
            raise VideoRequestError(
                "video-sd-720p-proⅠ silently discards video references upstream; "
                "use video-sd-1080p-pro for video-referenced segments"
            )
        return
    if channel == "task" and getattr(route, "model_type", "") == WAN3_MODEL_TYPE:
        if ratio == "21:9":
            raise VideoRequestError("wan3.0 does not support the 21:9 ratio")
        if duration != -1 and not 2 <= duration <= 30:
            raise VideoRequestError("wan3.0 duration must be -1 (smart) or 2-30 seconds")
        return
    # ark / Seedance rules (unchanged from the historical validator).
    lowered = route.model.lower()
    # A declared model states its own resolutions, and _validate_declared has
    # already checked them. Naming one model here predates the registry and
    # made every model added since unable to offer 1080p.
    if declared is None and resolution == "1080p" and route.model != "doubao-seedance-2-0-260128":
        raise VideoRequestError("1080p is supported only by doubao-seedance-2-0-260128")
    if "2-5" in lowered:
        if duration == -1 or not 4 <= duration <= 30:
            raise VideoRequestError("Seedance 2.5 duration must be 4-30 seconds")
    elif duration != -1 and not 4 <= duration <= 15:
        raise VideoRequestError("Seedance 2.0 duration must be -1 or 4-15 seconds")
    if "fast" in lowered and generate_audio:
        raise VideoRequestError(
            f"{route.model} renders silent video; pass generate_audio=false for a "
            "b-roll shot, or pick a standard tier for anything spoken"
        )


# ── payload building ────────────────────────────────────────────────────────

#: Placeholder the relay's multi-material path binds to `images[i]`.
_IMAGE_FILE_REF = re.compile(r"@image_file_(\d+)")


def _with_image_file_refs(prompt: str, count: int) -> str:
    """Ensure every supplied image is named in the prompt.

    The relay binds `images[i]` to an `@image_file_{i+1}` mention; an image
    the prompt never mentions is simply not used, which is exactly the
    "reference ignored, task succeeds" failure this module exists to prevent.
    A caller that already wrote the placeholders keeps its own wording.
    """
    if count <= 0:
        return prompt
    mentioned = {int(n) for n in _IMAGE_FILE_REF.findall(prompt)}
    missing = [index for index in range(1, count + 1) if index not in mentioned]
    if not missing:
        return prompt
    lead = "，".join(f"@image_file_{index}" for index in missing)
    return (
        f"{lead} 是本片参考素材，保持其中人物的五官、脸型、发型与服装完全一致。\n"
        f"{prompt}"
    )


#: Portrait/landscape pixel pairs per resolution tier, for adaptors that take
#: a `WxH` string rather than a tier name.
#: Short/long pixel pair per tier name. Includes the tiers only MiniMax uses
#: (512p, 2k) so a model's own vocabulary survives the trip: its adaptor reads
#: the numbers back out of the string, so an unmapped tier would silently
#: become the default one.
_SIZE_BY_RESOLUTION = {
    "480p": (480, 854),
    "512p": (512, 912),
    "720p": (720, 1280),
    "768p": (768, 1344),
    "1080p": (1080, 1920),
    "2k": (1440, 2560),
}


def _size_shaped_body(
    route: Any,
    *,
    prompt: str,
    refs: list[dict[str, str]],
    resolution: str,
    ratio: str,
    duration: int,
) -> dict[str, Any]:
    """A `WxH` top-level `size`, which the adaptor parses into its own tiers."""
    short, long = _SIZE_BY_RESOLUTION.get(resolution, _SIZE_BY_RESOLUTION["720p"])
    portrait = ratio in ("9:16", "3:4") or not ratio
    width, height = (short, long) if portrait else (long, short)

    body: dict[str, Any] = {
        "model": route.model,
        "prompt": prompt,
        "size": f"{width}x{height}",
    }
    if duration != -1:
        body["duration"] = duration
    images = [ref["url"] for ref in refs if ref["kind"] == "image"]
    if images:
        body["images"] = images
        body["prompt"] = _with_image_file_refs(prompt, len(images))
    return body


def _task_shaped_body(
    route: Any,
    *,
    prompt: str,
    refs: list[dict[str, str]],
    resolution: str,
    ratio: str,
    duration: int,
    generate_audio: bool,
    watermark: bool,
    seed: int | None,
) -> dict[str, Any]:
    """The `metadata`-carried shape the gateway's task adaptor actually reads.

    Its requestPayload has Resolution, Ratio, Duration, Seed, GenerateAudio and
    Content[] — every one of them unmarshalled from `metadata`, never from the
    top level, where the video DTO has no field to hold them.

    Images additionally travel top-level as `images`: the adaptor appends those
    to Content[] itself before merging metadata, which is why that one field
    worked while `image_url` did not.
    """
    metadata: dict[str, Any] = {
        "resolution": resolution,
        "generate_audio": generate_audio,
        "watermark": watermark,
    }
    if ratio and ratio != "adaptive":
        metadata["ratio"] = ratio
    if duration != -1:
        metadata["duration"] = duration
    if seed is not None:
        metadata["seed"] = seed

    content = [
        {
            "type": f"{ref['kind']}_url",
            f"{ref['kind']}_url": {"url": ref["url"]},
            "role": ref.get("role") or f"reference_{ref['kind']}",
        }
        for ref in refs
    ]
    if content:
        metadata["content"] = content

    body: dict[str, Any] = {"model": route.model, "prompt": prompt, "metadata": metadata}
    images = [ref["url"] for ref in refs if ref["kind"] == "image"]
    if images:
        body["images"] = images
        body["prompt"] = _with_image_file_refs(prompt, len(images))
    return body


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
    seed: int | None = None,
    declared: Any | None = None,
) -> tuple[str, dict[str, Any]]:
    """(url_path, json_body) for the gateway channels.

    ``declared`` carries the model's wire_shape. Which body a channel reads is
    a property of the gateway adaptor behind it, not of the model name, and
    the three shapes are mutually unreadable: a resolution sent in the wrong
    place is accepted and ignored (or, on MiniMax, rejected outright).

    ``refs`` items: {"kind": "image"|"video", "url": public_url, "role": role}.
    The ark channel keeps its historical builder in video_production.py.
    """
    channel = getattr(route, "channel", "ark")
    if channel == "sd2":
        native = sd2_native_resolution(route.model)
        shape = getattr(declared, "wire_shape", None) if declared else None
        if shape == "size":
            # The adaptor parses its own resolution tiers out of a WxH string
            # and rejects the request outright without one — measured: sending
            # `resolution` instead returned "文生视频 ratio 不能为空".
            return "/v1/videos", _size_shaped_body(
                route, prompt=prompt, refs=refs, resolution=resolution,
                ratio=ratio, duration=duration,
            )
        if shape == "metadata" or (shape is None and native is None):
            # Not a name-encoded tier, so this model reaches the gateway's task
            # adaptor, whose requestPayload reads resolution / ratio / seed /
            # generate_audio / content[] **only out of `metadata`**. The
            # top-level video DTO has no field for any of them, and Go drops
            # unknown keys silently — measured 2026-09-01, asking for 720p/9:16
            # at the top level returned 1920x1080, the upstream default, while
            # the same values under `metadata` returned 720x1280 exactly.
            return "/v1/videos", _task_shaped_body(
                route, prompt=prompt, refs=refs, resolution=resolution,
                ratio=ratio, duration=duration, generate_audio=generate_audio,
                watermark=watermark, seed=seed,
            )

        body: dict[str, Any] = {
            "model": canonicalize_sd2_model_name(route.model),
            "prompt": prompt,
            "resolution": native,
        }
        if ratio and ratio != "adaptive":
            body["ratio"] = ratio
        # Send whatever explicit duration survived validation. The old 4-15
        # clamp was Seedance's range applied to every sd2 model, which silently
        # dropped a legal wan3 duration (2-30) and billed for the default.
        if duration != -1:
            body["duration"] = duration
        if seed is not None:
            body["seed"] = seed
        images = [ref["url"] for ref in refs if ref["kind"] == "image"]
        videos = [ref["url"] for ref in refs if ref["kind"] == "video"]
        if images:
            if is_wan3_model(route.model):
                # Measured 2026-09-01: wan3 behind this relay ignores
                # image_url outright — five variants each produced a
                # different person. `images` plus an @image_file_N mention in
                # the prompt is the documented multi-material path, and it is
                # the one that actually locks the face. Seedance accepts
                # either, so only wan3 is special-cased.
                body["images"] = images
                body["prompt"] = _with_image_file_refs(prompt, len(images))
            else:
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
        if seed is not None:
            metadata["seed"] = seed
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
            # Observed on the BossIP relay (2026-08-29): a completed
            # `GET /v1/videos/{id}` carries the OSS link under `metadata.url`
            # and nowhere else, so omitting it finalizes to "completed without
            # a video URL" after the generation has already been paid for.
            (data.get("metadata") or {}).get("url") if isinstance(data.get("metadata"), dict) else None,
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
) -> str:
    """Cross-user content key over everything that shapes the output.

    Deliberately excludes user/session/trace/time (cross-user reuse is the
    point) and deliberately INCLUDES inputs and extra_params — same prompt
    with a different reference image, or generate_audio true vs false, must
    never collide (both were real false-hit bugs in the reference system).
    Inputs are identified by content digest ("etag:size"), not per-user asset
    ids, so identical bytes match across users.

    A person reference is an ordinary image input. Its content digest already
    participates in this key, while provider-side materialization is owned by
    the configured relay and does not change the logical generation request.
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
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
