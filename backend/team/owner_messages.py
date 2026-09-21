"""User-authored supplements, atomically addressed to the run's coordinator."""
from typing import Annotated, Literal

from pydantic import Field

from agent.inbox import MAX_ATTACHMENTS, MAX_PROMPT_CHARS
from agent_catalog.schemas import Contract
from db.models.session import Session
from team.errors import TeamError
from team.journal import Actor, command, digest


class OwnerMessage(Contract):
    text: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    attachments: list[Annotated[str, Field(min_length=1, max_length=64)]] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)
    delivery: Literal["steer", "followup"] = "steer"


async def send(run_id: str, actor: Actor, key: str, message: OwnerMessage) -> dict:
    if actor.kind != "user":
        raise TeamError("AUTHORITY_REVOKED", "Only the owner may supply user instructions.", status=403)

    async def accept(writer):
        if writer.state["run"]["state"] not in {"running", "waiting", "paused"}:
            raise TeamError("TEAM_CLOSED", "This run is not accepting additional instructions.")
        # Lock order is run -> Session, as in every team runtime enqueue.
        root = await writer.db.get(Session, writer.run.root_session_id, with_for_update=True)
        if (root is None or root.is_deleted or root.kind == "team_member"
                or root.user_id != actor.owner_user_id or root.workspace_id != actor.workspace_id
                or root.project_id != writer.run.project_id):
            raise TeamError("TEAM_NOT_FOUND", "The coordinator session is not accessible.", status=404)
        from agent.inbox import InboxAttachmentError, InboxIdempotencyConflict, accept_user_input_locked
        try:
            receipt = await accept_user_input_locked(
                writer.db, session_row=root,
                delivery=message.delivery, prompt=message.text, attachments=message.attachments,
                client_id="owner:" + digest([run_id, writer.key])[:48],
                agent=root.agent, model=root.model, variant=root.variant,
                video_model=root.video_model, video_resolution=root.video_resolution,
            )
        except InboxIdempotencyConflict as exc:
            raise TeamError("IDEMPOTENCY_CONFLICT", str(exc)) from exc
        except (InboxAttachmentError, ValueError) as exc:
            raise TeamError("INVALID_OWNER_MESSAGE", str(exc), status=422) from exc
        writer.append("team.notice", "notice", {"id": receipt.id, "code": "OWNER_MESSAGE_ACCEPTED",
            "inbox_id": receipt.id, "session_id": root.id})
        return {"id": receipt.id, "session_id": root.id, "state": receipt.state,
            "seq": writer.state["seq"], "team_state": writer.state["run"]["state"]}

    result = await command(run_id, actor, key, {"action": "owner_message", **message.model_dump(mode="json")}, accept)
    # The recovery scanner also sees this inbox if the process exits here.
    # A paused run retains its input until an explicit owner resume.
    from team.scheduler import schedule
    schedule(run_id, actor)
    from bus import bus
    bus.publish("team.run.updated", {"userId": actor.owner_user_id, "sessionId": result["session_id"],
        "teamRunId": run_id, "seq": result["seq"], "state": result["team_state"]})
    return result
