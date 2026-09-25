"""Small typed team protocol exposed through the existing tool executor."""
from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import Field, ValidationInfo, field_validator

from agent_catalog.schemas import Contract, MemberSpec, TeamSpec
from team.errors import TeamError
from team.journal import Actor, command, snapshot
from tool.tool import ToolContext, ToolResult, define_tool


def actor_for(ctx: ToolContext) -> tuple[str, Actor]:
    from team.runtime_binding import current_binding
    binding = current_binding()
    if binding is None or binding.run_id is None or binding.member_id != ctx.session_id or binding.owner_user_id != ctx.user_id:
        raise TeamError("AUTHORITY_REVOKED", "This tool needs an admitted team member.", status=403)
    return binding.run_id, Actor(ctx.user_id, ctx.workspace_id, "member", ctx.session_id, ctx.run_id, ctx.run_generation)


def result(value: dict) -> ToolResult:
    changes = []
    member = value.get("member")
    if isinstance(member, dict) and member.get("id"):
        changes.append({"kind": "joined", "id": member["id"], "name": member.get("name") or member.get("alias") or member["id"]})
    if value.get("status") == "retired" and value.get("changed"):
        changes.append({"kind": "retired", "id": value["member_id"], "name": value.get("name") or value["member_id"]})
    final_response = value.get("summary") if value.get("state") == "completing" and value.get("final_status") in {"completed", "failed"} else None
    return ToolResult(title="Team", output=json.dumps(value, ensure_ascii=False), final_response=final_response, metadata={"team": True,
        "turn_yield": value.get("turn_yield", False), "team_member_changes": changes})


def protected(fn):
    async def execute(args, ctx):
        try:
            await ctx.assert_run_current()
            return await fn(args, ctx)
        except TeamError as exc:
            return ToolResult(title=exc.code, output=json.dumps(exc.to_dict(), ensure_ascii=False), metadata={"error": True, "code": exc.code, "blocked": exc.code == "PERMISSION_REQUIRES_USER"})
    return execute


async def mutate(ctx, args, callback, *, wake=True):
    run_id, actor = actor_for(ctx)
    key = getattr(args, "idempotency_key", None) or ctx.part_id
    async def with_watermark(writer):
        value = await callback(writer)
        # Persist the watermark with the receipt, rather than reading it after
        # commit. A lost-response retry must not acknowledge a later message
        # that this invocation never observed. finish adds one notice when the
        # command is otherwise unchanged.
        return {**value, "seq": writer.state["seq"] + int(not writer.events)}
    value = await command(run_id, actor, key, args.model_dump(mode="json"), with_watermark)
    if wake:
        from bus import bus
        latest = await snapshot(run_id, actor)
        bus.publish("team.run.updated", {"userId": actor.owner_user_id, "sessionId": latest["run"]["root_session_id"],
            "teamRunId": run_id, "seq": latest["seq"], "state": latest["run"]["state"]})
    if wake:
        from team.scheduler import schedule
        schedule(run_id, actor)
    return result(value)


class Empty(Contract):
    pass


class Proposal(Contract):
    mode: Literal["create", "amend"] = Field(default="create", description="Use amend during an active run to request approval for additional members or grants; team lists only NEW members and explicit policy changes.")
    title: str = Field(min_length=1, max_length=255, description="Required top-level proposal title, even when team.name is provided.")
    goal: str = Field(min_length=1, max_length=32000)
    team: TeamSpec | None = Field(default=None, description="Omit when the user selected a team template; the server resolves that exact roster. Otherwise supply TeamSpec here, including policy and preset_members. policy is nested under team, never at the proposal top level. max_members includes the coordinator.")
    acceptance_mode: Literal["auto", "coordinator"] | None = Field(default=None, description="Optional convenience field; applies only to an inline team, never overrides the user's selected template.")

    @field_validator("team", mode="wrap")
    @classmethod
    def amendment_roster(cls, value, handler, info: ValidationInfo):
        if value is not None and info.data.get("mode") == "amend":
            return TeamSpec.model_validate(value, context={"amendment": True})
        return handler(value)


