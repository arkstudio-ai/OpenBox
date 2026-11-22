"""The edit sheet of a library clip: what the cut was actually made of.

A clip that went through ``video_compose`` has its whole timeline on the job
(``video_jobs.request_data``): every shot's times, speed, move and filter, the
transitions, the caption and title styles, the music bed and the voice-over.
The reference card used to guess all of that from a few frames. Here it is
read back as facts: ``sheet`` keeps the parameters and drops everything that is
the source shop's own (caption and voice-over wording, asset addresses), and
``lines`` says it in the words the card's reader and the console show.

A clip with no compose job (generated in one piece, or brought in from
outside) has no sheet; the card then says so instead of guessing.
"""
from __future__ import annotations

from typing import Any

from core.log import create_logger

log = create_logger(__name__)

MAX_SHOTS = 40
MAX_TEXTS = 24
MOVES = {"zoom_in": "缓推", "zoom_out": "缓拉", "pan_left": "左移", "pan_right": "右移", "pan_up": "上移",
         "pan_down": "下移"}


def _num(value: Any, digits: int = 2) -> float | None:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _style(style: dict | None) -> dict:
    style = style if isinstance(style, dict) else {}
    kept = {key: style.get(key) for key in ("font", "size", "color", "outline", "outline_color", "color_style")
            if style.get(key) not in (None, "")}
    if style.get("font_url"):
        kept["font"] = "自定义字体"
    return kept


def _span(item: dict) -> list[float | None]:
    return [_num(item.get("from_sec")), _num(item.get("to_sec"))]


def sheet(request_data: dict | None) -> dict | None:
    """The parameters of one compose job, without the shop's wording or files; None when it holds no timeline."""
    data = request_data if isinstance(request_data, dict) else {}
    timeline = data.get("timeline")
    if not isinstance(timeline, dict) or not isinstance(timeline.get("shots"), list) or not timeline["shots"]:
        return None
    canvas = timeline.get("canvas") if isinstance(timeline.get("canvas"), dict) else {}
    out: dict = {"canvas": {key: canvas.get(key) for key in ("width", "height", "fps") if canvas.get(key)}}

    shots, at, known = [], 0.0, True
    for shot in timeline["shots"][:MAX_SHOTS]:
        if not isinstance(shot, dict):
            continue
        speed = _num(shot.get("speed")) or 1.0
        length = _num(shot.get("duration_sec"))
        played = round(length / speed, 2) if length else None
        transition = shot.get("transition_out") if isinstance(shot.get("transition_out"), dict) else None
        move = shot.get("move") if isinstance(shot.get("move"), dict) else None
        row: dict = {"at": [round(at, 2), round(at + played, 2)] if known and played else None, "sec": played}
        if speed != 1.0:
            row["speed"] = speed
        if move:
            row["move"] = move.get("preset") or "custom"
        if shot.get("filter"):
            row["filter"] = shot["filter"]
        if shot.get("fit") and shot["fit"] != "cover":
            row["fit"] = shot["fit"]
        if shot.get("backdrop") == "blur":
            row["backdrop"] = "blur"
        if _num(shot.get("volume")) == 0:
            row["muted"] = True
        if transition:
            row["transition"] = {"type": transition.get("type") or "fade", "sec": _num(transition.get("seconds")) or 0.5}
        shots.append(row)
        if known and played:
            at += played - (row["transition"]["sec"] if transition else 0)
        else:
            known = False
    out["shots"] = shots
    out["shot_count"] = len(timeline["shots"])
    total = _num(data.get("duration_sec")) or (round(at, 2) if known else None)
    if total:
        out["seconds"] = total
    if timeline.get("filter"):
        out["filter"] = timeline["filter"]

    captions = [cue for cue in timeline.get("captions") or [] if isinstance(cue, dict)]
    if captions:
        look = timeline.get("caption_style") if isinstance(timeline.get("caption_style"), dict) else {}
        motion = look.get("motion") if isinstance(look.get("motion"), dict) else {}
        lengths = [len(str(cue.get("text") or "")) for cue in captions]
        out["captions"] = {"count": len(captions), **_style(look.get("style")),
                           "bottom_ratio": _num(look.get("bottom_ratio"), 3),
                           "in_effect": motion.get("in_effect"), "max_chars": max(lengths),
                           "from": _num(captions[0].get("from_sec")), "to": _num(captions[-1].get("to_sec"))}

    texts = []
    for text in [t for t in timeline.get("texts") or [] if isinstance(t, dict)][:MAX_TEXTS]:
        motion = text.get("motion") if isinstance(text.get("motion"), dict) else {}
        texts.append({"at": _span(text), "x": _num(text.get("x")), "y": _num(text.get("y")),
                      "align": text.get("align") or "center", "chars": len(str(text.get("text") or "")),
                      **_style(text.get("style")),
                      **({"in_effect": motion["in_effect"]} if motion.get("in_effect") else {})})
    if texts:
        out["texts"] = texts

    soundtrack = data.get("soundtrack") if isinstance(data.get("soundtrack"), dict) else {}
    beds, voices = [], []
    for audio in [a for a in timeline.get("audio") or [] if isinstance(a, dict)]:
        if isinstance(audio.get("voiceover"), dict):
            voices.append(audio)
            continue
        bed: dict = {"at": _span(audio), "volume": _num(audio.get("volume"))}
        if audio.get("library_id"):
            bed["track_id"] = audio["library_id"]
        for key in ("fade_in_sec", "fade_out_sec"):
            if _num(audio.get(key)):
                bed[key] = _num(audio[key])
        if audio.get("duck_under_captions") or audio.get("duck_under_voice"):
            bed["duck_volume"] = _num(audio.get("duck_volume"))
        beds.append(bed)
    if soundtrack.get("track_id") and beds:
        # ``music=`` puts its bed last (tool/video_compose.py); the credit names it.
        beds[-1].update(track_id=soundtrack["track_id"], title=str(soundtrack.get("attribution") or "")[:80])
        if soundtrack.get("beat_cuts"):
            beds[-1]["beat_cuts"] = len(soundtrack["beat_cuts"])
    if beds:
        out["music"] = beds
    if voices:
        rates = [int(v["voiceover"].get("speech_rate") or 0) for v in voices]
        out["voiceover"] = {"voice": voices[0]["voiceover"].get("voice") or "", "lines": len(voices),
                            "speech_rate": [min(rates), max(rates)],
                            "from": _num(voices[0].get("from_sec")), "to": _num(voices[-1].get("to_sec")),
                            "volume": _num(voices[0].get("volume"))}

    overlays = [{"kind": o.get("kind") or "video", "at": _span(o),
                 **{key: _num(o.get(key)) for key in ("x", "y", "width", "height", "opacity")}}
                for o in timeline.get("overlays") or [] if isinstance(o, dict)]
    if overlays:
        out["overlays"] = overlays
    return out


