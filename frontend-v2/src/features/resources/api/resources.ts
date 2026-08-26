// Resource-centre data hooks. Components never fetch directly (§7).
// Everything here talks to /api/assets, which is the OSS ledger — the resource
// centre is a view over object storage, not over a local directory.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import type {
  Resource,
  ResourcePage,
  ResourceProject,
  ResourceQuery,
} from "@/features/resources/types"
import { resourceKeys } from "./keys"

function useUserId(): string {
  return useAuthStore((s) => s.user?.id ?? "anonymous")
}

function toSearch(query: ResourceQuery): string {
  const params = new URLSearchParams({
    project: query.project,
    source: query.source,
    kind: query.kind,
    sort: query.sort,
    limit: String(query.limit ?? 100),
  })
  if (query.q.trim()) params.set("q", query.q.trim())
  return params.toString()
}

export function useResourcesQuery(query: ResourceQuery, enabled = true) {
  const userId = useUserId()
  return useQuery({
    queryKey: resourceKeys.list(userId, query),
    queryFn: () => http.get<ResourcePage>(`/api/assets?${toSearch(query)}`),
    enabled,
    // Presigned GETs in the payload live an hour; refresh well inside that.
    staleTime: 30 * 60_000,
  })
}

/** Shares the workspace "projects" cache — same key, one source of truth. */
export function useResourceProjects() {
  const userId = useUserId()
  return useQuery({
    queryKey: resourceKeys.projects(userId),
    queryFn: () => http.get<ResourceProject[]>("/api/agent/project"),
    staleTime: 30_000,
  })
}

function useInvalidateResources() {
  const userId = useUserId()
  const qc = useQueryClient()
  return () => void qc.invalidateQueries({ queryKey: resourceKeys.all(userId) })
}

export function useDeleteResource() {
  const invalidate = useInvalidateResources()
  return useMutation({
    mutationFn: (id: string) => http.delete<{ ok: boolean }>(`/api/assets/${id}`),
    onSuccess: invalidate,
  })
}

export function useRenameResource() {
  const invalidate = useInvalidateResources()
  return useMutation({
    mutationFn: ({ id, name }: { id: string; name: string }) =>
      http.patch<Resource>(`/api/assets/${id}`, { name }),
    onSuccess: invalidate,
  })
}

/** Invalidator for callers that finished an upload outside a mutation. */
export function useRefreshResources() {
  return useInvalidateResources()
}

/** Ask for a freshly signed attachment URL and hand it to the browser.
 *  The disposition is part of the signature, so it cannot be appended to a
 *  URL that was already signed for inline viewing. */
export async function downloadResource(id: string): Promise<void> {
  const { url } = await http.get<{ url: string }>(`/api/assets/${id}/url?download=true`)
  const a = document.createElement("a")
  a.href = url
  a.rel = "noopener"
  a.target = "_blank"
  a.click()
}
