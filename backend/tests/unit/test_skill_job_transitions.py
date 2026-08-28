"""Status machine whitelist: every pair is either explicitly legal or rejected."""
import pytest

from skill_runtime.types import (
    CLAIMABLE_STATUSES,
    TERMINAL_STATUSES,
    WAITING_STATUSES,
    InvalidTransition,
    JobStatus,
    allowed_targets,
    assert_transition,
    can_transition,
)

EXPECTED = {
    JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.CANCELLED},
    JobStatus.RUNNING: {
        JobStatus.WAITING_EXTERNAL,
        JobStatus.WAITING_USER,
        JobStatus.WAITING_AGENT,
        JobStatus.RETRY_SCHEDULED,
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    },
    JobStatus.WAITING_EXTERNAL: {JobStatus.QUEUED, JobStatus.CANCELLED},
    JobStatus.WAITING_USER: {JobStatus.QUEUED, JobStatus.CANCELLED},
    JobStatus.WAITING_AGENT: {JobStatus.QUEUED, JobStatus.CANCELLED},
    JobStatus.RETRY_SCHEDULED: {JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.CANCELLED},
    JobStatus.SUCCEEDED: set(),
    JobStatus.FAILED: set(),
    JobStatus.CANCELLED: set(),
}


def test_transition_matrix_exhaustive():
    for src in JobStatus:
        for dst in JobStatus:
            assert can_transition(src, dst) == (dst in EXPECTED[src]), f"{src} -> {dst}"


def test_every_status_covered_by_matrix():
    assert set(EXPECTED) == set(JobStatus)


def test_terminal_statuses_have_no_exits():
    for status in TERMINAL_STATUSES:
        assert allowed_targets(status) == frozenset()


def test_all_statuses_reachable_from_queued():
    seen = {JobStatus.QUEUED}
    frontier = [JobStatus.QUEUED]
    while frontier:
        nxt = frontier.pop()
        for target in allowed_targets(nxt):
            if target not in seen:
                seen.add(target)
                frontier.append(target)
    assert seen == set(JobStatus)


def test_assert_transition_raises_with_pair():
    with pytest.raises(InvalidTransition) as excinfo:
        assert_transition(JobStatus.SUCCEEDED, JobStatus.QUEUED)
    assert "succeeded -> queued" in str(excinfo.value)


def test_assert_transition_allows_legal():
    assert_transition(JobStatus.QUEUED, JobStatus.RUNNING)
    assert_transition(JobStatus.RUNNING, JobStatus.WAITING_EXTERNAL)
    assert_transition(JobStatus.WAITING_EXTERNAL, JobStatus.QUEUED)


def test_waiting_states_wake_only_to_queued_or_cancelled():
    for status in WAITING_STATUSES:
        assert allowed_targets(status) == frozenset({JobStatus.QUEUED, JobStatus.CANCELLED})


def test_claimable_statuses_can_be_claimed_directly():
    assert CLAIMABLE_STATUSES == frozenset({JobStatus.QUEUED, JobStatus.RETRY_SCHEDULED})
    for status in CLAIMABLE_STATUSES:
        assert can_transition(status, JobStatus.RUNNING)


def test_waiting_states_cannot_be_claimed_directly():
    for status in WAITING_STATUSES:
        assert not can_transition(status, JobStatus.RUNNING)


def test_status_values_are_db_strings():
    assert JobStatus("queued") is JobStatus.QUEUED
    assert JobStatus.WAITING_EXTERNAL.value == "waiting_external"