async def _describe_music(found: dict) -> None:
    """Mood, tempo and name of the beds that came from a music library."""
    for bed in found.get("music") or []:
        track_id = bed.get("track_id")
        if not track_id:
            continue
        try:
            from video import bgm_library, music_catalog
            track = next((item for item in music_catalog.catalogue() if item.id == track_id), None)
            if track is not None:
                bed.setdefault("title", f"{track.title} - {track.artist}"[:80])
                bed.update(moods=list(track.moods)[:4], **({"bpm": round(track.bpm)} if track.bpm else {}))
            elif str(track_id).startswith("icepublic-"):
                known = await bgm_library.track(track_id)
                if known is not None:
                    bed.setdefault("title", known.name[:80])
                    bed["moods"] = [known.label]
        except Exception as exc:  # the id alone still says which track it was
            log.info("light example edit: music not described track=%s error=%s", track_id, type(exc).__name__)


async def for_output(output: dict | None) -> dict | None:
    """The sheet of the compose job that made this piece; None when no such job is on record."""
    asset_id = (output or {}).get("asset_id")
    if not asset_id:
        return None
    from sqlalchemy import select
    from db.base import get_db_session
    from db.models.video_job import VideoJob
    async with get_db_session() as db:
        job = await db.scalar(select(VideoJob).where(VideoJob.output_asset_id == asset_id, VideoJob.kind == "compose")
                              .order_by(VideoJob.created_at.desc()).limit(1))
    found = sheet(job.request_data) if job is not None else None
    if found is not None:
        await _describe_music(found)
    return found


def cut_points(found: dict | None) -> list[float]:
    """The middle of every shot: where a frame shows what that shot holds."""
    return [round((shot["at"][0] + shot["at"][1]) / 2, 2) for shot in (found or {}).get("shots") or [] if shot.get("at")]


def _g(value: Any) -> str:
    return f"{value:g}" if isinstance(value, (int, float)) else str(value)


def _look(style: dict) -> str:
    bits = [f"字体 {style['font']}"] if style.get("font") else []
    if style.get("size"):
        bits.append(f"字号 {style['size']}")
    if style.get("color"):
        bits.append(f"颜色 {style['color']}")
    if style.get("outline"):
        bits.append(f"描边 {style['outline']} {style.get('outline_color') or ''}".strip())
    if style.get("color_style"):
        bits.append(f"花字预设 {style['color_style']}")
    if style.get("in_effect"):
        bits.append(f"入场动画 {style['in_effect']}")
    return "，".join(bits)


