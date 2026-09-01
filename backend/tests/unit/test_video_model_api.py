"""The video-model picker's backend surface: what it offers, what it records."""
import pytest

from core.config import ProviderConfig, VideoModelConfig, get_config


def _declare(monkeypatch, models, allowed=None, *, channel="task"):
    config = get_config()
    monkeypatch.setattr(config.video_generation, "models",
                        [VideoModelConfig(**m) for m in models])
    monkeypatch.setattr(config.video_generation, "allowed_models", allowed or [])
    monkeypatch.setattr(config.video_generation, "channel_providers", {channel: "video-test"})
    monkeypatch.setitem(
        config.provider,
        "video-test",
        ProviderConfig(api_key="test-only", base_url="https://video.test"),
    )
    return config


def test_picker_lists_declared_models(monkeypatch):
    from api.metadata import _video_models

    config = _declare(monkeypatch, [
        {"id": "wan3.0-video", "name": "Wan 3.0", "channel": "task", "tier": "标准"},
        {"id": "wan3.0-video-prime", "channel": "task", "tier": "高级"},
    ])
    rows = _video_models(config)
    assert [r["id"] for r in rows] == ["wan3.0-video", "wan3.0-video-prime"]
    assert rows[0]["name"] == "Wan 3.0"
    # Falls back to the id so a row is never nameless in the menu.
    assert rows[1]["name"] == "wan3.0-video-prime"
    assert rows[1]["tier"] == "高级"


def test_picker_never_offers_what_the_submit_path_would_refuse(monkeypatch):
    """allowed_models governs the menu too, or the UI advertises a dead option."""
    from api.metadata import _video_models

    config = _declare(
        monkeypatch,
        [{"id": "cheap", "channel": "task"}, {"id": "pricey", "channel": "task"}],
        allowed=["cheap"],
    )
    assert [r["id"] for r in _video_models(config)] == ["cheap"]


def test_picker_hides_a_declared_model_without_a_bound_channel_provider(monkeypatch):
    from api.metadata import _video_models

    config = _declare(
        monkeypatch,
        [{"id": "wan3.0-video", "channel": "task"}],
    )
    monkeypatch.setattr(config.video_generation, "channel_providers", {})

    assert _video_models(config) == []


def test_undeclared_deployment_still_shows_its_one_real_model(monkeypatch):
    """An empty menu would read as "video is broken"; it isn't."""
    from api.metadata import _video_models

    config = _declare(monkeypatch, [])
    monkeypatch.setattr(config.video_generation, "model", "doubao-seedance-2-0-260128")
    rows = _video_models(config)
    assert [r["id"] for r in rows] == ["doubao-seedance-2-0-260128"]


@pytest.mark.asyncio
async def test_the_pick_is_recorded_and_clearable():
    """Recorded verbatim: substituting a video model would spend on the wrong one."""
    from datetime import datetime, timezone
    from uuid import uuid4

    from api.sessions import _remember_video_model
    from db.base import get_db_session
    from db.models.session import Session as SessionORM
    from db.models.user import User

    suffix = uuid4().hex[:10]
    user_id, session_id = f"user_{suffix}", f"session_{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=user_id, username=f"vma-{suffix}", created_at=now, updated_at=now))
        db.add(SessionORM(id=session_id, user_id=user_id, project_id="default",
                          created_at=now, updated_at=now))

    async def stored():
        async with get_db_session() as db:
            return (await db.get(SessionORM, session_id)).video_model

    session = type("S", (), {"id": session_id, "video_model": None})()
    await _remember_video_model(session, "wan3.0-video", user_id)
    assert await stored() == "wan3.0-video"

    # None means "the composer said nothing", not "clear it".
    session.video_model = "wan3.0-video"
    await _remember_video_model(session, None, user_id)
    assert await stored() == "wan3.0-video"

    # An empty string is an explicit reset to the deployment default.
    await _remember_video_model(session, "", user_id)
    assert await stored() is None
