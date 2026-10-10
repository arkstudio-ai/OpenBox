import type { ToolPart } from "@/shared/types/api"

/** Only canonical successful write-tool receipts create task cards. Prose is never parsed as an ID. */
export function taskReceipt(part: ToolPart): { taskId: string; commandId: string } | null {
  if (!["tasks.submit", "tasks.followup", "tasks.next_step", "tasks.pause", "tasks.resume", "tasks.cancel", "tasks.link_existing", "assets.attach", "schedules.run"].includes(part.tool) || part.status !== "completed" || !part.output) return null
  try {
    const value: unknown = JSON.parse(part.output)
    if (!value || typeof value !== "object" || !("task_id" in value) || !("command_id" in value) || !("state" in value)) return null
    const continuationStates: Record<string, string> = { continue: "accepted", complete: "completed", needs_decision: "needs_decision" }
    const expected = part.tool === "tasks.next_step"
      ? ("decision" in value && typeof value.decision === "string" ? continuationStates[value.decision] : undefined)
      : part.tool === "tasks.link_existing" ? "linked" : "accepted"
    return typeof value.task_id === "string" && typeof value.command_id === "string"
      && expected !== undefined && value.state === expected
      ? { taskId: value.task_id, commandId: value.command_id } : null
  } catch { return null }
}
