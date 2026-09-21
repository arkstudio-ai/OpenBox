import asyncio

from api import ws as ws_mod
from tests.unit.test_team_commands import setup_team


async def test_member_stream_requires_per_connection_subscription_and_ownership():
    _, actor, _, root, members = await setup_team()
    _, other, _, _, foreign_members = await setup_team()
    manager = ws_mod.WSConnectionManager()
    desktop, mobile, stranger = object(), object(), object()
    queues = [manager.register(actor.owner_user_id, desktop), manager.register(actor.owner_user_id, mobile),
              manager.register(other.owner_user_id, stranger)]
    await manager.subscribe_sessions(actor.owner_user_id, desktop, [members[0], foreign_members[0]])
    await manager.subscribe_sessions(other.owner_user_id, stranger, members)
    assert manager._subscriptions[desktop] == {members[0]}
    assert not manager._subscriptions[stranger]
    for member in members:
        await manager.send_to_user(actor.owner_user_id, {"type": "part.delta", "data": {"sessionId": member, "delta": "text"}})
    assert queues[0].qsize() == 1
    assert queues[1].empty() and queues[2].empty()
    await manager.send_to_user(actor.owner_user_id, {"type": "team.updated", "data": {"sessionId": root}})
    assert queues[0].qsize() == 2 and queues[1].qsize() == 1
    await manager.subscribe_sessions(actor.owner_user_id, desktop, [])
    await manager.send_to_user(actor.owner_user_id, {"type": "part.delta", "data": {"sessionId": members[0]}})
    assert queues[0].qsize() == 2
    await manager.send_to_user(actor.owner_user_id, {"type": "message.created", "data": {"sessionId": root}})
    assert queues[0].qsize() == 3 and queues[1].qsize() == 2
    manager.unregister(actor.owner_user_id, desktop)
    assert desktop not in manager._subscriptions


async def test_reconnect_snapshot_omits_members_and_waiting_team_keeps_sandbox():
    _, actor, _, root, _ = await setup_team()
    queue = asyncio.Queue()
    await ws_mod._enqueue_recovery_snapshot(actor.owner_user_id, queue)
    sessions = []
    while not queue.empty():
        sessions.append(queue.get_nowait()["data"]["sessionId"])
    assert sessions == [root]
    assert await ws_mod._has_active_agent_sessions(actor.owner_user_id)


async def test_ws_abort_cannot_control_member_and_root_pauses_whole_team(monkeypatch):
    from team.journal import snapshot
    from team import scheduler
    run_id, actor, _, root, members = await setup_team()
    monkeypatch.setattr(scheduler, "schedule", lambda *args: None)
    await ws_mod._handle_client_message(actor.owner_user_id, "user", {"type": "session.abort", "sessionId": members[0]})
    assert (await snapshot(run_id, actor))["run"]["state"] == "running"
    await ws_mod._handle_client_message(actor.owner_user_id, "user", {"type": "session.abort", "sessionId": root})
    assert (await snapshot(run_id, actor))["run"]["state"] == "pausing"
