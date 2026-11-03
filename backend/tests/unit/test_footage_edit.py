"""你拍我剪 (M3): media facts, prices, sound in the timeline, and the batch analysis."""
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select

from billing import footage
from db.base import get_db_session
from db.models.billing import UsageEvent
from db.models.file_asset import FileAsset
from db.models.message import Message
from db.repository.user_repo import PgUserRepo
from video.media_probe import meta_from_media_info


def test_media_facts_apply_rotation_and_read_sound():
    rotated = meta_from_media_info({"MediaInfo": {"FileInfoList": [{
        "FileBasicInfo": {"Duration": "8.000000", "Width": "768", "Height": "432", "FormatName": "mov,mp4"},
        "VideoStreamInfoList": [{"CodecName": "hevc", "Width": "768", "Height": "432", "Rotate": "90", "Fps": "30.0"}],
        "AudioStreamInfoList": [],
    }]}}, "m1")
    assert (rotated["width"], rotated["height"], rotated["orientation"]) == (432, 768, "portrait")
    assert rotated["codec"] == "hevc" and rotated["has_audio"] is False and rotated["duration_sec"] == 8.0
    landscape = meta_from_media_info({"MediaInfo": {"FileInfoList": [{
        "FileBasicInfo": {"Duration": "8.0"},
        "VideoStreamInfoList": [{"CodecName": "h264", "Width": "768", "Height": "432", "Rotate": None}],
        "AudioStreamInfoList": [{"CodecName": "aac"}],
    }]}}, "m2")
    assert landscape["orientation"] == "landscape" and landscape["has_audio"] is True


def test_prices_follow_the_decision():
    assert footage.quote_analysis(75).credits == Decimal("2")       # 5 × 15 s → 2 started minutes
    assert footage.quote_analysis(30).credits == Decimal("1")       # under a minute is a minute
    assert footage.quote_analysis(300).credits == Decimal("5")
    assert footage.quote_compose(60).credits == Decimal("5")
    assert footage.quote_compose(60.4).credits == Decimal("9")
    assert footage.quote_compose(180).credits == Decimal("9")
    assert footage.quote_compose(181).credits == Decimal("11")
    assert footage.quote_compose(241).credits == Decimal("13")
    # rates.json may override the defaults
    custom = {"version": "t", "media": {"footage": {"analyze_per_minute": "2"}}}
    assert footage.quote_analysis(75, rates=custom).credits == Decimal("4")


def test_sound_compiles_to_audio_tracks_and_clip_volume():
    from video.ims_compiler import compile_ims
    from video.timeline import Timeline
    base = "https://bossip.oss-cn-shanghai.aliyuncs.com/assets/u/"
    compiled = compile_ims(Timeline.model_validate({
        "shots": [{"asset": base + "a.mp4", "duration_sec": 4, "volume": 0},
                  {"asset": base + "b.mov", "in_sec": 1, "duration_sec": 4}],
        "audio": [{"asset": base + "bed.m4a", "to_sec": 20, "volume": 0.4, "fade_in_sec": 0.5, "fade_out_sec": 1}],
    }), output_url=base + "out.mp4")
    first, second = compiled.timeline["VideoTracks"][0]["VideoTrackClips"]
    assert first["Effects"] == [{"Type": "Volume", "Gain": 0.0}] and "Effects" not in second
    (bed,) = compiled.timeline["AudioTracks"][0]["AudioTrackClips"]
    assert (bed["TimelineIn"], bed["TimelineOut"], bed["Out"]) == (0.0, 8.0, 8.0)  # clamped to the cut
    assert [effect["Type"] for effect in bed["Effects"]] == ["Volume", "AFade", "AFade"]
    assert any("clamped" in warning for warning in compiled.warnings)
    plain = compile_ims(Timeline.model_validate({"shots": [{"asset": base + "a.mp4", "duration_sec": 4}]}),
                        output_url=base + "out.mp4")
    assert "AudioTracks" not in plain.timeline


