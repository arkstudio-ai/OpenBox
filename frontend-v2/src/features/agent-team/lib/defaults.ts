import type { AgentSpec, TeamSpec } from "../types"

// Fallback before the server's core_tools catalogue has loaded.
export const CORE_TOOLS = [
  "read",
  "glob",
  "grep",
  "write",
  "edit",
  "multiedit",
  "apply_patch",
  "bash",
  "view_image",
  "share_file",
]

export const READ_TOOLS = [
  "read",
  "glob",
  "grep",
  "view_image",
  "web_search",
  "web_fetch",
  "skill",
  "skill_search",
  "todo_read",
  "todo_write",
]
export function emptyAgent(): AgentSpec {
  return {
    schema_version: 1,
    name: "",
    description: "",
    when_to_use: "",
    instruction: "",
    display: { icon: "bot", color: "blue" },
    example_tasks: [],
    input_schema: null,
    output_schema: null,
    default_model: null,
    model_locked: false,
    allowed_models: [],
    reasoning: null,
    generation_options: {},
    tool_allowlist: [...new Set([...CORE_TOOLS, ...READ_TOOLS])],
    mcp_refs: [],
    skill_mode: "selected",
    skill_refs: [],
    resource_refs: [],
    execution_policy: { max_steps: 50, max_wall_time_seconds: 1800, tool_categories: [] },
  }
}
export function emptyTeam(): TeamSpec {
  return {
    schema_version: 1,
    name: "",
    description: "",
    goal_input_schema: null,
    coordinator: { agent_ref: "builtin:team-coordinator", model: null },
    preset_members: [],
    policy: {
      member_selection: "coordinator_select",
      member_creation: "run_scoped",
      allowed_agent_ids: [],
      allowed_models: [],
      delegable_tools: [...new Set([...CORE_TOOLS, ...READ_TOOLS])],
      allowed_skills: null,
      mcp_refs: [],
      max_members: 8,
      max_concurrent_members: 3,
      max_tasks: 100,
      max_messages: 300,
      max_pending_messages_per_member: 32,
      max_message_bytes: 32768,
      max_coordinator_turns: 60,
      max_wall_time_seconds: 7200,
      paid_tools: {},
      permission_rules: [],
    },
    result_schema: null,
    acceptance_mode: "coordinator",
    resource_refs: [],
  }
}
export const validAgentDraft = (value: AgentSpec) =>
  Boolean(
    value.name.trim() && value.description.trim() && value.when_to_use.trim() && value.instruction.trim(),
  )
export const validTeamDraft = (value: TeamSpec) =>
  Boolean(
    value.name.trim() &&
    value.preset_members.every((member) => !member.inline || validAgentDraft(member.inline)) &&
    (value.policy.member_selection === "coordinator_select" ||
      value.preset_members.some((member) => member.enabled)),
  )
