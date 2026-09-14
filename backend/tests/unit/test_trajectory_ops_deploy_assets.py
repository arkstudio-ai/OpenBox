"""Deployment assets of the trajectory worker topology: compose overlay, scripts, systemd units, k8s manifests."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from configparser import ConfigParser
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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
TRACE_PASSWORD = "0123456789abcdef0123456789abcdef"
BACKUP_KEY = "backups/postgres/20260915/openbox-20260914T193000Z.dump"

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
            stdin=subprocess.DEVNULL, timeout=120,
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
        "drill-spool-full.sh", "rebuild-trace-db.sh", "analytics-export.sh", "drill-delete-session.sh",
        "restore-check.sh",
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
        ("create-trace-db.sh", []),
        ("pg-backup.sh", ["--database", "Openbox"]),
        ("pg-backup.sh", ["--bogus"]),
        ("push-metrics.sh", ["--instance", "gw2 prod"]),
        ("install-timers.sh", ["--instance", "gw2 prod"]),
        ("setup-alarms.sh", ["--group-id", "abc"]),
        ("apply-oss-lifecycle.sh", ["--region", "cn-shanghai"]),
        ("drill-blob-outage.sh", ["--fault", "drop everything"]),
        ("drill-spool-full.sh", ["--minutes", "-1"]),
        ("rebuild-trace-db.sh", ["--only", "session_1"]),
        ("analytics-export.sh", ["--date", "yesterday"]),
        ("analytics-export.sh", ["--wait", "soon"]),
        ("drill-delete-session.sh", ["--session-id", "session 1"]),
        ("drill-delete-session.sh", ["--session-id", "session_1", "--user-token-file", "/missing", "--admin-token-file", "/missing"]),
        ("drill-delete-session.sh", ["--session-id", "session_1", "--api-url", "http://127.0.0.1:8080/api"]),
        ("restore-check.sh", []),
        ("restore-check.sh", ["--key", "assets/user/file.dump"]),
        ("restore-check.sh", ["--key", "backups/../assets/x.dump"]),
        ("restore-check.sh", ["--file", "/missing.dump"]),
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


@needs_bash
def test_drill_defaults_follow_the_plan(sandbox):
    worker_stop = sandbox.run(SCRIPTS_DIR / "drill-worker-stop.sh")
    assert "stop trajectory-worker for 15 min" in worker_stop.stderr
    assert "drain within 300 s of the restart" in worker_stop.stderr
    assert "for 30 min" in sandbox.run(SCRIPTS_DIR / "drill-blob-outage.sh").stderr


@needs_bash
def test_deletion_drill_and_restore_check_only_describe_themselves_without_execute(sandbox):
    token = sandbox.root / "token"
    token.write_text("header.payload.signature\n")
    dump = sandbox.root / "openbox.dump"
    dump.write_bytes(b"PGDMP")
    runs = [
        ("drill-delete-session.sh", ["--session-id", "session_1", "--user-token-file", str(token),
                                     "--admin-token-file", str(token)]),
        ("restore-check.sh", ["--file", str(dump)]),
        ("restore-check.sh", ["--key", BACKUP_KEY]),
    ]
    for script, args in runs:
        result = sandbox.run(SCRIPTS_DIR / script, *args)
        assert result.returncode == 0, result.stderr
        assert "dry run" in result.stderr
    assert sandbox.calls() == []


@needs_bash
def test_drills_compare_worker_metrics_as_numbers(sandbox):
    checks = (
        '. "$1"; number_greater 3.0 2 && number_greater 1 0 && ! number_greater 2 2.0 && ! number_greater 0 0 '
        '&& ! number_greater "" 0'
    )
    result = subprocess.run(
        [BASH, "-c", checks, "checks", str(SCRIPTS_DIR / "lib.sh")], env=sandbox.env, capture_output=True, text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr


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
        if DATA.get("compose_fails"):
            print("service trajectory-worker refers to undefined volume blob-data", file=sys.stderr)
            return 1
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


@needs_bash
def test_prune_images_removes_nothing_when_the_compose_files_cannot_be_read(sandbox):
    sandbox.stub(PRUNE_RESPONDER, "docker")
    (sandbox.bin / "images.json").write_text(json.dumps({
        "images": [["openbox-backend", f"t{day}", image(str(day)), f"2026-09-0{day + 1}T00:00:00Z"] for day in range(5)],
        "containers": {},
        "compose_images": [],
        "compose_fails": True,
    }))
    script = SCRIPTS_DIR / "prune-images.sh"

    executed = sandbox.run(script, "--execute")
    assert executed.returncode == 1 and "nothing was removed" in executed.stderr
    assert not [call for call in sandbox.calls() if call[1:3] in (["image", "rm"], ["image", "prune"])]

    dry = sandbox.run(script)
    assert dry.returncode == 0, dry.stderr
    assert "not protected in this listing" in dry.stderr
    assert re.findall(r"would remove (\S+)", dry.stderr) == ["openbox-backend:t1", "openbox-backend:t0"]


# Fake gw2 host: docker compose (postgres, worker), docker inspect/info/stats, sha256sum, journalctl and curl.
# Answers come from server.json; what the scripts changed or sent is appended to server.jsonl.
SERVER_RESPONDER = """
import hashlib, json, os, sys
from pathlib import Path

STUB = Path(os.environ["STUB_DIR"])
STATE = json.loads((STUB / "server.json").read_text())


def note(kind, **fields):
    with (STUB / "server.jsonl").open("a") as log:
        log.write(json.dumps({"kind": kind, **fields}) + "\\n")


def noted(kind):
    return [entry for entry in map(json.loads, (STUB / "server.jsonl").read_text().splitlines()) if entry["kind"] == kind]


