"""MiniMax H3 via the direct Turbo server or the legacy Jupyter Workbench."""

import asyncio
import hashlib
import json
import math
import re
import ssl
import subprocess
import tempfile
import uuid
import wave
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from tool.video_providers import VideoRequestError

SUBMIT_PATH = "/prompt"
MODEL = "ComfyUI-MiniMax-H3"
MEGAPIXELS = {"480p": 0.43, "720p": 0.69, "768p": 1.03}
RATIOS = {"9:16", "16:9"}
IMAGE_EXT = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
VIDEO_EXT = {"video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm", "video/x-m4v": ".m4v"}
AUDIO_FORMAT = {
    "audio/wav": "wav", "audio/wave": "wav", "audio/x-wav": "wav", "audio/vnd.wave": "wav",
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "mov", "audio/m4a": "mov", "audio/x-m4a": "mov",
    "audio/flac": "flac", "audio/x-flac": "flac", "audio/ogg": "ogg", "audio/aac": "aac", "audio/x-aac": "aac",
    "audio/webm": "matroska",
}
REFERENCE_MEDIA = {"reference_image": IMAGE_EXT, "reference_video": VIDEO_EXT, "reference_audio": AUDIO_FORMAT}
MAX_FRAME_BYTES = 12_000_000
MAX_REFERENCE_VIDEO_BYTES = 50_000_000
MAX_REFERENCE_AUDIO_BYTES = 24_000_000
MAX_REFERENCE_AUDIO_SECONDS = 15.05  # allow a few samples of container padding
REFERENCE_SIZE_ERROR = "H3 输入须为非空文件，图片至多 12 MB，参考视频至多 50 MB，参考音频至多 24 MB。"
INPUT_FOLDER = "openbox-h3-inputs"
_DIALOGUE = re.compile(r"<d>\s*\[[A-Za-z][A-Za-z -]*\]\s*((?:[^<>]|<scenetrans>|<cutoff>)+?)\s*</d>")
_SPEECH_BOUNDARY = re.compile(r"<(?:scenetrans|cutoff)>")
_ID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\Z")
_VIDEO = re.compile(r"clip_[A-Za-z0-9_-]+\.mp4\Z")
WORKFLOW = Path(__file__).resolve().parents[1] / ".openbox/skills/comfyui-h3/assets/h3-workflow.json"
TURBO_WORKFLOW = WORKFLOW.with_name("h3-turbo.json")


def direct_endpoint(endpoint: str | None) -> bool:
    return bool(endpoint) and not urlsplit(endpoint).path.rstrip("/")


def extended_inputs(route):
    return bool(getattr(route, "comfy_workers", ()) or getattr(route, "workbench", False)
                or direct_endpoint(getattr(route, "base_url", None)))


def configured_endpoint(provider) -> tuple[str, str]:
    endpoint = (provider.base_url or "").rstrip("/") if provider else ""
    token = (provider.api_key or "") if provider else ""
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise VideoRequestError("ComfyUI 的 HTTPS 地址或端口无效。") from exc
    if (parsed.scheme != "https" or not parsed.hostname or port == 0 or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or re.search(r"[\s\\]", endpoint)
            or any(segment in (".", "..") for segment in parsed.path.split("/"))
            or (parsed.path and not re.fullmatch(r"(?:/[\w.-]+)*/proxy/[0-9]+", parsed.path))
            or not token or re.search(r"\s", token)):
        raise VideoRequestError("ComfyUI 需要 HTTPS 服务根地址或 Jupyter 代理地址，以及有效的 Token。")
    return endpoint, token


def client(route, timeout) -> httpx.AsyncClient:
    scheme = "Bearer" if direct_endpoint(route.base_url) else "token"
    ca_file = getattr(route, "tls_ca_file", None)
    return httpx.AsyncClient(base_url=route.base_url, timeout=timeout, trust_env=False,
                             verify=ssl.create_default_context(cafile=ca_file) if ca_file else True,
                             headers={"Authorization": f"{scheme} {route.api_key}"}, follow_redirects=False)


