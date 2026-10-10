// What the call window shows, as a pure function of what happened. The session
// decides when things happen (sockets, timers, audio); this decides what they
// mean for the screen, so every transition can be tested without either.
import type {
  CallEnd,
  CallState,
  CallStatus,
  EndReason,
  ErrorKey,
  Phase,
  ServerEvent,
  TurnState,
} from "./types"

export type CallAction =
  | { type: "start" }
  | { type: "mic_granted" }
  | { type: "server"; event: ServerEvent; at: number }
  | { type: "playing"; playing: boolean }
  | { type: "level"; mic: number; out: number }
  | { type: "hang_up"; at: number }
  | { type: "end"; reason: EndReason; at: number; errorKey?: ErrorKey }
  | { type: "dismiss" }

const SILENT = { mic: 0, out: 0 }

export const initialCall: CallState = {
  status: "idle",
  phase: "greeting",
  working: false,
  late: false,
  playing: false,
  callId: null,
  startedAt: null,
  stoppedAt: null,
  maxSeconds: null,
  limit: null,
  turns: {},
  cost: null,
  ended: null,
  level: SILENT,
}

const LIVE: ReadonlySet<CallStatus> = new Set(["requesting_mic", "connecting", "connected", "ending"])
// A newer server may send values this client has no copy for: unknown phases
// are ignored and unknown endings read as an error (spec §5.3).
const PHASES: ReadonlySet<string> = new Set<Phase>([
  "greeting",
  "listening",
  "thinking",
  "speaking",
  "working",
])
const END_REASONS: ReadonlySet<string> = new Set<EndReason>([
  "hangup",
  "error",
  "limit",
  "quota",
  "concurrent",
  "mic_lost",
  "network",
  "unsupported",
  "mic_denied",
  "mic_missing",
  "mic_busy",
])
/** Turns the assistant is still working on: what "还有 n 件事在办" counts. */
const OPEN_TURNS: ReadonlySet<TurnState> = new Set(["accepted", "working", "late"])

export function isLive(status: CallStatus): boolean {
  return LIVE.has(status)
}

/** The phase to show. The server says `listening` once the model has finished
 *  generating, which is seconds before its last words leave the speaker. */
export function displayPhase(call: Pick<CallState, "phase" | "playing">): Phase {
  return call.playing && call.phase === "listening" ? "speaking" : call.phase
}

export function callReducer(state: CallState, action: CallAction): CallState {
  switch (action.type) {
    case "start":
      return { ...initialCall, status: "requesting_mic" }
    case "mic_granted":
      return state.status === "requesting_mic" ? { ...state, status: "connecting" } : state
    case "server":
      return isLive(state.status) ? onServerEvent(state, action.event, action.at) : state
    case "playing":
      return isLive(state.status) && state.playing !== action.playing
        ? { ...state, playing: action.playing }
        : state
    case "level":
      return state.status === "connected" &&
        (state.level.mic !== action.mic || state.level.out !== action.out)
        ? { ...state, level: { mic: action.mic, out: action.out } }
        : state
    case "hang_up":
      return state.status === "connected"
        ? { ...state, status: "ending", stoppedAt: action.at, playing: false, level: SILENT }
        : state
    case "end":
      return isLive(state.status)
        ? toEnded(state, localEnd(state, action.at, { reason: action.reason, errorKey: action.errorKey }))
        : state
    case "dismiss":
      return state.status === "ended" ? initialCall : state
  }
}

function onServerEvent(state: CallState, event: ServerEvent, at: number): CallState {
  switch (event.type) {
    case "ready":
      return state.status === "connecting"
        ? {
            ...state,
            status: "connected",
            phase: "greeting",
            callId: event.call_id,
            maxSeconds: event.max_seconds ?? null,
            startedAt: at,
          }
        : state
    case "phase":
      return state.status === "connected" && PHASES.has(event.value)
        ? { ...state, phase: event.value, working: event.working === true, late: event.late === true }
        : state
    case "turn": {
      const messageId = event.message_id ?? state.turns[event.turn_id]?.messageId
      const turn = messageId ? { state: event.state, messageId } : { state: event.state }
      return { ...state, turns: { ...state.turns, [event.turn_id]: turn } }
    }
    case "cost":
      return { ...state, cost: event }
    case "limit":
      return { ...state, limit: event.reason }
    case "error":
      return toEnded(state, localEnd(state, at, { reason: "error", errorMessage: event.message || null }))
    case "ended":
      return toEnded(state, {
        reason: END_REASONS.has(event.reason) ? event.reason : "error",
        durationSeconds: event.duration_seconds ?? 0,
        pendingTurns: event.pending_turns ?? 0,
        cost: event.cost ?? state.cost,
        errorKey: null,
        errorMessage: null,
      })
    default:
      // Unknown events are ignored (spec §5.3); heartbeats only keep the line alive.
      return state
  }
}

function toEnded(state: CallState, ended: CallEnd): CallState {
  return { ...state, status: "ended", playing: false, level: SILENT, ended }
}

interface EndCause {
  reason: EndReason
  errorKey?: ErrorKey | null
  errorMessage?: string | null
}

/** An ending the server did not describe: measured and counted from what the client saw. */
function localEnd(
  state: CallState,
  at: number,
  { reason, errorKey = null, errorMessage = null }: EndCause,
): CallEnd {
  // After the limit announcement the server is the one hanging up; a close that
  // beats its `ended` frame is still that ending, not a dropped call.
  const limited =
    state.limit !== null && errorMessage === null && (reason === "network" || reason === "error")
  const until = state.stoppedAt ?? at
  return {
    reason: limited ? "limit" : reason,
    durationSeconds: state.startedAt === null ? 0 : Math.max(0, Math.round((until - state.startedAt) / 1000)),
    pendingTurns: Object.values(state.turns).filter((turn) => OPEN_TURNS.has(turn.state)).length,
    cost: state.cost,
    errorKey: limited ? null : errorKey,
    errorMessage,
  }
}
