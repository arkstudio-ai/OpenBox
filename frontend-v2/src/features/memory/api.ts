import { useQuery } from "@tanstack/react-query"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { http } from "@/shared/api/http"
import { listMemories, memoryApi, type MemoryTab } from "@/shared/api/memory"
import type { Project } from "@/shared/types/api"

export function useMemoryScope() {
  const userId = useAuthStore((state) => state.user?.id ?? "anonymous")
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  return { userId, workspaceId, key: ["memory", userId, workspaceId] as const }
}

export function useMemories(tab: MemoryTab, projectId: string) {
  const { key } = useMemoryScope()
  return useQuery({ queryKey: [...key, "list", tab, projectId], queryFn: () => listMemories(tab, projectId) })
}

export function useMemoryProjects() {
  const { key } = useMemoryScope()
  return useQuery({
    queryKey: [...key, "projects"],
    queryFn: () => http.get<Project[]>("/api/agent/project"),
    staleTime: 30_000,
  })
}

export function useMemorySources(id: string) {
  const { key } = useMemoryScope()
  return useQuery({ queryKey: [...key, "sources", id], queryFn: () => memoryApi.sources(id), enabled: !!id })
}

export function useMemoryHistory(id: string) {
  const { key } = useMemoryScope()
  return useQuery({ queryKey: [...key, "history", id], queryFn: () => memoryApi.history(id), enabled: !!id })
}

/** Read fresh on every open: a list row or an earlier read never authorizes text. */
export function useMemoryDetail(id: string) {
  const { key } = useMemoryScope()
  return useQuery({
    queryKey: [...key, "detail", id],
    queryFn: () => memoryApi.get(id),
    enabled: !!id,
    staleTime: 0,
    gcTime: 0,
  })
}

export function useMemoryCleanup(id: string) {
  const { key } = useMemoryScope()
  return useQuery({ queryKey: [...key, "cleanup", id], queryFn: () => memoryApi.cleanup(id), enabled: !!id })
}
