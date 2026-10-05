"""Finite schema and persistent identity/receipt checks without a browser.

The companion Chromium suite supplies actual pipe/HTTP/WS evidence. These
small checks do not claim that journal state alone proves physical drainage.
"""
import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "container"))
from browser_resource import BrowserError, BrowserJournal, Operation, validate_args  # noqa: E402


@pytest.mark.parametrize("kind,args", [
    ("navigate", {"url": "file:///tmp/private"}),
    ("navigate", {"url": "javascript:alert(1)"}),
    ("navigate", {"url": "http://[malformed"}),
    ("capture", {"expression": "private"}),
    ("mouse", {"x": True, "y": 1, "button": "left"}),
    ("mouse", {"x": 1024, "y": 1, "button": "left"}),
    ("key", {"key": "Control"}),
    ("text", {"text": "x" * 4097}),
    ("wheel", {"x": 0, "y": 0, "delta_x": 9000, "delta_y": 0}),
])
def test_finite_input_rejects_hidden_or_unbounded_actions(kind, args):
    with pytest.raises(BrowserError, match="BROWSER_INVALID_OPERATION"):
        validate_args(kind, args)


def test_original_journal_has_one_live_supervisor_and_restart_is_closed(tmp_path):
    first = BrowserJournal(tmp_path / "journal", "a" * 64, "fixture-workspace")
    identity = dict(first.identity)
    assert first.status(True)["control"]["admission"] == "open"
    with pytest.raises(BlockingIOError):
        BrowserJournal(tmp_path / "journal", "a" * 64, "fixture-workspace")
    first.release_lock()
    second = BrowserJournal(tmp_path / "journal", "a" * 64, "fixture-workspace")
    try:
        assert second.identity["profile_id"] == identity["profile_id"]
        assert second.identity["journal_id"] == identity["journal_id"]
        assert second.identity["runtime_id"] != identity["runtime_id"]
        assert second.status(True)["control"]["admission"] == "closed"
        assert second.status(True)["control"]["status"] == "hold"
    finally:
        second.release_lock()


def test_journal_cannot_be_reassigned_to_another_resource_or_owner(tmp_path):
    first = BrowserJournal(tmp_path / "journal", "b" * 64, "fixture-workspace")
    first.release_lock()
    with pytest.raises(ValueError, match="cannot change"):
        BrowserJournal(tmp_path / "journal", "c" * 64, "fixture-workspace")


def test_client_requires_explicit_pinning_and_does_not_follow_discovery():
    location = ROOT / "backend/sandbox/browser_resource_client.py"
    spec = importlib.util.spec_from_file_location("isolated_browser_resource_client", location)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    client = module.BrowserResourceClient("http://127.0.0.1:9", "fixture-key-only")
    with pytest.raises(module.BrowserResourceError, match="BROWSER_IDENTITY_REQUIRED"):
        client._identity()
    client.identity = {"runtime_id": "original"}
    with pytest.raises(module.BrowserResourceError, match="BROWSER_IDENTITY_CHANGED"):
        client._verify({"protocol": "browser_resource_v1", "identity": {"runtime_id": "replacement"}})


@pytest.mark.parametrize("dispatched,expected", [(False, "canceled"), (True, "unknown")])
def test_restart_distinguishes_unsent_admission_from_dispatched_unknown(tmp_path, dispatched, expected):
    first = BrowserJournal(tmp_path / "journal", "d" * 64, "fixture-workspace")
    operation = Operation(identity=first.identity, fence=first.status(True)["control"]["fence"],
        operation_id="interrupted-capture", kind="capture")
    first.admit(operation)
    if dispatched:
        first.begin(operation)
    first.release_lock()
    second = BrowserJournal(tmp_path / "journal", "d" * 64, "fixture-workspace")
    try:
        receipt = second.receipt("operation", operation.operation_id)
        assert receipt["state"] == expected
        assert receipt["identity"] == operation.identity.model_dump()
        assert second.status(True)["control"]["admission"] == "closed"
    finally:
        second.release_lock()
