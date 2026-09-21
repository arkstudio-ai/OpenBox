from datetime import timedelta
from decimal import Decimal

import pytest

from core.identifier import ascending
from db.base import get_db_session
from db.models.billing import UsageEvent
from db.models.session import Session
from db.models.team import TeamRun
from team import history
from team.errors import TeamError
from team.journal import utcnow
from tests.unit.test_team_commands import setup_team


@pytest.mark.parametrize("attention_source", ["paused", "unreported", "pending"])
async def test_run_history_sorts_attention_before_recency_and_pages_without_replay(monkeypatch, attention_source):
    run_id, actor, _, root, _ = await setup_team()
    now = utcnow()
    async with get_db_session() as db:
        original = await db.get(TeamRun, run_id)
        original.state, original.pause_reason = "paused", "user"
        original.summary = {"needs_attention": True, "team_configuration": {"private": "not a list summary"}}
        if attention_source != "paused":
            original.state, original.pause_reason, original.ended_at = "canceled", None, now
            original.session_active, original.project_active = None, None
            original.summary = {"needs_attention": False}
            identifier = ascending("usage")
            db.add(UsageEvent(id=identifier, idempotency_key=identifier, workspace_id=actor.workspace_id,
                user_id=actor.owner_user_id, session_id=root, session_title="Canceled but cost unknown",
                model_id="openai/test", kind="chat", tokens={}, total_tokens=0, credits=None,
                status=attention_source, pricing={}, created_at=now))
        for index in range(3):
            sid, rid = ascending("session"), ascending("team")
            db.add(Session(id=sid, user_id=actor.owner_user_id, workspace_id=actor.workspace_id,
                project_id=original.project_id, title="Later", agent="build", status="idle", created_at=now, updated_at=now))
            await db.flush()
            db.add(TeamRun(id=rid, root_session_id=sid, owner_user_id=actor.owner_user_id, workspace_id=actor.workspace_id,
                project_id=original.project_id, title=f"Later {index}", goal="Goal", state="completed", revision=1,
                policy_snapshot={}, grant_snapshot={}, created_at=now, updated_at=now, ended_at=now))
    def forbidden(*args, **kwargs):
        raise AssertionError("Run lists must not replay events")
    monkeypatch.setattr("team.journal.read_state", forbidden)
    first = await history.list_runs(actor, limit=2)
    assert first["items"][0]["id"] == run_id
    assert first["items"][0]["summary"]["pause_reason"] == ("user" if attention_source == "paused" else None)
    assert first["items"][0]["summary"]["needs_attention"]
    assert "team_configuration" not in first["items"][0]["summary"]
    second = await history.list_runs(actor, cursor=first["next_cursor"], limit=2)
    assert len({item["id"] for item in first["items"] + second["items"]}) == 4
    assert second["next_cursor"] is None
    with pytest.raises(TeamError):
        await history.list_runs(actor, cursor="invalid")


async def test_run_usage_matches_ledger_member_time_scope_and_marks_unknown():
    run_id, actor, _, root, members = await setup_team()
    now = utcnow()
    async with get_db_session() as db:
        run = await db.get(TeamRun, run_id)
        start = now - timedelta(minutes=1)
        run.created_at, run.ended_at = start, now
        run.state, run.session_active, run.project_active = "completed", None, None
        for sid, offset, credits, status in [(root, 1, "1.25", "shadow"), (members[0], 2, "2.50", "charged"),
            (members[1], 3, None, "unpriced"), (members[1], 4, None, "pending"),
            (root, -10, "90", "charged"), (root, 120, "80", "charged")]:
            identifier = ascending("usage")
            db.add(UsageEvent(id=identifier, idempotency_key=identifier, workspace_id=actor.workspace_id,
                user_id=actor.owner_user_id, session_id=sid, session_title="Team usage",
                model_id="openai/test", kind="chat", tokens={}, total_tokens=10,
                credits=Decimal(credits) if credits else None, status=status, pricing={}, created_at=start + timedelta(seconds=offset)))
    usage = await history.usage(run_id, actor)
    assert Decimal(usage["credits"]) == Decimal("3.75")
    assert usage["tokens"] == 40 and usage["calls"] == 4
    assert usage["unpriced"] == 1 and usage["pending"] == 1
    listed = (await history.list_runs(actor))["items"][0]["usage"]
    assert listed == {key: value for key, value in usage.items() if key != "items"}
    from dataclasses import replace
    assert (await history.list_runs(replace(actor, owner_user_id="other")))["items"] == []
    with pytest.raises(TeamError):
        await history.usage(run_id, replace(actor, owner_user_id="other"))


async def test_cost_categories_partition_ledger_and_do_not_guess_missing_metadata():
    run_id, actor, _, root, members = await setup_team()
    async with get_db_session() as db:
        for index, (sid, category, credits) in enumerate([
            (root, "coordinator", "1.25"), (members[0], "member_work", "2.5"),
            (members[0], "rework", "0.75"), (members[1], "rework", None),
            (members[1], None, "0.1"), (root, "foreign-run", "0.2")]):
            identifier = ascending("usage")
            metadata = {"run_id": run_id if category != "foreign-run" else "other", "member_id": sid,
                "category": category if category != "foreign-run" else "coordinator"}
            db.add(UsageEvent(id=identifier, idempotency_key=identifier, workspace_id=actor.workspace_id,
                user_id=actor.owner_user_id, session_id=sid, session_title="Attribution fixture",
                model_id="openai/test", kind="chat" if index != 2 else "image_gen", tokens={}, total_tokens=10,
                credits=Decimal(credits) if credits else None, status="shadow" if credits else "unreported",
                pricing={"team_attribution": metadata} if category else {}, created_at=utcnow()))
    usage = await history.usage(run_id, actor)
    categories = {item["category"]: item for item in usage["categories"]}
    assert Decimal(categories["coordinator"]["credits"]) == Decimal("1.25")
    assert Decimal(categories["member_work"]["credits"]) == Decimal("2.5")
    assert Decimal(categories["rework"]["credits"]) == Decimal("0.75")
    assert categories["rework"]["unpriced"] == 1
    assert categories["unattributed"]["calls"] == 2
    assert sum(Decimal(item["credits"]) for item in categories.values()) == Decimal(usage["credits"])
    assert sum(item["calls"] for item in categories.values()) == usage["calls"] == 6
    assert (await history.list_runs(actor))["items"][0]["usage"]["categories"] == usage["categories"]
