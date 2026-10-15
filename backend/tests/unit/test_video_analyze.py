"""video_analyze: sandbox sampling, frame floor, staging, metering, caching."""
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import tool.video_analyze as va
from tool.tool import ToolContext
from tool.video_analyze import VideoAnalyzeArgs, execute
from video.analysis import parse_analysis, AnalysisParseError

GOOD_JSON = '{"form":"口播","topic":"留白","audience":"装修人群","hook":"别把家塞满","structure":[{"from_sec":0,"to_sec":5,"what":"抛观点"}],' \
            '"visual_style":"书房中景","script_text":"装修真正高级的地方是留白","on_screen_text":["留白"],"topics":["装修"],' \
            '"recreate_elements":{"presenter":"女","scene":"书房","pace":"稳","caption_style":"底部白字","music":"无"},"risk_notes":""}'


class FakeSandbox:
    base_url = "http://sandbox.test"

    def __init__(self, *, frames=8, audio=True, duration=25.3, ffmpeg=True):
        self.frames, self.audio, self.duration, self.ffmpeg = frames, audio, duration, ffmpeg
        self.commands: list[str] = []

    async def execute(self, command, timeout=120, workdir="/workspace"):
        self.commands.append(command)
        if "ffprobe" in command:
            if not self.ffmpeg:
                return SimpleNamespace(exit_code=9, stdout="NO_FFMPEG", stderr="")
            return SimpleNamespace(exit_code=0, stdout=f"{self.duration}\n", stderr="")
        if "ffmpeg -y" in command:
            names = [f"frame_{i:02d}.jpg" for i in range(1, self.frames + 1)] + (["audio.mp3"] if self.audio else [])
            return SimpleNamespace(exit_code=0, stdout="\n".join(names), stderr="")
        if "obx-file put" in command:
            oks = [f"OK:{seg.split('OK:')[1].split(' ')[0]}" for seg in command.split("echo ")[1::2] if seg.startswith("OK:")]
            return SimpleNamespace(exit_code=0, stdout="\n".join(oks), stderr="")
        return SimpleNamespace(exit_code=0, stdout="", stderr="")


class FakeOss:
    bucket = "b"; host = "b.oss-cn-shanghai.aliyuncs.com"; endpoint = "oss-cn-shanghai.aliyuncs.com"; region = "cn-shanghai"
    internal_host = "b.oss-cn-shanghai-internal.aliyuncs.com"

    def presign_put(self, key, mime, expires_sec=600, internal=False):
        return f"https://{self.host}/{key}?put"

    def presign_get(self, key, expires_sec=3600, **_):
        return f"https://{self.host}/{key}?get"


@pytest.fixture
def env(monkeypatch):
    oss = FakeOss()
    monkeypatch.setattr("core.oss.get_oss", lambda: oss)

    async def ensure_cli(*_a, **_k):
        return None
    monkeypatch.setattr("sandbox.assets.ensure_cli", ensure_cli)
    monkeypatch.setattr("sandbox.assets._use_internal_oss", lambda _o: False)

    async def transcribe(url):
        return {"text": "装修真正高级的地方是留白", "duration_ms": 25300, "model": "fun-asr", "provider": "dashscope"}
    monkeypatch.setattr(va, "_transcribe", transcribe)

    calls = {}

    async def complete(ctx, *, model, frames, duration, transcript, timeout):
        calls.update({"model": model, "frames": frames, "duration": duration, "transcript": transcript})
        return GOOD_JSON, {"input": 9000, "output": 1600, "total": 10600}, Decimal("0.09")
    monkeypatch.setattr(va, "_complete", complete)
    monkeypatch.setenv("BILLING_MODE", "shadow")
    return oss, calls


