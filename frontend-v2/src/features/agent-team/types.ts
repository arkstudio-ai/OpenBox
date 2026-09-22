export interface SkillRef {
  name: string
  source?: string | null
}
export interface AgentSpec {
  schema_version: 1
  name: string
  description: string
  when_to_use: string
  instruction: string
  display: { icon: string; color: string }
  example_tasks: string[]
  input_schema: Record<string, unknown> | null
  output_schema: Record<string, unknown> | null
  default_model: string | null
  model_locked: boolean
  allowed_models: string[]
  reasoning: string | null
  generation_options: Record<string, unknown>
  tool_allowlist: string[]
  mcp_refs: Array<{ server: string; tools: string[] }>
  skill_mode: "selected" | "all_accessible"
  skill_refs: SkillRef[]
  resource_refs: string[]
  execution_policy: { max_steps: number; max_wall_time_seconds: number; tool_categories: string[] }
}
export interface TeamPolicy {
  member_selection: "explicit_only" | "coordinator_select"
  member_creation: "disabled" | "run_scoped"
  allowed_agent_ids: string[]
  allowed_models: string[]
  delegable_tools: string[]
  allowed_skills: SkillRef[] | null
  mcp_refs: Array<{ server: string; tools: string[] }>
  max_members: number
  max_concurrent_members: number
  max_tasks: number
  max_messages: number
  max_pending_messages_per_member: number
  max_message_bytes: number
  max_coordinator_turns: number
  max_wall_time_seconds: number
  paid_tools: Record<string, { authorized: true }>
  permission_rules?: Array<{ permission: string; pattern: string; action: "allow" }>
}
export interface MemberSpec {
  alias: string
  agent_ref?: string | null
  version_id?: string | null
  inline?: AgentSpec | null
  responsibility: string
  model_override?: string | null
  additional_skills: SkillRef[]
  enabled: boolean
  version_policy: "latest_at_run_start" | "pinned"
}
export interface TeamSpec {
  schema_version: 1
  name: string
  description: string
  goal_input_schema: Record<string, unknown> | null
  coordinator: { agent_ref: string; model: string | null }
  preset_members: MemberSpec[]
  policy: TeamPolicy
  result_schema: Record<string, unknown> | null
  acceptance_mode: "auto" | "coordinator"
  resource_refs: string[]
}
export interface CapabilitySummary {
  model?: string
  tool_tiers?: Record<string, string>
  tool_ids?: string[]
  skills?: SkillRef[]
  warnings?: Array<{ code?: string; message?: string; tools?: string[]; skill?: string }>
  issues?: Array<{ code: string; message: string }>
}
export interface DefinitionVersion<T> {
  published?: boolean
  id: string
  version: number
  spec: T
  content_digest: string
  capability_summary: CapabilitySummary
  created_at?: string
}
export interface Definition<T> {
  id: string
  name: string
  source: string
  status: "draft" | "active" | "archived"
  readonly?: boolean
  provenance?: { auto_approved?: boolean; autoapproval_undone?: boolean; [key: string]: unknown }
  current_version_number?: number | null
  revision: number
  current_version_id: string | null
  draft_version_id: string | null
  version: DefinitionVersion<T>
  created_at?: string
  updated_at?: string
  run_count?: number
  member_previews?: Array<{ alias: string; name: string; display?: AgentSpec["display"] | null }>
}
export interface Page<T> {
  items: T[]
  next_cursor?: string | null
  next_offset?: number | null
  total?: number
}
export interface DefinitionPage<T> extends Page<Definition<T>> {
  builtin?: Definition<T>[]
  tool_presets?: Record<string, string[]>
  plugin_tools?: string[]
  core_tools?: string[]
  tool_tiers?: Record<string, string>
}
export interface TeamMember {
  id: string
  alias: string
  role: "coordinator" | "member"
  source: string
  name: string
  description: string
  model: string
  tool_ids: string[]
  skill_refs: SkillRef[]
  definition_id?: string | null
  version_id?: string | null
  membership_state: string
  execution_state: string
  current_attempt?: string | null
  error?: string | null
  responsibility?: string
  admission_seq: number
  display?: AgentSpec["display"] | null
}
export interface TeamTask {
  id: string
  title: string
  description?: string
  state: string
  revision: number
  owner_member_id: string
  dependencies: string[]
  deliverable: boolean
  acceptance_mode: string
  current_attempt?: string | null
  blocked_reason?: string | null
  expected_output?: string
}
export interface TeamRunInfo {
  id: string
  title: string
  root_session_id: string
  project_id: string
  template_id?: string | null
  state: string
  revision: number
  seq?: number
  created_at: string
  ended_at?: string | null
  pause_reason?: string | null
  capacity_retry_at?: string | null
  failure_reason?: string | null
  final_summary?: string
  final_artifact_ids?: string[]
  workspace_snapshots?: Partial<Record<"start" | "end", { status: string; hash?: string | null }>>
  goal?: string
  summary?: { task_count?: number; pause_reason?: string; needs_attention?: boolean }
  usage?: TeamUsage
}
export interface TeamUsage {
  credits: string
  tokens: number
  calls: number
  unpriced: number
  pending: number
  categories?: Array<Omit<TeamUsage, "categories" | "items"> & { category: string }>
  items?: Array<{
    member_id: string
    model: string
    kind: string
    category: string
    credits: string
    tokens: number
    calls: number
    unpriced: number
    pending: number
  }>
}
export interface TeamSnapshot {
  id: string
  seq: number
  run: TeamRunInfo
  policy: TeamPolicy
  members: TeamMember[]
  tasks: TeamTask[]
  task_count: number
  completed_task_count?: number
  artifact_count: number
  links: Array<{ from: string; to: string; kind: "task" | "message"; count: number }>
  notices: Array<{ id?: string; code?: string; message?: string; reason?: string; kind?: string }>
}
export interface TeamMessage {
  id: string
  from_member_id: string
  to_member_id: string
  kind: string
  state: string
  body: string
  created_at?: string
  queued_seq: number
  task_id?: string | null
}
export interface TeamAttempt {
  id: string
  task_id: string
  member_id: string
  number: number
  state: string
  summary: string
  error?: string | null
  implicit: boolean
  output?: unknown
  artifact_ids: string[]
}
export interface TeamArtifact {
  id: string
  name?: string
  title?: string
  kind?: string
  asset_id?: string
  path?: string
  url?: string
  task_id?: string
  member_id?: string
  summary?: string
  content?: string
  file_asset_id?: string
  asset?: { id: string; name: string; mime: string; size: number } | null
}
export interface TeamEvent {
  sequence: number
  kind: string
  entity_id: string
  payload: { data: Record<string, unknown> }
}
export interface TeamEvents {
  events: TeamEvent[]
  seq: number
  last_seq: number
  has_more: boolean
}
export interface LineupDetail {
  kind: "team_lineup"
  spec: TeamSpec
  coordinator: CapabilitySummary
  members: Array<CapabilitySummary & { alias: string; name: string; source: string; responsibility: string }>
  grant: { paid_tools?: Record<string, { authorized: true }> }
  concurrent_slots: number
}
export const terminalTeam = (state: string) => ["completed", "canceled", "failed"].includes(state)
export const taskDisplayState = (task: TeamTask, members: TeamMember[]) =>
  task.state === "running" &&
  members.find((member) => member.id === task.owner_member_id)?.execution_state === "queued"
    ? "queued"
    : task.state
