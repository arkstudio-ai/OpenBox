import type { SkillDisplay } from "@/shared/lib/skill-display"
import { useEffect, useMemo } from "react"
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { http, request } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { wsClient } from "@/shared/ws/client"
import type {
  AgentSpec,
  Definition,
  DefinitionPage,
  DefinitionVersion,
  Page,
  TeamEvents,
  TeamRunInfo,
  TeamPolicy,
  TeamSnapshot,
  TeamSpec,
  TeamUsage,
} from "../types"
import { terminalTeam } from "../types"

type Kind = "agent" | "team"
export function useModelCapabilities() {
  const scope = useTeamScope()
  return useQuery({
    queryKey: [...scope, "model-capabilities"],
    queryFn: ({ signal }) =>
      http.get<{
        models: { id: string; reasoning_variants: string[]; unavailable_for_team?: boolean }[]
      }>("/api/agent-capabilities/models", { signal }),
  })
}
export function useMcpCapabilities() {
  const scope = useTeamScope()
  return useQuery({
    queryKey: [...scope, "mcp-capabilities"],
    queryFn: ({ signal }) =>
      http.get<{
        available: boolean
        enabled: boolean
        services: { name: string; status: string; tools: string[]; resources: string[] }[]
      }>("/api/agent-capabilities/mcp", { signal }),
  })
}
export function useTeamScope() {
  const user = useAuthStore((s) => s.user?.id ?? "anonymous")
  const workspace = useWorkspaceStore((s) => s.currentId)
  return useMemo(() => ["agent-team", user, workspace] as const, [user, workspace])
}
function params(values: Record<string, string | undefined>) {
  return new URLSearchParams(
    Object.entries(values).filter((pair): pair is [string, string] => pair[1] !== undefined),
  ).toString()
}
export function useDefinitions<T extends AgentSpec | TeamSpec>(
  kind: Kind,
  search = "",
  status?: string,
  enabled = true,
) {
  const scope = useTeamScope()
  return useInfiniteQuery({
    queryKey: [...scope, "definitions", kind, search, status],
    initialPageParam: "",
    queryFn: ({ pageParam, signal }) =>
      http.get<DefinitionPage<T>>(
        `/api/${kind}-definitions?${params({ search, status, cursor: pageParam || undefined })}`,
        { signal },
      ),
    getNextPageParam: (last) => last.next_cursor || undefined,
    enabled,
  })
}
export function useDefinition<T extends AgentSpec | TeamSpec>(kind: Kind, id: string | null) {
  const scope = useTeamScope()
  return useQuery({
    queryKey: [...scope, "definition", kind, id],
    enabled: !!id,
    queryFn: ({ signal }) =>
      http.get<Definition<T>>(`/api/${kind}-definitions/${encodeURIComponent(id!)}`, { signal }),
  })
}
export function useVersions<T extends AgentSpec | TeamSpec>(kind: Kind, id: string, enabled = true) {
  const scope = useTeamScope()
  return useInfiniteQuery({
    queryKey: [...scope, "versions", kind, id],
    enabled: enabled && !!id,
    initialPageParam: "",
    queryFn: ({ pageParam, signal }) =>
      http.get<Page<DefinitionVersion<T>>>(
        `/api/${kind}-definitions/${encodeURIComponent(id)}/versions?${params({ before: pageParam || undefined })}`,
        { signal },
      ),
    getNextPageParam: (last) => (last.next_cursor ? String(last.next_cursor) : undefined),
  })
}
export interface DefinitionWrite<T> {
  action: "create" | "draft-version" | "versions" | "archive" | "duplicate" | "undo-autoapproval"
  id?: string
  spec?: T
  expected_revision?: number
  version_id?: string
  name?: string
  key?: string
}
export function useDefinitionWrite<T extends AgentSpec | TeamSpec>(kind: Kind) {
  const scope = useTeamScope()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ action, id, key, ...body }: DefinitionWrite<T>) =>
      request<Definition<T>>(
        `/api/${kind}-definitions${action === "create" ? "" : `/${encodeURIComponent(id!)}/${action}`}`,
        {
          method: action === "draft-version" ? "PUT" : "POST",
          headers: { "Idempotency-Key": key ?? crypto.randomUUID() },
          body: JSON.stringify(body),
        },
      ),
    onSuccess: (data) => {
      qc.setQueryData([...scope, "definition", kind, data.id], data)
      void qc.invalidateQueries({ queryKey: [...scope, "definitions", kind] })
      void qc.invalidateQueries({ queryKey: [...scope, "versions", kind, data.id] })
    },
    retry: false,
  })
}
export function useTeamRuns(
  filters: { session_id?: string; status?: string; template_id?: string; project_id?: string } = {},
  enabled = true,
) {
  const scope = useTeamScope()
  const qc = useQueryClient()
  useEffect(
    () =>
      wsClient.on("team.run.updated", () => {
        void qc.invalidateQueries({ queryKey: [...scope, "runs"] })
      }),
    [qc, scope],
  )
  return useInfiniteQuery({
    queryKey: [...scope, "runs", filters],
    initialPageParam: "",
    enabled,
    queryFn: ({ pageParam, signal }) =>
      http.get<Page<TeamRunInfo>>(
        `/api/team-runs?${params({ ...filters, cursor: pageParam || undefined })}`,
        { signal },
      ),
    getNextPageParam: (last) => last.next_cursor || undefined,
    refetchInterval: 30_000,
  })
}
/** Events are a durable wake hint, not a second state store. Only fetch a new
 * snapshot when its sequence advances; the five-second catch-up covers drops. */
