"""Keep the provider evidence that produced a command, not just its human intent.

Human source references establish who requested work. They do not establish
where the model obtained a generated title, instructions or schedule. Freeze
that derivation before admitting the command so later consumers can recheck
the original sources instead of treating generated text as human input.
"""
from copy import deepcopy
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import json

from sqlalchemy import event, func, select

from assistant.policy import AssistantError
from db.models.message import Message

FIELDS = ("source_refs", "business_reads", "decision_refs", "task_snapshots")
MAX_DERIVATION_BYTES = 256_000
_path = ContextVar("assistant_command_source_path", default=())
_walk = ContextVar("assistant_command_source_walk", default=None)


class _CommandWalk:
    """Reuse completed branches within one validation or read-only SQL snapshot.

    The outer validation or explicit projection owns its lifetime. Each proof
    carries its descendants and longest path so a shallower visit cannot hide
    a later cycle or an over-depth path. Provider freshness checkpoints and
    admissions always start a new walk.
    """
    MAX_ENTRIES = 512

    def __init__(self, db):
        self.db = db
        self.transaction = db.sync_session.get_transaction()
        self.values = {}
        self.frames = []
        self.enabled = not (db.new or db.dirty or db.deleted)
        event.listen(db.sync_session, "after_flush", self.invalidate)
        event.listen(db.sync_session, "do_orm_execute", self.executing)

    def invalidate(self, *args):
        self.enabled = False
        self.values.clear()

    def executing(self, state):
        if not state.is_select:
            self.invalidate()

    def close(self):
        event.remove(self.db.sync_session, "after_flush", self.invalidate)
        event.remove(self.db.sync_session, "do_orm_execute", self.executing)

    def usable(self, db):
        current = (db is self.db and self.transaction is not None and self.transaction.is_active
                and db.sync_session.get_transaction() is self.transaction
                and not (db.new or db.dirty or db.deleted))
        if not current:
            self.invalidate()
        return self.enabled

    def include(self, proof):
        if not self.frames:
            return
        frame = self.frames[-1]
        if proof is None or frame[0] is None:
            frame[0] = None
            return
        frame[0].update(proof[0])
        frame[1] = max(frame[1], proof[1] + 1)
        if len(frame[0]) > self.MAX_ENTRIES:
            frame[0] = None

    def read(self, db, key, path):
        if not self.usable(db):
            self.values.clear()
            return False
        proof = self.values.get(key)
        if proof is None:
            return False
        if proof[0].intersection(path) or len(path) + proof[1] > 64:
            raise AssistantError(410, "ASSISTANT_COMMAND_SOURCE_UNVERIFIED", "Command source derivation is unavailable")
        self.include(proof)
        return True


@contextmanager
def _command_walk(db):
    existing = _walk.get()
    if existing is not None:
        yield existing
        return
    walk = _CommandWalk(db)
    token = _walk.set(walk)
    try:
        yield walk
    finally:
        walk.close()
        _walk.reset(token)


def command_validation(validate):
    """Share a walk within this validation or its enclosing read-only snapshot."""
    @wraps(validate)
    async def checked(db, *args, **kwargs):
        with _command_walk(db):
            return await validate(db, *args, **kwargs)
    return checked


def command_derivation_ref(command):
    from assistant.commands import command_digest
    proof = (command.source_ref or {}).get("derivation")
    return ({"version": 1, "command_id": command.id, "digest": command_digest(proof)}
            if proof is not None else None)


@command_validation
async def capture_command_derivation(db, main, source, part, human_refs):
    from assistant.commands import command_digest
    from assistant.context_sources import checked_context_locked, consumed_contexts

    message = await db.get(Message, part.message_id)
    contexts, verified = await consumed_contexts(db, main, message,
        run_fence=(main.id, source.run_id, source.generation))
    mode = "coordination" if source.coordination_inbox_id else "ordinary"
    if not verified or not contexts or any(context.get("mode") != mode for context in contexts):
        raise AssistantError(409, "ASSISTANT_COMMAND_CONTEXT_UNVERIFIED",
                             "A command needs its actual provider context in the bound execution mode")
    combined = {key: {} for key in FIELDS}
    for context in contexts:
        # Validate the exact observations consumed, not a refreshed substitute.
        # Ordinary progress can change; source scope and provenance cannot.
        checked = await checked_context_locked(db, main, context)
        if mode == "coordination":
            from assistant.continuation import validate_reference
            root = await validate_reference(db, main, checked.get("continuation_ref"))
            # The retained instructions may themselves be model-derived. Keep
            # those original dependencies alongside the coordinator's reads.
            for key, values in (root.source_ref.get("derivation") or {}).items():
                if key in FIELDS:
                    for ref in values:
                        combined[key][command_digest(ref)] = ref
                    if len(combined[key]) > 200:
                        raise AssistantError(409, "ASSISTANT_CONTEXT_BUDGET",
                                             "Command derivation exceeds its source budget")
        for key in FIELDS:
            for ref in checked.get(key, []):
                combined[key][command_digest(ref)] = ref
                if len(combined[key]) > 200:
                    raise AssistantError(409, "ASSISTANT_CONTEXT_BUDGET",
                                         "Command derivation exceeds its source budget")
    proof = {"version": 1, "main_id": main.id,
             **{key: list(refs.values()) for key, refs in combined.items()}}
    available = {(ref["session_id"], ref["message_id"], ref["part_id"], ref["content_hash"])
                 for ref in proof["source_refs"]}
    if any(tuple(ref[key] for key in ("session_id", "message_id", "part_id", "content_hash"))
           not in available for ref in human_refs):
        raise AssistantError(409, "ASSISTANT_COMMAND_CONTEXT_UNVERIFIED",
                             "Read the original human request before issuing this command")
    if len(json.dumps(proof, ensure_ascii=False).encode()) > MAX_DERIVATION_BYTES:
        raise AssistantError(409, "ASSISTANT_CONTEXT_BUDGET", "Command derivation exceeds its byte budget")
    return deepcopy(proof)


