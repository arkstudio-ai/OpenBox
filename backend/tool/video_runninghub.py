"""RunningHub's separate Turbo text/image APIs behind one video model.

Input assets decide the endpoint; prompt keywords never discard a reference.
This adapter does no scheduling or billing: the existing video job owns both.
"""
import asyncio
import hashlib
from io import BytesIO
from typing import Any

from tool.video_providers import CAPABILITY_HINT, VideoRequestError

MODEL = "MiniMax-H3-Max-Turbo"
PREFIX = "/openapi/v2/minimax/h3-max-turbo"
RATIOS = {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}
MAX_FRAME_BYTES = 10_000_000
INPUT_DOWNLOAD_FAILED = "runninghub_input_media_download_failed"


class FrameValidationError(VideoRequestError):
    """The source cannot be read or converted to a Turbo frame."""


def _adapt_frame(data: bytes) -> bytes | None:
    """Return an upload-ready copy, or None if the original already fits."""
    from PIL import Image, ImageOps

    with Image.open(BytesIO(data)) as source:
        width, height = source.size
        within_ratio = 2 * width <= 5 * height and 2 * height <= 5 * width
        if (within_ratio and len(data) <= MAX_FRAME_BYTES
                and source.format in {"JPEG", "PNG", "WEBP"}):
            source.verify()
            return None
        image = ImageOps.exif_transpose(source)
        # Video frames are opaque. Composite any transparent pixels onto the same
        # matte we use for padding, without stretching or cropping the photo.
        if image.mode in {"RGBA", "LA", "PA"} or "transparency" in image.info:
            rgba = image.convert("RGBA")
            image = Image.new("RGB", rgba.size, "black")
            image.paste(rgba, mask=rgba.getchannel("A"))
        else:
            image = image.convert("RGB")
        image.thumbnail((3840, 3840), Image.Resampling.LANCZOS)
        width, height = image.size
        padding_size = (max(width, (2 * height + 4) // 5), max(height, (2 * width + 4) // 5))
        frame = Image.new("RGB", padding_size, "black")
        frame.paste(image, ((frame.width - width) // 2, (frame.height - height) // 2))
        output = BytesIO()
        frame.save(output, format="JPEG", quality=90, optimize=True)
        if output.tell() > MAX_FRAME_BYTES:
            frame.thumbnail((1920, 1920), Image.Resampling.LANCZOS)
            output = BytesIO()
            frame.save(output, format="JPEG", quality=85, optimize=True)
        if output.tell() > MAX_FRAME_BYTES:
            raise ValueError("converted frame too large")
        return output.getvalue()


async def prepare_frame_assets(assets, roles, *, user_id: str, save_copies: bool = False) -> dict[str, str]:
    """Keep source assets intact and store model-only copies when needed."""
    from core.oss import get_oss

    oss = get_oss()

    async def check(asset, role):
        label = "首帧" if role == "first_frame" else "尾帧"
        try:
            data = await oss.get_object(asset.oss_key, timeout=20)
            adapted = await asyncio.to_thread(_adapt_frame, data)
            if adapted is None or not save_copies:
                return None
            digest = hashlib.sha256(adapted).hexdigest()
            key = f"analysis/{user_id}/runninghub-frames/{asset.id}-{digest}.jpg"
            await oss.put_object(key, adapted, content_type="image/jpeg", forbid_overwrite=True)
            return (asset.id, key)
        except Exception as exc:
            raise FrameValidationError(f"快速档{label}暂时无法读取或处理，请稍后重试。") from exc

    sources = {asset.id: (asset, role) for asset, role in zip(assets, roles, strict=True)}
    copies = await asyncio.gather(*(check(asset, role) for asset, role in sources.values()))
    return dict(copy for copy in copies if copy is not None)


class SubmissionRejected(VideoRequestError):
    """An explicit business refusal without an accepted provider task."""

    def __init__(self, data: dict[str, Any]):
        self.code = error_code(data)
        # For server logs only (callers redact it); never part of the message.
        self.provider_message = str(data.get("errorMessage") or "")[:500]
        super().__init__(error_message(data))


def error_code(data: dict[str, Any]) -> str:
    value = str(data.get("errorCode") or "")
    return value if value.isdecimal() and len(value) <= 12 else "unknown"


def has_error(data: dict[str, Any]) -> bool:
    return data.get("errorCode") not in (None, "", 0, "0")


def input_download_failed(data: dict[str, Any]) -> bool:
    reason = str(data.get("errorMessage") or "").lower()
    return error_code(data) == "1007" and (
        "failed to download input media" in reason or "输入素材下载失败" in reason
    )


def terminal_input_download_failure(data: dict[str, Any]) -> bool:
    return (
        str(data.get("status", "")).upper() in {"FAILED", "FAILURE", "ERROR"}
        and input_download_failed(data)
    )


def error_message(data: dict[str, Any], media: str = "视频") -> str:
    # Provider text can echo credentials or signed URLs. Only our own messages
    # cross the tool's public-error boundary.
    code = error_code(data)
    if code == "605":
        return "RunningHub 账户余额不足（605）；请联系管理员充值。"
    if input_download_failed(data):
        return f"RunningHub 下载{media}输入素材失败（1007）。"
    if code == "1007":
        reason = str(data.get("errorMessage") or "").lower()
        if media == "视频" and ("aspect ratio" in reason or "宽高比" in reason):
            return "RunningHub 首尾帧宽高比不符合要求（1007），请调整到 0.4–2.5 后再生成。"
        return f"RunningHub {media}参数校验失败（1007），请检查输入。"
    return f"RunningHub {media}请求失败（错误码 {code}）；请检查渠道配置和账户状态。"


def task_id(data: dict[str, Any]) -> str:
    value = data.get("taskId")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    value = str(value).strip()
    return value if value not in ("", "0", "-1") else ""


def validate(
    model: str, *, resolution: str, ratio: str, duration: int,
    input_mimes: list[str], roles: tuple[str, ...], generate_audio: bool,
    prompt: str | None = None, watermark: bool = False,
) -> None:
    if model != MODEL:
        raise VideoRequestError(f"RunningHub model {model} has no configured adapter" + CAPABILITY_HINT)
    if resolution not in {"480p", "768p"} or not 5 <= duration <= 15:
        raise VideoRequestError("RunningHub Turbo supports 480p/768p and an explicit 5–15 seconds" + CAPABILITY_HINT)
    if prompt is not None and not 1 <= len(prompt) <= 2048:
        raise VideoRequestError("RunningHub Turbo prompt must contain 1–2048 characters")
    if watermark or not generate_audio:
        raise VideoRequestError("RunningHub Turbo has no watermark or mute parameter; use watermark=false and generate_audio=true")
    if any(not mime.startswith("image/") for mime in input_mimes):
        raise VideoRequestError("RunningHub Turbo needs an image for each frame; select a reference-capable video model for video or audio inputs" + CAPABILITY_HINT)
    if len(input_mimes) > 2:
        raise VideoRequestError("RunningHub Turbo accepts one first frame and at most one last frame" + CAPABILITY_HINT)
    if roles and len(roles) != len(input_mimes):
        raise VideoRequestError("Every RunningHub frame must have a matching role")
    frame_roles = roles or tuple("reference_image" for _ in input_mimes)
    if len(input_mimes) == 1 and frame_roles[0] != "first_frame":
        raise VideoRequestError(
            "RunningHub Turbo needs an explicit first_frame selected from the person's intent; "
            "a generic reference image is not automatically a starting frame. Clarify the image's "
            "purpose or select a reference-capable model; do not relabel it just to pass validation"
        )
    if len(input_mimes) == 2 and set(frame_roles) != {"first_frame", "last_frame"}:
        raise VideoRequestError(
            "RunningHub Turbo accepts two images only as intentional first_frame/last_frame endpoints. "
            "For ambiguous intent ask whether these are endpoints, separate shots or combined references; "
            "do not assign roles by upload order or relabel references just to pass validation"
        )
    if input_mimes:
        if ratio != "adaptive":
            raise VideoRequestError("RunningHub image-to-video inherits the first frame's aspect ratio; use ratio=adaptive and provide a frame with the desired shape")
    elif ratio not in RATIOS:
        raise VideoRequestError("RunningHub text-to-video needs an explicit aspect ratio: " + "/".join(sorted(RATIOS)))


def build_payload(route, *, prompt, refs, resolution, ratio, duration, generate_audio, watermark):
    validate(
        route.model, prompt=prompt, resolution=resolution, ratio=ratio, duration=duration,
        generate_audio=generate_audio, watermark=watermark,
        input_mimes=["image/png" if r.get("kind") == "image" else str(r.get("kind")) for r in refs],
        roles=tuple(r.get("role") or "reference_image" for r in refs),
    )
    body = {"prompt": prompt, "resolution": resolution, "duration": str(duration)}
    if not refs:
        return PREFIX + "/text-to-video", {**body, "aspectRatio": ratio}
    first = next(r for r in refs if r.get("role") != "last_frame")
    body["firstFrameUrl"] = first["url"]
    last = next((r for r in refs if r.get("role") == "last_frame"), None)
    if last:
        body["lastFrameUrl"] = last["url"]
    return PREFIX + "/image-to-video", body


def result_url(data: dict[str, Any]) -> str:
    results = data.get("results")
    if not isinstance(results, list):
        return ""
    for result in results:
        if not isinstance(result, dict) or str(result.get("outputType", "")).lower() != "mp4":
            continue
        url = result.get("url")
        if isinstance(url, str) and url.startswith("https://"):
            return url
    return ""


def state(data: dict[str, Any]) -> str:
    value = str(data.get("status") or "").upper()
    # An accepted task with contradictory fields still owns a paid operation.
    # Keep polling its identity rather than inviting a replacement generation.
    if has_error(data) and not (task_id(data) and value in {"QUEUED", "RUNNING"}):
        return "failed"
    if value in {"FAILED", "FAILURE", "ERROR"}:
        return "failed"
    if value in {"CANCELLED", "CANCELED"}:
        return "cancelled"
    if value == "SUCCESS":
        return "completed" if result_url(data) else "in_progress"
    return "queued" if value == "QUEUED" else "in_progress"
