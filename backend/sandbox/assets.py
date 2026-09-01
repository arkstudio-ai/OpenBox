"""Deliver OSS assets into the sandbox — and the `obx-file` tool that does it.

Attachment bytes go browser → OSS → sandbox: the desktop pulls straight from
OSS over Alibaba's network, so nothing streams through the backend or the
SSH tunnel. The puller is `obx-file`, a small system-level CLI this module
installs into the sandbox (so the agent can also run it by hand in the
terminal, grep-style):

    obx-file get <presigned-url> <dest>   # OSS → sandbox
    obx-file put <src> <presigned-url>    # sandbox → OSS (empty-CT signing)
"""

import asyncio
import base64
import shlex
from typing import Sequence

from core.log import create_logger
from core.oss import OssClient

log = create_logger("sandbox.assets")

OBX_FILE_SCRIPT = """#!/bin/sh
# obx-file - move files between OSS and this sandbox via presigned URLs.
#   obx-file get <url> <dest>
#   obx-file put <src> <url> [content-type]
# The content-type must match what the URL was signed with: empty when
# omitted, exactly the given value otherwise.
set -e
case "$1" in
  get)
    [ -n "$2" ] && [ -n "$3" ] || { echo "usage: obx-file get <url> <dest>" >&2; exit 2; }
    mkdir -p "$(dirname "$3")"
    curl -fsSL --retry 3 --retry-delay 1 -o "$3" "$2"
    ;;
  put)
    [ -f "$2" ] || { echo "no such file: $2" >&2; exit 2; }
    [ -n "$3" ] || { echo "usage: obx-file put <src> <url> [content-type]" >&2; exit 2; }
    curl -fsSL --retry 3 --retry-delay 1 -X PUT -H "Content-Type:${4:+ $4}" --upload-file "$2" "$3"
    ;;
  *)
    echo "usage: obx-file get <url> <dest> | obx-file put <src> <url> [content-type]" >&2
    exit 2
    ;;
esac
"""

#: Container ids that already have the tool this process lifetime.
_installed: set[str] = set()


class AssetDeliveryError(RuntimeError):
    """Strict delivery could not land every expected durable asset."""

    def __init__(
        self,
        *,
        expected_asset_ids: Sequence[str],
        missing_asset_ids: Sequence[str],
        code: str = "delivery_failed",
        retryable: bool = True,
    ):
        self.expected_asset_ids = tuple(expected_asset_ids)
        self.missing_asset_ids = tuple(missing_asset_ids)
        self.code = code
        self.retryable = retryable
        super().__init__(
            "attachment delivery incomplete; missing="
            + ",".join(self.missing_asset_ids)
        )


def _use_internal_oss(oss: OssClient) -> bool:
    """Use OSS intranet only when the desktop and bucket share a region.

    Alibaba's ``*-internal`` endpoints are regional.  A Shanghai WUYING
    desktop cannot reach a Hangzhou bucket through the Hangzhou intranet
    hostname; selecting it merely because both services are Alibaba leaves an
    otherwise valid asset in OSS without its workspace copy.
    """
    from core.config import get_config

    config = get_config()
    desktop_region = str(getattr(config, "wuying_region_id", "") or "").strip().lower()
    bucket_region = str(getattr(oss, "region", "") or "").strip().lower()
    return (
        config.sandbox_provider.lower() == "wuying"
        and bool(desktop_region)
        and desktop_region == bucket_region
        and oss.internal_host != oss.host
    )


async def ensure_cli(client, container_key: str) -> None:
    """Install obx-file into the sandbox (idempotent, cached per container).

    Production WUYING deploys install the helper system-wide. Development
    sandboxes fall back to the runner's own bin directory without sudo or a
    predictable shared /tmp filename.
    """
    if container_key in _installed:
        return
    b64 = base64.b64encode(OBX_FILE_SCRIPT.encode()).decode()
    cmd = f"""set -e
if ! command -v obx-file >/dev/null 2>&1; then
  install -d -m 0750 "$HOME/.local/bin"
  tmp=$(mktemp "${{TMPDIR:-/tmp}}/.openbox-obx-file.XXXXXX")
  trap 'rm -f "$tmp"' EXIT HUP INT TERM
  printf %s {b64} | base64 -d > "$tmp"
  chmod 0755 "$tmp"
  install -m 0755 "$tmp" "$HOME/.local/bin/obx-file"
fi
"""
    result = await client.execute(cmd, timeout=30)
    if result.exit_code != 0:
        raise RuntimeError(f"obx-file install failed: {result.stderr[:200]}")
    _installed.add(container_key)


