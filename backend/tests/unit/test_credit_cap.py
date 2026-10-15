"""``max_credits`` on a /v1 prompt: stored on the session, enforced before paid media steps, visible on the session view."""
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from billing import credit_cap
from core.identifier import ascending
from db.base import get_db_session
from db.models.billing import UsageEvent
from db.models.session import Session as SessionRow
from db.models.video_job import VideoJob
from tests.unit.test_v1_routes import _session, client, default_config, key_identity, seed_scope  # noqa: F401


def test_parse_max_credits():
    assert credit_cap.parse_max_credits(None) is None
    assert credit_cap.parse_max_credits("30") == Decimal("30")
    assert credit_cap.parse_max_credits("0.5") == Decimal("0.5")
    for bad in ("0", "-1", "abc", "1.1234567", "1e9", True):
        with pytest.raises(ValueError):
            credit_cap.parse_max_credits(bad)


async def _usage(session_id: str, wid: str, credits: str, kind: str = "video_generate", status: str = "charged"):
    async with get_db_session() as db:
        db.add(UsageEvent(id=ascending("usage"), idempotency_key=uuid4().hex, workspace_id=wid, user_id="u",
                          session_id=session_id, message_id=None, session_title="t", model_id="m", kind=kind, tokens={},
                          total_tokens=0, credits=Decimal(credits), status=status, pricing={},
                          created_at=datetime.now(timezone.utc).replace(tzinfo=None)))


async def _job(session_id: str, uid: str, credits: str | None, *, kind: str = "segment", status: str = "submitting",
               result_data: dict | None = None) -> str:
    """A generation/compose job row as the submit path writes it: quoted, not yet settled."""
    now = datetime.now(timezone.utc)
    request_data = {"billing_quote": {"model_id": "video-gen:x:720p", "tier": "720p", "quantity": 5,
                                      "credits": credits, "snapshot": {}}} if credits is not None else {}
    async with get_db_session() as db:
        job = VideoJob(id=ascending("video"), user_id=uid, session_id=session_id, kind=kind, idempotency_key=uuid4().hex,
                       request_hash="h", status=status, model="m", request_data=request_data, result_data=result_data or {},
                       attempt=0, created_at=now, updated_at=now)
        db.add(job)
        return job.id


async def _capped_session(uid: str, wid: str, cap: str):
    from session import session as session_mod

    created = await session_mod.create_session(model="openai/gemini-3.8-flash", agent="build", title="cap", user_id=uid, workspace_id=wid)
    await credit_cap.set_cap(created.id, Decimal(cap))
    return created.id


