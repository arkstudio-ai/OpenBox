// What the personal assistant is doing right now, in words, while a turn runs.
// The raw tool calls stay on the server; the page says "正在查看任务进展…".
import type { ToolLike } from "./turn-view"

const ACTIVITY: Array<[RegExp, string]> = [
  [/^tasks\.(submit|followup|next_step)$/, "delegating"],
  [/^tasks\.(pause|resume|cancel|archive|link_existing)$/, "updatingTask"],
  [/^(tasks\.(list|get)|results\.read|history\.read|sessions\.list|projects\.list)$/, "checkingWork"],
  [/^memory\.remember$|^memory\.update$|^decisions\.propose$/, "remembering"],
  [/^memory\./, "recalling"],
  [/^knowledge\./, "reading"],
  [/^requests\./, "checkingRequests"],
  [/^schedules\.|^briefing\./, "scheduling"],
  [/^projects\.brief\./, "updatingBrief"],
  [/^assets\./, "handlingFiles"],
  [/^status\./, "checkingStatus"],
  [/^sessions\.rename$/, "updatingTask"],
]

/** i18n key suffix under `assistant.activity` for the call in flight, else the last one. */
export function assistantActivity(tools: ToolLike[]): string {
  const running = tools.filter((tool) => tool.status === "running" || tool.status === "pending")
  const current = running[running.length - 1] ?? tools[tools.length - 1]
  if (!current || current.type !== "tool") return "thinking"
  return ACTIVITY.find(([pattern]) => pattern.test(current.tool))?.[1] ?? "working"
}
