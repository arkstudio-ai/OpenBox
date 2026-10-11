"""RunningHub image models behind the built-in ``image_gen`` tool.

The configured model id selects a model family; whether the call carries input
images selects the endpoint (text-to-image or image-to-image/edit). The HTTP
client, HTTP-200 business rejections (605 余额不足 among them) and the task
query are the video channel's (``tool/video_providers.py`` and
``tool/video_runninghub.py``); this module only shapes requests and reads
results. RunningHub makes one image per task. The ``taskId`` returned by the
submit is the durable receipt: a task that outlives the tool call is finished
later by querying it, never by submitting again.

Parameters follow RunningHub's capability table (HM-RunningHub/OpenClaw_RH_Skills,
capabilities.json 2026-08-18); prices were read from the official
price-preview API on 2026-10-10 (docs/RUNNINGHUB_IMAGE_CHANNEL.md).
"""
from __future__ import annotations

import asyncio
import math
import re
import struct
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from core.log import create_logger

log = create_logger("tool.image_runninghub")

_BASE_RATIOS = ("1:1", "16:9", "9:16", "4:3", "3:4", "3:2", "2:3", "5:4", "4:5", "21:9")
_TALL_RATIOS = ("1:4", "4:1", "1:8", "8:1")
_G2_RATIOS = ("empty", "3:2", "1:1", "2:3", "5:4", "4:5", "16:9", "9:16", "21:9", "3:4", "4:3",
              "9:21", "1:2", "2:1", "1:3", "3:1")
_TIERS = (("1k", 1024), ("2k", 2048), ("4k", 4096))
_IMAGE_TYPES = {"png", "jpg", "jpeg", "webp"}
#: A requested shape this far from every supported ratio is refused, not bent.
_RATIO_TOLERANCE = 0.08
#: The signed input URLs must outlive a queued task, but no more than that.
INPUT_URL_TTL_SECONDS = 3600
_SUBMIT_TIMEOUT_SECONDS = 60
_QUERY_TIMEOUT_SECONDS = 30
_MAX_QUERY_FAILURES = 5
#: Query backoff: 2s, x1.5 per round, at most 10s (live tasks took 38-101s).
POLL_FIRST_DELAY_SECONDS = 2.0
POLL_MAX_DELAY_SECONDS = 10.0
#: Measured 2026-10-10: runninghub.cn answers 40310 for the Nano Banana and
#: GPT Image 2 channels ("因合规要求，该模型在当前CN节点已下线，请前往全球站
#: (runninghub.ai) 接入使用"); Seedream v5 Pro still runs there.
_OFFLINE_CODE = "40310"
_OFFLINE_MESSAGE = ("RunningHub 已因合规要求在当前站点下线该模型（40310）；"
                    "请改用 Seedream v5 Pro，或让管理员配置全球站 runninghub.ai 的凭据")


@dataclass(frozen=True)
class ImageModel:
    id: str
    name: str
    text_endpoint: str
    image_endpoint: str
    #: "ratio": aspectRatio + resolution tier; "size": width/height in pixels.
    shape: str
    text_ratios: tuple[str, ...] = ()
    image_ratios: tuple[str, ...] = ()
    resolutions: tuple[str, ...] = ("1k", "2k", "4k")
    max_inputs: int = 10
    max_input_mb: int = 10
    max_prompt: int = 20_000


#: The registry: id -> endpoints and limits. Display names match RunningHub's.
MODELS: dict[str, ImageModel] = {model.id: model for model in (
    ImageModel("rhart-image-n-pro", "Nano Banana Pro", "rhart-image-n-pro/text-to-image",
               "rhart-image-n-pro/edit", "ratio", _BASE_RATIOS, ("empty", *_BASE_RATIOS)),
    ImageModel("rhart-image-n-g31-flash", "Nano Banana 2", "rhart-image-n-g31-flash/text-to-image",
               "rhart-image-n-g31-flash/image-to-image", "ratio", (*_BASE_RATIOS, *_TALL_RATIOS),
               ("empty", *_BASE_RATIOS, *_TALL_RATIOS), max_input_mb=30),
    ImageModel("rhart-image-g-2", "GPT Image 2", "rhart-image-g-2/text-to-image",
               "rhart-image-g-2/image-to-image", "ratio", _G2_RATIOS, _G2_RATIOS, max_input_mb=30),
    ImageModel("seedream-v5-pro", "Seedream v5 Pro", "seedream-v5-pro/text-to-image",
               "seedream-v5-pro/image-to-image", "size", resolutions=("1k", "2k"), max_prompt=5_000),
)}


