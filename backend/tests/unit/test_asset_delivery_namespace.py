"""OSS downloads land at the canonical tenant/project/asset path."""

from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

import pytest

import sandbox.assets as assets
from db.base import get_db_session
from db.models.file_asset import FileAsset
from db.models.project import Project
from db.models.session import Session
from db.models.user import User
from project.workspace import asset_sandbox_path


async def _seed_asset() -> tuple[str, str, str, str]:
    suffix = uuid.uuid4().hex[:12]
    user_id = f"asset-user-{suffix}"
    project_id = f"asset-project-{suffix}"
    session_id = f"asset-session-{suffix}"
    asset_id = f"asset-{suffix}"
    now = datetime.now(timezone.utc)
    async with get_db_session() as db:
        db.add(User(
            id=user_id,
            username=user_id,
            created_at=now,
            updated_at=now,
        ))
        db.add(Project(
            id=project_id,
            user_id=user_id,
            name="Assets",
            slug=f"assets-{suffix}",
            created_at=now,
            updated_at=now,
        ))
        db.add(Session(
            id=session_id,
            user_id=user_id,
            project_id=project_id,
            status="idle",
            created_at=now,
            updated_at=now,
        ))
        db.add(FileAsset(
            id=asset_id,
            user_id=user_id,
            name="report.pdf",
            oss_key=f"objects/{asset_id}",
            mime="application/pdf",
            size=10,
            status="ready",
            source="user",
            transient=False,
            is_deleted=False,
            created_at=now,
        ))
    return user_id, project_id, session_id, asset_id


@pytest.mark.asyncio
async def test_deliver_uses_namespaced_destination(monkeypatch):
    commands: list[str] = []

    class Client:
        async def execute(self, command, timeout=None):
            commands.append(command)
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

    class Oss:
        host = "oss.example"
        internal_host = "oss.example"

        def presign_get(self, key, expires_sec, internal):
            assert key == "objects/report"
            return "https://oss.example/signed"

    async def cli_ready(_client, _key):
        return None

    monkeypatch.setattr(assets, "ensure_cli", cli_ready)
    item = SimpleNamespace(
        id="asset-1",
        name="report.pdf",
        oss_key="objects/report",
    )

    landed = await assets.deliver(
        Client(),
        "desktop-1",
        Oss(),
        [item],
        user_id="alice",
        project_id="project-1",
    )

    expected = asset_sandbox_path(
        "alice",
        "project-1",
        "report.pdf",
        asset_id="asset-1",
    )
    assert landed == [expected]
    assert expected in commands[0]
    assert "/workspace/uploads" not in commands[0]


@pytest.mark.asyncio
async def test_strict_delivery_retries_until_every_expected_path_lands(monkeypatch):
    user_id, project_id, session_id, asset_id = await _seed_asset()
    expected_path = asset_sandbox_path(
        user_id,
        project_id,
        "report.pdf",
        asset_id=asset_id,
    )
    attempts: list[list[str]] = []

    async def fake_client(_session_id, *, user_id):
        return SimpleNamespace(user_id=user_id)

    async def fake_deliver(_client, _key, _oss, rows, **_kwargs):
        attempts.append([row.id for row in rows])
        return [] if len(attempts) == 1 else [expected_path]

    async def no_sleep(_delay):
        return None

    import core.oss as oss_module
    import sandbox.manager as manager_module

    monkeypatch.setattr(manager_module.sandbox_manager, "get_client", fake_client)
    monkeypatch.setattr(assets, "deliver", fake_deliver)
    monkeypatch.setattr(assets.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(oss_module, "get_oss", lambda: object())

    landed = await assets.deliver_asset_ids(
        session_id,
        user_id,
        [asset_id],
        strict=True,
        expected_asset_ids=[asset_id],
        max_attempts=2,
    )
    assert landed == [expected_path]
    assert attempts == [[asset_id], [asset_id]]


@pytest.mark.asyncio
async def test_strict_delivery_rejects_missing_durable_asset_without_partial_start():
    user_id, _project_id, session_id, asset_id = await _seed_asset()
    missing_id = f"missing-{asset_id}"
    with pytest.raises(assets.AssetDeliveryError) as caught:
        await assets.deliver_asset_ids(
            session_id,
            user_id,
            [asset_id, missing_id],
            strict=True,
            expected_asset_ids=[asset_id, missing_id],
        )
    assert caught.value.missing_asset_ids == (missing_id,)
    assert caught.value.code == "asset_unavailable"
    assert caught.value.retryable is False
