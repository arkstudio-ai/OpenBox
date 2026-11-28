"""Replay every recorded session through the real agent loop and compare. Part of `make check-kernel`.

See tests/replay/harness.py for the case format, the seams and the
normalisation. To re-record after an intended change, then review it:

    cd backend && OPENBOX_REPLAY_UPDATE=1 uv run --extra test pytest tests/replay -q
    git add -N tests/replay/recordings && git diff -M tests/replay/recordings

Long texts (system prompt, tool list) live in recordings/_shared under a
content hash, so a prompt edit shows up as a rename with a line diff (-M).
To see what an agent_tuning switch would change, record with
OPENBOX_REPLAY_TUNING=<switch>[,<switch>] and discard the result afterwards:
the committed recordings are the switches-off baseline.
"""
import pytest

from tests.replay import harness
from tests.unit.test_durable_questions import state  # noqa: F401  (migration-backed database)


@pytest.fixture(autouse=True)
def sqlite_only(monkeypatch):
    """Recordings are made on SQLite; a PostgreSQL run would persist the same events in another order."""
    monkeypatch.delenv("OBX_QUESTION_TEST_DATABASE_URL", raising=False)


@pytest.mark.parametrize("name", harness.case_names())
async def test_replay(name, state, monkeypatch, tmp_path):
    case = harness.load_case(name)
    observed = await harness.run_case(case, monkeypatch, tmp_path / "workdir")
    if harness.UPDATE:
        harness.write_recording(name, observed)
        return
    recorded = harness.read_recording(name)
    assert recorded is not None, (
        f"no recording for {name}; record it with OPENBOX_REPLAY_UPDATE=1 (see tests/replay/test_replay.py)")
    diff = harness.differences(recorded, observed)
    assert not diff, (
        f"replay of {name} differs from tests/replay/recordings/{name}. If the change is intended, "
        f"re-record with OPENBOX_REPLAY_UPDATE=1 and review the diff.\n{diff}")


@pytest.mark.parametrize("switch", ["relaxed_prompt"])
async def test_a_deliberate_prompt_change_shows_up_as_a_diff(switch, state, monkeypatch, tmp_path):
    """The baseline is recorded with every agent_tuning switch off. Turning one on must
    change what the model is sent, and only that: the system prompt differs, the
    history, tools and persisted events do not."""
    if harness.UPDATE or harness.TUNING_FROM_ENV:
        pytest.skip("compares against the switches-off baseline")
    base = "multi_turn"
    case = harness.load_case(base)
    case["config"] = {**case.get("config", {}), "agent_tuning": {switch: True}}
    observed = await harness.run_case(case, monkeypatch, tmp_path / "workdir")
    recorded = harness.read_recording(base)
    assert harness.differences(recorded, observed), f"turning on {switch} changed nothing the model is sent"
    assert len(observed["requests"]) == len(recorded["requests"])
    for old, new in zip(recorded["requests"], observed["requests"]):
        old_system, *old_rest = old["body"]["messages"]
        new_system, *new_rest = new["body"]["messages"]
        assert old_system["content"] != new_system["content"]
        assert old_rest == new_rest
        assert {k: v for k, v in old["body"].items() if k != "messages"} == \
            {k: v for k, v in new["body"].items() if k != "messages"}
    assert observed["events"] == recorded["events"]
    assert observed["messages"] == recorded["messages"]


async def test_a_call_that_leaves_the_script_fails_the_case(state, monkeypatch, tmp_path):
    """When the loop's tool call no longer matches the script, the case fails with that call named."""
    if harness.UPDATE:
        pytest.skip("not a recording")
    case = harness.load_case("single_tool")
    case["turns"][0]["tools"][0]["args"] = {"file_path": "/workspace/other.txt"}
    with pytest.raises(harness.ScriptMismatch, match=r"read\(\{\"file_path\": \"/workspace/notes.txt\"\}\)"):
        await harness.run_case(case, monkeypatch, tmp_path / "workdir")


def test_prune_shared_after_update():
    """Runs last in the module: drop shared texts that no recording refers to."""
    if harness.UPDATE:
        harness.prune_shared()