def dialogue(prompt: str) -> list[str]:
    lines = [_SPEECH_BOUNDARY.sub("", m.group(1)).strip() for m in _DIALOGUE.finditer(prompt)]
    if prompt.count("<d>") != len(lines) or prompt.count("</d>") != len(lines) or not all(lines):
        raise VideoRequestError("请将原生台词写为 <d>[Chinese] 台词</d>，每句话单独标记。")
    return lines


def validate(*, resolution, ratio, duration, input_mimes, roles, generate_audio, prompt, watermark, direct=False):
    if resolution not in MEGAPIXELS or ratio not in RATIOS or not 2 <= duration <= 15:
        raise VideoRequestError("ComfyUI H3 支持 2–15 秒、9:16 或 16:9，以及 480p/720p/768p 档。")
    if not generate_audio or watermark:
        raise VideoRequestError("ComfyUI H3 固定生成原生音轨，水印选项请关闭。")
    if not prompt or len(prompt) > 12_000:
        raise VideoRequestError("ComfyUI H3 需要 1–12000 字符的分镜提示词。")
    has_references = any(role in REFERENCE_MEDIA for role in roles)
    if has_references or direct:
        pictures, videos, audios = (roles.count(role) for role in REFERENCE_MEDIA)
        formats = REFERENCE_MEDIA
        if direct:
            formats = {**REFERENCE_MEDIA, "first_frame": IMAGE_EXT, "last_frame": IMAGE_EXT}
            if any(roles.count(role) > 1 for role in ("first_frame", "last_frame")):
                raise VideoRequestError("H3 每段至多一张首帧和一张尾帧。")
        if (len(input_mimes) != len(roles) or len(roles) > 8 or videos > 3 or audios > 3
                or any(mime not in formats.get(role, ()) for mime, role in zip(input_mimes, roles))):
            if not direct:
                raise VideoRequestError("H3 R2V 最多 8 个参考，其中视频与音频各至多 3 段，请使用参考图、参考视频或参考音频角色。")
            raise VideoRequestError("H3 输入素材角色或格式不匹配。最多 8 个素材，其中参考视频与音频各至多 3 段。")
        if audios and not (pictures or videos or (direct and ("first_frame" in roles or "last_frame" in roles))):
            raise VideoRequestError("H3 参考音频须同时提供视觉素材。")
        if not has_references:
            pictures = len(roles)
        for label, count in zip(("Picture", "Video", "Audio"), (pictures, videos, audios), strict=True):
            indices = {int(value) for value in re.findall(rf"<{label} (\d+)>", prompt)}
            if indices - set(range(1, count + 1)):
                raise VideoRequestError(f"H3 R2V 的 <{label} N> 标签须和实际素材顺序对应。")
    else:
        if len(input_mimes) not in (1, 2) or any(mime not in IMAGE_EXT for mime in input_mimes):
            raise VideoRequestError("ComfyUI H3 需要一张 PNG/JPEG/WebP 首帧，可另选一张尾帧。")
        expected = {"first_frame"} if len(input_mimes) == 1 else {"first_frame", "last_frame"}
        if len(roles) != len(input_mimes) or set(roles) != expected:
            raise VideoRequestError("请为 ComfyUI H3 明确指定首帧，以及可选的尾帧。")
        if re.search(r"<Audio \d+>", prompt):
            raise VideoRequestError("H3 的 <Audio N> 需要 R2V 的 reference_audio 素材。")
    dialogue(prompt)


