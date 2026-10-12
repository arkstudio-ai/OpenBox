"""v14: tenant-scoped facts and stable semantic choices, not transcript matching."""
from datetime import datetime, timezone
import json

import pytest

from assistant.status_tools import platforms
from assistant.policy import AssistantError
from assistant.service import ensure_main_session
from db.base import get_db_session
from db.models.platform_account import PlatformAccount
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from voice import calls, phrases
from voice.meter import CallMeter


async def test_first_introduction_ignores_failed_handshake_and_other_users():
    owner, other, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    call = await calls.create_call(user_id=owner, workspace_id=workspace, main_session_id=main.id,
                                  client="web", model="m", voice="v")
    assert await calls.first_call(owner)
    await calls.finish_call(call, status="failed", end_reason="error", duration_seconds=0, turns=0,
                            snapshot=CallMeter().snapshot())
    assert await calls.first_call(owner)
    await calls.finish_call(call, status="ended", end_reason="hangup", duration_seconds=20, turns=1,
                            snapshot=CallMeter().snapshot())
    assert not await calls.first_call(owner)
    assert await calls.first_call(other)
    assert "完整的标准介绍" in phrases.greeting_instructions("zh", first_call=True)
    assert "完整的标准介绍" not in phrases.greeting_instructions("zh")


async def test_authorization_snapshot_is_scoped_redacted_and_not_a_live_probe(monkeypatch):
    owner, _, workspace = await accounts()
    foreign, _, foreign_workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model="test/model")
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        for key, user, space, nickname, deleted in (("own", owner, workspace, "本店", None),
                ("foreign", foreign, foreign_workspace, "别人的店", None),
                ("deleted", owner, workspace, "已解绑", now)):
            db.add(PlatformAccount(id=key, workspace_id=space, bound_by_user_id=user,
                platform="douyin_creator", auth_kind="desktop_cookie", external_id=key,
                nickname=nickname, status="bound", access_token_ciphertext="NEVER_RETURN_THIS",
                refresh_token_ciphertext="SECRET", scopes="", bound_at=now, created_at=now,
                updated_at=now, deleted_at=deleted))
    async def remote(_): return "remote"
    monkeypatch.setattr("session.browser_pref.get_browser_mode", remote)
    value = await platforms(user_id=owner, workspace_id=workspace, main_id=main.id)
    assert value["live_verified"] is False
    by_key = {row["platform"]: row for row in value["platforms"]}
    assert by_key["douyin_creator"]["accounts"][0]["applicable"] is False
    assert by_key["douyin_laike"]["accounts"] == []
    assert by_key["wechat_channels"]["login_detection"] is False
    raw = json.dumps(value, ensure_ascii=False)
    assert "本店" in raw
    assert all(secret not in raw for secret in ("别人的店", "已解绑", "NEVER_RETURN_THIS", "SECRET"))
    from assistant.status_tools import spoken_platforms
    spoken = spoken_platforms(value)
    assert len(spoken["historical_records"]) == 1 and spoken["historical_records"][0]["nickname"] == "本店"
    assert spoken["current_login_state"] == "unknown" and spoken["live_verified"] is False
    with pytest.raises(AssistantError):
        await platforms(user_id=foreign, workspace_id=workspace, main_id=main.id)


def test_business_tiers_use_real_presets_and_prices_and_hide_missing_tiers(monkeypatch):
    from assistant.model_choices import business_video_tiers, resolve_video_tier
    rows = [{"tier": "fast", "model": "m", "resolution": "480p", "resolutions": ["480p"],
             "prices": {"480p": "0.51"}, "description": "支持图片"}]
    monkeypatch.setattr("api.metadata._model_tiers", lambda _: {"video": rows})
    tiers = business_video_tiers(object(), [{"id": "m", "supports_reference_video": False}])
    assert len(tiers) == 1 and tiers[0]["credits_per_second"] == {"480p": "0.51"}
    assert tiers[0]["capabilities"]["supports_reference_video"] is False
    values = {"video_tier": "fast", "video_tier_confirmed": False}
    resolve_video_tier(values, {"video_tiers": tiers})
    assert "video_model" not in values
    with pytest.raises(AssistantError):
        resolve_video_tier({**values, "video_tier_confirmed": True, "video_resolution": "1080p"}, {"video_tiers": tiers})


async def test_platform_snapshot_is_exposed_through_the_authorized_assistant_tool():
    from tests.unit.test_assistant_sessions_v2 import assistant, main_turn, tool_call
    owner, _, workspace, main = await assistant()
    ctx, lease, _ = await main_turn(owner, workspace, main, "只查授权记录，不登录")
    try:
        value, _ = await tool_call(ctx, "status.platforms", {})
        assert value["status"] == "ok" and value["live_verified"] is False
        assert all(row["current_connection"] == "unverified" for row in value["platforms"])
        from assistant.status_tools import spoken_platforms
        spoken = spoken_platforms(value)
        assert "historical_records" not in spoken and spoken["status"] == "login_not_verified"
        assert spoken["current_login_state"] == "unknown"
    finally:
        await lease.release(session_status="idle")


async def test_failed_context_reads_do_not_invent_first_contact_or_logout(monkeypatch):
    from unittest.mock import AsyncMock
    from voice.prompt import front_context
    for name, value in (("voice.prompt.user_facts", {}), ("voice.recall.core_memories", ""),
                        ("voice.prompt.recent_summary", ""), ("voice.prompt.since_last_call", ("", ""))):
        monkeypatch.setattr(name, AsyncMock(return_value=value))
    monkeypatch.setattr("voice.calls.first_call", AsyncMock(side_effect=RuntimeError("database unavailable")))
    monkeypatch.setattr("assistant.status_tools.platforms", AsyncMock(side_effect=TimeoutError()))
    facts = await front_context(user_id="u", workspace_id="w", main_session_id="m")
    assert facts.first_call is False and facts.platforms is None