class ImageRequestRefused(ValueError):
    """Refused here before any paid call; the message names only the request."""


class ImageTaskRejected(RuntimeError):
    """RunningHub provably did not accept the task (no taskId, nothing charged)."""

    def __init__(self, kind: str, reason: str, *, status: int | None = None,
                 code: str | None = None, detail: str = ""):
        super().__init__(reason)
        self.kind, self.reason, self.status, self.code, self.detail = kind, reason, status, code, detail


class ImageTaskFailed(RuntimeError):
    """An accepted task ended FAILED/CANCELLED: final, and no image exists."""

    def __init__(self, task_id: str, message: str, *, code: str | None = None):
        super().__init__(message)
        self.task_id, self.message, self.code = task_id, message, code


class ImageTaskPending(RuntimeError):
    """An accepted task has no downloadable image yet; reconcile, never resubmit."""

    def __init__(self, task_id: str, reason: str):
        super().__init__(reason)
        self.task_id, self.reason = task_id, reason


@dataclass(frozen=True)
class PlannedRequest:
    endpoint: str
    body: dict[str, Any]
    #: Billing tier ("1k"/"2k"/"4k") that the price-preview API charges.
    tier: str
    #: Says how a WIDTHxHEIGHT request was expressed, when it was translated.
    note: str | None = None


def unsupported(model: ImageModel, *, n: int, mask: bool, background: str | None,
                input_count: int, prompt: str) -> str | None:
    """A request this model cannot take at all, decided before anything is paid."""
    if n != 1:
        return (f"{model.name} makes one image per request; call image_gen once per variant "
                "(each call is billed separately)")
    if mask:
        return f"{model.name} takes no mask; describe the region to change in the prompt instead"
    if background == "transparent":
        return f"{model.name} cannot make a transparent background; ask for a plain or chroma-key one"
    if input_count > model.max_inputs:
        return f"{model.name} accepts at most {model.max_inputs} input images"
    if len(prompt) > model.max_prompt:
        return f"{model.name} accepts prompts of at most {model.max_prompt} characters"
    return None


def _ratio(name: str) -> float:
    left, right = name.split(":")
    return int(left) / int(right)


def _closest_ratio(value: float, options: list[str]) -> str | None:
    best = min(options, key=lambda name: abs(math.log(_ratio(name) / value)))
    return best if abs(_ratio(best) / value - 1) <= _RATIO_TOLERANCE else None


def _tier(long_edge: int, resolutions: tuple[str, ...]) -> str:
    available = [(name, edge) for name, edge in _TIERS if name in resolutions]
    for name, edge in available:
        if edge >= long_edge * 0.9:
            return name
    return available[-1][0]


def image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Width and height from a PNG/JPEG/WebP header, without decoding."""
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return struct.unpack(">II", data[16:24])
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            chunk = data[12:16]
            if chunk == b"VP8X":
                return (int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1)
            if chunk == b"VP8L":
                bits = int.from_bytes(data[21:25], "little")
                return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
            if chunk == b"VP8 ":
                width, height = struct.unpack("<HH", data[26:30])
                return width & 0x3FFF, height & 0x3FFF
        if data.startswith(b"\xff\xd8"):
            index = 2
            while index + 9 < len(data):
                if data[index] != 0xFF:
                    index += 1
                    continue
                marker = data[index + 1]
                if marker == 0xFF:  # fill byte
                    index += 1
                    continue
                if marker == 0x01 or 0xD0 <= marker <= 0xD7:  # markers without a length
                    index += 2
                    continue
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    height, width = struct.unpack(">HH", data[index + 5:index + 9])
                    return width, height
                index += 2 + struct.unpack(">H", data[index + 2:index + 4])[0]
    except (struct.error, IndexError):
        return None
    return None


def plan_request(model: ImageModel, *, prompt: str, size: str, image_urls: list[str],
                 output_format: str, input_size: tuple[int, int] | None = None) -> PlannedRequest:
    """Translate the tool's request into one RunningHub endpoint and body."""
    match = re.fullmatch(r"(\d+)x(\d+)", size)
    if size != "auto" and not match:
        raise ImageRequestRefused("size must be 'auto' or WIDTHxHEIGHT (for example 2048x1152)")
    requested = (int(match.group(1)), int(match.group(2))) if match else None
    if requested and min(requested) <= 0:
        raise ImageRequestRefused("size needs a positive width and height")
    edit = bool(image_urls)
    body: dict[str, Any] = {"prompt": prompt}
    if edit:
        body["imageUrls"] = list(image_urls)
    note = None
    if model.shape == "size":
        if requested:
            width, height = requested
        elif edit and input_size and min(input_size) > 0:
            # Keep the source's shape at the model's default 2k long edge.
            scale = 2048 / max(input_size)
            width, height = (max(240, round(side * scale)) for side in input_size)
        else:
            width, height = 2048, 2048
        if not (240 <= width <= 8192 and 240 <= height <= 8192):
            raise ImageRequestRefused(f"{model.name} accepts width and height from 240 to 8192 pixels")
        body.update(width=width, height=height, outputFormat="png" if output_format == "png" else "jpeg")
        tier = "1k" if width * height <= 1024 * 1024 else "2k"
    else:
        options = model.image_ratios if edit else model.text_ratios
        if requested is None:
            # "auto": keep the source's shape when editing, else the model's
            # own choice where it has one, else square. 2k costs what 1k does.
            ratio, tier = ("empty" if "empty" in options else "1:1"), "2k"
        else:
            width, height = requested
            ratio = _closest_ratio(width / height, [name for name in options if name != "empty"])
            if ratio is None:
                supported = ", ".join(name for name in options if name != "empty")
                raise ImageRequestRefused(
                    f"{model.name} cannot make a {width}x{height} shape; supported aspect ratios: {supported}")
            tier = _tier(max(width, height), model.resolutions)
            note = (f"size {size} was sent to {model.name} as aspect ratio {ratio} at {tier} "
                    "resolution (this model takes a ratio and a tier, not exact pixels)")
        body.update(aspectRatio=ratio, resolution=tier)
    return PlannedRequest(model.image_endpoint if edit else model.text_endpoint, body, tier, note)


