import type { ToolPart } from "@/shared/types/api"

/** Only canonical successful write-tool receipts create task cards. Prose is never parsed as an ID. */
export function taskReceipt(part: ToolPart): { taskId: string; commandId: string } | null {
  if (!["tasks.submit", "tasks.followup", "tasks.pause", "tasks.resume", "tasks.cancel"].includes(part.tool) || part.status !== "completed" || !part.output) return null
  try {
    const value: unknown = JSON.parse(part.output)
    if (!value || typeof value !== "object" || !("task_id" in value) || !("command_id" in value) || !("state" in value)) return null
    return typeof value.task_id === "string" && typeof value.command_id === "string" && value.state === "accepted"
      ? { taskId: value.task_id, commandId: value.command_id } : null
  } catch { return null }
}
