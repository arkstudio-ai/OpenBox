"""Real UID/capability and transport regression in a disposable Linux container.

Run with Python (no pytest dependency) in a container that has the Action
Server requirements installed, this directory mounted read-only, networking
disabled and OPENBOX_ISOLATION_TEST_CONTAINER=1. Fixtures live only in the
disposable container; no existing desktop, data volume or credentials are used.
"""
import asyncio
import io
import json
import os
from pathlib import Path
import pwd
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile


ENABLED = (sys.platform == "linux" and os.geteuid() == 0
           and os.environ.get("OPENBOX_ISOLATION_TEST_CONTAINER") == "1"
           and Path("/.dockerenv").exists())
SERVER_DIR = Path(__file__).resolve().parents[1]

PROBE = r'''
import json, os, subprocess, sys
from pathlib import Path

def probe():
    status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines())
    def denied(path):
        try:
            Path(path).read_bytes()
        except PermissionError:
            return True
        return False
    try:
        os.setresuid(0, 0, 0)
        regain_denied = False
    except PermissionError:
        regain_denied = True
    elevated = subprocess.run([os.environ["SETUID_FIXTURE"], "-I", "-S", "-c",
        "import os;print(os.geteuid())"], capture_output=True, text=True, check=True)
    return {"uid": os.getresuid(), "gid": os.getresgid(), "groups": os.getgroups(), "session_id": os.getsid(0),
        "nnp": status["NoNewPrivs"].strip(),
        "caps": {k: int(status[k].strip(), 16) for k in ("CapInh", "CapPrm", "CapEff", "CapAmb")},
        "regain_denied": regain_denied, "setuid_uid": int(elevated.stdout.strip()),
        "private_file_denied": denied(os.environ["PRIVATE_FIXTURE"]),
        "supervisor_env_denied": denied("/proc/" + os.environ["ROOT_FIXTURE_PID"] + "/environ"),
        "service_secret_absent": all(k not in os.environ for k in (
            "SESSION_API_KEY", "OPENAI_API_KEY", "OPENBOX_RESOURCE_CONTROL_DB",
            "OPENBOX_EXECUTOR_USER", "OPENBOX_CHILD_ENV_PAYLOAD")),
        "home": os.environ.get("HOME"), "user": os.environ.get("USER")}

if __name__ == "__main__":
    print(json.dumps(probe()), flush=True)
'''


