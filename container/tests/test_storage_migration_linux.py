"""Real offline volume/journal migration in an explicit disposable container.

Run with the same read-only source mount and network-disabled test image as
test_execution_identity_linux.py. Never mount an existing user data volume.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import pwd
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import storage_migration as migration
from resource_gate import Fence, ResourceGate

ENABLED = (sys.platform == "linux" and os.geteuid() == 0
           and os.environ.get("OPENBOX_ISOLATION_TEST_CONTAINER") == "1"
           and Path("/.dockerenv").exists())


@unittest.skipUnless(ENABLED, "requires an explicit disposable Linux test container")
class StorageMigrationLinuxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.account = pwd.getpwnam("sandbox")
        except KeyError:
            subprocess.run(["useradd", "-m", "-s", "/bin/bash", "sandbox"], check=True)
            cls.account = pwd.getpwnam("sandbox")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="openbox-storage-migration-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o755)
        self.data, self.work = self.root / "data", self.root / "workspace"
        self.data.mkdir()
        self.work.mkdir()
        os.chown(self.data, self.account.pw_uid, self.account.pw_gid)
        self.data.chmod(0o775)
        (self.data / "skills").mkdir()
        (self.data / "mcp").mkdir()
        self.config = self.data / "mcp" / "config.json"
        self.config.write_bytes(b'{"servers":{"fixture":{"command":"fixture-server"}}}')
        self.config.chmod(0o400)
        self.output = self.work / "keep.bin"
        self.output.write_bytes(bytes(range(256)) * 32)
        self.output.chmod(0o444)
        self.executable = self.work / "keep.sh"
        self.executable.write_text("#!/bin/sh\nprintf preserved\\n\n")
        self.executable.chmod(0o755)
        os.link(self.output, self.work / "keep-alias.bin")
        (self.work / "skills").symlink_to(self.data / "skills")
        self.private = self.root / "private-record"
        self.private.write_text("outside-user-scope")
        self.private.chmod(0o600)
        (self.work / "private-link").symlink_to(self.private)
        self.journal = self.data / migration.CONTROL / migration.JOURNAL
        self.gate = ResourceGate(self.journal)
        self.fence = Fence("a" * 64, 1, "automation", "workspace-fixture")
        self.gate.bind(self.fence, "initial-bind", self.gate.status()["journal_id"])
        headers = {"x-openbox-resource": self.fence.resource_id, "x-openbox-resource-epoch": "1",
                   "x-openbox-resource-journal": self.gate.status()["journal_id"],
                   "x-openbox-resource-owner": "automation", "x-openbox-resource-owner-id": self.fence.owner_id,
                   "x-openbox-resource-operation": "existing-effect"}
        for name in ("completed", "unknown", "running"):
            operation = self.gate.admit({**headers, "x-openbox-resource-step": name}, "POST", "/execute")
            self.gate.checkpoint(operation)
            if name != "running":
                operation["quiescent"] = name == "completed"
                self.gate.finish(operation)
        self.gate.close(self.fence, "existing-close", self.gate.status()["journal_id"])
        self.before = self.gate.status()
        self.identity = self.before["journal_id"]
        self.snapshot = migration._journal(self.journal, self.identity)
        self.contents = {self.config: self.config.read_bytes(), self.output: self.output.read_bytes(),
                         self.executable: self.executable.read_bytes(), self.private: self.private.read_bytes()}
        self.args = {"data_root": str(self.data), "workspace_root": str(self.work), "executor": "sandbox",
                     "expected_journal_id": self.identity, "offline": True}

    def migrate(self, **kwargs):
        return migration.migrate(**{**self.args, **kwargs})

    def assert_preserved(self):
        self.assertEqual(ResourceGate(self.journal).status(), self.before)
        self.assertEqual(migration._journal(self.journal, self.identity), self.snapshot)
        for path, body in self.contents.items():
            self.assertEqual(path.read_bytes(), body)
        self.assertEqual(self.private.stat().st_uid, 0)
        self.assertEqual(self.private.stat().st_mode & 0o777, 0o600)

    def test_migrates_in_place_preserves_history_and_kernel_denies_control_access(self):
        inode = self.journal.stat().st_ino
        result = self.migrate()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["blocking_count"], 2)
        self.assertEqual(self.journal.stat().st_ino, inode)
        self.assert_preserved()
        self.assertEqual(self.data.stat().st_uid, 0)
        self.assertEqual(self.data.stat().st_mode & 0o7777, 0o1770)
        self.assertEqual(self.journal.parent.stat().st_mode & 0o7777, 0o700)
        self.assertEqual(self.output.stat().st_uid, self.account.pw_uid)
        self.assertEqual(self.output.stat().st_ino, (self.work / "keep-alias.bin").stat().st_ino)
        self.assertEqual(os.readlink(self.work / "skills"), str(self.data / "skills"))
        self.assertTrue(os.stat(self.executable).st_mode & 0o100)
        from execution_identity import prepare_child, protect_path
        with patch.dict(os.environ, {"OPENBOX_EXECUTOR_USER": "sandbox"}):
            protect_path(self.journal)
            code = """