async def _ctx(sandbox):
    from db.base import get_db_session
    from db.models.file_asset import FileAsset

    uid, wid = "u_" + uuid.uuid4().hex[:8], "w_" + uuid.uuid4().hex[:8]
    aid = "asset_" + uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(FileAsset(id=aid, user_id=uid, workspace_id=wid, session_id="s1", project_id=None, name="hot.mp4",
                         oss_key=f"assets/{uid}/{aid}/hot.mp4", mime="video/mp4", size=10, status="ready", source="agent",
                         transient=False, created_at=now))
    return ToolContext(user_id=uid, workspace_id=wid, session_id="s1", message_id="m1", sandbox=sandbox), aid


def _kv(result):
    return dict(line.split("=", 1) for line in result.output.splitlines() if "=" in line)


def test_args_and_schema_parsing():
    with pytest.raises(ValidationError, match="requires source"):
        VideoAnalyzeArgs(action="analyze")
    with pytest.raises(ValidationError, match="requires job_id"):
        VideoAnalyzeArgs(action="status")
    parsed = parse_analysis("```json\n" + GOOD_JSON + "\n```")
    assert parsed.form == "口播" and parsed.structure[0].to_sec == 5
    with pytest.raises(AnalysisParseError):
        parse_analysis("sorry, no")
    with pytest.raises(AnalysisParseError):
        parse_analysis('{"form": "舞蹈"}')  # not in the vocabulary


async def test_direct_media_url_is_sampled_transcribed_analysed_and_cached(env):
    oss, calls = env
    sb = FakeSandbox()
    ctx, _ = await _ctx(sb)
    url = "https://v11-weba.douyinvod.com/abc/video.mp4?x=1"
    result = await execute(VideoAnalyzeArgs(source=url), ctx)
    kv = _kv(result)
    assert kv["status"] == "completed" and kv["frames_used"] == "8" and kv["duration_sec"] == "25.3"
    assert result.metadata["analysis"]["form"] == "口播"
    assert len(calls["frames"]) == 8 and calls["frames"][0].endswith("frame_01.jpg?get")
    assert "留白" in calls["transcript"]
    # Referer + UA headers for douyin CDN, frames spread across the duration
    assert any("Referer: https://www.douyin.com/" in c for c in sb.commands)
    assert any("fps=0.316" in c for c in sb.commands)  # 8 / 25.3
    # STT (0.05) + vision (0.09) reported
    assert kv["credits"] == "0.14"
    # second call: cached, no sandbox work
    n = len(sb.commands)
    again = await execute(VideoAnalyzeArgs(source=url), ctx)
    assert again.metadata["cached"] is True and _kv(again)["job_id"] == kv["job_id"] and len(sb.commands) == n
    # force re-runs
    forced = await execute(VideoAnalyzeArgs(source=url, force=True), ctx)
    assert forced.metadata.get("cached") is None and _kv(forced)["job_id"] != kv["job_id"]


async def test_own_bucket_url_is_signed_for_ffmpeg_and_cached_by_unsigned_key(env):
    """A bare URL into the private asset bucket 403s in ffprobe; it must be presigned,
    and the cache key must be the stable unsigned object so a re-run hits the cache."""
    oss, calls = env
    sb = FakeSandbox()
    ctx, aid = await _ctx(sb)
    key = f"assets/{ctx.user_id}/{aid}/留白装修口播15秒.mp4"
    from urllib.parse import quote
    url = f"https://{oss.host}/{quote(key)}"
    result = await execute(VideoAnalyzeArgs(source=url), ctx)
    kv = _kv(result)
    assert kv["status"] == "completed"
    assert any(f"{key}?get" in c and "ffprobe" in c for c in sb.commands), sb.commands[:2]
    # no douyin Referer for our own bucket
    assert not any("Referer:" in c for c in sb.commands)
    # oss:// spelling and the percent-encoded https spelling resolve to the same cached job
    again = await execute(VideoAnalyzeArgs(source=f"oss://{oss.bucket}/{key}"), ctx)
    assert again.metadata["cached"] is True and _kv(again)["job_id"] == kv["job_id"]
    # someone else's object in our bucket is refused, not signed
    other = await execute(VideoAnalyzeArgs(source=f"https://{oss.host}/assets/u_other/x/hot.mp4"), ctx)
    assert other.title == "Analysis refused" and "not your asset" in other.output
    assert not any("u_other" in c for c in sb.commands)


