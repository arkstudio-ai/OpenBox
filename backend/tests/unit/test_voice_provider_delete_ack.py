"""Delete acknowledgements are target-bound and cannot release unrelated voice turns."""
import asyncio
import json

import pytest

from core.config import VoiceConfig
from voice import provider as provider_module
from voice.provider import ProviderUnavailable, RealtimeProvider


class Socket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.requests = asyncio.Queue()
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self.incoming.get()
        if event is None:
            raise StopAsyncIteration
        return json.dumps(event)

    async def send(self, raw):
        event = json.loads(raw)
        self.sent.append(event)
        self.requests.put_nowait(event)

    async def close(self):
        self.incoming.put_nowait(None)

    def push(self, event):
        self.incoming.put_nowait(event)


@pytest.fixture
async def peer():
    provider = RealtimeProvider(VoiceConfig(api_key="test-secret-key"))
    socket = provider._ws = Socket()
    yield provider, socket
    await provider.close()


async def deleting(peer, item_id="item_target", timeout=1):
    provider, socket = peer
    task = asyncio.create_task(provider.delete_item_confirmed(item_id, timeout=timeout))
    request = await asyncio.wait_for(socket.requests.get(), .5)
    assert request["type"] == "conversation.item.delete" and request["item_id"] == item_id
    assert request["event_id"]
    return task, request


async def next_event(provider):
    return await asyncio.wait_for(anext(provider.events()), .5)


def missing(target, *, request_id=None, **changes):
    error = {"type": "invalid_request_error", "code": "invalid_value",
             "param": "conversation.item.delete", "message": "Cannot find item with id: " + target}
    if request_id is not None:
        error["event_id"] = request_id
    error.update(changes)
    # This is a provider event ID, never the client's request identity.
    return {"type": "error", "event_id": "server_error_1", "error": error}


async def test_delete_ack_runs_without_a_bridge_consumer_and_requires_exact_item(peer):
    provider, socket = peer
    task, request = await deleting(peer)
    socket.push({"type": "conversation.item.deleted", "event_id": "server_other", "item_id": "item_other"})
    assert (await next_event(provider)).item_id == "item_other"
    assert not task.done()
    socket.push({"type": "conversation.item.deleted", "event_id": "server_target", "item_id": "item_target"})
    await asyncio.wait_for(task, .5)
    assert request["event_id"] != "server_target"


async def test_correlated_exact_missing_item_is_a_successful_absence_ack(peer):
    provider, socket = peer
    task, request = await deleting(peer)
    socket.push(missing("item_target", request_id=request["event_id"]))
    await asyncio.wait_for(task, .5)
    event = await next_event(provider)
    assert (event.kind, event.item_id, event.event_id) == (
        "provider_confirmed_absent", "item_target", request["event_id"])
    assert not provider.last_error


async def test_bailian_exact_missing_item_without_echo_binds_the_only_target_request(peer, caplog, monkeypatch):
    monkeypatch.setattr(provider_module.log, "propagate", True)
    provider, socket = peer
    task, request = await deleting(peer)
    socket.push(missing("item_target"))
    await asyncio.wait_for(task, .5)
    event = await next_event(provider)
    assert (event.kind, event.item_id, event.event_id) == (
        "provider_confirmed_absent", "item_target", request["event_id"])
    assert not provider.last_error
    assert "provider_confirmed_absent" in caplog.text
    assert "single_item_request" in caplog.text
    assert "Cannot find item" not in caplog.text


@pytest.mark.parametrize("changes", [
    {"type": "server_error"},
    {"code": "permission_denied"},
    {"param": "conversation.item.retrieve"},
    {"message": "Cannot find item with id: item_other"},
    {"message": "Cannot find item with id: item_target_extra"},
    {"message": "Cannot find item with id: item_target. Try again."},
    {"message": " Cannot find item with id: item_target"},
    {"message": "cannot find item with id: item_target"},
    {"item_id": "item_other"},
])
async def test_correlated_other_errors_fail_and_still_reach_the_bridge(peer, changes):
    provider, socket = peer
    task, request = await deleting(peer)
    socket.push(missing("item_target", request_id=request["event_id"], **changes))
    with pytest.raises(ProviderUnavailable):
        await asyncio.wait_for(task, .5)
    assert (await next_event(provider)).kind == "provider_error"
    assert provider.last_error


@pytest.mark.parametrize("request_id", ["unknown_request", "", None])
async def test_unrelated_missing_target_cannot_ack_a_pending_delete(peer, request_id):
    provider, socket = peer
    task, request = await deleting(peer)
    socket.push(missing("item_other", request_id=request_id))
    assert (await next_event(provider)).kind == "provider_error"
    assert not task.done()
    socket.push({"type": "conversation.item.deleted", "item_id": "item_target"})
    await asyncio.wait_for(task, .5)


async def test_explicit_wrong_request_id_cannot_fall_back_to_an_exact_target(peer):
    provider, socket = peer
    task, request = await deleting(peer)
    event = missing("item_target", request_id="a_previous_request")
    event["event_id"] = request["event_id"]
    socket.push(event)
    assert (await next_event(provider)).kind == "provider_error"
    assert not task.done()
    socket.push({"type": "conversation.item.deleted", "item_id": "item_target"})
    await asyncio.wait_for(task, .5)