def worker_python(args):
    module, command, options = args[1], args[2], args[3:]
    if module == "trajectory.ops.backup" and command == "upload":
        data = sys.stdin.buffer.read()
        values = dict(zip(options[::2], options[1::2]))
        note("upload", key=values["--key"], size=int(values["--size"]), sha256=values["--sha256"],
             received=len(data), received_sha256=hashlib.sha256(data).hexdigest())
        return STATE.get("upload_status", 0)
    if module == "trajectory.ops.backup" and command == "download":
        note("download", options=options)
        sys.stdout.buffer.write(STATE.get("download", "PGDMP").encode())
        return STATE.get("download_status", 0)
    if module == "trajectory.ops.cms" and command == "push":
        data = sys.stdin.buffer.read()
        note("cms", options=options, stdin=data.decode())
        print(json.dumps({"version": 1, "pushed_at": 1}))
        return STATE.get("cms_status", 0)
    if module == "trajectory.ops.cms" and command == "put":
        note("cms_put", options=options)
        return STATE.get("cms_status", 0)
    if module == "trajectory.ops.deletion":
        note("deletion", command=command, options=options)
        return STATE.get(f"deletion_{command}_status", 0)
    if module == "trajectory.analytics" and command == "export":
        note("analytics", options=options)
        return STATE.get("analytics_status", 0)
    return 99


def psql(args):
    if STATE.get("postgres_down"):
        print("psql: error: connection to server on socket failed", file=sys.stderr)
        return 2
    database = args[args.index("-d") + 1]
    flags = [flag for flag in ("-c", "-tAc") if flag in args]
    if not flags:
        note("sql_stdin", database=database, sql=sys.stdin.read())
        return 0
    sql = args[args.index(flags[0]) + 1]
    if sql.startswith("SELECT 1 FROM pg_database"):
        if sql.split("'")[1] in STATE["databases"]:
            print(1)
    elif sql.startswith("SELECT pg_database_size"):
        print(STATE.get("trace_db_bytes", 0))
    elif sql == "SHOW shared_preload_libraries":
        print(STATE.get("preload", ""))
    elif sql.startswith("SELECT string_agg"):
        print("pg_trgm 1.6")
    elif sql.startswith("SELECT coalesce(array_to_string(rolconfig"):
        print("statement_timeout=5s, work_mem=32MB")
    elif sql.startswith("SELECT pg_get_userbyid(datdba)"):
        print(STATE.get("trace_db_owner", "openbox_trace"))
    elif sql.startswith("SELECT count(*) FROM pg_class") and "relkind IN" in sql:
        print(STATE.get("restored_tables", 0))
    elif sql.startswith("SELECT count(*) FROM pg_class"):
        print(STATE.get("foreign_relations", 0))
    elif sql.startswith("SELECT count(*) FROM pg_extension"):
        print(STATE.get("pg_stat_statements_installed", 1))
    elif sql.startswith("SELECT count(*) FROM pg_stat_statements"):
        note("statements_query", database=database, sql=sql)
        print(STATE.get("business_statements", 0))
    elif sql.startswith("SELECT pg_size_pretty"):
        print("12 MB")
    else:
        note("sql", database=database, sql=sql)
    return 0


def curl(args):
    headers = sys.stdin.read()
    method, url = args[args.index("-X") + 1], args[-1]
    earlier = len([entry for entry in noted("curl") if "/api/admin/" in entry["url"]])
    note("curl", method=method, url=url, headers=headers)
    if "/api/admin/" in url:
        codes = STATE.get("admin_codes", [200, 404])
        code = codes[min(earlier, len(codes) - 1)]
    else:
        code = STATE.get("delete_status", 200)
    Path(args[args.index("-o") + 1]).write_text("{}")
    sys.stdout.write(str(code))
    return 0


def respond(name, args):
    if name == "curl":
        return curl(args)
    if name == "sha256sum":
        print(hashlib.sha256(Path(args[0]).read_bytes()).hexdigest() + "  " + args[0])
        return 0
    if name == "journalctl":
        print("\\n".join(STATE.get("kernel", [])))
        return 0
    if args[:1] == ["info"]:
        print("/")
        return 0
    if args[:2] == ["stats", "--no-stream"]:
        print(STATE.get("stats", "12.50% 40.25%"))
        return 0
    if args[:2] == ["inspect", "-f"]:
        template, target = args[2], args[3]
        running = target.removesuffix("-id") in STATE["running"]
        if ".State.Running" in template:
            print("true" if running else "false")
        elif ".State.Health" in template:
            print("healthy" if running else "exited")
        elif ".Mounts" in template:
            print(STATE.get("spool_dir", ""))
        elif template == "{{.Id}}":
            print(target + "-full")
        return 0
    if args[:1] != ["compose"]:
        return 99
    args = args[1:]
    if args == ["config", "--services"]:
        print("backend\\nfrontend\\npostgres\\nredis\\ntrajectory-worker")
        return 0
    if args[:2] == ["ps", "-q"]:
        print(args[2] + "-id")
        return 0
    if args[:4] == ["exec", "-T", "postgres", "psql"]:
        return psql(args[4:])
    if args[:4] == ["exec", "-T", "postgres", "pg_dump"]:
        if STATE.get("dump_fails"):
            return 1
        note("pg_dump", args=args[4:])
        sys.stdout.buffer.write(b"PGDMP" + args[-1].encode())
        return 0
    if args[:4] == ["exec", "-T", "postgres", "pg_restore"]:
        data = sys.stdin.buffer.read()
        if "--list" in args:
            print("\\n".join(STATE.get("toc", [])))
            return 0
        note("pg_restore", args=args[4:], received=len(data))
        return STATE.get("restore_status", 0)
    if args[:4] == ["exec", "-T", "trajectory-worker", "python"]:
        return worker_python(args[4:])
    if args[:7] == ["run", "--rm", "--no-deps", "-T", "--entrypoint", "python", "trajectory-worker"]:
        note("one-off")
        return worker_python(args[7:])
    print("unexpected docker call: " + " ".join(args), file=sys.stderr)
    return 99
