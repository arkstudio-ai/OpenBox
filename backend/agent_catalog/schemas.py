"""Bounded configuration shared by forms, AI proposals and member admission."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator


READ_TOOLS = ["read", "glob", "grep", "view_image", "web_search", "web_fetch", "skill", "skill_search", "todo_read", "todo_write"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MCPRef(Contract):
    server: str = Field(min_length=1, max_length=128)
    tools: list[str] = Field(default_factory=lambda: ["*"], max_length=128)

    @field_validator("tools")
    @classmethod
    def bounded_patterns(cls, value):
        if any(not pattern.strip() or len(pattern) > 512 or "\x00" in pattern for pattern in value):
            raise ValueError("MCP tool and resource patterns must be nonempty and at most 512 characters")
        return list(dict.fromkeys(pattern.strip() for pattern in value))


class SkillRef(Contract):
    name: str = Field(min_length=1, max_length=256)
    source: str | None = Field(default=None, max_length=256)


class AgentDisplay(Contract):
    icon: str = Field(default="bot", max_length=64)
    color: str = Field(default="blue", max_length=32)


class ExecutionPolicy(Contract):
    max_steps: int = Field(default=50, ge=1, le=500)
    max_wall_time_seconds: int = Field(default=1800, ge=10, le=86400)
    tool_categories: list[Literal["T0", "T1", "T2", "MCP"]] = Field(default_factory=list, max_length=4,
        description="Optional additional ceiling on tool_allowlist and mcp_refs. Empty keeps their exact scopes; nonempty allows only these categories.")


class GenerationOptions(Contract):
    max_output_tokens: int | None = Field(default=None, ge=1, le=1_000_000, strict=True)


class AgentSpec(Contract):
    schema_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=40)
    description: str = Field(min_length=1, max_length=500)
    when_to_use: str = Field(min_length=1, max_length=1000)
    instruction: str = Field(min_length=1, max_length=8192)
    display: AgentDisplay = Field(default_factory=AgentDisplay)
    example_tasks: list[str] = Field(default_factory=list, max_length=5)
    input_schema: dict[str, Any] | None = Field(default=None,
        description="Optional JSON Schema with type=object and a properties map. Omit when no structured input is needed.")
    output_schema: dict[str, Any] | None = Field(default=None,
        description="Optional JSON Schema with type=object and a properties map. For prose use an object with a string property, or omit the schema; a top-level string schema is unsupported.")
    default_model: str | None = Field(default=None, max_length=128)
    model_locked: bool = False
    allowed_models: list[str] = Field(default_factory=list, max_length=128)
    reasoning: str | None = Field(default=None, max_length=32,
        description="Omit unless the user explicitly requested a reasoning override. If set, use an exact reasoning_variants value from the configured model catalogue; never invent standard/default/auto.")
    generation_options: dict[str, Any] = Field(default_factory=dict,
        description="Optional max_output_tokens positive integer cap, including reasoning tokens. Other generation options are unsupported and rejected.")
    tool_allowlist: list[str] = Field(default_factory=lambda: list(READ_TOOLS), max_length=256)
    mcp_refs: list[MCPRef] = Field(default_factory=list, max_length=64)
    skill_mode: Literal["selected", "all_accessible"] = "selected"
    skill_refs: list[SkillRef] = Field(default_factory=list, max_length=256)
    resource_refs: list[str] = Field(default_factory=list, max_length=128)
    execution_policy: ExecutionPolicy = Field(default_factory=ExecutionPolicy)

    @field_validator("generation_options")
    @classmethod
    def supported_generation_options(cls, value: dict) -> dict:
        return GenerationOptions.model_validate(value).model_dump(exclude_none=True)

    @field_validator("input_schema", "output_schema")
    @classmethod
    def validate_schema(cls, value: dict | None) -> dict | None:
        if value is not None:
            from agent.subagent_composition import SubagentCompositionError, validate_output_schema
            try:
                return validate_output_schema(value)
            except SubagentCompositionError as exc:
                raise ValueError(str(exc)) from exc
        return value

    @field_validator("example_tasks", "resource_refs", "tool_allowlist", "allowed_models")
    @classmethod
    def bounded_strings(cls, value: list[str]) -> list[str]:
        if any(not item.strip() or len(item) > 2000 for item in value):
            raise ValueError("entries must contain 1–2000 characters")
        if len(value) != len(set(value)):
            raise ValueError("entries must be unique")
        return value

    @model_validator(mode="after")
    def locked_model(self) -> AgentSpec:
        if self.model_locked and not self.default_model:
            raise ValueError("a locked model requires default_model")
        if self.default_model and self.allowed_models and self.default_model not in self.allowed_models:
            raise ValueError("default_model must belong to allowed_models")
        return self


class PaidToolGrant(Contract):
    """Explicit tool permission; spending uses the account's credit ledger."""
    authorized: Literal[True] = True

    @model_validator(mode="before")
    @classmethod
    def legacy_limits(cls, value):
        # Read immutable historical grants without keeping a second wallet.
        if isinstance(value, dict):
            return {key: item for key, item in value.items() if key not in {"per_call", "total"}}
        return value


class PermissionGrant(Contract):
    permission: str = Field(min_length=1, max_length=128, pattern=r"^[a-z][a-z0-9_:.-]*$",
        description="Exact permission named by PERMISSION_REQUIRES_USER. File writes/edits use edit; shell commands use bash. Wildcard permission names are not allowed.")
    pattern: str = Field(min_length=1, max_length=2048,
        description="Exact requested path/command or a user-approved glob. Confirm the narrowest required scope.")
    action: Literal["allow"] = "allow"

    @field_validator("pattern")
    @classmethod
    def valid_pattern(cls, value):
        if "\x00" in value or "\r" in value or "\n" in value:
            raise ValueError("Permission scope must be one nonempty path or command pattern")
        return value


