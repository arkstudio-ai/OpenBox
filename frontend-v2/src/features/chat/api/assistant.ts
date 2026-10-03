import { useEffect } from "react"
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { ApiError, http } from "@/shared/api/http"
import { useAuthStore } from "@/shared/api/auth-store"
import { useWorkspaceStore } from "@/shared/api/workspace-store"
import { wsClient } from "@/shared/ws/client"
import type { SessionStatus } from "@/shared/types/api"
import type { SendMessageVars } from "./messages"

export interface AssistantResult {
  result_id: string
  run_id: string
  generation: number
  result_message_id: string | null
  outcome: "succeeded" | "error" | "aborted" | string
  delivery_state: "pending" | "accepted" | "retry_wait" | "processed" | "blocked"
  report_attempt: number
  assistant_inbox_id: string | null
  processed_message_id: string | null
  processed_sequence?: number | null
  last_error_code: string | null
  observed_intent_revision: number
  created_at: string
}

export interface AssistantTaskView {
  task: {
    id: string
    title: string
    project_id: string
    execution_session_id: string
    desired_state: "running" | "paused" | "canceled"
    observed_state: string
    control_revision: number
    intent_revision: number
    updated_at: string
  }
  execution_session: { id: string; status: SessionStatus }
  run_binding: { run_id: string | null; generation: number; phase: string } | null
  latest_result: AssistantResult | null
  latest_submission: {
    submission_id: string
    command_id: string
    inbox_id: string
    disposition: string
    accepted_at: string
    applied_at: string | null
    run_id: string | null
    generation: number | null
  } | null
  pending_requests_location: "execution_session"
}

export interface AssistantAnswerPosition {
  message_id: string
  sequence: number
  available: boolean
  display_token?: string
}

export interface AssistantSnapshot {
  state: "not_created" | "ready"
  session: {
    id: string
    user_id: string
    workspace_id: string
    project_id: string
    kind: "assistant"
    agent: "assistant"
    model: string
    variant: string | null
    status: SessionStatus
  } | null
  high_water_mark: number
  last_seen_sequence: number
  tasks: AssistantTaskView[]
  next_task_cursor: string | null
  answers: AssistantAnswerPosition[]
  next_before_sequence: number | null
  unread_count: number
  unread_count_is_lower_bound: boolean
}

export interface AssistantReceipt {
  inbox_id: string
  assistant_session_id: string
  client_id: string
  inbox_client_id: string
  state: "accepted" | "claimed" | "settled" | "canceled"
  delivery: "followup"
  message_id: string | null
  run_id: string | null
  generation: number | null
}

export interface AssistantSourcePage {
  result_id: string
  source_version: string
  offset: number
  sources: Array<{ session_id: string; message_id: string; part_id: string; text: string }>
  next_offset: number | null
}

export const assistantKeys = {
  all: (userId: string, workspaceId: string | null) => ["assistant", userId, workspaceId] as const,
  snapshot: (userId: string, workspaceId: string | null) => ["assistant", userId, workspaceId, "snapshot"] as const,
  task: (userId: string, workspaceId: string | null, taskId: string) => ["assistant", userId, workspaceId, "task", taskId] as const,
  transcripts: (userId: string, workspaceId: string | null, sessionId: string) => ["assistant", userId, workspaceId, "transcript", sessionId] as const,
  transcript: (userId: string, workspaceId: string | null, sessionId: string, ids: string[]) =>
    [...assistantKeys.transcripts(userId, workspaceId, sessionId), ids.join(",")] as const,
}

function useScope() {
  const userId = useAuthStore((state) => state.user?.id ?? "anonymous")
  const workspaceId = useWorkspaceStore((state) => state.currentId)
  return { userId, workspaceId }
}

export function scopedOptions(workspaceId: string | null, signal?: AbortSignal): RequestInit {
  return { signal, headers: workspaceId ? { "X-Workspace-Id": workspaceId } : undefined }
}

export function useAssistantSnapshot(enabled = true) {
  const { userId, workspaceId } = useScope()
  return useQuery({
    queryKey: assistantKeys.snapshot(userId, workspaceId),
    queryFn: ({ signal }) => http.get<AssistantSnapshot>("/api/assistant", scopedOptions(workspaceId, signal)),
    enabled: enabled && userId !== "anonymous" && !!workspaceId,
    staleTime: 5_000,
    refetchInterval: enabled ? 15_000 : false,
    retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
  })
}

export function useEnsureAssistant() {
  const { userId, workspaceId } = useScope()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () => http.post<{ session_id: string }>("/api/assistant/ensure", {}, scopedOptions(workspaceId)),
    onSuccess: () => qc.invalidateQueries({ queryKey: assistantKeys.all(userId, workspaceId) }),
  })
}