def validate_poster_motion_region(region, profile, direct, frames):
    if region is None:
        return
    if not direct or profile != "poster":
        raise VideoRequestError("海报动态区域需要直连 ComfyUI 和 poster 预设。")
    if not frames.get("first_frame") or frames["first_frame"] != frames.get("last_frame"):
        raise VideoRequestError("指定动态区域时，请用同一张已排版的原画作为首尾帧。")
    if (not isinstance(region, (list, tuple)) or len(region) != 4
            or any(not isinstance(v, (float, int)) or not 0 <= v <= 1 for v in region)):
        raise VideoRequestError("动态区域需填写四个 0 到 1 的数值，依次为左边距、上边距、宽和高。")
    x, y, width, height = region
    if width <= 0 or height <= 0 or x + width > 1 + 1e-9 or y + height > 1 + 1e-9:
        raise VideoRequestError("海报动态区域须有面积且完整位于画面内。")


def build_payload(*, prompt, refs, resolution, ratio, duration, generate_audio, watermark, seed, h3_profile=None,
                  direct=False, poster_motion_region=None):
    validate(resolution=resolution, ratio=ratio, duration=duration,
             input_mimes=[r.get("mime", "") for r in refs], roles=tuple(r["role"] for r in refs),
             generate_audio=generate_audio, prompt=prompt, watermark=watermark, direct=direct)
    if h3_profile not in (None, "standard", "hybrid", "poster"):
        raise VideoRequestError("Unknown ComfyUI H3 profile.")
    validate_poster_motion_region(poster_motion_region, h3_profile, direct, {r["role"]: r["object_key"] for r in refs})
    return SUBMIT_PATH, {
        "prompt": prompt, "resolution": resolution, "ratio": ratio, "duration": duration,
        **({"h3_profile": h3_profile} if h3_profile else {}),
        **({"poster_motion_region": poster_motion_region} if poster_motion_region is not None else {}),
        "seed": seed, "refs": [{k: r[k] for k in ("mime", "role", "object_key")} for r in refs],
    }


def _normalize_reference_video(data: bytes, mime: str) -> tuple[bytes, float]:
    with tempfile.TemporaryDirectory(prefix="openbox-h3-reference-") as tmp:
        source = Path(tmp) / ("source" + VIDEO_EXT[mime])
        target = Path(tmp) / "reference.mp4"
        source.write_bytes(data)
        demuxer = "matroska" if mime == "video/webm" else "mov"
        input_format = ["-protocol_whitelist", "file", "-f", demuxer]
        try:
            result = subprocess.run([
                "ffprobe", "-v", "error", *input_format, "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(source),
            ], capture_output=True, check=True, timeout=15)
            duration = float(result.stdout.strip())
            if not 2 <= duration <= 15.05:
                raise VideoRequestError("H3 参考视频每段须为 2–15 秒，请先选取需要迁移的原片动作。")
            subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", *input_format, "-i", str(source), "-map", "0:v:0",
                "-map_metadata", "-1", "-an", "-t", "15", "-vf",
                "fps=24,scale=640:640:force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
                "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
                str(target),
            ], capture_output=True, check=True, timeout=45)
            frames = subprocess.run([
                "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=nb_frames",
                "-of", "default=nw=1:nk=1", str(target),
            ], capture_output=True, check=True, timeout=15)
            # Audio or edit-list padding in the original container can outlast the pictures.
            duration = int(frames.stdout.strip()) / 24
            if duration < 2:
                raise VideoRequestError("H3 参考视频需要至少 2 秒可用画面。")
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise VideoRequestError("H3 无法读取参考视频，请使用有效的 MP4、MOV 或 WebM 片段。") from exc
        return target.read_bytes(), duration


def reference_size_limit(role: str) -> int:
    if role == "reference_audio":
        return MAX_REFERENCE_AUDIO_BYTES
    if role == "reference_video":
        return MAX_REFERENCE_VIDEO_BYTES
    return MAX_FRAME_BYTES