async def _owner_with_clips(durations: list[float], *, audio: list[bool]):
    uid = uuid4().hex
    user = await PgUserRepo().create(id=uid, username="fe" + uid[:8], password_hash=None)
    from session.session import create_session, update_session
    session = await create_session(model="m", agent="build", user_id=user["id"], workspace_id=user["default_workspace_id"])
    session = await update_session(session.id, user_id=user["id"], light_entry="footage_edit")
    now = datetime.now(timezone.utc)
    ids = []
    async with get_db_session() as db:
        for index, (duration, sound) in enumerate(zip(durations, audio)):
            asset_id = "asset_" + uuid4().hex[:20]
            ids.append(asset_id)
            db.add(FileAsset(id=asset_id, user_id=user["id"], workspace_id=user["default_workspace_id"],
                             session_id=session.id, name=f"clip{index}.mov", oss_key=f"assets/{user['id']}/clip{index}.mov",
                             mime="video/quicktime", size=1, status="ready", created_at=now,
                             meta={"duration_sec": duration, "orientation": "portrait", "has_audio": sound,
                                   "width": 720, "height": 1280, "codec": "hevc"}))
    return user, session, ids


async def test_one_batch_is_one_charge_by_total_minutes(monkeypatch):
    from tool import footage_analyze as fa
    from tool import video_production as vp
    from tool.tool import ToolContext

    user, session, ids = await _owner_with_clips([15, 15, 15, 15, 15], audio=[True, True, False, False, False])
    heard: list[str] = []

    async def fake_asr(target, url):
        heard.append(url)
        return {"text": "大家好，这里是巷口面馆", "sentences": [{"from_sec": 0.4, "to_sec": 2.6, "text": "大家好"}]}

    async def fake_vision(frames, *, duration, orientation, speech):
        assert len(frames) == 5 and all("video%2Fsnapshot" in url or "video/snapshot" in url for _, url in frames)
        return {"subject": "门头", "segments": [{"from_sec": 0, "to_sec": 3, "shot": "门头", "usable": True}]}

    class FakeOss:
        host = "bossip.oss-cn-shanghai.aliyuncs.com"

        def presign_get(self, key, expires_sec=3600, **_):
            return f"https://{self.host}/{key}?sig"

        def _presign(self, method, key, expires, subresource=None):
            return f"https://{self.host}/{key}?x-oss-process={subresource['x-oss-process']}"

    monkeypatch.setattr("core.oss.get_oss", lambda: FakeOss())
    monkeypatch.setattr(vp, "_configured_transcription_target", lambda: object())
    monkeypatch.setattr(vp, "_provider_transcribe", fake_asr)
    monkeypatch.setattr(fa, "_vision", fake_vision)
    monkeypatch.setattr("billing.service.billing_mode", lambda: "enforce")
    monkeypatch.setattr("billing.media.billing_mode", lambda: "enforce")

    ctx = ToolContext(session_id=session.id, user_id=user["id"], workspace_id=user["default_workspace_id"])
    result = await fa.execute(fa.FootageAnalyzeArgs(asset_ids=ids), ctx)
    meta = result.metadata
    assert len(meta["clips"]) == 5 and meta["total_footage_sec"] == 75 and meta["analysis_credits"] == "2"
    assert len(heard) == 2  # only clips with sound are transcribed
    assert meta["clips"][0]["transcript"] == "大家好，这里是巷口面馆" and meta["clips"][2]["transcript"] is None
    again = await fa.execute(fa.FootageAnalyzeArgs(asset_ids=list(reversed(ids))), ctx)
    assert again.metadata["analysis_credits"] == "2"
    async with get_db_session() as db:
        rows = (await db.scalars(select(UsageEvent).where(UsageEvent.session_id == session.id))).all()
    assert [(row.kind, row.credits, row.session_title) for row in rows] == [("footage_analyze", Decimal("2"), "素材分析")]

    stranger, _, _ = await _owner_with_clips([5], audio=[False])
    refused = await fa.execute(fa.FootageAnalyzeArgs(asset_ids=ids[:1]), ToolContext(
        session_id=session.id, user_id=stranger["id"], workspace_id=stranger["default_workspace_id"]))
    assert refused.metadata.get("error") is True


