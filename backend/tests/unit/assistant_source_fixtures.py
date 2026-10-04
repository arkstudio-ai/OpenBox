"""Simulate a provider response boundary for tests that bypass the processor."""
from agent.loop import _to_llm_messages
from assistant.context_sources import record_provider_context
from assistant.evidence import projection_digest
from assistant.projection import project_main_messages
from session.agent_event_log import checkpoint_model_request, load_canonical_model_surface


async def consume_lease_context(lease, message):
    from db.base import get_db_session
    from db.models.session import Session
    from tool.tool import ToolContext
    async with get_db_session() as db:
        session = await db.get(Session, lease.session_id)
    ctx = ToolContext(user_id=lease.user_id, workspace_id=session.workspace_id, project_id=session.project_id,
        session_id=session.id, agent_id="assistant", message_id=message.id,
        run_id=lease.run_id, run_generation=lease.generation)
    return await consume_context(ctx)


async def consume_context(ctx, *, messages=None, respond=True):
    surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
    projected = await project_main_messages(list(surface.messages), ctx=ctx)
    if messages is None:
        messages = _to_llm_messages(projected, user_id=ctx.user_id, assistant_projection_verified=True)
    ctx._assistant_context["messages_digest"] = projection_digest(messages)
    await checkpoint_model_request(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence,
        request_id=f"fixture:{ctx.message_id}:{surface.event_sequence}", model_id="test/model",
        provider_binding_digest="a" * 64, tool_schema_digest="b" * 64, prompt_shape_digest="c" * 64,
        expected_event_sequence=surface.event_sequence, expected_event_digest=surface.event_digest,
        message_id=ctx.message_id, assistant_context=ctx._assistant_context)
    if respond:
        await record_provider_context(ctx, messages)
    return messages