def _normalize_reference_audio(data: bytes, mime: str) -> tuple[bytes, float]:
    with tempfile.TemporaryDirectory(prefix="openbox-h3-voice-") as tmp:
        source, target = Path(tmp) / "source", Path(tmp) / "reference.wav"
        source.write_bytes(data)
        try:
            subprocess.run([
                "ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file", "-f", AUDIO_FORMAT[mime],
                "-i", str(source), "-map", "0:a:0", "-vn", "-map_metadata", "-1",
                "-t", "15.2", "-ar", "32000", "-ac", "2", "-c:a", "pcm_s16le", str(target),
            ], capture_output=True, check=True, timeout=30)
            with wave.open(str(target), "rb") as audio:
                duration = audio.getnframes() / audio.getframerate()
        except (OSError, EOFError, wave.Error, subprocess.SubprocessError) as exc:
            raise VideoRequestError("H3 无法读取参考音频，请检查 WAV、MP3 或其他支持的音频文件。") from exc
        if not 2 <= duration <= MAX_REFERENCE_AUDIO_SECONDS:
            raise VideoRequestError("H3 参考音频每段须为 2–15 秒。")
        return target.read_bytes(), duration


async def _upload_reference(connection, ref):
    from core.oss import get_oss

    data = await get_oss().get_object(ref["object_key"], timeout=30)
    role, mime = ref["role"], ref["mime"]
    if not data or len(data) > reference_size_limit(role):
        raise VideoRequestError(REFERENCE_SIZE_ERROR)
    seconds = 0.0
    if role == "reference_video":
        data, seconds = await asyncio.to_thread(_normalize_reference_video, data, mime)
        suffix, mime = ".mp4", "video/mp4"
    elif role == "reference_audio":
        data, seconds = await asyncio.to_thread(_normalize_reference_audio, data, mime)
        suffix, mime = ".wav", "audio/wav"
    else:
        suffix = IMAGE_EXT[mime]
    name = hashlib.sha256(data).hexdigest() + suffix
    response = await connection.post("/upload/image", data={"subfolder": INPUT_FOLDER, "type": "input"},
                                     files={"image": (name, data, mime)})
    response.raise_for_status()
    uploaded = response.json()
    if uploaded.get("name") != name or uploaded.get("subfolder") != INPUT_FOLDER or uploaded.get("type") != "input":
        raise VideoRequestError("ComfyUI 未确认输入素材。")
    return role, f"{INPUT_FOLDER}/{name}", seconds


def _canvas(body):
    w, h = (int(v) for v in body["ratio"].split(":"))
    pixels = MEGAPIXELS[body["resolution"]] * 1_000_000
    length = body["duration"] * 24
    return {
        "width": math.ceil(math.sqrt(pixels * w / h) / 32) * 32,
        "height": math.ceil(math.sqrt(pixels * h / w) / 32) * 32,
        "length": length + (5 - length) % 17,
    }


def _seed(body, task_id):
    return body["seed"] if body["seed"] is not None else (int(uuid.UUID(task_id)) & 0x7fffffff)


def _workbench_graph(body):
    graph = json.loads(WORKFLOW.read_text())
    profile = body.get("h3_profile")
    if profile in (None, "standard", "hybrid"):
        return graph
    if profile != "poster":
        raise VideoRequestError("Unknown ComfyUI H3 profile.")
    # Same wiring as the verified 12-step Workbench take. The face refiner
    # keeps its original DMAD model; the two style LoRAs feed the sampler.
    model = graph["6"]["inputs"]["model"]
    for node, name, weight in (
        ("80", "H3电影感V2.0.safetensors", 0.5),
        ("81", "h3-realism-people-t2v-i2v-r2v.safetensors", 0.8),
    ):
        graph[node] = {"class_type": "LoraLoaderModelOnly",
                       "inputs": {"model": model, "lora_name": name, "strength_model": weight}}
        model = [node, 0]
    graph["6"]["inputs"]["model"] = model
    graph["15"]["inputs"]["steps"] = 12
    graph["23"]["inputs"]["realism"] = True
    graph["24"]["inputs"]["enabled"] = True
    return graph


