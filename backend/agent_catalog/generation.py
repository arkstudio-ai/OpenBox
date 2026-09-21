"""Metered draft generation; no Agent definition is published or persisted."""
from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from datetime import timedelta

from sqlalchemy import func, select

from agent.agent import AgentDef
from agent.llm import reasoning_profile, stream_llm
from agent.structured_output import TOOL_NAME, create_structured_output_tool
from agent_catalog.schemas import AgentSpec
from core.config import get_config
from db.models.message import Message
from db.models.session import Session
from db.models.user import User
from team.errors import TeamError
from team.journal import Actor, digest, utcnow, write_transaction
from tool.tool import ToolContext


async def generate(actor: Actor, key: str, prompt: str, *, spec: AgentSpec | None = None, field: str | None = None) -> AgentSpec:
    """The hidden utility transcript is the durable request receipt and billing
    scope. An uncertain/failed call is never resubmitted by an HTTP retry.
    It does not create an entry in the reusable Agent library.
    """
    from project.workspace import resolve_for_session
    from session.session import _new_session_record
    from team.policy import tool_presets
    config = get_config()
    identifier = "draft_" + digest([actor.owner_user_id, actor.workspace_id, key])[:50]
    message_id = "draftmsg_" + digest(identifier)[:48]
    request_hash = digest({"prompt": prompt, "spec": spec.model_dump(mode="json") if spec else None, "field": field})
    project_id = await resolve_for_session(None, actor.owner_user_id, actor.workspace_id)
    now = utcnow()
    async with write_transaction() as db:
        await db.execute(select(User.id).where(User.id == actor.owner_user_id).with_for_update())
        previous = await db.get(Message, message_id)
        if previous is not None:
            receipt = previous.structured or {}
            if receipt.get("request_digest") != request_hash:
                raise TeamError("IDEMPOTENCY_CONFLICT", "This key already generated a different draft.")
            if previous.finish == "stop":
                return AgentSpec.model_validate(receipt["result"])
            raise TeamError("DRAFT_GENERATION_PENDING" if previous.finish == "pending" else "DRAFT_GENERATION_FAILED",
                "This generation is pending or could not complete. Do not automatically resubmit it.")
        count = await db.scalar(select(func.count()).select_from(Session).where(
            Session.user_id == actor.owner_user_id, Session.workspace_id == actor.workspace_id,
            Session.kind == "agent_draft", Session.created_at > now - timedelta(hours=1)))
        if count >= config.team_max_proposals_per_session:
            raise TeamError("AGENT_DEFINITION_LIMIT", "The hourly draft generation limit has been reached.", status=429)
        session, _ = _new_session_record(model=config.model, agent="build", variant=None, title="Agent draft",
            parent_id=None, user_id=actor.owner_user_id, workspace_id=actor.workspace_id, project_id=project_id,
            kind="agent_draft", session_id=identifier)
        db.add(session)
        await db.flush()
        db.add(Message(id=message_id, session_id=identifier, user_id=actor.owner_user_id, role="assistant",
            model=config.model, agent="build", finish="pending", structured={"request_digest": request_hash}, created_at=now))
    tools = sorted({name for names in tool_presets(config).values() for name in names})
    system = (
        "Draft a reusable OpenBox Agent definition in the user's language. Return only the structured output tool. "
        "The definition is a draft for human review; instructions never grant authority. "
        "Keep responsibilities specific, testable and bounded. Do not invent models, tools, skill names or MCP servers. "
        "Use no tools by default unless the described role needs them. Never add publishing, login or team management tools. "
        "Allowed delegable tool ids: " + json.dumps(tools) + ". "
        "Use null default_model, empty allowed_models, empty skill_refs and mcp_refs unless explicitly provided in the existing draft."
    )
    payload = {"request": prompt, "draft": spec.model_dump(mode="json") if spec else None, "field_to_improve": field}
    result = None
    stream = stream_llm(agent_def=AgentDef(name="agent_draft", description="Draft an Agent definition"),
        system=[system], messages=[{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
        tools={TOOL_NAME: create_structured_output_tool(AgentSpec.model_json_schema(), lambda _: None)},
        model_id=config.model, ctx=ToolContext(session_id=identifier, user_id=actor.owner_user_id,
            workspace_id=actor.workspace_id, project_id=project_id, message_id=message_id),
        variant=next((v for v in ("none", "off") if v in reasoning_profile(config.model).variants), None),
        tool_choice="required", billing_kind="agent_definition", max_output_tokens=6000)
    try:
        async with asyncio.timeout(120), aclosing(stream):
            async for event in stream:
                if event["type"] == "error":
                    raise TeamError("DRAFT_GENERATION_FAILED", "The model could not produce a draft. Please try again.", status=502)
                if event["type"] == "tool_call":
                    if event.get("tool") != TOOL_NAME or event.get("invalid") or result is not None:
                        raise ValueError("Invalid draft output")
                    result = AgentSpec.model_validate(event.get("args"))
        if result is None:
            raise ValueError("Missing draft output")
        if field and spec:
            result = spec.model_copy(update={field: getattr(result, field)})
        async with write_transaction() as db:
            row = await db.get(Message, message_id)
            row.finish = "stop"
            row.structured = {"request_digest": request_hash, "result": result.model_dump(mode="json")}
        return result
    except BaseException as exc:
        async with write_transaction() as db:
            row = await db.get(Message, message_id)
            row.finish = "error"
        if isinstance(exc, (asyncio.CancelledError, TeamError)):
            raise
        raise TeamError("DRAFT_GENERATION_FAILED", "The model returned an invalid or incomplete draft.", status=502) from exc
