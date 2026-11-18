"""Keep an Agent edit on the guided mobile form of this very conversation."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from light import production
from light.mode import LightError
from light.production_form import FormFields
from tool.tool import ToolContext, ToolResult, define_tool


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["update", "propose"] = "update"
    revision: int = Field(ge=1)
    changes: FormFields
    summary: str = Field(default="", max_length=300)


async def execute(args: Args, ctx: ToolContext) -> ToolResult:
    changes = args.changes.model_dump(exclude_none=True)
    if not changes or (args.action == "propose" and not args.summary.strip()):
        return ToolResult(title="Draft unchanged", output="Provide the changed fields and a summary for a proposal.")
    try:
        revision = await production.update_from_agent(
            ctx, revision=args.revision, changes=changes, proposal=args.summary.strip() if args.action == "propose" else None,
        )
    except LightError as exc:
        return ToolResult(title="Draft unchanged", output=str(exc), metadata={"blocked": True, "code": exc.code})
    return ToolResult(title="Draft saved", output=f"Saved {args.action} to the current form, revision {revision}.",
                      metadata={"revision": revision, "action": args.action})


light_production_form_tool = define_tool(
    "light_production_form", description=(
        "Save editable copy/dish rows or a requested change to the current production form. Use action=propose "
        "with a user-facing summary for an unapproved proposal. Provide the exact current form revision. "
        "Only include changed fields. This tool changes the draft, and never generates or publishes media."
    ), parameters=Args, execute=execute, sandbox_required=False, parallel_safe=False,
)
