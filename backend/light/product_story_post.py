"""The product story's exact-frame offline edit and verified delivery encode."""

import asyncio
import json
import math
from pathlib import Path
import shutil
import struct
import wave

from PIL import Image, ImageDraw, ImageFont, ImageOps

from light import product_story_plan as recipe
from light.media_fonts import latin_font, mixed_font
from light.remake import RemakeStopped
from light.remake_lettering import _has_cjk
from light.video_language import MEDIA_TAGS

WIDTH, HEIGHT = 1280, 720
RATE = 48000
FONTS = (
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
)


def require_tools():
    if not all(shutil.which(name) for name in ("ffmpeg", "ffprobe")) or not any(Path(f).exists() for f in FONTS):
        raise RemakeStopped("当前制作服务缺少品牌剧情广告的剪辑组件。")


async def _run(program, *args):
    try:
        process = await asyncio.create_subprocess_exec(
            program, *map(str, args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise RemakeStopped("剪辑组件无法启动。") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), 120)
    except (TimeoutError, asyncio.CancelledError) as exc:
        if process.returncode is None:
            process.kill()
            await process.wait()
        if isinstance(exc, TimeoutError):
            raise RemakeStopped("剪辑暂时超时，重试会用回已核对的镜头。") from exc
        raise
    if process.returncode:
        from core.log import create_logger
        create_logger(__name__).warning("Product edit %s failed: %s", program, stderr.decode(errors="replace")[-800:])
        raise RemakeStopped("视频剪辑未完成，已保留素材供重试。")
    return stdout


async def ff(*args):
    return await _run("ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                      "-filter_threads", "2", "-filter_complex_threads", "2", *args)


async def _clip(folder, index, cut, shot):
    source = folder / f"source-{index:02d}.mp4"
    target = folder / f"edit-{index:02d}.mp4"
    duration = cut["output_frames"] / recipe.FPS
    start = cut["start"] / recipe.FPS
    end = (cut["start"] + cut["frames"]) / recipe.FPS
    speed = cut["speed"]
    crop = ("crop=w='trunc(iw*6/7/2)*2':h='trunc(ih*6/7/2)*2':x='iw*0.06':y='ih*0.05',"
            if index == 14 and not shot.get("adaptive") else "")
    pixels = (
        f"fps=24,trim=start_frame={cut['start']}:end_frame={cut['start'] + cut['frames']},"
        f"setpts=(PTS-STARTPTS)/{speed:.12f},fps=24,tpad=stop_mode=clone:stop_duration=0.15,"
        f"trim=end_frame={cut['output_frames']},{crop}scale=1280:720:force_original_aspect_ratio=increase,"
        "crop=1280:720,setsar=1"
    )
    voice = bool(shot["spoken_line"])
    if dub := cut.get("dub"):
        delay = round((cut["cues"][0]["from_sec"] - start) / speed * 1000)
        inputs = ["-i", folder / f"voice-{index:02d}.wav"]
        audio = (f"[1:a:0]atempo={dub['tempo'] * speed:.9f},aresample={RATE},"
                 "aformat=channel_layouts=stereo,highpass=f=75,"
                 f"adelay={max(0, delay)}:all=1,apad,atrim=duration={duration:.9f}[track]")
    elif cut["has_audio"]:
        audio = (f"[0:a:0]atrim=start={start:.9f}:end={end:.9f},asetpts=PTS-STARTPTS,atempo={speed:.9f},"
                 f"aresample={RATE},aformat=channel_layouts=stereo,highpass=f=75,"
                 f"volume={1 if voice else .41},afade=t=in:d=0.025,afade=t=out:st={duration - .05:.9f}:d=0.05,"
                 f"apad,atrim=duration={duration:.9f}[track]")
        inputs = []
    else:
        audio = f"[1:a]atrim=duration={duration:.9f}[track]"
        inputs = ["-f", "lavfi", "-i", f"anullsrc=r={RATE}:cl=stereo"]
    await ff("-threads", "2", "-i", source, *inputs, "-filter_complex", f"[0:v]{pixels}[picture];{audio}",
             "-map", "[picture]", "-map", "[track]", "-c:v", "libx264", "-threads:v", "3", "-preset", "fast",
             "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "alac", "-video_track_timescale", "24000", target)
    return target


def _screen(folder, still):
    with Image.open(folder / "source-18.jpg") as plate:
        screen = ImageOps.fit(plate.convert("RGB"), (1536, 864), method=Image.Resampling.LANCZOS)
    coords = recipe.case()["screen"]
    x0, y0, x1, y1 = coords["bounds"]
    size = x1 - x0, y1 - y0
    with Image.open(still) as source:
        photo = ImageOps.fit(source.convert("RGB"), size, method=Image.Resampling.LANCZOS, centering=(.5, .42))
    mask = Image.new("L", size)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), coords["radius"], fill=255)
    px, py = coords["notch"]
    radius = coords["notch_radius"]
    hole = screen.crop((px-radius, py-radius, px+radius+1, py+radius+1))
    lens = Image.new("L", hole.size)
    ImageDraw.Draw(lens).ellipse((0, 0, hole.width-1, hole.height-1), fill=255)
    screen.paste(photo, (x0, y0), mask)
    screen.paste(hole, (px-radius, py-radius), lens)
    path = folder / "photo-on-phone.png"
    screen.save(path)
    return path


