"""Recover final prose after a crash between team_finish and Part persistence."""
from db.base import get_db_session
from db.models.message import Message
from db.models.part import Part
from models.message import TextPart
from session.session import save_part
from team.errors import TeamError
from team.journal import owned_run, read_state
from tool.tool import final_response_part_id


async def recover_final_response(run_id, actor) -> None:
    async with get_db_session() as db:
        run = await owned_run(db, run_id, actor)
        state = await read_state(db, run)
        publication = state["run"].get("final_response")
        if run.state != "completing" or not publication:
            return
        part_id = final_response_part_id(publication["tool_part_id"])
        message = await db.get(Message, publication["message_id"])
        tool = await db.get(Part, publication["tool_part_id"])
        if (message is None or message.user_id != actor.owner_user_id or message.session_id != run.root_session_id
                or message.role != "assistant" or tool is None or tool.message_id != message.id
                or tool.session_id != run.root_session_id or tool.user_id != actor.owner_user_id
                or tool.data.get("tool") != "team_finish"):
            raise TeamError("FINAL_RESPONSE_INVALID", "The final answer does not belong to this team's closing message.")
        part = TextPart(id=part_id, text=state["run"]["final_summary"], channel="final",
            message_id=message.id, session_id=run.root_session_id)
        existing = await db.get(Part, part_id)
        if existing is not None:
            if (existing.session_id != part.session_id or existing.message_id != part.message_id
                    or existing.user_id != actor.owner_user_id or existing.data.get("text") != part.text
                    or existing.data.get("channel") != "final"):
                raise TeamError("FINAL_RESPONSE_INVALID", "The existing final answer conflicts with its committed receipt.")
            return
    # Called only after every Driver released its lease. The durable message
    # and stable Part ID preserve ownership, exactly-once create and replay.
    await save_part(part, is_new=True, user_id=actor.owner_user_id)
