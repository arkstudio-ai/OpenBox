// Skill centre data hooks. Components never fetch directly (ENGINEERING_SPEC §7).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { http, request } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import type {
  Catalog,
  CatalogInstallResult,
  InstalledSkill,
  McpConfig,
  McpServer,
} from "@/features/skills-center/types"
import { skillCenterKeys } from "./keys"

function useUserId(): string {
  return useAuthStore((s) => s.user?.id ?? "anon")
}

/** Everything installed and everything on offer, fetched together. */
export function useInstalledSkills() {
  const userId = useUserId()
  return useQuery({
    queryKey: skillCenterKeys.skills(userId),
    queryFn: () => http.get<InstalledSkill[]>("/api/agent/skill"),
  })
}

export function useMcpServers() {
  const userId = useUserId()
  return useQuery({
    queryKey: skillCenterKeys.mcp(userId),
    queryFn: () => http.get<McpServer[]>("/api/agent/mcp"),
  })
}

export function useCatalog() {
  const userId = useUserId()
  return useQuery({
    queryKey: skillCenterKeys.catalog(userId),
    queryFn: () => http.get<Catalog>("/api/agent/catalog"),
  })
}

/** Invalidate every list the centre renders — an install moves more than one. */
function useRefreshAll() {
  const qc = useQueryClient()
  const userId = useUserId()
  return () => {
    void qc.invalidateQueries({ queryKey: skillCenterKeys.all(userId) })
  }
}

export interface CatalogInstallVars {
  id: string
  kind: "skill" | "mcp"
  withMcp?: string[]
  env?: Record<string, Record<string, string>>
}

export function useInstallFromCatalog() {
  const refresh = useRefreshAll()
  return useMutation({
    mutationFn: (vars: CatalogInstallVars) =>
      http.post<CatalogInstallResult>("/api/agent/catalog/install", {
        id: vars.id,
        kind: vars.kind,
        with_mcp: vars.withMcp ?? [],
        env: vars.env ?? {},
      }),
    onSuccess: refresh,
  })
}

export function useAddMcpServer() {
  const refresh = useRefreshAll()
  return useMutation({
    mutationFn: async (vars: { name: string; config: McpConfig }) => {
      await http.post("/api/agent/mcp", { name: vars.name, ...vars.config })
      // Configure and connect are separate calls; a server that is configured
      // but never connected contributes no tools, which reads as a broken
      // install rather than an unfinished one.
      await http.post(`/api/agent/mcp/${encodeURIComponent(vars.name)}/connect`, undefined)
    },
    onSuccess: refresh,
  })
}

export function useRemoveMcpServer() {
  const refresh = useRefreshAll()
  return useMutation({
    mutationFn: (name: string) => http.delete(`/api/agent/mcp/${encodeURIComponent(name)}`),
    onSuccess: refresh,
  })
}

export function useConnectMcpServer() {
  const refresh = useRefreshAll()
  return useMutation({
    mutationFn: (name: string) =>
      http.post(`/api/agent/mcp/${encodeURIComponent(name)}/connect`, undefined),
    onSuccess: refresh,
  })
}

export function useDisconnectMcpServer() {
  const refresh = useRefreshAll()
  return useMutation({
    mutationFn: (name: string) =>
      http.post(`/api/agent/mcp/${encodeURIComponent(name)}/disconnect`, undefined),
    onSuccess: refresh,
  })
}

export function useUninstallSkill() {
  const refresh = useRefreshAll()
  return useMutation({
    mutationFn: (name: string) => http.delete(`/api/agent/skill/${encodeURIComponent(name)}`),
    onSuccess: refresh,
  })
}

/** Install a skill from a pasted SKILL.md or a git URL. */
export function useInstallSkill() {
  const refresh = useRefreshAll()
  return useMutation({
    mutationFn: (vars: { url?: string; name?: string; content?: string }) =>
      http.post<InstalledSkill>("/api/agent/skill/install", vars),
    onSuccess: refresh,
  })
}

/** Install a skill from an uploaded archive (zip/tar/tar.gz/tgz). */
export function useUploadSkillArchive() {
  const refresh = useRefreshAll()
  return useMutation({
    mutationFn: (vars: { file: File; name?: string }) => {
      const form = new FormData()
      form.append("file", vars.file)
      if (vars.name) form.append("name", vars.name)
      // Bypass http.post: FormData must reach fetch untouched so the browser
      // sets its own multipart boundary.
      return request<{ name: string; skills_count?: number; install_log?: string }>(
        "/api/agent/skill/upload",
        { method: "POST", body: form },
      )
    },
    onSuccess: refresh,
  })
}