import json, os, sys
from pathlib import Path
data, work = map(Path, sys.argv[1:])
denied = []
for action in [lambda: (data/'openbox-control/control.sqlite3').read_bytes(),
               lambda: (data/'openbox-control').rename(data/'stolen-control'),
               lambda: (work/'private-link').write_text('must-not-change')]:
    try: action()
    except PermissionError: denied.append(True)
    else: denied.append(False)
(data/'ordinary-setting').write_text('allowed')
(work/'ordinary-output').write_text('allowed')
print(json.dumps(denied))
"""
            command, env = prepare_child([sys.executable, "-I", "-c", code, str(self.data), str(self.work)], {})
            probe = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(json.loads(probe.stdout), [True, True, True])
        self.assert_preserved()
        repeated = self.migrate()
        self.assertEqual(repeated, result)
        self.assertEqual(self.journal.stat().st_ino, inode)

    def test_cli_requires_offline_and_pinned_identity_and_never_logs_file_contents(self):
        command = [sys.executable, "-I", str(SOURCE / "storage_migration.py"), "--data-root", str(self.data),
                   "--workspace-root", str(self.work), "--executor", "sandbox", "--expected-journal-id", self.identity]
        refused = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("offline", refused.stderr)
        self.assertEqual(self.data.stat().st_uid, self.account.pw_uid)
        accepted = subprocess.run([*command, "--offline"], capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(json.loads(accepted.stdout)["journal_id"], self.identity)
        self.assertNotIn("fixture-server", accepted.stdout + accepted.stderr)
        self.assert_preserved()

    def test_interrupted_migration_resumes_without_resetting_unknown_operations(self):
        original = migration._handoff
        def stop_after_data(fd, nodes, uid, gid):
            original(fd, nodes, uid, gid)
            raise InterruptedError("simulated power loss after user-data ownership change")
        with patch.object(migration, "_handoff", stop_after_data):
            with self.assertRaises(InterruptedError):
                self.migrate()
        marker = self.journal.parent / migration.CHECKPOINT
        self.assertEqual(json.loads(marker.read_text())["state"], "preparing")
        self.assert_preserved()
        with self.assertRaisesRegex(migration.MigrationError, "interrupted offline"):
            migration.require_ready(str(self.journal), "sandbox")
        env = {**os.environ, "SESSION_API_KEY": "fixture-key", "OPENBOX_EXECUTOR_USER": "sandbox",
               "OPENBOX_RESOURCE_CONTROL_DB": str(self.journal)}
        start = subprocess.run([sys.executable, "-I", str(SOURCE / "action_server.py")],
                               env=env, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(start.returncode, 0)
        self.assertIn("interrupted offline storage migration", start.stderr)
        self.assertEqual(self.migrate()["state"], "ready")
        migration.require_ready(str(self.journal), "sandbox")
        self.assert_preserved()

    def test_interrupted_migration_refuses_changed_journal_without_reset(self):
        with patch.object(migration, "_handoff", side_effect=InterruptedError("fixture interruption")):
            with self.assertRaises(InterruptedError):
                self.migrate()
        # A real committed change after the checkpoint must stop resume, even
        # when it preserves the journal identity. Never refresh the baseline.
        with closing(sqlite3.connect(self.journal)) as db:
            db.execute("UPDATE control SET command_id='late-close'")
            db.commit()
        with self.assertRaisesRegex(migration.MigrationError, "Journal changed"):
            self.migrate()
        self.assertEqual(self.gate.status()["control"]["command_id"], "late-close")
        self.assertEqual(self.gate.status()["blocking_count"], 2)

    def test_missing_or_wrong_journal_never_creates_a_replacement(self):
        with self.assertRaisesRegex(migration.MigrationError, "pinned identity"):
            self.migrate(expected_journal_id="0" * 32)
        self.assertEqual(self.data.stat().st_uid, self.account.pw_uid)
        saved = self.journal.with_name("original-retained.sqlite3")
        self.journal.rename(saved)
        with self.assertRaises(FileNotFoundError):
            self.migrate()
        self.assertFalse(self.journal.exists())
        self.assertTrue(saved.exists())

    def test_external_hardlink_refused_before_ownership_changes(self):
        os.link(self.private, self.work / "outside-hardlink")
        with self.assertRaisesRegex(migration.MigrationError, "outside the migration scope"):
            self.migrate()
        self.assertEqual(self.data.stat().st_uid, self.account.pw_uid)
        self.assertEqual(self.private.stat().st_uid, 0)
        self.assert_preserved()

    def test_external_symlink_hardlink_is_also_outside_the_ownership_scope(self):
        outside = self.root / "outside-link"
        outside.symlink_to(self.private)
        os.link(outside, self.work / "linked-link", follow_symlinks=False)
        with self.assertRaisesRegex(migration.MigrationError, "outside the migration scope"):
            self.migrate()
        self.assertEqual(outside.lstat().st_uid, 0)
        self.assertEqual(self.data.stat().st_uid, self.account.pw_uid)

    def test_committed_wal_survives_a_writer_crash_and_migration(self):
        code = """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute('PRAGMA wal_autocheckpoint=0')
