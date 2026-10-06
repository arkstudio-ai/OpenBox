"""Simulate one provider request for tests that bypass the processor.

V2 (PERSONAL_ASSISTANT_DESIGN_V2.md 4.2): the request is the ordinary main
projection plus the turn-mode checkpoint; nothing is recorded for later
re-validation, so there is no separate response receipt.
"""
from agent.loop import _to_llm_messages
from assistant.projection import project_main_messages
from session.agent_event_log import checkpoint_model_request, load_canonical_model_surface


async def consume_context(ctx, *, messages=None):
    """Project, checkpoint and return the provider messages for ``ctx``'s run."""
    surface = await load_canonical_model_surface(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence)
    projected = await project_main_messages(list(surface.messages), ctx=ctx)
    if messages is None:
        messages = _to_llm_messages(projected, user_id=ctx.user_id, assistant_projection_verified=True)
    await checkpoint_model_request(ctx.session_id, user_id=ctx.user_id, run_fence=ctx.run_fence,
        request_id=f"fixture:{ctx.message_id}:{surface.event_sequence}", model_id="test/model",
        provider_binding_digest="a" * 64, tool_schema_digest="b" * 64, prompt_shape_digest="c" * 64,
        expected_event_sequence=surface.event_sequence, expected_event_digest=surface.event_digest,
        message_id=ctx.message_id, assistant_context=ctx._assistant_context)
    return messages
