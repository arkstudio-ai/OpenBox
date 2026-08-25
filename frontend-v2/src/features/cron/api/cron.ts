// Cron data hooks. Components never fetch directly (ENGINEERING_SPEC §7).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import type {
  CronJob,
  CronJobCreateInput,
  CronJobUpdateInput,
  CronRun,
  CronStatus,
} from "@/features/cron/types"
import { cronKeys } from "./keys"

function useUserId(): string {
  return useAuthStore((s) => s.user?.id ?? "anonymous")
}

export function useCronJobs() {
  const userId = useUserId()
  return useQuery({
    queryKey: cronKeys.jobs(userId),
    queryFn: () => http.get<CronJob[]>("/api/cron/jobs"),
    // A running job flips status server-side without a client action.
    refetchInterval: 30_000,
  })
}

export function useCronStatus() {
  const userId = useUserId()
  return useQuery({
    queryKey: cronKeys.status(userId),
    queryFn: () => http.get<CronStatus>("/api/cron/status"),
    refetchInterval: 60_000,
  })
}

export function useCronRuns(jobId: string, enabled: boolean) {
  const userId = useUserId()
  return useQuery({
    queryKey: cronKeys.runs(userId, jobId),
    queryFn: () => http.get<CronRun[]>(`/api/cron/jobs/${jobId}/runs`),
    enabled,
  })
}

function useInvalidateCron() {
  const userId = useUserId()
  const qc = useQueryClient()
  return () => {
    void qc.invalidateQueries({ queryKey: cronKeys.jobs(userId) })
    void qc.invalidateQueries({ queryKey: cronKeys.status(userId) })
  }
}

export function useCreateCronJob() {
  const invalidate = useInvalidateCron()
  return useMutation({
    mutationFn: (input: CronJobCreateInput) =>
      http.post<{ id: string; next_run_at: string | null }>("/api/cron/jobs", input),
    onSuccess: invalidate,
  })
}

export function useUpdateCronJob() {
  const invalidate = useInvalidateCron()
  return useMutation({
    mutationFn: ({ jobId, patch }: { jobId: string; patch: CronJobUpdateInput }) =>
      http.patch<{ ok: boolean }>(`/api/cron/jobs/${jobId}`, patch),
    onSuccess: invalidate,
  })
}

export function useDeleteCronJob() {
  const invalidate = useInvalidateCron()
  return useMutation({
    mutationFn: (jobId: string) => http.delete<{ ok: boolean }>(`/api/cron/jobs/${jobId}`),
    onSuccess: invalidate,
  })
}

export function useRunCronJob() {
  const userId = useUserId()
  const qc = useQueryClient()
  const invalidate = useInvalidateCron()
  return useMutation({
    mutationFn: (jobId: string) =>
      http.post<{ ok: boolean; status?: string; reason?: string }>(`/api/cron/jobs/${jobId}/run`),
    onSuccess: (_data, jobId) => {
      invalidate()
      void qc.invalidateQueries({ queryKey: cronKeys.runs(userId, jobId) })
    },
  })
}

export function usePauseAllCronJobs() {
  const invalidate = useInvalidateCron()
  return useMutation({
    mutationFn: () => http.post<{ ok: boolean; paused: number }>("/api/cron/jobs/pause-all"),
    onSuccess: invalidate,
  })
}

export function useResumeAllCronJobs() {
  const invalidate = useInvalidateCron()
  return useMutation({
    mutationFn: () => http.post<{ ok: boolean; resumed: number }>("/api/cron/jobs/resume-all"),
    onSuccess: invalidate,
  })
}
