"""Backup rejects untrusted metadata before payload reads or cloud mutations."""
import json
import math
import sys
import types

import pytest
from fastapi import HTTPException

from tests.unit.test_action_server_desktop_lease import server
from file_worker import FileWorkerError


@pytest.fixture
def cloud(monkeypatch):
    objects, calls = {}, []
    class Blob:
        def __init__(self, key):
            self.key = key
        def exists(self):
            calls.append(("exists", self.key))
            return self.key in objects
        def download_as_bytes(self):
            calls.append(("download", self.key))
            return objects[self.key]
        def upload_from_string(self, data):
            pytest.fail("invalid snapshot performed a cloud upload")
        def delete(self):
            pytest.fail("invalid snapshot performed a cloud deletion")
    google, package, storage = (types.ModuleType(name) for name in ("google", "google.cloud", "google.cloud.storage"))
    google.cloud, package.storage = package, storage
    def client():
        calls.append(("client", None))
        return types.SimpleNamespace(bucket=lambda _name: types.SimpleNamespace(blob=Blob))
    storage.Client = client
    for module in (google, package, storage):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    return objects, calls


@pytest.mark.parametrize("path,mtime", [
    ("../private", 1), ("/private", 1), ("normal/../../private", 1),
    ("normal//file", 1), ("normal\\file", 1), ("", 1),
    ("normal", math.nan), ("normal", math.inf), ("normal", True),
    ("normal", "not-a-time"), ("normal", 10**1000),
])
def test_restore_validates_all_manifest_entries_before_the_first_payload(cloud, path, mtime):
    objects, calls = cloud
    objects[".manifest.json"] = json.dumps({"files": {"valid-first.txt": 1, path: mtime}}).encode()
    objects["valid-first.txt"] = b"must-not-be-read-or-written"
    with pytest.raises(HTTPException) as error:
        server.restore_workspace(server.BackupRequest(bucket="fixture"))
    assert error.value.status_code == 400
    assert ("download", "valid-first.txt") not in calls


def test_unreadable_isolated_snapshot_stops_before_creating_cloud_client(cloud, monkeypatch):
    monkeypatch.setattr(server, "execution_user", lambda: "sandbox")
    monkeypatch.setattr(server, "_exec_env", lambda: {})
    def storage(operation, arguments, env):
        if operation == "json_read":
            return {"files": {"private.txt": 1}}
        raise FileWorkerError("unreadable snapshot")
    monkeypatch.setattr(server, "storage_request", storage)
    monkeypatch.setattr(server, "_scan_workspace", lambda _path: pytest.fail("fell back to a privileged scan"))
    with pytest.raises(FileWorkerError):
        server.backup_workspace(server.BackupRequest(bucket="fixture"))
    assert cloud[1] == []


def test_symlinked_old_directory_cannot_authorize_cloud_deletion(cloud, monkeypatch):
    monkeypatch.setattr(server, "execution_user", lambda: "sandbox")
    monkeypatch.setattr(server, "_exec_env", lambda: {})
    def storage(operation, arguments, env):
        if operation == "json_read":
            return {"files": {"old-directory/file.txt": 1}}
        return {"files": {}, "skipped_directories": ["old-directory"]}
    monkeypatch.setattr(server, "storage_request", storage)
    with pytest.raises(HTTPException) as error:
        server.backup_workspace(server.BackupRequest(bucket="fixture"))
    assert error.value.status_code == 409
    assert cloud[1] == []
