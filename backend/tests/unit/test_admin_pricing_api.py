"""The operator pricing table and its audited, idempotent writes."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from api.admin_pricing import router
from auth.middleware import get_current_user
from billing import rules
from billing.pricing import catalogue
from db.base import get_db_session
from db.models.audit_log import AuditLog
from db.models.billing import PricingRule, UsageEvent
from db.repository.user_repo import PgUserRepo


def _client(identity: dict) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: dict(identity)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def isolated_rules():
    rules.clear()
    yield
    rules.clear()


@pytest.fixture
async def admin():
    suffix = uuid.uuid4().hex[:10]
    user = await PgUserRepo().create(id=f"aprice-{suffix}", username=f"aprice-{suffix}", password_hash="unused", role="admin")
    return {"user_id": user["id"], "role": "admin"}


async def _usage(workspace_id, *, model_id, kind, credits, cost, status="charged"):
    async with get_db_session() as db:
        db.add(UsageEvent(id="usage_" + uuid.uuid4().hex[:12], idempotency_key=uuid.uuid4().hex, workspace_id=workspace_id,
                          user_id="u", session_id="s", message_id=None, session_title="t", model_id=model_id, kind=kind,
                          tokens={}, total_tokens=0, credits=Decimal(credits), cost_credits=None if cost is None else Decimal(cost),
                          status=status, pricing={}, created_at=datetime.now(timezone.utc)))


async def test_non_admins_are_refused():
    async with _client({"user_id": "u1", "role": "user"}) as client:
        assert (await client.get("/api/admin/pricing")).status_code == 403
        assert (await client.put("/api/admin/pricing/llm:x", json={})).status_code == 403


async def test_table_lists_every_item_with_cost_sale_margin_and_flags(admin):
    wid = "w_" + uuid.uuid4().hex[:8]
    await _usage(wid, model_id="video-gen:MiniMax-H3:768p", kind="video_generate", credits="5", cost="0.9")
    await _usage(wid, model_id="video-gen:MiniMax-H3:768p", kind="video_generate", credits="5", cost="0.9", status="shadow")
    await _usage(wid, model_id="openai/gemini-3.8-flash", kind="chat", credits="1", cost=None)
    async with _client(admin) as client:
        body = (await client.get("/api/admin/pricing")).json()
    rows = {row["key"]: row for row in body["items"]}
    h3 = rows["video-gen:MiniMax-H3:768p"]
    assert h3["sale"] == {"per_second": "0.50"} and h3["cost"]["per_second"] == "0.09" and h3["cost"]["basis"] == "metaso"
    assert h3["margin_pct"]["per_second"] == "455.6" and "below_cost" not in h3["flags"]
    assert h3["usage_30d"]["charged_events"] >= 1 and h3["usage_30d"]["credits"] == "5" and h3["usage_30d"]["cost_credits"] == "0.9"
    assert h3["usage_30d"]["shadow_credits"] == "5"
    gemini = rows["llm:gemini-3.8-flash"]
    assert gemini["alias_of"] == "gemini-3.7-flash" and "alias" in gemini["flags"]
    assert "below_cost" in gemini["flags"] and set(gemini["below_cost_fields"]) >= {"input", "output"}
    assert gemini["cost"]["basis"] == "rovinai" and gemini["sale_credits"]["input"] == "5.084025"
    # Priced but never costed: visible, not pretended.
    assert "no_cost" in rows["llm:claude-opus-5"]["flags"]
    # Items the file prices only at a dead tier still appear, and unused ones say so.
    assert "unused_30d" in rows["video-gen:video-sd-1080p-pro:1080p"]["flags"]
    assert body["summary"]["credits"] == body["summary"]["credits"]  # present
    assert body["summary"]["flags"]["below_cost"] >= 1
    kinds = [row["kind"] for row in body["items"]]
    assert kinds == sorted(kinds, key=lambda k: rules.KINDS.index(k))


async def test_write_requires_confirmation_below_cost_then_takes_effect_and_is_audited(admin):
    key = "video-gen:MiniMax-H3:768p"
    request_key = uuid.uuid4().hex
    async with _client(admin) as client:
        low = {"request_key": request_key, "reason": "促销", "expected_revision": 0, "sale": {"per_second": "0.05"}}
        refused = await client.put(f"/api/admin/pricing/{key}", json=low)
        assert refused.status_code == 422 and refused.json()["detail"]["code"] == "BELOW_COST"
        assert refused.json()["detail"]["fields"] == ["per_second"]
        ok = await client.put(f"/api/admin/pricing/{key}", json={**low, "allow_below_cost": True})
        assert ok.status_code == 200, ok.text
        result = ok.json()
        assert result["rule"]["revision"] == 1 and result["below_cost_fields"] == ["per_second"]
        assert result["effective_within_seconds"] == rules.REFRESH_SECONDS
        # In effect for the next quote in this process.
        from billing import media
        assert media.quote_generation("MiniMax-H3", "768p", 10).credits == Decimal("0.50")
        assert catalogue()["version"].endswith(result["rule"]["id"][-8:])
        # Same request key replays; a different intent under it conflicts.
        again = await client.put(f"/api/admin/pricing/{key}", json={**low, "allow_below_cost": True})
        assert again.status_code == 200 and again.json()["replayed"] is True
        conflict = await client.put(f"/api/admin/pricing/{key}", json={**low, "allow_below_cost": True, "reason": "x"})
        assert conflict.status_code == 409
        # Stale revision is refused; the right one supersedes.
        stale = await client.put(f"/api/admin/pricing/{key}", json={"request_key": uuid.uuid4().hex, "reason": "r",
                                                                  "expected_revision": 0, "sale": {"per_second": "0.60"}})
        assert stale.status_code == 409 and stale.json()["detail"]["current_revision"] == 1
        second = await client.put(f"/api/admin/pricing/{key}", json={"request_key": uuid.uuid4().hex, "reason": "恢复",
                                                                   "expected_revision": 1, "sale": {"per_second": "0.60"}})
        assert second.status_code == 200 and second.json()["rule"]["revision"] == 2
        table = (await client.get("/api/admin/pricing")).json()
        row = next(r for r in table["items"] if r["key"] == key)
        assert row["sale"] == {"per_second": "0.6"} and row["base_sale"] == {"per_second": "0.50"} and "overridden" in row["flags"]
        history = (await client.get(f"/api/admin/pricing/{key}/history")).json()
        assert [r["revision"] for r in history["revisions"]] == [2, 1]
        assert history["revisions"][1]["superseded_at"] is not None
        assert [op["action"] for op in history["operations"]] == ["write", "write"]
    async with get_db_session() as db:
        audits = (await db.scalars(select(AuditLog).where(AuditLog.resource_id == key))).all()
        assert len(audits) == 2 and all(a.action == "admin.pricing.write" for a in audits)
        assert all(a.details["request"]["reason"] for a in audits)


async def test_revert_and_disable(admin):
    key = "video-gen:wan3.0-video:480p"
    async with _client(admin) as client:
        put = await client.put(f"/api/admin/pricing/{key}", json={"request_key": uuid.uuid4().hex, "reason": "r",
                                                                "expected_revision": 0, "sale": {"per_second": "0.35"}})
        assert put.status_code == 200
        from billing import media
        assert media.quote_generation("wan3.0-video", "480p", 2).credits == Decimal("0.70")
        no_confirm = await client.put(f"/api/admin/pricing/{key}", json={"request_key": uuid.uuid4().hex, "reason": "r",
                                                                       "expected_revision": 1, "status": "disabled"})
        assert no_confirm.status_code == 422 and no_confirm.json()["detail"]["code"] == "CONFIRM_DISABLE"
        off = await client.put(f"/api/admin/pricing/{key}", json={"request_key": uuid.uuid4().hex, "reason": "下架",
                                                                "expected_revision": 1, "status": "disabled", "confirm_disable": True})
        assert off.status_code == 200
        assert media.quote_generation("wan3.0-video", "480p", 2).credits is None
        assert media.quote_generation("wan3.0-video", "720p", 2).credits == Decimal("1.20")
        row = next(r for r in (await client.get("/api/admin/pricing")).json()["items"] if r["key"] == key)
        assert "disabled" in row["flags"] and "unpriced" not in row["flags"]
        back = await client.post(f"/api/admin/pricing/{key}/revert", json={"request_key": uuid.uuid4().hex, "reason": "恢复",
                                                                         "expected_revision": 2})
        assert back.status_code == 200 and back.json()["rule"] is None
        assert media.quote_generation("wan3.0-video", "480p", 2).credits == Decimal("0.60")
        # Other tests' rules may still apply; this one must not.
        assert back.json()["reverted"]["id"] not in catalogue().get("rules_applied", [])
        gone = await client.post(f"/api/admin/pricing/{key}/revert", json={"request_key": uuid.uuid4().hex, "reason": "x",
                                                                         "expected_revision": 2})
        assert gone.status_code == 404
    async with get_db_session() as db:
        current = (await db.scalars(select(PricingRule).where(PricingRule.key == key, PricingRule.superseded_at.is_(None)))).all()
        assert current == []


async def test_unknown_items_and_bad_fragments_are_refused(admin):
    async with _client(admin) as client:
        body = {"request_key": uuid.uuid4().hex, "reason": "r", "expected_revision": 0, "sale": {"per_second": "1"}}
        assert (await client.put("/api/admin/pricing/video-gen:never-heard-of:720p", json=body)).status_code == 404
        assert (await client.put("/api/admin/pricing/nonsense", json=body)).status_code == 422
        bad = await client.put("/api/admin/pricing/video-gen:MiniMax-H3:768p", json={**body, "sale": {"per_second": 1.0}})
        assert bad.status_code == 422 and bad.json()["detail"]["code"] == "INVALID_PRICE"
        empty = await client.put("/api/admin/pricing/video-gen:MiniMax-H3:768p", json={**body, "sale": None})
        assert empty.status_code == 422 and empty.json()["detail"]["code"] == "NOTHING_TO_WRITE"
        past = await client.put("/api/admin/pricing/video-gen:MiniMax-H3:768p", json={
            **body, "valid_until": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()})
        assert past.status_code == 422 and past.json()["detail"]["code"] == "VALID_UNTIL_PAST"


async def test_preview_quotes_current_and_draft_prices(admin):
    async with _client(admin) as client:
        res = await client.post("/api/admin/pricing/preview", json={"key": "video-gen:MiniMax-H3:768p", "usage": {"seconds": 20},
                                                                   "sale": {"per_second": "0.30"}})
        assert res.status_code == 200, res.text
        body = res.json()
        assert body["current"] == {"credits": "10", "cost_credits": "3.6", "reason": None, "version": catalogue()["version"]}
        assert body["draft"]["credits"] == "6" and body["draft"]["cost_credits"] == "3.6"
        llm = await client.post("/api/admin/pricing/preview", json={"key": "llm:gemini-3.8-flash",
                                                                   "usage": {"input": 1_000_000, "output": 0}})
        assert llm.json()["current"]["credits"] == "5.084025" and llm.json()["current"]["cost_credits"] == "6"
        voice = await client.post("/api/admin/pricing/preview", json={"key": "voice-realtime:qwen3.8-omni-flash-realtime",
                                                                     "usage": {"output_audio": 1_000_000}})
        assert voice.json()["current"]["credits"] == "12"
        exported = await client.get("/api/admin/pricing/export")
        assert exported.status_code == 200 and exported.json()["credits_per_cny"] == "1"
        assert "attachment" in exported.headers["content-disposition"]
