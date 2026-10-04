"""The assistant profile is server-owned and a real loop never prepares a VM."""
from types import SimpleNamespace
from datetime import datetime, timezone
from uuid import uuid4

from agent import loop
from agent.agent import get_agent, list_agents, list_subagents
from agent.driver import reserve_run
from agent.inbox import accept_inbox_item
from agent.processor import StepOutcome, StepResult
from assistant.reporting import ASSISTANT_TOOLS
from assistant.service import ensure_main_session
from core.config import AgentOverride
from models.message import TextPart
from session.session import save_part
from db.base import get_db_session
from db.models.file_asset import FileAsset
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401


def test_profile_cannot_be_overridden_or_selected_as_a_subagent(monkeypatch):
    config = _loop_config()
    config.agent["assistant"] = AgentOverride(tools=["bash"], prompt="override", hidden=False, mode="all")
    monkeypatch.setattr("core.config.get_config", lambda: config)
    frozen = get_agent("assistant")
    from assistant.continuation import COORDINATION_TOOLS
    assert frozen.hidden and frozen.mode == "primary" and set(frozen.tools) == ASSISTANT_TOOLS | COORDINATION_TOOLS
    frozen.tools.append("bash")
    assert "bash" not in get_agent("assistant").tools
    assert "assistant" not in {a.name for a in list_agents() + list_subagents()}
    assert loop.resolve_agent_name(SimpleNamespace(agent="build"), SimpleNamespace(kind="assistant")) == "assistant"


async def test_real_assistant_loop_skips_sandbox_and_uses_fixed_prompt(monkeypatch):
    config = _loop_config()
    calls = []
    async def processor(**kwargs):
        calls.append(kwargs)
        ctx = kwargs["ctx"]
        assert ctx.sandbox is None
        await save_part(TextPart(session_id=ctx.session_id, message_id=ctx.message_id,
            text="I can coordinate tasks in your projects."), is_new=True, user_id=ctx.user_id,
            run_fence=ctx.run_fence)
        return StepResult(outcome=StepOutcome.CONTINUE, finish_reason="stop",
                          text="I can coordinate tasks in your projects.")
    build_prompt = loop._build_system_prompt
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor)
    monkeypatch.setattr(loop, "_build_system_prompt", build_prompt)
    async def forbidden(*args, **kwargs):
        raise AssertionError("the assistant must not prepare a sandbox")
    monkeypatch.setattr("sandbox.sandbox_manager.get_client", forbidden)
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model=config.model)
    await accept_inbox_item(session_id=main.id, user_id=owner, delivery="followup", prompt="What can you do?",
        agent="assistant", origin="human", origin_ref={"actor_user_id": owner})
    lease = await reserve_run(main.id, owner)
    try:
        await loop.run_loop(main.id, owner, lease=lease)
        assert len(calls) == 1
        assert any("private personal assistant" in item for item in calls[0]["system"])
    finally:
        await lease.release(session_status="idle")


async def test_main_attachments_are_references_and_missing_files_close_the_turn_without_a_sandbox(monkeypatch):
    from agent.inbox import claim_inbox_boundary, deliver_claimed_attachments
    from db.models.agent_inbox import AgentInboxItem
    async def forbidden(*args, **kwargs):
        raise AssertionError("main attachments must not prepare or deliver to a sandbox")
    monkeypatch.setattr("sandbox.assets.deliver_asset_ids", forbidden)
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace)
    asset_id = "pa-asset-" + uuid4().hex
    async with get_db_session() as db:
        db.add(FileAsset(id=asset_id, user_id=owner, workspace_id=workspace, session_id=main.id,
            project_id=main.project_id, name="evidence.txt", oss_key="test/evidence", mime="text/plain",
            size=10, status="ready", created_at=datetime.now(timezone.utc)))
    accepted = await accept_inbox_item(session_id=main.id, user_id=owner, delivery="followup", prompt="Use this file",
        attachments=[asset_id], agent="assistant", origin="human", origin_ref={"actor_user_id": owner})
    lease = await reserve_run(main.id, owner)
    try:
        await claim_inbox_boundary(lease, step=1, include_next_turn=True)
        delivered = await deliver_claimed_attachments(lease, expected_asset_ids=[asset_id])
        assert delivered.runnable_item_ids == (accepted.id,)
        async with get_db_session() as db:
            (await db.get(FileAsset, asset_id)).is_deleted = True
        unavailable = await deliver_claimed_attachments(lease, expected_asset_ids=[asset_id])
        assert unavailable.terminal_item_ids == (accepted.id,) and unavailable.result_message_id
        async with get_db_session() as db:
            item = await db.get(AgentInboxItem, accepted.id)
            assert item.state == "settled" and item.outcome == "delivery_error"
    finally:
        await lease.release(session_status="idle")
