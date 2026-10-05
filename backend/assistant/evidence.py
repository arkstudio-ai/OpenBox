"""Source validity for both direct reads and answers derived from those reads."""
import json

from sqlalchemy import and_, join, literal, select

from assistant.commands import command_digest
from assistant.command_sources import command_validation, validation_original
from assistant.policy import AssistantError
from assistant.results import part_hash, part_identity, validate_result_source, validate_source_asset
from assistant.source_scope import execution_scope, project_scope
from db.models.agent_event import AgentEvent
from db.models.assistant import AssistantTask, TaskResult
from db.models.message import Message
from db.models.part import Part
from db.models.project import Project
from db.models.session import Session
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
    snapshot_checks = validation.get("snapshot_checks")
    if snapshot_checks is None:
        part, message = await validation_original(db, "source_original", (user_id, workspace_id, main_id), ref,
            lambda: _source_original(db, ref, user_id=user_id, workspace_id=workspace_id, main_id=main_id),
            fingerprint=lambda value: part_identity(value[0]))
    else:
        part, message = await snapshot_checks.check(db, "source_original", (user_id, workspace_id, main_id), ref,
            lambda: _source_original(db, ref, user_id=user_id, workspace_id=workspace_id, main_id=main_id))
    # Only the original row/scope lookup is shared between answers. Descend
    # into its provenance again with this answer's own path and budgets.
    if ref.get("session_id") == main_id and message.role == "assistant":
        await validate_message_sources(db, message, user_id=user_id, workspace_id=workspace_id,
                                       main_id=main_id, visited=visited, depth=depth + 1, validation=validation)
    elif part.data.get("origin") == "task_result":
        result = await db.get(TaskResult, (part.data.get("origin_ref") or {}).get("result_id"))
        if result is None:
            raise AssistantError(410, "ASSISTANT_SOURCE_UNAVAILABLE", "The original result is unavailable")
        await validate_result_source(db, result, user_id=user_id, workspace_id=workspace_id, main_id=main_id,
                                     snapshot_checks=snapshot_checks)
    elif (part.data.get("origin_ref") or {}).get("execution_mode") == "coordination":
        from assistant.commands import _authority
        from assistant.continuation import validate_reference
        main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id,
                                snapshot_checks=snapshot_checks)
        origin = part.data["origin_ref"]
        await validate_reference(db, main, {**origin, "coordination_inbox_id": origin.get("inbox_id")},
                                 snapshot_checks=snapshot_checks)
    if ref.get("session_id") != main_id:
        from assistant.execution_sources import validate_execution_message
        await validate_execution_message(db, message, user_id=user_id, workspace_id=workspace_id,
            main_id=main_id, snapshot_checks=snapshot_checks)
    if len(validation["refs"]) >= 200:
        raise AssistantError(410, "ASSISTANT_SOURCE_UNVERIFIED", "Source dependency exceeds the read budget")
    validation["refs"][key] = part
    return part


async def _source_original(db, ref, *, user_id, workspace_id, main_id):
    session_id = ref.get("session_id")
    original = join(Part, Message, and_(Message.id == Part.message_id,
        Message.session_id == session_id, Message.user_id == user_id))
    source = and_(Part.id == ref.get("part_id"), Part.message_id == ref.get("message_id"),
                  Part.session_id == session_id, Part.user_id == user_id)
    if session_id != main_id:
        # Keep missing scope facts without selecting a Part outside that scope.
        # Validation order is link -> execution -> project -> original bytes.
        anchor = select(literal(1).label("one")).subquery()
        row = (await db.execute(select(AssistantTask.id.label("task_id"),
            Session.id.label("execution_id"), Project.id.label("project_id"), Part, Message)
            .select_from(anchor).outerjoin(AssistantTask, and_(AssistantTask.assistant_session_id == main_id,
            AssistantTask.execution_session_id == session_id, AssistantTask.user_id == user_id,
            AssistantTask.workspace_id == workspace_id))
            .outerjoin(Session, execution_scope(user_id=user_id, workspace_id=workspace_id))
            .outerjoin(Project, project_scope(user_id=user_id, workspace_id=workspace_id))
            .outerjoin(original, and_(source, Session.id.is_not(None), Project.id.is_not(None)))
            .execution_options(populate_existing=True))).one()
        if row.task_id is None:
            raise AssistantError(410, "ASSISTANT_SOURCE_UNAVAILABLE", "Source is no longer linked to this assistant")
        if row.execution_id is None:
            raise AssistantError(409, "ASSISTANT_EXECUTION_UNAVAILABLE", "The original execution Session is unavailable")
        if row.project_id is None:
            raise AssistantError(404, "ASSISTANT_PROJECT_UNAVAILABLE", "The owned project is unavailable")
        part, message = row.Part, row.Message
    else:
        row = (await db.execute(select(Part, Message).select_from(original).where(source)
            .execution_options(populate_existing=True))).one_or_none()
        part, message = row if row is not None else (None, None)
    if part is None or part_hash(part) != ref.get("content_hash"):
        raise AssistantError(410, "ASSISTANT_SOURCE_CHANGED", "Original evidence changed or is unavailable")
    await validate_source_asset(db, part, user_id=user_id, workspace_id=workspace_id)
    return part, message


