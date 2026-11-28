"""Tail selection for compaction — the messages that survive verbatim.

Ported behaviour from opencode's SessionCompaction.select(); these tests pin
the properties that make the tail useful: it never starts mid-exchange, it
respects the budget, and it never swallows the whole history.
"""
import pytest

from agent.compaction_select import (
    MAX_PRESERVE_RECENT_TOKENS,
    MIN_PRESERVE_RECENT_TOKENS,
    estimate,
    preserve_recent_budget,
    select,
    split_turn,
    turns,
)


class FakeMsg:
    def __init__(self, id, role, text="", parts=None):
        self.id = id
        self.role = role
        self.parts = parts if parts is not None else [{"type": "text", "text": text}]


def user(id, text="hi"):
    return FakeMsg(id, "user", text)


def assistant(id, text="ok"):
    return FakeMsg(id, "assistant", text)


def compaction_request(id):
    return FakeMsg(id, "user", parts=[{"type": "compaction", "auto": True}])


def convo(n_turns, msgs_per_turn=2, text="x" * 400):
    """n_turns turns, each a user message followed by assistant messages."""
    out = []
    for t in range(n_turns):
        out.append(user(f"u{t}", text))
        for a in range(msgs_per_turn - 1):
            out.append(assistant(f"a{t}_{a}", text))
    return out


# ── budget ──

def test_budget_is_a_quarter_of_usable_context():
    assert preserve_recent_budget(100_000) == 25_000


def test_budget_is_clamped_at_both_ends():
    assert preserve_recent_budget(1_000) == MIN_PRESERVE_RECENT_TOKENS
    assert preserve_recent_budget(1_000_000) == MAX_PRESERVE_RECENT_TOKENS


def test_configured_budget_wins_and_cannot_go_negative():
    assert preserve_recent_budget(100_000, 5_000) == 5_000
    assert preserve_recent_budget(100_000, -1) == 0


# ── turns ──

def test_turns_are_opened_by_user_messages():
    msgs = convo(3)
    result = turns(msgs)
    assert [t.id for t in result] == ["u0", "u1", "u2"]
    assert [(t.start, t.end) for t in result] == [(0, 2), (2, 4), (4, 6)]


def test_compaction_requests_do_not_open_a_turn():
    msgs = [user("u0"), assistant("a0"), compaction_request("c0"), assistant("s0")]
    assert [t.id for t in turns(msgs)] == ["u0"]
    # ...and the turn extends over the bookkeeping messages
    assert turns(msgs)[0].end == 4


def test_no_turns_when_history_has_no_user_message():
    assert turns([assistant("a0")]) == []


# ── estimate ──

def test_estimate_counts_tool_input_and_output_not_just_text():
    msg = FakeMsg("m", "assistant", parts=[
        {"type": "tool", "input": {"path": "x" * 100}, "output": "y" * 100},
    ])
    assert estimate([msg]) > 0


def test_estimate_of_nothing_is_zero():
    assert estimate([]) == 0
    assert estimate([FakeMsg("m", "user", parts=[])]) == 0


# ── split_turn ──

def test_split_never_lands_on_the_user_message_that_opened_the_turn():
    msgs = convo(1, msgs_per_turn=4)
    t = turns(msgs)[0]
    got = split_turn(msgs, t, budget=estimate(msgs))
    assert got is not None
    assert got[0] > t.start
    assert got[1] != "u0"


def test_split_returns_none_for_a_single_message_turn():
    msgs = [user("u0")]
    assert split_turn(msgs, turns(msgs)[0], budget=10_000) is None


def test_split_returns_none_when_budget_is_zero():
    msgs = convo(1, msgs_per_turn=4)
    assert split_turn(msgs, turns(msgs)[0], budget=0) is None


def test_split_picks_the_earliest_message_that_fits():
    msgs = convo(1, msgs_per_turn=5)
    t = turns(msgs)[0]
    one_message = estimate(msgs[4:5])
    got = split_turn(msgs, t, budget=one_message)
    assert got == (4, msgs[4].id)


# ── select ──

def test_history_smaller_than_the_budget_keeps_the_newest_request():
    # Everything would fit in the tail, but keeping it all means the summary
    # covers nothing and compaction would spin. Summarising it all would fold
    # the request being answered, so the older turns go instead.
    msgs = convo(5)
    sel = select(msgs, usable_tokens=1_000_000)
    assert sel.tail_start_id == "u4"
    assert [m.id for m in sel.head] == [m.id for m in msgs[:8]]