@pytest.mark.parametrize("changes", [
    {"event_id": ""}, {"event_id": None},
    {"type": "server_error"}, {"code": "invalid_event"},
    {"param": "conversation.item.retrieve"},
    {"message": "Cannot find item with id: item_target_other"},
    {"message": "Cannot find item with id: item_target."},
    {"message": "Cannot find item with id: item_target\n"},
    {"message": "Item item_target not found"},
    {"item_id": "item_other"},
])
async def test_unechoed_errors_need_the_entire_measured_absence_shape(peer, changes):
    provider, socket = peer
    task, request = await deleting(peer)
    socket.push(missing("item_target", **changes))
    assert (await next_event(provider)).kind == "provider_error"
    assert not task.done()
    socket.push({"type": "conversation.item.deleted", "item_id": "item_target"})
    await asyncio.wait_for(task, .5)


async def test_unconfirmed_and_concurrent_confirmed_deletes_send_one_request(peer):
    provider, socket = peer
    await provider.delete_item("item_target")
    request = await socket.requests.get()
    first = asyncio.create_task(provider.delete_item_confirmed("item_target"))
    second = asyncio.create_task(provider.delete_item_confirmed("item_target"))
    await provider.delete_item("item_target")
    socket.push(missing("item_target"))
    await asyncio.wait_for(asyncio.gather(first, second), .5)
    assert (await next_event(provider)).kind == "provider_confirmed_absent"
    await provider.delete_item_confirmed("item_target")
    await provider.delete_item("item_target")
    assert socket.sent == [request]


async def test_cancelling_one_waiter_does_not_cancel_the_shared_receipt(peer):
    provider, socket = peer
    first, request = await deleting(peer)
    second = asyncio.create_task(provider.delete_item_confirmed("item_target"))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    socket.push(missing("item_target", request_id=request["event_id"]))
    await asyncio.wait_for(second, .5)
    assert socket.sent == [request]
    assert (await next_event(provider)).kind == "provider_confirmed_absent"


async def test_timeout_stays_visible_and_late_receipt_never_requires_a_second_request(peer):
    provider, socket = peer
    task, request = await deleting(peer, timeout=.01)
    with pytest.raises(TimeoutError):
        await task
    later = asyncio.create_task(provider.delete_item_confirmed("item_target"))
    socket.push(missing("item_target"))
    await asyncio.wait_for(later, .5)
    assert (await next_event(provider)).kind == "provider_confirmed_absent"
    assert socket.sent == [request]


async def test_unsolicited_absence_with_no_local_delete_is_not_consumed(peer):
    provider, socket = peer
    socket.push(missing("item_unknown"))
    assert (await next_event(provider)).kind == "provider_error"
    assert not socket.sent


async def test_an_old_socket_receipt_cannot_release_the_new_socket(peer):
    old, old_socket = peer
    old_task, old_request = await deleting(peer)
    new = RealtimeProvider(VoiceConfig(api_key="test-secret-key"))
    new_socket = new._ws = Socket()
    try:
        new_task, new_request = await deleting((new, new_socket))
        assert old_request["event_id"] != new_request["event_id"]
        old_socket.push(missing("item_target"))
        await asyncio.wait_for(old_task, .5)
        assert (await next_event(old)).kind == "provider_confirmed_absent"
        assert not new_task.done()
        new_socket.push(missing("item_target", request_id=old_request["event_id"]))
        assert (await next_event(new)).kind == "provider_error"
        assert not new_task.done()
        new_socket.push({"type": "conversation.item.deleted", "item_id": "item_target"})
        await asyncio.wait_for(new_task, .5)
    finally:
        await new.close()


async def test_concurrent_targets_cannot_cross_acknowledge(peer):
    provider, socket = peer
    first, first_request = await deleting(peer, "item_first")
    second, second_request = await deleting(peer, "item_second")
    assert first_request["event_id"] != second_request["event_id"]
    socket.push(missing("item_first", request_id=second_request["event_id"]))
    with pytest.raises(ProviderUnavailable):
        await asyncio.wait_for(second, .5)
    assert (await next_event(provider)).kind == "provider_error"
    assert not first.done()
    socket.push({"type": "conversation.item.deleted", "item_id": "item_first"})
    await asyncio.wait_for(first, .5)


async def test_connection_close_fails_pending_delete_without_waiting_for_timeout(peer):
    provider, socket = peer
    task, _ = await deleting(peer)
    socket.push(None)
    with pytest.raises(ProviderUnavailable, match="connection closed"):
        await asyncio.wait_for(task, .5)


async def test_explicit_close_fails_all_waiters(peer):
    provider, socket = peer
    first, _ = await deleting(peer, "item_first")
    second, _ = await deleting(peer, "item_second")
    await provider.close()
    results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), .5)
    assert all(isinstance(value, ProviderUnavailable) for value in results)


async def test_absent_prompt_is_retired_without_inventing_a_deleted_event(peer):
    provider, socket = peer
    provider._prompt_items.add("item_target")
    task, _ = await deleting(peer)
    socket.push(missing("item_target"))
    await asyncio.wait_for(task, .5)
    assert not provider._prompt_items
    assert (await next_event(provider)).kind == "provider_confirmed_absent"
