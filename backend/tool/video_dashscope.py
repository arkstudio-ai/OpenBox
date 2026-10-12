"""Wan 3.0's native DashScope protocol (not the new-api gateway dialect).

https://help.aliyun.com/zh/model-studio/wan3-video-generation-api-reference
"""
from collections import Counter
from typing import Any

SUBMIT_PATH = "/api/v1/services/aigc/video-generation/video-synthesis"
FRAME_ROLES = {"first_frame", "last_frame"}
LIMITS = {"first_frame": 1, "last_frame": 1, "reference_image": 10,
          "reference_video": 5, "reference_audio": 5}


def validate(*, resolution: str, ratio: str, duration: int,
             input_mimes: list[str], roles: tuple[str, ...], prompt: str | None) -> None:
    from tool.video_providers import VideoRequestError

    if resolution not in {"480p", "720p", "1080p"}:
        raise VideoRequestError("Wan 3.0 supports 480p/720p/1080p")
    if ratio not in {"adaptive", "21:9", "16:9", "4:3", "1:1", "3:4", "9:16"}:
        raise VideoRequestError("unsupported Wan 3.0 aspect ratio")
    if duration != -1 and not 2 <= duration <= 30:
        raise VideoRequestError("Wan 3.0 duration must be -1 or 2-30 seconds")
    if prompt is not None and (not prompt.strip() or len(prompt) > 20_000):
        raise VideoRequestError("Wan 3.0 prompt must contain 1-20000 characters")
    if roles and len(roles) != len(input_mimes):
        raise VideoRequestError("each Wan 3.0 input needs its own reference role")
    effective = roles or tuple("reference_" + mime.split("/")[0] for mime in input_mimes)
    counts = Counter(effective)
    for role, mime in zip(effective, input_mimes):
        kind = "image" if role in FRAME_ROLES else role.removeprefix("reference_")
        if role not in LIMITS or not mime.startswith(kind + "/"):
            raise VideoRequestError("Wan 3.0 reference role does not match the asset media type")
        if counts[role] > LIMITS[role]:
            raise VideoRequestError(f"Wan 3.0 accepts at most {LIMITS[role]} {role} inputs")
    if FRAME_ROLES & counts.keys() and counts.keys() - FRAME_ROLES:
        raise VideoRequestError("Wan 3.0 first/last frames cannot be mixed with reference image/video/audio")
    if counts["last_frame"] and not counts["first_frame"]:
        raise VideoRequestError("Wan 3.0 last_frame requires first_frame")


def build_payload(route: Any, *, prompt: str, refs: list[dict[str, str]],
                  resolution: str, ratio: str, duration: int, generate_audio: bool,
                  watermark: bool, seed: int | None) -> tuple[str, dict[str, Any]]:
    from tool.video_providers import VideoRequestError

    validate(resolution=resolution, ratio=ratio, duration=duration, prompt=prompt,
             input_mimes=[ref["kind"] + "/unknown" for ref in refs],
             roles=tuple(ref.get("role") or "reference_" + ref["kind"] for ref in refs))
    parameters: dict[str, Any] = {
        "resolution": resolution.upper(), "ratio": ratio, "duration": duration,
        "audio": generate_audio, "watermark": watermark,
        # The skill already supplies the approved full shot and spoken text.
        "prompt_extend": False,
    }
    if seed is not None:
        if not -1 <= seed <= 2147483647:
            raise VideoRequestError("Wan 3.0 seed must be -1 or 0-2147483647")
        parameters["seed"] = seed
    inputs: dict[str, Any] = {"prompt": prompt}
    if refs:
        inputs["media"] = [{"type": ref.get("role") or "reference_" + ref["kind"],
                            "url": ref["url"]} for ref in refs]
    return SUBMIT_PATH, {"model": route.model, "input": inputs, "parameters": parameters}


def output(data: dict[str, Any]) -> dict[str, Any]:
    value = data.get("output")
    return value if isinstance(value, dict) else {}


def state(data: dict[str, Any]) -> str:
    value = str(output(data).get("task_status") or "").upper()
    return {"PENDING": "queued", "RUNNING": "in_progress", "SUCCEEDED": "completed",
            "FAILED": "failed", "CANCELED": "cancelled", "UNKNOWN": "failed"}.get(value, "in_progress")


def failure_detail(data: dict[str, Any]) -> str:
    info = output(data)
    if info.get("task_status") == "UNKNOWN":
        return "阿里云任务不存在或查询已过期，请核对原任务记录，不要自动重新生成。"
    # Never expose arbitrary upstream text, prompts or signed asset URLs.
    code = info.get("code") or data.get("code")
    reasons = {
        "InvalidParameter": "阿里云视频参数校验失败，请检查时长、素材角色与模型能力。",
        "InvalidApiKey": "阿里云视频鉴权失败，请检查密钥及所属地域。",
        "DataInspectionFailed": "阿里云视频内容检查未通过。",
        "Arrearage": "阿里云账户余额或额度不足。",
        "ModelNotFound": "阿里云当前账户或地域无法使用所选视频模型。",
    }
    return reasons.get(code, "阿里云视频任务失败，请通过已保存的任务 ID 核对服务端记录。")