@protected
async def propose(args: Proposal, ctx):
    from agent_catalog.catalog import prepare_lineup
    from core.config import get_config
    from db.base import get_db_session
    from db.models.session import Session
    from db.models.question import QuestionCheckpoint
    from sqlalchemy import select
    config = get_config()
    if not config.team_admission_enabled:
        raise TeamError("TEAM_ADMISSION_DISABLED", "Team admission is disabled.", status=403)
    if args.mode == "amend":
        if args.team is None:
            raise TeamError("INVALID_AMENDMENT", "Provide the new members or grant changes in team.", status=422)
        from team.amendments import prepare
        from skill.provider import ScopeKey
        run_id, actor = actor_for(ctx)
        prepared, references = await prepare(run_id, actor, args.team,
            scope=ScopeKey(user_id=ctx.user_id, project_id=ctx.project_id, workdir=ctx.workdir))
        from question.question import Question, QuestionOption, ask
        await ask(ctx.session_id, [Question(question=f"是否应用这次团队调整？\n原因：{args.goal}", header="调整阵容",
            options=[QuestionOption(label="应用调整", description="按下面列出的新增成员和授权范围调整当前团队"),
                     QuestionOption(label="保持原样", description="继续使用当前阵容和授权")],
            detail={"kind": "team_lineup", "mode": "amend", "title": args.title, "goal": args.goal, **prepared.public()})],
            tool={"callID": ctx.part_id, "messageID": ctx.message_id}, user_id=ctx.user_id,
            continuation={"kind": "team_lineup", "mode": "amend", "lineup": prepared.saved(), **references})
        raise RuntimeError("Durable question did not suspend")
    from team.runtime_binding import current_binding
    binding = current_binding()
    if binding and binding.run_id:
        raise TeamError("TEAM_ALREADY_ACTIVE", "Use mode=amend to adjust the active team.")
    async with get_db_session() as db:
        session = await db.get(Session, ctx.session_id)
        if session is None or session.user_id != ctx.user_id or session.workspace_id != ctx.workspace_id or session.parent_id or session.kind != "normal" or session.agent not in {"build", "team"}:
            raise TeamError("AUTHORITY_REVOKED", "Only an interactive build or team root can propose a lineup.", status=403)
        prior = (await db.execute(select(QuestionCheckpoint).where(QuestionCheckpoint.session_id == session.id))).scalars().all()
        proposals = [row for row in prior if row.continuation.get("kind") == "team_lineup"]
        if len(proposals) >= config.team_max_proposals_per_session:
            raise TeamError("TEAM_PROPOSAL_LIMIT", "This conversation reached its proposal limit.")
        if any(row.status == "rejected" or row.answers == [["不用组队"]] for row in proposals):
            raise TeamError("TEAM_PROPOSAL_REJECTED", "The user declined teams in this conversation; continue in build mode.")
    from team.selection import selected_lineup
    proposed = args.team or TeamSpec(name=args.title[:40])
    if args.acceptance_mode is not None:
        proposed = proposed.model_copy(update={"acceptance_mode": args.acceptance_mode})
    spec, references = await selected_lineup(ctx.session_id, Actor(ctx.user_id, ctx.workspace_id), proposed)
    from skill.provider import ScopeKey
    prepared = await prepare_lineup(spec, Actor(ctx.user_id, ctx.workspace_id),
        scope=ScopeKey(user_id=ctx.user_id, project_id=ctx.project_id, workdir=ctx.workdir))
    from question.question import Question, QuestionOption, ask
    public = prepared.public()
    roster = "、".join(f"{item['name']}（{item['responsibility'] or item['description']}）" for item in public["members"]) or "由协调者根据目标选择成员"
    await ask(ctx.session_id, [Question(question=f"是否开始这支团队？\n目标：{args.goal}\n成员：{roster}", header="组队方案",
        options=[QuestionOption(label="开始", description="按下面的阵容与授权范围开始执行"), QuestionOption(label="不用组队", description="继续由当前 Agent 完成")],
        detail={"kind": "team_lineup", "title": args.title, "goal": args.goal, **public})],
        tool={"callID": ctx.part_id, "messageID": ctx.message_id}, user_id=ctx.user_id,
        continuation={"kind": "team_lineup", "title": args.title, "goal": args.goal, "lineup": prepared.saved(), **references})
    raise RuntimeError("Durable question did not suspend")


class CatalogSearch(Contract):
    query: str = Field(default="", max_length=200)
    limit: int = Field(default=20, ge=1, le=50)


