// TypeScript port of backend/trajectory/projector.py (projector_version 1).
// Record values must equal the server's at every watermark, so this file
// follows the Python control flow branch for branch — including choices that
// look odd from a UI point of view (a chunk event overwriting result_preview,
// `run_id` equality between two missing ids). Divergence is a bug; the golden
// fixture tests in projector.test.ts are the referee.
//
// Unlike the Python version, a batch copies the record map once and clones each
// touched record once, so replaying thousands of deltas stays linear.
import {
  EVENT_FAMILIES,
  ID_FIELDS,
  PROJECTOR_VERSION,
  type ProjectionState,
  type TrajectoryEvent,
  type TrajectoryRecord,
} from "../types/protocol"
import { cmpSeq } from "./seq"
import {
  elapsedMs,
  isPlainObject,
  pyDumps,
  pyEqual,
  pyGet,
  pyOr,
  pyPreview,
  pyStr,
  pyTruthy,
  type PlainObject,
} from "./python"

export const EVENT_TYPES: ReadonlySet<string> = new Set(
  Object.entries(EVENT_FAMILIES).flatMap(([family, actions]) =>
    actions.map((action) => `${family}.${action}`),
  ),
)

/** Statuses the interruption sweep and compatibility checkpoints leave alone. */
export const TERMINAL = new Set([
  "completed",
  "failed",
  "cancelled",
  "denied",
  "timed_out",
  "unknown",
  "interrupted",
  "expired",
])
export const ERROR_STATUSES = new Set(["failed", "denied", "timed_out"])

type Records = Readonly<Record<string, TrajectoryRecord>>

const STREAM_TYPES = new Set(["request.delta", "tool.output", "request.usage"])
const TEXT_BLOCK_TYPES = new Set(["text", "output_text", "reasoning", "reasoning_text"])
const BLOCK_RESERVED = new Set(["delta", "text", "arguments", "mode", "chunk_index"])
const LIFECYCLE_FAMILIES = new Set([
  "request",
  "tool",
  "run",
  "turn",
  "step",
  "agent",
  "question",
  "permission",
  "job",
  "compaction",
  "takeover",
])
const POINT_KINDS = new Set(["interrupt", "resume", "retry", "gap", "late_result"])
const TIMED_KINDS = new Set([
  "request",
  "tool",
  "run",
  "turn",
  "step",
  "agent",
  "permission",
  "question",
  "job",
  "compaction",
  "takeover",
])
const OWN_ID_FAMILIES = new Set(["question", "permission", "job", "artifact", "compaction", "takeover"])
const INTERRUPTIBLE_KINDS = new Set(["tool", "request", "assistant", "step"])
const CONTENT_FAMILIES = new Set(["input", "message", "part"])
const ACTION_STATUS: Readonly<Record<string, string>> = {
  cancelled: "cancelled",
  expired: "expired",
  interrupted: "interrupted",
  removed: "deleted",
}
const STATUS_SYNONYMS: Readonly<Record<string, string>> = {
  success: "completed",
  succeeded: "completed",
  error: "failed",
  timeout: "timed_out",
  rejected: "denied",
}

export function emptyState(): ProjectionState {
  return {
    projector_version: PROJECTOR_VERSION,
    through_seq: "0",
    records: {},
    unsupported_events: [],
    coverage_start: null,
  }
}

function dataOf(event: TrajectoryEvent): PlainObject {
  const data = (event as unknown as PlainObject).data
  return isPlainObject(data) ? data : {}
}

function envelope(event: TrajectoryEvent): PlainObject {
  return event as unknown as PlainObject
}

function splitType(type: string): [string, string] {
  const dot = type.indexOf(".")
  return dot < 0 ? [type, ""] : [type.slice(0, dot), type.slice(dot + 1)]
}

function objectOr(value: unknown): PlainObject {
  return isPlainObject(value) ? value : {}
}

function identity(event: TrajectoryEvent, field: string): unknown {
  return pyOr(pyGet(envelope(event), field), pyGet(dataOf(event), field))
}

