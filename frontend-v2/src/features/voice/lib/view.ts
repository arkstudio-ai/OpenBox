// What the call window shows, derived from the call and its clock.
import { formatClock } from "@/shared/lib/format"
import { LATE_COPY, PHASE_COPY, STATUS_COPY } from "../constants/copy"
import type { CallStatus, CostSnapshot, Phase } from "./types"

/** How long after connecting the "just talk" hint stays. */
const HINT_MS = 10_000
/** Below this much call time left, the window says how many minutes remain. */
const REMAINING_NOTE_SECONDS = 5 * 60

export interface CallFacts {
  status: CallStatus
  phase: Phase
  working: boolean
  late: boolean
  muted: boolean
  startedAt: number | null
  maxSeconds: number | null
  cost: CostSnapshot | null
}

export interface CallView {
  status: CallStatus
  phase: Phase
  muted: boolean
  /** "02:14" from `ready` on. */
  clock: string | null
  /** Copy key for the status line. */
  statusKey: string
  /** Copy key for the small note beside it while a handed-off turn is still being worked on. */
  noteKey: string | null
  remainingMinutes: number | null
  cost: CostSnapshot | null
  showHint: boolean
}

export interface CallControls {
  onCollapse: () => void
  onExpand: () => void
  onToggleMute: () => void
  onHangUp: () => void
}

function statusKey(call: CallFacts): string {
  if (call.status === "connected")
    return call.phase === "working" && call.late ? LATE_COPY : PHASE_COPY[call.phase]
  if (call.status === "requesting_mic" || call.status === "connecting" || call.status === "ending")
    return STATUS_COPY[call.status]
  return PHASE_COPY.greeting
}

export function callView(call: CallFacts, elapsedMs: number): CallView {
  const connected = call.status === "connected"
  const clocked = call.startedAt !== null && (connected || call.status === "ending")
  const left = call.maxSeconds === null ? null : call.maxSeconds - elapsedMs / 1000
  // The phase leads; a turn still being worked on while the conversation goes
  // on is the side note (spec §4.1). In the working phase it would say it twice.
  const working = connected && call.working && call.phase !== "working"
  return {
    status: call.status,
    phase: call.phase,
    muted: call.muted,
    clock: clocked ? formatClock(elapsedMs / 1000) : null,
    statusKey: statusKey(call),
    noteKey: working ? (call.late ? LATE_COPY : PHASE_COPY.working) : null,
    remainingMinutes:
      connected && left !== null && left < REMAINING_NOTE_SECONDS ? Math.max(0, Math.ceil(left / 60)) : null,
    cost: call.cost,
    showHint: connected && elapsedMs < HINT_MS,
  }
}