export function useTeamRun(id: string | null) {
  const scope = useTeamScope()
  const qc = useQueryClient()
  const key = useMemo(() => [...scope, "run", id], [scope, id])
  const query = useQuery({
    queryKey: key,
    enabled: !!id,
    queryFn: ({ signal }) => http.get<TeamSnapshot>(`/api/team-runs/${id}`, { signal }),
  })
  useEffect(() => {
    if (!id) return
    let disposed = false
    let running = false
    const catchup = async () => {
      if (running || disposed) return
      const current = qc.getQueryData<TeamSnapshot>(key)
      if (!current) return
      running = true
      try {
        const events = await http.get<TeamEvents>(`/api/team-runs/${id}/events?after_seq=${current.seq}`)
        if (!disposed && events.last_seq > current.seq) {
          await qc.invalidateQueries({ queryKey: key })
          await qc.invalidateQueries({ queryKey: [...scope, "collection", id] })
        }
      } catch {
        if (!disposed) void qc.invalidateQueries({ queryKey: key })
      } finally {
        running = false
      }
    }
    const off = wsClient.on("team.run.updated", (event) => {
      if (event.teamRunId === id) void catchup()
    })
    const reconnect = wsClient.on("__connected", () => {
      void catchup()
    })
    const timer = window.setInterval(() => {
      if (!document.hidden && !terminalTeam(qc.getQueryData<TeamSnapshot>(key)?.run.state ?? ""))
        void catchup()
    }, 5_000)
    return () => {
      disposed = true
      off()
      reconnect()
      window.clearInterval(timer)
    }
  }, [id, qc, scope, key])
  return query
}
export function useTeamCollection<T>(
  id: string | null,
  collection: string,
  filters: { member?: string; between?: string; task_id?: string } = {},
  enabled = true,
) {
  const scope = useTeamScope()
  return useInfiniteQuery({
    queryKey: [...scope, "collection", id, collection, filters],
    enabled: !!id && enabled,
    initialPageParam: 0,
    queryFn: ({ pageParam, signal }) =>
      http.get<Page<T>>(
        `/api/team-runs/${id}/${collection}?${params({ ...filters, offset: String(pageParam) })}`,
        { signal },
      ),
    getNextPageParam: (last) => last.next_offset ?? undefined,
  })
}
export function useTeamControl(id: string) {
  const scope = useTeamScope()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ action, revision }: { action: "pause" | "resume" | "cancel"; revision: number }) =>
      http.post(
        `/api/team-runs/${id}/${action}`,
        { expected_revision: revision },
        { headers: { "Idempotency-Key": crypto.randomUUID() } },
      ),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: [...scope, "run", id] })
      void qc.invalidateQueries({ queryKey: [...scope, "runs"] })
    },
  })
}

