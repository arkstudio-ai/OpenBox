import { useQuery } from "@tanstack/react-query"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { useDebouncedValue } from "@/shared/hooks/useDebouncedValue"
import type { MentionScope } from "./useMentionMenu"

/** Each picker owns its query. The route supplies the resource API boundary. */
export function useResourceSearch(scope: MentionScope | undefined, query: string, enabled: boolean) {
  const userId = useAuthStore((state) => state.user?.id ?? "anonymous")
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const q = query.trim()
  const settled = useDebouncedValue(q, 160)
  const remote = Boolean(q && scope?.search)
  const waiting = remote && q !== settled
  const result = useQuery({
    // Shares the resource invalidation prefix used by uploads and description edits.
    queryKey: ["resources", userId, "picker", workspaceId, scope?.project, scope?.source, settled],
    queryFn: ({ signal }) => scope!.search!(settled, signal),
    enabled: enabled && remote && !waiting,
    staleTime: 5_000,
  })
  const local = (scope?.items ?? []).filter((item) =>
    `${item.name} ${item.description ?? ""}`.toLowerCase().includes(q.toLowerCase()),
  )
  return {
    items: remote ? (waiting ? [] : result.data ?? []) : local,
    loading: remote ? waiting || result.isLoading : scope?.loading ?? false,
    error: remote && !waiting && result.isError,
  }
}