async def test_owned_asset_is_materialised_and_foreign_or_non_video_refused(env, monkeypatch):
    sb = FakeSandbox()
    ctx, aid = await _ctx(sb)

    async def materialize(asset, ctx):
        return f"/workspace/generated_videos/{asset.name}"
    monkeypatch.setattr("tool.video_production._materialize_asset", materialize)
    result = await execute(VideoAnalyzeArgs(source=aid), ctx)
    assert _kv(result)["status"] == "completed"
    assert any("/workspace/generated_videos/hot.mp4" in c for c in sb.commands)

    other, _ = await _ctx(sb)
    refused = await execute(VideoAnalyzeArgs(source=aid), other)
    assert "not a ready asset owned by you" in refused.output
    refused = await execute(VideoAnalyzeArgs(source="https://example.com/page.html"), ctx)
    assert "not a known media host" in refused.output


async def test_too_few_frames_is_a_failure_not_a_text_fallback(env):
    oss, calls = env
    ctx, _ = await _ctx(FakeSandbox(frames=2))
    result = await execute(VideoAnalyzeArgs(source="https://v1.douyinvod.com/x.mp4"), ctx)
    assert result.metadata["status"] == "failed" and "need ≥4" in result.output and "NOT run" in result.output
    assert calls == {}  # the model was never called
    from db.base import get_db_session
    from db.models.video_job import VideoJob
    async with get_db_session() as db:
        job = await db.get(VideoJob, result.metadata["job_id"])
    assert job.status == "failed" and "frame" in job.error


async def test_retry_after_failure_reuses_the_job_and_clears_the_stale_error(env):
    """Same source after a failed attempt: the failed row is retried, and the
    completed output must not still show the previous attempt's error."""
    sb = FakeSandbox(ffmpeg=False)
    ctx, _ = await _ctx(sb)
    url = "https://v11-weba.douyinvod.com/retry/video.mp4"
    failed = await execute(VideoAnalyzeArgs(source=url), ctx)
    assert failed.metadata["status"] == "failed" and "no ffmpeg" in failed.output
    sb.ffmpeg = True
    ok = await execute(VideoAnalyzeArgs(source=url), ctx)
    kv = _kv(ok)
    assert kv["status"] == "completed" and kv["job_id"] == failed.metadata["job_id"]
    assert "error=" not in ok.output
    again = await execute(VideoAnalyzeArgs(source=url), ctx)
    assert again.metadata["cached"] is True and "error=" not in again.output


async def test_missing_ffmpeg_and_overlong_video_are_refused(env):
    ctx, _ = await _ctx(FakeSandbox(ffmpeg=False))
    result = await execute(VideoAnalyzeArgs(source="https://v1.douyinvod.com/x.mp4"), ctx)
    assert "no ffmpeg" in result.output
    ctx, _ = await _ctx(FakeSandbox(duration=1200))
    result = await execute(VideoAnalyzeArgs(source="https://v1.douyinvod.com/x.mp4"), ctx)
    assert "limit is 600s" in result.output


async def test_no_audio_skips_transcription_and_unparseable_model_output_fails_cleanly(env, monkeypatch):
    oss, calls = env
    ctx, _ = await _ctx(FakeSandbox(audio=False))
    result = await execute(VideoAnalyzeArgs(source="https://v1.douyinvod.com/silent.mp4"), ctx)
    assert _kv(result)["status"] == "completed" and calls["transcript"] == "" and _kv(result)["credits"] == "0.09"

    async def garbage(ctx, **_k):
        return "I cannot help with that", {}, Decimal("0.01")
    monkeypatch.setattr(va, "_complete", garbage)
    ctx2, _ = await _ctx(FakeSandbox())
    result = await execute(VideoAnalyzeArgs(source="https://v1.douyinvod.com/other.mp4"), ctx2)
    assert result.metadata["status"] == "failed" and "no JSON object" in result.output