async def test_a_footage_cut_settles_on_the_ladder():
    from types import SimpleNamespace

    from billing.media import settle_compose
    user, session, ids = await _owner_with_clips([20], audio=[True])
    asset = SimpleNamespace(workspace_id=user["default_workspace_id"])
    job = SimpleNamespace(id="video_" + uuid4().hex[:12], session_id=session.id, user_id=user["id"])
    credits = await settle_compose(job, asset, width=720, height=1280, duration_sec=58.2)
    async with get_db_session() as db:
        row = (await db.scalars(select(UsageEvent).where(UsageEvent.idempotency_key == f"compose:{job.id}"))).one()
    assert row.credits == Decimal("5") and row.model_id == "footage-compose" and row.session_title == "成片"
    assert credits in (Decimal("5"), None)  # None outside enforce/shadow


def test_footage_skill_and_entry_ship():
    from pathlib import Path

    from light.entries import light_entries
    entry = light_entries().entry("footage_edit")
    assert entry.kind == "primary" and entry.skill == "footage-edit" and not entry.requires.desktop
    assert [step.id for step in entry.steps] == ["type", "upload", "plan", "compose", "deliver"]
    root = Path(__file__).resolve().parents[2] / ".openbox" / "skills" / "footage-edit"
    assert (root / "SKILL.md").is_file() and (root / "references" / "structure-templates.md").is_file()


@pytest.mark.parametrize("duration,expected", [(8, 4), (15, 5), (60, 5)])
def test_frame_times_spread_over_the_clip(duration, expected):
    from tool.footage_analyze import _frame_times
    times = _frame_times(duration)
    assert len(times) == expected and 0 < times[0] < times[-1] < duration


async def test_a_clip_read_on_upload_is_not_read_again(monkeypatch):
    """Clips on a 你拍我剪 page are read as they land; the tool then only collects."""
    from tool import footage_analyze as fa
    from tool import video_production as vp
    from tool.tool import ToolContext

    user, session, ids = await _owner_with_clips([15, 15], audio=[True, False])
    calls = {"asr": 0, "vision": 0}

    async def fake_asr(target, url):
        calls["asr"] += 1
        return {"text": "欢迎光临", "sentences": [{"from_sec": 0, "to_sec": 1.5, "text": "欢迎光临"}]}

    async def fake_vision(frames, *, duration, orientation, speech):
        calls["vision"] += 1
        assert "转写" not in speech  # picture and speech are read side by side, not in sequence
        return {"subject": "门头", "segments": [{"from_sec": 0, "to_sec": 3, "shot": "门头", "usable": True}]}

    class FakeOss:
        host = "bossip.oss-cn-shanghai.aliyuncs.com"

        def presign_get(self, key, expires_sec=3600, **_):
            return f"https://{self.host}/{key}?sig"

        def _presign(self, method, key, expires, subresource=None):
            return f"https://{self.host}/{key}?x-oss-process={subresource['x-oss-process']}"

    monkeypatch.setattr("core.oss.get_oss", lambda: FakeOss())
    monkeypatch.setattr(vp, "_configured_transcription_target", lambda: object())
    monkeypatch.setattr(vp, "_provider_transcribe", fake_asr)
    monkeypatch.setattr(fa, "_vision", fake_vision)

    for asset_id in ids:
        fa.prepare_in_background(asset_id, user["id"])
    result = await fa.execute(fa.FootageAnalyzeArgs(asset_ids=ids), ToolContext(
        session_id=session.id, user_id=user["id"], workspace_id=user["default_workspace_id"]))
    clips = result.metadata["clips"]
    assert [c["subject"] for c in clips] == ["门头", "门头"] and clips[0]["transcript"] == "欢迎光临"
    assert calls == {"asr": 1, "vision": 2}  # once per clip, on upload; the tool reused it
    async with get_db_session() as db:
        stored = await db.get(FileAsset, ids[0])
    assert stored.meta["reading"]["v"] == fa.READING_VERSION
    again = await fa.execute(fa.FootageAnalyzeArgs(asset_ids=ids), ToolContext(
        session_id=session.id, user_id=user["id"], workspace_id=user["default_workspace_id"]))
    assert calls == {"asr": 1, "vision": 2} and again.metadata["clips"][1]["segments"]


def test_cutting_words_and_clips_open_the_video_tools():
    from agent.tool_exposure import ExposureSignals, route_explicit_intent_packs
    assert "video" in route_explicit_intent_packs(ExposureSignals(user_task_text="素材传好了，帮我剪"))
    assert "video" in route_explicit_intent_packs(ExposureSignals(user_task_text="就这么剪"))
    assert "video" in route_explicit_intent_packs(ExposureSignals(user_task_text="看看", attachment_kinds=("video",)))
    assert "video" not in route_explicit_intent_packs(ExposureSignals(user_task_text="明天天气"))