db.execute("UPDATE control SET command_id='wal-commit-before-crash'")
db.commit()
os._exit(0)
"""
        subprocess.run([sys.executable, "-I", "-c", code, str(self.journal)], timeout=10, check=True)
        self.assertTrue(self.journal.with_name(self.journal.name + "-wal").exists())
        self.assertNotIn(b"wal-commit-before-crash", self.journal.read_bytes())
        self.before["control"]["command_id"] = "wal-commit-before-crash"
        self.snapshot = migration._journal(self.journal, self.identity)
        result = self.migrate()
        self.assertEqual(result["snapshot_sha256"], self.snapshot["snapshot_sha256"])
        self.assert_preserved()

    def test_control_symlink_and_special_user_file_are_refused(self):
        fifo = self.work / "pending-pipe"
        os.mkfifo(fifo)
        with self.assertRaisesRegex(migration.MigrationError, "FIFOs"):
            self.migrate()
        self.assertEqual(self.data.stat().st_uid, self.account.pw_uid)
        fifo.unlink()
        original = self.journal.parent
        saved = original.with_name("retained-control")
        original.rename(saved)
        original.symlink_to(saved, target_is_directory=True)
        with self.assertRaises(OSError):
            self.migrate()
        self.assertEqual(self.data.stat().st_uid, self.account.pw_uid)

    def test_volume_lock_and_live_executor_refuse_migration(self):
        with migration._root(self.data):
            with self.assertRaisesRegex(migration.MigrationError, "holds this volume"):
                self.migrate()
        from execution_identity import prepare_child
        with patch.dict(os.environ, {"OPENBOX_EXECUTOR_USER": "sandbox"}):
            command, env = prepare_child([sys.executable, "-I", "-c", "import time;print('ready',flush=True);time.sleep(30)"], {})
        child = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            with self.assertRaisesRegex(migration.MigrationError, "Stop executor"):
                self.migrate()
        finally:
            child.kill()
            child.wait(timeout=5)
            child.stdout.close()
        self.assertEqual(self.data.stat().st_uid, self.account.pw_uid)
        self.assert_preserved()

    def test_checkpoint_scope_corruption_or_unexpected_database_are_not_overwritten(self):
        marker = self.journal.parent / migration.CHECKPOINT
        marker.write_text('{"version":"future-version","state":"ready"}')
        with self.assertRaisesRegex(migration.MigrationError, "Unsupported migration checkpoint"):
            self.migrate()
        self.assertIn("future-version", marker.read_text())
        marker.unlink()
        with closing(sqlite3.connect(self.journal)) as db:
            db.execute("CREATE TABLE unfamiliar (id INTEGER)")
            db.commit()
        with self.assertRaisesRegex(migration.MigrationError, "Unsupported existing journal schema"):
            self.migrate()
        self.assertEqual(self.data.stat().st_uid, self.account.pw_uid)

    def test_live_root_service_is_not_mistaken_for_an_offline_volume(self):
        service = self.root / "action_server.py"
        service.write_text("import time\nprint('ready',flush=True)\ntime.sleep(30)\n")
        child = subprocess.Popen([sys.executable, "-I", str(service)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            with self.assertRaisesRegex(migration.MigrationError, "Stop executor and Action Server"):
                self.migrate()
        finally:
            child.kill()
            child.wait(timeout=5)
            child.stdout.close()
        self.assertEqual(self.data.stat().st_uid, self.account.pw_uid)
        self.assert_preserved()

    def test_ready_startup_allows_new_history_but_rejects_replaced_identity(self):
        self.migrate()
        with closing(sqlite3.connect(self.journal)) as db:
            db.execute("UPDATE control SET command_id='another-close'")
            db.commit()
        migration.require_ready(str(self.journal), "sandbox")
        with closing(sqlite3.connect(self.journal)) as db:
            db.execute("UPDATE identity SET journal_id=?", ("f" * 32,))
            db.commit()
        with self.assertRaisesRegex(migration.MigrationError, "journal identity changed"):
            migration.require_ready(str(self.journal), "sandbox")

    def test_live_former_file_owner_must_stop_before_identity_migration(self):
        old = pwd.getpwnam("nobody")
        os.chown(self.output, old.pw_uid, old.pw_gid)
        from execution_identity import prepare_child
        with patch.dict(os.environ, {"OPENBOX_EXECUTOR_USER": "nobody"}):
            command, env = prepare_child([sys.executable, "-I", "-c", "import time;print('ready',flush=True);time.sleep(30)"], {})
        child = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            with self.assertRaisesRegex(migration.MigrationError, "Stop executor"):
                self.migrate()
            self.assertEqual(self.output.stat().st_uid, old.pw_uid)
        finally:
            child.kill()
            child.wait(timeout=5)
            child.stdout.close()
        self.migrate()
        self.assertEqual(self.output.stat().st_uid, self.account.pw_uid)
        self.assert_preserved()


if __name__ == "__main__":
    unittest.main(verbosity=2)
