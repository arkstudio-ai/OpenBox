from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from uuid import uuid4

import pytest

from light import production, production_form
from light.production_outputs import is_advanced_parent


def test_advanced_output_requires_current_owners_human_turn_not_guided_or_automatic():
    now = datetime.now(timezone.utc)
    owner = NS(id="session", user_id="owner")
    parent = NS(role="user", session_id="session", user_id="owner", created_at=now)
    text = {"origin": "human", "origin_ref": {"entrypoint": "chat"}}
    assert is_advanced_parent(parent, text, owner, now - timedelta(seconds=1))
    assert not is_advanced_parent(parent, text, owner, now + timedelta(seconds=1))
    assert not is_advanced_parent(parent, {"origin": "cron"}, owner, now)
    assert not is_advanced_parent(parent, {}, owner, now)
    assert not is_advanced_parent(parent, {"origin": "human", "origin_ref": {"entrypoint": "light_production"}}, owner, now)
    parent.user_id = "other"
    assert not is_advanced_parent(parent, text, owner, now)
    parent.user_id, parent.session_id = "owner", "another-session"
    assert not is_advanced_parent(parent, text, owner, now)


def test_advanced_final_is_visible_without_allowing_a_guided_end_card():
    now = datetime.now(timezone.utc)
    row = NS(light_entry="video", created_at=now - timedelta(seconds=10),
             light_production={**production_form.initial_form("video", "巷口", ""),
                               "template": "wan_story", "run": {"action": "render", "remake_version": "wan-story-v2"}})
    def file(message, asset, role="final"):
        return NS(message_id=message, created_at=now,
                  data={"asset_id": asset, "mime_type": "video/mp4", "relation": {"role": role}})
    files = [file("guided", "end-card"), file("advanced", "new-full-film"), file("advanced", "workprint", "intermediate")]
    assert production._output_candidates(row, [], files) == []
    assert production._output_candidates(row, [], files, advanced_messages={"advanced"}) == ["new-full-film"]


async def test_advanced_context_resolves_the_owned_completed_cloud_film():
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from db.models.video_job import VideoJob
    from db.repository.user_repo import PgUserRepo
    from light.mode import LightError
    from light.production_outputs import current_result_context
    from session.session import create_session

    owner = await PgUserRepo().create(id=uuid4().hex, username=f"ctx{uuid4().hex[:10]}", password_hash=None)
    session = await create_session(
        model="test/model", user_id=owner["id"], workspace_id=owner["default_workspace_id"],
        light_entry="footage_edit", light_production=production_form.initial_form("footage_edit", "巷口", ""),
    )
    assert await current_result_context(session) == ""
    now = datetime.now(timezone.utc)
    asset_id, job_id = uuid4().hex, uuid4().hex
    async with get_db_session() as db:
        db.add(FileAsset(id=asset_id, user_id=session.user_id, workspace_id=session.workspace_id,
                         session_id=session.id, name="full-film.mp4", mime="video/mp4", status="ready",
                         oss_key="owned/full-film.mp4", created_at=now))
        await db.flush()
        db.add(VideoJob(id=job_id, user_id=session.user_id, session_id=session.id, kind="compose",
                        idempotency_key=uuid4().hex, status="completed", output_asset_id=asset_id,
                        created_at=now, updated_at=now, completed_at=now,
                        request_data={"timeline": {"version": 1, "shots": [{"asset": "source", "duration_sec": 28.6}]}},
                        result_data={"duration_sec": 28.6}))
    context = await current_result_context(session)
    assert asset_id in context and job_id in context and "28.6" in context
    assert 'names=["video_compose"]' in context and "measured_edit_map" in context
    assert '"from_sec": 0.0, "to_sec": 28.6' in context
    assert "thumbnail" not in context and "https://" not in context and "oss_key" not in context
    assert "owned/full-film.mp4" not in context
    with pytest.raises(LightError):
        await current_result_context(NS(id=session.id, user_id="another-owner", workspace_id=session.workspace_id))


def test_edit_map_uses_playback_time_and_matches_descriptions_by_source():
    from light.production_outputs import edit_map
    form = {"remake_adaptation": {"shots": [{"kind": "detail", "action": "旋转特写"}]},
            "run": {"wan_cuts": [{"shot": 1, "source": "food"}]}}
    timeline = {"shots": [{"asset": "reaction", "duration_sec": 4, "speed": 2,
                           "transition_out": {"type": "fade", "seconds": 0.6}},
                          {"asset": "food", "duration_sec": 2.6}]}
    result = edit_map(timeline, form)
    assert result == [
        {"shot": 1, "from_sec": 0.0, "to_sec": 2.0},
        {"shot": 2, "from_sec": 1.4, "to_sec": 4.0, "kind": "detail", "action": "旋转特写"},
    ]
    assert edit_map({"shots": [{"asset": "open-ended"}]}, form) == []