def test_picture_replies_are_read_despite_fences_prose_and_trailing_commas():
    from tool.footage_analyze import parse_picture
    reply = '好的，下面是结果：\n```json\n{"subject": "门头", "segments": [{"from_sec": 0, "to_sec": 3,},],}\n```'
    assert parse_picture(reply) == {"subject": "门头", "segments": [{"from_sec": 0, "to_sec": 3}]}
    with pytest.raises(ValueError):
        parse_picture("没有结果")
    with pytest.raises(ValueError):
        parse_picture('{"subject": "门头", "segments": [')


async def test_a_hung_picture_reading_is_asked_again_and_the_first_answer_wins():
    import asyncio

    from tool.footage_analyze import FootageRefusal, first_answer

    calls = []

    async def hangs_then_answers():
        calls.append(len(calls))
        if len(calls) == 1:
            await asyncio.sleep(30)
        return {"subject": f"try {len(calls)}"}

    assert await asyncio.wait_for(first_answer(hangs_then_answers, hedge_after=0.05), 2) == {"subject": "try 2"}

    answers = iter([ValueError("no JSON object in the reply"), {"subject": "门头"}])

    async def bad_then_good():
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    assert await first_answer(bad_then_good, hedge_after=10) == {"subject": "门头"}

    async def always_bad():
        raise ValueError("no JSON object in the reply")

    with pytest.raises(FootageRefusal):
        await first_answer(always_bad, hedge_after=10)


async def test_a_finished_footage_cut_is_delivered_without_an_agent_turn():
    from video import compose_recovery as cr
    user, session, ids = await _owner_with_clips([15], audio=[False])
    from session.session import create_user_message
    await create_user_message(session_id=session.id, text="就这么剪", agent="build", model=None, user_id=user["id"])
    now = datetime.now(timezone.utc)
    out = "asset_" + uuid4().hex[:20]
    async with get_db_session() as db:
        db.add(FileAsset(id=out, user_id=user["id"], workspace_id=user["default_workspace_id"], session_id=session.id,
                         name="cut.mp4", oss_key=f"assets/{user['id']}/cut.mp4", mime="video/mp4", size=1,
                         status="ready", created_at=now))
        from db.models.video_job import VideoJob
        job = VideoJob(id="video_" + uuid4().hex[:20], user_id=user["id"], session_id=session.id, kind="compose",
                       idempotency_key="cut:1", request_hash="h", status="completed", model="ims", output_asset_id=out,
                       request_data={}, result_data={}, attempt=1, created_at=now, updated_at=now, completed_at=now)
        db.add(job)
    async with get_db_session() as db:
        job = await db.get(VideoJob, job.id)
    assert await cr.deliver(job) is True
    async with get_db_session() as db:
        fresh = await db.get(VideoJob, job.id)
        messages = (await db.scalars(select(Message).where(Message.session_id == session.id,
                                                           Message.role == "assistant"))).all()
    assert fresh.attached_message_id and fresh.attached_message_id in {m.id for m in messages}


async def test_a_watched_cut_wakes_its_chat_once_it_settles(monkeypatch):
    from types import SimpleNamespace

    from video import compose_recovery as cr

    states = iter(["in_progress", "completed"])
    woken = []

    class _Db:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, model, job_id):
            return SimpleNamespace(id=job_id, status="in_progress", session_id="s1", attached_message_id=None,
                                   idempotency_key="k")

    async def fake_poll(job):
        return SimpleNamespace(id=job.id, status=next(states), session_id="s1", attached_message_id=None,
                               idempotency_key="k")

    async def not_busy(session_id):
        return False

    async def fake_wake(job):
        woken.append(job.id)
        return True

    monkeypatch.setattr(cr, "WATCH_INTERVAL_SECONDS", 0)
    monkeypatch.setattr("db.base.get_db_session", lambda: _Db())
    monkeypatch.setattr("tool.video_compose.poll_compose_job", fake_poll)
    monkeypatch.setattr(cr, "_session_busy", not_busy)
    monkeypatch.setattr(cr, "wake_for", fake_wake)
    await cr._watch("video_w1")
    assert woken == ["video_w1"]
