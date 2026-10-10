// Value → `voice:` copy key tables (ENGINEERING_SPEC §10.3: keys are never built from values).
import type { EndReason, ErrorKey, Phase } from "../lib/types"

export const PHASE_COPY: Record<Phase, string> = {
  greeting: "state.greeting",
  listening: "state.listening",
  thinking: "state.thinking",
  speaking: "state.speaking",
  working: "state.working",
}

/** The status line around the conversation itself. */
export const STATUS_COPY = {
  requesting_mic: "state.requestingMic",
  connecting: "state.connecting",
  ending: "state.ending",
} as const

/** The working note is "still on it" once the server says the turn is late. */
export const LATE_COPY = "state.late"

/** What the ended panel says happened. Refusals before the call (microphone,
 *  browser) read as the matching `errors.*` line. */
export const END_COPY: Record<EndReason, string> = {
  hangup: "ended.hangup",
  error: "ended.error",
  limit: "ended.limit",
  quota: "ended.quota",
  concurrent: "ended.concurrent",
  mic_lost: "ended.micLost",
  network: "ended.network",
  unsupported: "errors.unsupported",
  mic_denied: "errors.micDenied",
  mic_missing: "errors.micMissing",
  mic_busy: "errors.micBusy",
}

export const ERROR_COPY: Record<ErrorKey, string> = {
  connectFailed: "errors.connectFailed",
  disabled: "errors.disabled",
  backlog: "errors.backlog",
  assistantUnavailable: "errors.assistantUnavailable",
}

/** Endings the panel offers to redial; today's quota or a call on another device would only refuse again. */
export const REDIAL_REASONS: ReadonlySet<EndReason> = new Set(["hangup", "error", "network", "limit"])