def workflow(body, frames, task_id):
    graph = _workbench_graph(body)
    size = _canvas(body)
    if "first_frame" in frames:
        graph["11"]["inputs"]["image"] = frames["first_frame"]
    else:
        del graph["11"], graph["13"]["inputs"]["first_frame"]
    video = graph["13"]["inputs"]
    video.update(size)
    if "last_frame" in frames:
        graph["12"]["inputs"]["image"] = frames["last_frame"]
    else:
        graph.pop("12")
        video.pop("last_frame")
    graph["23"]["inputs"]["h3_prompt"] = _keyframe_prompt(body["prompt"], size["length"], "last_frame" in frames, "first_frame" in frames)
    graph["17"]["inputs"]["seed"] = _seed(body, task_id)
    graph["21"]["inputs"]["filename_prefix"] = f"openbox-h3/{task_id}/clip"
    return graph


def _keyframe_prompt(prompt, length, has_last_frame, has_first_frame=True):
    # Keep an authored structured body, and accept the short prompts already
    # used by existing callers by supplying the missing field boundaries.
    marker = "integrated_multimodal_description:"
    if marker in prompt:
        body = f"{marker} {prompt.split(marker, 1)[1].strip()}"
    else:
        body = marker + " [Shot 1] " + prompt.strip()
    for field in ("overall_soundscape", "non_diegetic_music"):
        if f"{field}:" not in body:
            body += f"\n\n{field}: N/A"
    if not has_first_frame and not has_last_frame:
        return body
    if has_last_frame:
        last_shot = max((int(n) for n in re.findall(r"\[Shot (\d+)\]", body)), default=1)
        if not has_first_frame:
            return (f"For the target video, at {length / 24:.2f} seconds into the target video, "
                    f"<Picture 1> (from [Shot {last_shot}]) is fully referenced.\n\n{body}")
        alignment = (
            "How the reference pictures align with the target video — Picture 1 (from Shot 1) aligns "
            f"with the 0.00-second mark of the target video; Picture 2 (from Shot {last_shot}) "
            f"aligns with the {length / 24:.2f}-second mark of the target video."
        )
    else:
        alignment = "For the target video, at 0.00 seconds into the target video, <Picture 1> (from [Shot 1]) is fully referenced."
    return f"{alignment}\n\n{body}"


def reference_workflow(body, references, task_id):
    """Use the Workbench acceleration chain with native H3 reference conditioning."""
    graph = _workbench_graph(body)
    del graph["11"], graph["12"]
    graph["23"]["inputs"]["h3_prompt"] = body["prompt"]
    graph["17"]["inputs"]["seed"] = _seed(body, task_id)
    graph["21"]["inputs"]["filename_prefix"] = f"openbox-h3/{task_id}/clip"
    graph["13"] = {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
        "clip": ["1", 0], "vae": ["2", 0], "audio_vae": ["3", 0],
        "prompt": ["23", 0], **_canvas(body), "ref_image_size": "match",
    }}
    counts = dict.fromkeys(REFERENCE_MEDIA, 0)
    for index, (role, path) in enumerate(references):
        node = str(30 + index * 2)
        slot = counts[role]
        counts[role] += 1
        if role == "reference_video":
            frames = str(int(node) + 1)
            graph[node] = {"class_type": "LoadVideo", "inputs": {"file": path}}
            graph[frames] = {"class_type": "GetVideoComponents", "inputs": {"video": [node, 0]}}
            graph["13"]["inputs"][f"ref_videos.ref_video_{slot}"] = [frames, 0]
        elif role == "reference_audio":
            graph[node] = {"class_type": "LoadAudio", "inputs": {"audio": path}}
            graph["13"]["inputs"][f"ref_audios.ref_audio_{slot}"] = [node, 0]
        else:
            graph[node] = {"class_type": "ShortDramaReadImage", "inputs": {"image": path}}
            graph["13"]["inputs"][f"ref_images.ref_image_{slot}"] = [node, 0]
    return graph