@command_validation
async def validate_command_derivation(db, main, command, *, snapshot_checks=None):
    """Recheck a recorded derivation without relabeling legacy or human commands."""
    reference = command.source_ref or {}
    if "derivation" not in reference:
        # Earlier isolated commands have no such manifest. This is explicitly
        # not proof that they can participate in newly enabled memory paths.
        return False
    proof = reference["derivation"]
    path = _path.get()
    if (command.id in path or len(path) >= 64 or not isinstance(proof, dict)
            or proof.get("version") != 1 or proof.get("main_id") != main.id
            or command.actor_user_id != main.user_id or command.workspace_id != main.workspace_id
            or command.assistant_session_id != main.id
            or any(not isinstance(proof.get(key), list) or len(proof[key]) > 200 for key in FIELDS)
            or len(json.dumps(proof, ensure_ascii=False).encode()) > MAX_DERIVATION_BYTES):
        raise AssistantError(410, "ASSISTANT_COMMAND_SOURCE_UNVERIFIED", "Command source derivation is unavailable")
    from assistant.commands import command_digest
    walk = _walk.get()
    key = (main.user_id, main.workspace_id, main.id, command.id, command_digest(reference))
    success = False
    token = None
    try:
        if walk.read(db, key, path):
            return True
        frame = [{command.id}, 1]
        walk.frames.append(frame)
        token = _path.set((*path, command.id))
        if reference.get("continuation_authority") is not None:
            from assistant.continuation import validate_reference
            await validate_reference(db, main, reference["continuation_authority"], snapshot_checks=snapshot_checks)
        from assistant.evidence import validate_business_reads, validate_source_ref
        from assistant.decisions import validate_decision_refs
        from assistant.task_context import validate_task_snapshots
        validation = {"messages": set(), "refs": {}, "snapshot_checks": snapshot_checks}
        for ref in proof["source_refs"]:
            await validate_source_ref(db, ref, user_id=main.user_id, workspace_id=main.workspace_id,
                                      main_id=main.id, validation=validation)
        await validate_business_reads(db, proof["business_reads"], user_id=main.user_id,
                                      workspace_id=main.workspace_id, main_id=main.id, snapshot_checks=snapshot_checks)
        await validate_decision_refs(db, main, proof["decision_refs"], validation=validation)
        await validate_task_snapshots(db, main, proof["task_snapshots"], snapshot_checks=snapshot_checks)
        success = True
    finally:
        if token is not None:
            _path.reset(token)
            frame = walk.frames.pop()
            completed = (frozenset(frame[0]), frame[1]) if success and frame[0] is not None else None
            if completed is not None and walk.usable(db) and len(walk.values) < walk.MAX_ENTRIES:
                walk.values[key] = completed
            walk.include(completed)
    return True


@command_validation
async def validate_task_command_sources(db, task, *, before=None, snapshot_checks=None):
    """Check admitted/current inputs, or only materialized inputs preceding a result.

    A later followup must not invalidate an older immutable result. Canceled
    and unapplied steering inputs never entered the execution's context.
    """
    from assistant.commands import _authority
    from db.models.agent_inbox import AgentInboxItem
    from db.models.assistant import AssistantCommand, TaskSubmission
    has_derivation = (func.jsonb_exists(AssistantCommand.source_ref, "derivation")
        if db.get_bind().dialect.name == "postgresql" else
        func.json_type(AssistantCommand.source_ref, "$.derivation").is_not(None))
    query = select(AssistantCommand, AgentInboxItem).join(TaskSubmission,
        TaskSubmission.command_id == AssistantCommand.id).join(AgentInboxItem,
        AgentInboxItem.id == TaskSubmission.inbox_id).where(TaskSubmission.task_id == task.id,
        TaskSubmission.disposition.not_in(("canceled", "not_applied")), has_derivation)
    if before is not None:
        query = query.join(Message, Message.id == AgentInboxItem.message_id).where(Message.created_at <= before)
    async def originals():
        return list((await db.execute(query.order_by(TaskSubmission.accepted_at, TaskSubmission.id).limit(201))).all())
    if snapshot_checks is None:
        commands = await originals()
    else:
        # Cache independent original rows only. The graph and its path/budget
        # checks below still run for every result/answer that uses them.
        commands = await snapshot_checks.check(db, "task_command_sources",
            (task.user_id, task.workspace_id, task.assistant_session_id),
            {"task_id": task.id, "before": before.isoformat() if before else None}, originals)
    if len(commands) > 200:
        raise AssistantError(410, "ASSISTANT_COMMAND_SOURCE_UNVERIFIED", "Task derivation exceeds its verification budget")
    if not commands:
        return
    main = await _authority(db, user_id=task.user_id, workspace_id=task.workspace_id,
                            main_id=task.assistant_session_id)
    for command, inbox in commands:
        reference = inbox.origin_ref or {}
        if (inbox.user_id != task.user_id or inbox.session_id != task.execution_session_id
                or reference.get("command_id") != command.id or reference.get("task_id") != task.id
                or reference.get("derivation_ref") != command_derivation_ref(command)):
            raise AssistantError(410, "ASSISTANT_COMMAND_SOURCE_UNVERIFIED", "Input derivation binding changed")
        await validate_command_derivation(db, main, command, snapshot_checks=snapshot_checks)
