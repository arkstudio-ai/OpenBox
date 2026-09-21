from scripts.team_eval_metrics import critical_path, execution_times, cost_partition
import pytest

from scripts.run_team_evaluation import MANIFEST_DIGEST, MODEL, matching_lineup, validate_round


def event(kind, identifier, second, **data):
    return {"kind": kind, "entity_id": identifier, "created_at": f"2026-09-21T00:00:{second:02d}+00:00", "data": data}


def test_critical_path_counts_retry_and_review_wait_but_not_parallel_sum():
    tasks = [{"id": name, "state": "succeeded", "dependencies": deps} for name, deps in [("a", []), ("b", []), ("c", ["a", "b"])]]
    events = [event("team.task", "a", 1, state="running"), event("team.task", "b", 2, state="running"),
        event("team.task", "a", 4, state="failed"), event("team.task", "a", 6, state="running"),
        event("team.task", "b", 7, state="succeeded"), event("team.task", "a", 9, state="review"),
        event("team.task", "a", 10, state="succeeded"), event("team.task", "c", 12, state="running"),
        event("team.task", "c", 17, state="succeeded")]
    assert critical_path(tasks, events) == {"seconds": 14, "task_ids": ["a", "c"], "incomplete_task_ids": []}
    tasks[-1]["state"] = "blocked"
    assert critical_path(tasks, events)["seconds"] is None


def test_member_states_partition_lifetime_and_retirement_stops_idle_count():
    events = [event("team.member.admitted", "one", 0), event("team.member", "one", 2, execution_state="queued"),
        event("team.member", "one", 3, execution_state="running"), event("team.member", "one", 5, execution_state="waiting"),
        event("team.member", "one", 8, execution_state="idle"), event("team.member", "one", 10, membership_state="retired")]
    assert execution_times(events, "2026-09-21T00:00:20+00:00") == {"one": {"idle": 4, "queued": 1, "running": 2, "waiting": 3}}


def test_cost_categories_preserve_unknown_and_reject_foreign_attribution():
    item = {"run_id": "run", "session_id": "root", "run_created_at": "2026-09-21T00:00:02+00:00", "usage": [
        {"session_id": "root", "credits": "1", "tokens": 5, "created_at": "2026-09-21T00:00:01+00:00"},
        {"session_id": "member", "credits": "2", "tokens": 6, "attribution": {"run_id": "run", "member_id": "member", "category": "rework"}},
        {"session_id": "member", "credits": None, "tokens": 7, "attribution": {"run_id": "other", "member_id": "member", "category": "member_work"}}]}
    costs = cost_partition(item)
    assert costs["proposal"]["known_credits"] == "1" and costs["rework"]["known_credits"] == "2"
    assert costs["member_work"]["known_credits"] == "0" and costs["unattributed"]["unknown_count"] == 1


def test_frozen_evaluation_receipt_cannot_switch_models_or_rules():
    receipt = {"round": "v2", "model": "openai/qwen3.8-flash", "manifest_sha256": MANIFEST_DIGEST,
        "registered_protocol": "docs/evaluations/agent-team-v2/PROTOCOL.md", "protocol_sha256": "frozen"}
    validate_round(receipt, "v2", "frozen")
    for changed in ({"model": MODEL}, {"protocol_sha256": "changed"}, {"manifest_sha256": "changed"}):
        with pytest.raises(ValueError):
            validate_round({**receipt, **changed}, "v2", "frozen")
    with pytest.raises(ValueError):
        validate_round(receipt, "v1", "frozen")
    with pytest.raises(ValueError):
        validate_round({key: value for key, value in receipt.items() if key != "protocol_sha256"}, "v2", "frozen")


def test_legacy_v1_receipt_stays_resumable_only_as_v1():
    receipt = {"manifest_sha256": MANIFEST_DIGEST, "registered_protocol": "docs/evaluations/agent-team-v1/PROTOCOL.md"}
    validate_round(receipt, "v1", "legacy")
    with pytest.raises(ValueError):
        validate_round(receipt, "v2", "legacy")


def test_qwen_lineup_confirmation_requires_the_exact_frozen_model_and_limits():
    model = "openai/qwen3.8-flash"
    detail = {"kind": "team_lineup", "coordinator": {"model": model},
        "spec": {"policy": {"budget_credits": "2", "max_members": 4, "max_concurrent_members": 2,
            "max_wall_time_seconds": 600, "max_coordinator_turns": 30}},
        "members": [{"model": model, "tool_ids": ["team_task_update"]}]}
    question = {"questions": [{"detail": detail}]}
    assert matching_lineup(question, model=model)
    assert not matching_lineup(question)
    detail["members"][0]["model"] = MODEL
    assert not matching_lineup(question, model=model)
    detail["members"][0]["model"] = model
    detail["spec"]["policy"]["budget_credits"] = "3"
    assert not matching_lineup(question, model=model)
    detail["spec"]["policy"]["budget_credits"] = "10"
    assert matching_lineup(question, model=model, budget="10")
    detail["spec"]["policy"]["budget_credits"] = "10.01"
    assert not matching_lineup(question, model=model, budget="10")


def test_v3_budget_change_does_not_rewrite_an_old_round():
    receipt = {"round": "v3", "model": "openai/qwen3.8-flash", "budget_credits": "10",
        "manifest_sha256": MANIFEST_DIGEST, "registered_protocol": "docs/evaluations/agent-team-v3/PROTOCOL.md",
        "protocol_sha256": "frozen"}
    validate_round(receipt, "v3", "frozen")
    with pytest.raises(ValueError):
        validate_round({**receipt, "budget_credits": "20"}, "v3", "frozen")
    with pytest.raises(ValueError):
        validate_round(receipt, "v2", "frozen")


def test_v4_uses_account_credits_without_reinterpreting_legacy_rounds():
    model = "openai/qwen3.8-flash"
    policy = {"max_members": 4, "max_concurrent_members": 2, "max_wall_time_seconds": 600, "max_coordinator_turns": 30}
    detail = {"kind": "team_lineup", "coordinator": {"model": model},
        "spec": {"policy": policy}, "members": [{"model": model, "tool_ids": ["team_task_update"]}]}
    question = {"questions": [{"detail": detail}]}
    assert matching_lineup(question, model=model, budget=None)
    policy["budget_credits"] = "10"
    assert not matching_lineup(question, model=model, budget=None)
    receipt = {"round": "v4", "model": model, "budget_credits": None,
        "manifest_sha256": MANIFEST_DIGEST, "registered_protocol": "docs/evaluations/agent-team-v4/PROTOCOL.md", "protocol_sha256": "account-credits"}
    validate_round(receipt, "v4", "account-credits")
    with pytest.raises(ValueError):
        validate_round(receipt, "v3", "account-credits")
