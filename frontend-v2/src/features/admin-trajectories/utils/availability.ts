// Why a field shows what it shows (UI_SPEC §5.2). "Not produced yet",
// "not recorded", "recorded as empty", "absent", "deleted" and "unsupported"
// are different facts and must never collapse onto 0, {} or a blank.
import { isMediaEnvelope, isPayloadEnvelope, type MediaEnvelope, type PayloadRef } from "../types/protocol"
import { TERMINAL } from "./projector"
import { isPlainObject } from "./python"
import type { ViewRecord } from "./view"

export type FieldState =
  | { state: "available"; value: unknown }
  | { state: "payload"; ref: PayloadRef }
  | { state: "pending" }
  | { state: "not_recorded" }
  | { state: "not_applicable" }
  | { state: "empty" }
  /** Captured and confirmed not to exist, e.g. the "before" of a newly created file. */
  | { state: "absent" }
  | { state: "deleted"; reason: string | null }
  | { state: "unsupported" }
  | { state: "corrupt" }

export type FieldStateName = FieldState["state"]

export function isClosed(record: ViewRecord): boolean {
  return record.end_seq !== null || TERMINAL.has(record.status ?? "")
}

function isEmptyValue(value: unknown): boolean {
  if (value === "" || value === null) return true
  if (Array.isArray(value)) return value.length === 0
  return isPlainObject(value) && Object.keys(value).length === 0
}

const WRAPPER_STATES = new Set([
  "available",
  "pending",
  "not_recorded",
  "not_applicable",
  "absent",
  "deleted",
  "unsupported",
  "corrupt",
])

/**
 * A captured value that states its own availability, e.g. a file version
 * `{availability, text, sha256, size_bytes, source}`. `$payload` references
 * are handled separately.
 */
export function isAvailabilityWrapper(
  value: unknown,
): value is { availability: string; reason?: unknown } & Record<string, unknown> {
  return (
    isPlainObject(value) &&
    typeof value.availability === "string" &&
    WRAPPER_STATES.has(value.availability) &&
    !isPayloadEnvelope(value)
  )
}

function fromWrapper(
  value: { availability: string; reason?: unknown } & Record<string, unknown>,
): FieldState {
  switch (value.availability) {
    case "available":
      return { state: "available", value }
    case "deleted":
      return { state: "deleted", reason: typeof value.reason === "string" ? value.reason : null }
    case "pending":
    case "not_recorded":
    case "not_applicable":
    case "absent":
    case "unsupported":
    case "corrupt":
      return { state: value.availability }
    default:
      return { state: "unsupported" }
  }
}

/**
 * A `$media` wrapper as a field state: a protected payload when retained,
 * otherwise the stated reason. Never a URL or inline bytes.
 */
export function mediaState(value: MediaEnvelope): FieldState {
  const ref = value.$media
  const reason = typeof ref.reason === "string" ? ref.reason : null
  switch (ref.availability ?? "available") {
    case "deleted":
      return { state: "deleted", reason }
    case "corrupt":
      return { state: "corrupt" }
    case "unsupported":
      return { state: "unsupported" }
    case "pending":
      return { state: "pending" }
    case "available":
      return typeof ref.payload_id === "string" && ref.payload_id
        ? {
            state: "payload",
            ref: {
              ...ref,
              payload_id: ref.payload_id,
              media_type: ref.media_type ?? value.declared_media_type ?? null,
            },
          }
        : { state: "not_recorded" }
    default:
      return { state: "not_recorded" }
  }
}

/**
 * Classify one captured value. `awaitsResult` marks fields that an open record
 * may still produce (output, answers…): absent — or present but still empty —
 * on an open record means "pending"; only a closed record confirms emptiness.
 */
export function classifyValue(
  record: ViewRecord,
  present: boolean,
  value: unknown,
  awaitsResult = false,
): FieldState {
  const open = !isClosed(record)
  if (!present) return awaitsResult && open ? { state: "pending" } : { state: "not_recorded" }
  if (isPayloadEnvelope(value)) {
    const availability = value.$payload.availability ?? "available"
    if (availability === "deleted") return { state: "deleted", reason: value.$payload.reason ?? null }
    if (availability === "corrupt") return { state: "corrupt" }
    if (availability === "unsupported") return { state: "unsupported" }
    return { state: "payload", ref: value.$payload }
  }
  if (isMediaEnvelope(value)) return mediaState(value)
  if (isAvailabilityWrapper(value)) return fromWrapper(value)
  if (isEmptyValue(value)) return awaitsResult && open ? { state: "pending" } : { state: "empty" }
  return { state: "available", value }
}

export function fieldState(record: ViewRecord, key: string, awaitsResult = false): FieldState {
  const data = record.data
  if (!data) return { state: "not_recorded" }
  const present = Object.prototype.hasOwnProperty.call(data, key)
  return classifyValue(record, present, data[key], awaitsResult)
}

/** First captured key among aliases, e.g. `system` vs `instructions`. */
export function firstField(record: ViewRecord, keys: readonly string[], awaitsResult = false): FieldState {
  for (const key of keys) {
    if (record.data && Object.prototype.hasOwnProperty.call(record.data, key))
      return fieldState(record, key, awaitsResult)
  }
  return classifyValue(record, false, undefined, awaitsResult)
}
