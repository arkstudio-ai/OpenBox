"""Deployment assets of the trajectory worker topology: compose overlay, scripts, systemd units, k8s manifests."""
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from configparser import ConfigParser
from pathlib import Path

import pytest
import yaml

from trajectory.ops import cms, lifecycle

REPO = Path(__file__).resolve().parents[3]
DEPLOY = REPO / "deploy" / "gw2"
SCRIPTS_DIR = DEPLOY / "scripts"
UNITS_DIR = DEPLOY / "systemd"
SCRIPTS = sorted(SCRIPTS_DIR.glob("*.sh"))
EXECUTABLES = [path for path in SCRIPTS if path.name != "lib.sh"]
BASH = shutil.which("bash")
DOCKER = shutil.which("docker")
needs_bash = pytest.mark.skipif(BASH is None, reason="bash is not installed")
needs_python3 = pytest.mark.skipif(shutil.which("python3") is None, reason="python3 is not on PATH")
IMAGE_PIN = "openbox-backend:20260915-trajectory-example"

# A fake command: logs its arguments and lets responder.respond(name, args) answer.
STUB = """#!{python}
import json, os, sys
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps([os.path.basename(sys.argv[0]), *sys.argv[1:]]) + "\\n")
sys.path.insert(0, os.environ["STUB_DIR"])
import responder
raise SystemExit(responder.respond(os.path.basename(sys.argv[0]), sys.argv[1:]))
"""
REFUSE = """
import sys
def respond(name, args):
    print(f"unexpected {name} call", file=sys.stderr)
    return 99
"""


class Sandbox:
    """Runs a script with fake docker/aliyun commands first on PATH and a scratch OPENBOX_DIR."""

    def __init__(self, root: Path):
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir()
        self.log = root / "calls.jsonl"
        self.log.write_text("")
        (root / "openbox").mkdir()
        self.env = {
            "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(root),
            "TMPDIR": str(root),
            "STUB_LOG": str(self.log),
            "STUB_DIR": str(self.bin),
            "OPENBOX_DIR": str(root / "openbox"),
            "OPENBOX_LOCK_DIR": str(root),
        }
        self.stub(REFUSE, "docker", "aliyun")

    def stub(self, responder: str, *names: str) -> None:
        (self.bin / "responder.py").write_text(textwrap.dedent(responder))
        for name in names:
            path = self.bin / name
            path.write_text(STUB.format(python=sys.executable))
            path.chmod(0o755)

    def run(self, script: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [BASH, str(script), *args], env={**self.env, **env}, cwd=self.root, capture_output=True, text=True,
            timeout=120,
        )

    def calls(self) -> list[list[str]]:
        return [json.loads(line) for line in self.log.read_text().splitlines() if line]

    def forget(self) -> None:
        self.log.write_text("")


@pytest.fixture
def sandbox(tmp_path):
    return Sandbox(tmp_path)


def test_every_script_of_the_spec_is_shipped():
    assert {path.name for path in EXECUTABLES} == {
        "create-trace-db.sh", "prune-images.sh", "pg-backup.sh", "push-metrics.sh", "install-timers.sh",
        "apply-oss-lifecycle.sh", "setup-alarms.sh", "drill-worker-stop.sh", "drill-blob-outage.sh",
        "drill-spool-full.sh", "rebuild-trace-db.sh",
    }


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_scripts_are_strict_bash(script):
    text = script.read_text()
    assert text.startswith("#!/usr/bin/env bash\n")
    assert "\nset -euo pipefail\n" in text
    if script.name != "lib.sh":
        assert os.access(script, os.X_OK)
        assert '. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"' in text


@needs_bash
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda path: path.name)
def test_scripts_parse(script):
    subprocess.run([BASH, "-n", str(script)], check=True)


@needs_bash
@pytest.mark.parametrize("script", EXECUTABLES, ids=lambda path: path.name)
def test_help_touches_neither_docker_nor_the_cloud(sandbox, script):
    result = sandbox.run(script, "--help")
    assert result.returncode == 0, result.stderr
    assert script.name in result.stdout
    assert sandbox.calls() == []