/** Python `max(rows, key=int(start_seq))`: the first row with the highest seq. */
function latest(rows: Iterable<TrajectoryRecord>): TrajectoryRecord | null {
  let best: TrajectoryRecord | null = null
  for (const row of rows) if (!best || cmpSeq(row.start_seq, best.start_seq) > 0) best = row
  return best
}

function contentTargets(
  event: TrajectoryEvent,
  family: string,
  records?: Records,
): Array<[string, string]> | string {
  const data = dataOf(event)
  const message = objectOr(pyGet(data, "message", {}))
  const part = objectOr(pyGet(data, "part", {}))
  const role = pyOr(pyGet(data, "role"), pyGet(message, "role"))
  if (family === "part") {
    const partType = pyGet(part, "type")
    // Tool/plan/file parts are already typed records of their own.
    if (partType !== null && partType !== "text" && partType !== "reasoning") return []
  }
  const kind = role === "user" ? "user" : "assistant"
  const requestId = identity(event, "request_id")
  if (kind === "assistant") {
    // An empty chat container is not a second AI output.
    if (family === "message" && pyGet(data, "operation") === "created" && !pyTruthy(pyGet(message, "finish")))
      return []
    if (records && !pyTruthy(requestId)) {
      const messageId = identity(event, "message_id")
      const related = latest(
        Object.values(records).filter(
          (row) => row.kind === "assistant" && (row.message_id ?? null) === messageId,
        ),
      )
      if (related) return [[related.record_id, "assistant"]]
    }
    if (
      family === "part" &&
      !pyTruthy(requestId) &&
      !pyTruthy(pyOr(pyGet(part, "text"), pyGet(part, "content")))
    )
      return []
  }
  const id = pyOr(kind === "assistant" ? requestId : null, identity(event, "message_id"), event.event_id)
  return `${kind}:${pyStr(id)}`
}

function requestTargets(event: TrajectoryEvent, action: string): Array<[string, string]> {
  if (action === "retry_scheduled" || action === "route_changed")
    return [[`retry:${event.event_id}`, "retry"]]
  const requestId = pyStr(identity(event, "request_id"))
  const base: Array<[string, string]> = [[`request:${requestId}`, "request"]]
  if (action === "delta") base.push([`assistant:${requestId}`, "assistant"])
  return base
}

function lifecycleTargets(event: TrajectoryEvent, family: string, action: string): Array<[string, string]> {
  const own = pyStr(identity(event, `${family}_id`))
  const base: Array<[string, string]> = [[`${family}:${own}`, family]]
  if (family === "run" && (action === "interrupted" || action === "cancel_requested")) {
    base.push([`interrupt:${event.event_id}`, "interrupt"])
  } else if (family === "run" && action === "started" && pyTruthy(pyGet(dataOf(event), "resume_of_run_id"))) {
    base.push([`resume:${own}`, "resume"])
  }
  return base
}

const LIFECYCLE_TARGET_FAMILIES = new Set(["turn", "run", "step", "agent"])

/** projector.targets: every (record_id, kind) an event updates. */
export function targets(event: TrajectoryEvent, records?: Records): Array<[string, string]> {
  const [family, action] = splitType(event.type)
  const data = dataOf(event)
  if (family === "request") return requestTargets(event, action)
  if (LIFECYCLE_TARGET_FAMILIES.has(family)) return lifecycleTargets(event, family, action)
  if (family === "message" || family === "part") {
    const result = contentTargets(event, family, records)
    if (typeof result !== "string") return result
    return [[result, result.slice(0, result.indexOf(":"))]]
  }
  let id: unknown = null
  let kind = family
  if (family === "trajectory" || family === "baseline") {
    kind = "baseline"
    id = pyGet(envelope(event), "trajectory_id", event.session_id)
  } else if (family === "input") {
    kind = "user"
    id = pyOr(identity(event, "message_id"), event.event_id)
  } else if (family === "tool") {
    id = identity(event, "call_id")
  } else if (OWN_ID_FAMILIES.has(family)) {
    id = pyOr(identity(event, `${family}_id`), pyGet(data, "id"), identity(event, "call_id"), event.event_id)
  } else if (family === "session") {
    kind = "settings"
  } else if (family === "recording") {
    kind = "gap"
  } else if (family === "operation") {
    kind = "late_result"
  } else if (family === "context") {
    kind = "context"
  }
  id = pyOr(id, event.event_id)
  return [[`${kind}:${pyStr(id)}`, kind]]
}

