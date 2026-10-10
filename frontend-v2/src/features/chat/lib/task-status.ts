// One plain-words status for a task the personal assistant follows, the way a
// secretary would put it: is it waiting on you, still going, or done. The
// detailed task/run/delivery states stay on the server; the page shows this.
import type { StatusTone } from "@/shared/ui/StatusPill"

export type TaskStatus = "waiting" | "running" | "queued" | "paused" | "done" | "failed" | "stopped" | "idle"

export interface TaskStatusInput {
  /** The conversation's live status (busy, waiting_input, idle, …). */
  sessionStatus?: string | null
  /** The task's own record (running, paused, completed, error, …). */
  observedState?: string | null
  desiredState?: string | null
  pendingQuestions?: number
  /** Outcome of the latest finished run: succeeded / error / aborted. */
  outcome?: string | null
}

const LIVE: ReadonlySet<string> = new Set(["busy", "finalizing", "retry", "compacting"])
const TROUBLE: ReadonlySet<string> = new Set(["error", "resume_blocked", "effect_unknown"])

/** States that win over whatever the run is doing: stopped, waiting on you, paused. */
function heldStatus({ sessionStatus, observedState, desiredState, pendingQuestions = 0 }: TaskStatusInput): TaskStatus | null {
  if (desiredState === "canceled" || observedState === "canceled" || observedState === "canceling") return "stopped"
  if (pendingQuestions > 0 || sessionStatus === "waiting_input" || observedState === "waiting_input") return "waiting"
  if (desiredState === "paused" || observedState === "paused" || observedState === "pausing") return "paused"
  return null
}

/** A live run or queue position, read from the conversation and the task record. */
function liveStatus({ sessionStatus, observedState }: TaskStatusInput): TaskStatus | null {
  if (sessionStatus && LIVE.has(sessionStatus)) return "running"
  if (observedState === "running" || observedState === "resuming") return "running"
  if (sessionStatus === "queued" || observedState === "queued") return "queued"
  return null
}

const OUTCOME: Record<string, TaskStatus> = { succeeded: "done", error: "failed", aborted: "stopped" }

export function taskStatus(input: TaskStatusInput): TaskStatus {
  const { sessionStatus, observedState, outcome } = input
  const held = heldStatus(input) ?? liveStatus(input)
  if (held) return held
  if (sessionStatus === "error" || (observedState && TROUBLE.has(observedState))) return "failed"
  if (observedState === "aborted") return "stopped"
  if (observedState === "completed") return outcome === "error" ? "failed" : "done"
  return (outcome && OUTCOME[outcome]) || "idle"
}

/** Still something to wait for or act on. */
export function isActiveTask(status: TaskStatus): boolean {
  return status === "waiting" || status === "running" || status === "queued" || status === "paused"
}

export const TASK_STATUS_TONE: Record<TaskStatus, StatusTone> = {
  waiting: "warn",
  running: "accent",
  queued: "muted",
  paused: "muted",
  done: "ok",
  failed: "danger",
  stopped: "muted",
  idle: "muted",
}

/** Status dot colours, token-driven like the scheduled-task dots. */
export const TASK_STATUS_DOT: Record<TaskStatus, string> = {
  waiting: "bg-accent",
  running: "bg-accent animate-pulse",
  queued: "bg-a300",
  paused: "bg-n400",
  done: "bg-sage",
  failed: "bg-danger",
  stopped: "bg-n400",
  idle: "bg-n400",
}

/** "3 小时前" within a week, then a short date; the caller passes its locale so
 *  this stays free of the i18n singleton. */
export function sinceLabel(iso: string, locale?: string): string {
  const date = new Date(iso)
  const diff = (date.getTime() - Date.now()) / 1000
  const abs = Math.abs(diff)
  if (!Number.isFinite(abs)) return ""
  if (abs < 7 * 86_400) {
    const rtf = new Intl.RelativeTimeFormat(locale || undefined, { numeric: "auto" })
    if (abs < 60) return rtf.format(Math.round(diff), "second")
    if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute")
    if (abs < 86_400) return rtf.format(Math.round(diff / 3600), "hour")
    return rtf.format(Math.round(diff / 86_400), "day")
  }
  const year = date.getFullYear() === new Date().getFullYear() ? {} : { year: "numeric" as const }
  return new Intl.DateTimeFormat(locale || undefined, { month: "short", day: "numeric", ...year }).format(date)
}

/** A task's latest reply as plain words for a card preview: markdown marks
 *  (emphasis, code, links, headings, quotes, tables) dropped, text kept. */
export function plainSummary(text: string): string {
  return text
    .replace(/```[^\n]*\n?/g, "")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/`([^`\n]+)`/g, "$1")
    .replace(/(\*\*|__)(.+?)\1/g, "$2")
    .replace(/(^|[\s(（])\*(?!\s)([^*\n]+?)\*(?=[\s).,;:!?，。；：！？）]|$)/gm, "$1$2")
    .replace(/(^|[\s(（])_(?!\s)([^_\n]+?)_(?=[\s).,;:!?，。；：！？）]|$)/gm, "$1$2")
    .replace(/^[ \t]{0,3}#{1,6}[ \t]+/gm, "")
    .replace(/^[ \t]{0,3}>[ \t]?/gm, "")
    .replace(/^[ \t]*\|?[ \t]*:?-{3,}:?[ \t]*(\|[ \t]*:?-{3,}:?[ \t]*)*\|?[ \t]*$/gm, "")
    .replace(/^[ \t]*\|(.*)\|[ \t]*$/gm, (_line, cells: string) => cells.split("|").map((cell) => cell.trim()).filter(Boolean).join(" · "))
    .replace(/^[ \t]*[-*+][ \t]+/gm, "• ")
    .replace(/\n{3,}/g, "\n\n")
    .trim()
}
