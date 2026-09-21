"""Skill first-read pointers are durable, atomic and separate from Trace."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from skill.storage import LocalSkillStore, OssSkillStore, get_blob_store


async def test_concurrent_local_first_reads_choose_one_complete_value(tmp_path):
    store = LocalSkillStore(tmp_path)
    key = "team-skills/owner/first-read/skill"
    candidates = [bytes([index]) * 65536 for index in range(12)]
    await asyncio.gather(*(store.put(key, value, content_type="application/octet-stream") for value in candidates))
    winner = await store.get(key)
    assert winner in candidates and await store.exists(key)
    await store.put(key, b"later edit", content_type="text/plain")
    assert await store.get(key) == winner
    assert not list(tmp_path.rglob(".snapshot-*"))


@pytest.mark.parametrize("key", ["../outside", "team-skills/../outside", "team-skills//empty", "team-skills/./dot", "team-skills/back\\slash"])
async def test_local_store_rejects_noncanonical_keys(tmp_path, key):
    with pytest.raises(ValueError):
        await LocalSkillStore(tmp_path).get(key)


def test_trace_configuration_does_not_select_skill_storage(tmp_path, monkeypatch):
    monkeypatch.setattr("core.config.get_config", lambda: SimpleNamespace(oss_bucket=""))
    monkeypatch.delenv("TEAM_SKILL_BLOB_PROVIDER", raising=False)
    monkeypatch.setenv("TEAM_SKILL_BLOB_LOCAL_PATH", str(tmp_path))
    monkeypatch.setenv("TRAJECTORY_BLOB_PROVIDER", "unavailable-trace-store")
    assert get_blob_store().root == tmp_path.resolve()


async def test_oss_uses_existing_client_with_conditional_writes(monkeypatch):
    oss = SimpleNamespace(put_object=AsyncMock(), get_object=AsyncMock(return_value=b"frozen"),
        head_object_info=AsyncMock(return_value={"size": 6}))
    monkeypatch.setattr("core.oss.get_oss", lambda: oss)
    store, key = OssSkillStore(), "team-skills/owner/content"
    await store.put(key, b"frozen", content_type="text/plain")
    oss.put_object.assert_awaited_once_with(key, data=b"frozen", content_type="text/plain", forbid_overwrite=True)
    assert await store.get(key) == b"frozen" and await store.exists(key)
    oss.get_object.side_effect = RuntimeError("private storage credentials")
    with pytest.raises(RuntimeError, match="storage is unavailable") as error:
        await store.get(key)
    assert "private" not in str(error.value)
