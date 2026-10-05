"""Slow local capture persistence must retain the original effect claim."""
import asyncio

import pytest
from sqlalchemy import select

from agent import effect_ledger as effects
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from db.models.external_effect import ExternalEffect
from tests.unit.test_private_browser_automation import (  # noqa: F401
    active, automation, browser_world, private_world, assistant_database,
    call, execute, effects_for,
)


@pytest.mark.parametrize("stage", ["store", "after_part"])
async def test_capture_keeps_claim_through_slow_local_persistence(active, monkeypatch, stage):
    import core.oss
    import session.session

    w = active
    await call(w, {"action": "capture"})
    # This uses the real SQL expiry/renewal checks, including SQLite's
    # second-resolution clock; only a local persistence delay is injected.
    monkeypatch.setattr(effects, "EFFECT_LEASE_SECONDS", 2)
    if stage == "store":
        oss = core.oss.get_oss()
        original = oss.put_object

        async def store(*args, **kwargs):
            await asyncio.sleep(2.4)
            return await original(*args, **kwargs)

        oss.put_object = store
        monkeypatch.setattr(core.oss, "get_oss", lambda: oss)
    else:
        original = session.session.save_part

        async def save(*args, **kwargs):
            result = await original(*args, **kwargs)
            await asyncio.sleep(2.4)
            return result

        monkeypatch.setattr(session.session, "save_part", save)
    result = await execute(w, {"action": "capture"})
    assert not result.metadata.get("error"), result
    effect, = await effects_for(w)
    assert effect.state == "succeeded" and effect.attempt_count == 1
    assert [kind for kind, _ in w.pipe.calls] == ["capture"]
    async with get_db_session() as db:
        observed, = (await db.scalars(select(AgentEvent).where(
            AgentEvent.session_id == w.ctx.session_id,
            AgentEvent.kind == "resource.observed"))).all()
    assert observed.payload["effect_id"] == effect.id
    assert observed.payload["asset_id"] == result.metadata["asset_id"]
    assert observed.payload["eligible"] is True


async def test_lost_claim_cancels_slow_capture_persistence(active, monkeypatch):
    import core.oss

    w = active
    await call(w, {"action": "capture"})
    monkeypatch.setattr(effects, "EFFECT_LEASE_SECONDS", 2)
    oss = core.oss.get_oss()
    canceled = asyncio.Event()

    async def store(*args, **kwargs):
        async with get_db_session() as db:
            effect, = (await db.scalars(select(ExternalEffect).where(
                ExternalEffect.session_id == w.ctx.session_id))).all()
            effect.claim_generation += 1
        try:
            await asyncio.sleep(3)
        except asyncio.CancelledError:
            canceled.set()
            raise
        raise AssertionError("A fenced-out capture kept persisting its result")

    oss.put_object = store
    monkeypatch.setattr(core.oss, "get_oss", lambda: oss)
    result = await execute(w, {"action": "capture"})
    assert result.metadata.get("error") and canceled.is_set()
    effect, = await effects_for(w)
    assert effect.state == "submitting" and effect.attempt_count == 1
    assert [kind for kind, _ in w.pipe.calls] == ["capture"] and not w.objects
    async with get_db_session() as db:
        assert await db.scalar(select(AgentEvent.id).where(
            AgentEvent.session_id == w.ctx.session_id,
            AgentEvent.kind == "resource.observed")) is None