def route_for(provider_name: str, model: str, api_key: str, base_url: str | None) -> Any:
    """The RunningHub route (credential + endpoint) that owns one image task."""
    from tool.video_providers import VideoRoute

    if not api_key or not base_url:
        raise RuntimeError(f"provider '{provider_name}' needs api_key and base_url for RunningHub images")
    return VideoRoute(
        provider=provider_name, model=model, api_key=api_key, base_url=base_url.rstrip("/"),
        submit_timeout_seconds=_SUBMIT_TIMEOUT_SECONDS, status_timeout_seconds=_QUERY_TIMEOUT_SECONDS,
        channel="runninghub", model_type="runninghub_image", auth_scheme="bearer",
        wire_format="runninghub_v2",
    )


def configured_route(provider_name: str, model: str, config) -> Any:
    provider = config.provider.get(provider_name)
    return route_for(provider_name, model, getattr(provider, "api_key", None) or "",
                     getattr(provider, "base_url", None))


def _rejection_kind(status: int, text: str) -> str:
    from tool.image_gen import _CONTENT_HINTS, _QUOTA_HINTS

    lowered = text.lower()
    if status == 402 or (status in (401, 403, 429) and any(hint in lowered for hint in _QUOTA_HINTS)):
        return "quota"
    if status in (401, 403):
        return "permission"
    if status == 429:
        return "rate_limit"
    if status == 404:
        return "not_found"
    if any(hint in lowered for hint in _CONTENT_HINTS):
        return "content_policy"
    return "invalid_request" if status in (400, 413, 415, 422) else "rejected"


def _error_text(response: Any) -> str:
    """The message field of an error body, else its (bounded) text."""
    try:
        if len(response.content) > 16_384:
            return ""
        raw = response.json()
    except (ValueError, UnicodeError):
        try:
            return response.text[:4_000]
        except (ValueError, UnicodeError):
            return ""
    if isinstance(raw, dict):
        for key in ("errorMessage", "message", "msg", "error"):
            value = raw.get(key)
            if isinstance(value, dict):
                value = value.get("message") or value.get("msg")
            if isinstance(value, str) and value.strip():
                return value
    return ""


