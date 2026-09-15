"""trajectory.available publication (SPEC §8.7): exact payload keys, local subscribers, never raises."""
from types import SimpleNamespace

from bus import bus, trajectory_hints
from trajectory.worker.notify import available_payload, publish_available


def test_payload_has_exactly_the_contract_keys():
    row = SimpleNamespace(id="trj_1", user_id="u1", session_id="s1", committed_seq=7, projected_seq=3)
    assert available_payload(row) == {"user_id": "u1", "owner_user_id": "u1", "session_id": "s1",
                                      "trajectory_id": "trj_1", "committed_seq": "7"}
    assert available_payload({"trajectory_id": "trj_2", "user_id": "u2", "session_id": "s2", "committed_seq": 0},
                             deleted=True) == {"user_id": "u2", "owner_user_id": "u2", "session_id": "s2",
                                               "trajectory_id": "trj_2", "committed_seq": "0", "deleted": True}


def test_publish_reaches_type_subscribers_and_never_the_business_bus(monkeypatch):
    def business_publish(*_args, **_kwargs):
        raise AssertionError("trajectory hints never travel on the business bus")

    monkeypatch.setattr(bus, "publish", business_publish)
    monkeypatch.setattr(trajectory_hints, "_channel", None)
    received = []
    unsubscribe = bus.subscribe("trajectory.available", received.append)
    try:
        publish_available(SimpleNamespace(id="trj_1", user_id="u1", session_id="s1", committed_seq=3))
    finally:
        unsubscribe()
    assert received == [{"type": "trajectory.available", "data": {
        "user_id": "u1", "owner_user_id": "u1", "session_id": "s1", "trajectory_id": "trj_1", "committed_seq": "3"}}]


def test_publish_never_raises(monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("bus down")
    monkeypatch.setattr(trajectory_hints, "publish_trajectory_hint", broken)
    publish_available(SimpleNamespace(id="trj_1", user_id="u1", session_id="s1", committed_seq=1))
    publish_available(object())
