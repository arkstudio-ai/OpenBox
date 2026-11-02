"""Inside one task's room: entering, leaving, handing over there and hearing its replies (voice/room.py)."""
from types import SimpleNamespace

from tests.unit.test_assistant_foundation import assistant_database  # noqa: F401
from tests.unit.test_assistant_commands import setup_task
from tests.unit.test_assistant_steering import running, terminal
from voice import tools
from voice.room import RoomWatcher, room_section
from voice.tools import CallScope


class FakeCall:
    def __init__(self):
        self.room = None

    async def enter_room(self, room):
        self.room = dict(room)

    async def leave_room(self):
        self.room = None


async def test_the_room_tools_bind_and_release_the_call_by_spoken_title():
    owner, _, workspace, main, kwargs = await setup_task()
    created = await setup_task_task(kwargs, "艾薇兰小红瓶调研")
    call = FakeCall()
    scope = CallScope(user_id=owner, workspace_id=workspace, main_session_id=main.id, call_id="call-r", call=call)
    missing = await tools.run("room_enter", scope, '{"title": "不存在"}')
    assert missing["status"] == "not_found" and missing["known_titles"] == ["艾薇兰小红瓶调研"] and call.room is None
    entered = await tools.run("room_enter", scope, '{"title": "艾薇兰"}')
    assert entered["status"] == "ok" and entered["say"].startswith("好，盯着「艾薇兰小红瓶调研」的现场")
    assert call.room == {"task_id": created["task_id"], "title": "艾薇兰小红瓶调研",
                         "session_id": created["execution_session_id"]}
    assert "正在盯「艾薇兰小红瓶调研」的现场" in room_section(call.room)
    left = await tools.run("room_leave", scope, "{}")
    assert left == {"status": "ok", "left": "艾薇兰小红瓶调研", "say": "好，回来了。"}
    assert call.room is None
    # Without the call itself (another read path), the tools do nothing.
    assert (await tools.run("room_enter", CallScope(user_id=owner, workspace_id=workspace, main_session_id=main.id),
                            '{"title": "艾薇兰"}'))["status"] == "unavailable"


async def setup_task_task(kwargs, title):
    from assistant.commands import accept_task_command
    return await accept_task_command(**kwargs, title=title)


async def test_replies_landing_in_the_bound_room_are_told_once_and_only_after_entering():
    args, receipt, lease, batch = await running()
    room = {"task_id": receipt["task_id"], "title": "艾薇兰", "session_id": receipt["execution_session_id"]}
    holder = SimpleNamespace(room=None)
    watcher = RoomWatcher(user_id=args["user_id"], workspace_id=args["workspace_id"], call_id="call-r",
                          room=lambda: holder.room)
    assert await watcher.poll() == []  # not inside any room
    holder.room = room
    assert await watcher.poll() == []  # the first look only learns what is there
    await terminal(lease, batch.messages[0].id)  # the room's run finishes with "Result from this run"
    [reply] = await watcher.poll()
    assert reply.title == "艾薇兰" and reply.text == "Result from this run" and reply.inbox_id
    assert await watcher.poll() == []
    await lease.release(session_status="idle")
