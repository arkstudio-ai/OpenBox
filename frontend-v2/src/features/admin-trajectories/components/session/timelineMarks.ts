// What the timeline actually draws. Up to a few hundred records every record
// is its own mark; beyond that, marks in the same lane and column merge so ten
// thousand tool calls stay a readable, focusable density strip instead of ten
// thousand buttons. A merged mark keeps every record id it stands for.
import type { Lane } from "../../constants/labels"
import type { TimelineItem } from "../../utils/timeline"

export const MERGE_THRESHOLD = 600
export const COLUMNS = 360

export interface TimelineMark {
  key: string
  lane: Lane
  start: number
  end: number
  point: boolean
  open: boolean
  /** Worst status among the merged records, for the danger / unknown outline. */
  tone: "danger" | "warn" | "normal"
  recordIds: string[]
}

const DANGER = new Set(["failed", "denied", "timed_out"])

function toneOf(status: string | null): TimelineMark["tone"] {
  if (status && DANGER.has(status)) return "danger"
  return status === "unknown" ? "warn" : "normal"
}

function worse(a: TimelineMark["tone"], b: TimelineMark["tone"]): TimelineMark["tone"] {
  if (a === "danger" || b === "danger") return "danger"
  return a === "warn" || b === "warn" ? "warn" : "normal"
}

function single(item: TimelineItem): TimelineMark {
  return {
    key: item.recordId,
    lane: item.lane,
    start: item.start,
    end: item.end,
    point: item.point,
    open: item.open,
    tone: toneOf(item.status),
    recordIds: [item.recordId],
  }
}

export function timelineMarks(items: readonly TimelineItem[]): TimelineMark[] {
  if (items.length <= MERGE_THRESHOLD) return items.map(single)
  const columns = new Map<string, TimelineMark>()
  for (const item of items) {
    const column = Math.min(COLUMNS - 1, Math.floor(item.start * COLUMNS))
    const key = `${item.lane}:${column}`
    const existing = columns.get(key)
    if (!existing) {
      columns.set(key, { ...single(item), key })
      continue
    }
    existing.end = Math.max(existing.end, item.end)
    existing.point = existing.point && item.point
    existing.open = existing.open || item.open
    existing.tone = worse(existing.tone, toneOf(item.status))
    existing.recordIds.push(item.recordId)
  }
  return [...columns.values()].sort((a, b) => a.start - b.start || (a.key < b.key ? -1 : 1))
}
