"""Explicit retry of an input that never reached the model.

The original failed input stays excluded and auditable. The user's retry is a
new, idempotent submission of its exact text and asset IDs in the same task;
normal admission, entitlement and strict attachment delivery still apply.
"""
from hashlib import sha256

from fastapi import HTTPException
from sqlalchemy import JSON, or_, select, type_coerce

from db.base import get_db_session
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantTask
from db.models.message import Message


async def retry_body(session, user_id, message_id, options):
    from api.sessions import PromptBody

    async with get_db_session() as db:
        failed = await db.scalar(select(Message).where(Message.id == message_id,
            Message.session_id == session.id, Message.user_id == user_id, Message.role == "assistant"))
        if not failed or (failed.error or {}).get("name") != "AttachmentDeliveryError":
            return None
        sources = list(await db.scalars(select(AgentInboxItem).where(
            AgentInboxItem.result_message_id == message_id, AgentInboxItem.session_id == session.id,
            AgentInboxItem.user_id == user_id, AgentInboxItem.state == "settled",
            AgentInboxItem.outcome == "delivery_error")))
        source = sources[0] if len(sources) == 1 else None
        if source is None or not source.attachments or (source.error or {}).get("retryable") is not True:
            raise HTTPException(409, {"code": "ATTACHMENT_RETRY_UNAVAILABLE",
                "message": "原附件已不可用，请检查附件后再继续原任务。"})
        # One public error has at most one explicit retry, including double
        # clicks and retries after a lost HTTP response. A later delivery error
        # has its own identity and therefore a fresh bounded delivery budget.
        client_id = "attachment-retry:" + sha256(message_id.encode()).hexdigest()[:40]
        accepted = await db.scalar(select(AgentInboxItem.id).where(
            AgentInboxItem.session_id == session.id, AgentInboxItem.user_id == user_id,
            or_(AgentInboxItem.client_id == client_id,
                type_coerce(AgentInboxItem.origin_ref, JSON)["client_message_id"].as_string() == client_id)))
        latest = await db.scalar(select(Message).where(Message.session_id == session.id,
            Message.user_id == user_id, Message.role == "user").order_by(
                Message.created_at.desc(), Message.id.desc()).limit(1))
        if latest is None or (latest.id != source.message_id and latest.client_message_id != client_id):
            raise HTTPException(409, {"code": "ATTACHMENT_RETRY_STALE",
                "message": "这个请求之后已有新的消息，请继续当前任务，避免重做旧请求。"})
        task = await db.scalar(select(AssistantTask).where(AssistantTask.execution_session_id == session.id,
            AssistantTask.user_id == user_id, AssistantTask.workspace_id == session.workspace_id,
            AssistantTask.archived_at.is_(None)))
        # Let the managed adapter recover the first accepted command's revision
        # on an idempotent replay; fresh acceptance still checks its snapshot.
        replay = accepted is not None
        return PromptBody(text=source.prompt, attachments=list(source.attachments),
            client_message_id=client_id, delivery="followup", agent=source.agent,
            model=(options.model if options and options.model else source.model),
            video_model=(options.video_model if options and options.video_model else source.video_model),
            video_resolution=(options.video_resolution if options and options.video_resolution else source.video_resolution),
            variant=source.variant,
            expected_task_revision=task.control_revision if task and not replay else None)