function newRecord(event: TrajectoryEvent, recordId: string, kind: string): TrajectoryRecord {
  const ids = Object.fromEntries(ID_FIELDS.map((key) => [key, pyGet(envelope(event), key, null)]))
  return {
    record_id: recordId,
    kind,
    title: kind,
    preview: null,
    result_preview: null,
    status: "pending",
    status_reason: null,
    ...(ids as Pick<TrajectoryRecord, (typeof ID_FIELDS)[number]>),
    start_seq: String(event.seq),
    end_seq: null,
    as_of_seq: String(event.seq),
    // A tool starts when it enters the executor, not when the model asked for it.
    started_at: kind === "tool" ? null : event.occurred_at,
    finished_at: null,
    duration_ms: null,
    timing_source: null,
    data: {},
    blocks: [],
    usage: {},
  }
}

/** projector._system_snapshot: a record only when the effective system or tools change. */
function systemSnapshot(records: Records, event: TrajectoryEvent): TrajectoryRecord | null {
  if (event.type !== "request.prepared") return null
  const actual = pyGet(dataOf(event), "input")
  if (!isPlainObject(actual)) return null
  let system = pyGet(actual, "system", pyGet(actual, "instructions"))
  const messages = pyGet(actual, "messages", pyGet(actual, "input"))
  if (system === null && Array.isArray(messages)) {
    system = messages.filter(
      (message) => isPlainObject(message) && (message.role === "system" || message.role === "developer"),
    )
  }
  if (system === null && !Object.prototype.hasOwnProperty.call(actual, "tools")) return null
  const snapshot = { system, tools: pyGet(actual, "tools") }
  const agentId = pyGet(envelope(event), "agent_id")
  const sourceId = pyGet(envelope(event), "source_session_id")
  const before = latest(
    Object.values(records).filter(
      (row) =>
        row.kind === "system" &&
        (row.agent_id ?? null) === agentId &&
        (row.source_session_id ?? null) === sourceId,
    ),
  )
  const previous = before
    ? { system: pyGet(before.data, "system"), tools: pyGet(before.data, "tools") }
    : null
  if (before && pyEqual(previous, snapshot)) return null
  const requestId = pyGet(envelope(event), "request_id")
  const record = newRecord(event, `system:${pyStr(requestId)}`, "system")
  record.title = before ? "System updated" : "System state"
  record.status = "completed"
  record.end_seq = String(event.seq)
  record.data = {
    ...snapshot,
    before: previous,
    source_request_id: requestId,
    capture_level: pyGet(dataOf(event), "capture_level"),
  }
  return record
}

function applyBlocks(record: TrajectoryRecord, data: PlainObject): void {
  let blocks = pyGet(data, "blocks")
  if (blocks === null) {
    blocks = [
      {
        block_id: pyGet(data, "block_id", "text:0"),
        type: pyGet(data, "block_type", "text"),
        delta: pyGet(data, "delta", ""),
      },
    ]
  }
  const list = Array.isArray(blocks) ? blocks : []
  list.forEach((block: unknown, index) => {
    if (!isPlainObject(block)) return
    const blockId = pyStr(pyGet(block, "block_id", `${pyStr(pyGet(block, "type", "text"))}:${index}`))
    let previous = record.blocks.find((item) => item.block_id === blockId)
    if (!previous) {
      previous = {
        block_id: blockId,
        type: pyGet(block, "type", pyGet(block, "block_type", "text")),
        text: "",
      }
      record.blocks.push(previous)
    }
    const chunkIndex = pyGet(block, "chunk_index", pyGet(data, "chunk_index"))
    if (chunkIndex !== null && (pyGet(previous, "chunk_index", -1) as number) >= (chunkIndex as number))
      return
    let delta = pyGet(block, "delta", pyGet(block, "text", pyGet(block, "arguments", "")))
    if (delta === null) delta = ""
    if (isPlainObject(delta)) delta = pyGet(delta, "text", pyGet(delta, "arguments", pyDumps(delta)))
    if (typeof delta !== "string") delta = pyDumps(delta)
    const mode = pyGet(block, "mode", pyGet(data, "mode", "delta"))
    previous.text =
      mode === "replace" ? (delta as string) : `${pyStr(pyGet(previous, "text", ""))}${delta as string}`
    for (const [key, value] of Object.entries(block)) {
      if (!BLOCK_RESERVED.has(key)) (previous as PlainObject)[key] = value
    }
    if (chunkIndex !== null) previous.chunk_index = chunkIndex as number
  })
  const text = record.blocks
    .filter((block) => TEXT_BLOCK_TYPES.has(block.type as string))
    .map((block) => pyStr(pyGet(block, "text", "")))
    .join("")
  record.preview = pyOr(pyPreview(text), record.blocks.length ? "tool calls" : null) as string | null
}

