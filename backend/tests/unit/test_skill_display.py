"""Localized UI copy survives installs without becoming a callable identity."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic import ValidationError

from api.metadata import InstallSkillBody, install_skill, upload_skill_archive
from skill.builtin import builtin_skills
from skill.display import display_fields, with_package_display
from skill.sandbox_display import enrich_install_display, save_install_display

COPY = {
    "display_name": {"zh-CN": "报告撰写", "en-US": "Report writing"},
    "display_description": {"zh-CN": "整理资料并撰写报告。", "en-US": "Organize research and write a report."},
}


def row(name="report", install_dir="report"):
    return {"name": name, "install_dir": install_dir, "source": "container",
            "description": "ORIGINAL DISCOVERY", "content": f"---\nname: {name}\ndescription: ORIGINAL DISCOVERY\n---\nORIGINAL INSTRUCTIONS",
            "base_dir": f"/data/skills/{install_dir}"}


def client(info=None):
    info = info or row()
    return SimpleNamespace(user_scope="u-" + "a" * 20,
        _get=AsyncMock(return_value=info), get_skill=AsyncMock(return_value=info),
        read_file_raw=AsyncMock(return_value=json.dumps(COPY)),
        write_file=AsyncMock(), _invalidate_catalogue_cache=Mock())


def test_every_builtin_has_both_languages_and_separate_identity():
    for spec in builtin_skills():
        assert spec.display_name.keys() == {"zh-CN", "en-US"}
        assert spec.display_description.keys() == {"zh-CN", "en-US"}
        assert spec.name not in spec.display_name.values()
        assert spec.name == spec.directory.name


@pytest.mark.parametrize("fields", [
    {"display_name": {"zh-CN": "x" * 121}},
    {"display_description": {"en-US": "x" * 1001}},
    {"display_name": {"fr": "Bonjour"}},
    {"display_name": "不能替换标识"},
    {"display_description": {"en-US": ["bad"]}},
])
def test_invalid_display_input_is_rejected(fields):
    with pytest.raises(ValidationError):
        InstallSkillBody(content="text", **fields)
    assert display_fields(fields) == {}


def test_one_language_and_empty_fields_are_backward_compatible():
    body = InstallSkillBody(name="report", content="body", display_name={"zh-CN": " 报告 ", "en-US": " "})
    assert display_fields(body.model_dump()) == {"display_name": {"zh-CN": "报告"}}
    assert display_fields(InstallSkillBody(content="body").model_dump()) == {}
    assert with_package_display({"display_name": {"en-US": "Original"}},
        {"display_name": {"zh-CN": "报告"}}, single=True)["display_name"] == {"zh-CN": "报告", "en-US": "Original"}


async def test_legacy_server_persists_sidecar_and_reload_uses_it_without_changing_calls():
    info = row()
    sandbox = client(info)
    installed = await save_install_display(sandbox, info, COPY)
    path, content = sandbox.write_file.await_args.args
    assert path == "/data/skills/report/openbox-display.json"
    assert json.loads(content) == COPY
    sandbox._invalidate_catalogue_cache.assert_called_once()
    assert installed["name"] == "report" and installed["content"] == info["content"]
    sandbox.read_file_raw.reset_mock()
    result = (await enrich_install_display(sandbox, [info]))[0]
    assert result["display_name"] == COPY["display_name"]
    assert result["description"] == "ORIGINAL DISCOVERY"
    assert result["content"].endswith("ORIGINAL INSTRUCTIONS")
    await enrich_install_display(sandbox, [info])
    sandbox.read_file_raw.assert_awaited_once_with("/data/skills/report/openbox-display.json", max_bytes=16_384)


async def test_package_label_does_not_rename_member_skills():
    rows = [row("research", "report-pack"), row("writer", "report-pack")]
    sandbox = client()
    result = await enrich_install_display(sandbox, rows)
    assert [entry["name"] for entry in result] == ["research", "writer"]
    assert all("display_name" not in entry for entry in result)
    assert all(entry["package_display_name"] == COPY["display_name"] for entry in result)


@pytest.mark.parametrize("info", [
    {**row(), "source": "builtin", "base_dir": "/opt/openbox/skills/report"},
    {**row(), "install_dir": "../outside"},
    {**row(), "base_dir": "/data/skills/u-" + "b" * 20 + "/report"},
])
async def test_display_write_stays_in_this_install(info):
    sandbox = client(info)
    with pytest.raises(ValueError):
        await save_install_display(sandbox, info, COPY)
    sandbox.write_file.assert_not_awaited()


async def test_new_server_requires_no_legacy_queries():
    sandbox = client()
    current = {**row(), **COPY, "display_metadata_version": 1}
    assert await enrich_install_display(sandbox, [current]) == [current]
    sandbox._get.assert_not_awaited()
    sandbox.read_file_raw.assert_not_awaited()


async def test_legacy_sidecar_read_is_bounded_before_loading_content():
    from sandbox.client import SandboxClient
    sandbox = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(exit_code=0, stdout="{}")))
    assert await SandboxClient.read_file_raw(sandbox, "/data/skills/report/openbox-display.json", max_bytes=16_384) == "{}"
    sandbox.execute.assert_awaited_once_with("head -c 16385 -- /data/skills/report/openbox-display.json", timeout=10)
    sandbox.execute.return_value.stdout = "中" * 6_000
    with pytest.raises(ValueError, match="exceeds"):
        await SandboxClient.read_file_raw(sandbox, "/data/skills/report/openbox-display.json", max_bytes=16_384)


async def test_install_api_keeps_slug_and_content_and_persists_display(monkeypatch):
    sandbox = client()
    sandbox.install_skill = AsyncMock(return_value=row())
    monkeypatch.setattr("sandbox.manager.sandbox_manager.get_client_any", AsyncMock(return_value=sandbox))
    body = InstallSkillBody(name="report", content="EXACT ORIGINAL", **COPY)
    result = await install_skill(body, current_user={"user_id": "person"})
    sandbox.install_skill.assert_awaited_once_with(url=None, name="report", content="EXACT ORIGINAL")
    assert result["name"] == "report" and result["display_name"] == COPY["display_name"]


async def test_upload_api_validates_before_install_and_keeps_archive_bytes(monkeypatch):
    from fastapi import HTTPException
    sandbox = client()
    sandbox.upload_skill_archive = AsyncMock(return_value={"name": "report", "install_dir": "report"})
    manager = AsyncMock(return_value=sandbox)
    monkeypatch.setattr("sandbox.manager.sandbox_manager.get_client_any", manager)
    file = SimpleNamespace(filename="report.zip", read=AsyncMock(return_value=b"EXACT ARCHIVE"))
    with pytest.raises(HTTPException) as error:
        await upload_skill_archive(file=file, name="report", current_user={"user_id": "person"}, display_name="not json")
    assert error.value.status_code == 422
    manager.assert_not_awaited()
    await upload_skill_archive(file=file, name="report", current_user={"user_id": "person"},
        display_name=json.dumps(COPY["display_name"]), display_description=json.dumps(COPY["display_description"]))
    sandbox.upload_skill_archive.assert_awaited_once_with(b"EXACT ARCHIVE", "report.zip", "report")
    assert sandbox.write_file.await_count == 1