def lines(found: dict | None) -> list[str]:
    """The sheet in plain lines, exact names kept (font, effect, transition, filter, track id)."""
    if not found:
        return []
    canvas = found.get("canvas") or {}
    head = f"画布 {canvas.get('width', '?')}×{canvas.get('height', '?')}，{canvas.get('fps', '?')} 帧/秒"
    if found.get("seconds"):
        head += f"，全长 {_g(found['seconds'])} 秒"
    head += f"，{found.get('shot_count', len(found.get('shots') or []))} 个镜头"
    if found.get("filter"):
        head += f"，全片滤镜 {found['filter']}"
    out = [head, "镜头："]
    for index, shot in enumerate(found.get("shots") or [], 1):
        when = f"{_g(shot['at'][0])}–{_g(shot['at'][1])} 秒" if shot.get("at") else (
            f"时长 {_g(shot['sec'])} 秒" if shot.get("sec") else "到素材结尾")
        bits = [when]
        if shot.get("speed"):
            bits.append(f"变速 {_g(shot['speed'])} 倍")
        if shot.get("move"):
            bits.append(f"运镜 {MOVES.get(shot['move'], '自定义推移')}（{shot['move']}）")
        if shot.get("filter"):
            bits.append(f"滤镜 {shot['filter']}")
        if shot.get("backdrop"):
            bits.append("留边处用虚化背景")
        elif shot.get("fit"):
            bits.append(f"画面适配 {shot['fit']}")
        if shot.get("muted"):
            bits.append("原声静音")
        bits.append(f"出点转场 {shot['transition']['type']} {_g(shot['transition']['sec'])} 秒" if shot.get("transition")
                    else "硬切")
        out.append(f"  {index}. " + "，".join(bits))
    captions = found.get("captions")
    if captions:
        where = f"底边距 {_g(round((captions.get('bottom_ratio') or 0) * 100, 1))}%"
        out.append(f"字幕：{captions['count']} 条，{_g(captions.get('from'))}–{_g(captions.get('to'))} 秒，{where}，"
                   f"最长一条 {captions['max_chars']} 字，{_look(captions)}")
    else:
        out.append("字幕：无")
    texts = found.get("texts") or []
    if texts:
        out.append(f"花字/标题：{len(texts)} 条")
        for index, text in enumerate(texts, 1):
            out.append(f"  {index}. {_g(text['at'][0])}–{_g(text['at'][1])} 秒，位置 横 {_g(text.get('x'))} 纵 {_g(text.get('y'))}"
                       f"（{text.get('align')} 对齐），{text['chars']} 字，{_look(text)}")
    else:
        out.append("花字/标题：无")
    music = found.get("music") or []
    for bed in music:
        bits = [f"{_g(bed['at'][0])}–{_g(bed['at'][1]) if bed['at'][1] is not None else '片尾'} 秒",
                f"音量 {_g(bed.get('volume'))}"]
        if bed.get("title"):
            bits.insert(0, f"曲目 {bed['title']}")
        if bed.get("track_id"):
            bits.append(f"曲目编号 {bed['track_id']}")
        else:
            bits.append("老板自带的音频")
        if bed.get("moods"):
            bits.append("情绪 " + "/".join(bed["moods"]))
        if bed.get("bpm"):
            bits.append(f"约 {bed['bpm']} 拍/分")
        if bed.get("fade_in_sec"):
            bits.append(f"淡入 {_g(bed['fade_in_sec'])} 秒")
        if bed.get("fade_out_sec"):
            bits.append(f"淡出 {_g(bed['fade_out_sec'])} 秒")
        if bed.get("duck_volume") is not None:
            bits.append(f"有人声或字幕时压到 {_g(bed['duck_volume'])}")
        if bed.get("beat_cuts"):
            bits.append(f"{bed['beat_cuts']} 个切点对了拍")
        out.append("配乐：" + "，".join(bits))
    if not music:
        out.append("配乐：无")
    voice = found.get("voiceover")
    if voice:
        low, high = voice["speech_rate"]
        rate = f"语速 {low}" if low == high else f"语速 {low} 到 {high}"
        out.append(f"配音：合成音色 {voice['voice']}，{voice['lines']} 句，{_g(voice.get('from'))}–{_g(voice.get('to'))} 秒，"
                   f"{rate}，音量 {_g(voice.get('volume'))}")
    else:
        out.append("配音：无合成配音")
    for overlay in found.get("overlays") or []:
        when = f"{_g(overlay['at'][0])}–{_g(overlay['at'][1])} 秒"
        if overlay["kind"] == "image" and (overlay.get("width") or 0) >= 0.99 and (overlay.get("height") or 0) >= 0.99:
            # Lettering drawn ahead as a picture and laid over the whole frame: its font is not on the timeline.
            out.append(f"整屏贴图：{when}（预先画好的花字或标题卡，字体不在剪辑清单里，看画面写）")
        else:
            out.append(f"画中画：{'图片' if overlay['kind'] == 'image' else '视频'}，{when}，"
                       f"位置 横 {_g(overlay.get('x'))} 纵 {_g(overlay.get('y'))}，宽 {_g(overlay.get('width'))} 高 {_g(overlay.get('height'))}")
    return out
