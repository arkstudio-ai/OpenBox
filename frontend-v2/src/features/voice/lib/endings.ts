// How a call ends when the server did not say (spec §5.5): a socket close code
// becomes the reason and copy the ended panel shows.
import type { EndReason, ErrorKey } from "./types"

export interface Ending {
  reason: EndReason
  errorKey?: ErrorKey
}

/** What a close means when no `ended` came first. */
export function closeEnding(code: number, ready: boolean): Ending {
  switch (code) {
    case 4009:
      return { reason: "concurrent" }
    case 4029:
      return { reason: "quota" }
    case 4503:
      return { reason: "error", errorKey: "disabled" }
    case 4404:
      return { reason: "error", errorKey: "assistantUnavailable" }
    case 4001:
    case 4003:
    case 1011:
      return { reason: "error", errorKey: "connectFailed" }
    case 4400:
      return { reason: "error" }
    default:
      if (!ready) return { reason: "error", errorKey: "connectFailed" }
      // A clean close the server did not explain is not a network failure.
      return code === 1000 ? { reason: "error" } : { reason: "network" }
  }
}

/** Endings the user meant or expected; everything else gets the error tone. */
export function isNormalEnding(reason: EndReason): boolean {
  return reason === "hangup" || reason === "limit"
}
