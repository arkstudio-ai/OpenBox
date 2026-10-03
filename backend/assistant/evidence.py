"""Source validity for both direct reads and answers derived from those reads."""
import json

from sqlalchemy import select

from assistant.commands import command_digest, task_locked
from assistant.policy import AssistantError
from assistant.results import part_hash, validate_result_source, validate_source_asset
from db.models.agent_event import AgentEvent
from db.models.assistant import AssistantTask, TaskResult
from db.models.message import Message
from db.models.part import Part
from session.agent_event_log import append_agent_event_locked


def projection_digest(value: dict) -> str:
    return command_digest(json.loads(json.dumps(value, default=str)))


async def validate_source_ref(db, ref, *, user_id, workspace_id, main_id, visited=None, depth=0, validation=None):
    validation = {"messages": set(), "refs": {}} if validation is None else validation
    key = command_digest(ref)
    if key in validation["refs"]:
        return validation["refs"][key]
    if depth > 64 or len(validation["refs"]) >= 200:
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
                                       main_id=main_id, visited=visited, depth=depth + 1, validation=validation)
    elif part.data.get("origin") == "task_result":
        result = await db.get(TaskResult, (part.data.get("origin_ref") or {}).get("result_id"))
        if result is None:
            raise AssistantError(410, "ASSISTANT_SOURCE_UNAVAILABLE", "The original result is unavailable")
        await validate_result_source(db, result, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
    if len(validation["refs"]) >= 200:
        raise AssistantError(410, "ASSISTANT_SOURCE_UNVERIFIED", "Source dependency exceeds the read budget")
    validation["refs"][key] = part
    return part


async def validate_message_sources(db, message, *, user_id, workspace_id, main_id, visited=None, depth=0, validation=None):
    """A saved answer never substitutes for its still-authorized evidence."""
    visited = set() if visited is None else visited
    validation = {"messages": set(), "refs": {}} if validation is None else validation
    if message.id in validation["messages"]:
        return
    if message.id in visited or depth > 64 or len(validation["messages"]) >= 200:
        raise AssistantError(410, "ASSISTANT_SOURCE_UNVERIFIED", "Source dependency could not be verified")
    visited = visited | {message.id}
    report = await db.scalar(select(TaskResult).where(TaskResult.processed_message_id == message.id))
    if report:
        await validate_result_source(db, report, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
    manifest = await db.scalar(select(AgentEvent).where(AgentEvent.session_id == main_id,
        AgentEvent.user_id == user_id, AgentEvent.message_id == message.id,
        AgentEvent.kind == "assistant.message.committed"))
    if (manifest is None or manifest.payload.get("provenance_version") != 2 or not manifest.payload.get("context_verified")
            or message.summary or message.error or message.finish != "stop"):
        raise AssistantError(410, "ASSISTANT_SOURCE_UNVERIFIED", "This derived answer has no validated source manifest")
    refs = manifest.payload.get("source_refs", [])
    if len(refs) > 200:
        raise AssistantError(410, "ASSISTANT_SOURCE_UNVERIFIED", "Source dependency exceeds the read budget")
    for ref in refs:
        await validate_source_ref(db, ref, user_id=user_id, workspace_id=workspace_id,
                                  main_id=main_id, visited=visited, depth=depth + 1, validation=validation)
    await validate_business_reads(db, manifest.payload.get("business_reads", []),
                                  user_id=user_id, workspace_id=workspace_id, main_id=main_id)
    validation["messages"].add(message.id)


async def validate_business_reads(db, reads, *, user_id, workspace_id, main_id):
    if len(reads) > 200:
        raise AssistantError(410, "ASSISTANT_SOURCE_UNVERIFIED", "Business sources exceed the read budget")
    for read in reads:
        from assistant.reads import get_task, list_projects, list_sessions, list_tasks
        function = {"projects.list": list_projects, "sessions.list": list_sessions,
                    "tasks.get": get_task, "tasks.list": list_tasks}.get(read.get("operation"))
        if function is None:
            raise AssistantError(410, "ASSISTANT_SOURCE_UNVERIFIED", "Unknown business source")
        value = await function(user_id=user_id, workspace_id=workspace_id, main_id=main_id,
                               db=db, **read["arguments"])
        if projection_digest(value) != read.get("digest"):
            raise AssistantError(410, "ASSISTANT_SOURCE_CHANGED", "Business state changed; read it again")


async def record_answer_sources_locked(db, main, message, *, run_fence) -> None:
    if main.kind != "assistant" or not run_fence or message.summary or message.error or message.finish != "stop":
        return
    from assistant.context_sources import consumed_contexts
    contexts, complete = await consumed_contexts(db, main, message, run_fence=run_fence)
    refs = {}
    business = {}
    for context in contexts:
        for ref in context.get("source_refs", []):
            refs[command_digest(ref)] = ref  # Preserve conflicting versions, never launder an earlier read.
        if context["mode"] == "ordinary":
            for read in context.get("business_reads", []):
                business[command_digest(read)] = read
    await append_agent_event_locked(db, main, kind="assistant.message.committed", payload={
        "provenance_version": 2, "context_verified": complete,
        "message_id": message.id, "source_refs": list(refs.values()), "business_reads": list(business.values()),
    }, run_fence=run_fence, message_id=message.id, idempotency_key=f"assistant-answer:{message.id}")
