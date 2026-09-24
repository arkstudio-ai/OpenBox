"""RunningHub's separate Turbo text/image APIs behind one video model.

Input assets decide the endpoint; prompt keywords never discard a reference.
This adapter does no scheduling or billing: the existing video job owns both.
"""
from typing import Any

from tool.video_providers import CAPABILITY_HINT, VideoRequestError

MODEL = "MiniMax-H3-Max-Turbo"
PREFIX = "/openapi/v2/minimax/h3-max-turbo"
RATIOS = {"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}
IMAGE_MIMES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}


class SubmissionRejected(VideoRequestError):
    """An explicit business refusal without an accepted provider task."""

    def __init__(self, data: dict[str, Any]):
        self.code = error_code(data)
        super().__init__(error_message(data))


def error_code(data: dict[str, Any]) -> str:
    value = str(data.get("errorCode") or "")
    return value if value.isdecimal() and len(value) <= 12 else "unknown"


def has_error(data: dict[str, Any]) -> bool:
    return data.get("errorCode") not in (None, "", 0, "0")


def error_message(data: dict[str, Any]) -> str:
    # Provider text can echo credentials or signed URLs. Only our own messages
    # cross the tool's public-error boundary.
    code = error_code(data)
    if code == "605":
        return "RunningHub 账户余额不足（605）；请联系管理员充值。"
    return f"RunningHub 视频请求失败（错误码 {code}）；请检查渠道配置和账户状态。"


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
    if any(mime not in IMAGE_MIMES for mime in input_mimes):
        raise VideoRequestError("RunningHub Turbo accepts JPEG/PNG/WEBP frames, not video or audio references; select a reference-capable video model" + CAPABILITY_HINT)
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