@needs_bash
@pytest.mark.parametrize(
    ("script", "args"),
    [
        ("prune-images.sh", ["--keep", "0"]),
        ("prune-images.sh", ["--keep"]),
        ("create-trace-db.sh", ["--database", "trace; DROP DATABASE openbox"]),
        ("pg-backup.sh", ["--database", "Openbox"]),
        ("pg-backup.sh", ["--bogus"]),
        ("push-metrics.sh", ["--instance", "gw2 prod"]),
        ("setup-alarms.sh", ["--group-id", "abc"]),
        ("apply-oss-lifecycle.sh", ["--region", "cn-shanghai"]),
        ("drill-blob-outage.sh", ["--fault", "drop everything"]),
        ("drill-spool-full.sh", ["--minutes", "-1"]),
        ("rebuild-trace-db.sh", ["--only", "session_1"]),
    ],
)
def test_invalid_arguments_stop_before_any_action(sandbox, script, args):
    result = sandbox.run(SCRIPTS_DIR / script, *args)
    assert result.returncode == 1
    assert "ERROR" in result.stderr
    assert sandbox.calls() == []


@needs_bash
@pytest.mark.parametrize("script", ["drill-worker-stop.sh", "drill-blob-outage.sh", "drill-spool-full.sh", "rebuild-trace-db.sh"])
def test_drills_only_describe_themselves_without_execute(sandbox, script):
    result = sandbox.run(SCRIPTS_DIR / script)
    assert result.returncode == 0, result.stderr
    assert "dry run" in result.stderr
    assert sandbox.calls() == []


PRUNE_RESPONDER = """
import json, os, sys
from pathlib import Path

DATA = json.loads((Path(os.environ["STUB_DIR"]) / "images.json").read_text())


def respond(name, args):
    images = DATA["images"]
    if args == ["ps", "-aq"]:
        print("\\n".join(DATA["containers"]))
        return 0
    if args[:3] == ["inspect", "--format", "{{.Image}}"]:
        for container in args[3:]:
            print(DATA["containers"][container])
        return 0
    if args == ["compose", "config", "--images"]:
        print("\\n".join(DATA["compose_images"]))
        return 0
    if args[:4] == ["image", "inspect", "--format", "{{.Id}}"]:
        for repository, tag, image, _ in images:
            if f"{repository}:{tag}" == args[4]:
                print(image)
                return 0
        return 1
    if args[:4] == ["image", "inspect", "--format", "{{.Created}}"]:
        for _, _, image, created in images:
            if image == args[4]:
                print(created)
                return 0
        return 1
    if args[:3] == ["image", "ls", "--no-trunc"]:
        for repository, tag, image, _ in images:
            print(repository, tag, image)
        print("<none> <none> sha256:" + "f" * 64)
        return 0
    if args[:2] == ["image", "ls"] and "dangling=true" in args:
        print("sha256:" + "f" * 64)
        return 0
    if args[:2] in (["image", "rm"], ["image", "prune"]):
        return 0
    print("unexpected docker call: " + " ".join(args), file=sys.stderr)
    return 99
"""


def image(char: str) -> str:
    return "sha256:" + char * 64