@unittest.skipUnless(ENABLED, "requires an explicitly enabled disposable Linux root container")
class ExecutionIdentityLinuxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.account = pwd.getpwnam("sandbox")
        except KeyError:
            subprocess.run(["useradd", "-m", "-s", "/bin/bash", "sandbox"], check=True)
            cls.account = pwd.getpwnam("sandbox")
        assert cls.account.pw_uid > 0 and cls.account.pw_gid > 0
        cls.root = Path(tempfile.mkdtemp(prefix="openbox-identity-fixture-"))
        cls.root.chmod(0o755)
        cls.work = cls.root / "work"
        cls.work.mkdir()
        os.chown(cls.work, cls.account.pw_uid, cls.account.pw_gid)
        skills = Path("/data/skills")
        skills.mkdir(parents=True, exist_ok=True)
        os.chown(skills, cls.account.pw_uid, cls.account.pw_gid)
        private = cls.root / "private"
        private.mkdir(mode=0o700)
        (private / "control-fixture").write_text("root-only-test-record")
        cls.probe_file = cls.root / "probe.py"
        cls.probe_file.write_text(PROBE)
        setuid = cls.root / "setuid-python"
        shutil.copyfile(sys.executable, setuid)
        setuid.chmod(0o4755)
        os.environ.update(OPENBOX_EXECUTOR_USER="sandbox", SESSION_API_KEY="fixture-key",
            OPENAI_API_KEY="fixture-provider-secret", PRIVATE_FIXTURE=str(private / "control-fixture"),
            SETUID_FIXTURE=str(setuid), ROOT_FIXTURE_PID=str(os.getpid()),
            OPENBOX_RESOURCE_CONTROL_DB=str(private / "control.sqlite3"))
        sys.path.insert(0, str(SERVER_DIR))
        import action_server
        cls.server = action_server
        cls.headers = {"X-API-Key": "fixture-key"}

    def setUp(self):
        # Every test must return its fixture process within a finite deadline.
        def timeout(_signum, _frame):
            raise TimeoutError("Linux isolation test deadline exceeded")
        signal.signal(signal.SIGALRM, timeout)
        signal.alarm(60)
        self.original_env = dict(os.environ)

    def tearDown(self):
        signal.alarm(0)
        os.environ.clear()
        os.environ.update(self.original_env)

    def assert_isolated(self, result):
        self.assertEqual(result["uid"], [self.account.pw_uid] * 3)
        self.assertEqual(result["gid"], [self.account.pw_gid] * 3)
        self.assertEqual(result["groups"], [])
        self.assertEqual(result["nnp"], "1")
        self.assertEqual(set(result["caps"].values()), {0})
        self.assertEqual(result["setuid_uid"], self.account.pw_uid)
        for key in ("regain_denied", "private_file_denied", "supervisor_env_denied", "service_secret_absent"):
            self.assertTrue(result[key], key)
        self.assertEqual(result["home"], self.account.pw_dir)
        self.assertEqual(result["user"], self.account.pw_name)

    def test_execute_and_stream_keep_the_protocol_and_drop_privileges(self):
        import httpx
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://fixture") as client:
                alive = await client.get("/alive")
                self.assertIn("unprivileged_child_v1", alive.json()["capabilities"])
                for route in ("/execute", "/execute_stream"):
                    response = await client.post(route, headers=self.headers, json={
                        "command": f"{sys.executable} {self.probe_file}", "workdir": str(self.work), "timeout": 10})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertIn("X-OpenBox-Remote-Operation", response.headers)
                    if route == "/execute":
                        self.assertEqual(response.json()["exit_code"], 0, response.text)
                        output = response.json()["stdout"]
                    else:
                        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
                        output = "".join(e["content"] for e in events if e.get("type") == "stdout")
                        self.assertEqual(events[-1]["exit_code"], 0)
                    self.assert_isolated(json.loads(output))
            self.assertFalse(self.server._resource_gate.status()["remote_exclusivity_verified"])
        asyncio.run(run())

    def test_skill_install_script_runs_unprivileged_in_a_prepared_writable_directory(self):
        target = self.work / "skill"
        target.mkdir()
        os.chown(target, self.account.pw_uid, self.account.pw_gid)
        (target / "install.sh").write_text(f"{sys.executable} {self.probe_file} > installed.json\n")
        log = self.server._run_skill_install_script(target)
        self.assertEqual(log.strip(), "")
        self.assert_isolated(json.loads((target / "installed.json").read_text()))
        self.assertEqual((target / "installed.json").stat().st_uid, self.account.pw_uid)

    def test_stdio_plugin_loader_environment_runs_only_after_dropping_privileges(self):
        plugin = self.root / "plugin.py"
        plugin.write_text("from probe import probe\nfrom mcp.server.fastmcp import FastMCP\n"
            "mcp = FastMCP('identity-fixture')\n@mcp.tool()\ndef identity() -> dict:\n    return probe()\n"
            "mcp.run(transport='stdio')\n")
        loader = self.root / "python-loader"
        loader.mkdir()
        marker = self.work / "loader-uid"
        (loader / "sitecustomize.py").write_text(
            f"import os\nwith open({str(marker)!r}, 'a') as output: output.write(str(os.geteuid())+'\\n')\n")
        async def run():
            manager = self.server.ContainerMcpManager()
            async with manager._stdio_session({"command": sys.executable, "args": [str(plugin)],
                    "env": {"PYTHONPATH": str(loader), "PLUGIN_TOKEN": "fixture-only"}, "timeout": 10}) as session:
                result = await session.call_tool("identity", {})
                self.assertFalse(getattr(result, "isError", False))
                self.assert_isolated(json.loads(result.content[0].text))
        asyncio.run(run())
        self.assertTrue(marker.exists())
        self.assertEqual(set(marker.read_text().splitlines()), {str(self.account.pw_uid)})

    def test_pty_terminal_uses_the_same_identity_without_exposing_service_credentials(self):
        from starlette.testclient import TestClient
        result_file = self.work / "pty-result.json"
        with TestClient(self.server.app) as client:
            with client.websocket_connect("/terminal?api_key=fixture-key") as socket:
                socket.send_bytes(b"\0" + f"{sys.executable} {self.probe_file} > {result_file}\n".encode())
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        result = json.loads(result_file.read_text())
                        break
                    except (FileNotFoundError, json.JSONDecodeError):
                        time.sleep(0.05)
                else:
                    self.fail("PTY child did not produce the identity receipt")
                self.assert_isolated(result)
            with self.assertRaises(ProcessLookupError):
                os.kill(result["session_id"], 0)

    def test_relay_process_does_not_inherit_root_or_api_credentials(self):
        # A real, local test executable takes npm's place; Popen/exec/privilege
        # setup and the relay readiness socket are not mocked.
        tools = self.root / "bin"
        tools.mkdir()
        npm = tools / "npm"
        result_file = self.work / "relay-result.json"
        npm.write_text(f"#!{sys.executable}\nimport json,socket,sys\nsys.path.insert(0,{str(self.root)!r})\n"
            f"from probe import probe\nwith open({str(result_file)!r},'w') as output: json.dump(probe(),output)\n"
            "listener=socket.socket()\nlistener.bind(('127.0.0.1',9222))\nlistener.listen()\n"
            "while True:\n    connection,_=listener.accept()\n    connection.close()\n")
        npm.chmod(0o755)
        os.environ["PATH"] = str(tools) + os.pathsep + os.environ["PATH"]
        builtin = self.root / "builtin"
        (builtin / "dev-browser").mkdir(parents=True)
        old_dir = self.server.BUILTIN_SKILLS_DIR
        self.server.BUILTIN_SKILLS_DIR = builtin
        try:
            result = asyncio.run(self.server.dev_browser_start())
            self.assertEqual(result["status"], "running")
            self.assert_isolated(json.loads(result_file.read_text()))
        finally:
            asyncio.run(self.server.dev_browser_stop())
            self.server.BUILTIN_SKILLS_DIR = old_dir

    def test_invalid_identity_refuses_startup_and_never_runs_target(self):
        import execution_identity
        marker = self.work / "must-not-run"
        for name in ("root", "missing-fixture-user"):
            env = {**os.environ, "OPENBOX_EXECUTOR_USER": name}
            failed = subprocess.run([sys.executable, str(SERVER_DIR / "action_server.py")],
                env=env, capture_output=True, text=True, timeout=10)
            self.assertNotEqual(failed.returncode, 0)
            env["OPENBOX_CHILD_ENV_PAYLOAD"] = json.dumps({"TOKEN": "private-fixture-token"})
            failed = subprocess.run([sys.executable, "-I", "-S", execution_identity.__file__,
                "--user", name, "--", "touch", str(marker)], env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(failed.returncode, 126)
            self.assertFalse(marker.exists())
            self.assertNotIn("private-fixture-token", failed.stdout + failed.stderr)

    def test_file_routes_read_write_search_and_binary_archives_as_the_sandbox_user(self):
        import httpx
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://fixture") as client:
                file = self.work / "file-route.txt"
                result = await client.post("/write_file", headers=self.headers, json={"path": str(file), "content": "alpha\n测试beta"})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(file.stat().st_uid, self.account.pw_uid)
                read = await client.post("/read_file", headers=self.headers, json={"path": str(file), "offset": 1, "limit": 1})
                self.assertEqual(read.json()["content"], "     2\t测试beta")
                listing = await client.post("/list_files", headers=self.headers, json={"path": str(self.work)})
                self.assertIn(file.name, {entry["name"] for entry in listing.json()["entries"]})
                glob = await client.post("/glob", headers=self.headers, json={"path": str(self.work), "pattern": "file-*.txt"})
                self.assertIn(str(file), glob.json()["files"])
                grep = await client.post("/grep", headers=self.headers, json={"path": str(file), "pattern": "测试beta"})
                self.assertEqual(grep.json()["exit_code"], 0, grep.text)
                self.assertIn("测试beta", grep.json()["output"])
                binary = bytes(range(256)) * 1000
                upload_dir = self.work / "binary"
                upload = await client.post("/upload", headers=self.headers, data={"destination": str(upload_dir)},
                    files={"file": ("二进制.bin", binary, "application/octet-stream")})
                self.assertEqual(upload.status_code, 200, upload.text)
                self.assertEqual((upload_dir / "二进制.bin").stat().st_uid, self.account.pw_uid)
                downloaded = await client.get("/download", headers=self.headers, params={"path": str(upload_dir / "二进制.bin")})
                self.assertEqual(downloaded.content, binary)
                self.assertIn("filename*=UTF-8''", downloaded.headers["content-disposition"])
                archive = await client.get("/download", headers=self.headers, params={"path": str(upload_dir)})
                self.assertEqual(archive.headers["content-type"], "application/zip")
                self.assertEqual(zipfile.ZipFile(io.BytesIO(archive.content)).read("二进制.bin"), binary)
                rejected = await client.post("/upload", headers=self.headers, data={"destination": str(upload_dir)},
                    files={"file": ("../file-route.txt", b"must-not-overwrite", "application/octet-stream")})
                self.assertEqual(rejected.status_code, 400, rejected.text)
                self.assertEqual(file.read_text(), "alpha\n测试beta")
        asyncio.run(run())

    def test_skill_archive_install_export_and_delete_stay_in_the_unprivileged_worker(self):
        import httpx
        name = "unpriv-worker-fixture"
        installed = Path("/data/skills") / name
        self.assertFalse(installed.exists())
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.writestr(f"{name}/SKILL.md", f"---\nname: {name}\ndescription: Local fixture.\n---\nTest.\n")
            archive.writestr(f"{name}/install.sh", f"{sys.executable} {self.probe_file} > uid.json\n")
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://fixture") as client:
                response = await client.post("/skills/upload", headers=self.headers,
                    files={"file": (name + ".zip", bundle.getvalue(), "application/zip")})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(installed.stat().st_uid, self.account.pw_uid)
                self.assert_isolated(json.loads((installed / "uid.json").read_text()))
                listing = await client.get("/skills", headers=self.headers)
                self.assertEqual(listing.status_code, 200, listing.text)
                self.assertIn(name, {skill["name"] for skill in listing.json()})
                catalog = await client.get("/catalog", headers=self.headers)
                self.assertEqual(catalog.status_code, 200, catalog.text)
                self.assertIn(name, {skill["name"] for skill in catalog.json()["skills"]})
                self.assertEqual(catalog.json()["boot_id"], self.server._ACTION_SERVER_BOOT_ID)
                version = await client.get("/catalog/version", headers=self.headers)
                self.assertEqual(version.json()["generation"], catalog.json()["generation"])
                cached = await client.get("/skills", headers={**self.headers, "If-None-Match": listing.headers["etag"]})
                self.assertEqual(cached.status_code, 304)
                exported = await client.get(f"/skills/{name}/archive", headers=self.headers)
                self.assertEqual(exported.status_code, 200, exported.text[:100] if exported.status_code != 200 else "")
                self.assertIn(f"{name}/SKILL.md", zipfile.ZipFile(io.BytesIO(exported.content)).namelist())
                removed = await client.delete(f"/skills/{name}", headers=self.headers)
                self.assertEqual(removed.status_code, 200, removed.text)
                self.assertFalse(installed.exists())
        asyncio.run(run())

    def test_catalogue_cannot_follow_a_skill_symlink_into_supervisor_private_data(self):
        import httpx
        fixture = Path("/data/skills/catalogue-permission-fixture")
        fixture.mkdir()
        os.chown(fixture, self.account.pw_uid, self.account.pw_gid)
        private = Path(os.environ["PRIVATE_FIXTURE"]).parent / "private-SKILL.md"
        marker = "private-description-must-not-escape"
        private.write_text(f"---\nname: catalogue-permission-fixture\ndescription: {marker}\n---\nPrivate.\n")
        link = fixture / "SKILL.md"
        link.symlink_to(private)
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://fixture") as client:
                for route in ("/skills", "/catalog", "/catalog/version"):
                    response = await client.get(route, headers=self.headers)
                    self.assertIn(response.status_code, (403, 502))
                    self.assertNotIn(marker, response.text)
        try:
            asyncio.run(run())
        finally:
            link.unlink()
            fixture.rmdir()

    def test_file_worker_cancellation_reaps_the_actual_child_without_writing(self):
        import file_worker
        import httpx
        original_spawn = file_worker.asyncio.create_subprocess_exec
        processes = []
        async def tracked_spawn(*args, **kwargs):
            process = await original_spawn(*args, **kwargs)
            processes.append(process)
            return process
        async def run():
            sent = asyncio.Event()
            async def partial_body():
                yield b'{"path":"'
                sent.set()
                await asyncio.Event().wait()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://fixture") as client:
                operation = asyncio.create_task(client.post("/write_file", headers={**self.headers, "Content-Type": "application/json"}, content=partial_body()))
                await asyncio.wait_for(sent.wait(), 5)
                operation.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await operation
                self.assertEqual(len(processes), 1)
                await asyncio.wait_for(processes[0].wait(), 3)
                self.assertIsNotNone(processes[0].returncode)
                with self.assertRaises(ProcessLookupError):
                    os.kill(processes[0].pid, 0)
        file_worker.asyncio.create_subprocess_exec = tracked_spawn
        try:
            asyncio.run(run())
        finally:
            file_worker.asyncio.create_subprocess_exec = original_spawn

    def test_invalid_file_executor_never_falls_back_to_the_root_handler(self):
        import httpx
        target = self.work / "invalid-worker-must-not-write"
        os.environ["OPENBOX_EXECUTOR_USER"] = "missing-fixture-user"
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://fixture") as client:
                response = await client.post("/write_file", headers=self.headers, json={"path": str(target), "content": "forbidden"})
                self.assertEqual(response.status_code, 502, response.text)
                self.assertFalse(target.exists())
        asyncio.run(run())

    def test_file_routes_cannot_read_or_write_private_paths_even_through_symlinks(self):
        import httpx
        secret = Path(os.environ["PRIVATE_FIXTURE"])
        alias = self.work / "private-alias"
        alias.symlink_to(secret)
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.app), base_url="http://fixture") as client:
                for target in (secret, alias, Path(f"/proc/{os.getpid()}/environ")):
                    read = await client.post("/read_file", headers=self.headers, json={"path": str(target)})
                    self.assertEqual(read.status_code, 403, read.text)
                    write = await client.post("/write_file", headers=self.headers, json={"path": str(target), "content": "must-not-write"})
                    self.assertEqual(write.status_code, 403, write.text)
                    download = await client.get("/download", headers=self.headers, params={"path": str(target)})
                    self.assertEqual(download.status_code, 403, download.text)
                self.assertEqual(secret.read_text(), "root-only-test-record")
                upload = await client.post("/upload", headers=self.headers, data={"destination": str(secret.parent)},
                    files={"file": (secret.name, b"must-not-write", "application/octet-stream")})
                self.assertEqual(upload.status_code, 403, upload.text)
                self.assertEqual(secret.read_text(), "root-only-test-record")
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main(verbosity=2)
