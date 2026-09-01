import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useAuthStore } from "@/shared/api/auth-store"
import { http } from "@/shared/api/http"
import type { MessageWithParts, Session } from "@/shared/types/api"
import { chatKeys } from "./keys"

export function useUserId(): string {
  return useAuthStore((s) => s.user?.id ?? "anonymous")
}

export function useMessagesQuery(sessionId: string, live = false) {
  const userId = useUserId()
  return useQuery({
    queryKey: chatKeys.messages(userId, sessionId),
    queryFn: () => http.get<MessageWithParts[]>(`/api/agent/session/${sessionId}/message`),
    enabled: sessionId.length > 0,
    // A reconnect can only replay durable state, not the WS frames missed
    // while the page was gone. Poll the durable snapshot during a live run so
    // a refreshed/reopened page converges even if it reconnects between two
    // events; take a fresh snapshot on every route mount for the same reason.
    refetchOnMount: "always",
    refetchInterval: live ? 1_000 : false,
  })
}

export interface SendMessageVars {
  text: string
  model?: string
  /** null explicitly clears the conversation override. */
  variant?: string | null
  /** Video model for this turn; the backend records it on the session. */
  videoModel?: string
  agent?: string
  attachments?: string[]
  clientMessageId: string
}

/** One wire path for both a new conversation's first prompt and later turns. */
export function sendPromptAsync(sessionId: string, vars: SendMessageVars) {
  return http.post<{ ok: boolean }>(`/api/agent/session/${sessionId}/prompt_async`, {
    text: vars.text,
    agent: vars.agent,
    model: vars.model,
    video_model: vars.videoModel,
    variant: vars.variant,
    attachments: vars.attachments?.length ? vars.attachments : undefined,
    client_message_id: vars.clientMessageId,
  })
}

export function useSendMessage(sessionId: string) {
  const qc = useQueryClient()
  const userId = useUserId()
  return useMutation({
    mutationFn: (vars: SendMessageVars) => sendPromptAsync(sessionId, vars),
    // The backend records the chosen model on the session, so the cached copy
    // is stale the moment a send goes out — and it is what restores the picker
    // when the user comes back to this conversation.
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["session", userId, sessionId] }),
  })
}

export function useAbortSession(sessionId: string) {
  return useMutation({
    mutationFn: () => http.post<{ ok: boolean }>(`/api/agent/session/${sessionId}/abort`),
  })
}

export interface CreateSessionVars {
  projectId?: string
  model?: string
  variant?: string | null
  agent?: string
}

export function useCreateSession() {
  const userId = useUserId()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (vars: CreateSessionVars) =>
      http.post<Session>("/api/agent/session", {
        project_id: vars.projectId,
        model: vars.model,
        variant: vars.variant,
        agent: vars.agent,
      }),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ["sessions", userId] }),
  })
}
