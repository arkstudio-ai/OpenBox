// File-change artifacts (`artifact.recorded`, `artifact_type: "file_diff"`,
// captured by the executor at a successful write). The record holds the latest
// version; each earlier `artifact.recorded` event for the same artifact is a
// revision, so the history comes from the event log, bounded by the position.
import type { TrajectoryEvent } from "../types/protocol"
import { classifyValue, isClosed, type FieldState } from "./availability"
import { eventsForRecord } from "./projector"
import { isPlainObject } from "./python"
import type { ViewRecord } from "./view"

export const FILE_DIFF = "file_diff"

export interface FileVersion {
  field: FieldState
  text: string | null
  sha256: string | null
  sizeBytes: number | null
  source: string | null
  /** The retained text differs from what the executor saw (secrets removed). */
  redacted: boolean
}

export interface FileRevision {
  seq: string
  occurredAt: string
  operation: string | null
  before: FileVersion
  after: FileVersion
  diff: FieldState
}

export interface FileDiffArtifact extends Omit<FileRevision, "seq" | "occurredAt"> {
  path: string | null
  name: string | null
  captureLevel: string | null
}

export function isFileDiff(record: ViewRecord): boolean {
  return record.kind === "artifact" && record.data?.artifact_type === FILE_DIFF
}

const str = (value: unknown) => (typeof value === "string" ? value : null)
const num = (value: unknown) => (typeof value === "number" && Number.isFinite(value) ? value : null)

function version(record: ViewRecord, data: Record<string, unknown>, key: "before" | "after"): FileVersion {
  const present = Object.prototype.hasOwnProperty.call(data, key)
  const value = data[key]
  const wrapper = isPlainObject(value) ? value : {}
  return {
    // A missing version is "not recorded"; never an empty file.
    field: classifyValue(record, present && value !== null, value),
    text: str(wrapper.text),
    sha256: str(wrapper.sha256),
    sizeBytes: num(wrapper.size_bytes),
    source: str(wrapper.source),
    redacted: wrapper.redacted === true,
  }
}

function diffField(record: ViewRecord, data: Record<string, unknown>): FieldState {
  const present = Object.prototype.hasOwnProperty.call(data, "diff") && data.diff !== null
  // Closed artifact: a missing diff (no before/after pair to compare) is not recorded, not pending.
  return classifyValue({ ...record, end_seq: record.end_seq ?? record.as_of_seq }, present, data.diff)
}

function revisionFrom(record: ViewRecord, data: Record<string, unknown>) {
  return {
    operation: str(data.operation),
    before: version(record, data, "before"),
    after: version(record, data, "after"),
    diff: diffField(record, data),
  }
}

export function fileDiffOf(record: ViewRecord): FileDiffArtifact | null {
  if (!isFileDiff(record) || !record.data) return null
  const data = record.data
  return {
    ...revisionFrom(record, data),
    path: str(data.path),
    name: str(data.name),
    captureLevel: str(data.capture_level),
  }
}

/** Every recorded revision up to the record's position, oldest first. */
export function fileRevisions(record: ViewRecord, events: readonly TrajectoryEvent[]): FileRevision[] {
  if (!isFileDiff(record)) return []
  const closed = isClosed(record) ? record : { ...record, end_seq: record.as_of_seq }
  return eventsForRecord(closed as Parameters<typeof eventsForRecord>[0], events)
    .filter((event) => event.type === "artifact.recorded" && isPlainObject(event.data))
    .map((event) => ({ seq: event.seq, occurredAt: event.occurred_at, ...revisionFrom(closed, event.data) }))
}