@needs_bash
def test_prune_images_keeps_used_pinned_and_newest_tags(sandbox):
    sandbox.stub(PRUNE_RESPONDER, "docker")
    (sandbox.bin / "images.json").write_text(json.dumps({
        "images": [
            ["openbox-backend", "t0", image("0"), "2026-08-25T08:00:00.5Z"],
            ["openbox-backend", "t1", image("1"), "2026-09-01T08:00:00.123456789Z"],
            ["openbox-backend", "t2", image("2"), "2026-09-05T08:00:00Z"],
            ["openbox-backend", "t3", image("3"), "2026-09-10T08:00:00.1Z"],
            ["openbox-backend", "t4", image("4"), "2026-09-12T08:00:00.1Z"],
            ["openbox-backend", "t5", image("5"), "2026-09-14T08:00:00.1Z"],
            ["openbox-frontend-v2", "f1", image("a"), "2026-09-01T00:00:00Z"],
            ["openbox-frontend-v2", "f2", image("b"), "2026-09-02T00:00:00Z"],
            ["openbox-frontend-v2", "f3", image("c"), "2026-09-03T00:00:00Z"],
            ["openbox-frontend-v2", "f4", image("d"), "2026-09-04T00:00:00Z"],
            ["postgres", "16-alpine", image("e"), "2026-06-01T00:00:00Z"],
        ],
        # c1 is a stopped container of an old backend image; t1 is only pinned in the compose files.
        "containers": {"c1": image("2"), "c2": image("5"), "c3": image("e")},
        "compose_images": ["openbox-backend:t1", "postgres:16-alpine", "redis:7-alpine"],
    }))
    script = SCRIPTS_DIR / "prune-images.sh"

    dry = sandbox.run(script)
    assert dry.returncode == 0, dry.stderr
    assert re.findall(r"would remove (\S+)", dry.stderr) == ["openbox-backend:t0", "openbox-frontend-v2:f1"]
    assert "keep openbox-backend:t1" in dry.stderr and "keep openbox-backend:t2" in dry.stderr
    assert not [call for call in sandbox.calls() if call[1:3] in (["image", "rm"], ["image", "prune"])]

    sandbox.forget()
    executed = sandbox.run(script, "--execute")
    assert executed.returncode == 0, executed.stderr
    assert [call[1:] for call in sandbox.calls() if call[1:3] in (["image", "rm"], ["image", "prune"])] == [
        ["image", "rm", "openbox-backend:t0"], ["image", "rm", "openbox-frontend-v2:f1"], ["image", "prune", "-f"],
    ]

    sandbox.forget()
    assert sandbox.run(script, "--keep", "6", "--execute").returncode == 0
    assert not [call for call in sandbox.calls() if call[1:3] == ["image", "rm"]]


LIFECYCLE_RESPONDER = """
import os, sys
from pathlib import Path

STATE = Path(os.environ["STUB_DIR"]) / "bucket.xml"


def respond(name, args):
    if args[:3] == ["ossutil", "api", "get-bucket-lifecycle"]:
        if os.environ.get("STUB_MODE") == "denied":
            print("Error: oss: service returned error: StatusCode=403, ErrorCode=AccessDenied", file=sys.stderr)
            return 1
        if not STATE.exists():
            print('Error: oss: service returned error: StatusCode=404, ErrorCode=NoSuchLifecycle', file=sys.stderr)
            return 1
        print(STATE.read_text())
        return 0
    if args[:3] == ["ossutil", "api", "put-bucket-lifecycle"]:
        source = args[args.index("--lifecycle-configuration") + 1]
        STATE.write_text(Path(source.removeprefix("file://")).read_text())
        return 0
    print("unexpected aliyun call: " + " ".join(args), file=sys.stderr)
    return 99
"""


@needs_bash
@needs_python3
def test_apply_oss_lifecycle_dry_run_apply_and_idempotence(sandbox):
    sandbox.stub(LIFECYCLE_RESPONDER, "aliyun")
    script = SCRIPTS_DIR / "apply-oss-lifecycle.sh"
    state = sandbox.bin / "bucket.xml"

    dry = sandbox.run(script, "--bucket", "openbox-assets")
    assert dry.returncode == 0, dry.stderr
    assert dry.stdout.count("<ID>openbox-") == 4
    assert [call[3] for call in sandbox.calls()] == ["get-bucket-lifecycle"]
    assert not state.exists()

    sandbox.forget()
    backups = sandbox.root / "lifecycle-backups"
    applied = sandbox.run(script, "--bucket", "openbox-assets", "--backup-dir", str(backups), "--execute")
    assert applied.returncode == 0, applied.stderr
    calls = sandbox.calls()
    assert [call[3] for call in calls] == ["get-bucket-lifecycle", "put-bucket-lifecycle", "get-bucket-lifecycle"]
    put = calls[1]
    assert (put[put.index("--bucket") + 1], put[put.index("--region") + 1]) == ("openbox-assets", "cn-shanghai")
    assert "--allow-same-action-overlap" not in put
    assert [path.read_text() for path in backups.iterdir()] == [""]
    assert lifecycle.verify((DEPLOY / "oss-lifecycle.xml").read_text(), state.read_text()) == []

    sandbox.forget()
    again = sandbox.run(script, "--bucket", "openbox-assets", "--backup-dir", str(backups), "--execute")
    assert again.returncode == 0, again.stderr
    assert "nothing to apply" in again.stderr
    assert [call[3] for call in sandbox.calls()] == ["get-bucket-lifecycle"]


