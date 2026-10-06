// The conversations the personal assistant watches, for the sidebar. One
// cheap read; socket hints refresh it, and a slow timer covers missed ones.
import { useEffect } from "react"
import { useQuery, useQueryClient } from "@tanstack/react-query"
import { http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { wsClient } from "@/shared/ws/client"
import type { SessionStatus } from "@/shared/types/api"
import { assistantKeys, scopedOptions } from "./assistant"

export interface AssistantWatchItem {
  task_id: string
  title: string
  project: { id: string; name: string }
  session_id: string
  session_status: SessionStatus
  desired_state: "running" | "paused" | "canceled"
  observed_state: string
  revision: number
  updated_at: string
  pending_questions: number
  latest_result?: { result_id: string; outcome: string; delivery_state: string; created_at: string; summary: string } | null
}

export interface AssistantWatchPage {
  items: AssistantWatchItem[]
  has_more: boolean
}

/** Fallback only: socket hints carry the latency. */
export const WATCH_POLL_MS = 30_000
/** Coalesces a burst of hints (a run settling emits several) into one read. */
const HINT_DELAY_MS = 500

export function useAssistantWatch(enabled = true) {
  const userId = useAuthStore((state) => state.user?.id ?? "anonymous")
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  const qc = useQueryClient()
  const active = enabled && userId !== "anonymous" && !!workspaceId
  const query = useQuery({
    queryKey: assistantKeys.watch(userId, workspaceId),
    queryFn: ({ signal }) => http.get<AssistantWatchPage>("/api/assistant/watch", scopedOptions(workspaceId, signal)),
    enabled: active,
    staleTime: 5_000,
    refetchInterval: active ? WATCH_POLL_MS : false,
    retry: false,
  })
  useEffect(() => {
    if (!active) return
    const key = assistantKeys.watch(userId, workspaceId)
    let timer: ReturnType<typeof setTimeout> | undefined
    const refresh = () => {
      timer ??= setTimeout(() => {
        timer = undefined
        void qc.invalidateQueries({ queryKey: key, exact: true })
      }, HINT_DELAY_MS)
    }
    const watched = (sessionId?: string) =>
      !!sessionId && (qc.getQueryData<AssistantWatchPage>(key)?.items ?? []).some((item) => item.session_id === sessionId)
    // A watched conversation changed state, or any run settled — the main
    // session's run may have started or stopped watching something.
    const status = (data: { sessionId: string; status: SessionStatus }) => {
      if (watched(data.sessionId) || data.status === "idle" || data.status === "error") refresh()
    }
    // Pending questions are counted per watched conversation.
    const question = (data: { session_id?: string }) => { if (!data.session_id || watched(data.session_id)) refresh() }
    const off = [
      wsClient.on("session.status", status),
      ...(["question.asked", "question.replied", "question.rejected", "question.cancelled"] as const)
        .map((event) => wsClient.on(event, question)),
      wsClient.on("__connected", refresh),
    ]
    return () => { clearTimeout(timer); off.forEach((stop) => stop()) }
  }, [active, qc, userId, workspaceId])
  return query
}
