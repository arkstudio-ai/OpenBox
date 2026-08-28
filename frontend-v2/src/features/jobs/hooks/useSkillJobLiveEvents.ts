// WS events are invalidation signals only: the snapshot GET stays the source
// of truth, so a duplicate or out-of-order event costs one refetch at most.
import { useEffect } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { wsClient } from "@/shared/ws/client"
import { useAuthStore } from "@/shared/api/auth-store"
import { jobKeys } from "@/features/jobs/api/keys"

export function useSkillJobLiveEvents(): void {
  const qc = useQueryClient()
  const userId = useAuthStore((s) => s.user?.id ?? "anonymous")

  useEffect(() => {
    return wsClient.on("skill.job.event", (data) => {
      void qc.invalidateQueries({ queryKey: jobKeys.all(userId) })
      if (data.jobId) {
        void qc.invalidateQueries({ queryKey: jobKeys.detail(userId, data.jobId) })
        void qc.invalidateQueries({ queryKey: jobKeys.artifacts(userId, data.jobId) })
      }
    })
  }, [qc, userId])
}
