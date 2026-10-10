"""Real loop/tool/provider checkpoints preserve a corrected human constraint."""
import json
import re
from types import SimpleNamespace

from sqlalchemy import select

from agent import loop, processor
from agent.driver import reserve_run
from agent.inbox import accept_inbox_item
from assistant.decisions import RECORDED, decision_context
from assistant.service import ensure_main_session
from db.base import get_db_session
from db.models.agent_event import AgentEvent
from session.agent_event_log import verify_agent_event_parity
from tests.unit.test_agent_loop_terminal_steps import _loop_config, _patch_real_loop_runtime
from tests.unit.test_assistant_foundation import accounts, assistant_database  # noqa: F401
from tool.assistant_tools import assistant_tools


async def test_real_provider_reads_human_source_proposes_and_commits_a_correction(monkeypatch):
    config = _loop_config()
    config.permission = {"*": "allow"}
    real_history = loop._resolve_history_tool_names
    _patch_real_loop_runtime(monkeypatch, config=config, process_step=processor.process_step)
    monkeypatch.setattr(loop, "_resolve_history_tool_names", real_history)
    async def tools(*args, **kwargs):
        return SimpleNamespace(tools={tool.id: tool for tool in assistant_tools}, catalogue_availability="available")
    monkeypatch.setattr(loop, "resolve_step_tools", tools)
    owner, _, workspace = await accounts()
    main = await ensure_main_session(user_id=owner, workspace_id=workspace, model=config.model)
    phase, calls, previous = 0, 0, None

    async def stream(**kwargs):
        nonlocal calls
        calls += 1
        ctx = kwargs["ctx"]
        wire_by_id = {tool.id: name for name, tool in kwargs["tools"].items()}
        payload = json.dumps(kwargs["messages"])
        assert ctx.sandbox is None
        if calls == 1:
            human_id = re.findall(r"Original human message_id=([^\]]+)", payload)[-1]
            yield {"type": "tool_call", "tool": wire_by_id["history.read"], "args": {
                "session_id": main.id, "message_ids": [human_id]}, "call_id": f"history-{phase}", "invalid": False}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
        elif calls == 2:
            page = next(json.loads(message["content"]) for message in reversed(kwargs["messages"])
                        if message.get("role") == "tool")
            entry = page["items"][0]
            ref = {key: entry["source_ref"][key] for key in ("session_id", "message_id", "part_id", "content_hash")}
            yield {"type": "tool_call", "tool": wire_by_id["decisions.propose"], "args": {
                "summary": "Use green instead of blue" if phase else "Use blue",
                "source_refs": [{**ref, "quote": entry["text"]}], "supersedes": [previous] if previous else []},
                "call_id": f"decision-{phase}", "invalid": False}
            yield {"type": "finish", "reason": "tool_calls", "usage": {}}
        else:
            assert calls == 3 and "pending_answer_commit" in payload and "grants_authority" in payload
            yield {"type": "text_delta", "text": "Noted the human correction." if phase else "Noted the human constraint."}
            yield {"type": "finish", "reason": "stop", "usage": {}}
    monkeypatch.setattr(processor, "stream_llm", stream)
    for index, prompt in enumerate(("Use blue for this work.", "Correction: use green instead of blue.")):
        phase, calls = index, 0
        await accept_inbox_item(session_id=main.id, user_id=owner, delivery="followup", prompt=prompt,
            agent="assistant", origin="human", origin_ref={"actor_user_id": owner})
        lease = await reserve_run(main.id, owner)
        try:
            await loop.run_loop(main.id, user_id=owner, lease=lease)
            assert calls == 3
            async with get_db_session() as db:
                saved = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == main.id,
                    AgentEvent.kind == RECORDED).order_by(AgentEvent.sequence))).all())
                assert len(saved) == phase + 1
                if previous:
                    assert saved[-1].payload["supersedes"] == [previous]
                previous = saved[-1].payload["decision_id"]
                current = await decision_context(db, SimpleNamespace(id=main.id, user_id=owner, workspace_id=workspace),
                    run_fence=(main.id, "future-run", 999))
                # V2 injects the effective note itself; its quoted source was checked once, at commit.
                assert [entry["decision_id"] for entry in current["decisions"]] == [previous]
                assert current["decisions"][0]["state"] == "effective"
                assert current["decisions"][0]["summary"] == ("Use green instead of blue" if phase else "Use blue")
                assert saved[-1].payload["source_refs"][0]["quote"] == prompt
        finally:
            await lease.release(session_status="idle")
    assert (await verify_agent_event_parity(main.id, user_id=owner)).ok
