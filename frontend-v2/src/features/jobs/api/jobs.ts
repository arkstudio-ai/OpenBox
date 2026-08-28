// Skill job data hooks. Components never fetch directly (ENGINEERING_SPEC §7).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import type { SkillJobArtifact, SkillJobSnapshot } from "@/features/jobs/types"
import { jobKeys } from "./keys"

function useUserId(): string {
  return useAuthStore((s) => s.user?.id ?? "anonymous")
}

export function useSessionSkillJobs(sessionId: string) {
  const userId = useUserId()
  return useQuery({
    queryKey: jobKeys.session(userId, sessionId),
    queryFn: () =>
      http.get<{ jobs: SkillJobSnapshot[] }>(
        `/api/skill-jobs?session_id=${encodeURIComponent(sessionId)}`,
      ),
    enabled: Boolean(sessionId),
    select: (data) => data.jobs,
    // Waiting/running states advance server-side; WS invalidation is the fast
    // path and this poll is the correctness backstop after a lost socket.
    refetchInterval: 30_000,
  })
}

export function useSkillJobArtifacts(jobId: string, enabled: boolean) {
  const userId = useUserId()
  return useQuery({
    queryKey: jobKeys.artifacts(userId, jobId),
    queryFn: () => http.get<{ artifacts: SkillJobArtifact[] }>(`/api/skill-jobs/${jobId}/artifacts`),
    select: (data) => data.artifacts,
    enabled,
  })
}

export function useInvalidateSkillJobs() {
  const userId = useUserId()
  const qc = useQueryClient()
  return (jobId?: string) => {
    void qc.invalidateQueries({ queryKey: jobKeys.all(userId) })
    if (jobId) void qc.invalidateQueries({ queryKey: jobKeys.detail(userId, jobId) })
  }
}

export function useCancelSkillJob() {
  const invalidate = useInvalidateSkillJobs()
  return useMutation({
    mutationFn: (jobId: string) =>
      http.post<{ job: SkillJobSnapshot }>(`/api/skill-jobs/${jobId}/cancel`, {}),
    onSuccess: (_data, jobId) => invalidate(jobId),
  })
}

export function useAnswerSkillJob() {
  const invalidate = useInvalidateSkillJobs()
  return useMutation({
    mutationFn: ({
      jobId,
      payload,
      idempotencyKey,
    }: {
      jobId: string
      payload: Record<string, unknown>
      idempotencyKey: string
    }) =>
      http.post<{ inputId: string; created: boolean }>(`/api/skill-jobs/${jobId}/inputs`, {
        payload,
        idempotency_key: idempotencyKey,
      }),
    onSuccess: (_data, vars) => invalidate(vars.jobId),
  })
}
