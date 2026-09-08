"""The cloud desktop's Chrome is the default browser; auto/remote are opt-in."""
from unittest.mock import AsyncMock

from session import browser_pref


async def test_unset_preference_means_the_cloud_browser(monkeypatch):
    monkeypatch.setattr(browser_pref, "PgPreferenceRepo", lambda: type("R", (), {"get": AsyncMock(return_value=None)})())
    assert await browser_pref.get_browser_mode("u1") == "local"
    assert browser_pref.DEFAULT_MODE == "local" and browser_pref.MODES[0] == "local"
    assert browser_pref.relay_mode("local") == "local"


async def test_stored_auto_is_still_honoured(monkeypatch):
    repo = type("R", (), {"get": AsyncMock(return_value={"extra": {"browser_mode": "auto"}})})()
    monkeypatch.setattr(browser_pref, "PgPreferenceRepo", lambda: repo)
    assert await browser_pref.get_browser_mode("u1") == "auto"