def _turbo_reference_conditioning(graph, size, prompt, uploaded):
    seconds = size["length"] / 24
    timeline = {"version": 10, "fps": 24, "selection": {"start": 0, "duration": seconds},
                "videoAudioEnabled": False, "videoClips": [], "images": [], "audios": []}
    width, height = size["width"], size["height"]
    ref_width, ref_height = (320, 576) if width < height else (576, 320)
    for role, path, duration in uploaded:
        if role == "reference_video":
            timeline["videoClips"].append({
                "file": path, "start": 0, "trimStart": 0, "duration": duration,
                "hasAudio": False, "referenceMode": "edit",
                "referenceWidth": ref_width, "referenceHeight": ref_height,
            })
        elif role == "reference_image":
            timeline["images"].append({"file": path})
        elif role == "reference_audio":
            timeline["audios"].append({"file": path, "duration": duration, "audioMode": "reference"})
    graph["30"] = {"class_type": "MiniMaxH3TimelinePlanner", "inputs": {
        "width": width, "height": height, "generation_seconds": seconds,
        "timeline_data": json.dumps(timeline, ensure_ascii=False),
    }}
    graph["13"] = {"class_type": "MiniMaxH3TimelineEncoder", "inputs": {
        "clip": ["1", 0], "vae": ["2", 0], "audio_vae": ["3", 0], "plan": ["30", 0],
        "prompt": prompt, "ref_image_size": "match",
    }}
    positive = ["13", 0]
    for loader, guide, index in (("11", "31", 0), ("12", "32", -1)):
        if loader in graph:
            graph[guide] = {"class_type": "MiniMaxH3AddGuide", "inputs": {
                "positive": positive, "latent": ["13", 1], "vae": ["2", 0],
                "image": [loader, 0], "frame_idx": index,
            }}
            positive = [guide, 0]
    return positive


def _poster_motion_latent(graph, size, region):
    """Encode the artwork across time and let SelfLift sample only the chosen area."""
    width, height = size["width"], size["height"]
    x, y, w, h = region
    left, top = min(width - 1, round(width * x)), min(height - 1, round(height * y))
    w = min(width - left, max(1, round(width * w)))
    h = min(height - top, max(1, round(height * h)))
    feather = min(max(8, round(width * 0.055)), w // 2, h // 2)
    graph.update({
        "90": {"class_type": "ImageScale", "inputs": {"image": ["11", 0], "width": width, "height": height,
                                                        "upscale_method": "lanczos", "crop": "disabled"}},
        "91": {"class_type": "RepeatImageBatch", "inputs": {"image": ["90", 0], "amount": size["length"]}},
        "92": {"class_type": "VAEEncode", "inputs": {"pixels": ["91", 0], "vae": ["2", 0]}},
        "93": {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["13", 1]}},
        "94": {"class_type": "LTXVConcatAVLatent", "inputs": {"video_latent": ["92", 0], "audio_latent": ["93", 1]}},
        "95": {"class_type": "SolidMask", "inputs": {"value": 0.0, "width": width, "height": height}},
        "96": {"class_type": "SolidMask", "inputs": {"value": 1.0, "width": w, "height": h}},
        "97": {"class_type": "FeatherMask", "inputs": {"mask": ["96", 0], "left": feather, "top": feather,
                                                        "right": feather, "bottom": feather}},
        "98": {"class_type": "MaskComposite", "inputs": {"destination": ["95", 0], "source": ["97", 0],
                                                          "x": left, "y": top, "operation": "add"}},
        "99": {"class_type": "SetLatentNoiseMask", "inputs": {"samples": ["94", 0], "mask": ["98", 0]}},
    })
    graph["17"]["inputs"]["latent_image"] = ["99", 0]


