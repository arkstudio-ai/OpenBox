// Value → translation key tables (ENGINEERING_SPEC §10.3: no dynamic keys).
// Unknown values fall back to an "other" key that shows the raw value, so a
// new backend status is visible rather than silently mislabelled.
import type { StatusTone } from "@/shared/ui/StatusPill"
import type { SessionSort } from "../utils/params"
import type { TabId } from "../utils/tabs"

export const KIND_LABELS: Readonly<Record<string, string>> = {
  user: "kind.user",
  assistant: "kind.assistant",
  request: "kind.request",
  tool: "kind.tool",
  system: "kind.system",
  context: "kind.context",
  agent: "kind.agent",
  permission: "kind.permission",
  question: "kind.question",
  interrupt: "kind.interrupt",
  resume: "kind.resume",
  retry: "kind.retry",
  compaction: "kind.compaction",
  job: "kind.job",
  artifact: "kind.artifact",
  plan: "kind.plan",
  todo: "kind.todo",
  skill: "kind.skill",
  tool_catalog: "kind.toolCatalog",
  takeover: "kind.takeover",
  settings: "kind.settings",
  history: "kind.history",
  baseline: "kind.baseline",
  gap: "kind.gap",
  late_result: "kind.lateResult",
  turn: "kind.turn",
  run: "kind.run",
  step: "kind.step",
}

export const RECORD_STATUS_LABELS: Readonly<Record<string, string>> = {
  pending: "status.pending",
  running: "status.running",
  streaming: "status.streaming",
  waiting: "status.waiting",
  accepted: "status.accepted",
  injected: "status.injected",
  completed: "status.completed",
  failed: "status.failed",
  cancelled: "status.cancelled",
  denied: "status.denied",
  timed_out: "status.timedOut",
  unknown: "status.unknown",
  interrupted: "status.interrupted",
  expired: "status.expired",
  deleted: "status.deleted",
}

export const RUN_STATUS_LABELS: Readonly<Record<string, string>> = {
  running: "runStatus.running",
  waiting: "runStatus.waiting",
  idle: "runStatus.idle",
  error: "runStatus.error",
}

export const RECORDING_STATUS_LABELS: Readonly<Record<string, string>> = {
  recording: "recordingStatus.recording",
  not_recorded: "recordingStatus.notRecorded",
  gap: "recordingStatus.gap",
  paused: "recordingStatus.paused",
  failed: "recordingStatus.failed",
}

/** Filter choices the server's list endpoint accepts. */
export const RUN_STATUS_FILTERS = ["running", "waiting", "idle", "error"] as const
export const RECORDING_STATUS_FILTERS = ["recording", "gap", "paused", "not_recorded"] as const

/** Labels for the server-side list orders (utils/params SESSION_SORTS); a page is never re-sorted locally. */
export const LIST_SORT_LABELS: Readonly<Record<SessionSort, string>> = {
  last_activity_desc: "list.sort.newest",
  last_activity_asc: "list.sort.oldest",
}

/** Readable position names, e.g. "Turn 2", and their compact row form "T2". */
export const ORDINAL_LABELS: Readonly<Record<string, string>> = {
  turn: "position.turn",
  run: "position.run",
  step: "position.step",
  request: "position.request",
}

export const ORDINAL_SHORT_LABELS: Readonly<Record<string, string>> = {
  turn: "position.turnShort",
  run: "position.runShort",
  step: "position.stepShort",
  request: "position.requestShort",
}

export const EXPORT_STATUS_LABELS: Readonly<Record<string, string>> = {
  pending: "export.status.pending",
  running: "export.status.running",
  completed: "export.status.completed",
  failed: "export.status.failed",
}

/** Why the viewer can no longer read trajectories (stores/access DenialReason). */
export const DENIAL_LABELS: Readonly<Record<string, string>> = {
  unauthenticated: "access.unauthenticated",
  forbidden: "access.forbidden",
  signed_out: "access.signedOut",
  role_changed: "access.roleChanged",
  identity_changed: "access.identityChanged",
}

export const TIMING_SOURCE_LABELS: Readonly<Record<string, string>> = {
  producer_monotonic: "timingSource.producerMonotonic",
  session_timestamps: "timingSource.sessionTimestamps",
  not_recorded: "timingSource.notRecorded",
}

export const SCHEMA_SOURCE_LABELS: Readonly<Record<string, string>> = {
  provider_request: "schemaSource.providerRequest",
  executor_registry: "schemaSource.executorRegistry",
}

export const CAPTURE_LEVEL_LABELS: Readonly<Record<string, string>> = {
  adapter_input: "captureLevel.adapterInput",
  provider_wire: "captureLevel.providerWire",
  executor_content: "captureLevel.executorContent",
}

export const BLOCK_TYPE_LABELS: Readonly<Record<string, string>> = {
  text: "block.text",
  output_text: "block.text",
  reasoning: "block.reasoning",
  reasoning_text: "block.reasoning",
  tool_arguments: "block.toolArguments",
}

export const SYNC_ERROR_LABELS: Readonly<Record<string, string>> = {
  denied: "sync.error.denied",
  not_recorded: "sync.error.notRecorded",
  corrupt: "sync.error.corrupt",
  gap: "sync.error.gap",
  malformed: "sync.error.malformed",
  network: "sync.error.network",
  deleted: "sync.error.deleted",
}

