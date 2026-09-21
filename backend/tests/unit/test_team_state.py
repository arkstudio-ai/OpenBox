"""Behavioral checks for the independently replayable team state machine."""
from copy import deepcopy
import random

import pytest
from pydantic import ValidationError

from agent_catalog.schemas import AgentSpec, TeamPolicy, TeamSpec
from team.errors import TeamError
from team.state import apply_event, budget_totals, empty_state, fold_events, ready_tasks


def event(state, kind, data):
    return {"team_run_id": state["id"], "sequence": state["seq"] + 1,
            "kind": kind, "entity_id": data["id"], "payload": {"schema_version": 1, "data": data}}


def append(prefix, event_kind, **data):
    return apply_event(prefix, event(prefix, event_kind, data))


def initial_state(**policy):
    state = empty_state("run")
    state = append(state, "team.run.created", id="run", root_session_id="lead", owner_user_id="user",
        workspace_id="ws", project_id="project", state="provisioning", revision=1,
        created_at="2026-09-20T00:00:00+00:00", policy_snapshot=TeamPolicy(**policy).model_dump(mode="json"),
        grant_snapshot={"version": 1, "budget_credits": "10", "paid_tools": {"image_gen": {"per_call": "6", "total": "10"}}})
    for member_id, role in (("lead", "coordinator"), ("writer", "member"), ("reviewer", "member")):
        state = append(state, "team.member.admitted", id=member_id, alias=member_id, role=role, source="coordinator",
                       composition={"private": "must not be cached"}, authority={"private": "must not be cached"})
        state = append(state, "team.member", id=member_id, membership_state="active")
    state = append(state, "team.run", **{**state["run"], "state": "running", "revision": 2})
    return state


def task(state, task_id="task", **overrides):
    data = {"id": task_id, "title": task_id, "description": "Compare the supplied evidence",
        "expected_output": "A reasoned comparison", "acceptance_criteria": "Cite both sources",
        "owner_member_id": "writer", "dependencies": [], "state": "pending", "revision": 1,
        "created_at": "2026-09-20T00:00:00+00:00", "updated_at": "2026-09-20T00:00:00+00:00", **overrides}
    return append(state, "team.task", **data)


def attempt(state, attempt_id="attempt", task_id="task", member="writer"):
    return append(state, "team.attempt", id=attempt_id, task_id=task_id, number=1, member_id=member,
                  state="running", started_at="2026-09-20T00:00:00+00:00")


def test_admissions_are_immutable_and_private_data_is_not_cached():
    state = initial_state()
    assert "composition" not in state["members"]["writer"]
    with pytest.raises(TeamError, match="rewritten"):
        append(state, "team.member.admitted", id="writer", alias="other", role="member")
    retired = append(state, "team.member", id="writer", membership_state="retired")
    with pytest.raises(TeamError) as error:
        append(retired, "team.member.admitted", id="replacement", alias="writer", role="member")
    assert error.value.code == "TEAM_MEMBER_ALIAS_TAKEN"


def test_sequence_and_payload_corruption_fail_without_changing_prefix():
    state = initial_state()
    before = deepcopy(state)
    candidate = event(state, "team.notice", {"id": "notice"})
    candidate["sequence"] += 1
    with pytest.raises(TeamError) as error:
        apply_event(state, candidate)
    assert error.value.code == "EVENT_SEQUENCE_GAP"
    assert state == before
    candidate["sequence"] -= 1
    candidate["payload"]["schema_version"] = 2
    with pytest.raises(TeamError, match="Unsupported"):
        apply_event(state, candidate)


def test_dependencies_block_dispatch_and_cycles_are_rejected():
    state = task(initial_state(), "research")
    state = task(state, "report", owner_member_id="reviewer", dependencies=["research"])
    assert [item["id"] for item in ready_tasks(state)] == ["research"]
    with pytest.raises(TeamError) as error:
        attempt(state, task_id="report", member="reviewer")
    assert error.value.code == "DEPENDENCY_NOT_READY"
    with pytest.raises(TeamError) as error:
        append(state, "team.task", **{**state["tasks"]["research"], "dependencies": ["report"], "revision": 2})
    assert error.value.code == "DEPENDENCY_CYCLE"


def test_one_running_attempt_per_member_and_no_stale_task_revision():
    state = task(task(initial_state()), "second")
    state = attempt(state)
    with pytest.raises(TeamError) as error:
        attempt(state, attempt_id="another", task_id="second")
    assert error.value.code == "MEMBER_BUSY"
    with pytest.raises(TeamError) as error:
        append(state, "team.task", **state["tasks"]["task"])
    assert error.value.code == "STALE_REVISION"
    state = append(state, "team.task", **{**state["tasks"]["task"], "state": "running", "current_attempt": "attempt", "revision": 2})
    with pytest.raises(TeamError) as error:
        append(state, "team.task", **{**state["tasks"]["task"], "owner_member_id": "reviewer", "revision": 3})
    assert error.value.code == "MEMBER_BUSY"


