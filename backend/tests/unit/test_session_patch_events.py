from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from api.sessions import UpdateSessionBody, update_session as patch_session
from bus.events import SESSION_TITLE, SESSION_UPDATED


class _Saved(SimpleNamespace):
    """The record session_mod.update_session hands back."""

    def model_dump(self, mode=None):
        return dict(vars(self))


def _owned(monkeypatch, saved: _Saved) -> list:
    current = SimpleNamespace(id=saved.id, model=saved.model, variant=None)
    monkeypatch.setattr("api.sessions._require_session_owned", AsyncMock(return_value=current))
    monkeypatch.setattr("api.sessions.session_mod.update_session", AsyncMock(return_value=saved))
    published = []
    monkeypatch.setattr("bus.bus.publish", lambda event_type, data=None: published.append((event_type, data)))
    return published


def _record(**fields) -> _Saved:
    return _Saved(**{"id": "s1", "title": "Draft", "agent": "build", "model": "openai/gpt-5", "variant": None, **fields})


@pytest.mark.asyncio
async def test_a_rename_reaches_the_owners_other_devices(monkeypatch):
    published = _owned(monkeypatch, _record(title="Spring sale"))

    await patch_session("s1", UpdateSessionBody(title="Spring sale"), {"user_id": "u1"})

    assert published == [
        (SESSION_UPDATED, {"userId": "u1", "sessionId": "s1", "title": "Spring sale"}),
        (SESSION_TITLE, {"userId": "u1", "sessionId": "s1", "title": "Spring sale"}),
    ]


@pytest.mark.asyncio
async def test_a_model_pick_announces_only_what_changed(monkeypatch):
    published = _owned(monkeypatch, _record(model="openai/deepseek-v4-flash"))

    await patch_session("s1", UpdateSessionBody(model="openai/deepseek-v4-flash"), {"user_id": "u1"})

    assert published == [
        (SESSION_UPDATED, {"userId": "u1", "sessionId": "s1", "model": "openai/deepseek-v4-flash"}),
    ]


@pytest.mark.asyncio
async def test_an_empty_patch_announces_nothing(monkeypatch):
    published = _owned(monkeypatch, _record())

    await patch_session("s1", UpdateSessionBody(), {"user_id": "u1"})

    assert published == []
