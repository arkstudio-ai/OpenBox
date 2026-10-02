import { useQuery } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"

export interface DebugCapabilities {
  enabled?: boolean
  debug?: boolean
  debug_view?: boolean
  replay?: boolean
  replay_enabled?: boolean
  debug_replay?: boolean
  wiki?: boolean
  [key: string]: unknown
}
export function debugCapabilityState(capabilities?: DebugCapabilities) {
  const disabled =
    capabilities?.enabled === false || capabilities?.debug === false || capabilities?.debug_view === false
  const replayAllowed =
    (capabilities?.debug_replay ?? capabilities?.replay ?? capabilities?.replay_enabled) === true && !disabled
  return { disabled, replayAllowed }
}
export interface DebugRun {
  id: string
  run_id?: string
  request_id: string
  turn_id?: string | null
  attempt_id?: string | null
  session_id?: string | null
  project_id?: string | null
  status: string
  created_at: string
  expires_at?: string | null
  parent_run_id?: string | null
  body_available?: boolean
  body_unavailable_reason?: string | null
  [key: string]: unknown
}
export interface DebugStep {
  id: string
  phase: string
  status: string
  reason_code?: string | null
  duration_ms?: number | null
  created_at?: string
  started_at?: string
  finished_at?: string | null
  usage?: Record<string, unknown> | null
  data?: Record<string, unknown> | null
  [key: string]: unknown
}
export interface DebugFilters {
  project_id: string
  session_id: string
  request_id: string
  status: string
  since: string
  until: string
}
export interface DebugRunList {
  runs: DebugRun[]
  capabilities: DebugCapabilities
  next_cursor?: string | null
}
export interface DebugHealth {
  capabilities: DebugCapabilities
  index?: Record<string, unknown> | null
  jobs?: Record<string, unknown> | null
  outbox?: Record<string, unknown> | null
  wiki?: Record<string, unknown> | null
}
export interface ReplayPreview {
  preview_id: string
  can_submit: boolean
  input?: unknown
  scope?: unknown
  steps?: string[]
  calls?: unknown
  cost_estimate?: number | Record<string, unknown> | null
  expires_at: string
  reason_code?: string | null
  [key: string]: unknown
}

export function useDebugScope() {
  const userId = useAuthStore((state) => state.user?.id ?? "anonymous")
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  return { userId, workspaceId, key: ["memory-debug", userId, workspaceId] as const }
}

export function debugListPath(filters: DebugFilters, cursor?: string | null) {
  const params = new URLSearchParams({ limit: "50" })
  for (const [key, value] of Object.entries(filters)) {
    if (!value) continue
    const instant = key === "since" || key === "until" ? new Date(value) : null
    params.set(key, instant && !Number.isNaN(instant.getTime()) ? instant.toISOString() : value)
  }
  if (cursor) params.set("cursor", cursor)
  return `/api/memory-debug/runs?${params}`
}

export function useDebugRuns(filters: DebugFilters, cursor?: string | null) {
  const { key } = useDebugScope()
  return useQuery({
    queryKey: [...key, "list", filters, cursor],
    queryFn: () => http.get<DebugRunList>(debugListPath(filters, cursor)),
  })
}
export function useDebugHealth() {
  const { key } = useDebugScope()
  return useQuery({
    queryKey: [...key, "health"],
    queryFn: () => http.get<DebugHealth>("/api/memory-debug/health"),
  })
}
export function useDebugRun(id: string) {
  const { key } = useDebugScope()
  return useQuery({
    queryKey: [...key, "run", id],
    queryFn: () =>
      http.get<{ run: DebugRun; steps: DebugStep[] }>(`/api/memory-debug/runs/${encodeURIComponent(id)}`),
    enabled: !!id,
  })
}

const runPath = (id: string) => `/api/memory-debug/runs/${encodeURIComponent(id)}`
export const debugApi = {
  preview: (id: string, steps: string[]) =>
    http.post<ReplayPreview>(`${runPath(id)}/replay/preview`, { steps }),
  replay: (previewId: string) =>
    http.post<{ run_id: string; attempt_id: string }>("/api/memory-debug/replay", {
      preview_id: previewId,
      confirm_cost: true,
    }),
}