function applyUsage(record: TrajectoryRecord, data: PlainObject): void {
  const incoming = pyGet(data, "usage", {})
  if (!isPlainObject(incoming)) return
  if (pyGet(data, "mode", "replace") === "replace") {
    record.usage = { ...incoming }
    return
  }
  for (const [key, value] of Object.entries(incoming)) {
    record.usage[key] = typeof value === "number" ? (pyGet(record.usage, key, 0) as number) + value : value
  }
}

function applyToolOutput(record: TrajectoryRecord, data: PlainObject): void {
  const chunkIndex = pyGet(data, "chunk_index")
  const last = pyGet(record.data, "output_chunk_index", -1) as number
  if (chunkIndex !== null && !((chunkIndex as number) > last)) return
  const output = pyGet(data, "output", pyGet(data, "delta", ""))
  if (pyGet(data, "mode", "delta") === "replace") record.data.output = output
  else if (typeof output === "string")
    record.data.output = `${pyStr(pyGet(record.data, "output", ""))}${output}`
  else record.data.output = output
  if (chunkIndex !== null) record.data.output_chunk_index = chunkIndex
  record.result_preview = pyPreview(pyGet(record.data, "output"))
}

function applyFinish(record: TrajectoryRecord, event: TrajectoryEvent, action: string): void {
  const data = dataOf(event)
  const raw = pyOr(pyGet(data, "status"), pyGet(data, "outcome"), ACTION_STATUS[action] ?? "completed")
  record.status = (typeof raw === "string" ? (STATUS_SYNONYMS[raw] ?? raw) : raw) as string | null
  record.finished_at = event.occurred_at
  record.end_seq = String(event.seq)
  if (Object.prototype.hasOwnProperty.call(data, "usage")) applyUsage(record, data)
}

/** Message/Part checkpoints are compatibility facts; they never overturn a terminal state. */
function applyContentCheckpoint(record: TrajectoryRecord, event: TrajectoryEvent): void {
  const message = pyGet(dataOf(event), "message", {})
  if (record.kind === "user" && record.status === "pending") {
    record.status = "accepted"
  } else if (
    isPlainObject(message) &&
    pyTruthy(pyGet(message, "finish")) &&
    !TERMINAL.has(record.status as string)
  ) {
    record.status = pyTruthy(pyGet(message, "error")) ? "failed" : "completed"
    record.end_seq = String(event.seq)
  }
}

function applyLifecycle(record: TrajectoryRecord, event: TrajectoryEvent): void {
  const data = dataOf(event)
  const [family, action] = splitType(event.type)
  if (event.type === "request.delta") {
    applyBlocks(record, data)
    record.status = "streaming"
  } else if (event.type === "request.usage") {
    applyUsage(record, data)
  } else if (event.type === "tool.output") {
    applyToolOutput(record, data)
  } else if (action === "started" || action === "spawned") {
    record.status = "running"
    record.started_at = event.occurred_at
  } else if (event.type === "input.accepted") {
    record.status = "accepted"
  } else if (event.type === "input.injected") {
    record.status = "injected"
  } else if (["requested", "asked", "submitted", "prepared"].includes(action)) {
    record.status = family === "permission" || family === "question" ? "waiting" : "pending"
  } else if (["finished", "resolved", "cancelled", "expired", "interrupted", "removed"].includes(action)) {
    applyFinish(record, event, action)
  } else if (family === "message" || family === "part") {
    applyContentCheckpoint(record, event)
  } else if (!LIFECYCLE_FAMILIES.has(family)) {
    record.status = pyGet(data, "status", "completed") as string | null
    record.end_seq = String(event.seq)
  }
  if (POINT_KINDS.has(record.kind)) {
    record.status = pyGet(data, "status", "completed") as string | null
    record.end_seq = String(event.seq)
  }
}