@protected
async def search_catalog(args: CatalogSearch, ctx):
    from agent_catalog.catalog import builtin_entries
    from agent_catalog.repository import list_definitions
    rows = await list_definitions("agent", Actor(ctx.user_id, ctx.workspace_id), search=args.query, status="active", limit=args.limit)
    builtin = [entry for entry in builtin_entries() if not args.query or args.query.casefold() in (entry["name"] + entry["version"]["spec"]["when_to_use"]).casefold()]
    entries = [*builtin, *rows["items"]][:args.limit]
    return result({"items": [{"id": entry["id"], "name": entry["name"], "version_id": entry["version"]["id"],
        "description": entry["version"]["spec"]["description"], "when_to_use": entry["version"]["spec"]["when_to_use"],
        "default_model": entry["version"]["spec"]["default_model"], "tools": entry["version"]["spec"]["tool_allowlist"]} for entry in entries], "next_cursor": rows["next_cursor"]})


class CatalogGet(Contract):
    definition_id: str
    version_id: str | None = None


@protected
async def get_catalog(args: CatalogGet, ctx):
    from agent_catalog.catalog import resolve_agent
    spec, version = await resolve_agent(args.definition_id, Actor(ctx.user_id, ctx.workspace_id), args.version_id)
    return result({"id": args.definition_id, "version_id": version, "spec": spec.model_dump(mode="json")})


class TeamView(Contract):
    section: Literal["summary", "tasks", "attempts", "messages", "artifacts"] = "summary"
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)
    task_id: str | None = Field(default=None, max_length=64, description="Optional exact task ID; omit or use null/empty string for all visible tasks.")

    @field_validator("task_id")
    @classmethod
    def empty_means_all_tasks(cls, value):
        return value or None


@protected
async def view(args: TeamView, ctx):
    from team.commands import member_status
    from team.model_view import view as model_view
    async def read(writer):
        member_status(writer, ctx.session_id, last_seen_seq=writer.state["seq"])
        return await model_view(writer, **args.model_dump())
    return await mutate(ctx, args, read, wake=False)


class StartMember(Contract):
    member: MemberSpec


@protected
async def start_member(args: StartMember, ctx):
    from agent_catalog.catalog import prepare_member
    from agent_catalog.schemas import TeamPolicy
    from team.commands import require_coordinator, require_open
    from team.service import admit_member
    run_id, actor = actor_for(ctx)
    state = await snapshot(run_id, actor)
    if actor.member_id != state["run"]["root_session_id"]:
        raise TeamError("AUTHORITY_REVOKED", "Only the coordinator can add members.", status=403)
    if state["grant"].get("member_selection", state["policy"]["member_selection"]) == "explicit_only":
        raise TeamError("PERMISSION_REQUIRES_USER", "This run uses only its confirmed lineup. Request an amendment before adding a member.")
    from team.amendments import GRANT_FIELDS
    from skill.provider import ScopeKey
    policy = TeamPolicy.model_validate({**state["policy"], **{key: state["grant"][key] for key in GRANT_FIELDS if key in state["grant"]}})
    member, compiled, version_id = await prepare_member(args.member, policy, actor,
        scope=ScopeKey(user_id=ctx.user_id, project_id=ctx.project_id, workdir=ctx.workdir))
    async def admit(writer):
        require_coordinator(writer)
        require_open(writer)
        # Recheck live grants after compilation, inside the admission fence.
        if writer.state["grant"].get("version", 1) != state["grant"].get("version", 1) or writer.state["grant"].get("member_selection") == "explicit_only" or set(compiled.spec.tool_allowlist) - set(writer.state["grant"].get("delegable_tools", [])):
            raise TeamError("AUTHORITY_REVOKED", "The run grant changed during member compilation.")
        return await admit_member(writer, member, compiled, source="coordinator", version_id=version_id)
    return await mutate(ctx, args, admit)


class MessageSend(Contract):
    to_member_ids: list[str] = Field(min_length=1, max_length=31)
    body: str = Field(min_length=1, max_length=32768)
    kind: Literal["message", "question", "answer", "handoff", "progress", "result", "control"] = "message"
    task_id: str | None = None
    task_attempt_id: str | None = None
    reply_to_message_id: str | None = None


