"""Mobile onboarding progress rides in user_preferences.extra["onboarding"]."""
from db.repository.preference_repo import PgPreferenceRepo


async def test_onboarding_is_merged_and_exposed_top_level():
    repo = PgPreferenceRepo()
    first = await repo.upsert("u1", onboarding={"welcome": True})
    assert first["onboarding"] == {"welcome": True}
    # A later mark replaces the map the client owns (it always sends the full set).
    second = await repo.upsert("u1", onboarding={"welcome": True, "drawer": True, "industry": "beauty"})
    assert second["onboarding"] == {"welcome": True, "drawer": True, "industry": "beauty"}
    assert (await repo.get("u1"))["onboarding"]["industry"] == "beauty"


async def test_extra_writes_from_other_clients_keep_onboarding():
    repo = PgPreferenceRepo()
    await repo.upsert("u2", onboarding={"welcome": True})
    # Web appearance sync writes its own keys only.
    row = await repo.upsert("u2", theme="dark", extra={"mode": "dark", "fontSize": "md"})
    assert row["extra"]["onboarding"] == {"welcome": True}
    assert row["extra"]["mode"] == "dark" and row["theme"] == "dark"
    # Browser mode preference is not wiped by the appearance write either.
    await repo.upsert("u2", extra={"browser_mode": "auto"})
    latest = await repo.get("u2")
    assert latest["extra"]["browser_mode"] == "auto" and latest["extra"]["mode"] == "dark"


async def test_empty_onboarding_clears_for_replay():
    repo = PgPreferenceRepo()
    await repo.upsert("u3", onboarding={"welcome": True, "drawer": True})
    cleared = await repo.upsert("u3", onboarding={})
    assert cleared["onboarding"] == {}
    assert (await repo.get("u3"))["onboarding"] == {}


async def test_missing_row_reads_as_no_progress():
    assert await PgPreferenceRepo().get("nobody") is None