export type TeamGrantChange = Pick<
  TeamPolicy,
  "paid_tools" | "permission_rules" | "max_coordinator_turns" | "max_wall_time_seconds"
> & { expected_revision: number }

export function useTeamGrant(id: string) {
  const scope = useTeamScope()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ key, ...body }: TeamGrantChange & { key: string }) =>
      http.post(`/api/team-runs/${id}/grant`, body, { headers: { "Idempotency-Key": key } }),
    onSettled: () => {
      void qc.invalidateQueries({ queryKey: [...scope, "run", id] })
      void qc.invalidateQueries({ queryKey: [...scope, "runs"] })
    },
    retry: false,
  })
}

export function useTeamUsage(id: string) {
  const scope = useTeamScope()
  const qc = useQueryClient()
  useEffect(
    () =>
      wsClient.on("team.run.updated", (event) => {
        if (event.teamRunId === id) void qc.invalidateQueries({ queryKey: [...scope, "usage", id] })
      }),
    [id, qc, scope],
  )
  return useQuery({
    queryKey: [...scope, "usage", id],
    queryFn: ({ signal }) => http.get<TeamUsage>(`/api/team-runs/${id}/usage`, { signal }),
  })
}

export function useSaveTeamConfiguration(runId: string, memberId?: string) {
  const scope = useTeamScope()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({
      key,
      ...body
    }: {
      name: string
      member_ids?: string[]
      member_names?: Record<string, string>
      key: string
    }) =>
      http.post<Definition<AgentSpec | TeamSpec>>(
        `/api/team-runs/${runId}/${memberId ? `members/${memberId}/save-definition` : "save-as-template"}`,
        body,
        { headers: { "Idempotency-Key": key } },
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: [...scope, "definitions"] })
    },
    retry: false,
  })
}

export interface CatalogSkill extends SkillDisplay {
  name: string
  description: string
  source?: string
  allowed_tools?: string[]
  requires_mcp?: string[]
}
export function useCatalogSkills() {
  const scope = useTeamScope()
  return useQuery({
    queryKey: [...scope, "skills"],
    queryFn: () => http.get<CatalogSkill[]>("/api/agent/skill"),
    staleTime: 60_000,
  })
}
export function useAgentTrial() {
  const scope = useTeamScope()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, versionId, projectId }: { id: string; versionId: string; projectId?: string }) =>
      http.post<{ session_id: string; version_id: string }>(
        `/api/agent-definitions/${id}/test-runs`,
        { version_id: versionId, project_id: projectId },
        { headers: { "Idempotency-Key": crypto.randomUUID() } },
      ),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: [...scope, "trials"] })
    },
  })
}

export function useTeamPreview() {
  return useMutation({
    mutationFn: (spec: TeamSpec) =>
      http.post<import("../types").LineupDetail>("/api/team-runs/preview", { spec }),
  })
}
export function useAgentDraftGeneration() {
  return useMutation({
    mutationFn: ({
      prompt,
      spec,
      field,
    }: {
      prompt: string
      spec?: AgentSpec
      field?: "instruction" | "when_to_use"
    }) =>
      http.post<{ spec: AgentSpec; capability_summary: import("../types").CapabilitySummary }>(
        "/api/agent-definitions/draft",
        { prompt, spec, field },
        { headers: { "Idempotency-Key": crypto.randomUUID() } },
      ),
    retry: false,
  })
}
export function useCatalogChatCreate() {
  return useMutation({
    mutationFn: async ({ projectId, prompt }: { projectId: string; prompt: string }) => {
      const session = await http.post<{ id: string }>("/api/agent/session", {
        project_id: projectId,
        agent: "build",
      })
      await http.post(`/api/agent/session/${session.id}/prompt_async`, {
        text: prompt,
        agent: "build",
        client_message_id: crypto.randomUUID(),
        delivery: "followup",
      })
      return session
    },
  })
}