@protected
async def send_message(args: MessageSend, ctx):
    from team.commands import queue_message
    async def send(writer):
        receipts = []
        for target in dict.fromkeys(args.to_member_ids):
            receipts.append(await queue_message(writer, to_member_id=target, **args.model_dump(exclude={"to_member_ids"})))
        return {"messages": receipts}
    return await mutate(ctx, args, send)


class TaskCreate(Contract):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=8000)
    owner_member_id: str
    expected_output: str = Field(min_length=1, max_length=2000)
    acceptance_criteria: str = Field(min_length=1, max_length=2000)
    dependencies: list[str] = Field(default_factory=list, max_length=1000)
    input_refs: list[str] = Field(default_factory=list, max_length=128)
    priority: int = Field(default=0, ge=-100, le=100)
    deliverable: bool = Field(default=False,
        description="Set true when this task produces an output the user requested. Set it when creating the task: dispatch can start immediately and running specifications cannot be edited.")
    acceptance_mode: Literal["auto", "coordinator"] = "auto"
    output_schema: dict | None = None
    write_scopes: list[str] = Field(default_factory=list, max_length=128)


@protected
async def create_task(args: TaskCreate, ctx):
    from team.commands import create_task as create
    return await mutate(ctx, args, lambda writer: create(writer, args.model_dump()))


class ArtifactReference(Contract):
    file_asset_id: str = Field(min_length=1, max_length=64,
        description="Ready asset_id from share_file or a completed media tool, in this team's project.")
    content_digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$",
        description="Optional SHA-256 to verify; the server always hashes and freezes actual asset bytes. Never invent a digest.")
    name: str = Field(min_length=1, max_length=255)
    summary: str = Field(default="", max_length=4000)


class TaskUpdate(Contract):
    task_id: str
    expected_revision: int | None = Field(default=None, ge=1, description="Required for EVERY coordinator action: accept, edit, retry, rework, reopen, cancel. Use task.revision from the latest team update or team_view. Members submit their current fenced attempt without a revision.")
    action: Literal["progress", "submit", "block", "fail", "edit", "accept", "retry", "rework", "reopen", "cancel"] = Field(
        description="submit/block/fail require a running attempt and summary. accept/rework require review; retry requires blocked/failed; reopen requires succeeded. retry/rework/reopen/cancel require reason.")
    summary: str = Field(default="", max_length=4000)
    output: Any = Field(default=None, description="Complete result matching the task's output_schema. Pass an object or array directly when the schema requires it, not JSON serialized inside a string.")
    artifacts: list[ArtifactReference] = Field(default_factory=list, max_length=128,
        description="Register ready project assets with the submitted result so they appear in the team's artifacts panel.")
    changes: dict | None = None
    reason: str | None = Field(default=None, max_length=1000,
        description="Required for retry, rework, reopen and cancel. Explain the change or decision here, not in summary. Never retry an unknown external outcome or a permission denial before a grant is confirmed.")


@protected
async def task_update(args: TaskUpdate, ctx):
    from team.commands import update_task
    from team.artifacts import prepare
    run_id, actor = actor_for(ctx)
    if args.artifacts and args.action != "submit":
        raise TeamError("INVALID_ARTIFACT", "Attach artifacts only when submitting a task.")
    artifacts = await prepare(run_id, actor, [ref.model_dump() for ref in args.artifacts])
    await ctx.assert_run_current()
    async def update(writer):
        value = await update_task(writer, **args.model_dump(exclude={"artifacts"}), artifacts=artifacts)
        if writer.actor.member_id != writer.run.root_session_id and args.action in {"submit", "block", "fail"}:
            # The scheduler owns the next task. End this turn after submitting
            # so an idle member cannot issue a redundant request while its
            # dependent task is already starting in another Driver.
            value["turn_yield"] = True
        return value
    return await mutate(ctx, args, update)


class Wait(Contract):
    after_seq: int = Field(ge=0)
    timeout_seconds: int = Field(default=600, ge=10, le=3600)


@protected
async def wait(args: Wait, ctx):
    from team.commands import wait_member
    return await mutate(ctx, args, lambda writer: wait_member(writer, args.after_seq, args.timeout_seconds))


class Interrupt(Contract):
    member_id: str
    reason: str = Field(min_length=1, max_length=1000)
    action: Literal["interrupt", "retire"] = Field(default="interrupt", description="Retire after the member stops, external outcomes are known and unfinished tasks are reassigned or canceled. History and aliases are retained.")