function applyTiming(record: TrajectoryRecord, data: PlainObject): void {
  if (Object.prototype.hasOwnProperty.call(data, "duration_ms")) {
    const duration = data.duration_ms as number | null
    record.duration_ms = duration
    record.timing_source =
      duration !== null ? (pyGet(data, "timing_source", "producer_monotonic") as string | null) : null
  } else if (
    pyTruthy(record.finished_at) &&
    pyTruthy(record.started_at) &&
    TIMED_KINDS.has(record.kind) &&
    record.status !== "denied" &&
    record.status !== "unknown"
  ) {
    const elapsed = elapsedMs(record.started_at as string, record.finished_at as string)
    if (elapsed !== null) {
      record.duration_ms = elapsed
      record.timing_source = "session_timestamps"
    }
  }
  // A tool that never entered its executor has no execution time, whatever the
  // finish event reports (a denial before start, invalid arguments…).
  if (record.kind === "tool" && record.started_at === null) {
    record.duration_ms = null
    record.timing_source = null
  }
}

function applyCommittedPart(record: TrajectoryRecord, event: TrajectoryEvent, family: string): void {
  const part = pyGet(dataOf(event), "part")
  if (family !== "part" || !isPlainObject(part)) return
  const partId = pyOr(pyGet(envelope(event), "part_id"), pyGet(part, "id"))
  if (!pyTruthy(partId)) return
  const existing = record.data.committed_parts
  record.data.committed_parts = { ...(isPlainObject(existing) ? existing : {}), [pyStr(partId)]: part }
}

/** projector._update. Mutates `record`, which the caller has already cloned. */
function update(record: TrajectoryRecord, event: TrajectoryEvent): void {
  const data = dataOf(event)
  const [family] = splitType(event.type)
  record.as_of_seq = String(event.seq)
  const source = envelope(event)
  for (const key of ID_FIELDS) {
    const value = pyGet(source, key)
    if (value !== null) (record as unknown as PlainObject)[key] = value
  }
  // Streaming chunks merge into one stable block/output instead of copying
  // every chunk into the record's details; raw chunks stay in the event log.
  if (!STREAM_TYPES.has(event.type)) Object.assign(record.data, data)
  applyCommittedPart(record, event, family)
  if (event.type === "trajectory.started") record.data.coverage_start = event.occurred_at
  if (event.type === "tool.requested") record.data.requested_at = event.occurred_at
  applyLifecycle(record, event)
  if (pyTruthy(pyGet(data, "error")) || pyTruthy(pyGet(data, "reason"))) {
    record.status_reason = pyPreview(pyOr(pyGet(data, "reason"), pyGet(data, "error")), 500)
  }
  applyTiming(record, data)
  record.title = pyStr(
    pyOr(
      pyGet(data, "title"),
      pyGet(data, "name"),
      pyGet(data, "tool"),
      pyGet(data, "tool_name"),
      pyGet(data, "model"),
      record.title,
    ),
  )
  const candidate = pyPreview(
    pyOr(
      pyGet(data, "text"),
      pyGet(data, "content"),
      pyGet(data, "input"),
      pyGet(data, "prompt"),
      pyGet(data, "questions"),
      pyGet(data, "requested_arguments"),
      pyGet(data, "arguments"),
      pyGet(data, "summary"),
    ),
  )
  if (candidate !== null && (record.preview === null || CONTENT_FAMILIES.has(family)))
    record.preview = candidate
  for (const key of ["output", "result", "answers", "model_output"]) {
    if (Object.prototype.hasOwnProperty.call(data, key)) {
      record.result_preview = pyPreview(data[key])
      break
    }
  }
}

function cloneRecord(record: TrajectoryRecord): TrajectoryRecord {
  return {
    ...record,
    data: { ...record.data },
    blocks: record.blocks.map((block) => ({ ...block })),
    usage: { ...record.usage },
  }
}

