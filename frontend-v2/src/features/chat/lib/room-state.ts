// Which word the task-room banner uses for a task's state (VOICE_SEAM_PLAN §11.3).
import type { Session } from "@/shared/types/api"

export type RoomState = "running" | "paused" | "effect_unknown" | "canceled" | "completed" | "error" | "waiting" | "queued"

export function roomState(control: NonNullable<Session["task_control"]>): RoomState {
  const observed = control.observed_state
  if (observed === "effect_unknown") return "effect_unknown"
  if (observed === "waiting_input") return "waiting"
  if (control.desired_state === "canceled" || observed === "canceled") return "canceled"
  if (control.desired_state === "paused" || observed === "paused" || observed === "resume_blocked") return "paused"
  if (observed === "completed") return "completed"
  if (observed === "error") return "error"
  if (observed === "queued") return "queued"
  return "running"
}