class TeamPolicy(Contract):
    member_selection: Literal["explicit_only", "coordinator_select"] = Field(default="coordinator_select",
        description="explicit_only fixes the approved roster; coordinator_select permits later additions within the approved scope.")
    member_creation: Literal["disabled", "run_scoped"] = Field(default="run_scoped",
        description="Use run_scoped whenever preset_members includes inline temporary members. disabled allows saved agent_ref members only. To fix a roster containing temporary members, keep run_scoped and set member_selection=explicit_only.")
    allowed_agent_ids: list[str] = Field(default_factory=list, max_length=256)
    allowed_models: list[str] = Field(default_factory=list, max_length=128)
    delegable_tools: list[str] = Field(default_factory=lambda: list(READ_TOOLS), max_length=256)
    allowed_skills: list[SkillRef] | None = None
    mcp_refs: list[MCPRef] = Field(default_factory=list, max_length=64)
    max_members: int = Field(default=8, ge=2, le=32)
    max_concurrent_members: int = Field(default=3, ge=1, le=32)
    max_tasks: int = Field(default=100, ge=1, le=1000)
    max_messages: int = Field(default=300, ge=1, le=10000)
    max_pending_messages_per_member: int = Field(default=32, ge=1, le=256)
    max_message_bytes: int = Field(default=32768, ge=256, le=65536)
    max_coordinator_turns: int = Field(default=60, ge=1, le=500)
    max_wall_time_seconds: int = Field(default=7200, ge=60, le=86400)
    paid_tools: dict[str, PaidToolGrant] = Field(default_factory=dict)
    permission_rules: list[PermissionGrant] = Field(default_factory=list, max_length=128,
        description="User-confirmed operation scopes for tools already in delegable_tools. Does not add tools or override deployment denies. In an amendment, supply the complete desired scope list, retaining any earlier grants still needed.")

    @model_validator(mode="before")
    @classmethod
    def legacy_budget(cls, value):
        if isinstance(value, dict):
            return {key: item for key, item in value.items() if key != "budget_credits"}
        return value

    @model_validator(mode="after")
    def limits(self) -> TeamPolicy:
        if self.max_concurrent_members > self.max_members:
            raise ValueError("concurrency cannot exceed max_members")
        return self


class MemberSpec(Contract):
    alias: str = Field(min_length=1, max_length=40, pattern=r"^[\w-]+$")
    agent_ref: str | None = Field(default=None, max_length=64)
    version_id: str | None = Field(default=None, max_length=64)
    inline: AgentSpec | None = None
    responsibility: str = Field(default="", max_length=2000)
    model_override: str | None = Field(default=None, max_length=128)
    additional_skills: list[SkillRef] = Field(default_factory=list, max_length=128)
    enabled: bool = True
    version_policy: Literal["latest_at_run_start", "pinned"] = Field(default="latest_at_run_start",
        description="Controls version resolution for saved agent_ref members. pinned requires their published version_id. Inline members have no library version and are always frozen at admission.")

    @model_validator(mode="after")
    def source(self) -> MemberSpec:
        if bool(self.agent_ref) == bool(self.inline):
            raise ValueError("exactly one of agent_ref and inline is required")
        if self.agent_ref and self.version_policy == "pinned" and not self.version_id:
            raise ValueError("pinned agent_ref members require a published version_id")
        if self.inline and self.version_id:
            raise ValueError("inline members have no library version_id; their content is frozen at admission")
        return self


class CoordinatorSpec(Contract):
    agent_ref: str = Field(default="builtin:team-coordinator", max_length=64)
    model: str | None = Field(default=None, max_length=128)


class TeamSpec(Contract):
    schema_version: Literal[1] = 1
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=1000)
    goal_input_schema: dict[str, Any] | None = None
    coordinator: CoordinatorSpec = Field(default_factory=CoordinatorSpec)
    preset_members: list[MemberSpec] = Field(default_factory=list, max_length=31)
    policy: TeamPolicy = Field(default_factory=TeamPolicy)
    result_schema: dict[str, Any] | None = None
    acceptance_mode: Literal["auto", "coordinator"] = "coordinator"
    resource_refs: list[str] = Field(default_factory=list, max_length=128)

    @field_validator("goal_input_schema", "result_schema")
    @classmethod
    def validate_schema(cls, value: dict | None) -> dict | None:
        return AgentSpec.validate_schema(value)

    @model_validator(mode="after")
    def roster(self, info: ValidationInfo) -> TeamSpec:
        aliases = [member.alias for member in self.preset_members]
        if len(set(aliases)) != len(aliases) or "coordinator" in aliases:
            raise ValueError("member aliases must be unique and cannot be coordinator")
        enabled = [member for member in self.preset_members if member.enabled]
        if len(enabled) + 1 > self.policy.max_members:
            raise ValueError("preset members exceed max_members including coordinator")
        if not enabled and self.policy.member_selection == "explicit_only" and not (info.context or {}).get("amendment"):
            raise ValueError("an empty lineup requires coordinator selection")
        return self


class TeamRequest(Contract):
    template_id: str | None = Field(default=None, max_length=64)
    allow_supplement: bool | None = None
    requested_agent_ids: list[str] = Field(default_factory=list, max_length=31)