def _titles(folder, plan):
    font = next(path for path in FONTS if Path(path).exists())
    graphic = Image.new("RGBA", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(graphic)

    def label(x, y, size, text, ink, *, edge=1252):
        family = (lambda pt: ImageFont.truetype(font, pt)) if plan.language == "zh" else (
            mixed_font if _has_cjk(text) else latin_font
        )
        face = family(size)
        while size > 10:
            face = family(size)
            if draw.textlength(text, font=face) <= edge - x:
                break
            size -= 1
        draw.text((x, y), text, font=face, fill=ink, anchor="lt")

    if isinstance(plan, recipe.BrandPlan):
        def brand_label(text, y, size, color):
            family = mixed_font if _has_cjk(text) else latin_font
            for point in range(size, 11, -1):
                face = family(point)
                if draw.textlength(text, font=face) <= 470:
                    draw.text((748, y), text, font=face, fill=color, anchor="lt")
                    return
                split = len(text) // 2
                spaces = [i for i, c in enumerate(text) if c.isspace()]
                if spaces:
                    split = min(spaces, key=lambda i: abs(i - split))
                lines = (text[:split].rstrip(), text[split:].lstrip())
                if max(draw.textlength(line, font=face) for line in lines) <= 470:
                    for index, line in enumerate(lines):
                        draw.text((748, y + index * (point + 10)), line, font=face, fill=color, anchor="lt")
                    return
            label(748, y, 12, text, color)

        locked = isinstance(plan, recipe.ContinuityPlan)
        brand_label(plan.product, 252, 62 if locked else 52, "#f4f8ff")
        brand_label(plan.headline, 395, 40 if locked else 27, "#deecff")
        music = {"zh": "配乐", "en": "Music", "vi": "Nhạc"}[plan.language]
        credit = f"{music} · Dream Culture — Kevin MacLeod"
    else:
        concept = {"zh": "概念广告", "en": "Concept ad", "vi": "Quảng cáo ý tưởng"}
        label(744, 245, 17, concept[plan.language], "#abc9ec")
        label(738, 279, 60, plan.product, "#f4f8ff")
        label(744, 370, 30, plan.headline, "#deecff")
        credit = "Music: Dream Culture — Kevin MacLeod"
    label(744, 650, 14, credit, "#b9cee9")
    label(744, 673, 13, "incompetech.com · CC BY 4.0 · Excerpted & mixed", "#b9cee9")
    path = folder / "titles.png"
    graphic.save(path)
    return path


async def _motion(folder, index, plate, frames, zoom, title=None):
    start = (f"scale=3840:2160:flags=lanczos,zoompan=z='{zoom}':x='(iw-iw/zoom)/2':"
             f"y='(ih-ih/zoom)/2':d={frames}:s=1280x720:fps=24,setsar=1")
    if index == 17:
        start += ",fade=t=in:s=0:n=3:color=0xf1eee3"
    graph = f"[0:v]{start}[picture]"
    extra = []
    if title:
        extra = ["-loop", "1", "-framerate", "24", "-i", title]
        graph = f"[0:v]{start}[hero];[2:v]format=rgba,fade=in:s=2:n=9:alpha=1[t];[hero][t]overlay[picture]"
    path = folder / f"edit-{index:02d}.mp4"
    await ff("-i", plate, "-f", "lavfi", "-i", f"anullsrc=r={RATE}:cl=stereo", *extra,
             "-filter_complex", graph, "-map", "[picture]", "-map", "1:a", "-frames:v", frames,
             "-t", f"{frames / 24:.9f}", "-c:v", "libx264", "-threads:v", "3", "-preset", "fast",
             "-crf", "16", "-pix_fmt", "yuv420p", "-c:a", "alac", "-video_track_timescale", "24000", path)
    return path


def _subtitles(folder, cuts):
    def stamp(seconds):
        ms = round(seconds * 1000)
        return f"{ms//3600000:02}:{ms//60000%60:02}:{ms//1000%60:02},{ms%1000:03}"

    entries = []
    offset = 0
    for cut in cuts:
        for cue in cut["cues"]:
            in_sec = cut["start"] / recipe.FPS
            length = cut["output_frames"] / recipe.FPS
            start = max(0, (cue["from_sec"] - in_sec) / cut["speed"])
            end = min(length, (cue["to_sec"] - in_sec) / cut["speed"])
            if end <= start:
                raise RemakeStopped("一个对白字幕超出了对应镜头。")
            entries.append((offset + start, offset + end, cue["text"]))
        offset += cut["output_frames"] / recipe.FPS
    path = folder / "dialogue.srt"
    path.write_text("".join(
        f"{number}\n{stamp(a)} --> {stamp(b)}\n{text}\n\n"
        for number, (a, b, text) in enumerate(entries, 1)
    ), encoding="utf-8")
    return path, entries


def _shutter(folder):
    path = folder / "shutter.wav"
    with wave.open(str(path), "wb") as file:
        file.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        for i in range(int(RATE * .13)):
            t = i / RATE
            level = (1 - math.exp(-t * 1300)) * math.exp(-t * 70)
            sample = level * (.24 * math.sin(2 * math.pi * 1800 * t) + .12 * math.sin(2 * math.pi * 460 * t))
            file.writeframesraw(struct.pack("<h", int(sample * 32767)))
    return path


async def assemble(folder, plan, cuts):
    shots = recipe.shots(plan)
    adaptive = isinstance(plan, recipe.BrandPlan)
    # Keep at most three encoders active in a single production.
    from light.remake import _parallel

    async def clip(cut):
        index = cut["shot"]
        return await _clip(folder, index, cut, shots[index - 1])

    live = await _parallel(cuts, clip, concurrency=3)
    title = await asyncio.to_thread(_titles, folder, plan)
    if adaptive:
        end_card = len(shots)
        specs = [(end_card, folder / f"source-{end_card:02d}.jpg", "1", title)]
    else:
        still = folder / "selfie.png"
        await ff("-i", live[-1], "-vf", f"select=eq(n\\,{shots[15]['frames'] - 1})",
                 "-frames:v", "1", "-update", "1", still)
        phone = await asyncio.to_thread(_screen, folder, still)
        specs = [
            (17, still, "1", None), (18, phone, "1.19-0.19*on/88", None),
            (19, folder / "source-19.jpg", "1.01+0.035*on/29", None),
            (20, folder / "source-20.jpg", "1.04-0.025*on/23", None),
            (21, folder / "source-21.jpg", "1.00+0.025*on/65", title),
        ]

    async def post(item):
        index, image, zoom, text = item
        return await _motion(folder, index, image, shots[index - 1]["frames"], zoom, title=text)

    clips = live + await _parallel(specs, post, concurrency=2)
    srt, cues = _subtitles(folder, cuts)
    count = len(clips)
    inputs = [item for file in clips for item in ("-threads", "1", "-i", file)]
    graph = "".join(
        f"[{i}:a]apad,atrim=duration={shot['frames']/recipe.FPS:.9f},asetpts=PTS-STARTPTS[a{i}];"
        for i, shot in enumerate(shots)
    )
    graph += "".join(f"[{i}:v][a{i}]" for i in range(count))
    graph += f"concat=n={count}:v=1:a=1[vcat][acat];[vcat]setpts=N/(24*TB)[picture];"
    inputs.extend(("-i", recipe.ASSETS / "dream-culture.mp3"))
    if adaptive:
        duck = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b, _ in cues)
        level = f"if({duck},0.14,0.40)" if duck else "0.40"
        graph += (
            f"[{count}:a]aresample={RATE},volume='{level}':eval=frame,"
            "afade=t=in:d=0.40,afade=t=out:st=53.1:d=0.9[music];"
            "[acat][music]amix=inputs=2:duration=first:normalize=0,"
            "apad,atrim=duration=54,alimiter=limit=0.84:level=false:latency=true[sound]"
        )
    else:
        inputs.extend(("-i", _shutter(folder)))
        first = round((sum(s["frames"] for s in shots[:6]) / recipe.FPS + .98) * 1000)
        duck = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b, _ in cues if a >= first / 1000)
        level = f"if({duck},0.21,0.53)" if duck else "0.53"
        graph += (
            f"[{count}:a]aresample={RATE},adelay={first}:all=1,volume='{level}':eval=frame,"
            "afade=t=in:st=14.14:d=0.38,afade=t=out:st=53.1:d=0.9[music];"
            f"[{count+1}:a]asplit[s1][s2];[s1]adelay={first}:all=1,volume=0.68[early];"
            "[s2]adelay=44000:all=1[late];"
            "[acat][music][early][late]amix=inputs=4:duration=first:normalize=0,"
            "apad,atrim=duration=54,volume=1.48,alimiter=limit=0.84:level=false:latency=true[sound]"
        )
    output = folder / "final.mp4"
    language = MEDIA_TAGS[plan.language]
    subtitle_opts = []
    if cues:
        inputs.extend(("-i", srt))
        subtitle_index = count + (1 if adaptive else 2)
        subtitle_opts = ["-map", f"{subtitle_index}:s", "-c:s", "mov_text", "-metadata:s:s:0", f"language={language}",
                         "-disposition:s:0", "0"]
    await ff(*inputs, "-filter_complex", graph, "-map", "[picture]", "-map", "[sound]", *subtitle_opts,
             "-c:v", "libx264", "-threads:v", "4", "-preset", "medium", "-crf", "15", "-r", "24",
             "-pix_fmt", "yuv420p", "-video_track_timescale", "24000", "-colorspace", "bt709",
             "-color_primaries", "bt709", "-color_trc", "bt709", "-c:a", "aac", "-b:a", "192k",
             "-metadata:s:a:0", f"language={language}",
             "-metadata", f"title={plan.product}｜{plan.title}", "-metadata", f"comment={recipe.CREDITS}",
             "-movflags", "+faststart", output)
    probe = json.loads(await _run("ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", output))
    video, audio, *tracks = probe["streams"]
    subtitle_ok = ((len(tracks) == 1 and tracks[0]["codec_name"] == "mov_text"
                    and tracks[0].get("tags", {}).get("language") == language) if cues else not tracks)
    if (video["nb_frames"] != str(recipe.FRAME_COUNT) or video["avg_frame_rate"] != "24/1"
            or (video["width"], video["height"]) != (WIDTH, HEIGHT)
            or any(abs(float(stream["duration"]) - recipe.DURATION) > .001 for stream in (video, audio, probe["format"]))
            or audio["codec_name"] != "aac" or audio["channels"] != 2 or not subtitle_ok
            or len(cues) != sum(bool(shot["spoken_line"]) for shot in shots)):
        raise RemakeStopped("成片时长、帧数或音轨校验未通过，镜头已保留。")
    await ff("-xerror", "-i", output, "-map", "0:v", "-map", "0:a", "-f", "null", "-")
    return output, {"duration_sec": recipe.DURATION, "frames": recipe.FRAME_COUNT,
                    "fps": recipe.FPS, "has_audio": True, "dialogue": len(cues), "attribution": recipe.CREDITS,
                    "language": plan.language, "subtitles_srt": srt.read_text(encoding="utf-8")}
