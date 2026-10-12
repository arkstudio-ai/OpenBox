// Wire types of the lite view (backend/trajectory/lite.py, turns.py; docs/SESSION_TRAJECTORY_LITE_PLAN.md §6.3).
import type { RecordSummary, Seq } from "../types/protocol"

export const TURN_STATUSES = [
  "completed",
  "completed_with_errors",
  "failed",
  "waiting",
  "running",
  "cancelled",
  "unknown",
] as const
export type TurnStatus = (typeof TURN_STATUSES)[number]

export function isTurnStatus(value: unknown): value is TurnStatus {
  return typeof value === "string" && (TURN_STATUSES as readonly string[]).includes(value)
}

/** The statuses an operator means by "异常". */
export const ABNORMAL_STATUSES: readonly TurnStatus[] = [
  "failed",
  "completed_with_errors",
  "waiting",
  "unknown",
]

export interface LiteUser {
  user_id: string
  username: string | null
  email: string | null
}

export interface LiteUserRow {
  user: LiteUser
  last_activity_at: string | null
  current_status: TurnStatus | string
  turns: number
  failed_turns: number
  waiting_turns: number
  running_turns: number
  sessions: number
  input_tokens: number | null
  output_tokens: number | null
  usage_complete: boolean
}

export interface ToolCount {
  total: number
  failed: number
}

export interface TurnSummary {
  turn_id: string
  session_id: string
  session_title: string | null
  user_id: string
  workspace_id: string
  started_at: string | null
  finished_at: string | null
  last_activity_at: string | null
  duration_ms: number | null
  status: TurnStatus | string
  status_reason: string | null
  waiting_kind: string | null
  user_preview: string | null
  assistant_preview: string | null
  user_message_id: string | null
  assistant_message_id: string | null
  main_agent_id: string | null
  has_attachments: boolean
  tool_counts: Record<string, ToolCount>
  tool_failures: number
  agent_count: number
  job_count: number
  artifact_count: number
  retry_count: number
  request_count: number
  input_tokens: number | null
  output_tokens: number | null
  usage_complete: boolean
  first_seq: Seq
  last_seq: Seq
  through_seq: Seq
}

export type StepType =
  | "user_input"
  | "assistant"
  | "tool"
  | "question"
  | "permission"
  | "takeover"
  | "agent"
  | "job"
  | "artifact"
  | "gap"
  | "retry"
  | "control"
  | "hidden"

/** A record summary plus the server's classification for the step flow. */
export interface LiteStep extends RecordSummary {
  step_type: StepType | string
  label: string | null
  group: string | null
  collapsible: boolean
  transparent: boolean
  /** The tool a tool step ran (its title is the result's title, e.g. "exit code: 0"). Absent on older servers. */
  tool_id?: string | null
}

export interface TurnDetail {
  summary: TurnSummary
  steps: LiteStep[]
  through_seq: Seq
  labels_version: number
}

export interface Page<T> {
  items: T[]
  next_cursor: string | null
  has_more: boolean
}

export interface ToolLabel {
  label?: string
  args?: string[]
  result?: string | null
  group?: string | null
  transparent?: boolean
}

export interface LiteLabels {
  version: number
  tools: Record<string, ToolLabel>
  prefixes: Record<string, ToolLabel>
  groups: Record<string, { label?: string; collapse?: boolean }>
  statuses: Record<string, string>
  waiting_kinds: Record<string, string>
}

export const EMPTY_LABELS: LiteLabels = {
  version: 0,
  tools: {},
  prefixes: {},
  groups: {},
  statuses: {},
  waiting_kinds: {},
}