async def _message_evidence(db, message_id, *, user_id, main_id):
    # The one-row anchor preserves either missing side: a report alone still
    # needs validation before the absent-manifest refusal, as in the two reads.
    anchor = select(literal(message_id).label("message_id")).subquery()
    return (await db.execute(select(TaskResult, AgentEvent).select_from(anchor)
        .outerjoin(TaskResult, TaskResult.processed_message_id == anchor.c.message_id)
        .outerjoin(AgentEvent, and_(AgentEvent.session_id == main_id,
            AgentEvent.user_id == user_id, AgentEvent.message_id == anchor.c.message_id,
            AgentEvent.kind == "assistant.message.committed")))).first()


@command_validation
async def validate_message_sources(db, message, *, user_id, workspace_id, main_id, visited=None, depth=0,
                                   validation=None, snapshot_checks=None):
    """A saved answer never substitutes for its still-authorized evidence."""
    visited = set() if visited is None else visited
    validation = {"messages": set(), "refs": {}, "snapshot_checks": snapshot_checks} if validation is None else validation
    if message.id in validation["messages"]:
        return
    if message.id in visited or depth > 64 or len(validation["messages"]) >= 200:
        raise AssistantError(410, "ASSISTANT_SOURCE_UNVERIFIED", "Source dependency could not be verified")
    visited = visited | {message.id}
    if message.summary:
        from assistant.compaction import validate_compaction_message
        await validate_compaction_message(db, message, user_id=user_id, workspace_id=workspace_id, main_id=main_id,
            visited=visited, depth=depth, validation=validation)
        validation["messages"].add(message.id)
        return
    snapshot_checks = validation.get("snapshot_checks")
    if snapshot_checks is None:
        report, manifest = await validation_original(db, "message_evidence", (user_id, workspace_id, main_id), message.id,
            lambda: _message_evidence(db, message.id, user_id=user_id, main_id=main_id))
    else:
        report, manifest = await snapshot_checks.check(db, "message_evidence", (user_id, workspace_id, main_id), message.id,
            lambda: _message_evidence(db, message.id, user_id=user_id, main_id=main_id))
    if report:
        await validate_result_source(db, report, user_id=user_id, workspace_id=workspace_id, main_id=main_id,
                                     snapshot_checks=validation.get("snapshot_checks"))
    if (manifest is None or manifest.payload.get("provenance_version") != 2 or not manifest.payload.get("context_verified")
            or message.summary or message.error or message.finish != "stop"):
        raise AssistantError(410, "ASSISTANT_SOURCE_UNVERIFIED", "This derived answer has no validated source manifest")
    refs = manifest.payload.get("source_refs", [])
    continuation_refs = manifest.payload.get("continuation_refs", [])
    if not isinstance(continuation_refs, list) or len(continuation_refs) > 200:
        raise AssistantError(410, "ASSISTANT_SOURCE_UNVERIFIED", "Continuation dependency exceeds the read budget")
    if manifest.payload.get("continuation_refs"):
        from assistant.commands import _authority
        from assistant.continuation import validate_reference
        main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id,
                                snapshot_checks=snapshot_checks)
        for ref in manifest.payload["continuation_refs"]:
            await validate_reference(db, main, ref, snapshot_checks=snapshot_checks)
    if len(refs) > 200:
        raise AssistantError(410, "ASSISTANT_SOURCE_UNVERIFIED", "Source dependency exceeds the read budget")
    for ref in refs:
        await validate_source_ref(db, ref, user_id=user_id, workspace_id=workspace_id,
                                  main_id=main_id, visited=visited, depth=depth + 1, validation=validation)
    await validate_business_reads(db, manifest.payload.get("business_reads", []),
                                  user_id=user_id, workspace_id=workspace_id, main_id=main_id,
                                  snapshot_checks=validation.get("snapshot_checks"))
    if manifest.payload.get("decision_refs") or manifest.payload.get("task_snapshots"):
        from assistant.commands import _authority
        from assistant.decisions import validate_decision_refs
        if snapshot_checks is None:
            main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id)
        else:
            main = await snapshot_checks.check(db, "authority", (user_id, workspace_id, main_id), None,
                lambda: _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id))
        await validate_decision_refs(db, main, manifest.payload.get("decision_refs", []), validation=validation, depth=depth + 1)
        from assistant.task_context import validate_task_snapshots
        await validate_task_snapshots(db, main, manifest.payload.get("task_snapshots", []),
                                      snapshot_checks=validation.get("snapshot_checks"))
    validation["messages"].add(message.id)