@protected
async def interrupt(args: Interrupt, ctx):
    from team.commands import require_coordinator, member_status
    if args.action == "retire":
        from team.membership import retire_member
        return await mutate(ctx, args, lambda writer: retire_member(writer, args.member_id, args.reason))
    async def request(writer):
        require_coordinator(writer)
        if args.member_id == writer.run.root_session_id:
            raise TeamError("INVALID_MEMBER", "Use the owner's run pause action to stop the coordinator.")
        member_status(writer, args.member_id, error=args.reason, interrupt_requested=True)
        return {"status": "interrupting", "member_id": args.member_id}
    # The post-commit scheduler observes and interrupts an exact generation.
    # A delayed tool response must never abort a newer replacement Driver.
    return await mutate(ctx, args, request)


class Finish(Contract):
    summary: str = Field(min_length=1, max_length=8000, description="The complete final answer displayed to the user, including substantive findings and numbers. Do not merely say that work was completed.")
    artifact_ids: list[str] = Field(default_factory=list, max_length=128)
    status: Literal["completed", "failed"] = Field(default="completed", description="Use failed only when the goal cannot be completed. Preserve useful results and explain the failure; unknown external operations still require reconciliation.")
    reason: str | None = Field(default=None, max_length=1000, description="Required concrete failure reason when status=failed.")


@protected
async def finish(args: Finish, ctx):
    from team.service import finish as complete
    return await mutate(ctx, args, lambda writer: complete(writer, **args.model_dump(),
        response_message_id=ctx.message_id, response_tool_part_id=ctx.part_id))


TOOLS = [
    ("team_propose", Proposal, propose, "Propose a bounded team lineup for explicit user confirmation. Before creating a team, load the agent-team Skill with skill when available. Required top-level fields: title, goal. For an inline lineup use {title, goal, team: {name, coordinator, preset_members, policy}}. Put all limits and grants inside team.policy. A fixed roster containing inline members uses member_selection=explicit_only and member_creation=run_scoped. Set each member's model_override when the user specifies a model for all members. Omit optional reasoning and output_schema unless needed and supported. CAPABILITY_UNSUPPORTED requires correcting the stated model/configuration issue, not repeated roster changes. This suspends until the user responds; it does not start work by itself."),
    ("agent_catalog_search", CatalogSearch, search_catalog, "Find reusable enabled Agents by responsibility and when-to-use hints."),
    ("agent_catalog_get", CatalogGet, get_catalog, "Read an accessible Agent definition and immutable version."),
    ("team_member_start", StartMember, start_member, "Admit an independent member within the confirmed run grant. Does not assign work; use team_task_create next."),
    ("team_view", TeamView, view, "Read roster, task board, permissions and committed sequence. Default summary includes recent artifact IDs for team_finish. Select tasks, attempts, artifacts or messages with offset/limit for details; use task_id to inspect one task and its results. artifact.id is distinct from file_asset_id."),
    ("team_message_send", MessageSend, send_message, "Send bounded data to explicit member IDs. Progress is recorded without waking. Messages never grant tools or permissions."),
    ("team_task_create", TaskCreate, create_task, "Create a task for an active member with dependencies and acceptance criteria. The scheduler dispatches only ready tasks. Mark final output tasks deliverable=true."),
    ("team_task_update", TaskUpdate, task_update, "Update using the latest task revision. Members submit/block/fail their current attempt; coordinators accept, rework, retry, edit or cancel. Unknown external outcomes cannot retry."),
    ("team_wait", Wait, wait, "Yield this turn until meaningful changes or timeout. Use seq from your latest team command receipt; a separate team_view call is unnecessary unless you need its data. no_progress means take action or report a blocker, never poll."),
    ("team_member_interrupt", Interrupt, interrupt, "Interrupt a member at a safe boundary, or retire a stopped member after reassigning/canceling unfinished work. Unknown external operations must be reconciled first; retired aliases cannot be reused."),
    ("team_finish", Finish, finish, "Finish only after every deliverable is accepted or explicitly canceled. Supply the final summary and registered artifact IDs."),
]

team_tools = [define_tool(name, parameters=params, execute=fn, description=description, sandbox_required=False, parallel_safe=False) for name, params, fn, description in TOOLS]
