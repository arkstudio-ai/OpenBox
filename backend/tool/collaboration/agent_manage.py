"""Build-root proposals reuse the durable question lifecycle and catalog."""
import json
from typing import Literal

from pydantic import Field

from agent_catalog import repository
from agent_catalog.schemas import AgentSpec, Contract
from agent_catalog.validation import agent_summary
from agent_catalog.chat_creation import check_limits, create_batch, require_skill_discovery
from core.config import get_config
from db.base import get_db_session
from db.models.session import Session
from team.errors import TeamError
from team.journal import Actor
from tool.collaboration.team_tools import protected, result
from tool.tool import ToolResult, define_tool


class AgentManageArgs(Contract):
    action: Literal["list", "get", "create", "update"]
    query: str = Field(default="", max_length=200)
    definition_id: str | None = None
    expected_revision: int | None = Field(default=None, ge=1)
    spec: AgentSpec | None = None
    specs: list[AgentSpec] | None = Field(default=None, min_length=1, max_length=4,
        description="For create only, submit one to four complete definitions. Use either spec or specs, not both.")


@protected
async def execute(args: AgentManageArgs, ctx):
    if not get_config().team_ui_enabled:
        raise TeamError("AGENT_CATALOG_DISABLED", "The Agent library is disabled.", status=403)
    async with get_db_session() as db:
        session = await db.get(Session, ctx.session_id)
        if session is None or session.user_id != ctx.user_id or session.workspace_id != ctx.workspace_id or session.parent_id or session.kind != "normal" or session.agent != "build":
            raise TeamError("AUTHORITY_REVOKED", "Only the interactive build root can manage reusable Agents.", status=403)
    actor = Actor(ctx.user_id, ctx.workspace_id)
    if args.action == "list":
        from agent_catalog.catalog import model_entries
        catalog = await repository.list_definitions("agent", actor, search=args.query)
        return result({**catalog, "models": model_entries(get_config())})
    if args.action == "get":
        if not args.definition_id:
            raise TeamError("INVALID_DEFINITION", "get requires definition_id.", status=422)
        return result(await repository.get("agent", args.definition_id, actor))
    if bool(args.spec) == bool(args.specs) or (args.action == "update" and args.specs):
        raise TeamError("INVALID_DEFINITION", "Provide spec for update; create accepts either spec or one to four specs.", status=422)
    specs = args.specs or [args.spec]
    if len({spec.name.casefold() for spec in specs}) != len(specs):
        raise TeamError("DEFINITION_NAME_TAKEN", "Use different names for the proposed Agents.", status=422)
    await require_skill_discovery(ctx)
    async with get_db_session() as db:
        await check_limits(db, ctx, specs)
    # Compile every definition before saving any of them or interrupting the
    # user. This also freezes each selected Skill through the form boundary.
    summaries = [await agent_summary(spec, actor, strict=True, sandbox=ctx.sandbox) for spec in specs]
    await ctx.assert_run_current()
    previous = None
    if args.action == "create":
        definitions, auto = await create_batch(actor, ctx, specs, summaries)
        if auto:
            saved = [{"definition_id": item["id"], "name": item["name"], "version_id": item["version"]["id"],
                      "revision": item["revision"]} for item in definitions]
            return ToolResult(title="Agents enabled", output=json.dumps({"definitions": definitions,
                "auto_approved": True, "message": "Created and enabled under the user's T0 preference. The user can undo publication in this conversation."}, ensure_ascii=False),
                metadata={"agent_autoapproved": saved})
    else:
        if not args.definition_id or args.expected_revision is None:
            raise TeamError("INVALID_DEFINITION", "update requires definition_id and expected_revision from get.", status=422)
        previous = (await repository.get("agent", args.definition_id, actor))["version"]["spec"]
        definitions = [await repository.mutate("agent", args.definition_id, actor, f"chat:{ctx.part_id}", "save_draft",
            args.expected_revision, spec=args.spec, capability_summary=summaries[0])]
    from question.question import Question, QuestionOption, ask
    questions, saved = [], []
    for spec, summary, definition in zip(specs, summaries, definitions, strict=True):
        public = {key: value for key, value in summary.items() if not key.startswith("_")}
        skills = "、".join(item["name"] for item in public.get("skills", [])) or "无"
        tools = "、".join(spec.tool_allowlist) or "无"
        questions.append(Question(header="更新 Agent" if previous else "创建 Agent",
            question=f"是否启用 {spec.name}？\n职责：{spec.description}\n适用：{spec.when_to_use}\nSkills：{skills}\n工具：{tools}\n模型：{public['model']}",
            options=[QuestionOption(label="启用", description="发布这个版本，允许在团队里选择"),
                     QuestionOption(label="先存为草稿", description="保留配置，暂不用于团队"),
                     QuestionOption(label="不要", description="归档这份定义")],
            detail={"kind": "agent_proposal", "definition_id": definition["id"], "spec": spec.model_dump(mode="json"),
                    "previous_spec": previous, "capability_summary": public}))
        saved.append({"definition_id": definition["id"], "version_id": definition["version"]["id"],
                      "revision": definition["revision"], "content_digest": definition["version"]["content_digest"]})
    await ask(ctx.session_id, questions, tool={"callID": ctx.part_id, "messageID": ctx.message_id}, user_id=ctx.user_id,
        continuation={"kind": "agent_proposal", "definitions": saved, "workspace_id": ctx.workspace_id})
    raise RuntimeError("Durable question did not suspend")


agent_manage_tool = define_tool("agent_manage", parameters=AgentManageArgs, execute=execute,
    description="List/get reusable Agents, create up to four complete definitions, or propose one update. Call skill_search in this conversation first, then choose relevant Skills explicitly. Search existing Agents to avoid duplicates. Leave model fields empty unless the user named a model. Creation follows the user's confirmation preference; any desktop, paid or MCP capability always requires confirmation. Updates require confirmation. Drafts cannot join teams. Only interactive build roots may call this tool; never call it inside a batch.",
    sandbox_required=False, parallel_safe=False)
