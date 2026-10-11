// The owner's "我已核对，继续": after checking the desktop, release a task's
// parked unknown outcomes (POST /api/assistant/tasks/{id}/effects/acknowledge).
// The server never resends anything; it only stops these outcomes from
// blocking resume and automatic continuation.
import { useMutation, useQueryClient } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { assistantKeys, scopedOptions } from "./assistant"

export interface AssistantEffectsAcknowledgement {
  command_id: string
  task_id: string
  execution_session_id: string
  action: "acknowledge_effects"
  state: "applied"
  acknowledged_effects: number
  acknowledged_tool_outcomes: number
  /** Outcomes still in flight or awaiting recovery; the task stays blocked. */
  remaining_unresolved: boolean
  task_revision: number
  desired_state: "running" | "paused" | "canceled"
  observed_state: string
}

export function useAcknowledgeTaskEffects() {
  const userId = useAuthStore((state) => state.user?.id ?? "anonymous")
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (request: { taskId: string; revision: number; key: string }) => http.post<AssistantEffectsAcknowledgement>(
      `/api/assistant/tasks/${encodeURIComponent(request.taskId)}/effects/acknowledge`,
      { idempotency_key: request.key, expected_revision: request.revision }, scopedOptions(workspaceId)),
    onSettled: () => qc.invalidateQueries({ queryKey: assistantKeys.all(userId, workspaceId) }),
  })
}
