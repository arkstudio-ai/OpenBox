"""Writing into a conversation without running the agent.

The first video and the Light opening cards both need a fixed assistant
message in a real session: the transcript stays the single source of what the
person was told, and the agent reads it on the next turn like any other
message. No LLM call, no run lease.
"""
from __future__ import annotations


async def say(session_id: str, message_id: str, user_id: str, text: str, *, channel: str = "final") -> None:
    """Append one text part to an assistant message and publish it to open clients."""
    from models.message import TextPart
    from session.session import save_part
    await save_part(TextPart(text=text, channel=channel, session_id=session_id, message_id=message_id),
                    is_new=True, user_id=user_id)


async def finish(session_id: str, message_id: str, user_id: str) -> None:
    """Close an assistant message the way a completed turn does."""
    from models.message import MessageInfo
    from session.session import update_message_info
    await update_message_info(MessageInfo(id=message_id, session_id=session_id, role="assistant", finish="stop"),
                              user_id=user_id)


async def exchange(session_id: str, user_id: str, *, user_text: str, assistant_text: str,
                   agent: str, model: str | None, entrypoint: str) -> tuple[str, str]:
    """A synthetic user line answered by a fixed assistant message; returns both message ids.

    The user line is the person's choice echoed back (「餐品展示 · 烟火气特写」), so
    the agent and the transcript see what was picked without a typed prompt.
    """
    from session import session as session_mod
    user_message = await session_mod.create_user_message(
        session_id=session_id, text=user_text, agent=agent, model=model, synthetic=True, user_id=user_id,
        origin="human", origin_ref={"actor_user_id": user_id, "entrypoint": entrypoint},
    )
    assistant = await session_mod.create_assistant_message(session_id, user_message.id, model_id=model,
                                                           agent=agent, user_id=user_id)
    await say(session_id, assistant.id, user_id, assistant_text)
    await finish(session_id, assistant.id, user_id)
    return user_message.id, assistant.id
