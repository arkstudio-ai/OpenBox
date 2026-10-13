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