def turbo_workflow(body, uploaded, task_id):
    graph = json.loads(TURBO_WORKFLOW.read_text())
    size = _canvas(body)
    frames = {role: path for role, path, _ in uploaded if role in ("first_frame", "last_frame")}
    for role, loader in (("first_frame", "11"), ("last_frame", "12")):
        if role in frames:
            graph[loader]["inputs"]["image"] = frames[role]
        else:
            del graph[loader], graph["13"]["inputs"][role]
    references = any(role in REFERENCE_MEDIA for role, _, _ in uploaded)
    if references:
        graph["1"]["class_type"] = "MiniMaxH3ResidentEncoder"
        positive = _turbo_reference_conditioning(graph, size, body["prompt"], uploaded)
    else:
        graph["13"]["inputs"].update(size, prompt=_keyframe_prompt(
            body["prompt"], size["length"], "last_frame" in frames, "first_frame" in frames))
        positive = ["13", 0]

    seed = _seed(body, task_id)
    if body.get("h3_profile") in ("hybrid", "poster"):
        # Use the Workbench model and sampler for both input modes. The direct
        # server supplies its own conditioning, native decoder and resident encoder.
        workbench = _workbench_graph(body)
        model_and_output = {"4", "5", "6", "7", "8", "9", "10", "14", "15", "16", "17",
                            "20", "21", "80", "81"}
        graph.update({node: value for node, value in workbench.items() if node in model_and_output})
        graph["1"]["class_type"] = "MiniMaxH3ResidentEncoder"
        graph["20"]["inputs"]["images"] = ["18", 0]
        for node in ("22", "23", "24", "25"):
            del graph[node]
        graph["17"]["inputs"].update(seed=seed, positive=positive)
        if body.get("poster_motion_region") is not None:
            _poster_motion_latent(graph, size, body["poster_motion_region"])
    else:
        if references:
            graph["4"]["inputs"]["unet_name"] = "minimax_h3_ref2va_pruned_int8_convrot.safetensors"
            graph["5"]["inputs"]["lora_name"] = "minimax_h3_ref2v_turbo_4step_v0.1_comfyui_bf16.safetensors"
            graph["15"]["inputs"]["steps"] = 4
        graph["22"]["inputs"]["noise_seed"] = seed
    graph["16"]["inputs"]["conditioning"] = positive
    graph["21"]["inputs"]["filename_prefix"] = f"openbox-h3/{task_id}/clip"
    return graph


def pool_workflow(body, uploaded, task_id):
    frames = {role: path for role, path, _ in uploaded if role in ("first_frame", "last_frame")}
    refs = [(role, path) for role, path, _ in uploaded if role in REFERENCE_MEDIA]
    if refs:
        graph = reference_workflow(body, refs, task_id)
        positive = ["13", 0]
        for role, loader, guide, index in (("first_frame", "11", "70", 0), ("last_frame", "12", "71", -1)):
            if role in frames:
                graph[loader] = {"class_type": "ShortDramaReadImage", "inputs": {"image": frames[role]}}
                graph[guide] = {"class_type": "MiniMaxH3AddGuide", "inputs": {
                    "positive": positive, "latent": ["13", 1], "vae": ["2", 0],
                    "image": [loader, 0], "frame_idx": index}}
                positive = [guide, 0]
        graph["16"]["inputs"]["conditioning"] = graph["17"]["inputs"]["positive"] = positive
    else:
        graph = workflow(body, frames, task_id)
    if body.get("poster_motion_region") is not None:
        _poster_motion_latent(graph, _canvas(body), body["poster_motion_region"])
    return graph


