"""Skill installation must not be steerable into the rest of the filesystem.

The install routes join a caller-supplied name onto SKILLS_DIR and rmtree the
existing copy at that path, and they unpack caller-supplied archives. Both were
exploitable: a name of ``../../opt/openbox/skills`` deleted the builtin skills,
and a tar carrying ``escape -> /`` followed by ``escape/x`` wrote outside the
tree without any member name containing "..".

These load the container's action_server by source, with its absolute paths
rebound into tmp_path, because it ships as a standalone script rather than an
importable package.
"""
import asyncio
import io
import multiprocessing
import os
import tarfile
import zipfile
from pathlib import Path

import pytest
from fastapi import HTTPException, UploadFile

ACTION_SERVER = Path(__file__).resolve().parents[3] / "container" / "action_server.py"


@pytest.fixture
def server(tmp_path):
    """action_server's module namespace, rooted at tmp_path instead of /data."""
    skills = tmp_path / "data" / "skills"
    builtin = tmp_path / "opt" / "openbox" / "skills"
    skills.mkdir(parents=True)
    builtin.mkdir(parents=True)

    src = ACTION_SERVER.read_text()
    src = src.replace('Path("/data/skills")', f'Path(r"{skills}")')
    src = src.replace('Path("/opt/openbox/skills")', f'Path(r"{builtin}")')
    src = src.replace('Path("/data/mcp/config.json")', f'Path(r"{tmp_path}/data/mcp/config.json")')
    src = src.replace('Path("/workspace/skills")', f'Path(r"{tmp_path}/workspace/skills")')
    src = src.replace('Path("/workspace/exports")', f'Path(r"{tmp_path}/workspace/exports")')
    src = src.replace('Path("/data/.manifest.json")', f'Path(r"{tmp_path}/data/.manifest.json")')
    src = src.replace('if __name__ == "__main__":', "if False:")

    # Supply the real source location so action_server can import its sibling
    # media_jobs module regardless of whether pytest was launched from the
    # repository root or from backend/ (the documented test command).
    ns = {"__name__": "action_server_under_test", "__file__": str(ACTION_SERVER)}
    exec(compile(src, str(ACTION_SERVER), "exec"), ns)
    ns["_test_skills_dir"] = skills
    ns["_test_builtin_dir"] = builtin
    return ns


# --- name validation ---------------------------------------------------------

@pytest.mark.parametrize("name", [
    "../../opt/openbox/skills",   # the escape that deleted the builtin skills
    "..",
    ".",
    "../evil",
    "nested/path",
    "back\\slash",
    ".hidden",                    # collides with the .<name>.incoming staging dirs
    "",
    "   ",
    "/etc/passwd",
])
def test_unsafe_skill_names_rejected(server, name):
    with pytest.raises(HTTPException) as exc:
        server["_safe_skill_name"](name)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("name,expected", [
    ("ok-name", "ok-name"),
    ("my skill", "my-skill"),      # spaces are normalised, not rejected
    ("UPPER_case.v2", "UPPER_case.v2"),
])
def test_safe_skill_names_accepted(server, name, expected):
    assert server["_safe_skill_name"](name) == expected


# --- clone URL validation ----------------------------------------------------

@pytest.mark.parametrize("url", [
    'ext::sh -c "touch /tmp/pwned"',   # git runs this through a shell
    "file:///etc",
    "",
    "   ",
])
def test_unsafe_clone_urls_rejected(server, url):
    with pytest.raises(HTTPException) as exc:
        server["_validate_skill_url"](url)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("url", [
    "https://github.com/anthropics/skills.git",
    "http://127.0.0.1:9418/probe.git",
    "git://127.0.0.1/probe.git",
    "ssh://git@github.com/o/r.git",
    "git@github.com:o/r.git",
])
def test_supported_clone_urls_accepted(server, url):
    assert server["_validate_skill_url"](url) == url


# --- archive extraction ------------------------------------------------------

