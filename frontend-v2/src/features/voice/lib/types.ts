// The call contract (docs/VOICE_CALL_SPEC.md §4.1, §5.3). Event and field names
// are the server's, unchanged, so a frame can be read without a mapping layer.

export type CallStatus = "idle" | "requesting_mic" | "connecting" | "connected" | "ending" | "ended"
export type Phase = "greeting" | "listening" | "thinking" | "speaking" | "working"
export type EndReason =
  | "hangup"
  | "error"
  | "limit"
  | "quota"
  | "concurrent"
  | "mic_lost"
  | "network"
  | "unsupported"
  | "mic_denied"
  | "mic_missing"
  | "mic_busy"
export type TurnState = "accepted" | "working" | "late" | "delivered" | "timeout" | "failed"
export type PhraseKey = "greeting" | "still_working" | "result_in_text" | "limit_reached"
export type LimitReason = "max_duration" | "daily_quota"
/** What an `error` ending was about, when the client knows: one `voice:errors.*` line each. */
export type ErrorKey = "connectFailed" | "disabled" | "backlog" | "assistantUnavailable"

export const COST_ITEMS = ["input_text", "input_audio", "output_text", "output_audio"] as const
export type CostItem = (typeof COST_ITEMS)[number]

/** The meter quantizes money to Decimal strings; numbers are accepted too. */
export type Yuan = string | number

export interface CostSnapshot {
  total_yuan: Yuan
  confirmed_yuan?: Yuan
  provisional_yuan?: Yuan
  costs_yuan?: Partial<Record<CostItem, Yuan>>
  tokens?: Record<string, number>
  settled_rounds?: number
  unreported_rounds?: number
  pending?: boolean
  final?: boolean
  price_date?: string
}

export interface ReadyEvent {
  type: "ready"
  call_id: string
  model: string
  input_sample_rate: number
  output_sample_rate: number
  max_seconds: number
  price_date: string
}

export interface TurnEvent {
  type: "turn"
  turn_id: string
  state: TurnState
  inbox_id?: string | null
  message_id?: string | null
}

export interface EndedEvent {
  type: "ended"
  reason: EndReason
  duration_seconds: number
  pending_turns: number
  cost: CostSnapshot | null
}

export interface ErrorEvent {
  type: "error"
  code: string
  /** Already in the user's language and safe to show. */
  message: string
}

export type ServerEvent =
  | ReadyEvent
  | { type: "phase"; value: Phase; working?: boolean; late?: boolean }
  | { type: "playback.clear" }
  | { type: "phrase"; key: PhraseKey }
  | TurnEvent
  | ({ type: "cost" } & CostSnapshot)
  | { type: "heartbeat"; elapsed_seconds: number }
  | { type: "limit"; reason: LimitReason; elapsed_seconds: number }
  | ErrorEvent
  | EndedEvent

export interface TurnProgress {
  state: TurnState
  messageId?: string
}

export interface CallEnd {
  reason: EndReason
  durationSeconds: number
  pendingTurns: number
  cost: CostSnapshot | null
  errorKey: ErrorKey | null
  /** The server's own `error.message`, shown as is. */
  errorMessage: string | null
}

export interface CallState {
  status: CallStatus
  phase: Phase
  working: boolean
  late: boolean
  /** Model audio is still coming out of the speaker. */
  playing: boolean
  callId: string | null
  /** Epoch ms of `ready`: the call clock starts there. */
  startedAt: number | null
  /** Epoch ms of the hang-up: the clock stops there while the server settles. */
  stoppedAt: number | null
  maxSeconds: number | null
  /** Set by `limit`: the server is saying goodbye and will end the call itself. */
  limit: LimitReason | null
  turns: Record<string, TurnProgress>
  cost: CostSnapshot | null
  ended: CallEnd | null
  /** Live loudness, 0–1, of the microphone and of the model's voice. */
  level: { mic: number; out: number }
}
