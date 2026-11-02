"""Task control changes reach the call as spoken notes (voice/control_notes.py, VOICE_SEAM_PLAN §3.2)."""
import pytest

from assistant.commands import accept_task_command
from assistant.control import accept_control_command
from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_commands import setup_task
from voice.control_notes import ControlWatcher, kind_of, note_text


def test_only_changes_the_user_needs_to_hear_become_notes():
    assert kind_of("canceled", "canceled", previous_desired="running", previous_observed="running") == "canceled"
    assert kind_of("canceled", "canceled", previous_desired="canceled", previous_observed="canceling") is None
    assert kind_of("canceled", "effect_unknown", previous_desired="canceled", previous_observed="canceling") == "effect_unknown"
    assert kind_of("paused", "paused", previous_desired="running", previous_observed="running") == "paused"
    assert kind_of("running", "queued", previous_desired="canceled", previous_observed="effect_unknown") == "resumed"
    assert kind_of("running", "running", previous_desired="running", previous_observed="queued") is None
    assert kind_of("paused", "resume_blocked", previous_desired="running", previous_observed="resuming") == "blocked"


def test_notes_are_speech_with_the_next_step_and_no_field_names():
    text = note_text("effect_unknown", "艾薇兰小红瓶调研", "zh")
    assert text.startswith("「艾薇兰小红瓶调研」") and "核对过了，继续" in text
    assert "effect" not in text and "state" not in text
    assert "是你在任务页按了停止" in note_text("canceled", "酒吧视频", "zh", by_user_on_page=True)
    assert "是你在任务页按了停止" not in note_text("canceled", "酒吧视频", "zh")
    assert note_text("resumed", "", "en") == "that task is going again."


async def test_a_task_stopped_from_its_page_during_the_call_is_told_once_with_who_did_it():
    owner, _, workspace, main, kwargs = await setup_task()
    created = await accept_task_command(**kwargs, title="艾薇兰小红瓶调研")
    watcher = ControlWatcher(user_id=owner, main_session_id=main.id, workspace_id=workspace, call_id="call-1")
    await watcher.start()
    assert await watcher.poll() == []
    # The task page's stop: a human command with no tool source.
    await accept_control_command(user_id=owner, workspace_id=workspace, main_id=main.id, task_id=created["task_id"],
        idempotency_key="stop-1", action="cancel", expected_revision=created["task_revision"], expected_run=None)
    notes = await watcher.poll()
    assert [(note.task_id, note.kind) for note in notes] == [(created["task_id"], "canceled")]
    assert notes[0].title == "艾薇兰小红瓶调研" and "是你在任务页按了停止" in notes[0].text
    assert await watcher.poll() == []  # told once
    # Created during the call: its own receipt tells it; the first poll only learns of it.
    later = await accept_task_command(**{**kwargs, "idempotency_key": "create-2", "prompt": "另一件事"}, title="酒吧视频")
    assert await watcher.poll() == []
    await accept_control_command(user_id=owner, workspace_id=workspace, main_id=main.id, task_id=later["task_id"],
        idempotency_key="pause-1", action="pause", expected_revision=later["task_revision"], expected_run=None)
    assert [note.kind for note in await watcher.poll()] == ["paused"]