def asset_cli_provision_script() -> str:
    """Return the root-only WUYING installer for the OSS transfer helper."""
    payload = base64.b64encode(OBX_FILE_SCRIPT.encode()).decode()
    return f"""set -e
tmp=$(mktemp /usr/local/bin/.obx-file.XXXXXX)
trap 'rm -f "$tmp"' EXIT HUP INT TERM
printf %s {payload} | base64 -d > "$tmp"
chmod 0755 "$tmp"
chown root:root "$tmp"
mv -f "$tmp" /usr/local/bin/obx-file
command -v obx-file >/dev/null
"""


async def deliver(
    client,
    container_key: str,
    oss: OssClient,
    assets: list,
    *,
    user_id: str,
    project_id: str,
) -> list[str]:
    """Pull each ready asset into its tenant/project namespace.

    A failed download is logged and skipped — the agent still gets the other
    files plus the message text, which beats failing the whole prompt.
    """
    await ensure_cli(client, container_key)
    from project.workspace import asset_sandbox_path

    landed: list[str] = []
    for asset in assets:
        url = oss.presign_get(
            asset.oss_key,
            expires_sec=1800,
            internal=_use_internal_oss(oss),
        )
        dest = asset_sandbox_path(
            user_id,
            project_id,
            asset.name,
            asset_id=asset.id,
        )
        cmd = f'PATH="$HOME/.local/bin:$PATH" obx-file get {shlex.quote(url)} {shlex.quote(dest)}'
        try:
            result = await client.execute(cmd, timeout=120)
            if result.exit_code == 0:
                landed.append(dest)
            else:
                log.warning(f"Asset {asset.id} download failed: {result.stderr[:200]}")
        except Exception as e:
            log.warning(f"Asset {asset.id} download failed: {e}")
    return landed