async def submit_task(route: Any, planned: PlannedRequest) -> str:
    """Submit once and return the taskId; definite refusals raise ImageTaskRejected.

    Timeouts, 5xx and replies without a taskId propagate unchanged: the task
    may exist, so they keep the caller's unknown-outcome protection.
    """
    import httpx

    from tool import video_providers
    from tool.image_gen import _redact_provider_text
    from tool.video_runninghub import SubmissionRejected, error_message, task_id

    try:
        raw = await video_providers.submit(route, "/openapi/v2/" + planned.endpoint, planned.body,
                                           purpose="image_generation")
    except SubmissionRejected as exc:
        log.warning("RunningHub refused an image task endpoint=%s code=%s message=%s", planned.endpoint,
                    exc.code, _redact_provider_text(getattr(exc, "provider_message", "")) or "-")
        if exc.code == _OFFLINE_CODE:
            raise ImageTaskRejected("not_found", _OFFLINE_MESSAGE, code=exc.code) from exc
        kind = "quota" if exc.code == "605" else "rejected"
        raise ImageTaskRejected(kind, error_message({"errorCode": exc.code}, "图片"),
                                code=exc.code) from exc
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if not 400 <= status < 500 or status in (408, 409, 499):
            raise
        detail = _redact_provider_text(_error_text(exc.response))
        raise ImageTaskRejected(_rejection_kind(status, detail), f"HTTP {status}",
                                status=status, detail=detail) from exc
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.ProxyError,
            httpx.UnsupportedProtocol) as exc:
        raise ImageTaskRejected("not_sent", f"{type(exc).__name__}: no connection was established") from exc
    accepted = task_id(raw) if isinstance(raw, dict) else ""
    if not accepted:
        raise RuntimeError("RunningHub accepted the request but returned no taskId")
    return accepted


def result_urls(data: dict[str, Any]) -> list[str]:
    results = data.get("results")
    urls = []
    for item in results if isinstance(results, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("url"), str):
            continue
        url, kind = item["url"], str(item.get("outputType") or "").lower().lstrip(".")
        path_kind = url.split("?", 1)[0].rsplit(".", 1)[-1].lower()
        if url.startswith("https://") and (kind or path_kind) in _IMAGE_TYPES:
            urls.append(url)
    return urls


def task_state(data: dict[str, Any]) -> str:
    """queued / running / completed / failed / cancelled for one query reply."""
    from tool.video_runninghub import has_error, task_id

    value = str(data.get("status") or "").upper()
    # An accepted task with contradictory fields still owns a paid operation.
    if has_error(data) and not (task_id(data) and value in {"QUEUED", "RUNNING"}):
        return "failed"
    if value in {"FAILED", "FAILURE", "ERROR"}:
        return "failed"
    if value in {"CANCELLED", "CANCELED"}:
        return "cancelled"
    if value == "SUCCESS":
        # A success without an image URL yet is polled again, as for video.
        return "completed" if result_urls(data) else "running"
    return "queued" if value == "QUEUED" else "running"


def failure_message(data: dict[str, Any]) -> str:
    """Our own words for a failed task; provider text stays in the logs."""
    from tool.image_gen import _CONTENT_HINTS, _redact_provider_text
    from tool.video_runninghub import error_code

    reason = " ".join(str(data.get(key) or "") for key in ("errorMessage", "failedReason"))
    log.warning("RunningHub image task failed task_id=%s code=%s reason=%s", data.get("taskId"),
                error_code(data), _redact_provider_text(reason) or "-")
    if any(hint in reason.lower() for hint in _CONTENT_HINTS):
        return "内容审核未通过：请调整提示词或输入图片"
    if str(data.get("status") or "").upper() in {"CANCELLED", "CANCELED"}:
        return "任务已被取消"
    return f"RunningHub 图片生成失败（错误码 {error_code(data)}）"


async def wait_for_task(route: Any, task_id: str, *, timeout: float,
                        progress: Callable[[str, float], Awaitable[None]] | None = None,
                        should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Poll /openapi/v2/query with backoff until the task ends or time runs out."""
    from tool import video_providers
    from tool.video_runninghub import error_code

    started = time.monotonic()
    delay, failures = POLL_FIRST_DELAY_SECONDS, 0
    while True:
        try:
            data = await video_providers.status(route, task_id, operation="image_status")
            failures = 0
        except Exception as exc:
            from question.runtime import RunRevoked

            if isinstance(exc, RunRevoked):
                raise
            failures += 1
            log.warning("RunningHub image query failed task_id=%s attempt=%s type=%s",
                        task_id, failures, type(exc).__name__)
            if failures >= _MAX_QUERY_FAILURES:
                raise ImageTaskPending(task_id, "its status could not be read") from exc
            data = None
        if data is not None:
            state = task_state(data)
            if state == "completed":
                return data
            if state in ("failed", "cancelled"):
                raise ImageTaskFailed(task_id, failure_message(data), code=error_code(data))
            if progress is not None:
                await progress(state, time.monotonic() - started)
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0 or (should_stop is not None and should_stop()):
            raise ImageTaskPending(task_id, f"it did not finish within {int(timeout)}s")
        await asyncio.sleep(min(delay, remaining))
        delay = min(delay * 1.5, POLL_MAX_DELAY_SECONDS)
