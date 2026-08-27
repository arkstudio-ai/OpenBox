// Skill centre data hooks. Components never fetch directly (ENGINEERING_SPEC §7).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { http, request, requestBlob } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import type { Project, Session } from "@/shared/types/api"
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
    // A skill can be created by an agent while this route is unmounted.
    refetchOnMount: "always",
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

/** Projects offered by the chat-creation dialog. Kept in this feature so it
 *  does not reach sideways into workspace's private API layer. */
export function useSkillProjects(enabled = true) {
  const userId = useUserId()
  return useQuery({
    queryKey: skillCenterKeys.projects(userId),
    queryFn: () => http.get<Project[]>("/api/agent/project"),
    enabled,
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
    mutationFn: (name: string) => http.post(`/api/agent/mcp/${encodeURIComponent(name)}/connect`, undefined),
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

export function usePublishSkill() {
  const refresh = useRefreshAll()
  return useMutation({
    mutationFn: (installDir: string) =>
      http.post<InstalledSkill>(`/api/agent/skill/${encodeURIComponent(installDir)}/publish`, undefined),
    onSuccess: refresh,
  })
}

function saveBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement("a")
  anchor.href = url
  anchor.download = filename
  anchor.rel = "noopener"
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

/** Download the whole install directory, not one member of a skill pack. */
export function useDownloadSkillArchive() {
  return useMutation({
    mutationFn: async (installDir: string) => {
      const result = await requestBlob(`/api/agent/skill/${encodeURIComponent(installDir)}/download`)
      saveBlob(result.blob, result.filename || `${installDir}.zip`)
    },
  })
}

export interface CreateSkillChatVars {
  projectId: string
  brief: string
  prompt: string
}

/** Create a real chat, seed it with the person's natural-language request,
 *  then hand the session id to the caller for navigation. */
export function useCreateSkillChat() {
  const userId = useUserId()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async ({ projectId, brief, prompt }: CreateSkillChatVars) => {
      const session = await http.post<Session>("/api/agent/session", {
        project_id: projectId,
        agent: "build",
        title: brief.length > 32 ? `${brief.slice(0, 32)}…` : brief,
      })
      const clientMessageId = `skill-create-${Date.now().toString(36)}-${Math.random()
        .toString(36)
        .slice(2, 8)}`
      await http.post<{ ok: boolean }>(`/api/agent/session/${session.id}/prompt_async`, {
        text: prompt,
        agent: "build",
        client_message_id: clientMessageId,
      })
      return session
    },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: skillCenterKeys.sessions(userId) })
    },
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