def test_single_turn_history_smaller_than_the_budget_is_summarised_whole():
    # Nothing is older than the request; keeping it would summarise nothing.
    msgs = convo(1, msgs_per_turn=3)
    sel = select(msgs, usable_tokens=1_000_000)
    assert sel.tail_start_id is None
    assert len(sel.head) == len(msgs)


def pending_step(id):
    """The assistant message a step opens before the model has said anything."""
    return FakeMsg(id, "assistant", parts=[{"type": "step-start"}])


def platform_note(id):
    return FakeMsg(id, "user", parts=[{
        "type": "text", "text": "Context was compacted. Continue working on the current task.",
        "synthetic": True, "origin": "system_recovery",
    }])


def test_compaction_before_the_answer_keeps_the_new_request_when_all_fits():
    # The replay case `compaction`: a short history whose request is already
    # over the threshold compacts before the new question is answered.
    msgs = convo(2) + [user("ask", "what is the code name?"), pending_step("step")]
    sel = select(msgs, usable_tokens=1_000_000)
    assert sel.tail_start_id == "ask"
    assert [m.id for m in sel.head] == [m.id for m in msgs[:4]]


def test_a_platform_note_does_not_displace_the_request_it_continues():
    msgs = convo(2) + [user("ask", "what is the code name?"), pending_step("step"), platform_note("note")]
    sel = select(msgs, usable_tokens=1_000_000)
    assert sel.tail_start_id == "ask"


def test_a_request_larger_than_the_budget_is_still_summarised():
    # Kept verbatim it can leave the next request over the threshold, and the
    # run would end in COMPACTION_FAILED instead of an answer (pinned end to end
    # by test_context_budget's new-input case).
    msgs = convo(3) + [user("ask", "x" * 200_000), pending_step("step")]
    sel = select(msgs, usable_tokens=100_000)
    assert sel.tail_start_id == "step"


def test_a_long_turn_still_summarises_its_settled_steps_with_the_request():
    # Compaction between the settled steps of one long turn is what lets a
    # long task keep running; keeping the whole turn would leave nothing to fold.
    msgs = convo(2) + [user("ask")] + [assistant(f"step{i}", "x" * 40_000) for i in range(4)]
    sel = select(msgs, usable_tokens=100_000)
    ids = [m.id for m in msgs]
    assert sel.tail_start_id not in ("ask", None)
    assert "ask" in [m.id for m in sel.head]
    assert ids.index(sel.tail_start_id) > ids.index("ask") + 1


def test_tail_starts_at_a_turn_boundary_when_whole_turns_fit():
    msgs = convo(6, text="x" * 20_000)
    sel = select(msgs, usable_tokens=100_000)
    ids = [m.id for m in msgs]
    assert sel.tail_start_id in ids
    idx = ids.index(sel.tail_start_id)
    assert [m.id for m in sel.head] == ids[:idx]


def test_head_and_tail_together_are_the_whole_history():
    msgs = convo(6, text="x" * 20_000)
    sel = select(msgs, usable_tokens=100_000)
    ids = [m.id for m in msgs]
    idx = ids.index(sel.tail_start_id)
    assert [m.id for m in sel.head] + ids[idx:] == ids


def test_tail_turns_zero_disables_the_tail():
    msgs = convo(5)
    sel = select(msgs, usable_tokens=100_000, tail_turns=0)
    assert sel.tail_start_id is None
    assert len(sel.head) == len(msgs)


def test_tail_turns_caps_how_far_back_the_tail_reaches():
    msgs = convo(6, text="x" * 400)
    sel = select(msgs, usable_tokens=1_000_000, tail_turns=2)
    ids = [m.id for m in msgs]
    idx = ids.index(sel.tail_start_id)
    assert idx >= turns(msgs)[-2].start


def test_tiny_budget_against_one_huge_turn_keeps_no_tail():
    msgs = [user("u0", "x" * 200_000), assistant("a0", "x" * 200_000)]
    sel = select(msgs, usable_tokens=100, configured_budget=1)
    assert sel.tail_start_id is None
    assert len(sel.head) == len(msgs)


def test_history_with_no_turns_is_summarised_whole():
    msgs = [assistant("a0"), assistant("a1")]
    sel = select(msgs, usable_tokens=100_000)
    assert sel.tail_start_id is None
    assert len(sel.head) == 2


def test_empty_history_is_handled():
    sel = select([], usable_tokens=100_000)
    assert sel.head == []
    assert sel.tail_start_id is None