@needs_bash
@needs_python3
def test_apply_oss_lifecycle_keeps_foreign_rules_and_refuses_conflicts(sandbox):
    sandbox.stub(LIFECYCLE_RESPONDER, "aliyun")
    script = SCRIPTS_DIR / "apply-oss-lifecycle.sh"
    state = sandbox.bin / "bucket.xml"
    state.write_text(
        "<LifecycleConfiguration><Rule><ID>assets-ia</ID><Prefix>assets/</Prefix><Status>Enabled</Status>"
        "<Transition><Days>60</Days><StorageClass>IA</StorageClass></Transition></Rule></LifecycleConfiguration>"
    )
    applied = sandbox.run(script, "--bucket", "openbox-assets", "--backup-dir", str(sandbox.root / "b"), "--execute")
    assert applied.returncode == 0, applied.stderr
    assert [lifecycle.rule_id(rule) for rule in lifecycle.parse(state.read_text())] == [
        "assets-ia", "openbox-trajectories-ia-30d", "openbox-trajectory-exports-expire-30d",
        "openbox-trajectories-abort-multipart-7d", "openbox-postgres-backups-expire-30d",
    ]

    state.write_text(
        "<LifecycleConfiguration><Rule><ID>abort-all</ID><Prefix></Prefix><Status>Enabled</Status>"
        "<AbortMultipartUpload><Days>1</Days></AbortMultipartUpload></Rule></LifecycleConfiguration>"
    )
    sandbox.forget()
    refused = sandbox.run(script, "--bucket", "openbox-assets", "--execute")
    assert refused.returncode == 1 and "abort multipart" in refused.stderr
    assert [call[3] for call in sandbox.calls()] == ["get-bucket-lifecycle"]

    sandbox.forget()
    denied = sandbox.run(script, "--bucket", "openbox-assets", "--execute", STUB_MODE="denied")
    assert denied.returncode == 1 and "AccessDenied" in denied.stderr
    assert [call[3] for call in sandbox.calls()] == ["get-bucket-lifecycle"]


ALIYUN_OK = """
def respond(name, args):
    print('{"Code":"200","Success":true}')
    return 0
"""


def option(call: list[str], name: str) -> str:
    return call[call.index(name) + 1]


@needs_bash
def test_setup_alarms_creates_the_spec_rules(sandbox):
    sandbox.stub(ALIYUN_OK, "aliyun")
    script = SCRIPTS_DIR / "setup-alarms.sh"

    dry = sandbox.run(script)
    assert dry.returncode == 0, dry.stderr
    assert dry.stderr.count("dry-run: aliyun cms PutCustomMetricRule") == 9
    assert sandbox.calls() == []

    applied = sandbox.run(script, "--execute", "--webhook", "https://hooks.example.invalid/cms")
    assert applied.returncode == 0, applied.stderr
    calls = sandbox.calls()
    assert len(calls) == 9 and all(call[1:3] == ["cms", "PutCustomMetricRule"] for call in calls)
    rules = {option(call, "--RuleId"): call for call in calls}
    worker = rules["openbox-gw2-worker-down"]
    assert option(worker, "--MetricName") == "worker_up"
    assert option(worker, "--Resources") == '[{"groupId":0,"dimension":"instance=gw2"}]'
    assert option(worker, "--ContactGroups") == "云账号报警联系人"
    assert (option(worker, "--region"), option(worker, "--Webhook")) == ("cn-shanghai", "https://hooks.example.invalid/cms")
    assert {option(call, "--MetricName"): (option(call, "--ComparisonOperator"), option(call, "--Threshold")) for call in calls} == {
        "host_disk_used_percent": (">=", "80"),
        "spool_bytes": (">=", str(1024**3)),
        "spool_oldest_age_seconds": (">=", "60"),
        "worker_up": ("<", "1"),
        "gaps_recorded_1h": (">", "0"),
        "projection_lag_events": (">=", "5000"),
        "blob_put_failures_5m": (">=", "10"),
        "trace_db_bytes": (">=", str(20 * 1024**3)),
        "oom_kills_1h": (">", "0"),
    }


