import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import type { Project } from "@/shared/types/api"
import { workspaceKeys } from "./keys"
import { useWorkspaceStore } from "@/shared/api/workspace-store"

export function useUserId(): string {
  return useAuthStore((s) => s.user?.id ?? "anonymous")
}

export function useProjectsQuery() {
  const userId = useUserId()
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  return useQuery({
    queryKey: workspaceKeys.projects(userId, workspaceId),
    queryFn: () => http.get<Project[]>("/api/agent/project"),
  })
}

export function useCreateProject() {
  const userId = useUserId()
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (name: string) => http.post<Project>("/api/agent/project", { name }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: workspaceKeys.projects(userId, workspaceId) }),
  })
}

export function useRenameProject() {
  const userId = useUserId()
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) =>
      http.patch<Project>(`/api/agent/project/${id}`, { name }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: workspaceKeys.projects(userId, workspaceId) }),
  })
}

export function useDeleteProject() {
  const userId = useUserId()
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => http.delete<{ ok: boolean }>(`/api/agent/project/${id}`),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: workspaceKeys.projects(userId, workspaceId) })
      void qc.invalidateQueries({ queryKey: workspaceKeys.sessions(userId, workspaceId) })
    },
  })
}