def _tar_with_absolute_symlink(target: Path) -> bytes:
    """An archive that escapes via a link, with no ".." in any member name."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        doc = b"---\nname: evil\ndescription: d\n---\nbody\n"
        info = tarfile.TarInfo("evil/SKILL.md")
        info.size = len(doc)
        tf.addfile(info, io.BytesIO(doc))

        link = tarfile.TarInfo("evil/escape")
        link.type = tarfile.SYMTYPE
        link.linkname = str(target)
        tf.addfile(link)

        payload = b"pwned"
        through = tarfile.TarInfo("evil/escape/pwned.txt")
        through.size = len(payload)
        tf.addfile(through, io.BytesIO(payload))
    return buf.getvalue()


def test_symlink_escape_has_no_dotdot_to_catch(server):
    """The pre-extraction name check cannot see this attack — hence filter=."""
    blob = _tar_with_absolute_symlink(server["_test_builtin_dir"])
    names = tarfile.open(fileobj=io.BytesIO(blob)).getnames()
    assert not any(".." in n for n in names)
    assert not any(n.startswith("/") for n in names)


def test_data_filter_refuses_absolute_symlink(server, tmp_path):
    blob = _tar_with_absolute_symlink(server["_test_builtin_dir"])
    dest = tmp_path / "extract"
    dest.mkdir()
    with tarfile.open(fileobj=io.BytesIO(blob)) as tf:
        with pytest.raises(tarfile.TarError):
            tf.extractall(dest, filter="data")
    assert not (server["_test_builtin_dir"] / "pwned.txt").exists()


@pytest.mark.asyncio
async def test_tar_upload_keeps_legacy_format_with_bounded_preflight(server):
    name = "tar-helper"
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w") as bundle:
        skill_md = _skill_md(name).encode()
        info = tarfile.TarInfo(f"{name}/SKILL.md")
        info.size = len(skill_md)
        bundle.addfile(info, io.BytesIO(skill_md))
        marker = b"legacy-tar"
        info = tarfile.TarInfo(f"{name}/marker.txt")
        info.size = len(marker)
        bundle.addfile(info, io.BytesIO(marker))

    installed = await server["upload_skill_archive"](
        UploadFile(filename=f"{name}.tar", file=io.BytesIO(blob.getvalue())),
        name,
        False,
        None,
    )

    assert installed["install_dir"] == name
    assert (server["_test_skills_dir"] / name / "marker.txt").read_bytes() == marker


@pytest.mark.asyncio
async def test_tar_link_is_rejected_by_endpoint_before_publish(server):
    name = "tar-link-helper"
    blob = _tar_with_absolute_symlink(server["_test_builtin_dir"])

    with pytest.raises(HTTPException) as rejected:
        await server["upload_skill_archive"](
            UploadFile(filename=f"{name}.tar", file=io.BytesIO(blob)),
            name,
            False,
            None,
        )

    assert rejected.value.detail["code"] == "special_entry"
    assert not (server["_test_skills_dir"] / name).exists()


# --- listing hygiene ---------------------------------------------------------

def test_staging_directories_are_not_listed_as_skills(server):
    """An install in flight must not surface as a half-written skill."""
    skills = server["_test_skills_dir"]
    staging = skills / ".half-written.incoming"
    staging.mkdir()
    (staging / "SKILL.md").write_text("---\nname: half\ndescription: d\n---\nx\n")

    found = server["_scan_skills_in_dir"](skills, source="container")
    assert [s["name"] for s in found] == []


def test_real_skill_directory_is_listed(server):
    skills = server["_test_skills_dir"]
    real = skills / "genuine"
    real.mkdir()
    (real / "SKILL.md").write_text("---\nname: genuine\ndescription: d\n---\nx\n")

    found = server["_scan_skills_in_dir"](skills, source="container")
    assert [s["name"] for s in found] == ["genuine"]


# --- atomic chat-created skill packages -------------------------------------

def _skill_md(name="greeting-helper"):
    return (
        "---\n"
        f"name: {name}\n"
        "description: Replies with a stable greeting.\n"
        "---\n\n"
        "# Instructions\n\nAlways use the greeting from references/greeting.txt.\n"
    )


def _skill_zip(name: str, marker: str) -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(f"{name}/SKILL.md", _skill_md(name))
        bundle.writestr(f"{name}/marker.txt", marker)
    return archive.getvalue()


def _skill_zip_entries(entries, *, compression=zipfile.ZIP_STORED) -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=compression) as bundle:
        for entry, content in entries:
            bundle.writestr(entry, content)
    return archive.getvalue()


def test_action_server_and_backend_use_the_same_skill_zip_resource_envelope(server):
    from skill import archive as backend_policy

    assert server["_SKILL_ARCHIVE_POLICY_VERSION"] == backend_policy.SKILL_ARCHIVE_POLICY_VERSION
    assert server["_SKILL_ARCHIVE_MAX_COMPRESSED_BYTES"] == backend_policy.SKILL_ARCHIVE_MAX_COMPRESSED_BYTES
    assert server["_SKILL_ARCHIVE_MAX_FILES"] == backend_policy.SKILL_ARCHIVE_MAX_ENTRIES
    assert server["_SKILL_ARCHIVE_MAX_FILE_BYTES"] == backend_policy.SKILL_ARCHIVE_MAX_FILE_BYTES
    assert server["_SKILL_ARCHIVE_MAX_TOTAL_BYTES"] == backend_policy.SKILL_ARCHIVE_MAX_TOTAL_BYTES
    assert server["_SKILL_ARCHIVE_MAX_RATIO"] == backend_policy.SKILL_ARCHIVE_MAX_RATIO
    assert server["_SKILL_ARCHIVE_RATIO_MIN_BYTES"] == backend_policy.SKILL_ARCHIVE_RATIO_MIN_BYTES
    assert server["_SKILL_ARCHIVE_MAX_PATH_BYTES"] == backend_policy.SKILL_ARCHIVE_MAX_PATH_BYTES
    assert server["_SKILL_ARCHIVE_MAX_DEPTH"] == backend_policy.SKILL_ARCHIVE_MAX_DEPTH


@pytest.mark.asyncio
async def test_zip_symlink_duplicate_and_collision_never_reach_publish(server):
    upload = server["upload_skill_archive"]
    name = "unsafe-restore-helper"
    link = zipfile.ZipInfo(f"{name}/link")
    link.create_system = 3
    link.external_attr = (0o120777 << 16)
    bad_archives = [
        (_skill_zip_entries([
            (f"{name}/SKILL.md", _skill_md(name)),
            (link, "../../outside"),
        ]), "special_entry"),
        (_skill_zip_entries([
            (f"{name}/SKILL.md", _skill_md(name)),
            (f"{name}/skill.md", "duplicate after case folding"),
        ]), "duplicate_path"),
        (_skill_zip_entries([
            (f"{name}/references/guide.md", "guide"),
            (f"{name}/references", "shadow"),
        ]), "path_collision"),
    ]

    for blob, expected_code in bad_archives:
        with pytest.raises(HTTPException) as rejected:
            await upload(
                UploadFile(filename=f"{name}.zip", file=io.BytesIO(blob)),
                name,
                True,
                1,
            )
        assert rejected.value.detail["code"] == expected_code
        assert not (server["_test_skills_dir"] / name).exists()
        assert server["_skill_restore_fence_generation"](
            server["_test_skills_dir"], name
        ) == 0


@pytest.mark.asyncio
async def test_zip_resource_rejection_preserves_restore_generation_fence(server):
    upload = server["upload_skill_archive"]
    name = "bounded-restore-helper"
    skills = server["_test_skills_dir"]

    # Make the budgets tiny so the test exercises every branch without
    # allocating production-sized payloads.
    original_file = server["_SKILL_ARCHIVE_MAX_FILE_BYTES"]
    original_total = server["_SKILL_ARCHIVE_MAX_TOTAL_BYTES"]
    original_entries = server["_SKILL_ARCHIVE_MAX_FILES"]
    try:
        server["_SKILL_ARCHIVE_MAX_FILE_BYTES"] = 16
        too_large = _skill_zip_entries([(f"{name}/SKILL.md", "x" * 17)])
        with pytest.raises(HTTPException) as single:
            await upload(
                UploadFile(filename=f"{name}.zip", file=io.BytesIO(too_large)),
                name,
                True,
                1,
            )
        assert single.value.detail["code"] == "file_too_large"

        server["_SKILL_ARCHIVE_MAX_FILE_BYTES"] = 32
        server["_SKILL_ARCHIVE_MAX_TOTAL_BYTES"] = 20
        total = _skill_zip_entries([
            (f"{name}/SKILL.md", "x" * 12),
            (f"{name}/guide.md", "y" * 12),
        ])
        with pytest.raises(HTTPException) as aggregate:
            await upload(
                UploadFile(filename=f"{name}.zip", file=io.BytesIO(total)),
                name,
                True,
                1,
            )
        assert aggregate.value.detail["code"] == "archive_too_large"

        server["_SKILL_ARCHIVE_MAX_TOTAL_BYTES"] = 100
        server["_SKILL_ARCHIVE_MAX_FILES"] = 2
        entries = _skill_zip_entries([
            ("one/SKILL.md", "one"),
            ("two/guide.md", "two"),
        ])
        with pytest.raises(HTTPException) as count:
            await upload(
                UploadFile(filename=f"{name}.zip", file=io.BytesIO(entries)),
                name,
                True,
                1,
            )
        assert count.value.detail["code"] == "too_many_entries"
    finally:
        server["_SKILL_ARCHIVE_MAX_FILE_BYTES"] = original_file
        server["_SKILL_ARCHIVE_MAX_TOTAL_BYTES"] = original_total
        server["_SKILL_ARCHIVE_MAX_FILES"] = original_entries

    assert not (skills / name).exists()
    assert server["_skill_restore_fence_generation"](skills, name) == 0

    # A rejected archive did not consume or advance the lifecycle generation.
    restored = await upload(
        UploadFile(filename=f"{name}.zip", file=io.BytesIO(_skill_zip(name, "valid"))),
        name,
        True,
        1,
    )
    assert restored["install_dir"] == name


@pytest.mark.asyncio
async def test_high_compression_ratio_is_rejected_before_zip_writes(server):
    upload = server["upload_skill_archive"]
    name = "zip-bomb-helper"
    original_ratio = server["_SKILL_ARCHIVE_MAX_RATIO"]
    original_minimum = server["_SKILL_ARCHIVE_RATIO_MIN_BYTES"]
    try:
        server["_SKILL_ARCHIVE_MAX_RATIO"] = 10
        server["_SKILL_ARCHIVE_RATIO_MIN_BYTES"] = 1
        bomb = _skill_zip_entries(
            [(f"{name}/SKILL.md", "0" * 100_000)],
            compression=zipfile.ZIP_DEFLATED,
        )
        with pytest.raises(HTTPException) as rejected:
            await upload(
                UploadFile(filename=f"{name}.zip", file=io.BytesIO(bomb)),
                name,
                True,
                1,
            )
        assert rejected.value.detail["code"] == "compression_ratio"
        assert not (server["_test_skills_dir"] / name).exists()
    finally:
        server["_SKILL_ARCHIVE_MAX_RATIO"] = original_ratio
        server["_SKILL_ARCHIVE_RATIO_MIN_BYTES"] = original_minimum


def test_export_stores_extremely_compressible_file_to_remain_restorable(server):
    name = "compressible-export-helper"
    target = server["_test_skills_dir"] / name
    target.mkdir()
    (target / "SKILL.md").write_text(_skill_md(name))
    (target / "zeros.bin").write_bytes(b"0" * (1024 * 1024))

    exported_name, blob = server["_skill_archive_bytes"](name)

    assert exported_name == name
    with zipfile.ZipFile(io.BytesIO(blob)) as bundle:
        assert bundle.getinfo(f"{name}/zeros.bin").compress_type == zipfile.ZIP_STORED
    # Producer and consumer policy are closed over the same bytes.
    server["_extract_bounded_skill_zip"](blob, target.parent / "extract-probe")


@pytest.mark.asyncio
async def test_archive_create_only_conflict_never_replaces_live_package(server):
    upload = server["upload_skill_archive"]
    name = "restore-helper"
    assert "skill_archive_create_only_v1" in (await server["alive"]())["capabilities"]

    first = await upload(
        UploadFile(filename=f"{name}.zip", file=io.BytesIO(_skill_zip(name, "winner"))),
        name,
        True,
        None,
    )
    assert first["install_dir"] == name

    with pytest.raises(HTTPException) as conflict:
        await upload(
            UploadFile(
                filename=f"{name}.zip",
                file=io.BytesIO(_skill_zip(name, "must-not-win")),
            ),
            name,
            True,
            None,
        )

    assert conflict.value.status_code == 409
    assert conflict.value.detail["code"] == "skill_already_exists"
    target = server["_test_skills_dir"] / name
    assert (target / "marker.txt").read_text() == "winner"
    assert not list(server["_test_skills_dir"].glob(".*.incoming"))

    # The public upload endpoint remains an explicit update by default.
    updated = await upload(
        UploadFile(filename=f"{name}.zip", file=io.BytesIO(_skill_zip(name, "updated"))),
        name,
        False,
        None,
    )
    assert updated["install_dir"] == name
    assert (target / "marker.txt").read_text() == "updated"


@pytest.mark.asyncio
async def test_durable_uninstall_fences_late_snapshot_restore(server):
    upload = server["upload_skill_archive"]
    name = "fenced-restore-helper"
    target = server["_test_skills_dir"] / name
    assert "skill_restore_fence_v1" in (await server["alive"]())["capabilities"]

    await upload(
        UploadFile(filename=f"{name}.zip", file=io.BytesIO(_skill_zip(name, "old"))),
        name,
        True,
        1,
    )
    removed = await server["uninstall_skill"](name, 2)
    assert removed["mutation_generation"] == 2
    assert not target.exists()

    with pytest.raises(HTTPException) as stale:
        await upload(
            UploadFile(
                filename=f"{name}.zip",
                file=io.BytesIO(_skill_zip(name, "must-not-revive")),
            ),
            name,
            True,
            1,
        )

    assert stale.value.status_code == 409
    assert stale.value.detail == {
        "code": "skill_restore_fenced",
        "name": name,
        "fenced_through_generation": 2,
        "message": "A newer durable uninstall fenced this Skill restore",
    }
    assert not target.exists()

    # A deliberate user upload is still an update/create operation and is not
    # mistaken for an automatic stale snapshot restore.
    await upload(
        UploadFile(filename=f"{name}.zip", file=io.BytesIO(_skill_zip(name, "new"))),
        name,
        False,
        None,
    )
    assert (target / "marker.txt").read_text() == "new"


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="cross-process lock regression requires fork inheritance",
)
def test_archive_create_only_has_one_winner_across_processes(server):
    skills_dir = server["_test_skills_dir"]
    name = "process-race-helper"
    labels = ("worker-a", "worker-b")
    for label in labels:
        staging = skills_dir / f".{name}.{label}.incoming"
        staging.mkdir()
        (staging / "SKILL.md").write_text(_skill_md(name))
        (staging / "marker.txt").write_text(label)

    context = multiprocessing.get_context("fork")
    start = context.Event()
    results = context.Queue()

    def publish(label: str) -> None:
        start.wait()
        staging = skills_dir / f".{name}.{label}.incoming"
        try:
            server["_publish_skill_staging"](
                skills_dir,
                name,
                staging,
                create_only=True,
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            results.put((label, exc.status_code, detail.get("code")))
        else:
            results.put((label, 200, None))

    processes = [context.Process(target=publish, args=(label,)) for label in labels]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    outcomes = [results.get(timeout=2) for _ in labels]
    assert sorted(status for _label, status, _code in outcomes) == [200, 409]
    assert [code for _label, status, code in outcomes if status == 409] == [
        "skill_already_exists"
    ]
    winner = next(label for label, status, _code in outcomes if status == 200)
    assert (skills_dir / name / "marker.txt").read_text() == winner


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(),
    reason="cross-process lock regression requires fork inheritance",
)
def test_restore_and_uninstall_race_finishes_absent_across_processes(server):
    skills_dir = server["_test_skills_dir"]
    name = "process-delete-fence-helper"
    staging = skills_dir / f".{name}.restore.incoming"
    staging.mkdir()
    (staging / "SKILL.md").write_text(_skill_md(name))
    (staging / "marker.txt").write_text("stale-restore")

    context = multiprocessing.get_context("fork")
    start = context.Event()
    results = context.Queue()

    def restore() -> None:
        start.wait()
        try:
            server["_publish_skill_staging"](
                skills_dir,
                name,
                staging,
                create_only=True,
                restore_generation=1,
            )
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            results.put(("restore", exc.status_code, detail.get("code")))
        else:
            results.put(("restore", 200, None))

    def uninstall() -> None:
        start.wait()
        result = asyncio.run(server["uninstall_skill"](name, 2))
        results.put(("uninstall", 200, result.get("mutation_generation")))

    processes = [
        context.Process(target=restore),
        context.Process(target=uninstall),
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    outcomes = {kind: (status, detail) for kind, status, detail in (
        results.get(timeout=2) for _ in processes
    )}
    assert outcomes["uninstall"] == (200, 2)
    assert outcomes["restore"] in {
        (200, None),
        (409, "skill_restore_fenced"),
    }
    assert not (skills_dir / name).exists()
    assert server["_skill_restore_fence_generation"](skills_dir, name) == 2


@pytest.mark.asyncio
async def test_create_skill_publishes_a_complete_package_atomically(server):
    request = server["CreateSkillRequest"](
        name="greeting-helper",
        skill_md=_skill_md(),
        files=[
            {"path": "references/greeting.txt", "content": "Hello, OpenBox!\n"},
            {"path": "scripts/render.py", "content": "print('hello')\n"},
        ],
    )

    result = await server["create_skill"](request)
    target = server["_test_skills_dir"] / "greeting-helper"

    assert result["created"] is True
    assert result["name"] == "greeting-helper"
    assert (target / "SKILL.md").read_text() == _skill_md()
    assert (target / "references/greeting.txt").read_text() == "Hello, OpenBox!\n"
    assert not list(server["_test_skills_dir"].glob(".*.incoming"))


@pytest.mark.parametrize("path", [
    "../escape.txt",
    "references/../../escape.txt",
    "/absolute.txt",
    "nested\\windows.txt",
    "hidden/.private.txt",
    ".hidden/file.txt",
    "install.sh",
    "scripts/INSTALL.SH",
    ".env",
    "keys/server.pem",
    "config/credentials.json",
    "secrets/value.txt",
])
@pytest.mark.asyncio
async def test_create_skill_rejects_unsafe_secret_and_executable_paths(server, path):
    request = server["CreateSkillRequest"](
        name="greeting-helper",
        skill_md=_skill_md(),
        files=[{"path": path, "content": "not allowed"}],
    )

    with pytest.raises(HTTPException) as exc:
        await server["create_skill"](request)

    assert exc.value.status_code == 400
    assert not (server["_test_skills_dir"] / "greeting-helper").exists()
    assert not list(server["_test_skills_dir"].glob(".*.incoming"))


@pytest.mark.parametrize("name", [
    "Greeting-Helper",
    "greeting_helper",
    "greeting helper",
    "-greeting",
    "greeting-",
    "greeting--helper",
    "a" * 65,
    "../greeting",
])
@pytest.mark.asyncio
async def test_create_skill_requires_a_strict_slug(server, name):
    # model_construct lets the endpoint's filesystem boundary be exercised for
    # overlong input too; HTTP requests also have a Pydantic length guard.
    request = server["CreateSkillRequest"].model_construct(
        name=name, skill_md=_skill_md(name), files=[]
    )
    with pytest.raises(HTTPException) as exc:
        await server["create_skill"](request)
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_create_skill_requires_matching_valid_frontmatter(server):
    request = server["CreateSkillRequest"](
        name="greeting-helper",
        skill_md=_skill_md("different-name"),
        files=[],
    )
    with pytest.raises(HTTPException) as exc:
        await server["create_skill"](request)
    assert exc.value.status_code == 400
    assert "exactly match" in exc.value.detail


@pytest.mark.asyncio
async def test_create_skill_enforces_total_package_size_before_writing(server):
    request = server["CreateSkillRequest"](
        name="greeting-helper",
        skill_md=_skill_md(),
        files=[
            {"path": f"references/{index}.txt", "content": "x" * (450 * 1024)}
            for index in range(5)
        ],
    )
    with pytest.raises(HTTPException) as exc:
        await server["create_skill"](request)
    assert exc.value.status_code == 400
    assert "package is too large" in exc.value.detail
    assert not (server["_test_skills_dir"] / "greeting-helper").exists()


@pytest.mark.asyncio
async def test_create_skill_never_overwrites_an_existing_install(server):
    target = server["_test_skills_dir"] / "greeting-helper"
    target.mkdir()
    original = _skill_md()
    (target / "SKILL.md").write_text(original)
    request = server["CreateSkillRequest"](
        name="greeting-helper",
        skill_md=original.replace("stable greeting", "changed greeting"),
        files=[],
    )

    with pytest.raises(HTTPException) as exc:
        await server["create_skill"](request)

    assert exc.value.status_code == 409
    assert (target / "SKILL.md").read_text() == original


@pytest.mark.asyncio
async def test_download_archive_has_one_top_level_dir_and_skips_unsafe_files(server, tmp_path):
    request = server["CreateSkillRequest"](
        name="greeting-helper",
        skill_md=_skill_md(),
        files=[{"path": "references/greeting.txt", "content": "hello"}],
    )
    await server["create_skill"](request)
    target = server["_test_skills_dir"] / "greeting-helper"
    (target / ".cache").mkdir()
    (target / ".cache/junk.txt").write_text("junk")
    (target / ".env").write_text("TOKEN=secret")
    (target / "private.key").write_text("secret")
    (target / "install.sh").write_text("touch /tmp/should-not-run")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    (target / "linked.txt").symlink_to(outside)
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "nested.txt").write_text("outside")
    (target / "linked-dir").symlink_to(outside_dir, target_is_directory=True)
    os.mkfifo(target / "named-pipe")

    response = await server["download_skill_archive"]("greeting-helper")
    assert response.media_type == "application/zip"
    assert response.headers["content-disposition"] == 'attachment; filename="greeting-helper.zip"'

    with zipfile.ZipFile(io.BytesIO(response.body)) as archive:
        names = archive.namelist()
        assert names == [
            "greeting-helper/SKILL.md",
            "greeting-helper/references/greeting.txt",
        ]
        assert archive.read("greeting-helper/references/greeting.txt") == b"hello"
        assert all(name.startswith("greeting-helper/") for name in names)
        assert "greeting-helper/install.sh" not in names


@pytest.mark.asyncio
async def test_export_writes_the_same_clean_archive_to_workspace(server):
    request = server["CreateSkillRequest"](
        name="greeting-helper",
        skill_md=_skill_md(),
        files=[{"path": "references/greeting.txt", "content": "hello"}],
    )
    await server["create_skill"](request)

    result = await server["export_skill_archive"]("greeting-helper")
    archive_path = Path(result["path"])

    assert result["filename"] == "greeting-helper.zip"
    assert archive_path == server["SKILL_EXPORTS_DIR"] / result["filename"]
    assert result["size"] == archive_path.stat().st_size
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == [
            "greeting-helper/SKILL.md",
            "greeting-helper/references/greeting.txt",
        ]


@pytest.mark.asyncio
async def test_unchanged_skill_archive_is_byte_for_byte_deterministic(server):
    request = server["CreateSkillRequest"](
        name="greeting-helper",
        skill_md=_skill_md(),
        files=[{"path": "references/greeting.txt", "content": "hello"}],
    )
    await server["create_skill"](request)

    first = await server["download_skill_archive"]("greeting-helper")
    second = await server["download_skill_archive"]("greeting-helper")

    assert first.body == second.body


# --- MCP subprocess environment ---------------------------------------------

def test_mcp_env_denylist_covers_the_server_api_key(server):
    """SESSION_API_KEY authenticates every caller; an MCP child must not get it."""
    denylist = server["_MCP_ENV_DENYLIST"]
    assert "SESSION_API_KEY" in denylist
    # Provider keys bill to the account owner.
    assert "ANTHROPIC_AUTH_TOKEN" in denylist
    assert "OPENAI_API_KEY" in denylist
    assert "ALIBABA_CLOUD_ACCESS_KEY_SECRET" in denylist


# --- MCP SDK field-name compatibility -----------------------------------------

class _Mcp1Tool:
    """Shape of mcp<2.0: camelCase model fields."""
    name = "echo"
    description = "Echoes"
    inputSchema = {"type": "object", "properties": {"message": {"type": "string"}}}


class _Mcp2Tool:
    """Shape of mcp>=2.0: the same fields renamed to snake_case."""
    name = "echo"
    description = "Echoes"
    input_schema = {"type": "object", "properties": {"message": {"type": "string"}}}


class _Mcp1Result:
    isError = True


class _Mcp2Result:
    is_error = True


@pytest.mark.parametrize("tool", [_Mcp1Tool(), _Mcp2Tool()])
def test_tool_schema_read_from_either_sdk_generation(server, tool):
    """An empty schema means the model gets no parameters and every call fails.

    mcp 2.0 renamed Tool.inputSchema to input_schema; reading only one spelling
    silently produced {} against the other.
    """
    schema = server["_mcp_attr"](tool, "input_schema", "inputSchema", default={})
    assert schema["properties"]["message"]["type"] == "string"


@pytest.mark.parametrize("result", [_Mcp1Result(), _Mcp2Result()])
def test_is_error_read_from_either_sdk_generation(server, result):
    """Reading only isError reported mcp 2.0 failures as successes."""
    assert server["_mcp_attr"](result, "is_error", "isError", default=False) is True


def test_mcp_attr_falls_back_to_default(server):
    class Neither:
        pass

    assert server["_mcp_attr"](Neither(), "is_error", "isError", default=False) is False


def test_mcp_attr_skips_none_valued_attribute(server):
    class NoneFirst:
        input_schema = None
        inputSchema = {"type": "object"}

    assert server["_mcp_attr"](NoneFirst(), "input_schema", "inputSchema", default={}) == {"type": "object"}


# --- MCP manager surface -----------------------------------------------------

def test_endpoints_only_touch_attributes_the_manager_has(server):
    """Guards the refactor hazard that shipped a 500.

    Making sessions per-operation removed _sessions from the manager, but the
    delete endpoint still probed it — every DELETE /mcp/servers/{name} raised
    AttributeError. Any `mcp_manager.<attr>` an endpoint reaches for has to
    exist on the class.
    """
    import re

    source = ACTION_SERVER.read_text()
    manager = server["ContainerMcpManager"]
    referenced = set(re.findall(r"mcp_manager\.(_?[a-zA-Z_][a-zA-Z0-9_]*)", source))
    assert referenced, "expected the endpoints to use the manager"
    missing = [name for name in referenced if not hasattr(manager, name)]
    assert missing == [], f"endpoints reference attributes the manager lacks: {missing}"


def test_manager_keeps_transport_state_inside_owner_tasks_only(server):
    """The manager must not expose context managers across request tasks.

    Persistent sessions live inside `_owners`; each owner enters, requests,
    and exits in its own task. Raw session/context-manager maps on the manager
    would reintroduce the anyio cross-task exit failure.
    """
    manager = server["ContainerMcpManager"]()
    assert not hasattr(manager, "_sessions")
    assert not hasattr(manager, "_transports")
    assert manager._owners == {}
