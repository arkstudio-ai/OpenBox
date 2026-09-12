// Timeline geometry. Two scales: by sequence (every record gets room, useful
// for dense bursts) and by recorded time (true durations and overlaps). Point
// facts — an input, a resume, a gap — stay points; a missing timestamp is left
// out of the time scale and counted, never drawn at zero.
import { KIND_LANES, type Lane } from "../constants/labels"
import type { Seq } from "../types/protocol"
import type { TimelineScale } from "../stores/view"
import { epochMs } from "./time"
import type { ViewRecord } from "./view"

export const POINT_KINDS = new Set([
  "user",
  "context",
  "baseline",
  "resume",
  "retry",
  "gap",
  "interrupt",
  "late_result",
  "system",
  "tool_catalog",
  "settings",
  "history",
  "plan",
  "todo",
  "skill",
  "artifact",
])

const MIN_WIDTH = 0.004

export interface TimelineItem {
  recordId: string
  kind: string
  lane: Lane
  /** Fractions of the visible span, 0..1. */
  start: number
  end: number
  point: boolean
  /** Still open at this position; the bar runs to the position's clock. */
  open: boolean
  status: string | null
}

export interface TimelineLayout {
  items: TimelineItem[]
  /** Records the time scale cannot place because a timestamp was not recorded. */
  untimed: number
}

interface LayoutOptions {
  scale: TimelineScale
  headSeq: Seq
  /** Recorded time of the position (replay) or now (live). */
  clock: string | null
}

function seqFraction(seq: Seq, head: bigint): number {
  if (head <= 0n) return 0
  const value = BigInt(seq)
  const clamped = value > head ? head : value
  return Number((clamped * 100_000n) / head) / 100_000
}

function laneOf(kind: string): Lane {
  return KIND_LANES[kind] ?? "state"
}

function bySequence(records: readonly ViewRecord[], headSeq: Seq): TimelineLayout {
  const head = BigInt(headSeq)
  const items = records.map((record): TimelineItem => {
    const point = POINT_KINDS.has(record.kind)
    const open = record.end_seq === null && !point
    const start = seqFraction(record.start_seq, head)
    const endSeq = record.end_seq ?? (open ? headSeq : record.start_seq)
    const end = point ? start : Math.max(seqFraction(endSeq, head), start + MIN_WIDTH)
    return {
      recordId: record.record_id,
      kind: record.kind,
      lane: laneOf(record.kind),
      start,
      end,
      point,
      open,
      status: record.status,
    }
  })
  return { items, untimed: 0 }
}

function byDuration(records: readonly ViewRecord[], clock: string | null): TimelineLayout {
  const clockMs = epochMs(clock)
  const timed = records.flatMap((record) => {
    const started = epochMs(record.started_at)
    if (started === null) return []
    const finished = epochMs(record.finished_at)
    return [{ record, started, finished }]
  })
  const starts = timed.map((item) => item.started)
  const ends = timed.map((item) => item.finished ?? clockMs ?? item.started)
  const t0 = starts.length ? Math.min(...starts) : 0
  const t1 = Math.max(t0 + 1, ...ends)
  const span = t1 - t0
  const items = timed.map(({ record, started, finished }): TimelineItem => {
    const point = POINT_KINDS.has(record.kind)
    const open = finished === null && !point && record.end_seq === null
    const start = (started - t0) / span
    const endMs = finished ?? (open ? (clockMs ?? started) : started)
    const end = point ? start : Math.max((endMs - t0) / span, start + MIN_WIDTH)
    return {
      recordId: record.record_id,
      kind: record.kind,
      lane: laneOf(record.kind),
      start,
      end: Math.min(1, end),
      point,
      open,
      status: record.status,
    }
  })
  return { items, untimed: records.length - timed.length }
}

export function layoutTimeline(records: readonly ViewRecord[], options: LayoutOptions): TimelineLayout {
  const layout =
    options.scale === "sequence" ? bySequence(records, options.headSeq) : byDuration(records, options.clock)
  layout.items.sort((a, b) => a.start - b.start || (a.recordId < b.recordId ? -1 : 1))
  return layout
}