export const CHECKPOINT_REJECTION_LABELS: Readonly<Record<string, string>> = {
  incompatible_version: "sync.rejection.incompatibleVersion",
  malformed: "sync.rejection.malformed",
  beyond_target: "sync.rejection.beyondTarget",
  future_watermark: "sync.rejection.futureWatermark",
}

export const RELATION_LABELS: Readonly<Record<string, string>> = {
  request: "relation.request",
  assistant: "relation.assistant",
  tool: "relation.tool",
  parentTool: "relation.parentTool",
  agent: "relation.agent",
  parentAgent: "relation.parentAgent",
  turn: "relation.turn",
  run: "relation.run",
  step: "relation.step",
  nextAttempt: "relation.nextAttempt",
  resumedRun: "relation.resumedRun",
  sourceRequest: "relation.sourceRequest",
  system: "relation.system",
}

export const AVAILABILITY_LABELS: Readonly<Record<string, string>> = {
  available: "availability.available",
  pending: "availability.pending",
  not_recorded: "availability.notRecorded",
  not_applicable: "availability.notApplicable",
  deleted: "availability.deleted",
  unsupported: "availability.unsupported",
  corrupt: "availability.corrupt",
  absent: "availability.absent",
  unknown: "availability.unknown",
}

const OK: StatusTone = "ok"
const DANGER: StatusTone = "danger"
const WARN: StatusTone = "warn"
const ACCENT: StatusTone = "accent"
const MUTED: StatusTone = "muted"

export const RECORD_STATUS_TONES: Readonly<Record<string, StatusTone>> = {
  completed: OK,
  accepted: OK,
  injected: OK,
  running: ACCENT,
  streaming: ACCENT,
  pending: MUTED,
  waiting: WARN,
  failed: DANGER,
  denied: DANGER,
  timed_out: DANGER,
  unknown: WARN,
  cancelled: MUTED,
  interrupted: WARN,
  expired: MUTED,
  deleted: MUTED,
}

export const RUN_STATUS_TONES: Readonly<Record<string, StatusTone>> = {
  running: ACCENT,
  waiting: WARN,
  idle: MUTED,
  error: DANGER,
}

export const RECORDING_STATUS_TONES: Readonly<Record<string, StatusTone>> = {
  recording: OK,
  not_recorded: MUTED,
  gap: WARN,
  paused: WARN,
  failed: DANGER,
}

export const TAB_LABELS: Readonly<Record<TabId, string>> = {
  summary: "tab.summary",
  preview: "tab.preview",
  raw: "tab.raw",
  source: "tab.source",
  input: "tab.input",
  options: "tab.options",
  usage: "tab.usage",
  timing: "tab.timing",
  arguments: "tab.arguments",
  result: "tab.result",
  schema: "tab.schema",
  diff: "tab.diff",
  systemPrompt: "tab.systemPrompt",
  tools: "tab.tools",
  task: "tab.task",
  tree: "tab.tree",
  request: "tab.request",
  decision: "tab.decision",
  questions: "tab.questions",
  answer: "tab.answer",
  impact: "tab.impact",
  related: "tab.related",
  attempts: "tab.attempts",
  error: "tab.error",
  output: "tab.output",
  progress: "tab.progress",
  content: "tab.content",
  versions: "tab.versions",
  events: "tab.events",
}

/** Timeline lane per kind, and the token classes that colour it. */
export type Lane = "input" | "model" | "tool" | "interaction" | "agent" | "lifecycle" | "state"

export const KIND_LANES: Readonly<Record<string, Lane>> = {
  user: "input",
  context: "input",
  baseline: "input",
  request: "model",
  assistant: "model",
  retry: "model",
  compaction: "model",
  system: "state",
  tool_catalog: "state",
  tool: "tool",
  job: "tool",
  artifact: "tool",
  permission: "interaction",
  question: "interaction",
  takeover: "interaction",
  agent: "agent",
  turn: "lifecycle",
  run: "lifecycle",
  step: "lifecycle",
  interrupt: "lifecycle",
  resume: "lifecycle",
  gap: "lifecycle",
  late_result: "lifecycle",
}

export const LANES: readonly Lane[] = ["input", "model", "tool", "interaction", "agent", "lifecycle", "state"]

export const LANE_LABELS: Readonly<Record<Lane, string>> = {
  input: "timeline.lane.input",
  model: "timeline.lane.model",
  tool: "timeline.lane.tool",
  interaction: "timeline.lane.interaction",
  agent: "timeline.lane.agent",
  lifecycle: "timeline.lane.lifecycle",
  state: "timeline.lane.state",
}

export const LANE_CLASSES: Readonly<Record<Lane, string>> = {
  input: "bg-a300",
  model: "bg-a700",
  tool: "bg-s600",
  interaction: "bg-a200",
  agent: "bg-s400",
  lifecycle: "bg-n400",
  state: "bg-n600",
}

export function labelKey(
  table: Readonly<Record<string, string>>,
  value: string | null | undefined,
  fallback: string,
): string {
  return (value && table[value]) || fallback
}
