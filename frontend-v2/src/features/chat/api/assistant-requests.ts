import { useEffect } from "react"
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { http } from "@/shared/api/http"
import { wsClient } from "@/shared/ws/client"
import type { WsEventName } from "@/shared/ws/events"
import type { PermissionRequest, QuestionRequest } from "@/shared/types/api"
import { assistantKeys, scopedOptions } from "./assistant"
import type { QuestionReceipt } from "./question"

interface RequestPage<T> {
  items: Array<T & { task_title: string; project_name?: string }>
  next_cursor: string | null
  receipts: QuestionReceipt[]
}

/** A question waiting in one of the user's other conversations (not watched). */
export interface WaitingQuestion {
  id: string
  session_id: string
  session_title: string
  project_name?: string | null
  questions: Array<{ header: string; question: string }>
}

/** Socket events that add, change or settle a pending request of each kind. */
const REQUEST_EVENTS: Record<"question" | "permission", readonly WsEventName[]> = {
  question: ["question.asked", "question.updated", "question.replied", "question.rejected", "question.cancelled"],
  permission: ["permission.asked", "permission.replied"],
}

function useRequests<T>(kind: "question" | "permission", active = true) {
  const userId = useAuthStore((state) => state.user?.id ?? "anonymous")
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const qc = useQueryClient()
  const enabled = active && userId !== "anonymous" && !!workspaceId
  useEffect(() => {
    if (!enabled) return
    const refresh = () => void qc.invalidateQueries({ queryKey: [...assistantKeys.requests(userId, workspaceId), kind] })
    const off = REQUEST_EVENTS[kind].map((event) => wsClient.on(event, refresh))
    return () => off.forEach((stop) => stop())
  }, [qc, userId, workspaceId, kind, enabled])
  return useInfiniteQuery({
    queryKey: [...assistantKeys.requests(userId, workspaceId), kind],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) => http.get<RequestPage<T>>(`/api/assistant/requests?kind=${kind}${pageParam
      ? `&cursor=${encodeURIComponent(pageParam)}` : ""}`, scopedOptions(workspaceId, signal)),
    getNextPageParam: (page) => page.next_cursor ?? undefined,
    // Socket events and assistant events invalidate these; the interval is a fallback.
    refetchInterval: enabled ? 15_000 : false,
    enabled,
  })
}

/** Questions in the user's other conversations: answered there, or by the assistant on request. */
function useWaiting(active = true) {
  const userId = useAuthStore((state) => state.user?.id ?? "anonymous")
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const qc = useQueryClient()
  const enabled = active && userId !== "anonymous" && !!workspaceId
  const key = [...assistantKeys.requests(userId, workspaceId), "waiting"]
  useEffect(() => {
    if (!enabled) return
    const refresh = () => void qc.invalidateQueries({ queryKey: [...assistantKeys.requests(userId, workspaceId), "waiting"] })
    const off = REQUEST_EVENTS.question.map((event) => wsClient.on(event, refresh))
    return () => off.forEach((stop) => stop())
  }, [qc, userId, workspaceId, enabled])
  return useQuery({
    queryKey: key,
    queryFn: ({ signal }) => http.get<{ items: WaitingQuestion[] }>("/api/assistant/requests/waiting",
      scopedOptions(workspaceId, signal)),
    refetchInterval: enabled ? 30_000 : false,
    enabled,
  })
}

export function useAssistantRequests(enabled = true) {
  const userId = useAuthStore((state) => state.user?.id ?? "anonymous")
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const requests = useRequests<QuestionRequest>("question", enabled)
  const approvals = useRequests<PermissionRequest>("permission", enabled)
  const other = useWaiting(enabled)
  const items = [...new Map(requests.data?.pages.flatMap((page) => page.items.map((item) => [item.id, item] as const))).values()]
  const permissions = [...new Map(approvals.data?.pages.flatMap((page) => page.items.map((item) => [item.id, item] as const))).values()]
  const waiting = other.data?.items ?? []
  const failed = [...requests.data?.pages[0]?.receipts ?? [], ...approvals.data?.pages[0]?.receipts ?? []]
    .filter((receipt) => receipt.state === "failed")
  const pending = [...new Set([...items, ...waiting].map((item) => `q:${item.id}`)
    .concat(permissions.map((item) => `p:${item.id}`)))]
  const error = requests.error ?? approvals.error ?? other.error
  const ids = [...pending, ...failed.map((item) => `failed:${item.command_id}`), ...(error ? ["error"] : [])]
  return { requests, approvals, items, permissions, waiting, failed, error, count: pending.length, ids,
    scopeKey: `${userId}:${workspaceId}`, sessions: new Set([...items, ...permissions, ...waiting].map((item) => item.session_id)),
    hasMore: !!requests.hasNextPage || !!approvals.hasNextPage }
}
