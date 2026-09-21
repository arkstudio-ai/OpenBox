"""A confirmed build-chat proposal keeps its admitted tools on a cold worker."""
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from agent import subagent_authority
from agent.agent import get_agent
from agent.loop import resolve_agent_name
from agent_catalog.compiler import compile_agent
from agent_catalog.schemas import TeamPolicy
from db.base import get_db_session
from db.models.session import Session
from team import runtime_binding
from team.policy import COORDINATOR_TOOLS


async def test_unconfirmed_team_exposes_planning_but_not_runtime_commands():
    from types import SimpleNamespace
    from team import runtime_binding
    token = runtime_binding._current.set(None)
    try:
        offered = dict.fromkeys(COORDINATOR_TOOLS | {"question", "read"})
        visible = await runtime_binding.restrict_current_tools(offered,
            session=SimpleNamespace(agent="team", kind="normal", parent_id=None))
        assert set(visible) == {"team_propose", "agent_catalog_search", "agent_catalog_get", "question", "read"}
    finally:
        runtime_binding._current.reset(token)
from team.service import start_confirmed_locked
from tests.unit.test_team_catalog import config, new_root
from tests.unit.test_team_compiler import spec


@pytest.mark.parametrize("stale_agent", ["build", "plan"])
async def test_confirmed_root_keeps_frozen_coordinator_after_admission_is_disabled(config, monkeypatch, stale_agent):
    monkeypatch.setattr(runtime_binding, "get_config", lambda: config)
    monkeypatch.setattr(subagent_authority, "_bound_frozen_agent", ContextVar("test_frozen_agent", default=None))
    root_id, _ = await new_root()
    compiled = compile_agent(spec(), config=config, role="coordinator")
    async with get_db_session() as db:
        root = await db.get(Session, root_id)
        await start_confirmed_locked(db, root=root, question_id="confirmed-build-turn",
            title="Mixed roster", goal="Review the calculation", policy=TeamPolicy(),
            grant={"version": 1, "budget_credits": "2", "delegable_tools": [], "paid_tools": {}},
            coordinator=compiled, members=[])
        # An earlier worker may have written the stale mode back. The journal
        # still owns execution authority, independently of this metadata.
        root.agent = stale_agent
    config.team_admission_enabled = False
    async with get_db_session() as db:
        root = await db.get(Session, root_id)
    authority = await subagent_authority.load_subagent_authority(root)
    name = resolve_agent_name(SimpleNamespace(agent=stale_agent), root,
        bound_agent_name=authority.composition.agent_preset.name)
    frozen = get_agent(name)
    assert name == "team"
    assert frozen.model == compiled.summary["model"]
    assert frozen.prompt == compiled.authority.composition.frozen_agent().prompt
    assert COORDINATOR_TOOLS <= set(frozen.tools)
    permitted = await runtime_binding.restrict_current_tools(dict.fromkeys(frozen.tools), session=root)
    assert COORDINATOR_TOOLS <= permitted.keys()


def test_unbound_mode_switch_remains_message_driven():
    assert resolve_agent_name(SimpleNamespace(agent="build"), SimpleNamespace(agent="plan")) == "build"
