"""Partner tariffs: quotes, ladder settlement, the /v1 account endpoints and the admin writes."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from api.v1.app import create_v1_app
from auth import api_key as keys
from auth.middleware import get_current_user
from billing import partner
from billing.service import UsageMeter
from cache import set_cache
from cache.memory_cache import MemoryCache
from core.identifier import ascending
from db.base import get_db_session
from db.models.billing import CreditBalance, CreditLedger, UsageEvent
from db.models.partner import PartnerRebate, PartnerTariff
from db.models.session import Session as SessionRow
from db.models.user import User
from db.models.workspace import Workspace, WorkspaceMember


# ─── fixtures ───

async def seed_scope() -> tuple[str, str]:
    uid, wid = f"u-{uuid4().hex[:10]}", f"w-{uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(id=uid, username=uid, role="user", is_active=True, failed_login_count=0, created_at=now, updated_at=now))
        await db.flush()
        db.add(Workspace(id=wid, name="高德", owner_user_id=uid, created_at=now, updated_at=now))
        await db.flush()
        db.add(WorkspaceMember(workspace_id=wid, user_id=uid, role="owner", status="active", created_at=now, updated_at=now))
        await db.flush()
        (await db.get(User, uid)).default_workspace_id = wid
    return uid, wid


def key_identity(uid: str, wid: str, **overrides) -> dict:
    identity = {"user_id": uid, "role": "user", "workspace_id": wid, "workspace_role": "owner", "auth_kind": "api_key",
                "api_key_id": f"key-{uuid4().hex[:8]}", "scopes": list(keys.DEFAULT_SCOPES),
                "policy": dict(keys.DEFAULT_POLICY), "rate_limit": "60/minute"}
    identity.update(overrides)
    return identity


def v1_client(identity: dict) -> httpx.AsyncClient:
    app = create_v1_app()
    app.dependency_overrides[get_current_user] = lambda: identity
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def admin_client(identity: dict) -> httpx.AsyncClient:
    from api.admin_partners import router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: dict(identity)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    from unittest.mock import AsyncMock
    monkeypatch.setenv("BILLING_MODE", "shadow")
    monkeypatch.setattr("billing.subscriptions.ensure_period_allowance", AsyncMock())
    set_cache(MemoryCache())
    partner.clear_caches()
    yield
    partner.clear_caches()
    set_cache(None)


@pytest.fixture
def partner_config(monkeypatch):
    from core.config import OpenBoxConfig
    config = OpenBoxConfig.model_validate({
        "jwt_secret": "x" * 32,
        "video_generation": {"provider": "test", "models": [
            {"id": "wan3.0-video", "channel": "sd2", "resolutions": ["480p", "720p", "1080p"], "duration_range": [2, 30], "max_duration_seconds": 30},
            {"id": "MiniMax-H3", "channel": "sd2", "resolutions": ["512p", "768p", "2k"]},
        ]},
        "model_tiers": {"video": [
            {"tier": "medium", "model": "wan3.0-video", "resolution": "720p"},
            {"tier": "low", "model": "MiniMax-H3", "resolution": "768p"},
        ]},
    })
    monkeypatch.setattr("core.config.get_config", lambda: config)
    return config


async def partner_session(uid: str, wid: str, *, quality: str = "medium", api_key_id: str = "key-1", task_id: str = "t_456") -> SessionRow:
    from sqlalchemy import update
    from session import session as session_mod

    created = await session_mod.create_session(model="openai/gemini-3.8-flash", agent="build", title="partner",
                                               user_id=uid, workspace_id=wid)
    async with get_db_session() as db:
        await db.execute(update(SessionRow).where(SessionRow.id == created.id).values({
            SessionRow.quality: quality, SessionRow.api_key_id: api_key_id, SessionRow.metadata_: {"task_id": task_id},
            SessionRow.video_model: "wan3.0-video", SessionRow.video_resolution: "1080p"}))
    async with get_db_session() as db:
        return await db.get(SessionRow, created.id)


# ─── tariff arithmetic ───

def test_default_tariff_validates_and_bad_documents_are_refused():
    doc = partner.validate_tariff(partner.default_tariff())
    assert doc["video"]["tiers"]["medium"]["default_resolution"] == "720p"
    broken = partner.default_tariff()
    broken["video"]["tiers"]["medium"]["multipliers"]["720p"] = "2"
    with pytest.raises(partner.TariffError, match="multiplier must be 1"):
        partner.validate_tariff(broken)
    broken = partner.default_tariff()
    broken["video"]["ladder"]["thresholds"] = [20000, 5000]
    with pytest.raises(partner.TariffError, match="ascending"):
        partner.validate_tariff(broken)
    broken = partner.default_tariff()
    broken["llm"]["models"]["openai/x"] = "nope"
    with pytest.raises(partner.TariffError, match="undeclared"):
        partner.validate_tariff(broken)


def test_video_quote_uses_base_price_times_multiplier():
    tariff = partner.default_tariff()
    tier = tariff["video"]["tiers"]["medium"]
    quote = partner.quote_video(tariff, "medium", tier, "wan3.0-video", "1080p", 12)
    assert quote.credits == Decimal("33.6") and quote.snapshot["per_second"] == "2.800000000000"
    assert quote.snapshot["partner"]["multiplier"] == "2" and quote.snapshot["partner"]["ladder"] == 1
    assert quote.model_id == "video-gen:wan3.0-video:1080p"
    refused = partner.quote_video(tariff, "medium", tier, "wan3.0-video", "2k", 12)
    assert refused.credits is None and "not offered" in refused.snapshot["reason"]
    third = partner.quote_video(tariff, "medium", tier, "wan3.0-video", "720p", 10, ladder=3)
    assert third.credits == Decimal("10")


def test_llm_and_compose_quotes():
    tariff = partner.default_tariff()
    name, tier = partner.llm_tier(tariff, "openai/gemini-3.8-flash")
    assert name == "medium"
    quote = partner.quote_llm(tariff, name, tier, {"input": 1_000_000, "output": 0, "cache_read": 0})
    assert quote.credits == Decimal("10.2")
    cached = partner.quote_llm(tariff, name, tier, {"input": 1_000_000, "output": 1_000_000, "cache_read": 1_000_000})
    assert cached.credits == Decimal("51") + Decimal("1.19")
    assert partner.llm_tier(tariff, "openai/unknown") is None
    compose = partner.quote_compose_partner(tariff, 720, 1280, 61)
    assert compose.minutes_billed == 120 and compose.credits == Decimal("0.09")


def _event(wid, *, kind, credits, tokens, pricing, created_at, status="charged"):
    return UsageEvent(id=ascending("usage"), idempotency_key=uuid4().hex, workspace_id=wid, user_id="u", session_id="s",
                      message_id=None, session_title="t", model_id="m", kind=kind, tokens=tokens, total_tokens=tokens.get("total", 0),
                      credits=Decimal(credits), status=status, pricing=pricing, created_at=created_at)


def test_ladder_summary_rebates_the_difference():
    tariff = partner.default_tariff()
    tariff["video"]["ladder"]["thresholds"] = [100, 300]
    at = datetime(2026, 10, 5, tzinfo=timezone.utc)
    events = []
    for i in range(5):  # 5 × 80 s medium 720p at ladder-1 price 1.40 → 400 s straddle all three steps
        events.append(_event("w", kind="video_generate", credits="112", tokens={"seconds_billed": 80},
                             pricing={"partner": {"quality": "medium", "multiplier": "1"}}, created_at=at + timedelta(minutes=i)))
    events.append(_event("w", kind="chat", credits="10.2", tokens={"input": 1_000_000, "output": 0, "cache_read": 0, "total": 1_000_000},
                         pricing={"partner": {"tier": "medium"}}, created_at=at))
    summary = partner.summarize(tariff, events, "2026-10")
    medium = summary["by_quality"]["medium"]
    assert medium["output_seconds"] == 400 and medium["ladder"] == 3
    # 100 s × 1.40 + 200 s × 1.20 + 100 s × 1.00 = 480
    assert medium["ladder_credits"] == Decimal("480") and medium["charged"] == Decimal("560")
    assert summary["llm"]["ladder_credits"] == Decimal("10.2") and summary["rebate"] == Decimal("80")


# ─── runtime charging on partner sessions ───

async def test_partner_session_llm_is_metered_from_the_tariff(partner_config):
    uid, wid = await seed_scope()
    session = await partner_session(uid, wid)
    meter = await UsageMeter.start(model_id="openai/gemini-3.8-flash", session_id=session.id, user_id=uid)
    assert meter.partner is not None
    credits = await meter.finish({"input": 100_000, "output": 10_000, "cache_read": 0})
    assert credits == Decimal("1.53")  # 0.1M × 10.2 + 0.01M × 51
    async with get_db_session() as db:
        event = await db.get(UsageEvent, meter.event_id)
    assert event.pricing["partner"]["tier"] == "medium" and event.pricing["source"] == "partner tariff"
    plain = await UsageMeter.start(model_id="openai/gemini-3.8-flash", session_id=session.id, user_id=uid)
    plain.partner = None


async def test_partner_session_video_quote_and_settlement(partner_config):
    uid, wid = await seed_scope()
    session = await partner_session(uid, wid, quality="medium")
    quote = await partner.quote_generation_for_session(session.id, "wan3.0-video", "1080p", 10)
    assert quote.credits == Decimal("28") and quote.snapshot["partner"]["quality"] == "medium"
    from billing.media import settle_generation
    job = SimpleNamespace(id=ascending("vjob"), user_id=uid, session_id=session.id, request_data={})
    asset = SimpleNamespace(workspace_id=wid)
    credits = await settle_generation(job, asset, model_id="wan3.0-video", resolution="1080p", duration_sec=10)
    assert credits == Decimal("28")
    async with get_db_session() as db:
        event = (await db.scalars(select(UsageEvent).where(UsageEvent.workspace_id == wid))).one()
    assert event.kind == "video_generate" and event.pricing["partner"]["multiplier"] == "2"
    assert event.tokens["seconds_billed"] == 10


# ─── /v1 account endpoints ───

async def test_models_me_usage_and_summary(partner_config):
    uid, wid = await seed_scope()
    identity = key_identity(uid, wid)
    session = await partner_session(uid, wid, api_key_id=identity["api_key_id"])
    from billing.media import settle_generation
    for seconds in (10, 20):
        job = SimpleNamespace(id=ascending("vjob"), user_id=uid, session_id=session.id, request_data={})
        await settle_generation(job, SimpleNamespace(workspace_id=wid), model_id="wan3.0-video", resolution="1080p", duration_sec=seconds)
    meter = await UsageMeter.start(model_id="openai/gemini-3.8-flash", session_id=session.id, user_id=uid)
    await meter.finish({"input": 40_000, "output": 6_000, "cache_read": 0})
    async with v1_client(identity) as http:
        models = await http.get("/models")
        assert models.status_code == 200, models.text
        body = models.json()
        medium = next(t for t in body["tiers"] if t["quality"] == "medium")
        assert medium["default_resolution"] == "720p" and medium["multipliers"]["1080p"] == "2"
        assert medium["base_price"] == "1.40" and medium["ladder"] == 1 and medium["month_output_seconds"] == 30
        assert medium["available"] is True and medium["max_shot_seconds"] == 30
        assert next(t for t in body["tiers"] if t["quality"] == "ultra")["available"] is False
        assert body["compose"]["round_seconds"] == 60

        me = await http.get("/me")
        assert me.status_code == 200, me.text
        assert me.json()["name"] == "高德" and me.json()["month_usage"]["output_seconds"] == {"medium": 30}
        assert me.json()["limits"] == {"requests_per_minute": 60, "concurrent_sessions": 5}
        assert me.json()["month_usage"]["llm_tokens"] == 46_000

        usage = await http.get("/usage")
        assert usage.status_code == 200, usage.text
        items = usage.json()["items"]
        assert len(items) == 3 and all(i["task_id"] == "t_456" for i in items)
        video = [i for i in items if i["kind"] == "video_generate"]
        assert video[0]["unit_price"] == "2.800000000000" and video[0]["multiplier"] == "2" and video[0]["status"] == "charged"
        chat = next(i for i in items if i["kind"] == "chat")
        assert chat["quantity"] == {"input_tokens": 40_000, "output_tokens": 6_000, "cached_tokens": 0} and chat["model_tier"] == "medium"
        by_task = await http.get("/usage", params={"task_id": "nope"})
        assert by_task.json() == {"items": [], "next_cursor": None}
        paged = await http.get("/usage", params={"limit": 2})
        assert len(paged.json()["items"]) == 2 and paged.json()["next_cursor"]
        rest = await http.get("/usage", params={"limit": 2, "cursor": paged.json()["next_cursor"]})
        assert len(rest.json()["items"]) == 1 and rest.json()["next_cursor"] is None
        bad = await http.get("/usage", params={"from": "2026-01-01T00:00:00Z", "to": "2026-09-01T00:00:00Z"})
        assert bad.status_code == 400

        summary = await http.get("/usage/summary", params={"group_by": "quality"})
        assert summary.status_code == 200, summary.text
        rows = summary.json()["rows"]
        assert {"quality": "medium", "output_seconds": 30, "credits": "84", "ladder": 1, "ladder_credits": "84"} in rows
        assert summary.json()["ladder_rebate"] == {"status": "pending", "credits": "0"}
        chat_row = next(r for r in rows if r.get("kind") == "chat")
        assert chat_row["tokens"] == 46_000 and chat_row["events"] == 1
        assert sum(1 for r in rows if r.get("kind") == "chat") == 1
        daily = await http.get("/usage/summary", params={"group_by": "day"})
        assert daily.json()["rows"][0]["output_seconds"] == 30


# ─── admin ───

async def test_admin_tariff_override_and_rebate_settlement(partner_config):
    from db.repository.user_repo import PgUserRepo
    suffix = uuid4().hex[:8]
    admin = await PgUserRepo().create(id=f"apart-{suffix}", username=f"apart-{suffix}", password_hash="unused", role="admin")
    identity = {"user_id": admin["id"], "role": "admin"}
    uid, wid = await seed_scope()
    custom = partner.default_tariff()
    custom["version"] = "custom-1"
    custom["video"]["tiers"]["medium"]["base_price"] = ["1.00", "0.90", "0.80"]
    async with admin_client(identity) as http:
        default = await http.get(f"/api/admin/partners/{wid}/tariff")
        assert default.json()["source"] == "default" and default.json()["revision"] == 0
        bad = await http.put(f"/api/admin/partners/{wid}/tariff", json={"tariff": {"video": {}}, "reason": "x"})
        assert bad.status_code == 422
        put = await http.put(f"/api/admin/partners/{wid}/tariff", json={"tariff": custom, "reason": "源码客户", "expected_revision": 0})
        assert put.status_code == 200, put.text
        assert put.json()["revision"] == 1 and put.json()["tariff"]["version"] == "custom-1"
        stale = await http.put(f"/api/admin/partners/{wid}/tariff", json={"tariff": custom, "reason": "again", "expected_revision": 0})
        assert stale.status_code == 409
    tariff, revision = await partner.tariff_for_workspace(wid)
    assert revision == 1 and tariff["version"] == "custom-1"
    session = await partner_session(uid, wid)
    quote = await partner.quote_generation_for_session(session.id, "wan3.0-video", "720p", 10)
    assert quote.credits == Decimal("10")

    # Last month's usage: 150 s at ladder 1 (1.00/s) with thresholds 100/300 → ladder credits 100 + 50 × 0.90 = 145
    custom["video"]["ladder"]["thresholds"] = [100, 300]
    async with get_db_session() as db:
        row = await db.get(PartnerTariff, wid)
        row.tariff = custom
    partner.clear_caches()
    first = datetime.now(timezone.utc).replace(day=1)
    last_month = (first - timedelta(days=1)).strftime("%Y-%m")
    at = (first - timedelta(days=10)).replace(tzinfo=None)
    async with get_db_session() as db:
        for i in range(3):
            db.add(_event(wid, kind="video_generate", credits="50", tokens={"seconds_billed": 50},
                          pricing={"partner": {"quality": "medium", "multiplier": "1"}}, created_at=at + timedelta(minutes=i)))
    async with admin_client(identity) as http:
        usage = await http.get(f"/api/admin/partners/{wid}/usage", params={"month": last_month})
        assert usage.status_code == 200, usage.text
        assert usage.json()["rebate"] == "5" and usage.json()["settlement"] is None
        too_early = await http.post(f"/api/admin/partners/{wid}/rebates/{partner.current_month()}", json={"reason": "x"})
        assert too_early.status_code == 409
        settled = await http.post(f"/api/admin/partners/{wid}/rebates/{last_month}", json={"reason": "月末对账"})
        assert settled.status_code == 200, settled.text
        assert settled.json()["rebate"] == "5" and settled.json()["replayed"] is False
        again = await http.post(f"/api/admin/partners/{wid}/rebates/{last_month}", json={"reason": "月末对账"})
        assert again.json()["replayed"] is True
        listed = await http.get("/api/admin/partners")
        assert listed.status_code == 200
    async with get_db_session() as db:
        assert (await db.get(CreditBalance, wid)).balance == Decimal("5")
        ledger = (await db.scalars(select(CreditLedger).where(CreditLedger.workspace_id == wid))).one()
        assert ledger.kind == "rebate"
        assert (await db.scalars(select(PartnerRebate).where(PartnerRebate.workspace_id == wid))).one().month == last_month
