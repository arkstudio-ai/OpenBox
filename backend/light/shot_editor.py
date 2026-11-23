"""Shot previews, retakes and explicit reassembly for guided video productions."""
from __future__ import annotations

import asyncio
import io
import math
import time
from collections import defaultdict
from datetime import timedelta
from typing import Annotated, Literal
from urllib.parse import unquote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select

from assets.service import owned_ready_query
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.video_job import VideoJob
from light.mode import LightError

KEY_PREFIX = "light-shot-edit:"
ACTIVE = {"dispatching", "submitting", "queued", "in_progress", "finalizing", "transfer_failed"}
FRAME_ROLES = ("first_frame", "last_frame")
Id = Annotated[str, StringConstraints(min_length=1, max_length=64)]


class ShotAction(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["retake", "compose", "titles"]
    revision: int = Field(ge=1)
    request_id: str = Field(min_length=16, max_length=48, pattern=r"^[a-zA-Z0-9_-]+$")
    version_id: Id | None = None
    prompt: str | None = Field(default=None, min_length=1, max_length=20_000)
    first_frame: Id | None = None
    last_frame: Id | None = None
    hide_title: bool | None = None
    versions: list[Id] = Field(default_factory=list, max_length=30)


def _fail(message, code="LIGHT_SHOT_STATE"):
    return LightError(code, message, 409)


def is_edit_job(job):
    return (getattr(job, "idempotency_key", None) or "").startswith(KEY_PREFIX)


def _references(data):
    return dict(zip(data.get("roles") or (), data.get("input_asset_ids") or ()))


def _request_refs(args):
    return {"roles": [ref.role for ref in args.input_assets],
            "input_asset_ids": [ref.asset_id for ref in args.input_assets]}


async def publish_request(ctx, prefix, args, **metadata):
    await publish_inputs(ctx, prefix, args.shot, args.prompt, {**_request_refs(args), **metadata})


async def publish_inputs(ctx, prefix, index, prompt, refs, *, merge=False):
    """Publish prepared frames before a provider has returned a job ID."""
    from light.production import _lock

    async with get_db_session() as db:
        row = await _lock(db, ctx.session_id, ctx.user_id, ctx.workspace_id, run_fence=ctx.run_fence)
        form = row.light_production
        run = form.get("run") or {}
        if run.get("job_prefix") != prefix:
            raise _fail("The production changed.", "LIGHT_PRODUCTION_CHANGED")
        inputs = dict(run.get("shot_inputs") or {})
        if merge:
            combined = {**_references(inputs.get(str(index), {})), **_references(refs)}
            refs = {**inputs.get(str(index), {}), **refs, "roles": list(combined),
                    "input_asset_ids": list(combined.values())}
        inputs[str(index)] = {"prompt": prompt, **refs}
        row.light_production = {**form, "run": {**run, "shot_inputs": inputs}}


def _asset_id(source):
    if source.startswith("asset_"):
        return source
    return next((part for part in unquote(urlsplit(source).path).split("/") if part.startswith("asset_")), "")


def _matching_frame(data, size, *, reuse_matching=False, preserve_edges=False):
    from PIL import Image, ImageOps

    with Image.open(io.BytesIO(data)) as image:
        if (reuse_matching and image.format == "JPEG" and image.mode == "RGB" and image.size == size
                and image.getexif().get(274, 1) == 1):
            image.load()
            return data
        image = ImageOps.exif_transpose(image)
        resize = ImageOps.pad if preserve_edges else ImageOps.fit
        frame = resize(image.convert("RGB"), size, method=Image.Resampling.LANCZOS)
        output = io.BytesIO()
        frame.save(output, format="JPEG", quality=94)
        return output.getvalue()


async def _normalize_frame(ctx, asset_id, size, *, preserve_edges=False):
    from core.oss import get_oss
    from light.remake import _reference_asset
    from PIL import Image

    async with get_db_session() as db:
        asset = await db.scalar(owned_ready_query(ctx.user_id, ctx.workspace_id).where(FileAsset.id == asset_id))
    if not asset:
        raise _fail("Select a readable image.", "LIGHT_PRODUCTION_ASSET")
    if not preserve_edges and asset.size > 20_000_000:
        raise _fail("Select a supported image under 20 MB.", "LIGHT_PRODUCTION_ASSET")
    try:
        data = await get_oss().get_object(asset.oss_key)
        frame = await asyncio.to_thread(
            _matching_frame, data, size, reuse_matching=asset.transient and asset_id.startswith("asset_remake_"),
            preserve_edges=preserve_edges,
        )
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise _fail("The selected frame is not a readable image.", "LIGHT_PRODUCTION_ASSET") from exc
    width, height = size
    if frame is data and asset.mime in {"image/jpeg", "image/jpg"}:
        return asset_id
    suffix = "-padded" if preserve_edges else ""
    return await _reference_asset(ctx, f"shot-input-{asset_id}-{width}x{height}{suffix}", frame)


def _timeline(job):
    return (job.request_data or {}).get("timeline") or {}


def _title_layers(form, base):
    from light.silk_road import TEMPLATE
    from light.silk_road_lettering import CHAPTER_TIMES

    if form.get("template") != TEMPLATE or not base:
        return {}
    return {i: layer for i, (start, end) in enumerate(CHAPTER_TIMES, 1)
            for layer in _timeline(base).get("overlays", ())
            if layer.get("from_sec") == start and layer.get("to_sec") == end}


def _title_changes(form, base, timeline, layers):
    desired = (form.get("shot_title_edits") or {}).get(base.id, {}) if base else {}
    existing = timeline.get("overlays", ())
    return {i: desired[str(i)] for i, layer in layers.items()
            if str(i) in desired and desired[str(i)] != (layer not in existing)}


def _apply_title_changes(timeline, layers, changes):
    if not changes:
        return timeline
    overlays = [layer for layer in timeline.get("overlays", ())
                if all(layer != layers[i] for i, hide in changes.items() if hide)]
    for index, hide in changes.items():
        layer = layers[index]
        if not hide and layer not in overlays:
            insert = next((i for i, existing in enumerate(overlays)
                           if existing["from_sec"] > layer["from_sec"]), len(overlays))
            overlays.insert(insert, layer)
    return {**timeline, "overlays": overlays}


def _finished(job):
    return job.status == "completed" and bool(job.output_asset_id)


def _stamp(job):
    from light.production import _utc
    return _utc(job.completed_at or job.created_at)


def _indices(job):
    return (job.request_data or {}).get("shot")


def _assembly(form, jobs, output_ids):
    """Resolve the editable master and, when applicable, its latest soundtrack."""
    run = form.get("run") or {}
    prefix = run.get("job_prefix") or ""
    key = run.get("wan_final_key")
    segments = {job.output_asset_id: job for job in jobs if job.kind == "segment" and _finished(job)}
    by_id = {job.output_asset_id: job for job in jobs if job.kind == "compose" and _finished(job)}
    eligible = [job for job in by_id.values() if not job.idempotency_key.startswith(KEY_PREFIX)
                and ((key and (job.idempotency_key == key or job.idempotency_key.startswith(key + ":retry:")))
                     or (not key and prefix and job.idempotency_key.startswith(prefix + ":final")))]
    if not eligible:
        eligible = [job for job in by_id.values() if job.output_asset_id in output_ids
                    and not job.idempotency_key.startswith(KEY_PREFIX)
                    and any(_asset_id(shot["asset"]) in segments for shot in _timeline(job).get("shots", []))]
    base = max(eligible, key=_stamp, default=None)
    if base is None:
        return None, None, {}
    current = by_id.get(output_ids[0]) if output_ids else base
    films = {base.output_asset_id: base}
    films.update({job.output_asset_id: job for job in by_id.values()
                  if job.idempotency_key.startswith(f"{KEY_PREFIX}{base.id}:compose:")})
    # A simple advanced-chat soundtrack edit wraps a full film in one shot.
    # Reuse its music against the underlying editable timeline.
    cursor = current
    while cursor is not None and cursor.output_asset_id not in films:
        data = _timeline(cursor)
        shots = data.get("shots") or []
        if (len(shots) != 1 or data.get("texts") or data.get("captions") or data.get("overlays")
                or data.get("filter") or shots[0].get("in_sec", 0) or shots[0].get("speed", 1) != 1
                or shots[0].get("volume", 1) != 0 or not data.get("audio")):
            return base, None, {}
        parent = by_id.get(_asset_id(shots[0]["asset"]))
        if not parent or _stamp(parent) >= _stamp(cursor):
            return base, None, {}
        duration = (parent.request_data or {}).get("duration_sec")
        if duration and abs((shots[0].get("duration_sec") or duration) - duration) > .12:
            return base, None, {}
        cursor = parent
    if cursor is None:
        return base, None, {}
    timeline = _timeline(cursor)
    if cursor is not current:
        timeline = {**timeline, "audio": _timeline(current)["audio"]}
    return base, cursor, timeline


def output_keys(form, jobs, base_output_ids=()):
    """Only registered assemblies of this edit lineage may become its result."""
    base, _, _ = _assembly(form, jobs, base_output_ids)
    return [job for job in jobs if base and job.kind == "compose" and _finished(job)
            and job.idempotency_key.startswith(f"{KEY_PREFIX}{base.id}:compose:")]


def _slots(form, jobs, timeline, base):
    from light import remake_case, silk_road
    from light.reference_quality import delivery_cuts

    run = form.get("run") or {}
    prefix = run.get("job_prefix") or ""
    plan = run.get("remake_adaptation") or form.get("remake_adaptation") or {}
    if not isinstance(plan, dict):
        plan = {}
    labels = {}
    world = plan.get("world") or {}
    if form.get("template") == silk_road.TEMPLATE:
        labels = {i: shot["label"] for i, shot in enumerate(silk_road.case()["shots"], 1)}
        if world.get("takes"):
            chapters = [chapter["title"] for chapter in world.get("chapters", ())]
            labels = {i: take.get("title") or (chapters[i - 1] if i <= len(chapters) else "")
                      for i, take in enumerate(world["takes"], 1)}
    elif form.get("template") == remake_case.TEMPLATE:
        labels = {shot["shot"]: shot.get("label", "") for shot in remake_case.manifest()["shots"]}
    elif isinstance(plan.get("shots"), list):
        labels = {i: shot.get("scene", "") for i, shot in enumerate(plan["shots"], 1)}
    elif form.get("template") == "wan_story":
        labels = {1: "", 2: "", 3: ""}
    prepared = run.get("shot_inputs") or {}
    active_source = {cut["shot"]: cut["source"] for cut in delivery_cuts(run) or ()}
    all_timeline_assets = {_asset_id(s["asset"]) for s in timeline.get("shots", ())}
    editor_prefix = f"{KEY_PREFIX}{base.id}:retake:" if base else None
    originals = defaultdict(list)
    edits = defaultdict(list)
    for job in sorted(jobs, key=lambda item: (item.created_at, item.id)):
        if job.kind != "segment":
            continue
        if editor_prefix and job.idempotency_key.startswith(editor_prefix):
            index = int(job.idempotency_key.removeprefix(editor_prefix).split(":", 1)[0])
            edits[index].append(job)
            continue
        index = _indices(job)
        if isinstance(index, int) and index > 0 and (
            (prefix and job.idempotency_key.startswith(prefix + ":"))
            or job.output_asset_id in all_timeline_assets
        ):
            originals[index].append(job)
    return labels, prepared, active_source, originals, edits


def _used(timeline, by_asset):
    result = {}
    for shot in timeline.get("shots", ()):
        job = by_asset.get(_asset_id(shot["asset"]))
        if job and _indices(job):
            result[_indices(job)] = job
    return result


def _film_sources(jobs, output_ids, known_indices, advanced_turns):
    """Trace delivered shots, including replacements on a finished film's timeline."""
    from video.timeline import Timeline

    segments = {job.output_asset_id: job for job in jobs if job.kind == "segment" and _finished(job)}
    films = sorted((job for job in jobs if job.kind == "compose" and _finished(job)
                    and job.output_asset_id in output_ids), key=_stamp)
    edits_by_turn = defaultdict(list)
    for segment in segments.values():
        if turn := advanced_turns.get(segment.attached_message_id):
            edits_by_turn[turn].append(segment)
    sources = {}
    for film in films:
        timeline = _timeline(film)
        own_sources = {i: job for i, job in _used(timeline, segments).items() if i in known_indices}
        cuts = timeline.get("shots") or []
        measured = timeline
        if len(cuts) == 1:
            clip = cuts[0]
            parent = sources.get(_asset_id(clip["asset"]))
            if (parent and not clip.get("in_sec") and clip.get("speed", 1) == 1
                    and (clip.get("duration_sec") is None or
                         (parent[2] is not None and abs(clip["duration_sec"] - parent[2]) <= .12))):
                own_sources, measured, _ = parent
                own_sources = dict(own_sources)

        try:
            parsed = Timeline.model_validate(measured)
        except ValueError:
            continue
        turn = advanced_turns.get(film.attached_message_id)
        spans = list(zip(parsed.shots, parsed.shot_spans()))
        for overlay in timeline.get("overlays", ()) if turn else ():
            if (overlay.get("kind", "video") != "video" or overlay.get("opacity", 1) != 1
                    or (overlay.get("x"), overlay.get("y"), overlay.get("width"), overlay.get("height")) != (0, 0, 1, 1)):
                continue
            # Legacy Agent edits can use a derived overlay with burned-in lettering.
            # The full-canvas overlay must cover one measured cut and have a unique
            # replacement from this same turn. Chat reports cut ordinals, not slot IDs.
            matches = [(ordinal, segments.get(_asset_id(shot.asset)))
                       for ordinal, (shot, span) in enumerate(spans, 1)
                       if span and abs(span[0] - overlay["from_sec"]) <= .12
                       and abs(span[1] - overlay["to_sec"]) <= .12]
            if len(matches) != 1 or matches[0][1] is None:
                continue
            ordinal, previous = matches[0]
            index = _indices(previous)
            if index not in known_indices:
                continue
            replacement = segments.get(_asset_id(overlay["asset"]))
            if replacement is None:
                candidates = [job for job in edits_by_turn[turn] if _indices(job) in {index, ordinal}
                              and _stamp(previous) < _stamp(job) <= _stamp(film)]
                replacement = candidates[0] if len(candidates) == 1 else None
            if replacement is not None:
                own_sources[index] = replacement
        duration = (film.request_data or {}).get("duration_sec")
        sources[film.output_asset_id] = (own_sources, measured, duration or parsed.duration_sec)
    return {asset_id: used for asset_id, (used, _, _) in sources.items()}


def _duration(job, asset):
    return float(((asset.meta or {}).get("duration_sec") if asset else None)
                 or (job.result_data or {}).get("duration_sec") or 0)


def _frame(asset, oss, *, tail=False, duration=0.0):
    if not asset or not oss:
        return None
    # Polling should keep the same URL long enough for the App's image cache.
    expires_sec = 3600 - int(time.time()) % 300
    if asset.mime.startswith("image/"):
        return {"asset_id": asset.id, "url": oss.presign_get(asset.oss_key, expires_sec=expires_sec)}
    if tail and duration <= 0:
        return None
    from tool.footage_analyze import snapshot_url
    fps = float((asset.meta or {}).get("fps") or 25)
    sec = max(0, duration - 1 / fps) if tail else 0
    return {"url": snapshot_url(oss, asset.oss_key, sec, exact=True, expires_sec=expires_sec)}


def _frame_pair(data, assets, oss, *, video=None, duration=0.0):
    refs = _references(data)
    reference = assets.get(refs.get("reference_video"))
    reference_duration = float((reference.meta or {}).get("duration_sec") or data.get("reference_duration") or 0) if reference else 0
    frames = {}
    for role in FRAME_ROLES:
        tail = role == "last_frame"
        if item := _frame(assets.get(refs.get(role)), oss):
            frames[role] = {**item, "source": "input"}
        elif item := _frame(video, oss, tail=tail, duration=duration):
            frames[role] = {**item, "source": "video"}
        elif item := _frame(reference, oss, tail=tail, duration=reference_duration):
            frames[role] = {**item, "source": "reference"}
    return frames


def _available_version(job, assets, oss):
    from light.production import _asset_view

    video = assets.get(job.output_asset_id)
    if not _finished(job) or video is None:
        return None
    duration = _duration(job, video)
    return {"id": job.id, "prompt": job.prompt or "", "created_at": _stamp(job).isoformat(),
            "duration_sec": duration, "media": _asset_view(video, oss),
            **_frame_pair(job.request_data or {}, assets, oss, video=video, duration=duration)}


async def project(db, row, jobs, output_ids, oss, *, advanced_turns=None):
    """A private editor projection for the opened task, with fresh signed previews."""
    if (row.light_entry != "video" or not row.light_production
            or not (output_ids or (row.light_production.get("run") or {}).get("action") == "render")):
        return None
    from light.production import _asset_view, _now, _utc
    from light.shot_frame_edits import read_drafts

    form = row.light_production
    base, master, timeline = _assembly(form, jobs, output_ids)
    labels, prepared, original_sources, originals, edits = _slots(form, jobs, timeline, base)
    indices = sorted(set(labels) | {int(i) for i in prepared} | set(originals))
    if not indices:
        return None
    film_sources = _film_sources(jobs, output_ids, set(indices), advanced_turns or {})
    for used in film_sources.values():
        for index, job in used.items():
            if job not in originals[index] and job not in edits.get(index, ()):
                originals[index].append(job)
    for group in originals.values():
        group.sort(key=_stamp)
    if not oss:
        from core.oss import OssNotConfigured, get_oss
        try:
            oss = get_oss()
        except OssNotConfigured:
            pass
    film_ids = set(output_ids)
    all_versions = [j for group in (*originals.values(), *edits.values()) for j in group]
    asset_ids = film_ids | {j.output_asset_id for j in all_versions if _finished(j)}
    for record in [*prepared.values(), *(j.request_data or {} for j in all_versions)]:
        asset_ids.update(record.get("input_asset_ids") or ())
    assets = {asset.id: asset for asset in (await db.scalars(owned_ready_query(row.user_id, row.workspace_id)
                                             .where(FileAsset.id.in_(asset_ids)))).all()} if asset_ids else {}
    by_asset = {job.output_asset_id: job for job in all_versions if _finished(job)}
    used = _used(timeline, by_asset)
    current_sources = film_sources.get(output_ids[0], used) if output_ids else used
    frame_drafts = await read_drafts(db, row, now=_now().isoformat())
    frame_history = form.get("shot_frame_inputs") or {}
    title_layers = _title_layers(form, base) if master else {}
    title_changes = _title_changes(form, base, timeline, title_layers)
    cards = []
    from tool.video_providers import declared_model
    from core.config import get_config
    config = get_config()
    for index in indices:
        group = originals.get(index, [])
        initial = prepared.get(str(index), {})
        versions = []
        for job in [*group, *edits.get(index, [])]:
            if version := _available_version(job, assets, oss):
                pinned = initial.get("frame_prompts", {}) if (
                    _references(job.request_data or {}) == _references(initial)
                ) else {}
                version["number"] = len(versions) + 1
                version["frame_prompts"] = frame_history.get(job.id, pinned)
                versions.append(version)
        accepted = next((v for v in versions if v["media"]["id"] == original_sources.get(index)), None)
        committed = current_sources.get(index) or used.get(index)
        latest_ids = {job.id for job in edits.get(index, ()) if not committed or _stamp(job) >= _stamp(committed)}
        latest = next((v for v in reversed(versions) if v["id"] in latest_ids), None)
        displayed = latest or next((v for v in versions if committed and v["id"] == committed.id), None) or accepted or (versions[-1] if versions else None)
        attempt = next(iter(reversed(edits.get(index) or group)), None)
        active = bool(attempt and attempt.status in ACTIVE)
        failed = bool(attempt and attempt.status in {"failed", "cancelled"})
        exact = (attempt.request_data or {}) if attempt else {}
        model = next((job.model for job in all_versions if displayed and job.id == displayed["id"]), None)
        card = {"index": index, "title": labels.get(index) or "", "status": (
            "generating" if active else "failed" if failed else "ready" if displayed else
            "generating" if exact or initial else "waiting"),
            "editable": bool(displayed and not active and master)
                        and index in used and bool(model == row.video_model),
            "frame_editable": bool(model and getattr(declared_model(model, config), "supports_first_last_frame", False)),
            "version": displayed, "versions": versions,
            "pending": bool(master and committed and latest and latest["id"] != committed.id),
        }
        if displayed:
            card["frame_drafts"] = {
                role: {"prompt": entry["prompt"], "status": state, "use_current": entry["use_current"],
                       "media": _asset_view(asset, oss) if asset else None}
                for role, (entry, asset, state) in frame_drafts.get(displayed["id"], {}).items()
            }
        if index in title_layers:
            card["title_hidden"] = title_changes.get(index, title_layers[index] not in timeline.get("overlays", ()))
            card["title_pending"] = index in title_changes
        if not displayed:
            card.update(prompt=(attempt.prompt if attempt else None) or initial.get("prompt") or "",
                        **_frame_pair({**initial, **exact}, assets, oss))
        cards.append(card)
    films = []
    film_jobs = {j.output_asset_id: j for j in jobs if j.kind == "compose" and _finished(j)}
    for asset_id in reversed(output_ids):
        if asset_id not in assets:
            continue
        job = film_jobs.get(asset_id)
        sources = film_sources.get(asset_id, {})
        if job and not sources:
            _, origin, inherited = _assembly(form, jobs, [asset_id])
            if origin:
                sources = _used(inherited, by_asset)
        film = {"media": _asset_view(assets[asset_id], oss), "number": len(films) + 1,
                "created_at": _stamp(job).isoformat() if job else _utc(assets[asset_id].created_at).isoformat(),
                "duration_sec": _duration(job, assets[asset_id]) if job else 0,
                "current": asset_id == output_ids[0],
                "segments": [{"index": idx, "version_id": source.id} for idx, source in sorted(sources.items())
                             if source.output_asset_id in assets]}
        films.append(film)
    pending_request = form.get("shot_edit_request") or {}
    editor_jobs = [job for job in jobs if base and job.idempotency_key.startswith(f"{KEY_PREFIX}{base.id}:")]
    pending_submit = bool(pending_request.get("until") and pending_request["until"] > _now().isoformat()
                          and not any(job.idempotency_key == pending_request.get("key") for job in editor_jobs))
    compose = next(iter(sorted((j for j in editor_jobs if j.kind == "compose"), key=_stamp, reverse=True)), None)
    active = pending_submit or any(job.status in ACTIVE for job in editor_jobs)
    plan = form.get("remake_adaptation") or {}
    return {"title": (plan.get("title") if isinstance(plan, dict) else None) or row.title,
            "shots": cards, "films": list(reversed(films)), "active": active,
            "can_compose": bool(master and not active and (title_changes or any(card["pending"] for card in cards))),
            "compose_state": compose.status if compose else None}


async def _snapshot(ctx, asset_id, role, meta):
    from core.oss import get_oss
    from light.remake import _reference_asset
    async with get_db_session() as db:
        asset = await db.scalar(owned_ready_query(ctx.user_id, ctx.workspace_id).where(FileAsset.id == asset_id))
    if asset is None:
        raise _fail("This video is no longer available.")
    from tool.footage_analyze import snapshot_url
    at = max(0, float(meta["duration_sec"]) - 1 / (meta.get("fps") or 25)) if role == "last_frame" else 0
    url = snapshot_url(get_oss(), asset.oss_key, at, width=min(meta.get("width") or 720, 1280), exact=True)
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            response = await client.get(url)
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise _fail("Could not read the original frame. Please select a replacement image.") from exc
    if not response.headers.get("content-type", "").startswith("image/jpeg") or len(response.content) > 4_000_000:
        raise _fail("Could not read a usable original frame.")
    return await _reference_asset(ctx, f"shot-frame-{asset_id}-{role}", response.content)


async def _retake_args(ctx, source, body, key):
    from light.video_tiers import SEEDANCE, TURBO
    from tool.video_production import VideoGenerateArgs, VideoInputRef
    from tool.video_providers import declared_model
    from core.config import get_config
    from video.media_probe import ensure_meta

    data = source.request_data or {}
    original = _references(data)
    changes = {role: getattr(body, role) for role in FRAME_ROLES
               if getattr(body, role) and getattr(body, role) != original.get(role)}
    refs = [VideoInputRef(asset_id=asset_id, role=role)
            for role, asset_id in zip(data.get("roles") or (), data.get("input_asset_ids") or (), strict=True)]
    meta = await ensure_meta(source.output_asset_id, user_id=ctx.user_id)
    if not meta or not meta.get("duration_sec"):
        raise _fail("Could not read the source video duration.")
    adaptive = False
    if changes:
        model = declared_model(source.model, get_config())
        if not getattr(model, "supports_first_last_frame", False):
            raise _fail("The selected video model does not accept end-frame inputs.")
        frames = {}
        for role in FRAME_ROLES:
            frames[role] = changes.get(role) or original.get(role) or await _snapshot(ctx, source.output_asset_id, role, meta)
        adaptive = getattr(model, "channel", None) == "dashscope" or source.model in {SEEDANCE, TURBO}
        if adaptive:
            width, height = meta.get("width") or 720, meta.get("height") or 720
            minimum = 300 if source.model == SEEDANCE else 240
            scale = max(minimum / min(width, height), min(1, 1920 / max(width, height)))
            width, height = max(minimum, round(width * scale)), max(minimum, round(height * scale))
            if source.model in {SEEDANCE, TURBO}:
                width, height = min(width, height * 5 // 2), min(height, width * 5 // 2)
            size = (width, height)
            sources = tuple(dict.fromkeys(frames.values()))
            normalized = await asyncio.gather(*(
                _normalize_frame(ctx, frame, size, **({"preserve_edges": True} if source.model == TURBO else {}))
                for frame in sources
            ))
            by_source = dict(zip(sources, normalized, strict=True))
            frames = {role: by_source[frame] for role, frame in frames.items()}
        refs = [VideoInputRef(asset_id=value, role=role) for role, value in frames.items()]
    duration = data.get("duration")
    if duration is None or duration == -1 and changes:
        duration = min(30, max(2, math.ceil(meta["duration_sec"] - .001)))
    return VideoGenerateArgs(
        action="submit", idempotency_key=key, prompt=body.prompt or source.prompt,
        model=source.model, resolution=data.get("resolution"),
        ratio="adaptive" if adaptive else data.get("ratio"),
        duration=duration, generate_audio=data.get("generate_audio"), watermark=data.get("watermark"),
        no_speech=not bool(data.get("spoken_line")), input_assets=refs,
        allow_duplicate=True, shot=_indices(source),
    )


async def _replace_sections(ctx, timeline, used, replacements, *, drop_removed_dialogue=False):
    from video.media_probe import ensure_meta
    from video.timeline import Timeline

    mapped = {used[_indices(new)].output_asset_id: new.output_asset_id for new in replacements}
    metas = await asyncio.gather(*(ensure_meta(asset, user_id=ctx.user_id) for asset in mapped.values()))
    if any(not (meta or {}).get("duration_sec") for meta in metas):
        raise _fail("One new fragment is still being read. Try composing again shortly.")
    durations = dict(zip(mapped.values(), (float(meta["duration_sec"]) for meta in metas), strict=True))
    source_limits = defaultdict(float)
    for shot in timeline["shots"]:
        source = _asset_id(shot["asset"])
        if source in mapped:
            if not shot.get("duration_sec"):
                raise _fail("This cut has no timed section for the selected fragment.")
            source_limits[source] = max(source_limits[source], (shot.get("in_sec") or 0) + shot["duration_sec"])
    revised = []
    for shot in timeline["shots"]:
        source = _asset_id(shot["asset"])
        if source in mapped:
            ratio = min(1, durations[mapped[source]] / source_limits[source])
            shot = {**shot, "asset": mapped[source], "in_sec": round((shot.get("in_sec") or 0) * ratio, 4),
                    "duration_sec": round(shot["duration_sec"] * ratio, 4), "speed": (shot.get("speed") or 1) * ratio}
            if shot["speed"] < .25:
                raise _fail("The new fragment is too short to keep this cut in sync.")
        revised.append(shot)
    result = Timeline.model_validate({**timeline, "shots": revised})
    if drop_removed_dialogue:
        from tool.video_production import normalize_spoken_text

        removed = {}
        for new in replacements:
            old = used[_indices(new)]
            line = (old.request_data or {}).get("spoken_line")
            if line and not (new.request_data or {}).get("spoken_line"):
                removed[old.output_asset_id] = normalize_spoken_text(line)
        spans = Timeline.model_validate(timeline).shot_spans()
        captions = [caption for caption in result.captions if not any(
            span and span[0] - 0.02 <= caption.from_sec < caption.to_sec <= span[1] + 0.02
            and removed.get(_asset_id(shot["asset"])) == normalize_spoken_text(caption.text)
            for shot, span in zip(timeline["shots"], spans, strict=True)
        )]
        result = result.model_copy(update={"captions": captions})
    return result.model_dump(mode="json")


def _align_silk_handoff(form, timeline, used):
    from light import silk_road

    reaction = used.get(9)
    shots = timeline["shots"]
    controls = (form.get("run") or {}).get("silk_controls") or {}
    if form.get("template") != silk_road.TEMPLATE or not controls or not reaction:
        return timeline
    index = silk_road.reaction_cut_index()
    if index + 1 < len(shots) and _asset_id(shots[index]["asset"]) == reaction.output_asset_id:
        return {**timeline, "shots": silk_road.reaction_after_reveal(shots)}
    return timeline


async def action(user_id, workspace_id, session_id, body: ShotAction):
    from light.production import _BUSY, _check_revision, _lock, _now, _views, get
    from tool.tool import ToolContext
    from tool import video_compose, video_production

    async with get_db_session() as db:
        row = await _lock(db, session_id, user_id, workspace_id)
        form = row.light_production
        jobs = list((await db.scalars(select(VideoJob).where(
            VideoJob.session_id == session_id, VideoJob.user_id == user_id))).all())
        if any(job.idempotency_key.startswith(KEY_PREFIX) and job.idempotency_key.endswith(":" + body.request_id) for job in jobs):
            return (await _views(db, [row], include_shots=True))[0]
        saving = form.get("shot_edit_request") or {}
        if saving.get("until", "") > _now().isoformat():
            if saving.get("key", "").endswith(":" + body.request_id):
                return (await _views(db, [row], include_shots=True))[0]
            raise _fail("Wait for the previous submission to be accepted.")
        _check_revision(form, body.revision)
        view = (await _views(db, [row], include_shots=True))[0]
        editor = view.get("shot_editor")
        if row.light_entry != "video" or not editor or not view.get("output") or view["status"] in _BUSY | {"review"} or editor["active"]:
            raise _fail("Finish the current video action before editing shots.")
        base, master, timeline = _assembly(form, jobs, [film["media"]["id"] for film in editor["films"]])
        if not base or not master:
            raise _fail("This version does not have an editable shot timeline.")
        by_id = {j.id: j for j in jobs if j.kind == "segment" and _finished(j)}
        used = _used(timeline, {j.output_asset_id: j for j in by_id.values()})
        ctx = ToolContext(session_id=row.id, user_id=user_id, workspace_id=workspace_id, project_id=row.project_id or "")
        source = None
        frame_prompts = {}
        replacements = []
        if body.action in {"retake", "titles"}:
            source = by_id.get(body.version_id)
            card = next((card for card in editor["shots"] if card["version"] and card["version"]["id"] == body.version_id), None)
            if not source or not card or not card["editable"]:
                raise _fail("This fragment changed. Open its latest version.")
            if body.action == "titles":
                if body.hide_title is None or "title_hidden" not in card:
                    raise _fail("This fragment has no editable chapter title.")
                title_edits = form.get("shot_title_edits") or {}
                row.light_production = {**form, "revision": form["revision"] + 1, "shot_title_edits": {
                    **title_edits, base.id: {**title_edits.get(base.id, {}), str(card["index"]): body.hide_title},
                }}
                row.updated_at = _now()
                await db.flush()
                return (await _views(db, [row], include_shots=True))[0]
            drafts = card.get("frame_drafts") or {}
            if any(draft["status"] == "generating" for draft in drafts.values()):
                raise _fail("Wait for the new frames to finish first.")
            if not (body.prompt or body.first_frame or body.last_frame):
                raise _fail("Edit the prompt or replace a frame first.")
            frames = {value for value in (body.first_frame, body.last_frame) if value}
            if frames:
                images = (await db.scalars(owned_ready_query(user_id, workspace_id).where(
                    FileAsset.id.in_(frames), FileAsset.mime.in_({"image/jpeg", "image/png", "image/webp"})))).all()
                if len(images) != len(frames):
                    raise _fail("Select an available JPEG, PNG or WebP image.", "LIGHT_PRODUCTION_ASSET")
            frame_prompts = dict(card["version"].get("frame_prompts") or {})
            for role in FRAME_ROLES:
                selected = getattr(body, role)
                if selected:
                    draft = drafts.get(role) or {}
                    if (draft.get("media") or {}).get("id") == selected:
                        frame_prompts[role] = draft["prompt"]
                    else:
                        frame_prompts.pop(role, None)
            key = f"{KEY_PREFIX}{base.id}:retake:{card['index']}:{body.request_id}"
        else:
            pending = {card["version"]["id"] for card in editor["shots"] if card["pending"]}
            if not editor["can_compose"] or pending != set(body.versions) or len(body.versions) != len(pending):
                raise _fail("The pending fragments changed. Review them again.", "LIGHT_PRODUCTION_CHANGED")
            replacements = [by_id[version] for version in body.versions]
            key = f"{KEY_PREFIX}{base.id}:compose:{body.request_id}"
        now = _now()
        row.light_production = {**form, "revision": form["revision"] + 1,
                                "shot_edit_request": {"key": key, "until": (now + timedelta(minutes=5)).isoformat()}}
        row.updated_at = now
    try:
        if source:
            request = await _retake_args(ctx, source, body, key)
            result = await video_production.execute_generate(request, ctx)
        else:
            from light.shortcut_plan import TEMPLATE as SHORTCUT_STORY

            revised = await _replace_sections(ctx, timeline, used, replacements,
                                              drop_removed_dialogue=form.get("template") == SHORTCUT_STORY)
            revised = _align_silk_handoff(
                form, revised, {**used, **{_indices(job): job for job in replacements}},
            )
            layers = _title_layers(form, base)
            revised = _apply_title_changes(revised, layers, _title_changes(form, base, timeline, layers))
            request = video_compose.VideoComposeArgs(action="submit", idempotency_key=key, timeline=revised,
                                                      filename=view["output"]["name"])
            from video.timeline import AudioClip

            current = next((job for job in jobs if job.kind == "compose"
                            and job.output_asset_id == editor["films"][0]["media"]["id"]), master)
            credit = (current.request_data or {}).get("soundtrack") or {}
            audio = _timeline(current).get("audio") or []
            if credit.get("track_id", "").startswith("netease:") and audio:
                result = await video_compose.execute_compose(
                    request, ctx, inherited_music=(AudioClip.model_validate(audio[-1]), credit),
                )
            else:
                result = await video_compose.execute_compose(request, ctx)
        meta = result.metadata or {}
        if not meta.get("job_id") or meta.get("status") in {"failed", "cancelled"} or meta.get("duplicate_in_flight"):
            raise _fail(result.output, "LIGHT_SHOT_SUBMIT")
        if source:
            if frame_prompts:
                async with get_db_session() as db:
                    row = await _lock(db, session_id, user_id, workspace_id)
                    saved = row.light_production.get("shot_frame_inputs") or {}
                    row.light_production = {**row.light_production, "shot_frame_inputs": {**saved, meta["job_id"]: frame_prompts}}
            watch_segment(meta["job_id"], ctx)
        else:
            from video.compose_recovery import watch
            watch(meta["job_id"])
    finally:
        async with get_db_session() as db:
            row = await _lock(db, session_id, user_id, workspace_id)
            form = row.light_production
            if (form.get("shot_edit_request") or {}).get("key") == key:
                row.light_production = {name: value for name, value in form.items() if name != "shot_edit_request"}
    return await get(user_id, workspace_id, session_id)


_watchers = set()


def watch_segment(job_id, ctx):
    """Use the video tool's regular bounded waits; recovery owns process restarts."""
    from core.log import create_logger
    from tool.video_production import VideoGenerateArgs, execute_generate

    async def follow():
        for iteration in range(13):
            result = await execute_generate(VideoGenerateArgs(action="wait", job_id=job_id, wait_iteration=iteration), ctx)
            if (result.metadata or {}).get("status") not in ACTIVE:
                break
            await asyncio.sleep(1)

    def finished(task):
        _watchers.discard(task)
        if not task.cancelled() and (exc := task.exception()):
            create_logger("light.shot_editor").warning("Shot wait stopped for %s: %s", job_id, type(exc).__name__)

    task = asyncio.create_task(follow())
    _watchers.add(task)
    task.add_done_callback(finished)
