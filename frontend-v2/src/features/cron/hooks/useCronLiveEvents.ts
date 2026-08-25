// Keep cron queries fresh from WS lifecycle events instead of leaning on the
// 30s poll. Mounted once wherever cron data is on screen (the status pill).
import { useEffect } from "react"
import { useQueryClient } from "@tanstack/react-query"
import { wsClient } from "@/shared/ws/client"
import { useAuthStore } from "@/shared/api/auth-store"
import { cronKeys } from "@/features/cron/api/keys"

const EVENTS = [
  "cron.job.created",
  "cron.job.updated",
  "cron.job.started",
  "cron.job.completed",
  "cron.job.failed",
  "cron.job.injected",
  "cron.job.auto_disabled",
] as const

export function useCronLiveEvents(): void {
  const qc = useQueryClient()
  const userId = useAuthStore((s) => s.user?.id ?? "anonymous")

  useEffect(() => {
    const offs = EVENTS.map((event) =>
      wsClient.on(event, (data) => {
        void qc.invalidateQueries({ queryKey: cronKeys.jobs(userId) })
        void qc.invalidateQueries({ queryKey: cronKeys.status(userId) })
        const jobId = (data as { jobId?: string }).jobId
        if (jobId) void qc.invalidateQueries({ queryKey: cronKeys.runs(userId, jobId) })
      }),
    )
    return () => offs.forEach((off) => off())
  }, [qc, userId])
}
