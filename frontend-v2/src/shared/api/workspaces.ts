import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore, type WorkspaceSummary } from "@/shared/api/workspace-store"

export interface WorkspaceMember {
  user_id: string
  username: string
  email: string | null
  role: "owner" | "admin" | "member"
  status: "active" | "invited" | "removed"
  created_at: string
}

export interface WorkspaceInvitation {
  id: string
  workspace_id: string
  workspace_name?: string
  target: string
  role: "admin" | "member"
  expires_at: string
  accepted_at?: string | null
}

export interface WorkspaceList {
  items: WorkspaceSummary[]
  default_workspace_id: string | null
}

export interface WorkspaceDetail extends WorkspaceSummary {
  members: WorkspaceMember[]
  invitations: WorkspaceInvitation[]
}

export function useWorkspacesQuery() {
  const userId = useAuthStore((state) => state.user?.id ?? "anonymous")
  return useQuery({
    queryKey: ["workspaces", userId],
    queryFn: async () => {
      const result = await fetchWorkspaces()
      useWorkspaceStore
        .getState()
        .setItems(result.items, result.default_workspace_id)
      return result
    },
    staleTime: 30_000,
  })
}

export function fetchWorkspaces() {
  return http.get<WorkspaceList>("/api/workspaces")
}

export function useCurrentWorkspaceQuery() {
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  return useQuery({
    queryKey: ["workspace", workspaceId],
    queryFn: () => http.get<WorkspaceDetail>("/api/workspaces/current"),
    enabled: Boolean(workspaceId),
  })
}

export function usePendingInvitationsQuery() {
  const userId = useAuthStore((state) => state.user?.id ?? "anonymous")
  return useQuery({
    queryKey: ["workspace-invitations", userId, "pending"],
    queryFn: () => http.get<{ items: WorkspaceInvitation[] }>("/api/workspaces/invitations/pending"),
  })
}

export function useInviteMember() {
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: { target: string; role: "admin" | "member" }) =>
      http.post<WorkspaceInvitation & { token: string }>(`/api/workspaces/${workspaceId}/invitations`, body),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId] }),
  })
}

export function useChangeMemberRole() {
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({ userId, role }: { userId: string; role: "admin" | "member" }) =>
      http.patch(`/api/workspaces/${workspaceId}/members/${userId}`, { role }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId] }),
  })
}

export function useRemoveMember() {
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (userId: string) => http.delete(`/api/workspaces/${workspaceId}/members/${userId}`),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["workspace", workspaceId] }),
  })
}

export function acceptInvitation(token: string) {
  return http.post<{ ok: boolean; workspace_id: string }>(
    `/api/workspaces/invitations/${encodeURIComponent(token)}/accept`,
  )
}
