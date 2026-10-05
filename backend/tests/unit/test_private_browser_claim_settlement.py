"""A committed terminal receipt is not a lost browser operation claim."""
import asyncio

import pytest
from sqlalchemy import select

from agent import effect_ledger as effects
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from tests.offline_wuying import install_wuying_offline_guard
from tests.unit.test_private_browser_automation import (  # noqa: F401
    active, automation, browser_world, private_world, assistant_database,
    call, execute, effects_for,
)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "true")
    install_wuying_offline_guard(monkeypatch)


async def test_own_terminal_commit_does_not_look_like_a_stolen_browser_claim(active, monkeypatch):
    w = active
    await call(w, {"action": "capture"})
    monkeypatch.setattr(effects, "EFFECT_LEASE_SECONDS", 2)
    original = effects.settle_effect
    committed, canceled = asyncio.Event(), asyncio.Event()

    async def settle_then_delay_return(*args, **kwargs):
        # Keep the actual SQL CAS, terminal receipt and cleared claim. Only
        # delay returning from the already committed operation to its caller.
        snapshot = await original(*args, **kwargs)
        committed.set()
        try:
            await asyncio.sleep(2.4)
        except asyncio.CancelledError:
            canceled.set()
            raise
        return snapshot

    monkeypatch.setattr(effects, "settle_effect", settle_then_delay_return)
    result = await execute(w, {"action": "capture"})
    effect, = await effects_for(w)
    assert committed.is_set()
    assert effect.state == "succeeded" and effect.attempt_count == 1
    assert effect.claim_token is None and effect.claim_kind is None
    assert [kind for kind, _ in w.pipe.calls] == ["capture"]
    async with get_db_session() as db:
        observed, = (await db.scalars(select(AgentEvent).where(
            AgentEvent.session_id == w.ctx.session_id,
            AgentEvent.kind == "resource.observed"))).all()
    assert observed.payload["effect_id"] == effect.id and observed.payload["eligible"] is True
    assert not result.metadata.get("error") and not canceled.is_set(), {
        "tool_error": result.metadata.get("error"), "post_commit_canceled": canceled.is_set(),
        "effect_state": effect.state, "attempts": effect.attempt_count,
    }
    assert result.metadata["asset_id"] == observed.payload["asset_id"]
