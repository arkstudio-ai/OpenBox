"""Run the approved remake inside the existing Agent turn using native OpenBox tools."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher

from sqlalchemy import or_, select

from assets.service import owned_ready_query
from core.log import create_logger
from core.oss import get_oss
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.video_job import VideoJob
from light import remake_case as case
from light.remake_lettering import end_card
from light.video_language import for_text, speech_hint
from tool import image_gen, video_compose, video_production
from tool.tool import ToolContext
from tool.video_runninghub import INPUT_DOWNLOAD_FAILED, terminal_input_download_failure
from video.timeline import Cue

log = create_logger(__name__)
MAX_VIDEO_WAIT = 20 * 60
MAX_COMPOSE_WAIT = 5 * 60
_IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}
_FAILED = {"failed", "cancelled"}


class RemakeStopped(RuntimeError):
    pass


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:28]


async def _check(ctx: ToolContext) -> None:
    if ctx.abort.is_set():
        raise asyncio.CancelledError
    await ctx.assert_run_current()


async def _reference_asset(ctx: ToolContext, key: str, data: bytes, *, mime: str = "image/jpeg") -> str:
    from session.agent_event_log import prepare_agent_event_write

    asset_id = f"asset_remake_{_digest(f'{ctx.session_id}:{ctx.workspace_id}:{key}')}"
    async with get_db_session() as db:
        row = await db.get(FileAsset, asset_id)
        if row is not None and row.status == "ready" and not row.is_deleted:
            return row.id
    filename = key + {"image/png": ".png", "video/mp4": ".mp4", "audio/mpeg": ".mp3"}.get(mime, ".jpg")
    oss_key = f"assets/{ctx.user_id}/{asset_id}/{filename}"
    await _check(ctx)
    await get_oss().put_object(oss_key, data, content_type=mime, forbid_overwrite=True)
    async with get_db_session() as db:
        await prepare_agent_event_write(db, session_id=ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
        if await db.get(FileAsset, asset_id) is None:
            db.add(FileAsset(id=asset_id, user_id=ctx.user_id, workspace_id=ctx.workspace_id,
                             session_id=ctx.session_id, project_id=ctx.project_id or None,
                             name=filename, mime=mime, size=len(data), oss_key=oss_key,
                             status="ready", source="agent", transient=True, created_at=datetime.now(timezone.utc)))
    return asset_id


async def _material_refs(ctx: ToolContext, asset_ids: list[str]) -> list[str]:
    if not asset_ids:
        return []
    async with get_db_session() as db:
        assets = {row.id: row for row in (await db.scalars(
            owned_ready_query(ctx.user_id, ctx.workspace_id).where(FileAsset.id.in_(asset_ids)),
        )).all()}
    refs = [asset_id for asset_id in asset_ids if asset_id in assets and assets[asset_id].mime in _IMAGE_MIMES][:2]
    if refs:
        return refs
    from tool.footage_analyze import snapshot_url
    import httpx

    videos = [assets[asset_id] for asset_id in asset_ids if asset_id in assets and assets[asset_id].mime.startswith("video/")]
    if not videos:
        return []
    row = videos[0]
    duration = (row.meta or {}).get("duration_sec") or 1
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.get(snapshot_url(get_oss(), row.oss_key, min(1, duration / 2), width=720))
            response.raise_for_status()
            if response.headers.get("content-type", "").startswith("image/jpeg") and len(response.content) <= 4_000_000:
                return [await _reference_asset(ctx, f"video-reference-{_digest(row.id)}", response.content)]
    except httpx.HTTPError as exc:
        log.info("Selected video reference could not be sampled session=%s error=%s", ctx.session_id, type(exc).__name__)
    return []


async def _frame(ctx: ToolContext, prefix: str, shot: dict, adaptation: case.Adaptation,
                 refs: list[str], role: str = "first_frame") -> str:
    index = shot["shot"]
    suffix = f"{index:02d}" + ("-end" if role == "last_frame" else "")
    version = case.manifest()["version"]
    reference = await _reference_asset(ctx, f"{version}-{suffix}", (case.ASSETS / f"{suffix}.jpg").read_bytes())
    # At the start of the serving shot the trivet must be empty even when the user supplies a dish photo.
    showing_product = shot["product_visible"] and (role == "last_frame" or "last_frame" not in shot)
    if not showing_product or (not refs and "小龙虾" in adaptation.product and not adaptation.appearance):
        return reference
    filename = f"remake-{_digest(prefix)}-{suffix}.png"
    async with get_db_session() as db:
        prior = await db.scalar(owned_ready_query(ctx.user_id, ctx.workspace_id)
                                .where(FileAsset.session_id == ctx.session_id, FileAsset.name == filename)
                                .order_by(FileAsset.created_at.desc()).limit(1))
    if prior:
        return prior.id
    prompt = (
        f"竖屏写实短剧9:16。第一张是本镜的构图和动作位置，{shot[role]}。"
        f"按选中素材将可见菜品适配为「{adaptation.product}」。{adaptation.appearance}。"
        "第二张及以后的图片只参考食物样貌。维持第一张图的机位、原有单份菜的数量、容器位置、"
        "双手、人物视线、场景、服装、照明和留白。全图只出现原有的这一份；图上不加字。"
    )
    await _check(ctx)
    frame_ctx = replace(ctx, part_id=f"remake-frame-{_digest(f'{prefix}:{suffix}')}", sandbox=None, _on_output=None)
    result = await image_gen.execute(image_gen.ImageGenArgs(prompt=prompt, input_images=[reference, *refs],
                                     filename=filename, size="864x1536", quality="high", n=1), frame_ctx,
                                     channel="default")
    asset_id = result.metadata.get("asset_id")
    async with get_db_session() as db:
        ready = await db.scalar(owned_ready_query(ctx.user_id, ctx.workspace_id).where(FileAsset.id == asset_id)) if asset_id else None
    if ready is None:
        raise RemakeStopped(f"第 {index} 段画面尚未就绪。重试会查找原任务。")
    return ready.id


def shot_request(prefix: str, shot: dict, frame_id: str, product: str,
                 last_id: str | None = None) -> video_production.VideoGenerateArgs:
    frames = [video_production.VideoInputRef(asset_id=frame_id, role="first_frame")]
    if "last_frame" in shot:
        if not last_id:
            raise ValueError("The serving shot needs its matched landing frame")
        frames.append(video_production.VideoInputRef(asset_id=last_id, role="last_frame"))
    spoken = shot["spoken_line"]
    direction = (f"只有{shot['speaker']}说话，清楚同步说一次台词。{speech_hint(shot.get('language'), spoken)}开场即开口，"
                 f"在第{shot['duration'] - 0.3:.1f}秒前说完。其他人安静反应。" if spoken else
                 "全程只有自然环境声，无人说话。")
    prompt = ("竖屏中国电影短剧，一镜到底。延续演员、道具和场景。"
              f"{shot['motion']}{direction}动作自然，保留实景声，画面无字幕、无文字、无水印、无配乐。")
    return video_production.VideoGenerateArgs(
        action="submit", idempotency_key=f"{prefix}:shot{shot['shot']}", shot=shot["shot"],
        resolution=case.RESOLUTION, ratio="adaptive", duration=max(5, shot["duration"]),
        generate_audio=True, watermark=False, spoken_line=spoken or None, no_speech=not spoken,
        prompt=prompt, input_assets=frames,
    )


@dataclass
class Dialogue:
    cues: list[Cue]
    source_duration: float
    voice_asset: str | None = None


def build_timeline(shots: list[dict], asset_ids: list[str], dialogue: dict[int, Dialogue],
                   *, lettering_asset: str) -> dict:
    captions, tracks, audio = [], [], []
    for shot, asset_id in zip(shots, asset_ids, strict=True):
        duration = shot["duration"]
        at = shot["from_sec"]
        track = {"asset": asset_id, "duration_sec": duration, "fit": "cover", "volume": 1}
        tracks.append(track)
        if not shot["spoken_line"]:
            continue
        recording = dialogue[shot["shot"]]
        first, last = recording.cues[0].from_sec, recording.cues[-1].to_sec
        # H3 generates at least five seconds. Move a four-second cut past the lead-in
        # when necessary, so the end of the actual sentence survives the edit.
        source_span = min(recording.source_duration, max(duration, last - first + 0.12))
        offset = round(max(0, min(first, recording.source_duration - source_span, last + 0.12 - source_span)), 3)
        speed = source_span / duration
        if recording.voice_asset:
            if last - first > duration:
                raise RemakeStopped("上菜的画外台词超过镜头时长，请缩短这句文案。")
            offset = round(max(0, min(first, recording.source_duration - duration, last + 0.12 - duration)), 3)
            speed = 1
            track["volume"] = 0.2
            audio.append({"asset": recording.voice_asset, "from_sec": at, "to_sec": at + duration,
                          "in_sec": offset, "volume": 1, "fade_in_sec": 0.04, "fade_out_sec": 0.08})
        else:
            track.update(in_sec=offset, duration_sec=source_span, speed=speed)
        for cue in recording.cues:
            start = round(at + max(0, (cue.from_sec - offset) / speed), 3)
            end = round(min(at + duration, at + (cue.to_sec - offset) / speed), 3)
            if end > start:
                captions.append({"text": cue.text, "from_sec": start, "to_sec": end})
    last = shots[-1]
    lettering = {"asset": lettering_asset, "kind": "image", "from_sec": last["from_sec"], "to_sec": last["to_sec"],
                 "x": 0, "y": 0, "width": 1, "height": 1, "fit": "contain", "volume": 0}
    return {
        "version": 1, "canvas": {"width": 720, "height": 1280, "fps": 25}, "shots": tracks,
        "audio": audio, "captions": captions, "caption_style": {"bottom_ratio": 0.12, "max_width": 0.87, "motion": {},
            "style": {"font": "AlibabaPuHuiTi", "size": 42, "color": "#FFFFFF", "outline": 2,
                      "outline_color": "#221A17"}}, "overlays": [lettering],
    }


def _spoken_cues(line: str, sentences: list[dict]) -> list[Cue]:
    spans = sorted((item for item in sentences if isinstance(item, dict) and item.get("text")
                    and isinstance(item.get("from_sec"), (int, float)) and isinstance(item.get("to_sec"), (int, float))
                    and 0 <= item["from_sec"] < item["to_sec"]), key=lambda item: item["from_sec"])
    if not spans:
        return []
    from light.wan_edit import _words as words

    heard = words("".join(item["text"] for item in spans))
    if SequenceMatcher(None, words(line), heard).ratio() < 0.65:
        return []
    for punctuation in ("。！？!?", "。！？!?，,"):
        phrases = [part for part in re.split(f"(?<=[{punctuation}])", line) if part]
        if len(phrases) == len(spans) and all(a["to_sec"] <= b["from_sec"] for a, b in zip(spans, spans[1:])):
            return [Cue(text=phrase, from_sec=span["from_sec"], to_sec=span["to_sec"])
                    for phrase, span in zip(phrases, spans)]
    return [Cue(text=line, from_sec=spans[0]["from_sec"], to_sec=max(item["to_sec"] for item in spans))]


async def _transcribe(ctx: ToolContext, asset_id: str, line: str, expected_duration: int, *, language=None) -> Dialogue:
    language = for_text(line, language)
    suffix = f":{language}" if language in {"vi", "en"} else ""
    result = await video_production.execute_transcribe(video_production.VideoTranscribeArgs(
        action="submit", asset_id=asset_id, language=language,
        idempotency_key=f"light-remake-caption:{asset_id}{suffix}"), ctx)
    if result.metadata.get("status") != "completed":
        raise RemakeStopped("对白识别尚未完成，请重试以核对原作业。")
    async with get_db_session() as db:
        job = await db.get(VideoJob, result.metadata["job_id"])
        transcript = (job.result_data or {}).get("transcript") if job else None
        asset = await db.get(FileAsset, asset_id)
        duration = (asset.meta or {}).get("duration_sec") if asset else None
    cues = _spoken_cues(line, (transcript or {}).get("sentences") or [])
    # Delivery lengths can exceed the requested number of seconds (e.g. 6.592 for 6).
    return Dialogue(cues, float(duration or max([expected_duration, *(cue.to_sec for cue in cues)])))


async def _serving_voice(ctx: ToolContext, run: dict, line: str, *, language=None) -> str:
    # Only hands are visible here. If the native take contains ambience alone,
    # record the same fictional supervisor on camera and use only its sound.
    frame = await _reference_asset(ctx, f"{case.manifest()['version']}-02", (case.ASSETS / "02.jpg").read_bytes())
    request = video_production.VideoGenerateArgs(
        action="submit", idempotency_key=f"{run['job_prefix']}:serving-voice", resolution=case.RESOLUTION,
        ratio="adaptive", duration=6, generate_audio=True, spoken_line=line, watermark=False,
        input_assets=[video_production.VideoInputRef(asset_id=frame, role="first_frame")],
        prompt=(f"餐馆里戴细圆眼镜的墨绿衬衣中国女主管，近景，温和但干脆地说台词。{speech_hint(language, line)}"
               "刚开场就开口，4秒内自然说完。她是上一镜替新人解围的那一位，留柔和餐厅底噪，其他人不说话。无配乐和字幕。"),
    )
    return await _job(ctx, request, kind="segment", label="主管上菜对白补录", run=run)


async def compose_remake(ctx: ToolContext, form: dict, run: dict, adaptation: case.Adaptation,
                         shots: list[dict], assets: list[str]) -> str:
    sound_ctx = replace(ctx, message_id="", sandbox=None, _on_output=None)

    async def measure(pair):
        shot, asset = pair
        line = shot["spoken_line"]
        recording = await _transcribe(sound_ctx, asset, line, max(5, shot["duration"]), language=adaptation.language)
        if not recording.cues and "last_frame" in shot:
            voice = await _serving_voice(sound_ctx, run, line, language=adaptation.language)
            recording = replace(await _transcribe(sound_ctx, voice, line, 6, language=adaptation.language), voice_asset=voice)
        if not recording.cues:
            raise RemakeStopped(f"第 {shot['shot']} 段没有识别到预定台词，已保留分镜和原声。")
        return shot["shot"], recording

    dialogue = dict(await _parallel([(shot, asset) for shot, asset in zip(shots, assets, strict=True)
                                     if shot["spoken_line"]], measure, concurrency=3))
    artwork = end_card(adaptation.end_copy or adaptation.product, form["shop"])
    lettering_asset = await _reference_asset(ctx, f"remake-lettering-{hashlib.sha256(artwork).hexdigest()[:20]}",
                                            artwork, mime="image/png")
    timeline = build_timeline(shots, assets, dialogue, lettering_asset=lettering_asset)
    edit = _digest(json.dumps(timeline, sort_keys=True))
    return await _job(ctx, video_compose.VideoComposeArgs(
        action="submit", idempotency_key=f"{run['job_prefix']}:final:{edit}", timeline=timeline,
        filename=f"{case.title(adaptation.language)}-{adaptation.product}.mp4",
    ), kind="compose", label="30 秒成片", run=run)


async def _existing_job(ctx, kind: str, key: str) -> VideoJob | None:
    async with get_db_session() as db:
        return await db.scalar(select(VideoJob).where(
            VideoJob.user_id == ctx.user_id, VideoJob.session_id == ctx.session_id, VideoJob.kind == kind,
            or_(VideoJob.idempotency_key == key, VideoJob.idempotency_key.startswith(key + ":retry:")),
        ).order_by(VideoJob.created_at.desc(), VideoJob.id.desc()).limit(1))


async def _legacy_input_download_failed(job: VideoJob) -> bool:
    """Older rows flattened every RunningHub 1007 into the same message."""
    if (job.status != "failed" or not job.provider_task_id
            or "1007" not in (job.error or "")
            or (job.result_data or {}).get("failure_code")):
        return False
    from tool.video_providers import provider_route_mismatch

    try:
        route, _ = video_production._configured_target(job.model)
        if route.channel != "runninghub" or provider_route_mismatch(job.request_data, route):
            return False
        response = await asyncio.wait_for(video_production._provider_status(route, job.provider_task_id), timeout=8)
        return terminal_input_download_failure(response)
    except Exception:
        log.info("could not recover the original download failure for video job %s", job.id)
        return False


async def _job(ctx, request, *, kind: str, label: str, run: dict) -> str:
    if kind == "segment":
        execute, args_type, deadline = video_production.execute_generate, video_production.VideoGenerateArgs, MAX_VIDEO_WAIT
    else:
        execute, args_type, deadline = video_compose.execute_compose, video_compose.VideoComposeArgs, MAX_COMPOSE_WAIT
    root_key = request.idempotency_key
    prior = await _existing_job(ctx, kind, root_key)
    key = root_key
    refresh_key = f"{root_key}:retry:media:{_digest(run['id'])[:12]}" if kind == "segment" else None
    if prior and prior.status in _FAILED:
        result_data = prior.result_data or {}
        ambiguous = (result_data.get("submit_error") or {}).get("submission_outcome") == "ambiguous"
        already_known = any(item["id"] == prior.id for item in run["existing"])
        if ambiguous:
            raise RemakeStopped(f"{label}需要核实原任务。")
        download_failed = refresh_key and prior.status == "failed" and prior.provider_task_id and (
            result_data.get("failure_code") == INPUT_DOWNLOAD_FAILED
            or (already_known and await _legacy_input_download_failed(prior))
        )
        if download_failed and prior.idempotency_key == refresh_key:
            raise RemakeStopped(f"{label}刷新链接后素材仍下载失败，进度已保留。")
        if download_failed:
            key = refresh_key
        elif already_known:
            key = f"{root_key}:retry:{_digest(run['id'])[:12]}"
        else:
            raise RemakeStopped(f"{label}需要核实原任务。")
        prior = None

    async def submit(attempt_key):
        if attempt_key == refresh_key:
            # OSS signatures have second-level precision. Let the replacement
            # submit mint a distinct URL even after an immediate upstream failure.
            await asyncio.sleep(1)
        await _check(ctx)
        valid = await execute(request.model_copy(update={"action": "validate"}), ctx)
        if not valid.output.startswith("valid=true"):
            log.warning("remake validation refused session=%s key=%s title=%s", ctx.session_id, attempt_key, valid.title)
            if valid.metadata.get("frame_validation"):
                raise RemakeStopped(f"{label}未提交。{valid.output}")
            raise RemakeStopped(f"{label}未通过制作参数校验。")
        result = await execute(request.model_copy(update={"idempotency_key": attempt_key}), ctx)
        job_id = result.metadata.get("job_id")
        if not job_id:
            log.warning("remake submission refused session=%s key=%s title=%s", ctx.session_id, attempt_key, result.title)
            raise RemakeStopped(f"{label}提交失败。{result.output[:220]}")
        return job_id

    if prior:
        job_id = prior.id
        key = prior.idempotency_key
    else:
        job_id = await submit(key)
    limit = time.monotonic() + deadline
    while time.monotonic() < limit:
        await _check(ctx)
        result = await execute(args_type(action="wait", job_id=job_id, wait_seconds=15, wait_iteration=1), ctx)
        status, asset = result.metadata.get("status"), result.metadata.get("asset_id")
        if status == "completed" and asset:
            return asset
        if (refresh_key and status == "failed" and not result.metadata.get("ambiguous_submit")
                and result.metadata.get("failure_code") == INPUT_DOWNLOAD_FAILED):
            if key == refresh_key:
                raise RemakeStopped(f"{label}刷新链接后素材仍下载失败，进度已保留。")
            key = refresh_key
            job_id = await submit(key)
            continue
        if status in _FAILED or result.metadata.get("ambiguous_submit"):
            raise RemakeStopped(f"{label}尚未交付，进度已保留。")
        await asyncio.sleep(2)
    raise RemakeStopped(f"{label}仍在处理，重试将查询原作业。")


async def _parallel(items, operation, *, concurrency: int, stop_on_error: bool = False):
    permits = asyncio.Semaphore(concurrency)
    failed = asyncio.Event()

    async def one(item):
        async with permits:
            if stop_on_error and failed.is_set():
                raise RemakeStopped("前序镜头需修正，尚未开始的生成请求已暂缓。")
            try:
                return await operation(item)
            except BaseException:
                failed.set()
                raise

    results = await asyncio.gather(*(one(item) for item in items), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
    return results


async def render(ctx: ToolContext, form: dict, run: dict, adaptation: case.Adaptation) -> str:
    from light.production import remake_stage
    from light.shot_editor import publish_request

    prefix = run["job_prefix"]
    shots = case.storyboard(adaptation)
    await remake_stage(ctx, prefix, "remake_frames")
    refs = await _material_refs(ctx, form["asset_ids"])
    async def frames_for(shot):
        first = await _frame(ctx, prefix, shot, adaptation, refs)
        preview = {name: value for name, value in shot.items() if name != "last_frame"}
        await publish_request(ctx, prefix, shot_request(prefix, preview, first, adaptation.product))
        last = await _frame(ctx, prefix, shot, adaptation, refs, "last_frame") if "last_frame" in shot else None
        if last:
            await publish_request(ctx, prefix, shot_request(prefix, shot, first, adaptation.product, last))
        return first, last

    frames = await _parallel(shots, frames_for, concurrency=2)
    await remake_stage(ctx, prefix, "remake_shots")
    segment_ctx = replace(ctx, message_id="", sandbox=None, _on_output=None)

    async def animate(item):
        shot, (first, last) = item
        return await _job(segment_ctx, shot_request(prefix, shot, first, adaptation.product, last),
                          kind="segment", label=f"第 {shot['shot']} 段", run=run)
    assets = await _parallel(list(zip(shots, frames, strict=True)), animate, concurrency=4)
    await remake_stage(ctx, prefix, "compose")
    return await compose_remake(ctx, form, run, adaptation, shots, assets)
