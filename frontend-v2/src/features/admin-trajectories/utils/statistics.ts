// Port of projector.statistics / contribution / agents. Usage is counted once
// per request record (record ids are request-scoped), so a retry is a second
// request and a delta never adds a second copy of the same usage.
import type { AgentSummary, ProjectionState, TrajectoryRecord, TrajectoryStatistics } from "../types/protocol"
import { ERROR_STATUSES } from "./projector"
import { elapsedMs, isPlainObject } from "./python"

interface Contribution {
  request_count: number
  tool_count: number
  error_count: number
  unknown_count: number
  input_tokens: number
  output_tokens: number
  usage_missing: number
}

function tokenValue(usage: unknown, names: readonly string[]): number | null {
  if (!isPlainObject(usage)) return null
  for (const name of names) {
    const value = usage[name]
    if (typeof value === "number") return value
  }
  return null
}

export function contribution(record: TrajectoryRecord | null): Contribution {
  const stats: Contribution = {
    request_count: 0,
    tool_count: 0,
    error_count: 0,
    unknown_count: 0,
    input_tokens: 0,
    output_tokens: 0,
    usage_missing: 0,
  }
  if (!record || (record.kind !== "request" && record.kind !== "tool")) return stats
  if (record.kind === "request") stats.request_count = 1
  else stats.tool_count = 1
  stats.error_count = ERROR_STATUSES.has(record.status as string) ? 1 : 0
  stats.unknown_count = record.status === "unknown" ? 1 : 0
  if (record.kind === "request") {
    const input = tokenValue(record.usage, ["input_tokens", "prompt_tokens", "input"])
    const output = tokenValue(record.usage, ["output_tokens", "completion_tokens", "output"])
    stats.input_tokens = input || 0
    stats.output_tokens = output || 0
    stats.usage_missing = input === null || output === null ? 1 : 0
  }
  return stats
}

/** Union of run intervals; overlapping runs are not double-counted. */
function runDuration(records: readonly TrajectoryRecord[]): number | null {
  const intervals: Array<[string, string]> = []
  for (const record of records) {
    if (record.kind === "run" && record.finished_at && record.started_at)
      intervals.push([record.started_at, record.finished_at])
  }
  if (!intervals.length) return null
  intervals.sort((a, b) => (a[0] === b[0] ? (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0) : a[0] < b[0] ? -1 : 1))
  const merged: Array<[string, string]> = []
  for (const [start, end] of intervals) {
    const last = merged[merged.length - 1]
    if (last && start <= last[1]) last[1] = end > last[1] ? end : last[1]
    else merged.push([start, end])
  }
  return merged.reduce((sum, [start, end]) => sum + Math.max(0, elapsedMs(start, end) ?? 0), 0)
}

export function statistics(state: ProjectionState): TrajectoryStatistics {
  const totals = contribution(null)
  const records = Object.values(state.records)
  for (const record of records) {
    const part = contribution(record)
    for (const key of Object.keys(totals) as Array<keyof Contribution>) totals[key] += part[key]
  }
  const { usage_missing: missing, ...counts } = totals
  const known = counts.request_count > missing
  return {
    ...counts,
    input_tokens: known ? counts.input_tokens : null,
    output_tokens: known ? counts.output_tokens : null,
    usage_complete: !missing,
    duration_ms: runDuration(records),
    through_seq: state.through_seq,
    coverage_start: state.coverage_start,
  }
}

export function agents(state: ProjectionState): AgentSummary[] {
  return Object.values(state.records)
    .filter((record) => record.kind === "agent")
    .map((record) => ({
      agent_id: record.agent_id,
      parent_agent_id: record.parent_agent_id,
      source_session_id: record.source_session_id,
      name: record.title,
      status: record.status,
      record_id: record.record_id,
    }))
}