export function useAssistantEvents(enabled = true) {
  const { userId, workspaceId } = useScope()
  const qc = useQueryClient()
  useEffect(() => {
    if (!enabled) return
    const refresh = () => void qc.invalidateQueries({ queryKey: assistantKeys.all(userId, workspaceId) })
    // SQL is the receipt authority. Live frames only prompt a fresh snapshot.
    const off = [wsClient.on("session.status", refresh), wsClient.on("message.updated", refresh),
      wsClient.on("tool.completed", refresh), wsClient.on("assistant.history.changed", refresh), wsClient.on("__connected", refresh)]
    return () => off.forEach((stop) => stop())
  }, [enabled, qc, userId, workspaceId])
}

export function useAssistantTask(taskId: string, enabled = true) {
  const { userId, workspaceId } = useScope()
  return useQuery({
    queryKey: assistantKeys.task(userId, workspaceId, taskId),
    queryFn: ({ signal }) => http.get<AssistantTaskView>(`/api/assistant/tasks/${encodeURIComponent(taskId)}`,
      scopedOptions(workspaceId, signal)),
    enabled: enabled && !!taskId && !!workspaceId,
    refetchInterval: 5_000,
    refetchOnMount: "always",
    retry: false,
  })
}

export function sendAssistantTurn(mainId: string, workspaceId: string | null, vars: SendMessageVars) {
  return http.post<AssistantReceipt>("/api/assistant/turns", {
    assistant_session_id: mainId, client_id: vars.clientMessageId, delivery: "followup", text: vars.text,
    model: vars.model, variant: vars.variant, attachment_ids: vars.attachments ?? [],
    video_model: vars.videoModel, video_resolution: vars.videoResolution,
  }, scopedOptions(workspaceId))
}

export function useAssistantReadCursor() {
  const { userId, workspaceId } = useScope()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (answer: AssistantAnswerPosition) => http.post<{ last_seen_sequence: number }>("/api/assistant/read-cursor", {
      last_seen_sequence: answer.sequence, display_token: answer.display_token,
    }, scopedOptions(workspaceId)),
    onSuccess: (receipt) => {
      qc.setQueryData<AssistantSnapshot>(assistantKeys.snapshot(userId, workspaceId), (old) => {
        if (!old) return old
        const seen = Math.max(old.last_seen_sequence, receipt.last_seen_sequence)
        return { ...old, last_seen_sequence: seen,
          unread_count: old.answers.filter((answer) => answer.available && answer.sequence > seen).length,
          unread_count_is_lower_bound: old.unread_count_is_lower_bound && (old.next_before_sequence ?? 0) > seen }
      })
    },
    onError: () => void qc.invalidateQueries({ queryKey: assistantKeys.snapshot(userId, workspaceId) }),
  })
}

export function useRetryAssistantReport() {
  const { userId, workspaceId } = useScope()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (request: { resultId: string; attempt: number; key: string }) => http.post(
      `/api/assistant/results/${encodeURIComponent(request.resultId)}/retry`,
      { idempotency_key: request.key, expected_report_attempt: request.attempt }, scopedOptions(workspaceId)),
    onSettled: () => qc.invalidateQueries({ queryKey: assistantKeys.all(userId, workspaceId) }),
  })
}

export function readAssistantResult(resultId: string, workspaceId: string | null, offset = 0, version?: string) {
  const params = new URLSearchParams({ max_chars: "8000", offset: String(offset) })
  if (version) params.set("source_version", version)
  return http.get<AssistantSourcePage>(`/api/assistant/results/${encodeURIComponent(resultId)}?${params}`,
    scopedOptions(workspaceId))
}

export function useAssistantResult(resultId: string, enabled: boolean) {
  const { userId, workspaceId } = useScope()
  return useInfiniteQuery({
    queryKey: [...assistantKeys.all(userId, workspaceId), "result", resultId],
    initialPageParam: { offset: 0, version: undefined as string | undefined },
    queryFn: ({ pageParam }) => readAssistantResult(resultId, workspaceId, pageParam.offset, pageParam.version),
    getNextPageParam: (page) => page.next_offset === null ? undefined : { offset: page.next_offset, version: page.source_version },
    enabled: enabled && !!resultId && !!workspaceId,
    retry: false,
    // A reopened report must validate its originals again before rendering cached text.
    staleTime: 0,
    refetchOnMount: "always",
  })
}

export function useAssistantTaskPages(enabled: boolean) {
  const { userId, workspaceId } = useScope()
  return useInfiniteQuery({
    queryKey: [...assistantKeys.all(userId, workspaceId), "tasks"],
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => {
      const params = new URLSearchParams({ limit: "20" })
      if (pageParam) params.set("task_cursor", pageParam)
      return http.get<AssistantSnapshot>(`/api/assistant?${params}`, scopedOptions(workspaceId, signal))
    },
    getNextPageParam: (page) => page.next_task_cursor ?? undefined,
    enabled: enabled && !!workspaceId && userId !== "anonymous",
    refetchInterval: enabled ? 15_000 : false,
    retry: false,
  })
}