def test_alarm_rules_only_use_metrics_that_the_push_reports():
    host = {name: 1 for name in cms.HOST_METRICS}
    health = {"status": "ok", "writer": True, "db": True, "spool": True, "blob_store": True}
    worker = {"counters": {name: 1 for name in cms.COUNTERS}, "gauges": {name: 1 for name in cms.GAUGES}}
    _, state = cms.collect(host, health, worker, {}, now=1757836800.0)
    values, _ = cms.collect(host, health, worker, state, now=1757836860.0)
    metrics = re.findall(r"^[a-z-]+\|([a-z0-9_]+)\|", (SCRIPTS_DIR / "setup-alarms.sh").read_text(), flags=re.MULTILINE)
    assert len(metrics) == 9 and set(metrics) <= set(values)


@needs_bash
def test_install_timers_dry_run_lists_the_units(sandbox):
    result = sandbox.run(SCRIPTS_DIR / "install-timers.sh", "--dry-run")
    assert result.returncode == 0, result.stderr
    installs = re.findall(r"dry-run: install -m 0644 \S+/systemd/(\S+) ", result.stderr)
    assert sorted(installs) == sorted(path.name for path in UNITS_DIR.iterdir())
    assert "systemctl enable --now openbox-trajectory-metrics.timer openbox-pg-backup.timer openbox-prune-images.timer" in result.stderr
    assert sandbox.calls() == []


def compose_available() -> bool:
    return DOCKER is not None and subprocess.run([DOCKER, "compose", "version"], capture_output=True).returncode == 0


