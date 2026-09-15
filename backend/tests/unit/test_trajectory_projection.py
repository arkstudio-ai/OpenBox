import json
from pathlib import Path
import pytest
from trajectory.context import TraceContext, bind, current
from trajectory.projector import agents, empty_state, reduce, replay, statistics
from trajectory.types import TrajectoryError, prepare, sequence
FIXTURE = Path(__file__).parents[2] / "trajectory/fixtures/session_v1.json"


@pytest.mark.parametrize('fixture_path', sorted(FIXTURE.parent.glob('*.json')), ids=lambda path:path.stem)
def test_all_shared_projection_fixtures(fixture_path):
    fixture=json.loads(fixture_path.read_text())
    state=replay(fixture['events'])
    assert state==fixture['expected_state']
    assert statistics(state)==fixture['expected_statistics']
    assert agents(state)==fixture['expected_agents']
    historical=fixture['historical']
    cutoff=int(historical['state']['through_seq'])
    prefix=replay([event for event in fixture['events'] if int(event['seq'])<=cutoff])
    assert prefix==historical['state']
    assert replay([event for event in fixture['events'] if int(event['seq'])>cutoff],prefix)==state


def test_cross_language_golden_full_chunked_and_checkpoint_replay():
    fixture = json.loads(FIXTURE.read_text())
    events = fixture["events"]
    state = replay(events)
    assert state == fixture["expected_state"]
    assert statistics(state) == fixture["expected_statistics"]
    assert agents(state) == fixture["expected_agents"]
    prefix = replay(events[:17])
    assert prefix == fixture["historical"]["state"]
    assert replay(events[17:], prefix) == state
    assert replay(events[:8] + events[8:21] + events[21:]) == state
    assert prefix["records"]["tool:call_a"]["data"]["output"] == "one"
    assert state["records"]["tool:call_a"]["data"]["output"] == "one two"
    assert state["records"]["assistant:req_a"]["blocks"][1]["text"] == "我来读取"
    assert statistics(state)["request_count"] == 3
    assert statistics(state)["input_tokens"] == 45


def test_unknown_versions_visible_context_immutable_and_seq_safe():
    event = json.loads(FIXTURE.read_text())["events"][0]
    event.update(version=99)
    assert reduce(empty_state(), event)["unsupported_events"] == [{"seq": "1", "type": "trajectory.started", "version": 99}]
    ctx = TraceContext("user", "root", agent_id="parent")
    with bind(ctx):
        with bind(ctx.derive(source_session_id="child", agent_id="child")):
            assert current().session_id == "root"
            assert current().agent_id == "child"
        assert current() is ctx
    assert current() is None
    assert TraceContext.from_dict(ctx.to_dict()) == ctx
    with pytest.raises(TrajectoryError, match="request_id"):
        prepare(ctx, {"type": "request.started", "data": {}})
    with pytest.raises(TrajectoryError): sequence("1.5")
    assert sequence("9007199254740993") == 9007199254740993
