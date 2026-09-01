"""Deliver OSS assets into the sandbox — and the `obx-file` tool that does it.

Attachment bytes go browser → OSS → sandbox: the desktop pulls straight from
OSS over Alibaba's network, so nothing streams through the backend or the
SSH tunnel. The puller is `obx-file`, a small system-level CLI this module
installs into the sandbox (so the agent can also run it by hand in the
terminal, grep-style):

    obx-file get <presigned-url> <dest>   # OSS → sandbox
    obx-file put <src> <presigned-url>    # sandbox → OSS (empty-CT signing)
"""
import base64
import shlex

from core.log import create_logger
from core.oss import OssClient

log = create_logger("sandbox.assets")

UPLOAD_DIR = "/workspace/uploads"

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


def _use_internal_oss(oss: OssClient) -> bool:
    """Use OSS intranet endpoints only when the desktop shares its region."""
    from core.config import get_config

    config = get_config()
    return (
        config.sandbox_provider.lower() == "wuying"
        and config.wuying_region_id == oss.region
        and oss.internal_host != oss.host
    )


async def ensure_cli(client, container_key: str) -> None:
    """Install obx-file into the sandbox (idempotent, cached per container).

    /usr/local/bin when passwordless sudo allows it, else ~/.local/bin; the
    caller runs it through `PATH="$HOME/.local/bin:$PATH"` so both work.
    """
    if container_key in _installed:
        return
    b64 = base64.b64encode(OBX_FILE_SCRIPT.encode()).decode()
    cmd = (
        'obx_file_tmp="$(mktemp "${TMPDIR:-/tmp}/obx-file.XXXXXX")" && '
        'trap \'rm -f "$obx_file_tmp"\' EXIT && '
        f"printf %s {b64} | base64 -d > \"$obx_file_tmp\" && "
        'chmod +x "$obx_file_tmp" && '
        '(sudo -n install -m755 "$obx_file_tmp" /usr/local/bin/obx-file 2>/dev/null || '
        '(mkdir -p "$HOME/.local/bin" && install -m755 "$obx_file_tmp" "$HOME/.local/bin/obx-file"))'
    )
    result = await client.execute(cmd, timeout=30)
    if result.exit_code != 0:
        raise RuntimeError(f"obx-file install failed: {result.stderr[:200]}")
    _installed.add(container_key)


async def deliver(client, container_key: str, oss: OssClient, assets: list) -> list[str]:
    """Pull each ready asset into /workspace/uploads. Returns landed paths.

    A failed download is logged and skipped — the agent still gets the other
    files plus the message text, which beats failing the whole prompt.
    """
    await ensure_cli(client, container_key)
    landed: list[str] = []
    for asset in assets:
        url = oss.presign_get(
            asset.oss_key,
            expires_sec=1800,
            internal=_use_internal_oss(oss),
        )
        dest = f"{UPLOAD_DIR}/{asset.name}"
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
    pin_part: bool = True,
) -> tuple[str, int]:
    """Push a sandbox image to OSS and pin a file part on the current message.

    Shared by view_image and the computer tool's screenshot: the model runs
    in the backend, so the only way it sees a picture that lives on the cloud
    desktop is to move it to OSS (no tunnel bytes) and hand back a presigned
    URL, which loop._to_llm_messages turns into real image content next turn.

    ``pin_part=False`` registers the asset without putting a file part on the
    message: an intermediate artefact (audio extracted for ASR, a trimmed clip)
    becomes addressable by ``asset_id`` without adding a card to the chat.

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
    await ensure_cli(ctx.sandbox, getattr(ctx.sandbox, "base_url", "") or ctx.session_id)
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

    if not pin_part:
        return asset_id, verified

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
                group_id=relation_group_id or (f"tool:{ctx.part_id}" if ctx.part_id else None),
                role=relation_role,
                kind=relation_kind,
                label=relation_label,
                caption=relation_caption,
                # Concurrent shots finish out of order, so attach order is
                # completion order. Without an explicit ordinal the renderer
                # falls back to position and labels the last shot "第 2 段".
                ordinal=relation_ordinal,
            ),
            session_id=ctx.session_id,
            message_id=ctx.message_id,
        ),
        is_new=True,
        user_id=ctx.user_id,
    )
    return asset_id, verified
