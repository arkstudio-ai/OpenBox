// Wire types for the admin trajectory protocol v1
// (docs/SESSION_TRAJECTORY_PROTOCOL.md, backend/trajectory/projector.py and
// repository.py). Shapes mirror what the server actually returns; client-only
// conveniences live in ./view.ts and never leak back into these types.

/** Decimal digits. Compare through utils/seq, never with `<` or Number(). */
export type Seq = string

export const PROTOCOL_VERSION = 1
export const PROJECTOR_VERSION = 1

/** Event family → actions accepted by the server (trajectory/types.py FAMILIES). */
export const EVENT_FAMILIES: Readonly<Record<string, readonly string[]>> = {
  trajectory: ["started"],
  baseline: ["captured"],
  input: ["accepted", "injected"],
  session: ["settings_changed"],
  history: ["reverted", "regenerated", "forked"],
  turn: ["started", "finished"],
  run: ["started", "finished", "cancel_requested", "interrupted"],
  step: ["started", "finished"],
  request: ["prepared", "started", "delta", "usage", "finished", "retry_scheduled", "route_changed"],
  tool: ["requested", "started", "output", "finished"],
  permission: ["requested", "resolved", "expired"],
  question: ["asked", "draft_saved", "resolved", "cancelled"],
  agent: ["spawned", "message", "finished"],
  compaction: ["started", "finished"],
  context: ["replaced", "injected"],
  message: ["committed"],
  part: ["committed"],
  plan: ["changed"],
  todo: ["changed"],
  skill: ["loaded"],
  tool_catalog: ["changed"],
  job: ["submitted", "progress", "finished"],
  artifact: ["recorded", "removed"],
  takeover: ["requested", "started", "finished"],
  recording: ["gap"],
  operation: ["late_result"],
}

/** TraceContext fields minus owner/session/workspace, in dataclass order. */
export const ID_FIELDS = [
  "source_session_id",
  "turn_id",
  "run_id",
  "generation",
  "agent_id",
  "parent_agent_id",
  "step_id",
  "request_id",
  "call_id",
  "parent_call_id",
  "message_id",
  "part_id",
  "caused_by_event_id",
] as const
export type IdField = (typeof ID_FIELDS)[number]

export type Availability =
  "available" | "pending" | "not_recorded" | "not_applicable" | "deleted" | "unsupported" | "corrupt"

export interface PayloadRef {
  payload_id: string
  sha256?: string | null
  size_bytes?: number | null
  media_type?: string | null
  availability?: Availability | string | null
  reason?: string | null
}

export interface PayloadEnvelope {
  $payload: PayloadRef
}

export function isPayloadEnvelope(value: unknown): value is PayloadEnvelope {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false
  const ref = (value as { $payload?: unknown }).$payload
  return typeof ref === "object" && ref !== null && typeof (ref as PayloadRef).payload_id === "string"
}

/**
 * Media captured in a model or service input (e.g. an inline base64 image),
 * retained as protected content: `{$media: ref, source_asset_id, source_kind,
 * original_encoding, declared_media_type}`. The actual bytes are only reachable
 * through the payload endpoint; the wrapper never carries base64 or a URL.
 */
export interface MediaEnvelope {
  $media: Partial<PayloadRef> & { availability?: Availability | string | null; reason?: string | null }
  source_asset_id?: string | null
  source_kind?: string | null
  original_encoding?: string | null
  declared_media_type?: string | null
}

export function isMediaEnvelope(value: unknown): value is MediaEnvelope {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false
  const ref = (value as { $media?: unknown }).$media
  return typeof ref === "object" && ref !== null && !Array.isArray(ref)
}

export interface TrajectoryEvent {
  event_id: string
  trajectory_id?: string | null
  user_id: string
  session_id: string
  seq: Seq
  type: string
  version: number
  occurred_at: string
  recorded_at?: string | null
  source_session_id?: string | null
  turn_id?: string | null
  run_id?: string | null
  generation?: number | null
  agent_id?: string | null
  parent_agent_id?: string | null
  step_id?: string | null
  request_id?: string | null
  call_id?: string | null
  parent_call_id?: string | null
  message_id?: string | null
  part_id?: string | null
  caused_by_event_id?: string | null
  data: Record<string, unknown>
}

export const RECORD_KINDS = [
  "user",
  "assistant",
  "request",
  "tool",
  "system",
  "context",
  "agent",
  "permission",
  "question",
  "interrupt",
  "resume",
  "retry",
  "compaction",
  "job",
  "artifact",
  "plan",
  "todo",
  "skill",
  "tool_catalog",
  "takeover",
  "settings",
  "history",
  "baseline",
  "gap",
  "late_result",
  "turn",
  "run",
  "step",
] as const
export type RecordKind = (typeof RECORD_KINDS)[number]