async def validate_business_reads(db, reads, *, user_id, workspace_id, main_id, fresh=False, snapshot_checks=None):
    if not isinstance(reads, list) or len(reads) > 200:
        raise AssistantError(410, "ASSISTANT_SOURCE_UNVERIFIED", "Business sources exceed the read budget")
    for read in reads:
        if not isinstance(read, dict):
            raise AssistantError(410, "ASSISTANT_SOURCE_UNVERIFIED", "Unknown business source")
        async def validate_read():
            await _validate_business_read(db, read, user_id=user_id, workspace_id=workspace_id,
                main_id=main_id, fresh=fresh, snapshot_checks=None if fresh else snapshot_checks)
        if snapshot_checks is None or fresh:
            await validate_read()
        else:
            await snapshot_checks.check(db, "business", (user_id, workspace_id, main_id), read, validate_read)


async def _validate_business_read(db, read, *, user_id, workspace_id, main_id, fresh, snapshot_checks):
    if read.get("version") == 2:
        from assistant.business_context import validate
        from assistant.commands import _authority
        main = await _authority(db, user_id=user_id, workspace_id=workspace_id, main_id=main_id,
                                snapshot_checks=snapshot_checks)
        await validate(db, main, read, fresh=fresh, snapshot_checks=snapshot_checks)
        return
    if "version" in read:
        raise AssistantError(410, "ASSISTANT_SOURCE_UNVERIFIED", "Unknown business source version")
    # Legacy records lack the observed body. Preserve their original
    # strict check; never certify today's data as historical evidence.
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
    decisions = {}
    tasks = {}
    continuations = {}
    for context in contexts:
        if context.get("continuation_ref"):
            ref = context["continuation_ref"]
            continuations[command_digest(ref)] = ref
        for ref in context.get("task_snapshots", []):
            tasks[command_digest(ref)] = ref
        for ref in context.get("decision_refs", []):
            decisions[command_digest(ref)] = ref
        for ref in context.get("source_refs", []):
            refs[command_digest(ref)] = ref  # Preserve conflicting versions, never launder an earlier read.
        if context["mode"] in {"ordinary", "coordination"}:
            for read in context.get("business_reads", []):
                business[command_digest(read)] = read
    await append_agent_event_locked(db, main, kind="assistant.message.committed", payload={
        "provenance_version": 2, "context_verified": complete,
        "message_id": message.id, "source_refs": list(refs.values()), "business_reads": list(business.values()),
        "decision_refs": list(decisions.values()),
        "task_snapshots": list(tasks.values()),
        **({"continuation_refs": list(continuations.values())} if continuations else {}),
    }, run_fence=run_fence, message_id=message.id, idempotency_key=f"assistant-answer:{message.id}")
