// Validating adapter between wire JSON and the projector. A checkpoint that
// fails validation is never partially installed: the caller replays from zero
// instead and reports why. An event that fails validation stops the projection
// at the last good seq — skipping it would silently claim a complete replay.
import {
  ID_FIELDS,
  PROJECTOR_VERSION,
  type CheckpointResponse,
  type ProjectionState,
  type TrajectoryEvent,
  type TrajectoryRecord,
} from "../types/protocol"
import { isPlainObject } from "./python"
import { gtSeq, isSeq, lteSeq } from "./seq"

export type CheckpointRejection = "incompatible_version" | "malformed" | "beyond_target" | "future_watermark"

export type CheckpointResult =
  | { kind: "none" }
  | { kind: "ok"; state: ProjectionState }
  | { kind: "rejected"; reason: CheckpointRejection }

function isNullableString(value: unknown): boolean {
  return value === null || typeof value === "string"
}

function isRecordShape(value: unknown, id: string): value is TrajectoryRecord {
  if (!isPlainObject(value)) return false
  if (value.record_id !== id || typeof value.kind !== "string" || typeof value.title !== "string")
    return false
  if (!isSeq(value.start_seq) || !isSeq(value.as_of_seq)) return false
  if (value.end_seq !== null && !isSeq(value.end_seq)) return false
  if (!isPlainObject(value.data) || !Array.isArray(value.blocks) || !isPlainObject(value.usage)) return false
  if (!isNullableString(value.started_at) || !isNullableString(value.finished_at)) return false
  if (value.duration_ms !== null && typeof value.duration_ms !== "number") return false
  return ID_FIELDS.every((key) => key in value)
}

/** A record claiming a position after the checkpoint's own watermark cannot be part of it. */
function withinWatermark(record: TrajectoryRecord, through: string): boolean {
  if (!lteSeq(record.start_seq, record.as_of_seq) || !lteSeq(record.as_of_seq, through)) return false
  return (
    record.end_seq === null || (lteSeq(record.start_seq, record.end_seq) && lteSeq(record.end_seq, through))
  )
}

export type StateInspection =
  { ok: true; state: ProjectionState } | { ok: false; reason: "malformed" | "future_watermark" }

export function inspectState(raw: unknown): StateInspection {
  if (!isPlainObject(raw) || raw.projector_version !== PROJECTOR_VERSION || !isSeq(raw.through_seq)) {
    return { ok: false, reason: "malformed" }
  }
  if (
    !isPlainObject(raw.records) ||
    !Array.isArray(raw.unsupported_events) ||
    !isNullableString(raw.coverage_start)
  ) {
    return { ok: false, reason: "malformed" }
  }
  const through = raw.through_seq
  let future = false
  for (const [id, record] of Object.entries(raw.records)) {
    if (!isRecordShape(record, id)) return { ok: false, reason: "malformed" }
    if (!withinWatermark(record, through)) future = true
  }
  for (const item of raw.unsupported_events) {
    if (!isPlainObject(item) || !isSeq(item.seq)) return { ok: false, reason: "malformed" }
    if (gtSeq(item.seq, through)) future = true
  }
  return future
    ? { ok: false, reason: "future_watermark" }
    : { ok: true, state: raw as unknown as ProjectionState }
}

export function validateState(raw: unknown): ProjectionState | null {
  const inspected = inspectState(raw)
  return inspected.ok ? inspected.state : null
}

/** Accept a checkpoint only if it is v1, well-formed, internally bounded and not after `atSeq`. */
export function ingestCheckpoint(response: CheckpointResponse, atSeq: string): CheckpointResult {
  const checkpoint = response.checkpoint
  if (!checkpoint) return { kind: "none" }
  if (checkpoint.projector_version !== PROJECTOR_VERSION)
    return { kind: "rejected", reason: "incompatible_version" }
  const inspected = inspectState(checkpoint.state)
  if (!inspected.ok) return { kind: "rejected", reason: inspected.reason }
  if (inspected.state.through_seq !== checkpoint.through_seq) return { kind: "rejected", reason: "malformed" }
  if (gtSeq(inspected.state.through_seq, atSeq)) return { kind: "rejected", reason: "beyond_target" }
  return { kind: "ok", state: inspected.state }
}

export class EventShapeError extends Error {
  constructor(readonly seq: string | null) {
    super(`Malformed trajectory event at seq ${seq ?? "?"}`)
    this.name = "EventShapeError"
  }
}

/**
 * Structural check only: unknown `type`/`version` are valid envelopes and are
 * recorded by the projector as unsupported, exactly like the server does.
 */
export function validateEvent(raw: unknown): TrajectoryEvent {
  if (!isPlainObject(raw) || !isSeq(raw.seq)) {
    throw new EventShapeError(isPlainObject(raw) && typeof raw.seq === "string" ? raw.seq : null)
  }
  const ok =
    typeof raw.event_id === "string" &&
    typeof raw.type === "string" &&
    typeof raw.occurred_at === "string" &&
    typeof raw.session_id === "string" &&
    (raw.data === undefined || isPlainObject(raw.data))
  if (!ok) throw new EventShapeError(raw.seq)
  return raw as unknown as TrajectoryEvent
}