async def deliver_asset_ids(
    session_id: str,
    user_id: str,
    asset_ids: list[str],
    *,
    strict: bool = False,
    expected_asset_ids: Sequence[str] | None = None,
    max_attempts: int = 3,
) -> list[str]:
    """Resolve owned durable asset ids and deliver them to this Session's sandbox.

    Existing callers keep best-effort partial semantics. Inbox/recovery callers
    opt into ``strict`` and provide the exact durable ids their claimed Parts
    reference; those calls retry a bounded number of times and fail closed
    until every expected path has landed.
    """
    if not asset_ids:
        if strict and expected_asset_ids:
            raise AssetDeliveryError(
                expected_asset_ids=expected_asset_ids,
                missing_asset_ids=expected_asset_ids,
                code="asset_unavailable",
                retryable=False,
            )
        return []
    requested = list(dict.fromkeys(asset_ids))
    expected = list(
        dict.fromkeys(requested if expected_asset_ids is None else expected_asset_ids)
    )
    if strict:
        if set(expected) != set(requested):
            raise ValueError("strict attachment expectation must match requested ids")
        if max_attempts < 1 or max_attempts > 5:
            raise ValueError("strict attachment attempts must be between 1 and 5")
    from sqlalchemy import select

    from core.oss import get_oss
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from sandbox.manager import sandbox_manager

    async with get_db_session() as db:
        rows = list(
            (
                await db.execute(
                    select(FileAsset).where(
                        FileAsset.id.in_(asset_ids),
                        FileAsset.user_id == user_id,
                        FileAsset.status == "ready",
                        FileAsset.is_deleted.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )
    by_id = {row.id: row for row in rows}
    ordered = [by_id[asset_id] for asset_id in requested if asset_id in by_id]
    if strict:
        missing_rows = [asset_id for asset_id in expected if asset_id not in by_id]
        if missing_rows:
            raise AssetDeliveryError(
                expected_asset_ids=expected,
                missing_asset_ids=missing_rows,
                code="asset_unavailable",
                retryable=False,
            )
    if not ordered:
        return []
    from session.session import workspace_identity_for

    owner_id, project_id = await workspace_identity_for(session_id)
    if owner_id != user_id:
        raise PermissionError("Attachment session ownership mismatch")
    client = await sandbox_manager.get_client(session_id, user_id=user_id)
    if not strict:
        return await deliver(
            client,
            f"{user_id}:{session_id}",
            get_oss(),
            ordered,
            user_id=user_id,
            project_id=project_id,
        )

    from project.workspace import asset_sandbox_path

    expected_paths = {
        asset.id: asset_sandbox_path(
            user_id,
            project_id,
            asset.name,
            asset_id=asset.id,
        )
        for asset in ordered
    }
    landed: set[str] = set()
    remaining = list(ordered)
    for attempt in range(max_attempts):
        try:
            landed.update(
                await deliver(
                    client,
                    f"{user_id}:{session_id}",
                    get_oss(),
                    remaining,
                    user_id=user_id,
                    project_id=project_id,
                )
            )
        except Exception:
            log.exception(
                "Strict asset delivery attempt failed session=%s attempt=%s",
                session_id,
                attempt + 1,
            )
        remaining = [
            asset for asset in remaining if expected_paths[asset.id] not in landed
        ]
        if not remaining:
            return [expected_paths[asset_id] for asset_id in expected]
        if attempt + 1 < max_attempts:
            await asyncio.sleep(min(0.1 * (2**attempt), 0.5))
    raise AssetDeliveryError(
        expected_asset_ids=expected,
        missing_asset_ids=[asset.id for asset in remaining],
        code="delivery_failed",
        retryable=True,
    )


async def _session_project(db, session_id: str | None, user_id: str) -> str | None:
    """The project a conversation belongs to, or None when it has no session."""
    from sqlalchemy import select

    from db.models.session import Session as SessionRow

    if not session_id:
        return None
    return (
        await db.execute(
            select(SessionRow.project_id).where(
                SessionRow.id == session_id, SessionRow.user_id == user_id
            )
        )
    ).scalar_one_or_none()


async def attach_sandbox_image(
    ctx,
    path: str,
    mime: str,
    size: int,
    name: str | None = None,
    transient: bool = False,
    *,
    relation_kind: str = "file",
    relation_role: str = "result",
    relation_group_id: str | None = None,
    relation_label: str | None = None,
    relation_caption: str | None = None,
) -> tuple[str, int]:
    """Push a sandbox image to OSS and pin a file part on the current message.

    Shared by view_image and the computer tool's screenshot: the model runs
    in the backend, so the only way it sees a picture that lives on the cloud
    desktop is to move it to OSS (no tunnel bytes) and hand back a presigned
    URL, which loop._to_llm_messages turns into real image content next turn.

    Returns (asset_id, verified_size). Raises RuntimeError on any failure —
    callers translate that into a ToolResult error.
    """
    from datetime import datetime, timezone
    from core.identifier import ascending
    from core.oss import get_oss
    from db.base import get_db_session
    from db.models.file_asset import FileAsset
    from models.message import FilePart, FileRelation
    from session.session import save_part

    oss = get_oss()
    obj_name = name or path.split("/")[-1]
    asset_id = ascending("asset")
    key = f"assets/{ctx.user_id}/{asset_id}/{obj_name}"

    # Keyed by the machine, not the conversation: obx-file is a property of
    # the container, so a new chat must not reinstall it.
    await ensure_cli(
        ctx.sandbox, getattr(ctx.sandbox, "base_url", "") or ctx.session_id
    )
    put_url = oss.presign_put(
        key,
        mime,
        expires_sec=600,
        internal=_use_internal_oss(oss),
    )
    push = await ctx.sandbox.execute(
        f'PATH="$HOME/.local/bin:$PATH" obx-file put {shlex.quote(path)} {shlex.quote(put_url)} {shlex.quote(mime)}',
        timeout=120,
    )
    if push.exit_code != 0:
        raise RuntimeError(push.stderr.strip()[:300] or "obx-file put failed")
    head = await oss.head(key)
    if not head:
        raise RuntimeError("Object missing in OSS after upload")
    verified = head["size"] or size

    async with get_db_session() as db:
        db.add(
            FileAsset(
                id=asset_id,
                user_id=ctx.user_id,
                session_id=ctx.session_id,
                # The resource centre files agent output under the same project
                # the conversation runs in.
                project_id=await _session_project(db, ctx.session_id, ctx.user_id),
                name=obj_name,
                oss_key=key,
                mime=mime,
                size=verified,
                status="ready",
                source="agent",
                transient=transient,
                created_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()

    await save_part(
        FilePart(
            id=ascending("part"),
            path=path,
            mime_type=mime,
            asset_id=asset_id,
            oss_key=key,
            size=verified,
            transient=transient,
            relation=FileRelation(
                source_part_id=ctx.part_id or None,
                group_id=relation_group_id
                or (f"tool:{ctx.part_id}" if ctx.part_id else None),
                role=relation_role,
                kind=relation_kind,
                label=relation_label,
                caption=relation_caption,
            ),
            session_id=ctx.session_id,
            message_id=ctx.message_id,
        ),
        is_new=True,
        user_id=ctx.user_id,
        run_fence=ctx.run_fence,
    )
    return asset_id, verified