export function isRecordKind(value: unknown): value is RecordKind {
  return typeof value === "string" && (RECORD_KINDS as readonly string[]).includes(value)
}

/** A merged output block: `{block_id, type, text, chunk_index?, ...captured}`. */
export interface RecordBlock {
  block_id: string
  type: unknown
  text: string
  chunk_index?: number
  [captured: string]: unknown
}

/**
 * One projected object. `status`/`title` hold whatever the producer sent, so
 * they are typed loosely here and narrowed at display time.
 */
export interface TrajectoryRecord {
  record_id: string
  kind: string
  title: string
  preview: string | null
  result_preview: string | null
  status: string | null
  status_reason: string | null
  source_session_id: string | null
  turn_id: string | null
  run_id: string | null
  generation: number | null
  agent_id: string | null
  parent_agent_id: string | null
  step_id: string | null
  request_id: string | null
  call_id: string | null
  parent_call_id: string | null
  message_id: string | null
  part_id: string | null
  caused_by_event_id: string | null
  start_seq: Seq
  end_seq: Seq | null
  as_of_seq: Seq
  started_at: string | null
  finished_at: string | null
  duration_ms: number | null
  timing_source: string | null
  data: Record<string, unknown>
  blocks: RecordBlock[]
  usage: Record<string, unknown>
}

/** List rows deliberately omit the complete prompt/input/output and blocks. */
export type RecordSummary = Omit<TrajectoryRecord, "data" | "blocks">

export interface UnsupportedEvent {
  seq: Seq
  type: string | null
  version: number | null
}

/** checkpoint.state and the client projection share this exact shape. */
export interface ProjectionState {
  projector_version: number
  through_seq: Seq
  records: Record<string, TrajectoryRecord>
  unsupported_events: UnsupportedEvent[]
  coverage_start: string | null
}

export interface TrajectoryStatistics {
  request_count: number
  tool_count: number
  error_count: number
  unknown_count: number
  input_tokens: number | null
  output_tokens: number | null
  usage_complete: boolean
  duration_ms: number | null
  through_seq: Seq
  coverage_start: string | null
}

export interface AgentSummary {
  agent_id: string | null
  parent_agent_id: string | null
  source_session_id: string | null
  name: string
  status: string | null
  record_id: string
}

export interface SessionOwner {
  user_id: string
  username: string | null
  email: string | null
}

export interface SessionWorkspace {
  id: string | null
  name: string | null
}

export interface SessionRow {
  session_id: string
  user_id: string
  trajectory_id: string | null
  title: string | null
  owner: SessionOwner
  workspace: SessionWorkspace | null
  workspace_id: string | null
  running_status: string | null
  recording_status: string
  coverage_start: string | null
  last_activity_at: string | null
  model: string | null
  agent: string | null
  committed_seq: Seq
  projected_through_seq: Seq
  through_seq: Seq
  /** List rows carry summary counters; `duration_ms` is null there. */
  statistics: Partial<TrajectoryStatistics> | null
}

export interface SessionHeader extends SessionRow {
  statistics: TrajectoryStatistics
  agents: AgentSummary[]
  capabilities: { recording: boolean; admin_read: boolean; export: boolean }
  projector_version: number
  unsupported_events?: UnsupportedEvent[]
}

export interface SessionPage {
  items: SessionRow[]
  next_cursor: string | null
  has_more: boolean
}

export interface RecordPage {
  items: RecordSummary[]
  next_cursor: string | null
  has_more: boolean
  through_seq: Seq
  projector_version: number
  unsupported_events?: UnsupportedEvent[]
}

export interface RecordDetail {
  record: TrajectoryRecord & { events?: TrajectoryEvent[] }
  through_seq: Seq
  projector_version: number
}

export interface EventPage {
  events: TrajectoryEvent[]
  from_seq: Seq
  through_seq: Seq
  until_seq: Seq
  has_more: boolean
  committed_seq: Seq
}

export interface CheckpointResponse {
  checkpoint: {
    through_seq: Seq
    projector_version: number
    state: unknown
    digest?: string | null
  } | null
  through_seq: Seq
}

export interface SearchHit {
  record_id: string
  seq: Seq
  kind: string
  preview: string | null
}

export interface SearchPage {
  items: SearchHit[]
  next_cursor: string | null
  has_more: boolean
  through_seq: Seq
}

export type ExportStatus = "pending" | "running" | "completed" | "failed"

export interface ExportJob {
  export_id: string
  status: ExportStatus | string
  through_seq: Seq
  error?: string | null
  download_url?: string | null
}
