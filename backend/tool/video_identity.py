"""Control surface for verified real-person references."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from tool.tool import ToolContext, ToolResult, define_tool
from video.materials import (
    MaterialProviderError,
    create_liveness_session,
    ensure_material_asset,
    get_identity,
    list_identities,
    list_identity_assets,
    refresh_liveness_session,
)


class VideoIdentityArgs(BaseModel):
    action: Literal["create", "status", "list", "add_asset"]
    identity_id: str | None = Field(default=None, max_length=96)
    label: str = Field(default="真人主持人", min_length=1, max_length=120)
    asset_id: str | None = Field(default=None, max_length=96)

    @model_validator(mode="after")
    def _required_by_action(self):
        if self.action in {"status", "add_asset"} and not self.identity_id:
            raise ValueError(f"{self.action} requires identity_id")
        if self.action == "add_asset" and not self.asset_id:
            raise ValueError("add_asset requires an owned ready image asset_id")
        return self


def _identity_output(identity: dict) -> str:
    lines = [
        f"identity_id={identity['identity_id']}",
        f"label={identity['label']}",
        f"status={identity['status']}",
    ]
    if identity.get("provider_group_id"):
        lines.append(f"provider_group_id={identity['provider_group_id']}")
    if identity["status"] == "awaiting_user":
        lines.extend(
            [
                f"expires_at={identity.get('expires_at') or ''}",
                "instruction=The authorization card is visible to the user. Ask them to complete it, stop, and call video_identity.status only after they say it is done.",
            ]
        )
    elif identity["status"] == "active":
        lines.append(
            "instruction=The verified-person group is active. Add the exact owned portrait with video_identity.add_asset before planning real-person segments."
        )
    elif identity["status"] in {"expired", "failed"}:
        lines.append("instruction=Create a new authorization session before using this real person.")
    if identity.get("error"):
        lines.append(f"error={identity['error']}")
    return "\n".join(lines)


async def execute_video_identity(args: VideoIdentityArgs, ctx: ToolContext) -> ToolResult:
    try:
        if args.action == "create":
            identity = await create_liveness_session(ctx.user_id, args.label)
            return ToolResult(
                title="真人授权",
                output=_identity_output(identity),
                metadata={"action": args.action, "identity": identity},
            )
        if args.action == "status":
            identity = await refresh_liveness_session(ctx.user_id, args.identity_id or "")
            return ToolResult(
                title="真人授权状态",
                output=_identity_output(identity),
                metadata={"action": args.action, "identity": identity},
            )
        if args.action == "add_asset":
            identity = await get_identity(ctx.user_id, args.identity_id or "")
            if not identity:
                raise RuntimeError("真人身份不存在或不属于当前用户")
            material = await ensure_material_asset(
                ctx.user_id,
                args.asset_id or "",
                identity_id=args.identity_id,
            )
            output = "\n".join(
                [
                    f"identity_id={args.identity_id}",
                    f"source_asset_id={material['source_asset_id']}",
                    f"material_asset_id={material['material_asset_id']}",
                    f"status={material['status']}",
                    "next_action=Use video_project.set_segments with character_reference_type=real_person and this identity_id.",
                ]
            )
            return ToolResult(
                title="真人素材已入库",
                output=output,
                metadata={"action": args.action, "identity": identity, "material_asset": material},
            )

        identities = await list_identities(ctx.user_id)
        enriched = []
        for identity in identities:
            item = dict(identity)
            item["assets"] = await list_identity_assets(ctx.user_id, identity["identity_id"])
            enriched.append(item)
        return ToolResult(
            title="真人素材库",
            output=json.dumps(
                [
                    {
                        "identity_id": item["identity_id"],
                        "label": item["label"],
                        "status": item["status"],
                        "asset_ids": [asset["source_asset_id"] for asset in item["assets"]],
                    }
                    for item in enriched
                ],
                ensure_ascii=False,
            ),
            metadata={"action": args.action, "identities": enriched},
        )
    except MaterialProviderError as exc:
        outcome_unknown = exc.code == "material_outcome_unknown"
        return ToolResult(
            title="真人素材操作失败",
            output=(
                str(exc)[:1000]
                if getattr(exc, "public_message", False)
                else "TokenSpace 素材服务请求失败；详细供应商响应未向会话公开。"
            ),
            metadata={
                "action": args.action,
                "failed": True,
                "error": True,
                "failure_code": exc.code,
                "retryable": exc.retryable,
                **(
                    {
                        "outcome_unknown": True,
                        "manual_review": True,
                        "do_not_retry": True,
                    }
                    if outcome_unknown
                    else {}
                ),
            },
        )
    except Exception as exc:
        return ToolResult(
            title="真人素材操作失败",
            output=(str(exc) or exc.__class__.__name__)[:1000],
            metadata={
                "action": args.action,
                "failed": True,
                "error": True,
                "failure_code": "video_identity_failed",
            },
        )


VIDEO_IDENTITY_DESCRIPTION = """\
Create or inspect H5 consent for a recognizable real person, then attach their
owned portrait to the active identity. Never use this for a virtual character.
Wait for reported completion and verify active status before continuing."""


video_identity_tool = define_tool(
    "video_identity",
    description=VIDEO_IDENTITY_DESCRIPTION,
    parameters=VideoIdentityArgs,
    execute=execute_video_identity,
    sandbox_required=False,
    parallel_safe=False,
)
