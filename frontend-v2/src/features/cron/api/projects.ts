// Project options for job creation — reuses the workspace "projects" cache
// (same query key shape, ENGINEERING_SPEC §7.2).
import { useQuery } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import type { CronProjectOption } from "@/features/cron/types"

export function useProjectOptions() {
  const userId = useAuthStore((s) => s.user?.id ?? "anonymous")
  return useQuery({
    queryKey: ["projects", userId],
    queryFn: () => http.get<CronProjectOption[]>("/api/agent/project"),
    staleTime: 30_000,
  })
}