"""


def server(sandbox: Sandbox, **state) -> Path:
    sandbox.stub(SERVER_RESPONDER, "docker", "sha256sum", "journalctl", "curl")
    defaults = {"databases": ["openbox", "openbox_trace"], "running": ["backend", "postgres", "trajectory-worker"]}
    (sandbox.bin / "server.json").write_text(json.dumps({**defaults, **state}))
    record = sandbox.bin / "server.jsonl"
    record.write_text("")
    return record


def notes(record: Path, kind: str) -> list[dict]:
    return [entry for entry in map(json.loads, record.read_text().splitlines()) if entry["kind"] == kind]


@needs_bash
def test_pg_backup_uploads_existing_databases_and_expires_only_its_own_dumps(sandbox):
    record = server(sandbox, databases=["openbox"])
    local = sandbox.root / "dumps"
    local.mkdir()
    ten_days_ago = time.time() - 10 * 86400
    for name in ("preflight.dump", "openbox-20260901T193000Z.dump", "openbox_trace-20260901T193000Z.dump.partial"):
        (local / name).write_bytes(b"old")
        os.utime(local / name, (ten_days_ago, ten_days_ago))

    result = sandbox.run(SCRIPTS_DIR / "pg-backup.sh", "--local-dir", str(local))

    assert result.returncode == 0, result.stderr
    assert "skipping openbox_trace: the database does not exist" in result.stderr
    assert [entry["args"] for entry in notes(record, "pg_dump")] == [["-U", "openbox", "-Fc", "openbox"]]
    (upload,) = notes(record, "upload")
    assert re.fullmatch(r"backups/postgres/\d{8}/openbox-\d{8}T\d{6}Z\.dump", upload["key"])
    dump = b"PGDMPopenbox"
    assert upload["size"] == upload["received"] == len(dump)
    assert upload["sha256"] == upload["received_sha256"] == hashlib.sha256(dump).hexdigest()
    assert notes(record, "one-off") == []
    # The uploaded dump and this script's expired dumps are gone; a foreign dump in the directory stays.
    assert sorted(path.name for path in local.iterdir()) == ["preflight.dump"]


@needs_bash
def test_pg_backup_of_the_legacy_tables_passes_the_pattern_to_pg_dump(sandbox):
    record = server(sandbox)
    result = sandbox.run(
        SCRIPTS_DIR / "pg-backup.sh", "--legacy-trajectory-tables", "--local-dir", str(sandbox.root / "dumps")
    )
    assert result.returncode == 0, result.stderr
    assert [entry["args"] for entry in notes(record, "pg_dump")] == [
        ["-U", "openbox", "-Fc", "--table=public.legacy_trajectory_*", "openbox"]
    ]
    (upload,) = notes(record, "upload")
    assert "/openbox-legacy-trajectory-" in upload["key"]


@needs_bash
@pytest.mark.parametrize(
    ("state", "message"),
    [
        ({"postgres_down": True}, "cannot query PostgreSQL for openbox"),
        ({"databases": []}, "no database was backed up"),
        ({"dump_fails": True}, "pg_dump of openbox failed"),
    ],
)
def test_pg_backup_fails_instead_of_skipping_silently(sandbox, state, message):
    record = server(sandbox, **state)
    result = sandbox.run(SCRIPTS_DIR / "pg-backup.sh", "--local-dir", str(sandbox.root / "dumps"))
    assert result.returncode == 1
    assert message in result.stderr and "backups complete" not in result.stderr
    assert notes(record, "upload") == []


@needs_bash
def test_pg_backup_keeps_the_dump_when_the_upload_in_a_one_off_worker_fails(sandbox):
    record = server(sandbox, databases=["openbox"], running=["backend", "postgres"], upload_status=1)
    local = sandbox.root / "dumps"
    result = sandbox.run(SCRIPTS_DIR / "pg-backup.sh", "--local-dir", str(local))
    assert result.returncode == 1 and "the local copy is kept" in result.stderr
    assert len(notes(record, "one-off")) == 1 and len(notes(record, "upload")) == 1
    (kept,) = local.iterdir()
    assert kept.read_bytes() == b"PGDMPopenbox" and kept.stat().st_mode & 0o777 == 0o600


def write_dotenv(sandbox: Sandbox, text: str) -> None:
    (sandbox.root / "openbox" / ".env").write_text(text)


@needs_bash
def test_create_trace_db_refuses_to_run_without_a_usable_password(sandbox):
    script = SCRIPTS_DIR / "create-trace-db.sh"
    server(sandbox)
    missing = sandbox.run(script)
    assert missing.returncode == 1 and "OPENBOX_TRACE_DB_PASSWORD is not set" in missing.stderr
    write_dotenv(sandbox, "OPENBOX_DB_PASSWORD=x\nOPENBOX_TRACE_DB_PASSWORD='short'\n")
    short = sandbox.run(script, "--dry-run")
    assert short.returncode == 1 and "must be 16 to 128" in short.stderr
    write_dotenv(sandbox, "OPENBOX_TRACE_DB_PASSWORD=has'quote-and-more-characters\n")
    assert sandbox.run(script).returncode == 1
    assert sandbox.calls() == []


@needs_bash
def test_create_trace_db_creates_the_role_over_stdin_and_hands_it_the_database(sandbox):
    script = SCRIPTS_DIR / "create-trace-db.sh"
    write_dotenv(sandbox, f"OPENBOX_IMAGE_TAG=stale\nOPENBOX_DB_PASSWORD=x\nOPENBOX_TRACE_DB_PASSWORD=\"{TRACE_PASSWORD}\"\n")
    record = server(sandbox, databases=["openbox"], preload="pg_stat_statements")

    dry = sandbox.run(script, "--dry-run")
    assert dry.returncode == 0, dry.stderr
    assert 'dry-run (postgres): CREATE DATABASE "openbox_trace" OWNER "openbox_trace"' in dry.stderr
    assert "PASSWORD '***'" in dry.stderr and TRACE_PASSWORD not in dry.stderr
    assert notes(record, "sql") == [] and notes(record, "sql_stdin") == []

    created = sandbox.run(script)
    assert created.returncode == 0, created.stderr
    (role,) = notes(record, "sql_stdin")
    assert role["database"] == "postgres"
    statements = role["sql"].splitlines()
    # Neither the server log nor pg_stat_statements may keep the statement that carries the password.
    assert statements[:3] == [
        "SET log_statement = 'none';", "SET log_min_error_statement = 'panic';", "SET pg_stat_statements.track = 'none';",
    ]
    assert statements.index(f"ALTER ROLE \"openbox_trace\" WITH LOGIN PASSWORD '{TRACE_PASSWORD}';") > 3
    assert "CREATE ROLE \"openbox_trace\" LOGIN;" in role["sql"]
    assert {"ALTER ROLE \"openbox_trace\" SET statement_timeout = '5s';",
            "ALTER ROLE \"openbox_trace\" SET work_mem = '32MB';"} <= set(statements)
    assert [(entry["database"], entry["sql"]) for entry in notes(record, "sql")] == [
        ("postgres", 'CREATE DATABASE "openbox_trace" OWNER "openbox_trace"'),
        ("openbox_trace", "CREATE EXTENSION IF NOT EXISTS pg_trgm"),
        ("openbox", "CREATE EXTENSION IF NOT EXISTS pg_stat_statements"),
    ]
    # The password is in no process's arguments and in no log line.
    assert TRACE_PASSWORD not in sandbox.log.read_text()
    assert TRACE_PASSWORD not in created.stderr

    record = server(sandbox, trace_db_owner="openbox", foreign_relations=2)
    existing = sandbox.run(script)
    assert existing.returncode == 0, existing.stderr
    assert "database openbox_trace exists" in existing.stderr
    assert "2 relation(s) in openbox_trace belong to another role" in existing.stderr
    assert len(notes(record, "sql_stdin")) == 1
    assert [entry["sql"] for entry in notes(record, "sql")] == [
        'ALTER DATABASE "openbox_trace" OWNER TO "openbox_trace"', "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    ]

    record = server(sandbox, postgres_down=True)
    down = sandbox.run(script)
    assert down.returncode == 1 and "cannot query PostgreSQL" in down.stderr
    assert notes(record, "sql") == [] and notes(record, "sql_stdin") == []


OOM_LINES = [
    "oom-kill:constraint=CONSTRAINT_MEMCG,task_memcg=/system.slice/docker-backend-id-full.scope,task=python,pid=11",
    "oom-kill:constraint=CONSTRAINT_MEMCG,task_memcg=/system.slice/docker-sandbox.scope,task=chrome,pid=12",
    "Memory cgroup out of memory: Killed process 11 (python)",
]


@needs_bash
def test_push_metrics_measures_the_host_and_keeps_the_counter_state(sandbox):
    spool = sandbox.root / "spool"
    (spool / "producers").mkdir(parents=True)
    record = server(sandbox, spool_dir=str(spool), trace_db_bytes=4096, kernel=OOM_LINES, business_statements=3)
    state_dir = sandbox.root / "ops"
    state_dir.mkdir()
    (state_dir / "cms-state.json").write_text('{"version":1,"last":{}}\n')

    result = sandbox.run(SCRIPTS_DIR / "push-metrics.sh", OPENBOX_OPS_STATE_DIR=str(state_dir))

    assert result.returncode == 0, result.stderr
    (push,) = notes(record, "cms")
    assert push["options"] == ["--instance", "gw2"]
    host_line, state_line = push["stdin"].splitlines()
    host = json.loads(host_line)
    assert set(host) == set(cms.HOST_METRICS)
    assert (host["oom_kills_1h"], host["backend_oom_kills_1h"], host["trace_db_bytes"]) == (2, 1, 4096)
    assert (host["spool_bytes"], host["spool_files"], host["spool_quarantined_files"]) == (0, 0, 0)
    assert 0 <= host["host_disk_used_percent"] <= 100
    assert (host["backend_cpu_percent"], host["backend_mem_percent"]) == (12.5, 40.25)
    assert host["business_trajectory_statements"] == 3
    (query,) = notes(record, "statements_query")
    assert query["database"] == "openbox" and "d.datname = 'openbox'" in query["sql"]
    assert "s.query ILIKE '%trajectory\\_%' AND s.query NOT ILIKE '%legacy\\_trajectory\\_%'" in query["sql"]
    assert json.loads(state_line) == {"version": 1, "last": {}}
    assert json.loads((state_dir / "cms-state.json").read_text()) == {"version": 1, "pushed_at": 1}
    assert notes(record, "one-off") == []


@needs_bash
def test_push_metrics_counts_no_business_statements_without_the_extension(sandbox):
    record = server(sandbox, pg_stat_statements_installed=0, stats="")
    result = sandbox.run(SCRIPTS_DIR / "push-metrics.sh", OPENBOX_OPS_STATE_DIR=str(sandbox.root / "ops"))
    assert result.returncode == 0, result.stderr
    host = json.loads(notes(record, "cms")[0]["stdin"].splitlines()[0])
    assert host["business_trajectory_statements"] == 0 and notes(record, "statements_query") == []
    assert host["backend_cpu_percent"] is None and host["backend_mem_percent"] is None


@needs_bash
def test_push_metrics_uses_a_one_off_worker_and_saves_the_state_when_the_report_fails(sandbox):
    record = server(sandbox, running=["backend", "postgres"], cms_status=1)
    state_dir = sandbox.root / "ops"
    script = SCRIPTS_DIR / "push-metrics.sh"

    failed = sandbox.run(script, "--instance", "aws-dev", OPENBOX_OPS_STATE_DIR=str(state_dir))

    assert failed.returncode == 1
    assert len(notes(record, "one-off")) == 1
    (push,) = notes(record, "cms")
    assert push["options"] == ["--instance", "aws-dev"]
    assert json.loads((state_dir / "cms-state.json").read_text()) == {"version": 1, "pushed_at": 1}

    (state_dir / "cms-state.json").write_text('{"version":1,"kept":true}')
    record = server(sandbox)
    dry = sandbox.run(script, "--dry-run", OPENBOX_OPS_STATE_DIR=str(state_dir))
    assert dry.returncode == 0, dry.stderr
    (push,) = notes(record, "cms")
    assert push["options"] == ["--instance", "gw2", "--dry-run"]
    assert json.loads((state_dir / "cms-state.json").read_text()) == {"version": 1, "kept": True}


def shanghai_yesterday() -> str:
    return (datetime.now(ZoneInfo("Asia/Shanghai")) - timedelta(days=1)).strftime("%Y-%m-%d")


@needs_bash
def test_analytics_export_runs_for_yesterday_and_reports_the_outcome(sandbox):
    script = SCRIPTS_DIR / "analytics-export.sh"
    record = server(sandbox)
    before = shanghai_yesterday()
    succeeded = sandbox.run(script)
    after = shanghai_yesterday()
    assert succeeded.returncode == 0, succeeded.stderr
    (export,) = notes(record, "analytics")
    assert export["options"] in (["--date", before], ["--date", after])
    assert [entry["options"] for entry in notes(record, "cms_put")] == [
        ["--instance", "gw2", "--metric", "analytics_export_failed=0"]
    ]

    record = server(sandbox, analytics_status=2)
    failed = sandbox.run(script, "--date", "2026-09-14", "--instance", "aws-dev")
    assert failed.returncode == 2 and "analytics export for 2026-09-14 failed" in failed.stderr
    assert [entry["options"] for entry in notes(record, "analytics")] == [["--date", "2026-09-14"]]
    assert [entry["options"] for entry in notes(record, "cms_put")] == [
        ["--instance", "aws-dev", "--metric", "analytics_export_failed=1"]
    ]

    record = server(sandbox, analytics_status=1)
    dry = sandbox.run(script, "--date", "2026-09-14", "--dry-run")
    assert dry.returncode == 1
    assert notes(record, "analytics")[0]["options"] == ["--date", "2026-09-14", "--dry-run"]
    assert notes(record, "cms_put")[0]["options"][-1] == "--dry-run"


@needs_bash
def test_analytics_export_reports_a_failure_when_the_worker_is_down(sandbox):
    record = server(sandbox, running=["backend", "postgres"])
    result = sandbox.run(SCRIPTS_DIR / "analytics-export.sh", "--date", "2026-09-14", "--wait", "0")
    assert result.returncode == 1 and "did not run" in result.stderr
    assert notes(record, "analytics") == []
    assert len(notes(record, "one-off")) == 1
    assert [entry["options"][-1] for entry in notes(record, "cms_put")] == ["analytics_export_failed=1"]


def token_files(sandbox: Sandbox) -> list[str]:
    (sandbox.root / "user.token").write_text("user.jwt-token\n")
    (sandbox.root / "admin.token").write_text("admin.jwt-token")
    return ["--session-id", "session_1", "--user-token-file", str(sandbox.root / "user.token"),
            "--admin-token-file", str(sandbox.root / "admin.token")]


@needs_bash
def test_drill_delete_session_deletes_through_the_api_and_waits_for_the_removal(sandbox):
    record = server(sandbox, admin_codes=[200, 410])
    args = token_files(sandbox)

    result = sandbox.run(SCRIPTS_DIR / "drill-delete-session.sh", *args, "--workspace-id", "ws_1", "--execute")

    assert result.returncode == 0, result.stderr
    assert "drill passed" in result.stderr
    requests = notes(record, "curl")
    assert [(entry["method"], entry["url"]) for entry in requests] == [
        ("GET", "http://127.0.0.1/api/admin/trajectories/sessions/session_1"),
        ("DELETE", "http://127.0.0.1:8080/api/agent/session/session_1"),
        ("GET", "http://127.0.0.1/api/admin/trajectories/sessions/session_1"),
    ]
    assert requests[1]["headers"] == "Authorization: Bearer user.jwt-token\nX-Workspace-Id: ws_1\n"
    assert requests[0]["headers"] == requests[2]["headers"] == "Authorization: Bearer admin.jwt-token\n"
    checks = notes(record, "deletion")
    assert [(entry["command"], entry["options"][:2]) for entry in checks] == [
        ("precheck", ["--session-id", "session_1"]), ("verify", ["--session-id", "session_1"]),
    ]
    assert checks[1]["options"][2] == "--timeout" and 0 < int(checks[1]["options"][3]) <= 900
    # The tokens reach curl on stdin only.
    assert "jwt-token" not in sandbox.log.read_text()


@needs_bash
@pytest.mark.parametrize(
    ("state", "message", "deleted"),
    [
        ({"admin_codes": [404]}, "expected 200 before the deletion", False),
        ({"deletion_precheck_status": 1}, "nothing was deleted", False),
        ({"delete_status": 403}, "answered HTTP 403", True),
        ({"deletion_verify_status": 1}, "is not fully deleted", True),
    ],
)
def test_drill_delete_session_fails_when_a_criterion_is_not_met(sandbox, state, message, deleted):
    record = server(sandbox, **state)
    result = sandbox.run(SCRIPTS_DIR / "drill-delete-session.sh", *token_files(sandbox), "--execute")
    assert result.returncode == 1 and message in result.stderr
    assert "drill passed" not in result.stderr
    assert any(entry["method"] == "DELETE" for entry in notes(record, "curl")) is deleted


TOC = [
    ";",
    "; Archive created at 2026-09-15 03:30:00 CST",
    "215; 1259 16386 TABLE public alembic_version openbox",
    "216; 1259 16390 TABLE public sessions openbox",
    "217; 1259 16395 TABLE public trajectory_events openbox_trace",
    "218; 0 0 TABLE ATTACH public trajectory_events_p20260915 openbox_trace",
    "3456; 0 16386 TABLE DATA public alembic_version openbox",
    "3; 3079 16400 EXTENSION - pg_trgm",
]


@needs_bash
def test_restore_check_restores_a_downloaded_backup_into_a_scratch_database_and_drops_it(sandbox):
    record = server(sandbox, toc=TOC, restored_tables=3, download="PGDMP restored")
    local = sandbox.root / "restore"

    result = sandbox.run(SCRIPTS_DIR / "restore-check.sh", "--key", BACKUP_KEY, "--local-dir", str(local), "--execute")

    assert result.returncode == 0, result.stderr
    assert "3 of 3 tables restored" in result.stderr and "restore check passed" in result.stderr
    assert [entry["options"] for entry in notes(record, "download")] == [["--key", BACKUP_KEY]]
    statements = notes(record, "sql")
    scratch = re.fullmatch(r'CREATE DATABASE "(openbox_restore_check_\d{8}t\d{6}z)" OWNER "openbox"',
                           statements[0]["sql"]).group(1)
    assert [(entry["database"], entry["sql"]) for entry in statements] == [
        ("postgres", f'CREATE DATABASE "{scratch}" OWNER "openbox"'),
        ("postgres", f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)'),
    ]
    (restore,) = notes(record, "pg_restore")
    assert restore["args"] == ["-U", "openbox", "-d", scratch, "--no-owner", "--no-privileges", "--exit-on-error"]
    assert restore["received"] == len(b"PGDMP restored")
    assert list(local.iterdir()) == []


@needs_bash
def test_restore_check_drops_the_scratch_database_when_the_check_fails(sandbox):
    dump = sandbox.root / "openbox.dump"
    dump.write_bytes(b"PGDMP local")
    record = server(sandbox, toc=TOC, restored_tables=2)
    mismatch = sandbox.run(SCRIPTS_DIR / "restore-check.sh", "--file", str(dump), "--execute")
    assert mismatch.returncode == 1 and "has 2 tables, the dump lists 3" in mismatch.stderr
    assert [entry["sql"].split(" ")[0] for entry in notes(record, "sql")] == ["CREATE", "DROP"]
    assert notes(record, "download") == [] and dump.exists()

    record = server(sandbox, toc=TOC, restore_status=1)
    failed = sandbox.run(SCRIPTS_DIR / "restore-check.sh", "--file", str(dump), "--execute")
    assert failed.returncode == 1 and "pg_restore into openbox_restore_check_" in failed.stderr
    assert [entry["sql"].split(" ")[0] for entry in notes(record, "sql")] == ["CREATE", "DROP"]

    record = server(sandbox, toc=[])
    empty = sandbox.run(SCRIPTS_DIR / "restore-check.sh", "--file", str(dump), "--execute")
    assert empty.returncode == 1 and "lists no tables" in empty.stderr
    assert notes(record, "sql") == []


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
    assert dry.stderr.count("dry-run: aliyun cms PutCustomMetricRule") == 16
    assert sandbox.calls() == []

    applied = sandbox.run(script, "--execute", "--webhook", "https://hooks.example.invalid/cms")
    assert applied.returncode == 0, applied.stderr
    calls = sandbox.calls()
    assert len(calls) == 16 and all(call[1:3] == ["cms", "PutCustomMetricRule"] for call in calls)
    rules = {option(call, "--RuleId"): call for call in calls}
    worker = rules["openbox-gw2-worker-down"]
    assert option(worker, "--MetricName") == "worker_up"
    assert option(worker, "--Resources") == '[{"groupId":0,"dimension":"instance=gw2"}]'
    assert option(worker, "--ContactGroups") == "云账号报警联系人"
    assert (option(worker, "--region"), option(worker, "--Webhook")) == ("cn-shanghai", "https://hooks.example.invalid/cms")
    # Average is the statistic the PutCustomMetricRule reference documents.
    assert {option(call, "--Statistics") for call in calls} == {"Average"}
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
        "archive_lag_events": (">=", "50000"),
        "hot_partitions": (">", "10"),
        "backend_cpu_percent": (">", "90"),
        "backend_mem_percent": (">", "90"),
        "business_trajectory_statements": (">", "0"),
        "events_ingested_24h": (">", "1000000"),
        "analytics_export_failed": (">", "0"),
    }
    for name in ("openbox-gw2-backend-cpu", "openbox-gw2-backend-memory"):
        # Five one-minute samples: above 90 % for 5 minutes.
        assert (option(rules[name], "--Period"), option(rules[name], "--EvaluationCount")) == ("60", "5")
    assert option(rules["openbox-gw2-events-ingested-24h"], "--Level") == "INFO"


def test_alarm_rules_only_use_metrics_that_are_reported():
    host = {name: 1 for name in cms.HOST_METRICS}
    health = {"status": "ok", "writer": True, "db": True, "spool": True, "blob_store": True}
    worker = {"counters": {name: 1 for name in cms.COUNTERS}, "gauges": {name: 1 for name in cms.GAUGES}}
    _, state = cms.collect(host, health, worker, {}, now=1757836800.0)
    values, _ = cms.collect(host, health, worker, state, now=1757836860.0)
    metrics = re.findall(r"^[a-z0-9-]+\|([a-z0-9_]+)\|", (SCRIPTS_DIR / "setup-alarms.sh").read_text(), flags=re.MULTILINE)
    assert len(metrics) == 16 and set(metrics) <= set(values) | set(cms.JOB_METRICS)
    assert "analytics_export_failed" in (SCRIPTS_DIR / "analytics-export.sh").read_text()


@needs_bash
def test_install_timers_dry_run_lists_the_units_and_the_metrics_instance(sandbox):
    script = SCRIPTS_DIR / "install-timers.sh"
    services = ("openbox-trajectory-metrics.service", "openbox-trajectory-analytics.service")
    result = sandbox.run(script, "--dry-run")
    assert result.returncode == 0, result.stderr
    installs = re.findall(r"dry-run: install -m 0644 \S+/systemd/(\S+) ", result.stderr)
    assert sorted(installs) == sorted(path.name for path in UNITS_DIR.iterdir())
    assert (
        "systemctl enable --now openbox-trajectory-metrics.timer openbox-pg-backup.timer "
        "openbox-trajectory-analytics.timer openbox-prune-images.timer"
    ) in result.stderr
    for service in services:
        assert (
            f"/etc/systemd/system/{service}.d/instance.conf: [Service]\n"
            "Environment=OPENBOX_CMS_INSTANCE=gw2\n"
        ) in result.stderr
    assert sandbox.calls() == []

    other = sandbox.run(script, "--dry-run", "--instance", "aws-dev")
    assert other.returncode == 0, other.stderr
    assert other.stderr.count("Environment=OPENBOX_CMS_INSTANCE=aws-dev\n") == 2

    removed = sandbox.run(script, "--dry-run", "--uninstall")
    assert removed.returncode == 0, removed.stderr
    for service in services:
        assert f"dry-run: rm -rf /etc/systemd/system/{service}.d" in removed.stderr


def compose_available() -> bool:
    return DOCKER is not None and subprocess.run([DOCKER, "compose", "version"], capture_output=True).returncode == 0


def compose_environment(**values: str) -> dict:
    environment = {key: value for key, value in os.environ.items() if not key.startswith(("COMPOSE_", "OPENBOX_"))}
    return {**environment, **values}


@pytest.mark.skipif(not compose_available(), reason="docker compose is not installed")
def test_overlay_validates_in_place_against_the_examples():
    result = subprocess.run(
        [DOCKER, "compose", "-f", "deploy/gw2/docker-compose.base.example.yml", "-f",
         "deploy/gw2/docker-compose.trajectory.yml", "-f", "deploy/gw2/docker-compose.override.example.yml",
         "config", "--format", "json"],
        cwd=REPO, env=compose_environment(OPENBOX_IMAGE_TAG="20260915-trajectory-example", OPENBOX_DB_PASSWORD="change-me",
                                          OPENBOX_TRACE_DB_PASSWORD="change-me-trace-password"),
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert services["trajectory-worker"]["environment"]["INTERNAL_API_TOKEN"] == "placeholder"


def test_the_validation_placeholder_stays_out_of_the_release_bundle():
    assert {"config export-ignore", "config/** export-ignore"} <= set((DEPLOY / ".gitattributes").read_text().splitlines())
    values = [
        line.partition("=")[2] for line in (DEPLOY / "config" / "backend.env").read_text().splitlines()
        if line and not line.startswith("#")
    ]
    assert values and set(values) <= {"placeholder", "example-bucket", "cn-shanghai", "false"}


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
    (project / "config" / "backend.env").write_text(
        "JWT_SECRET=placeholder\nINTERNAL_API_TOKEN=placeholder\n"
        "DATABASE_URL=postgresql+asyncpg://openbox:from-env-file@localhost:5432/openbox\n"
    )
    (project / "config" / "openbox.json").write_text("{}")
    (project / "secrets" / "aliyun-config.json").write_text("{}")

    result = subprocess.run(
        [DOCKER, "compose", "config", "--format", "json"], cwd=project, env=compose_environment(), capture_output=True,
        text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    assert config["name"] == "openbox"
    services = config["services"]
    assert "backend-worker" not in services

    worker = services["trajectory-worker"]
    assert worker["image"] == services["backend"]["image"] == IMAGE_PIN
    assert worker["command"] == [
        "/bin/sh", "-ec", "alembic -c alembic_trajectory.ini upgrade head && exec python -m trajectory.worker",
    ]
    assert worker["environment"]["TRAJECTORY_DATABASE_URL"] == (
        "postgresql+asyncpg://openbox_trace:change-me-trace-password@postgres:5432/openbox_trace"
    )
    # The business connection string of backend.env never reaches the worker.
    assert worker["environment"]["DATABASE_URL"] == ""
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
    # Old local trajectory blob files are not converted, so the worker does not mount the blob volume.
    assert "/legacy-blobs" not in mounts
    assert {name: dependency["condition"] for name, dependency in worker["depends_on"].items()} == {
        "postgres": "service_healthy", "redis": "service_healthy",
    }
    assert worker["healthcheck"]["test"] == ["CMD", "curl", "-fsS", "http://127.0.0.1:8090/health"]
    assert float(worker["cpus"]) == 1.0 and int(worker["mem_limit"]) == 1024**3
    assert worker["restart"] == "unless-stopped" and worker["logging"]["options"]["max-size"] == "50m"

    backend = services["backend"]
    assert {key: backend["environment"][key] for key in ("TRAJECTORY_SINK", "TRAJECTORY_WORKER_MODE", "TRAJECTORY_SPOOL_DIR")} == {
        "TRAJECTORY_SINK": "spool", "TRAJECTORY_WORKER_MODE": "external",
        "TRAJECTORY_SPOOL_DIR": "/var/lib/openbox/trajectory-spool",
    }
    assert backend["environment"]["DATABASE_URL"] == "postgresql+asyncpg://openbox:change-me@postgres:5432/openbox"
    spool_users = sorted(
        name for name, service in services.items()
        if any(volume.get("source") == "trajectory-spool" for volume in service.get("volumes", []))
    )
    assert spool_users == ["backend", "trajectory-worker"]
    assert services["frontend"]["environment"] == {"BACKEND_HOST": "backend:8080", "TRAJECTORY_HOST": "trajectory-worker:8090"}
    assert services["frontend"]["image"] == "openbox-frontend-v2:20260915-trajectory-example"

    postgres = services["postgres"]
    assert int(postgres["mem_limit"]) == 2 * 1024**3
    assert postgres["command"] == [
        "postgres", "-c", "shared_buffers=512MB", "-c", "effective_cache_size=1GB",
        "-c", "shared_preload_libraries=pg_stat_statements", "-c", "pg_stat_statements.track=all",
        "-c", "max_connections=200",
    ]

    # The sanitized base and override files follow the gw2 files.
    assert postgres["image"] == "public.ecr.aws/docker/library/postgres:16-alpine"
    assert services["redis"]["command"] == ["redis-server", "--appendonly", "yes"]
    assert backend["command"] == ["sh", "-c", "alembic upgrade head && exec uvicorn main:app --host 0.0.0.0 --port 8080"]
    assert {name: int(services[name]["mem_limit"]) for name in ("redis", "backend", "frontend")} == {
        "redis": 192 * 1024**2, "backend": 3 * 1024**3, "frontend": 128 * 1024**2,
    }
    assert {volume["target"]: volume["source"] for volume in backend["volumes"]}["/tmp/openbox-blobs"] == "blob-data"
    assert {"/run/secrets/aliyun-config.json", "/run/secrets/alipay", "/run/secrets/bossip-apns.p8"} <= set(
        volume["target"] for volume in backend["volumes"]
    )
    assert [(port.get("host_ip"), port["published"], port["target"]) for port in backend["ports"]] == [("127.0.0.1", "8080", 8080)]
    assert [(port["published"], port["target"]) for port in services["frontend"]["ports"]] == [("80", 80)]
    assert backend["depends_on"]["postgres"]["condition"] == services["frontend"]["depends_on"]["backend"]["condition"] == "service_healthy"


def unit(name: str) -> ConfigParser:
    parser = ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read_string((UNITS_DIR / name).read_text())
    return parser


def test_timers_follow_the_spec_schedules():
    schedules = {
        "openbox-trajectory-metrics.timer": "*-*-* *:*:00",
        "openbox-pg-backup.timer": "*-*-* 03:30:00 Asia/Shanghai",
        "openbox-trajectory-analytics.timer": "*-*-* 04:00:00 Asia/Shanghai",
        "openbox-prune-images.timer": "Sun *-*-* 04:30:00 Asia/Shanghai",
    }
    assert {path.name for path in UNITS_DIR.glob("*.timer")} == set(schedules)
    for name, calendar in schedules.items():
        timer = unit(name)
        assert timer["Timer"]["OnCalendar"] == calendar
        assert timer["Timer"]["Unit"] == name.replace(".timer", ".service")
        assert timer["Install"]["WantedBy"] == "timers.target"
    # A missed run (host down at 04:00) catches up at boot.
    assert unit("openbox-trajectory-analytics.timer")["Timer"]["Persistent"] == "true"


def test_services_run_the_deployed_scripts():
    services = sorted(UNITS_DIR.glob("*.service"))
    assert len(services) == 4
    for path in services:
        service = unit(path.name)
        assert service["Service"]["Type"] == "oneshot"
        assert "docker.service" in service["Unit"]["After"]
        executable = Path(service["Service"]["ExecStart"].split()[0])
        assert executable.parent == Path("/opt/openbox/deploy/gw2/scripts")
        assert (SCRIPTS_DIR / executable.name) in EXECUTABLES
    assert unit("openbox-prune-images.service")["Service"]["ExecStart"].endswith("prune-images.sh --execute")
    assert unit("openbox-trajectory-analytics.service")["Service"]["ExecStart"].endswith("/analytics-export.sh")


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
    assert "openbox_trace" in text  # the trace database URL uses the dedicated role
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
    # A pod is Ready only while every container is: a probe on the sidecar would take the business backend out of
    # its Service whenever the worker or the trace database is unhealthy, so the sidecar has none and keeps
    # retrying its migrations instead of crash-looping.
    assert not {"readinessProbe", "livenessProbe", "startupProbe"} & set(worker)
    assert backend["readinessProbe"]["httpGet"] == {"path": "/health", "port": 8080}
    assert worker["command"][:2] == ["/bin/sh", "-ec"]
    script = worker["command"][2]
    assert "until alembic -c alembic_trajectory.ini upgrade head; do" in script
    assert script.rstrip().endswith("exec python -m trajectory.worker")
    subprocess.run(["/bin/sh", "-n", "-c", script], check=True)

    backend_env = container_environment(backend, documents)
    assert (backend_env["TRAJECTORY_SINK"], backend_env["TRAJECTORY_WORKER_MODE"]) == ("spool", "external")
    worker_env = container_environment(worker, documents)
    assert worker_env["TRAJECTORY_WORKER_MODE"] == "external"
    assert worker_env["TRAJECTORY_BACKEND_INTERNAL_URL"] == "http://127.0.0.1:8080"
    assert backend_env["TRAJECTORY_SPOOL_DIR"] == worker_env["TRAJECTORY_SPOOL_DIR"] == "/var/lib/openbox/trajectory-spool"
    # The worker never receives the business database URL.
    assert worker_env.get("DATABASE_URL", "") == ""

    service = next(doc for doc in documents if doc["kind"] == "Service" and doc["metadata"]["name"] == "openbox-trajectory-worker")
    assert service["spec"]["selector"] == {"app": "openbox-backend"}
    assert service["spec"]["ports"][0]["targetPort"] == 8090
    if manifest == "base.yaml":
        # GKE health-checks the worker through its own BackendConfig; nothing is derived from a probe.
        annotation = json.loads(service["metadata"]["annotations"]["cloud.google.com/backend-config"])
        backend_config = next(
            doc for doc in documents if doc["kind"] == "BackendConfig" and doc["metadata"]["name"] == annotation["default"]
        )
        assert backend_config["spec"]["healthCheck"] == {"type": "HTTP", "requestPath": "/health", "port": 8090}
        assert backend_config["spec"]["timeoutSec"] == 3600
    ingress = next(doc for doc in documents if doc["kind"] == "Ingress")
    routes = {
        path["path"]: path["backend"]["service"] for rule in ingress["spec"]["rules"] for path in rule["http"]["paths"]
    }
    for path in ("/api/admin/trajectories", "/ws/admin/trajectories"):
        assert routes[path] == {"name": "openbox-trajectory-worker", "port": {"number": 8090}}
    assert routes["/api"]["name"] == "openbox-backend"
