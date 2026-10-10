"""Bounded retained code deployment; no SDK, guest or real service action."""
import base64
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shlex
import sys

import pytest

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("bounded_wuying_deploy", ROOT / "backend/scripts/wuying_deploy_action_server.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def small_bundle():
    content = b"print('fixed code fixture')\n"
    return {name: {"sha256": hashlib.sha256(content).hexdigest(), "content": base64.b64encode(content).decode()}
            for name in deploy.ACTION_SERVER_MODULES}


def execute_locally(commands):
    for command in commands:
        argv = shlex.split(command)
        assert argv[:4] == ["python3", "-I", "-S", "-c"] and len(argv) == 5
        exec(compile(argv[4], "retained-deployment-fixture", "exec"), {})


def fixture_root_authority(monkeypatch, root):
    """Only fake root metadata for the test-owned root and its ancestors.

    Publication, original retention, checksum validation and source compilation
    execute unchanged against actual isolated local files. Never guest paths.
    """
    original = Path.lstat
    ancestors = {root, *root.parents}
    def metadata(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if path in ancestors or path.is_relative_to(root):
            values = list(result)
            values[0] &= ~0o022
            values[4] = 0
            return os.stat_result(values)
        return result
    monkeypatch.setattr(Path, "lstat", metadata)
    monkeypatch.setattr(os, "geteuid", lambda: 0)


def test_fixed_source_bundle_excludes_runtime_data_and_fits_ecd_command_budget():
    files = deploy.action_server_bundle()
    assert set(files) == set(deploy.ACTION_SERVER_MODULES)
    # The removed private actor and private browser modules are no longer published.
    assert "private_actor.py" not in files and not {name for name in files if name.startswith("browser_")}
    # A deployed guest runs exactly the modules the sandbox image copies.
    dockerfile = (ROOT / "container/Dockerfile").read_text()
    assert set(re.findall(r"^COPY (\S+\.py) /opt/action_server/", dockerfile, re.M)) == set(files)
    commands = deploy.retained_deployment_commands(files, "fixture_release_01")
    assert all(len(base64.b64encode(command.encode())) <= 16 * 1024 for command in commands)
    combined = "\n".join(commands)
    assert "rm -" not in combined and "apt-get" not in combined and "chown" not in combined
    assert "/workspace" not in combined and "/data/" not in combined and "systemctl" not in combined


def test_actual_local_publication_retains_originals_chunks_and_replays_exactly(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "service"
    root.mkdir()
    original = b"# original fixture source\n"
    (root / "action_server.py").write_bytes(original)
    fixture_root_authority(monkeypatch, root)
    commands = deploy.retained_deployment_commands(small_bundle(), "fixture_release_02", remote_root=str(root))
    execute_locally(commands)
    stage = root / "releases/fixture_release_02"
    assert (stage / "original/action_server.py").read_bytes() == original
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    execute_locally(commands)
    assert {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()} == before
    assert list(stage.glob("chunk_*")) and (stage / "manifest.json").is_file()


def test_invalid_remote_bundle_is_rejected_before_any_original_is_replaced(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "service"
    root.mkdir()
    original = b"# original fixture source\n"
    (root / "action_server.py").write_bytes(original)
    fixture_root_authority(monkeypatch, root)
    files = small_bundle()
    data = b"def invalid(\n"
    files["file_worker.py"] = {"sha256": hashlib.sha256(data).hexdigest(), "content": base64.b64encode(data).decode()}
    with pytest.raises(SyntaxError):
        execute_locally(deploy.retained_deployment_commands(files, "fixture_release_03", remote_root=str(root)))
    assert (root / "action_server.py").read_bytes() == original
    assert not (root / "file_worker.py").exists()


def test_nonroot_publication_refuses_before_creating_staging(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "service"
    root.mkdir()
    monkeypatch.setattr(os, "geteuid", lambda: 12345)
    with pytest.raises(RuntimeError, match="existing root service"):
        execute_locally(deploy.retained_deployment_commands(small_bundle(), "fixture_release_04", remote_root=str(root)))
    assert list(root.iterdir()) == []


def test_action_only_deploy_never_calls_legacy_put_or_installs_media(monkeypatch):
    commands = []
    class LocalBoundary:
        def run(self, command, **kwargs):
            commands.append(command)
            return "fixture accepted"
        def put(self, *args, **kwargs):
            raise AssertionError("Legacy upload can remove a staging file")
    monkeypatch.setattr(deploy, "action_server_bundle", lambda source_dir=None: small_bundle())
    deploy.deploy_action_server_only(LocalBoundary(), release_id="fixture_release_05", no_restart=True)
    assert commands and not any("systemctl" in command or "apt-get" in command for command in commands)


def test_dry_run_resolves_explicit_dev_configuration_without_constructing_a_provider(tmp_path, monkeypatch, capsys):
    env = tmp_path / "dev.env"
    env.write_text("WUYING_DESKTOP_ID=ecd-fixed-fixture\nWUYING_REGION_ID=cn-fixture\n")
    monkeypatch.setattr(sys, "argv", ["deploy", "--action-server-only", "--dry-run", "--env-file", str(env)])
    monkeypatch.setattr(deploy, "Desktop", lambda *args: pytest.fail("Dry-run must not construct the cloud boundary"))
    assert deploy.main() == 0
    import json
    result = json.loads(capsys.readouterr().out)
    assert result["desktop_id"] == "ecd-fixed-fixture" and result["data_paths_modified"] == []
