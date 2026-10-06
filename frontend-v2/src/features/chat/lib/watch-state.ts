import type { AssistantWatchItem } from "../api/assistant-watch"

export type WatchState = "running" | "queued" | "waiting_input" | "idle" | "error" | "completed"

const LIVE: ReadonlySet<string> = new Set(["busy", "finalizing", "retry", "compacting"])
const TROUBLE: ReadonlySet<string> = new Set(["error", "resume_blocked", "effect_unknown"])

/** One dot per watched conversation. A question waiting on the user comes
 *  first; then the conversation's live status; the task's own record says
 *  whether a quiet conversation finished, failed or is simply idle. */
export function watchState(item: Pick<AssistantWatchItem, "session_status" | "observed_state" | "pending_questions">): WatchState {
  if (item.pending_questions > 0 || item.session_status === "waiting_input") return "waiting_input"
  if (LIVE.has(item.session_status)) return "running"
  if (item.session_status === "queued") return "queued"
  if (item.session_status === "error") return "error"
  if (item.observed_state === "running") return "running"
  if (item.observed_state === "queued") return "queued"
  if (item.observed_state === "waiting_input") return "waiting_input"
  if (TROUBLE.has(item.observed_state)) return "error"
  if (item.observed_state === "completed") return "completed"
  return "idle"
}

/** Status dot colours, token-driven like the scheduled-task dots. */
export const WATCH_DOT: Record<WatchState, string> = {
  running: "bg-accent animate-pulse",
  queued: "bg-a300",
  waiting_input: "bg-a700",
  idle: "bg-n400",
  error: "bg-danger",
  completed: "bg-sage",
}