async def submit(route, body, *, before_post=None):
    task_id = body.get("comfy_prompt_id") or str(uuid.uuid4())
    async with client(route, route.submit_timeout_seconds) as connection:
        uploaded = await asyncio.gather(*(_upload_reference(connection, ref) for ref in body["refs"]))
        if sum(seconds for role, _, seconds in uploaded if role == "reference_audio") > MAX_REFERENCE_AUDIO_SECONDS:
            raise VideoRequestError("H3 同一请求内的参考音频总长须在 15 秒以内。")
        if getattr(route, "workbench", False):
            graph = pool_workflow(body, uploaded, task_id)
        elif direct_endpoint(route.base_url):
            graph = turbo_workflow(body, uploaded, task_id)
        else:
            refs = [(role, path) for role, path, _ in uploaded]
            graph = (reference_workflow(body, refs, task_id) if any(role in REFERENCE_MEDIA for role, _ in refs)
                     else workflow(body, dict(refs), task_id))
        if before_post is not None:
            await before_post(task_id)
        try:
            queued = await connection.post(SUBMIT_PATH, json={"prompt": graph, "prompt_id": task_id})
            queued.raise_for_status()
            confirmed = queued.json().get("prompt_id")
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
            if before_post is None or (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500):
                raise
            # An interrupted reply is recoverable under the pre-recorded UUID on this worker.
            return {"id": task_id, "status": "in_progress"}
        if confirmed != task_id and before_post is not None:
            return {"id": task_id, "status": "in_progress"}
        if confirmed != task_id:
            raise RuntimeError("ComfyUI 没有确认任务编号，请检查队列后再决定是否重试。")
    return {"id": task_id, "status": "queued"}


async def status(route, task_id: str):
    if not _ID.fullmatch(task_id):
        raise VideoRequestError("ComfyUI 任务编号无效。")
    async with client(route, route.status_timeout_seconds) as connection:
        response = await connection.get(f"/history/{task_id}")
        response.raise_for_status()
        history = response.json().get(task_id)
        if history is None and getattr(route, "workbench", False):
            queued = await connection.get("/queue")
            queued.raise_for_status()
            if not any(row[1] == task_id for key in ("queue_running", "queue_pending") for row in queued.json()[key]):
                # It may have moved to history between the first lookup and the queue snapshot.
                final = await connection.get(f"/history/{task_id}")
                final.raise_for_status()
                history = final.json().get(task_id)
                if history is None:
                    return {"id": task_id, "status": "in_progress", "comfy_unseen": True}
    if history is None:
        return {"id": task_id, "status": "in_progress"}
    status_info = history.get("status", {})
    if status_info.get("status_str") != "success":
        return {"id": task_id, "status": "failed", "error": "ComfyUI 未完成此视频，请查看本地任务日志。"}
    folder = f"openbox-h3/{task_id}"
    videos = [item for group in history.get("outputs", {}).values()
              for items in group.values() if isinstance(items, list)
              for item in items if isinstance(item, dict) and item.get("type") == "output"
              and item.get("subfolder") == folder and _VIDEO.fullmatch(str(item.get("filename", "")))]
    if len(videos) != 1:
        return {"id": task_id, "status": "failed", "error": "ComfyUI 未返回此任务的唯一 MP4。"}
    query = urlencode({k: videos[0][k] for k in ("filename", "subfolder", "type")})
    return {"id": task_id, "status": "completed", "video_url": f"{route.base_url}/view?{query}"}


def owns_output_url(route, url: str) -> bool:
    value = urlsplit(url)
    base = urlsplit(route.base_url)
    if (value.scheme, value.netloc, value.path) != (base.scheme, base.netloc, base.path + "/view") or value.fragment:
        return False
    query = parse_qs(value.query)
    directory = query.get("subfolder", [""])
    name = query.get("filename", [""])
    return (set(query) == {"filename", "subfolder", "type"} and query["type"] == ["output"]
            and len(directory) == len(name) == 1 and directory[0].startswith("openbox-h3/")
            and bool(_ID.fullmatch(directory[0].removeprefix("openbox-h3/"))) and bool(_VIDEO.fullmatch(name[0])))
