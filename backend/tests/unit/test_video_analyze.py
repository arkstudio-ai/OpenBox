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