def test_tool_is_registered_exposed_and_build_only():
    from agent.agent import AGENTS, BUILD_ONLY_WORKFLOW_TOOLS
    from agent.tool_exposure import INTENT_PACKS

    assert "video_analyze" in BUILD_ONLY_WORKFLOW_TOOLS and "video_analyze" in AGENTS["build"].tools
    assert "video_analyze" in INTENT_PACKS["video"]
    assert va.video_analyze_tool.sandbox_required is True


# ── screen_text: the picture-text check behind the video skill's card 3 ─────
#
# 2026-10-15 (Gaode production, slug crab-voucher): Seedance 2.0 drew the
# presenter's lines into shots 4 and 5 as captions — with 限时 rendered as 随时 —
# although every prompt said 无字幕 twice. STT compared the audio (97.8% / 100%)
# and the cut was delivered as "clean, no captions". Only the frames show it.

from video.analysis import parse_screen_text  # noqa: E402

TEXT_JSON = '{"has_text": true, "items": [{"text": "随时领券更划算", "kind": "subtitle", "frames": [10, 11, 12]},' \
            '{"text": "bossip", "kind": "watermark", "frames": [1, 99]}], "notes": "底部白字随语音出现"}'
CLEAN_JSON = '{"has_text": false, "items": [], "notes": ""}'


class FrameCountingSandbox(FakeSandbox):
    """Produces exactly the frames ffmpeg was asked for, and answers md5sum."""

    def __init__(self, *, duration=12.4, md5="a" * 32):
        super().__init__(duration=duration)
        self.md5 = md5

    async def execute(self, command, timeout=120, workdir="/workspace"):
        if command.startswith("md5sum "):
            self.commands.append(command)
            return SimpleNamespace(exit_code=0, stdout=f"{self.md5}\n", stderr="")
        if "ffmpeg -y" in command:
            import re
            self.frames = int(re.search(r"-frames:v (\d+)", command).group(1))
            self.audio = "-vn" in command
        return await super().execute(command, timeout, workdir)


@pytest.fixture
def text_env(env, monkeypatch):
    oss, calls = env
    answers = {"text": TEXT_JSON}

    async def complete(ctx, *, model, frames, duration, transcript, timeout, **kw):
        calls.update({"model": model, "frames": frames, "duration": duration, "transcript": transcript, **kw})
        return answers["text"], {"input": 4000, "output": 200, "total": 4200}, Decimal("0.03")
    monkeypatch.setattr(va, "_complete", complete)
    return oss, calls, answers


def test_screen_text_parsing_turns_frames_into_seconds_and_drops_invented_frames():
    report = parse_screen_text(TEXT_JSON, frame_count=13, interval_sec=0.95)
    assert report.has_text is True
    caption, mark = report.items
    assert caption.kind == "subtitle" and caption.frames == [10, 11, 12]
    assert caption.seconds == [8.5, 9.5, 10.4]
    assert mark.frames == [1] and mark.seconds == [0.0]  # frame 99 of 13 does not exist
    # items are evidence: a report with text in it is never "no text"
    flipped = parse_screen_text(TEXT_JSON.replace('"has_text": true', '"has_text": false'), frame_count=13, interval_sec=1)
    assert flipped.has_text is True
    assert parse_screen_text(CLEAN_JSON, frame_count=6, interval_sec=1).has_text is False
    with pytest.raises(AnalysisParseError):
        parse_screen_text("no captions, promise", frame_count=6, interval_sec=1)
    with pytest.raises(ValidationError, match="screen_text requires source"):
        VideoAnalyzeArgs(action="screen_text")


