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
import io
import tarfile
from pathlib import Path

import pytest
from fastapi import HTTPException

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
    src = src.replace('Path("/data/.manifest.json")', f'Path(r"{tmp_path}/data/.manifest.json")')
    src = src.replace('if __name__ == "__main__":', "if False:")

    ns = {"__name__": "action_server_under_test"}
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
