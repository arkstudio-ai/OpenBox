"""Bookkeeping of recalls started at an input's claim (agent.recall_ahead)."""
import asyncio

from agent import recall_ahead
from agent.recall_ahead import RecallInputs


def _inputs(**changes):
    values = dict(query="q", recent=(("user", "hi"),), user_id="u", workspace_id="w", project_id=None,
                  include_all_projects=True, run_id="r", model_id="m")
    values.update(changes)
    return RecallInputs(**values)


def _entry(key, inputs, coroutine, *, age=0.0):
    entry = recall_ahead._Early(started=recall_ahead.time.monotonic() - age, inputs=inputs)
    entry.task = asyncio.get_running_loop().create_task(coroutine)
    recall_ahead._early[key] = entry
    return entry


async def _value(value):
    return value


async def _never():
    await asyncio.Event().wait()


async def _fails():
    raise RuntimeError("router down")


async def test_only_the_same_question_is_adopted(monkeypatch):
    monkeypatch.setattr(recall_ahead, "_early", {})
    same = _entry(("s", "m1"), _inputs(), _value({"items": []}))
    assert recall_ahead.adopt("s", "m1", _inputs()) is same.task
    assert await same.task == {"items": []}
    other = _entry(("s", "m2"), _inputs(), _never())
    assert recall_ahead.adopt("s", "m2", _inputs(model_id="n")) is None
    await asyncio.sleep(0)
    assert other.task.cancelled()
    assert recall_ahead.adopt("s", "missing", _inputs()) is None
    assert recall_ahead._early == {}


async def test_inputs_not_known_yet_or_a_failed_recall_are_not_adopted(monkeypatch):
    monkeypatch.setattr(recall_ahead, "_early", {})
    pending = _entry(("s", "m1"), None, _never())
    assert recall_ahead.adopt("s", "m1", _inputs()) is None
    failed = _entry(("s", "m2"), _inputs(), _fails())
    await asyncio.sleep(0)
    assert failed.task.done()
    assert recall_ahead.adopt("s", "m2", _inputs()) is None
    await asyncio.sleep(0)
    assert pending.task.cancelled()


async def test_a_finished_run_and_age_or_count_drop_unadopted_recalls(monkeypatch):
    monkeypatch.setattr(recall_ahead, "_early", {})
    mine = _entry(("s", "m1"), _inputs(), _never())
    theirs = _entry(("t", "m1"), _inputs(), _never())
    recall_ahead.discard("s")
    await asyncio.sleep(0)
    assert mine.task.cancelled() and not theirs.task.cancelled()
    old = _entry(("t", "old"), _inputs(), _never(), age=recall_ahead.MAX_AGE_SECONDS + 1)
    monkeypatch.setattr(recall_ahead, "MAX_ENTRIES", 2)
    recall_ahead._prune()
    await asyncio.sleep(0)
    assert old.task.cancelled() and ("t", "old") not in recall_ahead._early
    assert set(recall_ahead._early) == {("t", "m1")}
    recall_ahead.discard("t")