@pytest.mark.skipif(not compose_available(), reason="docker compose is not installed")
def test_overlay_validates_against_the_sanitized_production_compose(tmp_path):
    project = tmp_path / "openbox"
    (project / "deploy" / "gw2").mkdir(parents=True)
    (project / "config").mkdir()
    (project / "secrets").mkdir()
    shutil.copy(DEPLOY / "docker-compose.base.example.yml", project / "docker-compose.yml")
    shutil.copy(DEPLOY / "docker-compose.override.example.yml", project / "docker-compose.override.yml")
    shutil.copy(DEPLOY / "docker-compose.trajectory.yml", project / "deploy" / "gw2" / "docker-compose.trajectory.yml")
    dotenv = (DEPLOY / "env.example").read_text()
    assert "\nCOMPOSE_FILE=docker-compose.yml:deploy/gw2/docker-compose.trajectory.yml:docker-compose.override.yml\n" in dotenv
    # A stale tag in .env proves that the override's pins win over the overlay's default image.
    (project / ".env").write_text(dotenv.replace("OPENBOX_IMAGE_TAG=20260915-trajectory-example", "OPENBOX_IMAGE_TAG=stale"))
    (project / "config" / "backend.env").write_text("JWT_SECRET=placeholder\nINTERNAL_API_TOKEN=placeholder\n")
    (project / "config" / "openbox.json").write_text("{}")
    (project / "secrets" / "aliyun-config.json").write_text("{}")
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("COMPOSE_", "OPENBOX_"))}

    result = subprocess.run(
        [DOCKER, "compose", "config", "--format", "json"], cwd=project, env=environment, capture_output=True,
        text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert "backend-worker" not in services

    worker = services["trajectory-worker"]
    assert worker["image"] == services["backend"]["image"] == IMAGE_PIN
    assert worker["command"] == [
        "/bin/sh", "-ec", "alembic -c alembic_trajectory.ini upgrade head && exec python -m trajectory.worker",
    ]
    assert worker["environment"]["TRAJECTORY_DATABASE_URL"] == "postgresql+asyncpg://openbox:change-me@postgres:5432/openbox_trace"
    assert {key: worker["environment"][key] for key in (
        "REDIS_URL", "TRAJECTORY_WORKER_MODE", "TRAJECTORY_BLOB_PROVIDER", "TRAJECTORY_BACKEND_INTERNAL_URL",
        "ALIYUN_CLI_CONFIG", "INTERNAL_API_TOKEN",
    )} == {
        "REDIS_URL": "redis://redis:6379/0", "TRAJECTORY_WORKER_MODE": "external", "TRAJECTORY_BLOB_PROVIDER": "oss",
        "TRAJECTORY_BACKEND_INTERNAL_URL": "http://backend:8080", "ALIYUN_CLI_CONFIG": "/run/secrets/aliyun-config.json",
        "INTERNAL_API_TOKEN": "placeholder",
    }
    mounts = {volume["target"]: volume for volume in worker["volumes"]}
    assert mounts["/var/lib/openbox/trajectory-spool"]["source"] == "trajectory-spool"
    assert mounts["/run/secrets/aliyun-config.json"]["read_only"] is True
    assert (mounts["/legacy-blobs"]["source"], mounts["/legacy-blobs"]["read_only"]) == ("blob-data", True)
    assert worker["healthcheck"]["test"] == ["CMD", "curl", "-fsS", "http://127.0.0.1:8090/health"]
    assert float(worker["cpus"]) == 1.0 and int(worker["mem_limit"]) == 1024**3
    assert worker["restart"] == "unless-stopped" and worker["logging"]["options"]["max-size"] == "50m"

    backend = services["backend"]
    assert {key: backend["environment"][key] for key in ("TRAJECTORY_SINK", "TRAJECTORY_WORKER_MODE", "TRAJECTORY_SPOOL_DIR")} == {
        "TRAJECTORY_SINK": "spool", "TRAJECTORY_WORKER_MODE": "external",
        "TRAJECTORY_SPOOL_DIR": "/var/lib/openbox/trajectory-spool",
    }
    assert backend["environment"]["DATABASE_URL"].endswith("@postgres:5432/openbox")
    spool_users = sorted(
        name for name, service in services.items()
        if any(volume.get("source") == "trajectory-spool" for volume in service.get("volumes", []))
    )
    assert spool_users == ["backend", "trajectory-worker"]
    assert services["frontend"]["environment"]["TRAJECTORY_HOST"] == "trajectory-worker:8090"
    assert services["frontend"]["image"] == "openbox-frontend-v2:20260915-trajectory-example"

    postgres = services["postgres"]
    assert int(postgres["mem_limit"]) == 2 * 1024**3
    assert postgres["command"] == [
        "postgres", "-c", "shared_buffers=512MB", "-c", "effective_cache_size=1GB",
        "-c", "shared_preload_libraries=pg_stat_statements", "-c", "pg_stat_statements.track=all",
        "-c", "max_connections=200",
    ]


def unit(name: str) -> ConfigParser:
    parser = ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read_string((UNITS_DIR / name).read_text())
    return parser


def test_timers_follow_the_spec_schedules():
    schedules = {
        "openbox-trajectory-metrics.timer": "*-*-* *:*:00",
        "openbox-pg-backup.timer": "*-*-* 03:30:00 Asia/Shanghai",
        "openbox-prune-images.timer": "Sun *-*-* 04:30:00 Asia/Shanghai",
    }
    assert {path.name for path in UNITS_DIR.glob("*.timer")} == set(schedules)
    for name, calendar in schedules.items():
        timer = unit(name)
        assert timer["Timer"]["OnCalendar"] == calendar
        assert timer["Timer"]["Unit"] == name.replace(".timer", ".service")
        assert timer["Install"]["WantedBy"] == "timers.target"


def test_services_run_the_deployed_scripts():
    services = sorted(UNITS_DIR.glob("*.service"))
    assert len(services) == 3
    for path in services:
        service = unit(path.name)
        assert service["Service"]["Type"] == "oneshot"
        assert "docker.service" in service["Unit"]["After"]
        executable = Path(service["Service"]["ExecStart"].split()[0])
        assert executable.parent == Path("/opt/openbox/deploy/gw2/scripts")
        assert (SCRIPTS_DIR / executable.name) in EXECUTABLES
    assert unit("openbox-prune-images.service")["Service"]["ExecStart"].endswith("prune-images.sh --execute")


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze is not installed")
def test_units_pass_systemd_analyze_verify(tmp_path):
    for path in UNITS_DIR.iterdir():
        (tmp_path / path.name).write_text(path.read_text().replace("/opt/openbox/deploy/gw2", str(DEPLOY)))
    subprocess.run(["systemd-analyze", "verify", *map(str, sorted(tmp_path.iterdir()))], check=True)


def container_environment(container: dict, documents: list[dict]) -> dict:
    values = {}
    for source in container.get("envFrom", []):
        if "configMapRef" in source:
            config = next(doc for doc in documents if doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == source["configMapRef"]["name"])
            values.update(config["data"])
    for item in container.get("env", []):
        values[item["name"]] = item.get("value", item.get("valueFrom"))
    return values


@pytest.mark.parametrize("manifest", ["base.yaml", "aks.yaml"])
def test_k8s_runs_the_worker_as_a_sidecar_with_routes(manifest):
    text = (REPO / "k8s" / manifest).read_text()
    assert "untested" in text.splitlines()[1]
    documents = [doc for doc in yaml.safe_load_all(text) if doc]
    assert "openbox-backend-worker" not in {doc["metadata"]["name"] for doc in documents}
    deployment = next(doc for doc in documents if doc["kind"] == "Deployment" and doc["metadata"]["name"] == "openbox-backend")
    assert deployment["spec"]["replicas"] == 1
    pod = deployment["spec"]["template"]["spec"]
    containers = {container["name"]: container for container in pod["containers"]}
    backend, worker = containers["backend"], containers["trajectory-worker"]
    assert {"name": "trajectory-spool", "emptyDir": {}} in pod["volumes"]
    for container in (backend, worker):
        assert {"name": "trajectory-spool", "mountPath": "/var/lib/openbox/trajectory-spool"} in container["volumeMounts"]
    assert worker["image"] == backend["image"]
    assert worker["command"][-1] == "alembic -c alembic_trajectory.ini upgrade head && exec python -m trajectory.worker"
    assert worker["readinessProbe"]["httpGet"] == {"path": "/health", "port": 8090}

    backend_env = container_environment(backend, documents)
    assert (backend_env["TRAJECTORY_SINK"], backend_env["TRAJECTORY_WORKER_MODE"]) == ("spool", "external")
    worker_env = container_environment(worker, documents)
    assert worker_env["TRAJECTORY_WORKER_MODE"] == "external"
    assert worker_env["TRAJECTORY_BACKEND_INTERNAL_URL"] == "http://127.0.0.1:8080"
    assert backend_env["TRAJECTORY_SPOOL_DIR"] == worker_env["TRAJECTORY_SPOOL_DIR"] == "/var/lib/openbox/trajectory-spool"

    service = next(doc for doc in documents if doc["kind"] == "Service" and doc["metadata"]["name"] == "openbox-trajectory-worker")
    assert service["spec"]["selector"] == {"app": "openbox-backend"}
    assert service["spec"]["ports"][0]["targetPort"] == 8090
    ingress = next(doc for doc in documents if doc["kind"] == "Ingress")
    routes = {
        path["path"]: path["backend"]["service"] for rule in ingress["spec"]["rules"] for path in rule["http"]["paths"]
    }
    for path in ("/api/admin/trajectories", "/ws/admin/trajectories"):
        assert routes[path] == {"name": "openbox-trajectory-worker", "port": {"number": 8090}}
    assert routes["/api"]["name"] == "openbox-backend"