async def test_screen_text_samples_about_one_frame_per_second_without_audio(text_env):
    oss, calls, _ = text_env
    sb = FrameCountingSandbox(duration=12.4)
    ctx, _ = await _ctx(sb)
    result = await execute(VideoAnalyzeArgs(action="screen_text", source="/workspace/generated_videos/shot4.mp4"), ctx)
    kv = _kv(result)
    assert kv["status"] == "completed" and kv["frames_used"] == "13" and kv["has_text"] == "true"
    assert result.title.endswith("有文字")
    assert any("-frames:v 13" in c and "fps=1.048" in c for c in sb.commands)  # 13 / 12.4
    assert not sb.audio and calls["transcript"] == ""
    # the prompt asks about text and nothing else, and knows the sampling interval
    assert "找出画面里出现的一切文字" in calls["prompt_text"] and "0.95 秒" in calls["prompt_text"]
    assert calls["purpose"] == "video_screen_text"
    report = result.metadata["screen_text"]
    assert report["items"][0]["text"] == "随时领券更划算" and report["items"][0]["seconds"] == [8.5, 9.5, 10.4]
    assert kv["credits"] == "0.03"
    from db.base import get_db_session
    from db.models.video_job import VideoJob
    async with get_db_session() as db:
        job = await db.get(VideoJob, kv["job_id"])
    assert job.kind == "screen_text" and job.result_data["screen_text"]["has_text"] is True
    # status finds the screen_text job too
    status = await execute(VideoAnalyzeArgs(action="status", job_id=kv["job_id"]), ctx)
    assert _kv(status)["has_text"] == "true"


async def test_screen_text_frame_count_is_clamped_to_six_and_sixteen(text_env):
    for duration, expected in ((3.0, 6), (15.0, 15), (30.0, 16)):
        sb = FrameCountingSandbox(duration=duration, md5=uuid.uuid4().hex)
        ctx, _ = await _ctx(sb)
        result = await execute(VideoAnalyzeArgs(action="screen_text", source="/workspace/shot.mp4"), ctx)
        assert _kv(result)["frames_used"] == str(expected), duration
    # an explicit frame count is honoured
    sb = FrameCountingSandbox(duration=15.0)
    ctx, _ = await _ctx(sb)
    result = await execute(VideoAnalyzeArgs(action="screen_text", source="/workspace/shot.mp4", frames=8), ctx)
    assert _kv(result)["frames_used"] == "8"


async def test_screen_text_retake_at_the_same_path_is_checked_afresh(text_env):
    """shot4.mp4 :v2 overwrites :v1; the cached verdict for :v1 must not answer for it."""
    oss, calls, answers = text_env
    sb = FrameCountingSandbox(md5="1" * 32)
    ctx, _ = await _ctx(sb)
    first = await execute(VideoAnalyzeArgs(action="screen_text", source="/workspace/shot4.mp4"), ctx)
    assert _kv(first)["has_text"] == "true"
    n = len(sb.commands)
    same = await execute(VideoAnalyzeArgs(action="screen_text", source="/workspace/shot4.mp4"), ctx)
    assert same.metadata["cached"] is True and _kv(same)["job_id"] == _kv(first)["job_id"]
    assert len(sb.commands) == n + 1  # only the md5sum ran
    sb.md5 = "2" * 32
    answers["text"] = CLEAN_JSON
    retake = await execute(VideoAnalyzeArgs(action="screen_text", source="/workspace/shot4.mp4"), ctx)
    assert retake.metadata.get("cached") is None and _kv(retake)["job_id"] != _kv(first)["job_id"]
    assert _kv(retake)["has_text"] == "false" and retake.title.endswith("无文字")


async def test_screen_text_too_few_frames_is_a_failure_not_a_pass(text_env):
    oss, calls, _ = text_env
    sb = FrameCountingSandbox(duration=12.0)
    original = sb.execute

    async def starve(command, timeout=120, workdir="/workspace"):
        res = await original(command, timeout, workdir)
        if "ffmpeg -y" in command:
            return SimpleNamespace(exit_code=0, stdout="frame_01.jpg\nframe_02.jpg", stderr="")
        return res
    sb.execute = starve
    ctx, _ = await _ctx(sb)
    result = await execute(VideoAnalyzeArgs(action="screen_text", source="/workspace/shot.mp4"), ctx)
    assert result.metadata["status"] == "failed" and "NOT checked" in result.output
    assert "prompt_text" not in calls
