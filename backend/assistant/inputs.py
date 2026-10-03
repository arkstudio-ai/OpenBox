"""Human input acceptance; GETs and replay never dispatch new execution."""
from sqlalchemy import select

from agent.inbox import _validate_input, accept_inbox_item_locked, get_inbox_item
from assistant.commands import _authority, accept_task_command, command_digest
from assistant.identities import inbox_key
from assistant.policy import AssistantError, main_session_locked
from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantCommand, AssistantTask
from session.internal_parts import begin_session_write


async def accept_turn(*, user_id, workspace_id, main_id, client_id, text,
                      attachments=(), model=None, variant=None, variant_explicit=False,
                      video_model=None, video_resolution=None):
    if not client_id or len(client_id) > 64:
        raise ValueError("A stable client_id of 1..64 characters is required")
    if not text.strip():
        raise ValueError("Input text is required")
    _validate_input(prompt=text, attachments=attachments, client_id=client_id, output_format=None)
    async with get_db_session() as db:
        await begin_session_write(db)
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        main = await main_session_locked(db, user_id, workspace_id, lock=True)
        await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        key = inbox_key("assistant-turn", main_id, client_id)
        existing = await db.scalar(select(AgentInboxItem).where(AgentInboxItem.session_id == main_id,
            AgentInboxItem.user_id == user_id, AgentInboxItem.client_id == key))
        # Resolve defaults only on first acceptance. A changed Session default
        # cannot turn a network retry of the same request into a new command.
        chosen_model = model if model is not None else existing.model if existing else main.model
        chosen_variant = variant if variant_explicit or variant is not None else existing.variant if existing else main.variant
        video = video_model if video_model is not None else existing.video_model if existing else main.video_model
        resolution = (video_resolution if video_resolution is not None else
                      existing.video_resolution if existing else main.video_resolution)
        digest = command_digest({"text": text, "attachments": list(attachments), "model": model,
            "variant": variant, "variant_explicit": variant_explicit, "delivery": "followup"})
        if video_model is not None or video_resolution is not None:
            digest = command_digest({"base": digest, "video_model": video_model, "video_resolution": video_resolution})
        receipt = await accept_inbox_item_locked(db, main, delivery="followup", prompt=text,
            attachments=attachments, client_id=key, agent="assistant", model=chosen_model, variant=chosen_variant,
            video_model=video, video_resolution=resolution,
            origin="human", origin_ref={"actor_user_id": user_id, "entrypoint": "assistant_turn",
                "client_message_id": client_id, "request_digest": digest})
        return {"inbox_id": receipt.id, "assistant_session_id": main_id, "client_id": client_id,
                "inbox_client_id": key, "state": receipt.state, "delivery": "followup",
                "message_id": receipt.message_id, "run_id": receipt.run_id, "generation": receipt.generation}


async def accept_session_input(session, *, user_id, text, client_id, delivery=None,
                               attachments=(), model=None, variant=None, variant_explicit=False,
                               agent=None, expected_revision=None, has_unsupported_options=False,
                               video_model=None, video_resolution=None):
    """Compatibility adapter: linked execution input must still advance Task intent.

    Old clients do not know Task revisions. Only that compatibility path reads
    the current revision; command replay retains the original expected value.
    The command transaction still rejects a racing control/input change.
    """
    if not getattr(session, "workspace_id", None):
        return None
    async with get_db_session() as db:
        task = await db.scalar(select(AssistantTask).where(AssistantTask.execution_session_id == session.id,
            AssistantTask.user_id == user_id, AssistantTask.workspace_id == session.workspace_id))
        if session.kind != "assistant" and task is None:
            return None
        if not client_id:
            raise AssistantError(400, "ASSISTANT_CLIENT_ID_REQUIRED", "Assistant inputs require a stable client message ID")
        if delivery not in {None, "followup"}:
            raise AssistantError(409, "ASSISTANT_FOLLOWUP_REQUIRED", "Use followup for assistant inputs")
        if has_unsupported_options or agent not in {None, session.agent}:
            raise AssistantError(409, "ASSISTANT_INPUT_OPTIONS", "These input options are not supported on this assistant path")
        if task:
            key = inbox_key("assistant-legacy-input", session.id, client_id)
            await _authority(db, user_id=user_id, workspace_id=session.workspace_id, main_id=task.assistant_session_id)
            existing = await db.scalar(select(AssistantCommand).where(AssistantCommand.actor_user_id == user_id,
                AssistantCommand.workspace_id == session.workspace_id,
                AssistantCommand.assistant_session_id == task.assistant_session_id,
                AssistantCommand.idempotency_key == key))
            revision = expected_revision if expected_revision is not None else (
                existing.expected_revision if existing else task.control_revision)
    if task:
        receipt = await accept_task_command(user_id=user_id, workspace_id=session.workspace_id,
            main_id=task.assistant_session_id, idempotency_key=key, task_id=task.id,
            expected_revision=revision, prompt=text, attachments=attachments, model=model,
            variant=variant, variant_explicit=variant_explicit, client_message_id=client_id,
            video_model=video_model, video_resolution=video_resolution)
    else:
        receipt = await accept_turn(user_id=user_id, workspace_id=session.workspace_id, main_id=session.id,
            client_id=client_id, text=text, attachments=attachments, model=model,
            variant=variant, variant_explicit=variant_explicit,
            video_model=video_model, video_resolution=video_resolution)
    return await get_inbox_item(receipt["inbox_id"], user_id=user_id, session_id=session.id)
