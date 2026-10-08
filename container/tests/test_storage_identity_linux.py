"""Real filesystem/uid isolation; cloud storage is an in-memory test double.

Use the same explicit disposable-container invocation as
test_execution_identity_linux.py. Never mount an existing data volume.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_execution_identity_linux as fixture


@unittest.skipUnless(fixture.ENABLED, "requires the disposable Linux test container")
class StorageIdentityLinuxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture.ExecutionIdentityLinuxTests.setUpClass.__func__(cls)
        for path in (Path("/data"), Path("/data/mcp"), Path("/workspace")):
            path.mkdir(parents=True, exist_ok=True)
            os.chown(path, cls.account.pw_uid, cls.account.pw_gid)

    setUp = fixture.ExecutionIdentityLinuxTests.setUp
    tearDown = fixture.ExecutionIdentityLinuxTests.tearDown
    assert_isolated = fixture.ExecutionIdentityLinuxTests.assert_isolated

    def test_mcp_config_is_atomic_unprivileged_and_does_not_replace_corrupt_or_private_sources(self):
        import httpx
        import file_worker
        from file_worker import FileWorkerError, storage_request
        async def run():
            path = self.server.MCP_CONFIG_PATH
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://fixture") as client:
                payload = {"name": "storage-fixture", "type": "stdio", "command": sys.executable, "args": ["-c", "pass"]}
                added = await client.post("/mcp/servers", headers=self.headers, json=payload)
                self.assertEqual(added.status_code, 200, added.text)
                self.assertEqual(path.stat().st_uid, self.account.pw_uid)
                saved = path.read_bytes()
                loaded = await client.get("/mcp/servers", headers=self.headers)
                self.assertIn("storage-fixture", {entry["name"] for entry in loaded.json()})
                path.write_bytes(b"{invalid-json")
                refused = await client.post("/mcp/servers", headers=self.headers, json={**payload, "name": "must-not-save"})
                self.assertEqual(refused.status_code, 502, refused.text)
                self.assertEqual(path.read_bytes(), b"{invalid-json")
                path.unlink()
                os.mkfifo(path, 0o644)
                original_timeout = file_worker.TIMEOUT
                file_worker.TIMEOUT = 2
                try:
                    # A FIFO must exit by refusing the file type, rather than
                    # keeping the supervisor waiting for a writer until timeout.
                    with self.assertRaisesRegex(FileWorkerError, "refused the operation"):
                        storage_request("json_read", {"path": str(path)}, self.server._exec_env())
                finally:
                    file_worker.TIMEOUT = original_timeout
                private = Path(os.environ["PRIVATE_FIXTURE"]).parent / "private-config.json"
                private.write_text('{"servers":{"private-server-must-not-escape":{"command":"private"}}}')
                path.unlink()
                path.symlink_to(private)
                denied = await client.get("/mcp/servers", headers=self.headers)
                self.assertEqual(denied.status_code, 502, denied.text)
                self.assertNotIn("private-server-must-not-escape", denied.text)
                refused = await client.post("/mcp/servers", headers=self.headers, json=payload)
                self.assertEqual(refused.status_code, 502)
                self.assertIn("private-server-must-not-escape", private.read_text())
                path.unlink()
                storage_request("json_write", {"path": str(path)}, self.server._exec_env(), saved)
                removed = await client.delete("/mcp/servers/storage-fixture", headers=self.headers)
                self.assertEqual(removed.status_code, 200, removed.text)
        asyncio.run(run())

    def test_protected_paths_refuse_executor_owned_or_writable_ancestors_and_symlinks(self):
        from execution_identity import IsolationError, protect_path
        secure = self.root / "control-fixture" / "state.sqlite3"
        protect_path(secure, create_parent=True)
        self.assertEqual(secure.parent.stat().st_mode & 0o777, 0o700)
        secure.write_bytes(b"private-state")
        protect_path(secure)
        # A private 0700 leaf underneath an executor-owned directory is still
        # renameable by that executor and must not pass startup validation.
        unsafe = self.work / "root-owned-private" / "state.sqlite3"
        unsafe.parent.mkdir(mode=0o700)
        unsafe.write_bytes(b"must-remain")
        with self.assertRaises(IsolationError):
            protect_path(unsafe, create_parent=True)
        alias = self.root / "control-alias"
        alias.symlink_to(secure.parent)
        with self.assertRaises(IsolationError):
            protect_path(alias / secure.name)
        secure.chmod(0o666)
        with self.assertRaises(IsolationError):
            protect_path(secure)
        secure.chmod(0o600)
        refused = subprocess.run([sys.executable, "-I", str(fixture.SERVER_DIR / "action_server.py")],
            env={**os.environ, "OPENBOX_RESOURCE_CONTROL_DB": str(unsafe)}, capture_output=True, timeout=10)
        self.assertNotEqual(refused.returncode, 0)
        self.assertEqual(unsafe.read_bytes(), b"must-remain")

    def test_diagnostic_code_runs_without_root_or_supervisor_credentials(self):
        original = self.server.DIAG_TOOL
        self.server.DIAG_TOOL = self.probe_file
        try:
            self.assert_isolated(asyncio.run(self.server.browser_diag()))
        finally:
            self.server.DIAG_TOOL = original

    def test_kill_route_cannot_signal_root_processes_and_can_stop_its_own_uid(self):
        import httpx
        from execution_identity import prepare_child
        root_process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"], start_new_session=True)
        argv, env = prepare_child([sys.executable, "-c", "import time;time.sleep(60)"], self.server._exec_env())
        child = subprocess.Popen(argv, env=env, start_new_session=True)
        async def run():
            for _ in range(100):
                if Path(f"/proc/{child.pid}").stat().st_uid == self.account.pw_uid:
                    break
                await asyncio.sleep(0.02)
            else:
                self.fail("fixture did not become unprivileged")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://fixture") as client:
                denied = await client.post("/kill", headers=self.headers, json={"pid": root_process.pid})
                self.assertEqual(denied.status_code, 403, denied.text)
                self.assertIsNone(root_process.poll())
                for pid in (0, -1, 1):
                    rejected = await client.post("/kill", headers=self.headers, json={"pid": pid})
                    self.assertEqual(rejected.status_code, 422, rejected.text)
                killed = await client.post("/kill", headers=self.headers, json={"pid": child.pid})
                self.assertEqual(killed.status_code, 200, killed.text)
                child.wait(timeout=3)
                self.assertLess(child.returncode, 0)
        try:
            asyncio.run(run())
        finally:
            for process in (root_process, child):
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=3)

    def test_backup_restore_use_scoped_files_without_giving_cloud_credentials_to_workers(self):
        import httpx
        from file_worker import storage_request
        # Only the provider is substituted. Every file read/write, manifest,
        # permission transition and HTTP route remains the production path.
        blobs, uploads, deletes = {}, [], []
        class Blob:
            def __init__(self, key):
                self.key = key
            def exists(self):
                return self.key in blobs
            def download_as_bytes(self):
                return blobs[self.key]
            def upload_from_string(self, data):
                uploads.append(self.key)
                blobs[self.key] = data.encode() if isinstance(data, str) else data
            def delete(self):
                deletes.append(self.key)
                blobs.pop(self.key, None)
        class Client:
            def bucket(self, name):
                if name != "fixture-only":
                    raise AssertionError("Unexpected bucket")
                return types.SimpleNamespace(blob=Blob)
        originals = {name: sys.modules.get(name) for name in ("google", "google.cloud", "google.cloud.storage")}
        google, cloud, storage = (types.ModuleType(name) for name in originals)
        google.cloud, cloud.storage = cloud, storage
        storage.Client = Client
        sys.modules.update({"google": google, "google.cloud": cloud, "google.cloud.storage": storage})
        binary = bytes(range(256)) * 1000
        mtime = 1_800_000_000.125
        env = self.server._exec_env()
        storage_request("workspace_write", {"path": "storage-fixture/source.bin", "mtime": mtime}, env, binary)
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://fixture") as client:
                backup = await client.post("/backup", headers=self.headers, json={"bucket": "fixture-only"})
                self.assertEqual(backup.status_code, 200, backup.text)
                self.assertEqual(blobs["storage-fixture/source.bin"], binary)
                self.assertEqual(self.server.MANIFEST_PATH.stat().st_uid, self.account.pw_uid)
                blobs["storage-fixture/restored.bin"] = binary[::-1]
                blobs[".manifest.json"] = json.dumps({"files": {"storage-fixture/restored.bin": mtime}}).encode()
                restored = await client.post("/restore", headers=self.headers, json={"bucket": "fixture-only"})
                self.assertEqual(restored.status_code, 200, restored.text)
                target = Path("/workspace/storage-fixture/restored.bin")
                self.assertEqual(target.read_bytes(), binary[::-1])
                self.assertEqual(target.stat().st_uid, self.account.pw_uid)
                self.assertAlmostEqual(target.stat().st_mtime, mtime)
                # Validate every remote manifest entry before the first write.
                blobs["storage-fixture/must-not-appear"] = b"forbidden"
                blobs[".manifest.json"] = json.dumps({"files": {
                    "storage-fixture/must-not-appear": mtime, "../outside": mtime}}).encode()
                rejected = await client.post("/restore", headers=self.headers, json={"bucket": "fixture-only"})
                self.assertEqual(rejected.status_code, 400, rejected.text)
                self.assertFalse(Path("/workspace/storage-fixture/must-not-appear").exists())
                secret = Path(os.environ["PRIVATE_FIXTURE"])
                alias = Path("/workspace/storage-fixture/private-link")
                alias.symlink_to(secret.parent)
                path = "storage-fixture/private-link/" + secret.name
                blobs[path] = b"must-not-write"
                blobs[".manifest.json"] = json.dumps({"files": {path: mtime}}).encode()
                rejected = await client.post("/restore", headers=self.headers, json={"bucket": "fixture-only"})
                self.assertEqual(rejected.status_code, 502, rejected.text)
                self.assertEqual(secret.read_text(), "root-only-test-record")
                # An old directory replaced with a link is not evidence of
                # deletion; no cloud upload/delete may start for that snapshot.
                storage_request("json_write", {"path": str(self.server.MANIFEST_PATH)}, env,
                    json.dumps({"files": {path: mtime}}).encode())
                uploads.clear()
                deletes.clear()
                rejected = await client.post("/backup", headers=self.headers, json={"bucket": "fixture-only"})
                self.assertEqual(rejected.status_code, 409, rejected.text)
                self.assertEqual(uploads, [])
                self.assertEqual(deletes, [])
                alias.unlink()
        try:
            asyncio.run(run())
        finally:
            for name, module in originals.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module


if __name__ == "__main__":
    unittest.main(verbosity=2)
