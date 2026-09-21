"""Pure team event fold and invariants, independent of storage and clocks.

Like DSH's projection/task-graph, each event replaces an entity snapshot.
Admission and message bodies stay in the journal; the cache contains references.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Literal

from pydantic import Field, ValidationError

from agent_catalog.schemas import Contract, TeamPolicy
from team.errors import TeamError

TERMINAL = frozenset({"completed", "canceled", "failed"})
RUN_TRANSITIONS = {
    "provisioning": {"running", "failed", "pausing", "canceling"},
    "running": {"waiting", "pausing", "canceling", "completing", "failed"},
    "waiting": {"running", "pausing", "canceling", "completing", "failed"},
    "pausing": {"paused", "canceling"},
    "paused": {"running", "canceling"},
    "canceling": {"canceled"},
    "completing": {"completed", "canceling", "failed"},
    "completed": set(), "canceled": set(), "failed": set(),
}
TASK_TRANSITIONS = {
    "pending": {"running", "canceled"},
    "running": {"review", "blocked", "failed", "outcome_unknown", "canceled"},
    "review": {"succeeded", "pending", "canceled"},
    "succeeded": {"pending"},
    "blocked": {"pending", "canceled", "outcome_unknown"},
    "failed": {"pending", "canceled"},
    "outcome_unknown": {"review", "failed", "canceled"},
    "canceled": set(),
}


class TaskSnapshot(Contract):
    id: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=8000)
    input_refs: list[str] = Field(default_factory=list, max_length=128)
    expected_output: str = Field(min_length=1, max_length=2000)
    acceptance_criteria: str = Field(min_length=1, max_length=2000)
    output_schema: dict | None = None
    owner_member_id: str
    dependencies: list[str] = Field(default_factory=list, max_length=1000)
    priority: int = Field(default=0, ge=-100, le=100)
    state: Literal["pending", "running", "review", "succeeded", "blocked", "failed", "outcome_unknown", "canceled"] = "pending"
    revision: int = Field(default=1, ge=1)
    deliverable: bool = False
    acceptance_mode: Literal["auto", "coordinator"] = "auto"
    write_scopes: list[str] = Field(default_factory=list, max_length=128)
    exclusive_group: Literal["desktop"] | None = None
    current_attempt: str | None = None
    blocked_reason: str | None = Field(default=None, max_length=1000)
    cancellation_reason: str | None = Field(default=None, max_length=1000)
    created_at: str
    updated_at: str


class AttemptSnapshot(Contract):
    id: str = Field(min_length=1, max_length=64)
    task_id: str
    number: int = Field(ge=1)
    member_id: str
    state: Literal["running", "review", "succeeded", "blocked", "failed", "outcome_unknown", "canceled"] = "running"
    driver_run_id: str | None = None
    generation: int | None = Field(default=None, ge=1)
    summary: str = Field(default="", max_length=4000)
    artifact_ids: list[str] = Field(default_factory=list, max_length=128)
    output: Any = None
    implicit: bool = False
    error: str | None = Field(default=None, max_length=1000)
    external_effects: list[dict] = Field(default_factory=list, max_length=128)
    paid_reservations: list[str] = Field(default_factory=list, max_length=4096)
    started_at: str
    ended_at: str | None = None


class MemberStatus(Contract):
    id: str
    membership_state: Literal["provisioning", "active", "retired", "failed"]
    error: str | None = Field(default=None, max_length=1000)
    current_attempt: str | None = None
    last_seen_seq: int = Field(default=0, ge=0)
    wait_after_seq: int | None = Field(default=None, ge=0)
    wait_deadline: str | None = None
    nudged: bool = False
    interrupt_requested: bool = False
    coordinator_turns: int = Field(default=0, ge=0)
    consecutive_failures: int = Field(default=0, ge=0)
    last_failure_generation: int | None = Field(default=None, ge=1)
    execution_state: Literal["idle", "queued", "running", "waiting", "stopped"] = "idle"


def empty_state(run_id: str) -> dict[str, Any]:
    return {"schema_version": 1, "id": run_id, "seq": 0, "run": {}, "policy": {},
            "grant": {}, "members": {}, "tasks": {}, "attempts": {},
            "messages": {}, "artifacts": {}, "reservations": {}, "notices": []}


def _require(condition: bool, code: str, message: str, current: Any = None) -> None:
    if not condition:
        raise TeamError(code, message, current=current)


def check_revision(current: dict, expected: int | None) -> None:
    _require(expected == current.get("revision"), "STALE_REVISION", "The entity changed; use its latest revision.", current)


def assert_task_graph(tasks: dict[str, dict]) -> None:
    """Reject missing, repeated and cyclic dependencies, including canceled tasks."""
    for task in tasks.values():
        deps = task["dependencies"]
        _require(len(deps) == len(set(deps)), "DEPENDENCY_CYCLE", "Dependencies must be unique.")
        for dependency in deps:
            _require(dependency in tasks, "TEAM_TASK_NOT_FOUND", f"Dependency {dependency} does not belong to this run.")
    visiting: set[str] = set()
    visited: set[str] = set()

    # Iterative traversal remains bounded even for the largest permitted graph.
    for root in tasks:
        stack = [(root, False)]
        while stack:
            current, leaving = stack.pop()
            if leaving:
                visiting.remove(current)
                visited.add(current)
            elif current not in visited:
                _require(current not in visiting, "DEPENDENCY_CYCLE", f"Dependency cycle includes {current}.")
                visiting.add(current)
                stack.append((current, True))
                stack.extend((dependency, False) for dependency in reversed(tasks[current]["dependencies"]))


def ready_tasks(state: dict) -> list[dict]:
    return sorted(
        (task for task in state["tasks"].values()
         if task["state"] == "pending" and all(state["tasks"][dep]["state"] == "succeeded" for dep in task["dependencies"])),
        key=lambda task: (-task["priority"], task["created_at"], task["id"]),
    )


def amount(value: Any) -> Decimal:
    try:
        _require(isinstance(value, str), "INVALID_AMOUNT", "Journal amounts must be decimal strings.")
        result = Decimal(value)
        _require(result.is_finite() and result >= 0 and result.as_tuple().exponent >= -12
                 and result < Decimal("1e16"), "INVALID_AMOUNT", "Amount exceeds billing precision.")
        return result
    except (InvalidOperation, TypeError, ValueError) as exc:
        if isinstance(exc, TeamError):
            raise
        raise TeamError("INVALID_AMOUNT", "Invalid decimal amount.") from exc


def budget_totals(state: dict, tool: str | None = None) -> tuple[Decimal, Decimal]:
    reserved = Decimal(0)
    settled = Decimal(0)
    for item in state["reservations"].values():
        if tool is not None and item["tool"] != tool:
            continue
        if item["state"] == "reserved":
            reserved += amount(item["amount"])
        elif item["state"] == "settled":
            settled += amount(item["settled_amount"])
    return reserved, settled


def _member(state: dict, member_id: str) -> dict:
    member = state["members"].get(member_id)
    _require(member is not None, "TEAM_MEMBER_NOT_FOUND", "Member does not belong to this run.", list(state["members"].values()))
    return member


def _transition(old: str, new: str, transitions: dict, code: str) -> None:
    _require(old == new or new in transitions.get(old, set()), code, f"Invalid transition: {old} → {new}.")


def apply_event(previous: dict, event: dict) -> dict:
    """Validate a candidate against the committed prefix without mutating it."""
    _require(event.get("team_run_id") == previous["id"], "INVALID_EVENT", "Event belongs to another team.")
    _require(event.get("sequence") == previous["seq"] + 1, "EVENT_SEQUENCE_GAP", "Team event sequence is not contiguous.")
    payload = event.get("payload", {})
    _require(payload.get("schema_version") == 1 and isinstance(payload.get("data"), dict), "INVALID_EVENT", "Unsupported team event payload.")
    data = deepcopy(payload["data"])
    entity_id = event.get("entity_id")
    _require(data.get("id") == entity_id, "INVALID_EVENT", "Event entity and snapshot identity differ.")
    kind = event.get("kind")
    state = deepcopy(previous)
    seq = event["sequence"]
    try:
        _apply(state, kind, entity_id, data, seq)
    except (ValidationError, KeyError, TypeError) as exc:
        raise TeamError("INVALID_EVENT", f"Malformed {kind} snapshot: {exc}") from exc
    state["seq"] = seq
    return state


def _apply(state: dict, kind: str, entity_id: str, data: dict, seq: int) -> None:
    if kind == "team.run.created":
        _require(not state["run"] and seq == 1 and entity_id == state["id"], "INVALID_EVENT", "A run must be created exactly once at sequence 1.")
        _require(data["state"] == "provisioning" and data["revision"] == 1, "INVALID_EVENT", "A run begins provisioning at revision 1.")
        state["policy"] = TeamPolicy.model_validate(data.pop("policy_snapshot")).model_dump(mode="json")
        state["grant"] = data.pop("grant_snapshot")
        state["run"] = data
        return
    _require(bool(state["run"]), "INVALID_EVENT", "Run creation event is missing.")
    _require(state["run"]["state"] not in TERMINAL, "TEAM_CLOSED", "Terminal team journals are closed.")
    if kind == "team.run":
        old = state["run"]
        _require(old["state"] not in TERMINAL, "TEAM_CLOSED", "Terminal runs cannot be reopened.")
        _require(data["revision"] == old["revision"] + 1, "STALE_REVISION", "Run revisions must be contiguous.", old)
        _transition(old["state"], data["state"], RUN_TRANSITIONS, "INVALID_RUN_TRANSITION")
        for key in ("id", "root_session_id", "owner_user_id", "workspace_id", "project_id", "created_at"):
            _require(data[key] == old[key], "INVALID_EVENT", f"Run {key} is immutable.")
        if data["state"] == "running":
            coordinator = state["members"].get(old["root_session_id"], {})
            _require(coordinator.get("role") == "coordinator" and coordinator.get("membership_state") == "active", "INVALID_MEMBER", "An active coordinator is required before execution.")
        if data["state"] in {"completing", "completed"}:
            tasks = list(state["tasks"].values())
            failure = data.get("final_status") == "failed"
            _require(not failure or data["state"] == "completing", "INVALID_TEAM_RESULT", "Failed work cannot become completed.")
            if failure:
                _require(bool((data.get("failure_reason") or "").strip()), "INVALID_TEAM_RESULT", "Failed work requires a reason.")
            else:
                _require(any(t["deliverable"] for t in tasks), "DELIVERABLES_INCOMPLETE", "A team needs at least one deliverable task.")
                _require(all(t["state"] == "succeeded" or (t["state"] == "canceled" and t.get("cancellation_reason")) for t in tasks if t["deliverable"]), "DELIVERABLES_INCOMPLETE", "Deliverables must be accepted or canceled with a reason.")
            _require(bool(data.get("final_summary", "").strip()), "DELIVERABLES_INCOMPLETE", "Final summary is required.")
        if data["state"] in TERMINAL:
            _require(not any(a["state"] in {"running", "outcome_unknown"} for a in state["attempts"].values()), "OUTCOME_UNKNOWN", "Active or unknown attempts must be reconciled first.")
            _require(budget_totals(state)[0] == 0, "OUTCOME_UNKNOWN", "Unsettled reservations remain.")
        state["run"] = data
    elif kind == "team.grant":
        _require(data["version"] == state["grant"].get("version", 1) + 1, "STALE_REVISION", "Grant versions must be contiguous.")
        if "limits" in data:
            _require(not set(data["limits"]) - {"max_coordinator_turns", "max_wall_time_seconds"}, "INVALID_GRANT", "Only explicit execution limits may be revised.")
            state["policy"] = TeamPolicy.model_validate({**state["policy"], **data["limits"]}).model_dump(mode="json")
        state["grant"] = data
    elif kind == "team.member.admitted":
        _require(state["run"]["state"] in {"provisioning", "running", "waiting"}, "TEAM_CLOSED", "Run no longer accepts members.")
        _require(entity_id not in state["members"], "INVALID_EVENT", "Member admission cannot be rewritten.")
        _require(all(m["alias"] != data["alias"] for m in state["members"].values()), "TEAM_MEMBER_ALIAS_TAKEN", "Member aliases cannot be reused.")
        _require(data["role"] in {"coordinator", "member"}, "INVALID_EVENT", "Invalid team role.")
        if data["role"] == "coordinator":
            _require(entity_id == state["run"]["root_session_id"] and not any(m["role"] == "coordinator" for m in state["members"].values()), "INVALID_EVENT", "Only the root session can coordinate a run.")
        else:
            _require(entity_id != state["run"]["root_session_id"], "INVALID_EVENT", "The coordinator cannot also be a work member.")
        _require(len(state["members"]) < state["policy"]["max_members"], "TEAM_MEMBER_LIMIT", "The member limit includes failed and retired members.")
        # Never cache full instructions, composition or authority.
        fields = ("id", "alias", "role", "source", "definition_id", "version_id", "name", "description", "responsibility", "model", "tool_ids", "skill_refs", "exclusive_group", "config_digest", "display")
        member = {key: data.get(key) for key in fields if key != "display" or key in data}
        member.update(MemberStatus(id=entity_id, membership_state="provisioning").model_dump())
        member["admission_seq"] = seq
        state["members"][entity_id] = member
    elif kind == "team.member":
        data = MemberStatus.model_validate(data).model_dump()
        member = _member(state, entity_id)
        _transition(member["membership_state"], data["membership_state"], {"provisioning": {"active", "failed"}, "active": {"retired", "failed"}}, "INVALID_MEMBER_TRANSITION")
        _require(data["last_seen_seq"] <= seq and (data["wait_after_seq"] is None or data["wait_after_seq"] <= seq), "INVALID_EVENT", "Member watermarks cannot exceed the committed prefix.")
        _require((data["wait_after_seq"] is None) == (data["wait_deadline"] is None), "INVALID_EVENT", "Wait watermark and deadline must be paired.")
        member.update(data)
    elif kind == "team.task":
        data = TaskSnapshot.model_validate(data).model_dump(mode="json")
        old = state["tasks"].get(entity_id)
        owner = _member(state, data["owner_member_id"])
        _require(owner["role"] == "member", "INVALID_TASK", "Work tasks belong to work members.")
        if old is None:
            _require(state["run"]["state"] in {"running", "waiting"}, "TEAM_PAUSED", "Tasks cannot be created while the run is paused or closing.")
            _require(len(state["tasks"]) < state["policy"]["max_tasks"], "TEAM_TASK_LIMIT", "Team task limit reached.")
            _require(data["revision"] == 1 and data["state"] == "pending", "INVALID_TASK", "A task begins pending at revision 1.")
        else:
            _require(data["revision"] == old["revision"] + 1, "STALE_REVISION", "Task revisions must be contiguous.", old)
            _transition(old["state"], data["state"], TASK_TRANSITIONS, "INVALID_TASK_TRANSITION")
            if data["owner_member_id"] != old["owner_member_id"]:
                _require(old["state"] == "pending", "MEMBER_BUSY", "Only pending tasks can be reassigned.")
        if data["state"] == "running":
            attempt = state["attempts"].get(data["current_attempt"])
            _require(attempt is not None and attempt["task_id"] == entity_id and attempt["member_id"] == owner["id"] and attempt["state"] == "running", "INVALID_ATTEMPT", "Task must reference its running attempt.")
        state["tasks"][entity_id] = data
        assert_task_graph(state["tasks"])
    elif kind == "team.attempt":
        data = AttemptSnapshot.model_validate(data).model_dump(mode="json")
        _require((data["driver_run_id"] is None) == (data["generation"] is None), "INVALID_ATTEMPT", "Driver run and generation must be paired.")
        task = state["tasks"].get(data["task_id"])
        _require(task is not None, "TEAM_TASK_NOT_FOUND", "Attempt task does not exist.")
        _require(task["owner_member_id"] == data["member_id"], "INVALID_ATTEMPT", "Attempt must use its task owner.")
        old = state["attempts"].get(entity_id)
        if old is None:
            _require(state["run"]["state"] in {"running", "waiting"}, "TEAM_PAUSED", "Attempts cannot start while the run is paused or closing.")
            member = _member(state, data["member_id"])
            _require(task["state"] == "pending" and data["state"] == "running" and member["membership_state"] == "active", "INVALID_ATTEMPT", "Only ready tasks and active members can start an attempt.")
            _require(all(state["tasks"][dep]["state"] == "succeeded" for dep in task["dependencies"]), "DEPENDENCY_NOT_READY", "Task dependencies are not accepted.")
            _require(not any(a["state"] == "running" and (a["member_id"] == data["member_id"] or a["task_id"] == data["task_id"]) for a in state["attempts"].values()), "MEMBER_BUSY", "One running attempt per member and task.")
            number = max((a["number"] for a in state["attempts"].values() if a["task_id"] == data["task_id"]), default=0) + 1
            _require(data["number"] == number, "INVALID_ATTEMPT", "Attempt numbers must be contiguous.")
        else:
            for key in ("task_id", "member_id", "number", "started_at"):
                _require(data[key] == old[key], "INVALID_ATTEMPT", f"Attempt {key} is immutable.")
            _transition(old["state"], data["state"], TASK_TRANSITIONS, "INVALID_ATTEMPT")
            if old["driver_run_id"] is not None and data["driver_run_id"] != old["driver_run_id"]:
                _require(data["generation"] is not None and data["generation"] > old["generation"], "STALE_GENERATION", "A replaced Driver must have a newer generation.")
        state["attempts"][entity_id] = data
    elif kind == "team.message.queued":
        _require(entity_id not in state["messages"], "INVALID_EVENT", "Message IDs cannot be reused.")
        sender = _member(state, data["from_member_id"])
        target = _member(state, data["to_member_id"])
        _require(sender["membership_state"] == "active" and target["membership_state"] in {"active", "provisioning"}, "TEAM_MEMBER_NOT_FOUND", "Message members must be available.")
        _require(data["kind"] in {"message", "question", "answer", "handoff", "progress", "result", "control"}, "INVALID_MESSAGE", "Unsupported message kind.")
        _require(data["kind"] != "control" or sender["role"] == "coordinator", "AUTHORITY_REVOKED", "Only the coordinator sends control messages.")
        _require(isinstance(data["body"], str) and bool(data["body"].strip()), "INVALID_MESSAGE", "Message text is required.")
        _require(len(data["body"].encode("utf-8")) <= state["policy"]["max_message_bytes"], "TEAM_MESSAGE_TOO_LARGE", "Message exceeds its byte limit.")
        _require(len(state["messages"]) < state["policy"]["max_messages"], "TEAM_MESSAGE_LIMIT", "Run message limit reached.")
        pending = sum(m["to_member_id"] == target["id"] and m["state"] == "queued" for m in state["messages"].values())
        _require(data["kind"] == "progress" or pending < state["policy"]["max_pending_messages_per_member"], "TEAM_MAILBOX_FULL", "Member mailbox is full.")
        for key, collection in (("task_id", "tasks"), ("task_attempt_id", "attempts"), ("reply_to_message_id", "messages")):
            _require(not data.get(key) or data[key] in state[collection], "INVALID_MESSAGE", f"Message {key} must belong to this run.")
        state["messages"][entity_id] = {key: data.get(key) for key in ("id", "from_member_id", "to_member_id", "kind", "task_id", "task_attempt_id", "reply_to_message_id", "created_at")}
        state["messages"][entity_id].update({"state": "queued", "queued_seq": seq})
    elif kind in {"team.message.delivered", "team.message.recorded", "team.message.canceled", "team.message.failed"}:
        message = state["messages"].get(entity_id)
        _require(message is not None and message["state"] == "queued", "INVALID_MESSAGE", "Only queued messages can receive a receipt.")
        _require(data["to_member_id"] == message["to_member_id"], "INVALID_MESSAGE", "Receipt target changed.")
        target_state = kind.rsplit(".", 1)[1]
        _require(target_state != "recorded" or message["kind"] == "progress", "INVALID_MESSAGE", "Only progress messages are recorded without delivery.")
        _require(target_state != "delivered" or (message["kind"] != "progress" and bool(data.get("inbox_id"))), "INVALID_MESSAGE", "Delivery requires a durable Inbox receipt.")
        message.update({"state": target_state, "receipt_seq": seq, "inbox_id": data.get("inbox_id")})
    elif kind == "team.artifact":
        _require(entity_id not in state["artifacts"], "INVALID_ARTIFACT", "Submitted artifact versions are immutable.")
        attempt = state["attempts"].get(data["attempt_id"])
        _require(attempt is not None and attempt["task_id"] == data["task_id"] and attempt["member_id"] == data["member_id"], "INVALID_ARTIFACT", "Artifact origin must match its attempt.")
        _require(bool(data.get("file_asset_id")) and len(data.get("content_digest", "")) == 64, "INVALID_ARTIFACT", "Artifacts require an immutable asset and digest.")
        state["artifacts"][entity_id] = data
    elif kind.startswith("team.budget."):
        _budget(state, kind, entity_id, data)
    elif kind == "team.notice":
        state["notices"] = (state["notices"] + [{**data, "seq": seq}])[-100:]
    else:
        raise TeamError("INVALID_EVENT", f"Unsupported team event kind {kind!r}.")


def _budget(state: dict, kind: str, entity_id: str, data: dict) -> None:
    if kind == "team.budget.reserved":
        _require(entity_id not in state["reservations"], "IDEMPOTENCY_CONFLICT", "A paid operation can be reserved only once.")
        limit = state["grant"].get("paid_tools", {}).get(data["tool"])
        _require(limit is not None, "PERMISSION_REQUIRES_USER", "This paid tool has no user preauthorization.")
        requested = amount(data["amount"])
        _require(requested > 0, "INVALID_RESERVATION", "The operation must have a positive verified price bound.")
        state["reservations"][entity_id] = {**data, "state": "reserved"}
    elif kind == "team.budget.linked":
        prior = state["reservations"].get(entity_id)
        _require(prior is not None and prior["state"] == "reserved" and prior["tool"] == data["tool"], "INVALID_RESERVATION", "Only a pending paid operation can attach usage.")
        keys = list(dict.fromkeys([*prior.get("billing_keys", []), data["billing_key"]]))
        _require(len(keys) <= prior["expected_usage_count"], "INVALID_RESERVATION", "This paid operation has more usage meters than approved.")
        state["reservations"][entity_id] = {**prior, "billing_keys": keys}
    elif kind in {"team.budget.settled", "team.budget.released"}:
        prior = state["reservations"].get(entity_id)
        _require(prior is not None and prior["state"] == "reserved", "INVALID_RESERVATION", "Only an unresolved reservation can settle or release.")
        _require(data["tool"] == prior["tool"], "INVALID_RESERVATION", "Reservation tool cannot change.")
        if kind == "team.budget.settled":
            _require(amount(data["settled_amount"]) <= amount(prior["amount"]) and bool(data.get("billing_ref")), "INVALID_RESERVATION", "Settlement needs a billing reference and cannot exceed its reserved upper bound.")
        else:
            _require(data.get("confirmed_not_dispatched") is True, "OUTCOME_UNKNOWN", "Unknown external operations cannot release budget.")
        state["reservations"][entity_id] = {**prior, **data, "state": kind.rsplit(".", 1)[1]}
    else:
        raise TeamError("INVALID_EVENT", f"Unsupported budget event {kind!r}.")


def fold_events(run_id: str, events: Iterable[dict]) -> dict:
    state = empty_state(run_id)
    for event in events:
        state = apply_event(state, event)
    return state
