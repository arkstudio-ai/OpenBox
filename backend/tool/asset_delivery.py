"""Hand an owned OSS asset back to the user as a card on the current reply.

Generated videos already live in OSS and are attached to the reply that saw
them finish. When the user later asks "send me the video again", the model
used to paste the 24-hour ``download_url`` token into Markdown — a link the
mobile app cannot open and that is dead by the next day — or call
``share_file``, which needs a sandbox the user may not be entitled to. This
helper re-attaches the asset as a normal file part: no sandbox, no token, and
the chat renders the same playable card with its own download button.
"""
from __future__ import annotations

from sqlalchemy import select

from agent.driver import LeaseLostError
from core.log import create_logger
from question.runtime import RunRevoked
from tool.tool import ToolContext, ToolResult

log = create_logger("tool.asset_delivery")


async def find_owned_ready_asset(asset_id: str, ctx: ToolContext):
    from db.base import get_db_session
    from db.models.file_asset import FileAsset

    value = (asset_id or "").strip()
    if value.startswith("asset:"):
        value = value[6:]
    if not value:
        return None
    async with get_db_session() as db:
        return (
            await db.execute(
                select(FileAsset).where(
                    FileAsset.id == value,
                    FileAsset.user_id == ctx.user_id,
                    FileAsset.is_deleted.is_(False),
                    FileAsset.status == "ready",
                )
            )
        ).scalar_one_or_none()


async def attach_owned_asset(
    asset,
    ctx: ToolContext,
    *,
    kind: str,
    role: str = "final",
    label: str | None = None,
    metadata: dict | None = None,
) -> ToolResult:
    """Attach ``asset`` to the reply being written. Needs no sandbox."""
    from models.message import FilePart, FileRelation
    from session.session import save_part

    if not ctx.message_id:
        return ToolResult(
            title="Nothing to attach to",
            output="This call has no reply to attach the asset to.",
            metadata={"error": True},
        )
    name = asset.name
    try:
        await save_part(
            FilePart(
                path=f"/workspace/generated_videos/{name}",
                mime_type=asset.mime,
                asset_id=asset.id,
                oss_key=asset.oss_key,
                size=asset.size,
                transient=False,
                relation=FileRelation(
                    source_part_id=ctx.part_id or None,
                    group_id=f"asset:{asset.id}:delivery:{ctx.message_id}",
                    role=role,
                    kind=kind,
                    label=label or name,
                    metadata={"asset_id": asset.id, "redelivery": True, **(metadata or {})},
                ),
                session_id=ctx.session_id,
                message_id=ctx.message_id,
            ),
            is_new=True,
            user_id=ctx.user_id,
            run_fence=ctx.run_fence,
        )
    except (RunRevoked, LeaseLostError):
        raise
    except Exception as exc:
        log.warning("asset re-attachment failed", exc_info=True)
        return ToolResult(
            title=f"Could not attach {name}",
            output=f"{type(exc).__name__}: {str(exc)[:300]}",
            metadata={"error": True, "asset_id": asset.id},
        )
    return ToolResult(
        title=name,
        output=(
            f"attached=true\nasset_id={asset.id}\nname={name}\nmime={asset.mime}\nbytes={asset.size}\n"
            "The file is now a card on this reply; the user downloads it from the card. "
            "Do not write a link or URL for it."
        ),
        metadata={"asset_id": asset.id, "mime": asset.mime, "size": asset.size, "attached": True},
    )