async def test_unsettled_jobs_count_against_the_cap_before_they_are_settled(default_config):
    """Cap 60: a 55.5 shot whose job row exists but is not settled yet must block a 44.4 shot."""
    uid, wid = await seed_scope()
    sid = await _capped_session(uid, wid, "60")
    await credit_cap.guard(sid, step="generation", credits=Decimal("55.5"))
    first = await _job(sid, uid, "55.5")
    with pytest.raises(credit_cap.CreditCapExceeded) as excinfo:
        await credit_cap.guard(sid, step="generation", credits=Decimal("44.4"))
    assert excinfo.value.spent == Decimal("55.5") and excinfo.value.expected == Decimal("44.4")
    async with get_db_session() as db:
        row = await db.get(SessionRow, sid)
    view = await credit_cap.status(row)
    assert view["spent"] == "55.5" and view["settled"] == "0" and view["in_flight"] == "55.5" and view["remaining"] == "4.5"
    assert view["exceeded"]["step"] == "generation" and view["exceeded"]["spent"] == "55.5"
    lines = await credit_cap.estimate_lines(sid)
    assert "credit_cap_spent=55.5" in lines and "credit_cap_in_flight=55.5" in lines and "credit_cap_remaining=4.5" in lines
    # A compose job in flight counts the same way, and its own row is left out when it is the step being checked.
    render = await _job(sid, uid, None, kind="render", status="dispatching")
    await credit_cap.guard(sid, step="compose", credits=Decimal("4.5"), exclude_job_id=render)
    with pytest.raises(credit_cap.CreditCapExceeded):
        await credit_cap.guard(sid, step="compose", credits=Decimal("4.6"), exclude_job_id=render)
    # Once the shot settles, the usage row replaces the quote: no double counting.
    async with get_db_session() as db:
        db.add(UsageEvent(id=ascending("usage"), idempotency_key=f"generate:{first}", workspace_id=wid, user_id=uid,
                          session_id=sid, message_id=None, session_title="t", model_id="m", kind="video_generate", tokens={},
                          total_tokens=0, credits=Decimal("55.5"), status="charged", pricing={},
                          created_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        job = await db.get(VideoJob, first)
        job.status = "completed"
    async with get_db_session() as db:
        row = await db.get(SessionRow, sid)
    view = await credit_cap.status(row)
    assert view["spent"] == "55.5" and view["settled"] == "55.5" and view["in_flight"] == "0"


async def test_failed_jobs_and_free_reuse_do_not_count(default_config):
    uid, wid = await seed_scope()
    sid = await _capped_session(uid, wid, "60")
    await _job(sid, uid, "55.5", status="failed")
    await _job(sid, uid, "55.5", status="cancelled")
    await _job(sid, uid, "55.5", kind="render", status="failed")
    await _job(sid, uid, "55.5", status="transfer_failed")
    await _job(sid, uid, "55.5", status="completed", result_data={"reuse": True, "reused_from_job": "video_x"})
    await _job(sid, uid, "1", kind="stt", status="dispatching")                      # transcription is not a cap step here
    await credit_cap.guard(sid, step="generation", credits=Decimal("44.4"))         # nothing above counts
    async with get_db_session() as db:
        row = await db.get(SessionRow, sid)
    assert (await credit_cap.status(row))["spent"] == "0"
    # A job without a saved quote is counted as 0, not refused.
    await _job(sid, uid, None)
    await credit_cap.guard(sid, step="generation", credits=Decimal("60"))
    # Jobs from before the cap was (re)set belong to an earlier request.
    await _job(sid, uid, "59")
    await credit_cap.set_cap(sid, Decimal("60"))
    await credit_cap.guard(sid, step="generation", credits=Decimal("60"))


async def test_reserve_serialises_concurrent_submits_of_one_session(default_config):
    """Two shots of 35 submitted at once against a cap of 60: exactly one is admitted."""
    import asyncio

    uid, wid = await seed_scope()
    sid = await _capped_session(uid, wid, "60")
    outcomes: list[str] = []

    async def submit(label: str):
        try:
            async with credit_cap.reserve(sid, step="generation", credits=Decimal("35")):
                await asyncio.sleep(0.01)                                           # the window before the job row lands
                await _job(sid, uid, "35")
            outcomes.append(f"{label}:ok")
        except credit_cap.CreditCapExceeded:
            outcomes.append(f"{label}:refused")

    await asyncio.gather(submit("a"), submit("b"))
    assert sorted(o.split(":")[1] for o in outcomes) == ["ok", "refused"]
    assert not credit_cap._locks                                                    # idle locks are dropped
    # An idempotent re-submit whose row already exists is not charged against the cap again.
    async with credit_cap.reserve(sid, step="generation", credits=Decimal("35"), already_admitted=True):
        pass
    with pytest.raises(credit_cap.CreditCapExceeded):
        async with credit_cap.reserve(sid, step="generation", credits=Decimal("35")):
            pass
    async with credit_cap.reserve(None, step="generation", credits=Decimal("999")):   # no session: no cap
        pass


async def test_guard_refuses_past_the_cap_and_records_the_hit(default_config):
    from session import session as session_mod

    uid, wid = await seed_scope()
    created = await session_mod.create_session(model="openai/gemini-3.8-flash", agent="build", title="cap", user_id=uid, workspace_id=wid)
    await credit_cap.guard(created.id, step="generation", credits=Decimal("999"))   # no cap yet: nothing happens
    await credit_cap.set_cap(created.id, Decimal("10"))
    await credit_cap.guard(created.id, step="generation", credits=Decimal("8"))
    await _usage(created.id, wid, "8")
    await _usage(created.id, wid, "5", kind="chat")                                  # text calls do not count
    await credit_cap.guard(created.id, step="generation", credits=Decimal("2"))     # 8 + 2 = 10 still fits
    with pytest.raises(credit_cap.CreditCapExceeded) as excinfo:
        await credit_cap.guard(created.id, step="compose", credits=Decimal("3"), note="61s 720p")
    assert excinfo.value.code == "CREDIT_CAP_EXCEEDED" and "Nothing was submitted" in str(excinfo.value)
    async with get_db_session() as db:
        row = await db.get(SessionRow, created.id)
    view = await credit_cap.status(row)
    assert view["max_credits"] == "10" and view["spent"] == "8" and view["remaining"] == "2"
    assert view["exceeded"]["step"] == "compose" and view["exceeded"]["expected"] == "3"
    lines = await credit_cap.estimate_lines(created.id)
    assert "credit_cap_remaining=2" in lines
    await credit_cap.set_cap(created.id, None)
    async with get_db_session() as db:
        row = await db.get(SessionRow, created.id)
    assert row.credit_cap is None and await credit_cap.status(row) is None


async def test_prompt_sets_the_cap_and_the_session_view_shows_it(default_config, monkeypatch):
    uid, wid = await seed_scope()
    identity = key_identity(uid, wid)
    user_message_id = ascending("message")

    async def accept(**kwargs):
        return SimpleNamespace(id="inbox_1", state="accepted", created=True, message_id=None)

    async def wake(session_id, user_id):
        return "run_1"

    async def get_item(item_id, *, user_id, session_id=None):
        return SimpleNamespace(id=item_id, state="claimed", message_id=user_message_id)

    monkeypatch.setattr("agent.inbox.accept_inbox_item", accept)
    monkeypatch.setattr("agent.inbox.wake_inbox_session", wake)
    monkeypatch.setattr("agent.inbox.get_inbox_item", get_item)
    async with client(identity) as http:
        session_id = await _session(http)
        bad = await http.post(f"/sessions/{session_id}/messages", json={"text": "hi", "max_credits": "-5"})
        assert bad.status_code == 400 and bad.json()["error"]["code"] == "INVALID_REQUEST"
        sent = await http.post(f"/sessions/{session_id}/messages", json={"text": "做一条 30 秒口播", "max_credits": "120"})
        assert sent.status_code == 202, sent.text
        shown = await http.get(f"/sessions/{session_id}")
        cap = shown.json()["credit_cap"]
        assert cap["max_credits"] == "120" and cap["spent"] == "0" and cap["remaining"] == "120" and cap["exceeded"] is None
        from api.v1.ids import internal_id
        await _usage(internal_id(session_id, "session"), wid, "45.5")
        shown = await http.get(f"/sessions/{session_id}")
        assert shown.json()["credit_cap"]["remaining"] == "74.5"
        # A later prompt without a cap clears it.
        again = await http.post(f"/sessions/{session_id}/messages", json={"text": "第二段轻快一点"})
        assert again.status_code == 202
        assert (await http.get(f"/sessions/{session_id}")).json()["credit_cap"] is None
