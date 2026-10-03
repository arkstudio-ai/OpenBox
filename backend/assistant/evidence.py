"""Source validity for both direct reads and answers derived from those reads."""
import json

from sqlalchemy import select

from assistant.commands import command_digest, task_locked
from assistant.policy import AssistantError
from assistant.results import part_hash, validate_result_source, validate_source_asset
from db.models.agent_event import AgentEvent
from db.models.agent_inbox import AgentInboxItem
from db.models.assistant import AssistantTask, TaskResult
from db.models.message import Message
from db.models.part import Part
from session.agent_event_log import append_agent_event_locked

READ_EVENTS = ("assistant.history.read", "assistant.result.sources_read", "assistant.report.sources_read")


def projection_digest(value: dict) -> str:
    return command_digest(json.loads(json.dumps(value, default=str)))


async def validate_source_ref(db, ref, *, user_id, workspace_id, main_id, visited=None, depth=0):
    if depth > 8:
        raise AssistantError(410, "ASSISTANT_SOURCE_UNVERIFIED", "Source dependency depth exceeds the read budget")
    session_id = ref.get("session_id")
    if session_id != main_id:
        task_id = await db.scalar(select(AssistantTask.id).where(AssistantTask.assistant_session_id == main_id,
            AssistantTask.execution_session_id == session_id, AssistantTask.user_id == user_id,
            AssistantTask.workspace_id == workspace_id))
        if task_id is None:
            raise AssistantError(410, "ASSISTANT_SOURCE_UNAVAILABLE", "Source is no longer linked to this assistant")
        await task_locked(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id, task_id=task_id)
    part = await db.scalar(select(Part).join(Message, Message.id == Part.message_id).where(
        Part.id == ref.get("part_id"), Part.message_id == ref.get("message_id"),
        Part.session_id == session_id, Part.user_id == user_id, Message.session_id == session_id,
        Message.user_id == user_id))
    if part is None or part_hash(part) != ref.get("content_hash"):
        raise AssistantError(410, "ASSISTANT_SOURCE_CHANGED", "Original evidence changed or is unavailable")
    await validate_source_asset(db, part, user_id=user_id, workspace_id=workspace_id)
    message = await db.get(Message, part.message_id)
    if session_id == main_id and message.role == "assistant":
        await validate_message_sources(db, message, user_id=user_id, workspace_id=workspace_id,
                                       main_id=main_id, visited=visited, depth=depth + 1)
    return part


async def validate_message_sources(db, message, *, user_id, workspace_id, main_id, visited=None, depth=0):
    """A saved answer never substitutes for its still-authorized evidence."""
    visited = set() if visited is None else visited
    if message.id in visited or depth > 8:
        raise AssistantError(410, "ASSISTANT_SOURCE_UNVERIFIED", "Source dependency could not be verified")
    visited = visited | {message.id}
    report = await db.scalar(select(TaskResult).where(TaskResult.processed_message_id == message.id))
    if report:
        await validate_result_source(db, report, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        return
    manifest = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == main_id,
        AgentEvent.user_id == user_id, AgentEvent.message_id == message.id,
        AgentEvent.kind == "assistant.message.committed"))
    if manifest is None or message.summary or message.error or message.finish != "stop":
        raise AssistantError(410, "ASSISTANT_SOURCE_UNVERIFIED", "This derived answer has no validated source manifest")
    refs = manifest.payload.get("source_refs", [])
    if len(refs) > 200:
        raise AssistantError(410, "ASSISTANT_SOURCE_UNVERIFIED", "Source dependency exceeds the read budget")
    for ref in refs:
        await validate_source_ref(db, ref, user_id=user_id, workspace_id=workspace_id,
                                  main_id=main_id, visited=visited, depth=depth + 1)
    for read in manifest.payload.get("business_reads", []):
        from assistant.reads import get_task, list_projects, list_sessions, list_tasks
        function = {"projects.list": list_projects, "sessions.list": list_sessions,
                    "tasks.get": get_task, "tasks.list": list_tasks}.get(read.get("operation"))
        if function is None:
            raise AssistantError(410, "ASSISTANT_SOURCE_UNVERIFIED", "Unknown business source")
        value = await function(user_id=user_id, workspace_id=workspace_id, main_id=main_id, **read["arguments"])
        if projection_digest(value) != read.get("digest"):
            raise AssistantError(410, "ASSISTANT_SOURCE_CHANGED", "Business state changed; read it again")


async def record_answer_sources_locked(db, main, message, *, run_fence) -> None:
    if main.kind != "assistant" or not run_fence or message.summary or message.error or message.finish != "stop":
        return
    inputs = list((await db.scalars(select(AgentInboxItem).where(AgentInboxItem.session_id == main.id,
        AgentInboxItem.user_id == main.user_id, AgentInboxItem.run_id == run_fence[1],
        AgentInboxItem.generation == run_fence[2], AgentInboxItem.state.in_(("claimed", "settled"))))).all())
    if any(item.origin == "task_result" for item in inputs):
        return
    refs = {}
    input_ids = [item.message_id for item in inputs if item.message_id]
    for part in (await db.scalars(select(Part).where(Part.session_id == main.id,
        Part.user_id == main.user_id, Part.message_id.in_(input_ids), Part.type.in_(("text", "file"))))).all():
        refs[part.id] = {"session_id": main.id, "message_id": part.message_id, "part_id": part.id,
                         "content_hash": part_hash(part)}
    reads = list((await db.scalars(select(AgentEvent).where(AgentEvent.session_id == main.id,
        AgentEvent.user_id == main.user_id, AgentEvent.run_id == run_fence[1],
        AgentEvent.generation == run_fence[2], AgentEvent.kind.in_((*READ_EVENTS, "assistant.business.read")))
        .order_by(AgentEvent.sequence).limit(500))).all())
    business = {}
    for event in reads:
        if event.kind == "assistant.business.read":
            key = command_digest({"operation": event.payload["operation"], "arguments": event.payload["arguments"]})
            business[key] = event.payload
        for ref in event.payload.get("source_refs", []):
            refs[ref["part_id"]] = ref
    await append_agent_event_locked(db, main, kind="assistant.message.committed", payload={
        "message_id": message.id, "source_refs": list(refs.values()), "business_reads": list(business.values()),
    }, run_fence=run_fence, message_id=message.id, idempotency_key=f"assistant-answer:{message.id}")
