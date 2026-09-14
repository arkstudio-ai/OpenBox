// Replay navigation over the locally known event sequence. Positions move by
// seq; recorded time only paces playback. A negative gap between two events
// (clocks of different processes) plays as zero, and "skip idle" caps long
// waits so a session that paused for an hour does not stall the replay.
import type { Seq, TrajectoryEvent } from "../types/protocol"
import { epochMs } from "./time"
import { addSeq, gtSeq, lastIndexAtOrBefore, ltSeq } from "./seq"

export const IDLE_CAP_MS = 400
export const MIN_FRAME_MS = 16

const STEP_TYPES = new Set(["step.started"])
const FALLBACK_BOUNDARIES = new Set(["turn.started", "request.started"])

type Events = readonly TrajectoryEvent[]

/** The next position one event later. Seqs are contiguous, so this never skips. */
export function nextEventSeq(current: Seq, head: Seq): Seq | null {
  return ltSeq(current, head) ? addSeq(current, 1) : null
}

export function previousEventSeq(current: Seq, floor: Seq = "1"): Seq | null {
  return gtSeq(current, floor) ? addSeq(current, -1) : null
}

function boundaryTypes(events: Events): ReadonlySet<string> {
  return events.some((event) => STEP_TYPES.has(event.type)) ? STEP_TYPES : FALLBACK_BOUNDARIES
}

/** Seq of the next Step boundary after `current`, or null. */
export function nextStepSeq(events: Events, current: Seq): Seq | null {
  const types = boundaryTypes(events)
  const start = lastIndexAtOrBefore(events, current, (event) => event.seq) + 1
  for (let index = start; index < events.length; index += 1) {
    if (types.has(events[index].type)) return events[index].seq
  }
  return null
}

/** Seq of the Step boundary before `current`, or null. */
export function previousStepSeq(events: Events, current: Seq): Seq | null {
  const types = boundaryTypes(events)
  for (let index = lastIndexAtOrBefore(events, current, (event) => event.seq); index >= 0; index -= 1) {
    if (ltSeq(events[index].seq, current) && types.has(events[index].type)) return events[index].seq
  }
  return null
}

export function eventAt(events: Events, seq: Seq): TrajectoryEvent | null {
  const index = lastIndexAtOrBefore(events, seq, (event) => event.seq)
  return index >= 0 && events[index].seq === seq ? events[index] : null
}

export interface DelayOptions {
  rate: number
  skipIdle: boolean
}

/** Wall-clock wait before showing `next` after `previous` at the chosen speed. */
export function playbackDelay(
  previous: TrajectoryEvent | null,
  next: TrajectoryEvent,
  options: DelayOptions,
): number {
  const from = epochMs(previous?.occurred_at)
  const to = epochMs(next.occurred_at)
  let gap = from === null || to === null ? 0 : Math.max(0, to - from)
  if (options.skipIdle) gap = Math.min(gap, IDLE_CAP_MS)
  return Math.max(MIN_FRAME_MS, gap / options.rate)
}

/** Recorded time at a position: what "now" means for open intervals during replay. */
export function clockAt(events: Events, seq: Seq): string | null {
  const index = lastIndexAtOrBefore(events, seq, (event) => event.seq)
  return index >= 0 ? events[index].occurred_at : null
}
