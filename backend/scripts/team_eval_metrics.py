"""Read-only metrics derived from retained evaluation receipts and journals."""
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal


def moment(value):
    if value is None:
        return None
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def critical_path(tasks, events):
    """Longest final-DAG path using first dispatch -> final acceptance spans.

    Spans include retries, peer waits and review. Undispatched or unaccepted
    tasks make the path incomplete; their duration is not silently zeroed.
    This is an observed task-chain metric, not CPU time or potential speedup.
    """
    starts, ends = {}, {}
    for event in events:
        if event["kind"] != "team.task":
            continue
        task, state = event["entity_id"], event["data"].get("state")
        if state == "running":
            starts.setdefault(task, moment(event["created_at"]))
        if state == "succeeded":
            ends[task] = moment(event["created_at"])
    active = {task["id"]: task for task in tasks if task["state"] != "canceled"}
    missing = [identifier for identifier, task in active.items()
        if task["state"] != "succeeded" or identifier not in starts or identifier not in ends]
    if missing or not active:
        return {"seconds": None, "task_ids": [], "incomplete_task_ids": sorted(missing)}
    visited, visiting = {}, set()
    def path(identifier):
        if identifier in visited:
            return visited[identifier]
        if identifier in visiting or identifier not in active:
            raise ValueError("Incomplete or cyclic final task graph")
        visiting.add(identifier)
        prior = max((path(dep) for dep in active[identifier].get("dependencies", [])),
            key=lambda value: value[0], default=(0, []))
        duration = (ends[identifier] - starts[identifier]).total_seconds()
        if duration < 0:
            raise ValueError("Acceptance precedes dispatch")
        visited[identifier] = (prior[0] + duration, prior[1] + [identifier])
        visiting.remove(identifier)
        return visited[identifier]
    longest = max((path(identifier) for identifier in active), key=lambda value: value[0])
    return {"seconds": longest[0], "task_ids": longest[1], "incomplete_task_ids": []}


def execution_times(events, end):
    """Integrate mutually exclusive member states, clamped at observed end.

    These are member-seconds: parallel members may sum above wall time.
    Idle includes unassigned time since admission. Waiting and capacity queue
    are reported separately; neither is described as model computation.
    """
    end = moment(end)
    current, totals = {}, defaultdict(lambda: defaultdict(float))
    for event in events:
        if event["kind"] not in {"team.member.admitted", "team.member"}:
            continue
        identifier, data = event["entity_id"], event["data"]
        at = min(moment(event["created_at"]), end)
        if identifier in current:
            prior, before = current[identifier]
            totals[identifier][prior] += max(0, (at - before).total_seconds())
        prior = current.get(identifier, ("idle", at))[0]
        current[identifier] = (data.get("execution_state", prior), at)
        if data.get("membership_state") == "retired":
            current.pop(identifier, None)
    for identifier, (state, before) in current.items():
        totals[identifier][state] += max(0, (end - before).total_seconds())
    return {identifier: dict(states) for identifier, states in totals.items()}


def cost_partition(item):
    totals = {category: {"known_credits": Decimal(0), "unknown_count": 0, "tokens": 0}
        for category in ("coordinator", "member_work", "rework", "proposal", "unattributed")}
    for row in item.get("usage", []):
        attribution = row.get("attribution") or {}
        category = attribution.get("category")
        if (attribution.get("run_id") != item.get("run_id")
                or attribution.get("member_id") != row["session_id"]
                or category not in {"coordinator", "member_work", "rework"}):
            before_admission = (item.get("run_created_at") and row.get("created_at")
                and moment(row["created_at"]) < moment(item["run_created_at"]))
            category = "proposal" if before_admission and row["session_id"] == item["session_id"] and not attribution else "unattributed"
        target = totals[category]
        if row["credits"] is None:
            target["unknown_count"] += 1
        else:
            target["known_credits"] += Decimal(row["credits"])
        target["tokens"] += row.get("tokens") or 0
    return {category: {**value, "known_credits": str(value["known_credits"])} for category, value in totals.items()}


def journal_metrics(item, *, completed_at=None):
    events = item.get("events", [])
    end = completed_at or item["finished_at"]
    finished = moment(end)
    task_spans = critical_path(item.get("tasks", []), events) if item.get("run_id") else None
    notices = [event["data"] for event in events if event["kind"] == "team.notice"]
    return {"completion_at": finished.isoformat(),
        "completion_source": "database" if completed_at else "harness_observation",
        "elapsed_seconds": (finished - moment(item["started_at"])).total_seconds(),
        "observation_lag_seconds": (moment(item["finished_at"]) - finished).total_seconds(),
        "first_valid_result_seconds": item.get("first_accepted_seconds") if item.get("run_id") else
            ((finished - moment(item["started_at"])).total_seconds() if item["status"] == "completed" else None),
        "critical_path": task_spans,
        "member_state_seconds": execution_times(events, end) if events else {},
        "repeated_exchange_notices": sum(row.get("code") == "TEAM_REPEATED_EXCHANGE" for row in notices),
        "cost_categories": cost_partition(item)}