class Batch {
  readonly records: Record<string, TrajectoryRecord>
  private readonly owned = new Set<string>()
  throughSeq: string
  coverageStart: string | null
  unsupported: ProjectionState["unsupported_events"]

  constructor(readonly base: ProjectionState) {
    this.records = { ...base.records }
    this.throughSeq = base.through_seq
    this.coverageStart = base.coverage_start
    this.unsupported = base.unsupported_events
  }

  private mutable(recordId: string): TrajectoryRecord {
    if (!this.owned.has(recordId)) {
      this.records[recordId] = cloneRecord(this.records[recordId])
      this.owned.add(recordId)
    }
    return this.records[recordId]
  }

  private create(record: TrajectoryRecord): TrajectoryRecord {
    this.records[record.record_id] = record
    this.owned.add(record.record_id)
    return record
  }

  apply(event: TrajectoryEvent): void {
    if (cmpSeq(String(event.seq), this.throughSeq) <= 0) return
    this.throughSeq = String(event.seq)
    if (event.version !== 1 || !EVENT_TYPES.has(event.type)) {
      this.unsupported = [
        ...this.unsupported,
        { seq: String(event.seq), type: event.type ?? null, version: event.version ?? null },
      ]
      return
    }
    if (event.type === "trajectory.started") this.coverageStart = event.occurred_at
    const system = systemSnapshot(this.records, event)
    if (system) this.create(system)
    for (const [recordId, kind] of targets(event, this.records)) {
      const record = this.records[recordId]
        ? this.mutable(recordId)
        : this.create(newRecord(event, recordId, kind))
      update(record, event)
    }
    if (event.type === "request.finished") {
      const assistantId = `assistant:${pyStr(pyGet(envelope(event), "request_id"))}`
      if (this.records[assistantId]) update(this.mutable(assistantId), event)
    }
    if (event.type === "run.interrupted" || event.type === "recording.gap") this.sweepInterrupted(event)
  }

  private sweepInterrupted(event: TrajectoryEvent): void {
    const runId = pyGet(envelope(event), "run_id")
    for (const [recordId, previous] of Object.entries(this.records)) {
      if (!INTERRUPTIBLE_KINDS.has(previous.kind)) continue
      if ((previous.run_id ?? null) !== runId || TERMINAL.has(previous.status as string)) continue
      const record = this.mutable(recordId)
      record.status = "unknown"
      record.status_reason = "run_interrupted_without_committed_result"
      record.end_seq = String(event.seq)
      record.as_of_seq = String(event.seq)
    }
  }

  close(): ProjectionState {
    if (this.throughSeq === this.base.through_seq) return this.base
    return {
      ...this.base,
      through_seq: this.throughSeq,
      coverage_start: this.coverageStart,
      records: this.records,
      unsupported_events: this.unsupported,
    }
  }
}

/** Apply events in order. Already-applied seqs are ignored; the input is never mutated. */
export function reduceMany(state: ProjectionState, events: readonly TrajectoryEvent[]): ProjectionState {
  if (events.length === 0) return state
  const batch = new Batch(state)
  for (const event of events) batch.apply(event)
  return batch.close()
}

export function reduce(state: ProjectionState, event: TrajectoryEvent): ProjectionState {
  return reduceMany(state, [event])
}

export function replay(
  events: readonly TrajectoryEvent[],
  state: ProjectionState = emptyState(),
): ProjectionState {
  return reduceMany(state, events)
}

/**
 * The raw events the server's record detail returns (repository.get_record):
 * targets without state, within [start_seq, as_of_seq], plus any event of the
 * same request for an assistant record.
 */
export function eventsForRecord(
  record: TrajectoryRecord,
  events: readonly TrajectoryEvent[],
): TrajectoryEvent[] {
  return events.filter((event) => {
    if (cmpSeq(event.seq, record.start_seq) < 0 || cmpSeq(event.seq, record.as_of_seq) > 0) return false
    const ids = event.version === 1 ? targets(event).map(([id]) => id) : []
    return (
      ids.includes(record.record_id) ||
      (record.kind === "assistant" && event.request_id === record.request_id)
    )
  })
}