def test_unknown_effect_cannot_be_retried_or_finished():
    state = attempt(task(initial_state(), deliverable=True))
    state = append(state, "team.task", **{**state["tasks"]["task"], "state": "running", "current_attempt": "attempt", "revision": 2})
    state = append(state, "team.attempt", **{**state["attempts"]["attempt"], "state": "outcome_unknown"})
    state = append(state, "team.task", **{**state["tasks"]["task"], "state": "outcome_unknown", "revision": 3})
    with pytest.raises(TeamError) as error:
        append(state, "team.task", **{**state["tasks"]["task"], "state": "pending", "revision": 4})
    assert error.value.code == "INVALID_TASK_TRANSITION"
    with pytest.raises(TeamError) as error:
        append(state, "team.run", **{**state["run"], "state": "completing", "revision": 3, "final_summary": "Done"})
    assert error.value.code == "DELIVERABLES_INCOMPLETE"


def test_mailbox_receipt_does_not_imply_model_reading_and_progress_does_not_deliver():
    state = initial_state(max_pending_messages_per_member=1)
    state = append(state, "team.message.queued", id="msg", from_member_id="writer", to_member_id="reviewer", kind="question", body="Check these numbers")
    with pytest.raises(TeamError) as error:
        append(state, "team.message.queued", id="second", from_member_id="writer", to_member_id="reviewer", kind="question", body="More")
    assert error.value.code == "TEAM_MAILBOX_FULL"
    assert "body" not in state["messages"]["msg"]
    state = append(state, "team.message.delivered", id="msg", to_member_id="reviewer", inbox_id="durable-inbox")
    assert state["messages"]["msg"]["state"] == "delivered"
    assert state["members"]["reviewer"]["last_seen_seq"] == 0
    state = append(state, "team.message.queued", id="progress", from_member_id="writer", to_member_id="lead", kind="progress", body="Reading sources")
    with pytest.raises(TeamError):
        append(state, "team.message.delivered", id="progress", to_member_id="lead", inbox_id="no")
    state = append(state, "team.message.recorded", id="progress", to_member_id="lead")
    assert state["messages"]["progress"]["state"] == "recorded"


def test_reservations_ignore_legacy_caps_and_preserve_unknown_requests():
    state = initial_state()
    state = append(state, "team.budget.reserved", id="call1", tool="image_gen", amount="6")
    over_legacy_cap = append(state, "team.budget.reserved", id="call2", tool="image_gen", amount="60")
    assert sum(budget_totals(over_legacy_cap)) == 66
    with pytest.raises(TeamError) as error:
        append(state, "team.budget.released", id="call1", tool="image_gen", confirmed_not_dispatched=False)
    assert error.value.code == "OUTCOME_UNKNOWN"
    state = append(state, "team.budget.settled", id="call1", tool="image_gen", settled_amount="4.000000000001", billing_ref="usage-1")
    state = append(state, "team.budget.reserved", id="call2", tool="image_gen", amount="5.999999999999")
    assert sum(budget_totals(state)) == 10


@pytest.mark.parametrize("seed", range(12))
def test_fold_matches_incremental_replay_for_generated_dependency_graphs(seed):
    rng = random.Random(seed)
    state = initial_state()
    initial = deepcopy(state)
    events = []
    for index in range(40):
        deps = rng.sample(list(state["tasks"]), k=min(len(state["tasks"]), rng.randrange(4)))
        candidate_state = task(state, f"t{index}", dependencies=deps)
        candidate = event(state, "team.task", candidate_state["tasks"][f"t{index}"])
        events.append(candidate)
        state = candidate_state
    replayed = initial
    for item in events:
        replayed = apply_event(replayed, item)
    assert replayed == state
    for task_id, value in replayed["tasks"].items():
        if value["dependencies"]:
            assert task_id not in {task["id"] for task in ready_tasks(replayed)}


def test_config_rejects_ambiguous_or_out_of_range_definitions():
    base = {"name": "Research", "description": "Read sources", "when_to_use": "Compare source claims", "instruction": "Cite sources"}
    with pytest.raises(ValidationError):
        AgentSpec(**base, model_locked=True)
    with pytest.raises(ValidationError):
        AgentSpec(**base, example_tasks=["x"] * 6)
    with pytest.raises(ValidationError):
        AgentSpec(**base, output_schema={"type": "not-a-type"})
    with pytest.raises(ValidationError):
        TeamSpec(name="Empty", policy={"member_selection": "explicit_only"})
    assert AgentSpec(**base).skill_mode == "selected"


def test_pause_closes_admission_and_terminal_run_never_reopens():
    state = initial_state()
    state = append(state, "team.run", **{**state["run"], "state": "pausing", "revision": 3})
    state = append(state, "team.run", **{**state["run"], "state": "paused", "revision": 4})
    with pytest.raises(TeamError) as error:
        task(state)
    assert error.value.code == "TEAM_PAUSED"
    state = append(state, "team.run", **{**state["run"], "state": "canceling", "revision": 5})
    state = append(state, "team.run", **{**state["run"], "state": "canceled", "revision": 6})
    with pytest.raises(TeamError) as error:
        append(state, "team.run", **{**state["run"], "state": "running", "revision": 7})
    assert error.value.code == "TEAM_CLOSED"
